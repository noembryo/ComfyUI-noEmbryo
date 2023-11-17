__author__ = "noEmbryo"
__version__ = "0.0.1.0"


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
                "optional": {"image": ("IMAGE",),},
                }

    RETURN_TYPES = ("INT", "INT", "FLOAT", "INT", "INT")
    RETURN_NAMES = ("WIDTH", "HEIGHT", "SCALE_FACTOR",
                    "ORIGINAL_WIDTH", "ORIGINAL_HEIGHT")

    FUNCTION = "run"
    CATEGORY = "noEmbryo"

    def run(self, width, height, scale_factor, image=None):
        if image is not None:
            _, height, width, _ = image.shape

        new_width = int(width * scale_factor)
        new_height = int(height * scale_factor)

        return new_width, new_height, scale_factor, width, height


NODE_CLASS_MAPPINGS = {f"Resolution Scale /{__author__}": ResolutionScale}

NODE_DISPLAY_NAME_MAPPINGS = {f"Resolution Scale /{__author__}":
                              f"Resolution Scale /{__author__}"}
