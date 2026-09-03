"""H3 Motion Context clip stitcher for ComfyUI.

Loads NikoDemon80/ComfyUI-H3-Motion-Context clip archive files (h3_motion_context_av_v1),
decodes each approved clip once, crossfades the carried Motion Context head from
clips, and concatenates the picture/audio into one IMAGE + AUDIO pair.

This intentionally does NOT reconstruct a NestedTensor and feed the saved files back
into Motion Context.
The archive format is the sampler output, and this node is a final-media assembly tool.
"""

import fnmatch
import glob
import logging
import os
import re

import torch
import torch.nn.functional as F
import folder_paths
from comfy.utils import ProgressBar

try:
    from comfy_execution.graph_utils import get_original_node_id
except Exception:
    get_original_node_id = None

try:
    from safetensors.torch import load_file as st_load
except Exception:
    st_load = None

try:
    import torchaudio
except Exception:
    torchaudio = None

log_ = logging.getLogger("h3_motion_context_clip_stitcher")


def _resolve_folder(path):
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        p = "h3_context"
    candidates = [p, os.path.join(folder_paths.get_output_directory(), p)]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    raise FileNotFoundError("H3 Motion Context Clip Stitcher: folder not found: %s\n"
                            "You can use an absolute path or a path relative to "
                            "ComfyUI's output folder." % p)


def _clip_number(path):
    name = os.path.basename(path)
    # noinspection RegExpUnnecessaryNonCapturingGroup
    pat = re.compile(r"(?:^|_)(\d{5})(?:\.safetensors)$", re.IGNORECASE)
    m = pat.search(name)
    return int(m.group(1)) if m else -1


def _find_files(folder, pattern, first_clip, last_clip):
    pattern = (pattern or "clip_*.safetensors").strip()
    paths = []
    for p in glob.glob(os.path.join(folder, pattern)):
        if not os.path.isfile(p):
            continue
        if not p.lower().endswith(".safetensors"):
            continue
        idx = _clip_number(p)
        if idx < 0:
            continue
        if idx < int(first_clip):
            continue
        if 0 < int(last_clip) < idx:
            continue
        paths.append((idx, p))
    paths.sort(key=lambda x: x[0])
    if not paths:
        raise FileNotFoundError("H3 Motion Context Clip Stitcher: no numbered "
                                ".safetensors files matched '%s' in %s."
                                % (pattern, folder))

    # Do not silently skip a missing numbered clip. A gap usually means an
    # approved clip was not saved, and silently stitching around it would make
    # a misleading final timeline.
    expected = paths[0][0]
    for idx, _ in paths:
        if idx != expected:
            raise ValueError("H3 Motion Context Clip Stitcher: missing clip %05d between "
                             "the selected archive files." % expected)
        expected += 1
    return paths


def _load_archive(path):
    if st_load is None:
        raise RuntimeError("safetensors is unavailable in this "
                           "ComfyUI Python environment.")
    # noinspection PyCallingNonCallable
    data = st_load(path, device="cpu")
    if "video" not in data or "audio" not in data:
        raise ValueError("%s is not an h3_motion_context_av_v1 archive: "
                         "expected 'video' and 'audio'." % path)
    video = data["video"]
    audio = data["audio"]
    if video.ndim != 5:
        raise ValueError("%s: expected video [B,C,T,H,W], got %s"
                         % (path, tuple(video.shape)))
    if audio.ndim != 4:
        raise ValueError("%s: expected audio [B,C,2,T], got %s"
                         % (path, tuple(audio.shape)))
    if video.shape[0] != 1 or audio.shape[0] != 1:
        raise ValueError("%s: only batch size 1 archive clips are supported." % path)
    return video, audio


def _decode_video(vae, video_latent):
    """ Decode the H3 video stream and normalize to ComfyUI IMAGE format.
    """
    images = vae.decode(video_latent)
    # H3's VAE normally returns [B,T,H,W,C]. Some VAE implementations can
    # return [T,H,W,C], so accept both.
    if images.ndim == 5:
        images = images.reshape(-1, *images.shape[-3:])
    elif images.ndim != 4:
        raise RuntimeError("H3 video VAE returned unexpected shape %s"
                           % (tuple(images.shape),))
    return images.to(torch.float32).clamp(0, 1).cpu()


def _decode_audio(audio_vae, audio_latent):
    """ Decode the H3 audio stream using the same convention as ComfyUI's VAEDecodeAudio.
    """
    audio = audio_vae.decode(audio_latent)
    # Current ComfyUI audio VAE returns [B,L,C]. Convert to [B,C,L].
    if audio.ndim != 3:
        raise RuntimeError("H3 audio VAE returned unexpected shape %s" % (tuple(images.shape),))
    audio = audio.movedim(-1, 1)
    sr = int(getattr(audio_vae, "audio_sample_rate_output",
                     getattr(audio_vae, "audio_sample_rate", 32000)))
    return {"waveform": audio.to(torch.float32).cpu(), "sample_rate": sr}


def _resample_audio(audio, target_sr):
    if audio is None:
        return None
    sr = int(audio["sample_rate"])
    if sr == int(target_sr):
        return audio
    if torchaudio is None:
        raise RuntimeError("Audio sample rates differ (%d vs %d), but torchaudio is "
                           "unavailable to resample them." % (sr, int(target_sr)))
    # noinspection PyUnresolvedReferences
    waveform = torchaudio.functional.resample(audio["waveform"], sr, int(target_sr))
    return {"waveform": waveform, "sample_rate": int(target_sr)}


def _crossfade_boundary(prev_tail_images, cur_images, prev_tail_wave, cur_wave,
                        overlap_frames, cross_samples):
    """ Crossfade the previous clip's tail with the current clip's head.

    prev_tail_images: [L,H,W,C]  cur_images: [T,H,W,C]
    prev_tail_wave  : [1,C,Ls]   cur_wave: [1,C,Cs]  (or None)
    Returns (blend_images [L,H,W,C], blend_wave [1,C,Ls] or None).

    Video uses a linear dissolve ramp; audio uses an equal-power (cos/sin)
    ramp over the same time window so picture and sound stay in sync.
    """
    L = int(overlap_frames)
    if L <= 0:
        return cur_images[:0], None
    if L == 1:
        alpha = torch.full((1, 1, 1, 1), 0.5, dtype=prev_tail_images.dtype,
                           device=prev_tail_images.device)
    else:
        alpha = torch.linspace(0.0, 1.0, L, dtype=prev_tail_images.dtype,
                               device=prev_tail_images.device).view(L, 1, 1, 1)
    blend_images = prev_tail_images * (1.0 - alpha) + cur_images[:L] * alpha

    blend_wave = None
    if prev_tail_wave is not None and cur_wave is not None:
        n = int(cross_samples)
        if n <= 0:
            blend_wave = prev_tail_wave
        else:
            n = min(n, int(prev_tail_wave.shape[-1]), int(cur_wave.shape[-1]))
            theta = torch.linspace(0.0, 1.5707963267948966, n, dtype=prev_tail_wave.dtype,
                                   device=prev_tail_wave.device).view(1, 1, n)
            blend_wave = (prev_tail_wave[..., :n] * torch.cos(theta)
                          + cur_wave[..., :n] * torch.sin(theta))
    return blend_images, blend_wave


def _av_from_live_latent(latent):
    """ Extract (video, audio) tensors from an in-memory H3 AV LATENT,
    using the same unpacking convention as NikoDemon80's own
    _streams_from_latent()/save(): latent["samples"] is a NestedTensor
    (or tuple/list) whose unbind() gives (video, audio) in that order.
    """
    if not isinstance(latent, dict) or "samples" not in latent:
        raise ValueError("h3_motion_context: expected a MiniMax H3 AV latent dict with "
                         "a 'samples' key, got %r" % type(latent))
    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError("h3_motion_context: expected a MiniMax H3 AV latent (a nested "
                         "video/audio pair), got %r" % type(samples))
    if len(parts) < 2:
        raise ValueError("h3_motion_context: latent has no audio stream; wire the "
                         "sampler output of an H3 AV graph.")
    # NestedTensor.unbind() returns views into the packed underlying storage.
    # Passing such views (or tensors still carrying nested metadata) to a VAE's
    # CUDA kernels can trigger cudaErrorIllegalAddress. Force a real, dense,
    # detached CPU copy of each stream before handing them to the VAE.
    video = parts[0].detach().to("cpu", copy=True).contiguous()
    audio = parts[1].detach().to("cpu", copy=True).contiguous()
    # Live streams can carry the same shapes as the archive files (video
    # [B,C,T,H,W] or [C,T,H,W]; audio [B,C,2,T] or [B,L,C]).
    expected_ndim = {"video": (4, 5), "audio": (3, 4)}
    for name, t in (("video", video), ("audio", audio)):
        if t.ndim not in expected_ndim[name]:
            raise ValueError("h3_motion_context: live %s stream has unexpected "
                             "shape %s." % (name, tuple(t.shape)))
        if not torch.is_floating_point(t):
            raise ValueError("h3_motion_context: live %s stream is not a float "
                             "tensor (dtype %s)." % (name, t.dtype))
    return video, audio


class H3MotionContextClipStitcher:
    """ Load, decode, and crossfade approved H3 Motion Context clips.
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"folder": ("STRING", {"default": "h3_context",
            "tooltip": "Folder containing clip_00001.safetensors, "
                       "clip_00002.safetensors, etc.\nAbsolute paths and paths relative "
                       "to ComfyUI/output are accepted."}),
            "pattern": ("STRING", {"default": "clip_*.safetensors",
                "tooltip": "Filename glob. The final five-digit number is treated as "
                           "the clip index."}),
            "first_clip": ("INT", {"default": 1, "min": 1, "max": 9999,
                "tooltip": "First approved clip to include."}),
             "last_clip": ("INT", {"default": 0, "min": 0, "max": 9999,
                "tooltip": "Last clip to include. 0 = every clip from first_clip onward."}),
            "context_length": (["5", "22", "39", "56"], {"default": "22",
                "tooltip": "Number of decoded frames to crossfade at each clip boundary. "
                           "The normal setting is 22 frames.\n"
                           "This is the overlap length that is dissolved between "
                           "adjacent clips.\n"
                           "5, 22, 39 or 56 are the lengths that are a whole number of "
                           "latent steps, which is why other numbers aren't offered."}),
            "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                "tooltip": "H3 native output rate. Keep this at 24 unless your workflow "
                           "deliberately changes it."}), },
            "optional": {"video_vae": ("VAE", {"tooltip": "MiniMax H3 video VAE "
                                                          "(FP16 or INT8 ConvRot)."}),
                "audio_vae": ("VAE", {
                    "tooltip": "MiniMax H3 audio VAE FP32. Required for the AUDIO "
                               "output."}),
                "latent": ("LATENT", {
                    "tooltip": "Optional: the currently-generated AV latent (from your "
                               "H3 sampler), used in place of the highest-numbered file "
                               "on disk."}),
                         },
                }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT", "STRING")
    RETURN_NAMES = ("images", "audio", "frame_count", "report")
    FUNCTION = "stitch"
    CATEGORY = "noEmbryo"
    DESCRIPTION = ("Final assembly for NikoDemon80's H3 Motion Context AV clip archives.\n"
                   "Loads numbered h3_motion_context_av_v1 files, decodes one clip at a "
                   "time, dissolves the overlap between adjacent clips (video + synchronized audio), "
                   "and concatenates them to a final video and audio stream.")

    # noinspection PyUnusedLocal
    @classmethod
    def IS_CHANGED(cls, folder, pattern, first_clip, last_clip, context_length, fps,
                   video_vae=None, audio_vae=None, latent=None):
        # noinspection PyBroadException
        try:
            d = _resolve_folder(folder)
            files = _find_files(d, pattern, first_clip, last_clip)
            # noinspection PyTypeChecker
            return tuple((p, os.stat(p).st_mtime_ns, os.path.getsize(p))
                         for _, p in files) + (int(context_length), float(fps),)
        except Exception:
            return float("NaN")

    @staticmethod
    def stitch(folder, pattern, first_clip, last_clip, context_length, fps,
               video_vae=None, audio_vae=None, latent=None,):
        if video_vae is None:
            raise ValueError("Connect your MiniMax H3 video VAE to 'video_vae'.")
        if st_load is None:
            raise RuntimeError("safetensors is not available in this ComfyUI environment")

        d = _resolve_folder(folder)
        files = _find_files(d, pattern, first_clip, last_clip)
        live_entry = None

        if latent is not None:
            if files:
                files = files[:-1]  # drop the presumed-duplicate on-disk file
                live_index = files[-1][0] + 1 if files else int(first_clip)
            else:
                live_index = int(first_clip)
            video_latent, audio_latent = _av_from_live_latent(latent)
            live_entry = (live_index, None, video_latent, audio_latent)  # path=None marks it as live

        image_parts = []
        audio_parts = []
        report_lines = []
        target_sr = None

        overlap = int(context_length)

        prev_tail_img = None
        prev_tail_wave = None

        all_entries = [(idx, path, None, None) for idx, path in files]
        if live_entry is not None:
            # noinspection PyTypeChecker
            all_entries.append(live_entry)

        total_count = len(files) + (1 if live_entry is not None else 0)
        # noinspection PyCallingNonCallable
        pbar = ProgressBar(total_count, node_id=get_original_node_id()
                           if get_original_node_id is not None else None)
        log_.info("H3 clip stitcher: %d clip(s) selected from %s", total_count, d)

        # Degenerate crossfade (no overlap or a single clip) falls back to a
        # plain concatenation, which is exactly what the trim modes would do.
        crossfade_active = overlap > 0 and len(all_entries) > 1

        for pos, (idx, path, live_video, live_audio) in enumerate(all_entries):
            if path is not None:
                video_latent, audio_latent = _load_archive(path)
            else:
                video_latent, audio_latent = live_video, live_audio
                log_.info("H3 clip stitcher: clip %05d taken from live latent input",
                          idx)

            # Decode one clip at a time. The decoded result is immediately moved
            # to CPU, so a long chain does not keep every VAE result on VRAM.
            images = _decode_video(video_vae, video_latent)
            del video_latent

            audio = None
            if audio_vae is not None:
                audio = _decode_audio(audio_vae, audio_latent,
                                      # normalize=normalize_audio_per_clip
                                      )
            del audio_latent

            decoded_frames = int(images.shape[0])
            is_last = pos == len(all_entries) - 1

            if not crossfade_active:
                # Single clip (or zero overlap): no boundaries to blend, just
                # emit the whole decoded clip and finish.
                if audio is not None and target_sr is None:
                    target_sr = int(audio["sample_rate"])
                image_parts.append(images)
                if audio is not None:
                    audio_parts.append(audio["waveform"])
                report_lines.append("clip_%05d: decoded=%d frames, no crossfade "
                                    "(single clip), audio=%.4fs"
                                    % (idx, decoded_frames, 0.0
                                    if audio is None else audio["waveform"].shape[-1]
                                                          / float(audio["sample_rate"])))
                pbar.update_absolute(pos + 1, total_count)
                del images
                if audio is not None:
                    del audio
                continue

            if crossfade_active:
                if decoded_frames < 2 * overlap:
                    raise ValueError("Crossfade requires each clip to have at least "
                                     "2*context_length (%d) frames; clip %05d has %d."
                                     % (overlap, idx, decoded_frames))

                # Resample this clip's audio to the shared target rate before
                # splitting, so the head/tail sample counts line up across clips.
                if audio is not None:
                    if target_sr is None:
                        target_sr = int(audio["sample_rate"])
                    audio = _resample_audio(audio, target_sr)
                if prev_tail_wave is not None:
                    prev_tail_wave = _resample_audio(prev_tail_wave, target_sr)

                n = 0
                if audio is not None:
                    sr = int(audio["sample_rate"])
                    n = int(round((overlap / float(fps)) * sr))
                    if n <= 0:
                        n = 1
                    if n >= audio["waveform"].shape[-1]:
                        raise ValueError("Audio is too short to extract a %d-frame "
                                         "(%0.4fs) crossfade head/tail for clip %05d."
                                         % (overlap, overlap / float(fps), idx))

                head_img = images[:overlap]
                body_img = images[overlap:-overlap]
                tail_img = images[-overlap:]

                head_wave = body_wave = tail_wave = None
                if audio is not None:
                    wave = audio["waveform"]
                    sr = int(audio["sample_rate"])
                    head_wave = {"waveform": wave[..., :n], "sample_rate": sr}
                    body_wave = {"waveform": wave[..., n:-n], "sample_rate": sr}
                    tail_wave = {"waveform": wave[..., -n:], "sample_rate": sr}

                if pos == 0:
                    # First clip: emit head+body raw, buffer the tail for the next boundary.
                    image_parts.append(torch.cat([head_img, body_img], dim=0))
                    if audio is not None:
                        audio_parts.append(torch.cat([head_wave["waveform"],
                                                      body_wave["waveform"]], dim=-1))
                    prev_tail_img = tail_img
                    prev_tail_wave = tail_wave
                else:
                    blend_img, blend_wave = _crossfade_boundary(prev_tail_img, images,
                        prev_tail_wave[
                            "waveform"] if prev_tail_wave is not None else None,
                        audio["waveform"] if audio is not None else None, overlap, n)
                    image_parts.append(blend_img)
                    if audio is not None:
                        audio_parts.append(blend_wave)
                        audio_parts.append(body_wave["waveform"])
                    if is_last:
                        # Last clip: emit body+tail raw after its boundary blend.
                        image_parts.append(torch.cat([body_img, tail_img], dim=0))
                        if audio is not None:
                            audio_parts.append(tail_wave["waveform"])
                    else:
                        image_parts.append(body_img)
                        prev_tail_img = tail_img
                        prev_tail_wave = tail_wave

                kept_frames = decoded_frames - (overlap if not is_last else 0)
                audio_sec = (0.0 if audio is None else
                             audio["waveform"].shape[-1] / float(audio["sample_rate"]))
                report_lines.append("clip_%05d: decoded=%d frames, crossfade=%d frames "
                                    "(%.4fs), kept=%d, audio=%.4fs"
                                    % (idx, decoded_frames, overlap, overlap / float(fps),
                                       kept_frames, audio_sec))

                # Advance the green progress bar once this clip is fully decoded and
                # its parts have been appended to the stitched timeline.
                pbar.update_absolute(pos + 1, total_count)

                del images
                if audio is not None:
                    del audio

        final_images = torch.cat(image_parts, dim=0).contiguous()
        del image_parts

        final_audio = None
        if audio_parts:
            final_waveform = torch.cat(audio_parts, dim=-1).contiguous()
            del audio_parts
            final_audio = {"waveform": final_waveform, "sample_rate": int(target_sr)}

        frame_count = int(final_images.shape[0])
        video_seconds = frame_count / float(fps)
        audio_seconds = (final_audio["waveform"].shape[-1]
                         / float(final_audio["sample_rate"])
                         if final_audio is not None else 0.0)

        report_lines.append("TOTAL: %d frames = %.4fs at %.3f fps; audio=%.4fs%s"
                            % (frame_count, video_seconds, float(fps), audio_seconds,
                               "" if final_audio is not None
                               else " (no audio_vae connected)"))
        report = "\n".join(report_lines)
        log_.info("H3 clip stitcher finished: %d frames (%.3fs), audio %.3fs",
                  frame_count, video_seconds, audio_seconds)

        return final_images, final_audio, frame_count, report


class _AVStreamPair:
    """Minimal stand-in for a NestedTensor: wraps (video, audio) tensors and
    exposes the unbind() interface that comfy-core's LTXVSeparateAVLatent
    (and the H3 sampler code) expects. The wrapped tensors are always dense,
    detached, contiguous copies, so they are safe to feed to the VAE kernels.
    """

    def __init__(self, video, audio):
        self._parts = [video, audio]

    def unbind(self):
        # noinspection PyTypeChecker
        return tuple(self._parts)

    def __iter__(self):
        return iter(self._parts)

    def __len__(self):
        return len(self._parts)


class H3ContextLatentConverter:
    """ Convert an H3 Motion Context archive latent (as loaded by
    MiniMaxH3MotionContextLoadLatent, whose 'samples' is a plain list) into
    the AV latent form that comfy-core's LTXVSeparateAVLatent expects
    (av_latent["samples"].unbind() -> (video, audio)).
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT", {
            "tooltip": "An H3 AV latent, e.g. the output of "
                       "MiniMaxH3MotionContextLoadLatent. Its 'samples' must be a "
                       "NestedTensor or a (video, audio) pair."})}}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "convert"
    CATEGORY = "noEmbryo"
    DESCRIPTION = ("Repackages the AV latent loaded from an H3 Motion Context clip "
                   "archive into the nested (video, audio) form that "
                   "LTXVSeparateAVLatent expects, so saved clips can be re-sampled, "
                   "upscaled, or re-saved.")

    @staticmethod
    def convert(latent):
        if not isinstance(latent, dict) or "samples" not in latent:
            raise ValueError("h3_context_latent_converter: expected a latent dict with "
                             "a 'samples' key, got %r" % type(latent))

        out = dict(latent)
        samples = latent["samples"]

        if hasattr(samples, "unbind"):
            parts = list(samples.unbind())
        elif isinstance(samples, (tuple, list)):
            parts = list(samples)
        else:
            raise ValueError("h3_context_latent_converter: 'samples' is neither "
                             "unbindable nor a (video, audio) pair, got %r"
                             % type(samples))

        if len(parts) < 2:
            raise ValueError("h3_context_latent_converter: latent has no audio "
                             "stream (only %d part(s)); expected an H3 AV latent."
                             % len(parts))

        expected_ndim = {"video": (4, 5), "audio": (3, 4)}
        names = ("video", "audio")
        dense = []
        for name, t in zip(names, parts[:2]):
            if t.ndim not in expected_ndim[name]:
                raise ValueError("h3_context_latent_converter: %s stream has "
                                 "unexpected shape %s." % (name, tuple(t.shape)))
            if not torch.is_floating_point(t):
                raise ValueError("h3_context_latent_converter: %s stream is not a "
                                 "float tensor (dtype %s)." % (name, t.dtype))
            # Force a real, dense, detached CPU copy: views into packed storage
            # (or tensors still carrying nested metadata) can make VAE CUDA
            # kernels crash with cudaErrorIllegalAddress.
            dense.append(t.detach().to("cpu", copy=True).contiguous())

        converted = {k: v for k, v in out.items() if k != "samples"}
        converted["samples"] = _AVStreamPair(dense[0], dense[1])
        return (converted,)


class H3MotionContextClipPurge:
    """ Delete the saved H3 Motion Context clip archive files from a folder.
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"mode": ("BOOLEAN", {"default": True,
            "label_on": "Purge", "label_off": "Preview (dry run)",
            "tooltip": "Purge (Enabled): delete the matching files.\n"
                       "Preview (dry run, Disabled): delete nothing; the report "
                       "just lists the files that would be deleted."}),
            "folder": ("STRING", {"default": "h3_context",
            "tooltip": "Folder whose root-level clip archives will be deleted.\n"
                       "Absolute paths and paths relative to ComfyUI/output are "
                       "accepted."}),
            "pattern": ("STRING", {"default": "clip_*.safetensors",
                "tooltip": "Filename glob. Only root-level FILES matching this "
                           "pattern are deleted.\nSub-folders are never touched."}), },
            "hidden": {"mode": "BOOLEAN"}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "purge"
    CATEGORY = "noEmbryo"
    OUTPUT_NODE = True
    DESCRIPTION = ("Deletes the numbered h3_motion_context_av_v1 clip archive files "
                   "at the root of a folder (default: h3_context).\n"
                   "Purge (Enabled): deletes the files.\n"
                   "Preview (Disabled): dry run - the report only lists what would "
                   "be deleted.\nOnly files matching the pattern are removed; "
                   "sub-folders and everything inside them are left untouched.")

    # noinspection PyUnusedLocal
    @classmethod
    def IS_CHANGED(cls, mode, folder, pattern):
        return float("NaN")

    @staticmethod
    def purge(mode, folder, pattern):
        d = _resolve_folder(folder)
        pattern = (pattern or "clip_*.safetensors").strip()

        doomed = []
        for entry in os.scandir(d):
            if entry.is_file(follow_symlinks=False) and not entry.is_dir():
                if fnmatch.fnmatch(entry.name, pattern):
                    doomed.append((entry.name, entry.stat().st_size))

        if not mode:  # Preview (dry run)
            lines = ["H3 clip purge (DRY RUN) in %s - nothing was deleted:" % d]
            lines += ["  would delete: %s (%s)" % (name, _fmt_size(size))
                      for name, size in doomed] or ["  no matching files."]
            lines.append("TOTAL: %d file(s), %s" %
                         (len(doomed), _fmt_size(sum(s for _, s in doomed))))
            report = "\n".join(lines)
            log_.info(report)
            return (report,)

        deleted = 0
        freed = 0
        lines = ["H3 clip purge in %s:" % d]
        for name, size in doomed:
            try:
                os.remove(os.path.join(d, name))
                deleted += 1
                freed += size
                lines.append("  deleted: %s (%s)" % (name, _fmt_size(size)))
            except OSError as e:
                lines.append("  FAILED to delete %s: %s" % (name, e))
        if not deleted and not doomed:
            lines.append("  no matching files.")
        lines.append("TOTAL: deleted %d file(s), freed %s" %
                     (deleted, _fmt_size(freed)))
        report = "\n".join(lines)
        log_.info(report)
        return (report,)


def _fmt_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0:
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f TiB" % size


NODE_CLASS_MAPPINGS = {
    "H3MotionContextClipStitcher": H3MotionContextClipStitcher,
    "H3ContextLatentConverter": H3ContextLatentConverter,
    "H3MotionContextClipPurge": H3MotionContextClipPurge,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3MotionContextClipStitcher": "H3 Motion Context Clip Stitcher",
    "H3ContextLatentConverter": "H3 Context Latent Converter",
    "H3MotionContextClipPurge": "H3 Motion Context Clip Purge",
}