"""
@author: noEmbryo
@title: noEmbryo nodes for ComfyUI
@nickname: noEmbryo
@description: Some useful nodes for ComfyUI
"""

import json
import os, io
from os.path import isfile, join, isdir
import importlib
from .nodes import LISTS_PATH, __author__, __version__

print(f"### Loading: {__author__} nodes v{__version__}")

node_list = [
    "nodes",
    # "resolution_scale",
    # "regex_text_chopper",
    # "load_image_from_dir",
]

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

for module_name in node_list:
    imported_module = importlib.import_module(".{}".format(module_name), __name__)

    NODE_CLASS_MAPPINGS = {**NODE_CLASS_MAPPINGS,
                           **imported_module.NODE_CLASS_MAPPINGS}
    NODE_DISPLAY_NAME_MAPPINGS = {**NODE_DISPLAY_NAME_MAPPINGS,
                                  **imported_module.NODE_DISPLAY_NAME_MAPPINGS}

os.makedirs(LISTS_PATH) if not isdir(LISTS_PATH) else None
for i in range(6):  # create TermList1-6 if not exists
    file_name = join(LISTS_PATH, "TermList{}.json".format(i + 1))
    if not isfile(file_name):
        with io.open(file_name, "w+", encoding="utf-8", newline="\n") as f:
            json.dump({"None": ""}, f, indent=4)

# WEB_DIRECTORY = "./js"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS",
           # "WEB_DIRECTORY"
           ]
