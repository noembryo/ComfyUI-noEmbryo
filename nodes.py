import io
import re
import json
from os.path import realpath, join, dirname

MANIFEST = {"name": "noEmbryo Nodes",
            "version": (1, 0, 3),
            "author": "noEmbryo",
            "project": "https://github.com/noembryo/ComfyUI-noEmbryo",
            "description": "Nodes for ComfyUI",
            "license": "MIT",
            }
__author__ = "noEmbryo"
__version__ = "1.0.5"

# LISTS_PATH = realpath("./custom_nodes/ComfyUI-noEmbryo/TermLists/")
LISTS_PATH = join(dirname(realpath(__file__)), "TermLists")


class PromptTermList:
    idx = 0
    data = {"None": ""}
    data_labels = []
    has_error = False
    input_error = ("Trying to store invalid input!\nUse the format:\n"
                   "label=... ...\nvalue=.... .... ...")

    def __init__(self):
        super(PromptTermList, self).__init__()
        self.name = type(self).__name__

    @classmethod
    def load_data_from_json(cls, json_file_path):
        """ Loads a json file from a path

        :type json_file_path: str
        :param json_file_path: The path to the json file
        """
        try:
            with io.open(json_file_path, mode="r", encoding="utf-8") as f:
                cls.data = json.load(f)
                cls.data_labels = list(cls.data.items())
        except FileNotFoundError:
            pass

    # noinspection PyMethodParameters
    @classmethod
    def INPUT_TYPES(cls):
        list_path = join(LISTS_PATH, f"TermList{cls.idx}.json")
        cls.load_data_from_json(list_path)
        term_list = [i[0] for i in cls.data_labels]
        return {"required": {"terms": (term_list,), },
                "optional": {"text": ("STRING", {"forceInput": True}),
                             # The round value representing the precision to round to,
                             # will be set to the step value by default.
                             # Can be set to False to disable rounding.
                             "strength": ("FLOAT", {"default": 1.0,
                                                    "min": 0.05,
                                                    "max": 2.0,
                                                    "step": 0.05,
                                                    "round": 0.01,
                                                    "display": "number"}),
                             "store_input": ("BOOLEAN", {"default": False}),
                             },
                }

    def save_data_from_input(self, text):
        """ Extracts the json values from the input text and stores them in the json file

        :type text: str
        :param text: The text input
        """
        lines = text.splitlines()
        if not len(lines) > 1:
            self.has_error = True
            print(f"{self.name}:", self.input_error)
            return
        if not all((lines[0].startswith("label="), lines[1].startswith("value="))):
            self.has_error = True
            print(f"{self.name}:", self.input_error)
            return
        label = lines[0][6:]
        value = lines[1][6:]
        if label == "None":
            print(f'{self.name}: The label "{label}" cannot be changed!')
            return
        if not value:
            if label in self.data:
                del self.data[label]
                print(f'{self.name}: The label "{label}" was deleted!')
            else:
                print(f'{self.name}: The label "{label}" does not exist!')
            return
        else:
            if label in self.data:
                print(f'{self.name}: The label "{label}" is updated!')
            else:
                print(f'{self.name}: The label "{label}" is saved!')
            self.data[label] = value
        with io.open(join(LISTS_PATH, "TermList{}.json".format(self.idx)), mode="w",
                     encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("Term",)
    # OUTPUT_NODE = True
    CATEGORY = "noEmbryo/Term Nodes"
    FUNCTION = "run"

    def run(self, terms, strength, store_input, text=None):
        selected = terms[:len(terms)]
        text_out = ""
        for i in self.data_labels:
            if i[0] == selected:
                text_out = f"{i[1]} "
                break
        if selected != "None" and strength != 1.0:
            text_out = f"({text_out}:{strength})"
        if text:
            if store_input:
                self.save_data_from_input(text)
                if not self.has_error:
                    text_out = ""
                else:
                    self.has_error = False
                    text_out = self.input_error
            else:
                if text_out:
                    text_out = f"{text_out}, {text}"
                else:
                    text_out = text
        return (text_out, )


class PromptTermList1(PromptTermList):
    idx = 1


class PromptTermList2(PromptTermList):
    idx = 2


class PromptTermList3(PromptTermList):
    idx = 3


class PromptTermList4(PromptTermList):
    idx = 4


class PromptTermList5(PromptTermList):
    idx = 5


class PromptTermList6(PromptTermList):
    idx = 6


class ResolutionScale:

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):

        return {"required": {"width": ("INT", {"default": 512}),
                             "height": ("INT", {"default": 512}),
                             "scale_factor": ("FLOAT", {"default": 2.0,
                                                        "min": 0.1,
                                                        "max": 8.0,
                                                        "step": 0.1,
                                                        "round": 0.1,
                                                        "display": "number"},),
                             },
                "optional": {"image": ("IMAGE",), },
                }

    RETURN_TYPES = ("INT", "INT", "FLOAT", "INT", "INT")
    RETURN_NAMES = ("Width", "Height", "Scale Factor",
                    "Original Width", "Original Height")

    FUNCTION = "run"
    CATEGORY = "noEmbryo"

    # noinspection PyMethodMayBeStatic
    def run(self, width, height, scale_factor, image=None):
        if image is not None:
            _, img_height, img_width, _ = image.shape
            if width == 0:
                ratio = img_width / img_height
                width = height * ratio
                width = int(width / 4) * 4
            elif height == 0:
                ratio = img_height / img_width
                height = width * ratio
                height = int(height / 4) * 4
            else:
                width = img_width
                height = img_height

        new_width = int(width * scale_factor)
        new_height = int(height * scale_factor)

        return new_width, new_height, scale_factor, width, height


class RegExTextChopper:

    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):

        return {"required": {"text": ("STRING", {"forceInput": True}),
                             "regex": ("STRING", {})
                             },
                "optional": {},
                }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("Part 1", "Part 2", "Part 3", "Part 4", "All parts")

    FUNCTION = "run"
    CATEGORY = "noEmbryo"

    @staticmethod
    def is_valid_regex(regex_from_user: str) -> bool:
        try:
            re.compile(re.escape(regex_from_user))
            is_valid = True
        except re.error:
            is_valid = False
        return is_valid

    def run(self, text, regex):
        if self.is_valid_regex(regex):
            obj = re.compile(regex, re.MULTILINE)
            result = obj.findall(text)
            try:
                text1 = result[0]
            except IndexError:
                text1 = ""
            try:
                text2 = result[1]
            except IndexError:
                text2 = ""
            try:
                text3 = result[2]
            except IndexError:
                text3 = ""
            try:
                text4 = result[3]
            except IndexError:
                text4 = ""
            text_all = "\n\n".join(result)
        else:
            text1 = text2 = text3 = text4 = ""
            text_all = text

        return text1, text2, text3, text4, text_all


NODE_CLASS_MAPPINGS = {"PromptTermList1": PromptTermList1,
                       "PromptTermList2": PromptTermList2,
                       "PromptTermList3": PromptTermList3,
                       "PromptTermList4": PromptTermList4,
                       "PromptTermList5": PromptTermList5,
                       "PromptTermList6": PromptTermList6,
                       f"Resolution Scale /{__author__}": ResolutionScale,
                       f"Regex Text Chopper /{__author__}": RegExTextChopper,
                       }

NODE_DISPLAY_NAME_MAPPINGS = {"PromptTermList1": f"PromptTermList 1 /{__author__}",
                              "PromptTermList2": f"PromptTermList 2 /{__author__}",
                              "PromptTermList3": f"PromptTermList 3 /{__author__}",
                              "PromptTermList4": f"PromptTermList 4 /{__author__}",
                              "PromptTermList5": f"PromptTermList 5 /{__author__}",
                              "PromptTermList6": f"PromptTermList 6 /{__author__}",
                              f"Resolution Scale /{__author__}":
                                  f"Resolution Scale /{__author__}",
                              f"Regex Text Chopper /{__author__}":
                                  f"Regex Text Chopper /{__author__}",
                              }
