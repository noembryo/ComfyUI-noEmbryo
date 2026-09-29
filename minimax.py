"""H3 Motion Context clip stitcher for ComfyUI.

Loads NikoDemon80/ComfyUI-H3-Motion-Context clip archive files (h3_motion_context_av_v1),
decodes each approved clip once, crossfades the carried Motion Context head from
clips, and concatenates the picture/audio into one IMAGE + AUDIO pair.

This intentionally does NOT reconstruct a NestedTensor and feed the saved files back
into Motion Context.
The archive format is the sampler output, and this node is a final-media assembly tool.
"""

import fnmatch
import gc
import glob
import inspect
import logging
import os
import re

import torch
import comfy.model_management
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

    Large latents (1MP and up) go through an explicit tiled decode with
    conservative tile sizes instead of vae.decode()'s adaptive heuristics,
    which can pick tiles that are far too aggressive and abort the native
    VAE kernels. decode_tiled is introspected so only supported kwargs are
    passed; small latents just use the plain decode path.
    """
    decode_fn = vae.decode
    kwargs = {}
    # video_latent is [B,C,T,H,W] or [C,T,H,W]; the spatial size is the last
    # two dims in either case.
    spatial = tuple(int(v) for v in video_latent.shape[-2:])
    latent_hw = spatial[0] * spatial[1]
    # The latent is ~8x downscaled from the pixel resolution, so ~1MP of
    # video corresponds to a latent spatial product around 16k pixels.
    tiled = getattr(vae, "decode_tiled", None)
    if callable(tiled) and latent_hw is not None and latent_hw >= 128 * 96:
        try:
            sig = inspect.signature(tiled)
            supported = set(sig.parameters)
        except (TypeError, ValueError):
            supported = set()
        if supported:
            # Mirror comfy-core's VAEDecodeTiled defaults, then scale the
            # spatial tiles down for very large latents so per-tile memory
            # stays bounded on 1MP+ latents.
            kwargs = {"tile_x": 256, "tile_y": 256, "tile_t": 64, "overlap": 64}
            if latent_hw >= 128 * 96:  # ~1MP+ of video space: shrink tiles
                kwargs.update(tile_x=192, tile_y=192)
            kwargs = {k: v for k, v in kwargs.items() if k in supported}
            decode_fn = tiled
            log_.info("H3 clip stitcher: using decode_tiled %s for %dx%d latent.",
                      kwargs, spatial[0], spatial[1])
    try:
        images = decode_fn(video_latent, **kwargs)
    except TypeError:  # Signature mismatch fallback: plain decode.
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
        raise RuntimeError("H3 audio VAE returned unexpected shape %s" % (tuple(audio.shape),))
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


# --- Texture ratchet correction (ported from ComfyUI-Hand-Tie-Clips/latents.py) ---
# The texture ratchet: high-band noise/grain increases monotonically at each join.
# The statistic is band_ratio = high-band std / total std.
# Measured across a chain it goes 0.3643 -> 0.3673 -> 0.3702 (monotone increase).
# The fix: match_band rescales only the high-frequency band to a target ratio.

def _band_gauss1d(sigma, device, dtype):
    r = max(1, int(round(3.0 * float(sigma))))
    x = torch.arange(-r, r + 1, dtype=torch.float32, device=device)
    k = torch.exp(-(x * x) / (2.0 * float(sigma) ** 2))
    return (k / k.sum()).to(dtype)


def _band_split(t, sigma=2.0):
    """Separable Gaussian low/high split over the last two dims. -> (lo, hi).

    Returns None when the tensor has no spatial extent to speak of.
    Replicate padding, not reflect: a latent's spatial dims are small and
    reflect needs the pad to be smaller than the dimension.
    """
    if t.dim() < 2 or t.shape[-1] < 8 or t.shape[-2] < 8:
        return None
    # noinspection PyUnresolvedReferences
    import torch.nn.functional as f
    h, w = int(t.shape[-2]), int(t.shape[-1])
    flat = t.reshape(-1, 1, h, w)
    k = _band_gauss1d(sigma, t.device, t.dtype)
    pad = k.numel() // 2
    lo = f.conv2d(f.pad(flat, (pad, pad, 0, 0), mode="replicate"),
                  k.view(1, 1, 1, -1))
    lo = f.conv2d(f.pad(lo, (0, 0, pad, pad), mode="replicate"),
                  k.view(1, 1, -1, 1))
    lo = lo.reshape(t.shape)
    return lo, t - lo


def _band_ratio(t, sigma=2.0):
    """High-band sigma as a fraction of total sigma. -> float, or None.

    This is the statistic the texture ratchet actually moves.
    Measured across a chain it goes 0.3643 -> 0.3673 -> 0.3702 (monotone increase).
    """
    got = _band_split(t, sigma)
    if got is None:
        return None
    lo, hi = got
    tot = float(t.float().std())
    if not tot or tot != tot:
        return None
    hi_sd = float(hi.float().std())
    if not hi_sd or hi_sd != hi_sd:
        return None
    return hi_sd / tot


def _match_band(t, target_ratio, sigma=2.0, clamp=(0.5, 2.0)):
    """Rescale t's high band so its high-band fraction becomes `target_ratio`.

    -> (tensor, k), or (t, None) when the tensor has no bands to match.

    Only the high half is scaled, so the low-frequency structure that carries
    the scene is bit-identical and the correction is one scalar. It cannot blur,
    sharpen unevenly, or invent detail; the worst it can do is get the gain
    wrong, which is why `clamp` exists.

    The fraction is against the tensor's own sigma, so restoring the ratio
    does move total sigma a little. That is deliberate: the ratio is the drifting
    statistic and sigma is the one that lies.
    """
    got = _band_split(t, sigma)
    if got is None or not target_ratio:
        return t, None
    lo, hi = got
    cur_hi = float(hi.float().std())
    if not cur_hi or cur_hi != cur_hi:
        return t, None

    # The naive `k = target * sigma / hi_sigma` is wrong, and quietly so:
    # scaling the high band changes the sigma it is a fraction OF, so the
    # target moves as you apply it. Measured, it undershot by 5% on a
    # latent-shaped tensor -- a correction that silently does most, but not
    # all, of its job is the worst kind to ship.
    #
    # First guess solves the fixed point assuming lo and hi are orthogonal:
    # k*H / sqrt(L^2 + k^2*H^2) = r, so k = r*L / (H*sqrt(1 - r^2)) ...
    r = min(float(target_ratio), 0.999)
    lo_sd = float(_band_split(t, 2.0)[0].float().std()) if _band_split(t, 2.0) else 0.0
    if not lo_sd:
        return t, None
    k = (r * lo_sd) / (cur_hi * max(1e-6, (1.0 - r * r) ** 0.5))
    # ... then refine against the statistic as actually measured, because a
    # difference of Gaussians is not an exact orthogonal projection. Two or
    # three passes converge, and a latent is small enough that this is free.
    for _ in range(4):
        k = min(max(k, clamp[0]), clamp[1])
        got_r = _band_ratio(lo + hi * k, 2.0)
        if not got_r:
            break
        if abs(got_r - r) <= 1e-4 * max(r, 1e-6):
            break
        k *= r / got_r
    k = min(max(k, clamp[0]), clamp[1])
    return lo + hi * k, k


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


def _fmt_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0:
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f TiB" % size


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
    CATEGORY = "noEmbryo/MiniMax H3"
    DESCRIPTION = ("Final assembly for NikoDemon80's H3 Motion Context AV clip archives.\n"
                   "Loads numbered h3_motion_context_av_v1 files, decodes one clip at a "
                   "time, dissolves the overlap between adjacent clips (video + "
                   "synchronized audio), and concatenates them to a final video and audio stream.")

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

        live_index = int(first_clip)
        if latent is not None:
            if files:
                # The last on-disk file is presumed to be the live latent's own
                # saved duplicate; drop it (with a warning) and renumber.
                log_.warning("H3 clip stitcher: dropping on-disk clip %05d; it is "
                             "presumed to be the live latent's duplicate.",
                             files[-1][0])
                files = files[:-1]
                live_index = files[-1][0] + 1 if files else int(first_clip)
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
            # Keep VRAM/RAM flat across clips: free the freshly cached decode
            # blocks so a long chain (or a big 1MP clip) cannot accumulate.
            gc.collect()
            comfy.model_management.soft_empty_cache()

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

                # # Clamp first clip for consistency (tone compensation is now handled
                # # by H3ClipRefiner node placed inline between sampler and Save Latent)
                # if pos == 0:
                #     images = images.clamp(0.0, 1.0)

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


class H3ClipRefiner:
    """ Inline texture-ratchet correction node for H3 Motion Context clips.

    Place this node BETWEEN the sampler (SamplerCustomAdvanced) and the
    H3 Motion Context Save Latent node. It operates on the LATENT level
    (no VAE decode/encode needed for measurement) to fix the texture ratchet:
    high-band noise/grain that increases monotonically at each join.

    The degradation is a "texture ratchet": high-band noise/grain increases
    monotonically at each join (+4.2% mid-band per join). The statistic is
    band_ratio = high-band std / total std. Measured across a chain it goes
    0.3643 -> 0.3673 -> 0.3702 (monotone increase).

    WORKFLOW:
    1. First run: set target_ratio=0.0 (measure mode). The node measures the
       band_ratio (high-band std / total std) of the video latent and logs it
       (e.g., "band_ratio=0.3673"). The latent passes through unchanged.
    2. Note the reported band_ratio from the FIRST clip (e.g., 0.3643).
    3. Subsequent runs: set target_ratio to the first clip's band_ratio (e.g.,
       0.3643). The node applies match_band to rescale the high-band so the
       band_ratio matches the target, fixing the texture ratchet.

    The correction uses match_band: rescales ONLY the high-frequency band to
    match the target band_ratio. Low-frequency structure (scene content) is
    bit-identical preserved. No VAE decode/encode needed for the correction.

    TO MAKE IT LESS GRAINY: If a clip measures at 0.3917, and you want it less
    grainy, set target_ratio to a LOWER value (e.g., 0.3800). The lower the
    target_ratio, the more the high-band grain is reduced.
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT", {
            "tooltip": "The generated AV latent from your H3 sampler "
                       "(SamplerCustomAdvanced output)."}),
            "target_ratio": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0,
                "step": 0.0001,
                "tooltip": "0.0 = measure only (report band_ratio, pass latent through).\n"
                           ">0.0 = target band_ratio to match. Set to the first clip's "
                           "band_ratio (e.g., 0.3643) to fix the texture ratchet.\n"
                           "To make it LESS grainy, set target_ratio LOWER than the measured "
                           "band_ratio (e.g., measured 0.3917 -> set 0.3800)."}),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0,
                "step": 0.01,
                "tooltip": "Correction strength. 0.0 = measure only (pass through).\n"
                           "1.0 = full correction to target_ratio.\n"
                           ">1.0 = over-correction (stronger grain reduction).\n"
                           "Use >1.0 if target_ratio alone doesn't reduce grain enough."}),
            },
            "optional": {"reference_latent": ("LATENT", {
                "tooltip": "Optional: reference latent (e.g., first clip) to measure "
                           "target_ratio from automatically. If connected, target_ratio "
                           "is ignored and measured from this latent."})},
                }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "refine"
    CATEGORY = "noEmbryo/MiniMax H3"
    DESCRIPTION = ("Inline texture-ratchet corrector for H3 Motion Context clips.\nPlace "
                   "between SamplerCustomAdvanced and H3 Motion Context Save Latent.\n"
                   "Fixes the texture ratchet: high-band noise/grain that increases at "
                   "each join.\ntarget_ratio=0: measures band_ratio "
                   "(high-band std / total std), passes latent through.\n"
                   "target_ratio>0: applies match_band to rescale high-band to target "
                   "ratio.\nstrength=0: measure only. strength=1: full correction. >1: "
                   "over-correction.\nRun first clip with target_ratio=0 to measure its "
                   "band_ratio, then set target_ratio to that value for all subsequent "
                   "clips.\nTo make it LESS grainy, set target_ratio LOWER than measured "
                   "band_ratio.\nUse strength>1.0 for over-correction if needed.")

    # noinspection PyUnusedLocal
    @classmethod
    def IS_CHANGED(cls, latent, target_ratio, strength,
                   reference_latent=None):
        # noinspection PyBroadException
        try:
            import hashlib
            latent_bytes = str(latent).encode()
            latent_hash = hashlib.md5(latent_bytes).hexdigest()[:16]
            return latent_hash, float(target_ratio), float(strength)
        except Exception:
            return float("NaN")

    @staticmethod
    def refine(latent, target_ratio, strength,
               reference_latent=None):
        # Extract video latent from AV latent
        if not isinstance(latent, dict) or "samples" not in latent:
            raise ValueError("H3ClipRefiner: expected a latent dict with 'samples' key")
        
        samples = latent["samples"]
        if hasattr(samples, "unbind"):
            parts = list(samples.unbind())
        elif isinstance(samples, (tuple, list)):
            parts = list(samples)
        else:
            raise ValueError("H3ClipRefiner: expected AV latent with video+audio")
        
        if len(parts) < 2:
            raise ValueError("H3ClipRefiner: latent has no audio stream")
        
        video_latent = parts[0].detach().to("cpu", copy=True).contiguous()
        audio_latent = parts[1].detach().to("cpu", copy=True).contiguous()
        
        # Handle video latent shape: [B,C,T,H,W] or [C,T,H,W]
        if video_latent.dim() == 5:
            video_latent = video_latent.squeeze(0)  # [C,T,H,W]
        # Now [C,T,H,W] - we need to process each channel/frame
        # The band functions work on [..., H, W] so we process per channel-frame
        
        # Measure band_ratio of the video latent
        # Flatten batch/channel/time dims for band_ratio measurement
        # video_latent is [C, T, H, W] -> reshape to [C*T, H, W] for band_ratio
        c, t, h, w = video_latent.shape
        flat_latent = video_latent.reshape(c * t, h, w)
        
        # Measure band_ratio with improved averaging over multiple sigma values
        # This gives a more robust measurement by averaging over multiple scales
        # Use hardcoded best defaults: sigma=2.0, clamp=(0.5, 2.0)
        sigmas = [1.0, 2.0, 3.0]
        band_ratios = []
        for s in sigmas:
            br = _band_ratio(flat_latent, sigma=s)
            if br is not None:
                band_ratios.append(br)
        
        if not band_ratios:
            raise ValueError("H3ClipRefiner: could not measure band_ratio (latent too small?)")
        
        band_ratio = sum(band_ratios) / len(band_ratios)
        
        # Determine target_ratio
        if reference_latent is not None:
            # Measure target_ratio from reference latent
            if not isinstance(reference_latent, dict) or "samples" not in reference_latent:
                raise ValueError("H3ClipRefiner: reference_latent must be an AV latent dict")
            ref_samples = reference_latent["samples"]
            if hasattr(ref_samples, "unbind"):
                ref_parts = list(ref_samples.unbind())
            elif isinstance(ref_samples, (tuple, list)):
                ref_parts = list(ref_samples)
            else:
                raise ValueError("H3ClipRefiner: reference_latent must be AV latent")
            if len(ref_parts) < 2:
                raise ValueError("H3ClipRefiner: reference_latent has no audio stream")
            ref_video = ref_parts[0].detach().to("cpu", copy=True).contiguous()
            if ref_video.dim() == 5:
                ref_video = ref_video.squeeze(0)
            rc, rt, rh, rw = ref_video.shape
            ref_flat = ref_video.reshape(rc * rt, rh, rw)
            
            # Measure reference band_ratio with same averaging
            ref_ratios = []
            for s in [1.0, 2.0, 3.0]:
                br = _band_ratio(ref_flat, sigma=s)
                if br is not None:
                    ref_ratios.append(br)
            if not ref_ratios:
                raise ValueError("H3ClipRefiner: could not measure target_ratio from "
                                 "reference_latent")
            target_ratio = sum(ref_ratios) / len(ref_ratios)
            log_.info("H3ClipRefiner: measured target_ratio=%.4f from reference_latent",
                      target_ratio)
        
        # Log the measurement
        log_.info("H3ClipRefiner: clip band_ratio=%.4f (avg of %d sigmas), target_ratio=%.4f",
                  band_ratio, len(band_ratios), target_ratio if target_ratio > 0 else 0.0)
        
        report = (f"H3ClipRefiner: band_ratio={band_ratio:.4f}"
                  f"{f', target_ratio={target_ratio:.4f}' if target_ratio > 0 else ''}")
        
        if target_ratio <= 0.0:
            # Measure only: report band_ratio, pass latent through
            report += (" [MEASURE ONLY - latent passed through. Set target_ratio to this "
                       "clip's band_ratio to fix texture ratchet.]")
            return latent, report
        
        # Apply match_band to rescale high-band to target_ratio
        # Use hardcoded best defaults: sigma=2.0, clamp=(0.5, 2.0)
        corrected_flat, gain = _match_band(flat_latent, target_ratio, sigma=2.0,
                                           clamp=(0.5, 2.0))
        
        if gain is None:
            report += " [MATCH_BAND FAILED - latent passed through unchanged]"
            log_.warning("H3ClipRefiner: match_band failed, passing latent through unchanged")
            return latent, report
        
        # Apply strength: interpolate between original and corrected
        # strength=0: original, strength=1: fully corrected, >1: over-correction
        if strength <= 0.0:
            # Measure only
            report += (" [MEASURE ONLY - latent passed through. Set target_ratio to this "
                       "clip's band_ratio to fix texture ratchet.]")
            return latent, report
        elif strength < 1.0:
            corrected_flat = flat_latent + (corrected_flat - flat_latent) * strength
            effective_gain = 1.0 + (gain - 1.0) * strength
        else:
            # strength >= 1.0: full correction or over-correction
            # Extrapolate beyond the corrected latent for over-correction
            corrected_flat = flat_latent + (corrected_flat - flat_latent) * strength
            effective_gain = 1.0 + (gain - 1.0) * strength
        
        # Reshape back to [C, T, H, W]
        corrected_video = corrected_flat.reshape(c, t, h, w)
        
        # Reconstruct AV latent with corrected video + original audio
        out = dict(latent)
        # Restore batch dim if original had it
        if parts[0].dim() == 5:
            corrected_video = corrected_video.unsqueeze(0)
        out["samples"] = _AVStreamPair(corrected_video, audio_latent)
        
        report += (f" [CORRECTED - gain={effective_gain:.4f},"
                   f" band_ratio {band_ratio:.4f} -> {target_ratio:.4f}, "
                   f"strength={strength:.2f}]")
        log_.info("H3ClipRefiner: applied match_band, gain=%.4f, "
                  "band_ratio %.4f -> %.4f, strength=%.2f",
                  effective_gain, band_ratio, target_ratio, strength)
        
        return out, report


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
    CATEGORY = "noEmbryo/MiniMax H3"
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
    CATEGORY = "noEmbryo/MiniMax H3"
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


class H3AVLatentFromVideo:
    """ Encode loaded video frames, or wrap an already-encoded latent, into an
    H3 Motion Context AV latent suitable for saving.
    """
    H3_FPS = 24.0

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video_vae": ("VAE", {
                "tooltip": "MiniMax H3 video VAE (FP16 or INT8 ConvRot)."}),
            "audio_vae": ("VAE", {
                "tooltip": "MiniMax H3 audio VAE FP32."}),
            "source_fps": ("FLOAT", {
                "default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                "tooltip": "The frame rate of the loaded video. Frames are "
                           "resampled to H3's native 24 fps by time-based "
                           "frame picking, so audio stays in sync at any "
                           "source rate."}),
            },
            "optional": {
                "images": ("IMAGE", {
                    "tooltip": "The whole video as frames (e.g. from VHS Load Video). "
                               "Leave un-connected when using the latent input."}),
                "latent": ("LATENT", {
                    "tooltip": "Optional alternative to images. Accepts either a "
                               "nested AV latent from LTXVConcatAVLatent (its audio "
                               "stream is used directly) or an already-encoded H3 "
                               "video LATENT from VAE Encode. A standard [B,C,H,W] "
                               "latent is wrapped as a one-frame H3 video stream; "
                               "connect AUDIO separately when it has no audio stream."}),
                "audio": ("AUDIO", {
                    "tooltip": "The video's audio (e.g. from VHS Load Video). "
                               "Leave un-connected for a silent clip. Ignored when "
                               "latent already contains an audio stream."}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "encode"
    CATEGORY = "noEmbryo/MiniMax H3"
    DESCRIPTION = ("Encodes a whole video (IMAGE frames + AUDIO) with the MiniMax H3 "
                   "VAEs into an AV latent that can be saved with 'H3 Motion Context "
                   "Save Latent' and stitched into later generations.")

    @staticmethod
    def _resample_frames(images, source_fps, target_fps):
        """ Time-based frame picking, mirroring VHS's force_rate behavior.
         """
        total = int(images.shape[0])
        if abs(float(source_fps) - float(target_fps)) < 1e-6:
            return images
        duration = total / float(source_fps)
        out_count = max(1, int(round(duration * float(target_fps))))
        idx = [min(total - 1, max(0, int(round(k * float(source_fps)
                                              / float(target_fps)))))
               for k in range(out_count)]
        return images[idx]

    @staticmethod
    def _is_nested_av_latent(latent):
        if not isinstance(latent, dict) or "samples" not in latent:
            return False
        samples = latent["samples"]
        return (getattr(samples, "is_nested", False)
                or isinstance(samples, (tuple, list))
                or (hasattr(samples, "unbind") and not torch.is_tensor(samples)))

    @staticmethod
    def _video_from_ordinary_latent(latent):
        if not isinstance(latent, dict) or "samples" not in latent:
            raise ValueError("h3_av_latent_from_video: expected a LATENT dict "
                             "with a 'samples' tensor, got %r" % type(latent))
        samples = latent["samples"]
        if not torch.is_tensor(samples):
            raise ValueError("h3_av_latent_from_video: ordinary latent input must "
                             "contain a tensor in 'samples', got %r"
                             % type(samples))
        if samples.ndim == 4:
            samples = samples.unsqueeze(2)
        elif samples.ndim != 5:
            raise ValueError("h3_av_latent_from_video: ordinary latent samples "
                             "must have shape [B,C,H,W] or [B,C,T,H,W], got %s."
                             % (tuple(samples.shape),))
        if not torch.is_floating_point(samples):
            raise ValueError("h3_av_latent_from_video: ordinary latent samples "
                             "are not a float tensor (dtype %s)." % (samples.dtype,))
        return samples.detach().to("cpu", copy=True).contiguous()

    @staticmethod
    def _frame_count_from_video_latent(video_latent):
        """Return the canonical H3 pixel-frame count for a video latent."""
        latent_t = int(video_latent.shape[2])
        if latent_t <= 1:
            return 1
        if latent_t == 2:
            return 5
        return ((latent_t - 2) // 5) * 17 + 5

    @staticmethod
    def _encode_latent(latent, audio_vae, audio=None):
        if H3AVLatentFromVideo._is_nested_av_latent(latent):
            video_latent, audio_latent = _av_from_live_latent(latent)
            source = "nested AV latent"
        else:
            video_latent = H3AVLatentFromVideo._video_from_ordinary_latent(latent)
            if audio_vae is None:
                raise ValueError("h3_av_latent_from_video: connect the MiniMax H3 "
                                 "audio VAE to 'audio_vae' when using an ordinary "
                                 "latent without an audio stream.")
            if audio is not None:
                audio = _resample_audio(
                    {"waveform": audio["waveform"][:1],
                     "sample_rate": int(audio["sample_rate"])},
                    int(getattr(audio_vae, "audio_sample_rate", 32000)))
                audio_latent = audio_vae.encode(
                    audio["waveform"].movedim(1, -1))
            else:
                sr = int(getattr(audio_vae, "audio_sample_rate", 32000))
                frame_count = H3AVLatentFromVideo._frame_count_from_video_latent(
                    video_latent)
                silence = torch.zeros(
                    1, 1, max(1, int(round(frame_count
                                           / H3AVLatentFromVideo.H3_FPS * sr))))
                audio_latent = audio_vae.encode(silence.movedim(1, -1))
            audio_latent = audio_latent.detach().to("cpu", copy=True).contiguous()
            source = "ordinary latent"

        if int(video_latent.shape[0]) != 1:
            raise ValueError("h3_av_latent_from_video: H3 Motion Context AV "
                             "latents must have batch size 1, got video shape %s."
                             % (tuple(video_latent.shape),))
        if int(audio_latent.shape[0]) != 1:
            raise ValueError("h3_av_latent_from_video: H3 Motion Context AV "
                             "latents must have batch size 1, got audio shape %s."
                             % (tuple(audio_latent.shape),))

        out = {"samples": _AVStreamPair(video_latent, audio_latent)}
        log_.info("H3 AV Latent from %s: video latent %s, audio latent %s",
                  source, tuple(video_latent.shape),
                  tuple(audio_latent.shape) if audio_latent is not None else None)
        return (out,)

    @staticmethod
    def encode(images=None, video_vae=None, audio_vae=None, source_fps=None,
               audio=None, latent=None):
        if images is None and latent is None:
            raise ValueError("h3_av_latent_from_video: connect either images or "
                             "latent.")
        if images is not None and latent is not None:
            raise ValueError("h3_av_latent_from_video: connect either images or "
                             "latent, not both.")
        if latent is not None:
            return H3AVLatentFromVideo._encode_latent(latent, audio_vae, audio)
        if images is None:
            raise ValueError("h3_av_latent_from_video: no frames to encode.")
        if int(images.shape[0]) < 1:
            raise ValueError("h3_av_latent_from_video: no frames to encode.")
        if video_vae is None:
            raise ValueError("h3_av_latent_from_video: connect the MiniMax H3 "
                             "video VAE to 'video_vae'.")
        if audio_vae is None:
            raise ValueError("h3_av_latent_from_video: connect the MiniMax H3 "
                             "audio VAE to 'audio_vae'.")

        frames = H3AVLatentFromVideo._resample_frames(
            images, source_fps, H3AVLatentFromVideo.H3_FPS)
        frames = frames.to(video_vae.device if hasattr(video_vae, "device")
                           else "cpu", non_blocking=False)
        video_latent = video_vae.encode(frames)  # [B,C,T,H,W] (batch axis = time)
        if getattr(video_latent, "ndim", 0) != 5:
            raise ValueError("h3_av_latent_from_video: video encode returned shape "
                             "%s, expected [B,C,T,H,W]."
                             % (tuple(getattr(video_latent, "shape", ())),))
        video_latent = video_latent.detach().to("cpu", copy=True).contiguous()

        if audio is not None:
            audio = _resample_audio(
                {"waveform": audio["waveform"][:1],
                 "sample_rate": int(audio["sample_rate"])},
                int(getattr(audio_vae, "audio_sample_rate", 32000)))
            audio_latent = audio_vae.encode(
                audio["waveform"].movedim(1, -1))  # [1,C,2,T]
            audio_latent = audio_latent.detach().to("cpu", copy=True).contiguous()
        else:  # A silent clip still needs an audio stream for the archive format.
            sr = int(getattr(audio_vae, "audio_sample_rate", 32000))
            silence = torch.zeros(1, 1, max(1, int(round(frames.shape[0]
                                                         / H3AVLatentFromVideo.H3_FPS
                                                         * sr))))
            audio_latent = audio_vae.encode(silence.movedim(1, -1))
            audio_latent = audio_latent.detach().to("cpu", copy=True).contiguous()

        out = {"samples": _AVStreamPair(video_latent, audio_latent)}
        log_.info("H3 AV Latent from Video: %d frames -> video latent %s, "
                  "audio latent %s", int(frames.shape[0]),
                  tuple(video_latent.shape),
                  tuple(audio_latent.shape) if audio_latent is not None else None)
        return (out,)


NODE_CLASS_MAPPINGS = {
    "H3MotionContextClipStitcher": H3MotionContextClipStitcher,
    "H3ClipRefiner": H3ClipRefiner,
    "H3ContextLatentConverter": H3ContextLatentConverter,
    "H3MotionContextClipPurge": H3MotionContextClipPurge,
    "H3AVLatentFromVideo": H3AVLatentFromVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3MotionContextClipStitcher": "H3 Motion Context Clip Stitcher",
    "H3ClipRefiner": "H3 Clip Refiner",
    "H3ContextLatentConverter": "H3 Context Latent Converter",
    "H3MotionContextClipPurge": "H3 Motion Context Clip Purge",
    "H3AVLatentFromVideo": "H3 AV Latent from Video",
}
