"""
@author: noEmbryo
@title: X Node
@nickname: noEmbryo
@description: Some useful nodes.
"""

import json
import os, io
from os.path import isfile, join, isdir

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS, LISTS_PATH

os.makedirs(LISTS_PATH) if not isdir(LISTS_PATH) else None
for i in range(len(NODE_CLASS_MAPPINGS)):
    file_name = join(LISTS_PATH, "TermList{}.json".format(i + 1))
    if not isfile(file_name):
        with io.open(file_name, "w+", encoding="utf-8", newline="\n") as f:
            json.dump({"None": ""}, f, indent=4)

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']
