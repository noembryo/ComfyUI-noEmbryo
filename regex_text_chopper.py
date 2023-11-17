import re

__author__ = "noEmbryo"
__version__ = "0.0.1.0"


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


NODE_CLASS_MAPPINGS = {f"Regex Text Chopper /{__author__}": RegExTextChopper}

NODE_DISPLAY_NAME_MAPPINGS = {f"Regex Text Chopper /{__author__}":
                              f"Regex Text Chopper /{__author__}"}
