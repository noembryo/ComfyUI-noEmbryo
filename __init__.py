"""
@author: noEmbryo
@title: noEmbryo nodes
@nickname: noEmbryo
@description: Some useful nodes for ComfyUI
"""

import json
import os
import io
from os.path import isfile, join, isdir
import importlib
import types
from aiohttp import web
from server import PromptServer

from .nodes import LISTS_PATH, JsonPromptLoader, __author__, __version__

print(f"### Loading: {__author__} nodes v{__version__}")

WEB_DIRECTORY = "./web/js"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

node_list = [
    "nodes",
    # "load_image_from_path",
]

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
            # noinspection PyTypeChecker
            json.dump({"None": ""}, f, indent=4)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS",
           "WEB_DIRECTORY", "setup",
           ]

# Add custom route for JSON loading
try:
    @PromptServer.instance.routes.get('/noembryo/get_json')
    async def get_json(request):
        path = request.query.get('path', '')
        if path:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    JsonPromptLoader.load_data(path)  # Update Python data
                    return web.json_response(data)
            except Exception as e:
                return web.json_response({"error": str(e)})
        return web.json_response({})
except ImportError:
    pass  # Server not available

# def setup():
#     @PromptServer.instance.routes.get('/noembryo/get_json')
#     async def get_json(request):
#         path = request.query.get('path', '')
#         if path:
#             try:
#                 with open(path, 'r', encoding='utf-8') as f:
#                     data = json.load(f)
#                     return web.json_response(data)
#             except Exception as e:
#                 return web.json_response({"error": str(e)})
#         return web.json_response({})
