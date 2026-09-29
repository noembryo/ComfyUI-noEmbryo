import os, re, io
import json
# import subprocess
# import tempfile
from os.path import realpath, join, dirname, isabs, splitext, basename
from datetime import datetime
import folder_paths
from .image_nodes import LoadImageFromPathEnhanced, ImageComposer
from .minimax import (H3MotionContextClipStitcher, H3ClipRefiner,
                       H3ContextLatentConverter,
                       H3MotionContextClipPurge, H3AVLatentFromVideo)

MANIFEST = {"name": "noEmbryo Nodes",
            "version": (1, 8, 2),
            "author": "noEmbryo",
            "project": "https://github.com/noembryo/ComfyUI-noEmbryo",
            "description": "Nodes for ComfyUI",
            "license": "MIT",
            }
__author__ = "noEmbryo"
__version__ = "1.8.2"

LISTS_PATH = join(dirname(realpath(__file__)), "TermLists")


class JsonPromptLoader:
    data = {}
    data_labels = ["None"]
    json_path = ""

    def __init__(self):
        super(JsonPromptLoader, self).__init__()
        self.name = type(self).__name__

    @classmethod
    def load_data(cls, json_path):
        cls.json_path = ""
        if not splitext(json_path)[1].lower() == ".json":
            return
        if json_path:
            try:
                with io.open(json_path, mode="r", encoding="utf-8") as f:
                    cls.data.clear()
                    cls.data["None"] = ""
                    cls.data.update(json.load(f))
                    cls.data_labels[:] = list(cls.data.keys())
                    cls.json_path = json_path
            except (FileNotFoundError, json.JSONDecodeError):
                cls.data.clear()
                cls.data.update({})
                cls.data_labels[:] = ["None"]
                if os.stat(json_path).st_size == 0:  # empty json files
                    cls.json_path = json_path
        else:  # no path given
            cls.data.clear()
            cls.data.update({})
            cls.data_labels[:] = ["None"]

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"json_path": ("STRING", {"default": "",
                                                      "tooltip": "Path to a JSON file with "
                                                                 "`item`:`prompt` pairs"}),
                            "selected_item": (cls.data_labels, cls.data),  # Options will be updated by JS
                            "variable": ("STRING", {"default": "{subject}",
                                                    "tooltip": "If this variable exists in the selected item's prompt,\n"
                                                               "it will be replaced with the custom_prompt text"}),
                            "custom_prompt": ("STRING", {"multiline": True, "default": "",
                                                         "tooltip": "Text to replace the variable in the selected prompt.\n"
                                                                    "You can also use it to save a new item or update an existing one.\n"
                                                                    "To do that you should use the following format:\n"
                                                                    "item=... ...\n"
                                                                    "value=.... .... ...\n"
                                                                    "To delete an existing item, use an empty value:\n"
                                                                    "item=... ...\n"
                                                                    "value="}),
                            },
                }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("Prompt",)
    FUNCTION = "run"
    CATEGORY = "noEmbryo/Prompt"
    DESCRIPTION = ("A node that can load a `.json` file with `item:prompt` pairs and outputs "
                   "the selected item's prompt, while combining it with a custom prompt.\n"
                   "It can load `.json` files from any directory, not just the node's directory.")

    def run(self, json_path, selected_item, variable, custom_prompt):
        self.load_data(json_path)
        if custom_prompt:
            message = self.edit_data(custom_prompt)
            if message:  # if the custom_prompt was saved successfully
                return (message,)
        if selected_item in self.data and selected_item != "None":
            prompt = self.data[selected_item]
            # if variable and "{" + variable + "}" in prompt:
            if variable and variable in prompt:
                prompt = prompt.replace(variable, custom_prompt)
                # prompt = prompt.replace("{" + variable + "}", custom_prompt)
            elif custom_prompt:
                prompt += " " + custom_prompt
        else:
            prompt = custom_prompt
        return (prompt,)

    def edit_data(self, text):
        """ Parses the json values from the custom_prompt and changes the json file

        :type text: str
        :param text: The custom_prompt text
        """
        lines = text.splitlines()
        if len(lines) >= 2:
            if all((lines[0].startswith("item="), lines[1].startswith("value="))):
                if not self.json_path:
                    return False
                item = lines[0][5:]
                lines_txt = "\n".join(lines[1:])
                value = lines_txt[6:]
                filename = basename(self.json_path)
                if item == "None":  # cannot change None
                    msg = f'{filename}: The item "{item}" cannot be changed!'
                    return msg
                if not value:  # delete item
                    if item in self.data:
                        del self.data[item]
                        msg = f'{filename}: The item "{item}" was deleted!'
                        self.save_json_file()
                    else:
                        msg = f'{filename}: The item "{item}" does not exist!'
                else:  # save/update item
                    if item in self.data:
                        msg = f'{filename}: The item "{item}" was updated!'
                    else:
                        msg = f'{filename}: The item "{item}" was added!'
                    self.data[item] = value
                    self.save_json_file()
                return msg
        return False

    def save_json_file(self):
        with io.open(self.json_path, mode="w", encoding="utf-8") as f:
            data = self.data.copy()
            if "None" in data:
                del data["None"]
            # noinspection PyTypeChecker
            json.dump(data, f, ensure_ascii=False, indent=4)


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

    @classmethod
    def INPUT_TYPES(cls):
        list_path = join(LISTS_PATH, f"TermList{cls.idx}.json")
        cls.load_data_from_json(list_path)
        term_list = [i[0] for i in cls.data_labels]
        # noinspection SqlNoDataSourceInspection,SqlResolve
        return {"required": {"terms": (term_list,{"tooltip": "Choose a term from the "
                                                             "TermList with the "
                                                             "corresponding number"}), },
                "optional": {"text": ("STRING", {"forceInput": True,
                                                 "tooltip": "Input text to store in the "
                                                            "TermList\nUse the format:\n"
                                                            "label=... ...\n"
                                                            "value=.... .... ..."}),
                             # The round value representing the precision to round to,
                             # will be set to the step value by default.
                             # Can be set to False to disable rounding.
                             "strength": ("FLOAT", {"default": 1.0,
                                                    "min": 0.05,
                                                    "max": 2.0,
                                                    "step": 0.05,
                                                    "round": 0.01,
                                                    "display": "number",
                                                    "tooltip": "Controls how much the "
                                                    "image is allowed to change.\n"
                                                    "0.0 = almost no change\n"
                                                    "1.0 = maximum creativity"}),
                             "store_input": ("BOOLEAN",
                                             {"default": False,
                                              "tooltip": "Store the input text in the "
                                                         "TermList\nUse the format:\n"
                                                         "label=... ...\nvalue=.... .... ..."}),
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
        lines_txt = "\n".join(lines[1:])
        value = lines_txt[6:]
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
            # noinspection PyTypeChecker
            json.dump(self.data, f, ensure_ascii=False, indent=4)

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("Term",)
    # OUTPUT_NODE = True
    CATEGORY = "noEmbryo/Prompt/Term Nodes"
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

        return {"required": {"text": ("STRING", {"forceInput": True,
                                                 "tooltip": "The text that we'll parse"}),
                             "regex": ("STRING", {"tooltip": "The RegEx pattern"})
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


class AutoSaveWorkflow:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "save_directory": ("STRING", {
                    "default": "saved_workflows",
                    "tooltip": "Relative to ComfyUI output directory or absolute path"
                }),
                "filename": ("STRING", {
                    "default": "workflow_{timestamp}",
                    "tooltip": "Filename (include {timestamp} for unique timestamps)"
                }),
                "trigger": ("BOOLEAN", {
                    "default": True,
                    "label_on": "Enabled",
                    "label_off": "Disabled",
                    "tooltip": "Save the workflow if Enabled"
                }),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "trigger": "BOOLEAN",  # Hidden trigger input
            },
        }

    RETURN_TYPES = ("STRING", "BOOLEAN")
    RETURN_NAMES = ("status", "✳️trigger")
    OUTPUT_TOOLTIPS = ("Get a status report text",
                       "Dammy output, to trigger execution if nothing is connected")
    FUNCTION = "execute"
    CATEGORY = "noEmbryo"
    OUTPUT_NODE = True

    # noinspection PyUnusedLocal
    @staticmethod
    def execute(trigger, save_directory, filename, prompt=None, extra_pnginfo=None):
        status = "Trigger disabled - workflow not saved"

        if trigger:
            try:
                workflow_data = extra_pnginfo.get("workflow", {}) if extra_pnginfo else {}

                # Process save directory
                if isabs(save_directory):
                    output_dir = save_directory
                else:
                    output_dir = join(folder_paths.get_output_directory(), save_directory)
                os.makedirs(output_dir, exist_ok=True)

                # Process filename with timestamp
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                processed_filename = filename.replace("{timestamp}", timestamp)

                # Ensure .json extension
                if not processed_filename.lower().endswith('.json'):
                    processed_filename += '.json'

                save_path = join(output_dir, processed_filename)

                # Save workflow to JSON
                with open(save_path, "w", encoding="utf-8") as f:
                    # noinspection PyTypeChecker
                    json.dump(workflow_data, f, indent=4)

                status = f"Workflow saved to: {save_path}"
            except Exception as e:
                status = f"Error saving workflow: {str(e)}"

        return (status,)


# class ReplaceAudioNoReEncode:
#     """ A minimal ComfyUI custom node that replaces the audio stream of an existing
#     video file with a new audio track, using ffmpeg's stream-copy mode for the
#     video (`-c:v copy`). The video bitstream is remuxed losslessly and is never
#     decoded/re-encoded — only the container is rewritten with a new audio stream.
#
#     Requires ffmpeg to be installed and available on PATH.
#
#     video_path : path to an existing encoded video file (e.g. output of
#                  VHS Video Combine, or any .mp4/.mov/.mkv on disk).
#     audio      : standard ComfyUI AUDIO type ({"waveform": tensor, "sample_rate": int}),
#                  e.g. from Load Audio, VHS audio output, or a generated audio node.
#     """
#
#     @classmethod
#     def INPUT_TYPES(cls):
#         return {
#             "required": {
#                 "video_path": ("STRING", {"default": "", "multiline": False,
#                                           "tooltip": "Path to the video file whose audio stream "
#                                                      "will be replaced (e.g. any .mp4/.mov/.mkv on disk)."}),
#                 "filename_prefix": ("STRING", {"default": "audio_replaced",
#                                                "tooltip": "Prefix for the output file name.\n"
#                                                           "The result is saved in the ComfyUI output "
#                                                           "directory as:\n"
#                                                           "<prefix>_<video name>_<counter>.<ext>"}),
#                 "audio_codec": (["aac", "copy"], {"default": "aac",
#                                                   "tooltip": "How to encode the new audio stream:\n"
#                                                              "• aac: re-encode to AAC 192kbps (always "
#                                                              "used when the audio comes from the AUDIO "
#                                                              "tensor input)\n"
#                                                              "• copy: remux the audio file losslessly, "
#                                                              "without re-encoding (only meaningful when "
#                                                              "using the audio_path input)"}),
#             },
#             "optional": {
#                 "audio": ("AUDIO", {"tooltip": "ComfyUI AUDIO signal (e.g. from Load Audio or a "
#                                                "generated audio node) to use as the new audio "
#                                                "stream.\nIgnored if audio_path is set."}),
#                 "audio_path": ("STRING", {"default": "", "multiline": False,
#                                           "tooltip": "Path to an audio file — or a video file, whose "
#                                                      "audio stream will be extracted — to use as the new "
#                                                      "audio stream. If set, it takes priority over the "
#                                                      "audio tensor input."}),
#                 "shortest": ("BOOLEAN", {"default": True,
#                                          "tooltip": "If enabled and the audio is shorter/longer than "
#                                                     "the video, the output is trimmed to the "
#                                                     "shorter of the two streams."}),
#             },
#             "hidden": {
#                 "prompt": "PROMPT",
#                 "extra_pnginfo": "EXTRA_PNGINFO",
#             },
#         }
#
#     DESCRIPTION = ("Replaces the audio stream of a video file without re-encoding the video. "
#                    "The new audio comes either from an AUDIO tensor input or from an audio file "
#                    "given by audio_path. Requires ffmpeg on the PATH.")
#
#     RETURN_TYPES = ("STRING",)
#     RETURN_NAMES = ("video_path",)
#     OUTPUT_TOOLTIPS = ("The path of the output video file with the replaced audio stream.",)
#     FUNCTION = "replace_audio"
#     CATEGORY = "noEmbryo"
#     OUTPUT_NODE = True
#
#     @staticmethod
#     def _ffm_escape(text):
#         """ Escapes a string for use as a value in an ffmetadata file """
#         for ch in ("\\", "=", ";", "#", "\n"):
#             text = text.replace(ch, "\\" + ch) if ch != "\n" else text.replace(ch, r"\n")
#         return text
#
#     @staticmethod
#     def write_wave_file(wave_path, waveform, sample_rate):
#         """ Writes a waveform tensor to a wav file, using only the standard library
#         """
#         import wave
#         import numpy as np
#         if waveform.dim() == 1:  # [samples] -> [1, samples]
#             waveform = waveform.unsqueeze(0)
#         # [channels, samples] -> [samples, channels]
#         samples = waveform.cpu().numpy().T
#         samples = np.clip(samples, -1.0, 1.0)
#         pcm = (samples * 32767.0).astype(np.int16)
#         with wave.open(wave_path, "wb") as wf:
#             wf.setnchannels(pcm.shape[1])
#             wf.setsampwidth(2)  # 2 bytes = 16 bit
#             wf.setframerate(sample_rate)
#             wf.writeframes(pcm.tobytes())
#
#     def replace_audio(self, video_path, filename_prefix, audio_codec,
#                       audio=None, audio_path="", shortest=True,
#                       prompt=None, extra_pnginfo=None):
#         if not video_path or not os.path.isfile(video_path):
#             raise FileNotFoundError(f"Video file not found: {video_path!r}")
#
#         output_dir = folder_paths.get_output_directory()
#         os.makedirs(output_dir, exist_ok=True)
#
#         tmp_audio_path = None
#         if audio_path:
#             if not os.path.isfile(audio_path):
#                 raise FileNotFoundError(f"Audio file not found: {audio_path!r}")
#             second_input = audio_path
#         elif audio is not None:
#             # --- Write the incoming AUDIO tensor to a temp wav file ---
#             waveform = audio["waveform"]
#             sample_rate = audio["sample_rate"]
#             if waveform.dim() == 3:  # [batch, channels, samples] -> take first item
#                 waveform = waveform[0]
#             tmp_audio_fd, tmp_audio_path = tempfile.mkstemp(suffix=".wav")
#             os.close(tmp_audio_fd)
#             self.write_wave_file(tmp_audio_path, waveform, sample_rate)
#             second_input = tmp_audio_path
#             # Copying raw PCM into a container makes no sense, so force aac
#             audio_codec = "aac"
#         else:
#             raise ValueError("No audio given: connect an AUDIO input or set audio_path.")
#
#         # --- Build a unique output path ---
#         base_name = os.path.splitext(os.path.basename(video_path))[0]
#         ext = os.path.splitext(video_path)[1] or ".mp4"
#         # Start from max existing number + 1, so deleted files don't cause name reuse.
#         # The prefix may contain subdirectories (e.g. "MMH3\NewAudio"), so the scan
#         # must look in the directory the files are actually written to.
#         out_path = os.path.join(output_dir, f"{filename_prefix}_{base_name}_001{ext}")
#         scan_dir = os.path.dirname(out_path)
#         os.makedirs(scan_dir, exist_ok=True)
#         # listdir() returns bare filenames, so only the last component of the
#         # prefix (without the directory part) can appear in them
#         prefix_name = os.path.basename(filename_prefix.replace("\\", "/"))
#         counter = 1
#         pattern = re.compile(rf"^{re.escape(prefix_name)}_{re.escape(base_name)}"
#                              rf"_(\d+){re.escape(ext)}$")
#         for fname in os.listdir(scan_dir):
#             m = pattern.match(fname)
#             if m:
#                 counter = max(counter, int(m.group(1)) + 1)
#         out_name = f"{filename_prefix}_{base_name}_{counter:03d}{ext}"
#         out_path = os.path.join(output_dir, out_name)
#
#         # --- Write the workflow metadata to a temp ffmetadata file ---
#         # (avoids Windows command-line length limits that -metadata args would hit)
#         meta_fd, meta_path = tempfile.mkstemp(suffix=".txt")
#         os.close(meta_fd)
#         with io.open(meta_path, "w", encoding="utf-8") as mf:
#             mf.write(";FFMETADATA1\n")
#             if prompt is not None:
#                 mf.write(f"prompt={self._ffm_escape(json.dumps(prompt))}\n")
#             if extra_pnginfo and "workflow" in extra_pnginfo:
#                 mf.write(f"workflow={self._ffm_escape(json.dumps(extra_pnginfo['workflow']))}\n")
#
#         # --- ffmpeg: stream-copy the video, only touch the audio ---
#         cmd = [
#             "ffmpeg", "-y",
#             "-i", video_path,
#             "-i", second_input,
#             "-i", meta_path,
#             "-map", "0:v:0",
#             "-map", "1:a:0",
#             "-map_metadata", "2",
#             "-c:v", "copy",
#         ]
#         if audio_codec == "copy":
#             cmd += ["-c:a", "copy"]
#         else:
#             cmd += ["-c:a", "aac", "-b:a", "192k"]
#         # allow arbitrary metadata keys in these containers
#         if ext.lower() in (".mp4", ".mov"):
#             cmd += ["-movflags", "use_metadata_tags"]
#         if shortest:
#             cmd.append("-shortest")
#         cmd.append(out_path)
#
#         def run_ffmpeg(command):
#             return subprocess.run(command, capture_output=True, text=True)
#
#         try:
#             result = run_ffmpeg(cmd)
#             if result.returncode != 0 and audio_codec == "copy":
#                 # "copy" can fail when the source audio codec is incompatible with
#                 # the output container (e.g. PCM in an AVI -> mp4). Retry with aac.
#                 fallback_cmd = list(cmd)
#                 for i, arg in enumerate(fallback_cmd):
#                     if arg == "-c:a" and fallback_cmd[i + 1] == "copy":
#                         fallback_cmd[i + 1] = "aac"
#                 result = run_ffmpeg(fallback_cmd)
#             if result.returncode != 0:
#                 raise RuntimeError(f"ffmpeg failed (exit {result.returncode}):\n{result.stderr}")
#         finally:
#             for tmp in (tmp_audio_path, meta_path):
#                 if tmp and os.path.exists(tmp):
#                     os.remove(tmp)
#
#         return (out_path,)


NODE_CLASS_MAPPINGS = {f"JsonPromptLoader -{__author__}": JsonPromptLoader,
                       f"Resolution Scale -{__author__}": ResolutionScale,
                       f"Regex Text Chopper -{__author__}": RegExTextChopper,
                       f"Auto Save Workflow -{__author__}": AutoSaveWorkflow,
                       f"Load Image (from path) -{__author__}": LoadImageFromPathEnhanced,
                       f"Image Composer -{__author__}": ImageComposer,
                        f"H3MotionContextClipStitcher -{__author__}": H3MotionContextClipStitcher,
                        f"H3ClipRefiner -{__author__}": H3ClipRefiner,
                       f"H3MotionContextClipPurge -{__author__}": H3MotionContextClipPurge,
                       f"H3ContextLatentConverter -{__author__}": H3ContextLatentConverter,
                       f"H3AVLatentFromVideo -{__author__}": H3AVLatentFromVideo,
                       # f"ReplaceAudioNoReEncode -{__author__}": ReplaceAudioNoReEncode,
                       "PromptTermList1": PromptTermList1,
                       "PromptTermList2": PromptTermList2,
                       "PromptTermList3": PromptTermList3,
                       "PromptTermList4": PromptTermList4,
                       "PromptTermList5": PromptTermList5,
                       "PromptTermList6": PromptTermList6,
                       }

NODE_DISPLAY_NAME_MAPPINGS = {f"JsonPromptLoader -{__author__}": f"Json Prompt Loader /{__author__}",
                              f"Resolution Scale -{__author__}": f"Resolution Scale /{__author__}",
                              f"Regex Text Chopper -{__author__}": f"Regex Text Chopper /{__author__}",
                              f"Auto Save Workflow -{__author__}": f"Auto Save Workflow /{__author__}",
                              f"Load Image (from path) -{__author__}": f"Load Image (from path) /{__author__}",
                              f"Image Composer -{__author__}": f"Image Composer /{__author__}",
                               f"H3MotionContextClipStitcher -{__author__}": f"H3 Motion Context Clip Stitcher /{__author__}",
                               f"H3ClipRefiner -{__author__}": f"H3 Clip Refiner /{__author__}",
                              f"H3MotionContextClipPurge -{__author__}": f"H3 Motion Context Clip Purge /{__author__}",
                              f"H3ContextLatentConverter -{__author__}": f"H3 Context Latent Converter /{__author__}",
                              f"H3AVLatentFromVideo -{__author__}": f"H3 AV Latent from Video /{__author__}",
                              # f"ReplaceAudioNoReEncode -{__author__}": f"Replace Audio no ReEncode /{__author__}",
                              "PromptTermList1": f"PromptTermList 1 /{__author__}",
                              "PromptTermList2": f"PromptTermList 2 /{__author__}",
                              "PromptTermList3": f"PromptTermList 3 /{__author__}",
                              "PromptTermList4": f"PromptTermList 4 /{__author__}",
                              "PromptTermList5": f"PromptTermList 5 /{__author__}",
                              "PromptTermList6": f"PromptTermList 6 /{__author__}",
                              }
