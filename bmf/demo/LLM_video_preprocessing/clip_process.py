#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import bmf
import os
import json
from pathlib import Path

logger = logging.getLogger("main")

FIXED_RES = 480

def prepare_dir(base_path):
    Path(base_path).mkdir(parents=True, exist_ok=True)

def get_operator_tmp_result_path(base_path, operator_name):
    file_name = f"{operator_name}_result.json"
    return os.path.join(base_path, file_name)


def get_operator_result_path(base_path, operator_name, clip_index):
    file_name = f"clip_{clip_index}_{operator_name}_result.json"
    return os.path.join(base_path, file_name)


def get_jpg_serial_path(base_path, clip_index, fixed_width):
    img_path = f"clip_{clip_index}_{fixed_width}_img"
    return os.path.join(
        base_path, img_path, "clip_{}_{}_img_%04d.jpg".format(clip_index, fixed_width)
    )

def get_jpg_dir_path(base_path, clip_index, fixed_width):
    img_path = f"clip_{clip_index}_{fixed_width}_img"
    return os.path.join(base_path, img_path)

def get_clip_path(base_path, clip_index, fixed_width):
    file_name = "clip_{}_{}.mp4".format(clip_index, fixed_width)
    return os.path.join(base_path, file_name)

class ClipProcess:
    def __init__(self, input_file, timelines, modes, config):
        self.input_file = input_file
        self.timelines = timelines
        self.config = config
        self.output_path = config.get("output_path", "clip_output")
        self.output_configs = config.get("output_configs", {})

        operator_options = []
        for operator_name in modes:
            operator_config = config.get(operator_name, {})
            # check if model path is valid, defaults to searching in current directory
            if "module_path" not in operator_config:
                operator_config["module_path"] = '.'
            if not os.path.exists(operator_config["module_path"]):
                print(f"Module {operator_name} not found in {operator_config['module_path']}... skipping")
                continue

            # create empty dict if not exists
            operator_config["options"] = operator_config.get("options", {})
            # ASSUMES that result_path is config file for output
            # if not, needs to be set manually in json file
            operator_config["options"]["result_path"] = get_operator_tmp_result_path(self.output_path, operator_name)

            # add its name 
            operator_config["module_name"] = operator_name
            # add default entry point
            operator_config["entry"] = operator_config.get("entry", "")
            operator_options.append(operator_config)

        logger.info(f"operator_options: {operator_options}")
        self.operator_options = operator_options
        prepare_dir(self.output_path)

    def operator_process(self, timeline):
        if len(self.operator_options) == 0:
            return True

        decode_param = {}
        decode_param["input_path"] = self.input_file
        decode_param["dec_params"] = {"threads": "4"}
        decode_param["durations"] = timeline
        graph = bmf.graph()
        v = graph.decode(decode_param)["video"]
        v = v.scale(
            f"if(lt(iw,ih),min({FIXED_RES},iw),-2):if(lt(iw,ih),-2,min({FIXED_RES},ih))"
        ).ff_filter("setsar", sar="1/1")
        for operator_option in self.operator_options:
            pre_module_argument = None
            if operator_option["pre_module"]:
                pre_module_argument = self.operator_premodules[operator_option["module_name"]]
            v = v.module(
                operator_option["module_name"],
                module_path=operator_option["module_path"],
                option=operator_option["options"],
                entry=operator_option["entry"],
                pre_module=pre_module_argument
            )
        pkts = v.start()
        count = 0
        for _, pkt in enumerate(pkts):
            if pkt.is_(bmf.VideoFrame):
                count += 1
        logger.info(f"operator process get videoframe count: {count}")
        return count > 0

    def process_one_clip(self, timeline, clip_index):
        passthrough = self.operator_process(timeline)
        if not passthrough:
            return

        if len(self.output_configs) == 0:
            return

        for output in self.output_configs:
            if output["type"] == "jpg":
                res = output["res"]
                img_dir = get_jpg_dir_path(self.output_path, clip_index, res)
                prepare_dir(img_dir)

        decode_param = {}
        decode_param["input_path"] = self.input_file
        decode_param["dec_params"] = {"threads": "4"}
        decode_param["durations"] = timeline
        graph = bmf.graph({"optimize_graph": False})
        v = graph.decode(decode_param)["video"]

        for operator_option in self.operator_options:
            operator_name = operator_option["module_name"]
            if operator_name == "ocr_crop":
                result_path = get_operator_tmp_result_path(
                    self.output_path, operator_name
                )
                if not os.path.exists(result_path):
                    continue
                with open(result_path, "r") as f:
                    operator_res = json.load(f)
                if (
                    "result" in operator_res
                    and "nms_crop_box" in operator_res["result"]
                ):
                    nms_crop_box = operator_res["result"]["nms_crop_box"]
                    left, top, right, bottom = nms_crop_box
                    v = v.ff_filter(
                        "crop",
                        f"w=iw*{right - left}:h=ih*{bottom - top}:x=iw*{left}:y=ih*{top}",
                    )
        self.bmf_output(v, self.output_configs, clip_index)
        graph.run()

    def process_clip_result(self, clip_index):
        for operator_option in self.operator_options:
            operator_name = operator_option["module_name"]
            tmp_path = get_operator_tmp_result_path(self.output_path, operator_name)
            if os.path.exists(tmp_path):
                os.rename(
                    tmp_path,
                    get_operator_result_path(self.output_path, operator_name, clip_index),
                )

    def process(self):
        # create premodule
        operator_premodules = {}
        for op_option in self.operator_options:
            if "pre_module" in op_option and op_option["pre_module"]:
                operator_premodule = bmf.create_module(
                    op_option["module_name"],
                    option=op_option["options"]
                )
                operator_premodules[op_option["module_name"]] = operator_premodule

        self.operator_premodules = operator_premodules
        for i in range(len(self.timelines)):
            timeline = self.timelines[i]
            self.process_one_clip(timeline, i)
            self.process_clip_result(i)

    def bmf_output(self, stream, configs, clip_index):
        if len(configs) == 0:
            return
        s = stream
        resize_streams = dict()

        for c in configs:
            o = s
            res_str = c["res"]
            if res_str != "orig":
                stream_key = res_str
                if stream_key not in resize_streams:
                    res = int(res_str)
                    o = o.scale(
                        "if(lt(iw,ih),{},-2):if(lt(iw,ih),-2,{})".format(res, res)
                    ).ff_filter("setsar", sar="1/1")
                    resize_streams[stream_key] = o
                else:
                    # get saved resized stream
                    o = resize_streams[stream_key]

            elif "limit" in c:
                res = int(c["limit"])
                o = o.scale(
                    f"if(lt(iw,ih),min({res},iw),-2):if(lt(iw,ih),-2,min({res},ih))"
                ).ff_filter("setsar", sar="1/1")

            # encode
            if c["type"] == "jpg":
                bmf.encode(
                    o,
                    None,
                    {
                        "output_path": get_jpg_serial_path(
                            self.output_path, clip_index, c["res"]
                        ),
                        "video_params": {
                            "vsync": "vfr",
                            "codec": "jpg",
                            "qscale": int(c["quality"]) if "quality" in c else 2,
                            "pix_fmt": "yuvj444p",
                            "threads": "4",
                        },
                        "format": "image2",
                    },
                )

            elif c["type"] == "mp4":
                bmf.encode(
                    o,
                    None,
                    {
                        "output_path": get_clip_path(self.output_path, clip_index, c["res"]),
                        "video_params": {
                            "vsync": "vfr",
                            "codec": "h264",
                            "preset": "veryfast",
                            "threads": "4",
                        },
                    },
                )
