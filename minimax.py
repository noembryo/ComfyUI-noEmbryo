

import fnmatch
import gc
import glob
import inspect
import logging
import os
import re

import torch
import comfy.model_management
import comfy.samplers
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
import comfy.sample

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
    # noinspection PyUnresolvedReferences
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


def _high_band_sd(t, sigma=2.0):
    """Absolute high-band standard deviation. -> float, or None.

    The band *ratio* only drifts ~1.6% across a chain (0.3643 -> 0.3702),
    so a gain match on the ratio moves a tiny delta that strength cannot
    amplify visibly. The absolute high-band sigma can drift much more when
    the total sigma also grows, so matching the absolute value gives a
    correction delta that is large enough for strength to move.
    """
    got = _band_split(t, sigma)
    if got is None:
        return None
    _, hi = got
    hi_sd = float(hi.float().std())
    if not hi_sd or hi_sd != hi_sd:
        return None
    return hi_sd


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
    """ H3 Motion Context clip stitcher for ComfyUI.

    Loads NikoDemon80/ComfyUI-H3-Motion-Context clip archive files (h3_motion_context_av_v1),
    decodes each approved clip once, crossfades the carried Motion Context head from
    clips, and concatenates the picture/audio into one IMAGE + AUDIO pair.
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


# H3 video VAE temporal mapping: each latent step covers this many pixel
# frames, cycling. The first step is the odd one (1 frame), then fours.
# This is the same (1,4,4,4,4) cycle H3-Motion-Context and ComfyUI-H3-Refine
# use, and it is what _frame_count_from_video_latent() below inverts.
_H3_FRAME_PER_TOKEN = (1, 4, 4, 4, 4)


def _steps_for_frames(n):
    """ Latent steps covering exactly n pixel frames from cycle position 0.

    Mirrors H3-Motion-Context's own _steps_for_frames: 5/22/39/56 -> 2/7/12/17.
    Returns None when n does not land on a whole token boundary (e.g. 3).
    """
    n = int(n)
    if n <= 0:
        return None
    covered = 0
    k = 0
    while covered < n:
        covered += _H3_FRAME_PER_TOKEN[k % len(_H3_FRAME_PER_TOKEN)]
        k += 1
    return k if covered == n else None


def _refine_resample(av_latent, model, positive, negative, denoise, steps, cfg,
                     sampler_name, scheduler, seed, node_id=None):
    """ A short, low-denoise second sample of a finished AV latent.

    av_latent is the full H3 AV latent (NestedTensor or tuple of [video, audio]).
    Returns the refined video latent on CPU [B,C,T,H,W].
    The model expects the full AV latent (video + audio) for the forward pass.

    :return: torch.Tensor [B,C,T,H,W] on CPU (video only)|None
    """
    device = model.load_device

    # Make a deep copy of the AV latent to avoid any in-place modifications
    # during the refine resample that could affect the original audio.
    if hasattr(av_latent, "to"):
        work = av_latent.to(device)
    elif isinstance(av_latent, (tuple, list)):
        work = tuple(p.to(device).clone() for p in av_latent)
    else:
        work = av_latent.to(device).clone()

    # Progress bar for refine resample steps
    pbar = ProgressBar(int(steps), node_id=node_id)

    # noinspection PyUnusedLocal
    def _pbar_callback(step, x0, x, total_steps):
        pbar.update_absolute(step + 1, total_steps)

    # Save model training mode and force eval to prevent batch norm updates
    # that would accumulate across clips and distort audio.
    was_training = model.training
    model.eval()
    try:
        with torch.no_grad():
            noise = comfy.sample.prepare_noise(work, int(seed))
            samples = comfy.sample.sample(model, noise, int(steps), float(cfg),
                                          str(sampler_name), str(scheduler), positive,
                                          negative, work, denoise=float(denoise),
                                          seed=int(seed), callback=_pbar_callback)
        # samples is AV latent (NestedTensor/tuple); extract video part
        if hasattr(samples, "unbind"):
            video = list(samples.unbind())[0]
        elif isinstance(samples, (tuple, list)):
            video = samples[0]
        else:
            video = samples
        return video.detach().to("cpu", copy=True).contiguous()
    finally:
        # Restore model training mode
        model.train(was_training)
        del work
        comfy.model_management.soft_empty_cache()


class H3ClipRefiner:
    """ Inline style-drift corrector for H3 Motion Context clips.

    Place this node BETWEEN the sampler (SamplerCustomAdvanced) and the
    H3 Motion Context Save Latent node. It operates on the LATENT level
    (no VAE decode/encode needed) to fix the style drift that accumulates
    across clips in a chain.

    The drift has four components:
    1. Luminance drift (mean shift)
    2. Contrast drift (std change)
    3. Color/saturation drift (per-channel mean and std changes)
    4. Texture ratchet (high-band noise/grain increase)

    TWO HALVES:
    * Statistical (always on): a per-channel affine (matches mean + std)
      followed by a high-band gain match. This fixes the DRIFT but, being
      pure arithmetic, cannot put back micro-detail the chain has lost.
    * Generative (optional): connect model + positive/negative and the node
      runs a short low-denoise REFINE RESAMPLE, then transfers ONLY its
      high band (detail) onto the original low band (structure). Keeping the
      original low band is what stops the background shift / dissolve that a
      whole-latent blend (e.g. H3-LongTakeNoCuts) suffers from.

    UNITS: context_length is in FRAMES (5/22/39/56), matching the Motion
    Context node and the Stitcher. The latent's time axis is in TOKENS, so it
    is converted with _steps_for_frames (22 frames -> 7 tokens) before the
    head/body/tail split.

    WORKFLOW:
    1. Run the FIRST clip through with reference_latent unconnected.
       The node measures and reports its statistics but passes the latent
       through unchanged.
    2. Feed that first clip's latent into reference_latent for all
       subsequent clips. The node matches every later clip to the first.
    3. (Optional) connect model + positive/negative to also regenerate lost
       detail; tune refine_denoise (~0.3-0.5) and detail_strength.
    4. Adjust strength if needed: 1.0 = full match, >1.0 = stronger.
    5. Set context_length to match your H3 Motion Context setting (default 22).
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT", {
            "tooltip": "The generated AV latent from your H3 sampler "
                       "(SamplerCustomAdvanced output)."}),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0,
                "step": 0.01,
                "tooltip": "Correction strength. 0.0 = measure only (pass through).\n"
                           "1.0 = full match to reference statistics.\n"
                           ">1.0 = overshoot (pushes past the reference)."}),
            "context_length": (["5", "22", "39", "56"], {"default": "22",
                "tooltip": "Number of frames at head/tail that are the Motion Context "
                           "overlap regions. Must match the context_length in "
                           "H3 Motion Context and Clip Stitcher."}),
            },
            "optional": {"reference_latent": ("LATENT", {
                "tooltip": "Reference latent (e.g., first clip). When connected, "
                           "all target statistics are measured from this latent "
                           "and the input latent is corrected to match it."}),
                "model": ("MODEL", {
                    "tooltip": "Optional. Connect your H3 model to enable the "
                               "refine resample: a short low-denoise second "
                               "sample that regenerates the micro-detail the "
                               "chain loses. Leave disconnected for the pure "
                               "statistical corrector."}),
                "positive": ("CONDITIONING", {
                    "tooltip": "Positive conditioning for the refine resample "
                               "(same prompt you sampled the clip with)."}),
                "negative": ("CONDITIONING", {
                    "tooltip": "Negative conditioning for the refine resample."}),
                "refine_denoise": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 1.0,
                    "step": 0.01,
                    "tooltip": "Denoise for the refine resample. ~0.3-0.5 restores "
                               "detail while keeping the structure (only the high "
                               "band is transferred, so the background cannot shift). "
                               "Higher changes content more."}),
                "refine_steps": ("INT", {"default": 3, "min": 1, "max": 50,
                    "tooltip": "Sampler steps for the refine resample. 2-4 is plenty."}),
                "refine_cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0,
                    "step": 0.1,
                    "tooltip": "CFG for the refine resample. Match your main sampler."}),
                "sampler_name": (comfy.samplers.SAMPLER_NAMES, {
                    "default": "euler",
                    "tooltip": "Sampler for the refine resample."}),
                "scheduler": (comfy.samplers.SCHEDULER_NAMES, {
                    "default": "normal",
                    "tooltip": "Scheduler for the refine resample."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                    "control_after_generate": True,
                    "tooltip": "Seed for the refine resample noise."}),
                "detail_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0,
                    "step": 0.01,
                    "tooltip": "How much of the refined high band (detail) to "
                               "transfer over the original. 0 = statistical only, "
                               "1 = full refined detail. The low band (structure) "
                               "is always kept from the original, so the background "
                               "never shifts."}),
                },
            }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("latent", "report")
    FUNCTION = "refine"
    CATEGORY = "noEmbryo/MiniMax H3"
    DESCRIPTION = ("Inline style-drift corrector + optional refine resample for "
                   "H3 Motion Context clips.\n"
                   "Place between SamplerCustomAdvanced and H3 Motion Context Save Latent.\n"
                   "Statistical half fixes luminance/contrast/color/texture drift; "
                   "connect model+conditioning to also regenerate lost micro-detail.\n"
                   "strength=0: measure only. strength=1.0: full match. >1: stronger.")

    # noinspection PyUnusedLocal
    @classmethod
    def IS_CHANGED(cls, latent, strength, context_length, reference_latent=None,
                   model=None, positive=None, negative=None, refine_denoise=0.4,
                   refine_steps=3, refine_cfg=1.0, sampler_name="euler",
                   scheduler="normal", seed=0, detail_strength=1.0):
        # noinspection PyBroadException
        try:
            import hashlib
            latent_bytes = str(latent).encode()
            latent_hash = hashlib.md5(latent_bytes).hexdigest()[:16]
            return (latent_hash, float(strength), int(context_length),
                    None if model is None else id(model),
                    float(refine_denoise), int(refine_steps), float(refine_cfg),
                    str(sampler_name), str(scheduler), int(seed),
                    float(detail_strength))
        except Exception:
            return float("NaN")

    @staticmethod
    def refine(latent, strength, context_length, reference_latent=None,
               model=None, positive=None, negative=None, refine_denoise=0.4,
               refine_steps=3, refine_cfg=1.0, sampler_name="euler",
               scheduler="normal", seed=0, detail_strength=1.0):
        # Extract video and audio from AV latent
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
        had_batch = False
        if video_latent.dim() == 5:
            if video_latent.shape[0] != 1:
                raise ValueError("H3ClipRefiner: only batch size 1 is supported, "
                                 "got video shape %s." % (tuple(video_latent.shape),))
            had_batch = True
            video_latent = video_latent.squeeze(0)  # [C,T,H,W]
        
        c, t, h, w = video_latent.shape
        # context_length is in FRAMES (5/22/39/56), but the latent's time axis
        # is in TOKENS. Convert first (22 frames -> 7 tokens) so the head/body/
        # tail split lines up with the frames the Stitcher actually crossfades.
        # Splitting by the raw frame count here was a units bug that kept far
        # too much (or, on short clips, the whole clip) out of the correction.
        overlap = _steps_for_frames(context_length) or 0

        # --- Split into head / body / tail along the temporal axis ---
        # The head (first `overlap` tokens) and tail (last `overlap` tokens)
        # are the Motion Context overlap regions that the Clip Stitcher
        # crossfades with the adjacent clips.
        #
        # NEW BEHAVIOUR: We now CORRECT the head/tail to match the corrected
        # body statistics. This eliminates the pulsing effect (bright head/tail
        # vs normal body) while keeping the crossfade smooth because ALL clips'
        # overlap regions are corrected to match the same reference target.
        if overlap > 0 and t >= 2 * overlap:
            head = video_latent[:, :overlap]              # [C, overlap, H, W]
            body = video_latent[:, overlap:t - overlap]   # [C, body, H, W]
            tail = video_latent[:, t - overlap:]          # [C, overlap, H, W]
            has_overlap = True
        else:
            # Clip too short to have a meaningful overlap region; treat the
            # whole clip as body (old behaviour) so short clips still work.
            head = None
            body = video_latent
            tail = None
            has_overlap = False

        # --- Optional refine resample (the generative half of the fix) ---
        # A short low-denoise second sample regenerates the micro-detail the
        # chain loses. Only its HIGH band is used later (detail transfer), so
        # the original low band -- the structure/background -- is never
        # disturbed and cannot shift the way a whole-latent blend does.
        refined_body = None
        if model is not None and positive is not None and detail_strength > 0.0:
            if negative is None:
                log_.warning("H3ClipRefiner: negative conditioning not connected; "
                             "refine resample requires both positive and negative. "
                             "Skipping detail transfer.")
            else:
                try:
                    # Use the full AV latent (video + audio) for the model forward pass
                    av_latent = latent["samples"]
                    log_.info("H3ClipRefiner: starting refine resample "
                              "(denoise=%.2f, steps=%d, cfg=%.2f, seed=%d)...",
                              refine_denoise, refine_steps, refine_cfg, int(seed))
                    # noinspection PyCallingNonCallable
                    node_id = get_original_node_id() if get_original_node_id is not None else None
                    # noinspection PyNoneFunctionAssignment
                    refined_video = _refine_resample(av_latent, model, positive, negative,
                                                      refine_denoise, refine_steps,
                                                      refine_cfg, sampler_name, scheduler,
                                                      seed, node_id)
                    # noinspection PyUnresolvedReferences
                    refined_video = refined_video.to(video_latent.dtype)
                    log_.info("H3ClipRefiner: refine resample returned video shape %s",
                              tuple(refined_video.shape))
                    # refined_video is [B,C,T,H,W] or [C,T,H,W]; remove batch dim if present
                    if refined_video.dim() == 5:
                        refined_video = refined_video[0]
                    if tuple(refined_video.shape) == tuple(video_latent.shape):
                        if has_overlap:
                            refined_head = refined_video[:, :overlap]
                            refined_body = refined_video[:, overlap:t - overlap]
                            refined_tail = refined_video[:, t - overlap:]
                        else:
                            refined_head = None
                            refined_body = refined_video
                            refined_tail = None
                        log_.info("H3ClipRefiner: refine resample done "
                                  "(denoise=%.2f, steps=%d, cfg=%.2f).",
                                  refine_denoise, refine_steps, refine_cfg)
                    else:
                        log_.warning("H3ClipRefiner: refine resample returned %s, "
                                     "expected %s; skipping detail transfer.",
                                     tuple(refined_video.shape),
                                     tuple(video_latent.shape))
                        refined_head = refined_body = refined_tail = None
                except Exception as e:
                    log_.warning("H3ClipRefiner: refine resample failed (%s); "
                                 "using statistical correction only.", e)
                    import traceback
                    tb = traceback.format_exc()
                    log_.warning("H3ClipRefiner: refine resample traceback:\n%s", tb)
                    refined_body = None

        body_flat = body.reshape(c * body.shape[1], h, w)
        # noinspection PyUnusedLocal
        flat_latent = video_latent.reshape(c * t, h, w)
        
        # --- Measure current statistics (from BODY only) ---
        current_mean = float(body_flat.mean())
        current_std = float(body_flat.std())
        current_channel_means = body.mean(dim=(1, 2, 3))  # [C]
        current_channel_stds = body.std(dim=(1, 2, 3))    # [C]
        
        hi_sds = []
        for s in [1.0, 2.0, 3.0]:
            hs = _high_band_sd(body_flat, sigma=s)
            if hs is not None:
                hi_sds.append(hs)
        current_hi_sd = sum(hi_sds) / len(hi_sds) if hi_sds else None

        # --- Determine target statistics ---
        target_mean = current_mean
        target_std_val = current_std
        target_channel_means = current_channel_means.clone()
        target_channel_stds = current_channel_stds.clone()
        target_hi_sd = current_hi_sd

        if reference_latent is not None:
            # Measure all targets from reference latent
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
            # Measure the reference from its BODY too, so the target reflects
            # the same region we are correcting (apples to apples).
            ref_overlap = overlap
            if ref_overlap > 0 and ref_video.shape[1] >= 2 * ref_overlap:
                ref_body = ref_video[:, ref_overlap:ref_video.shape[1] - ref_overlap]
            else:
                ref_body = ref_video
            ref_flat = ref_body.reshape(-1, ref_body.shape[-2], ref_body.shape[-1])

            target_mean = float(ref_flat.mean())
            target_std_val = float(ref_flat.std())
            target_channel_means = ref_body.mean(dim=(1, 2, 3))
            target_channel_stds = ref_body.std(dim=(1, 2, 3))

            ref_hi_sds = []
            for s in [1.0, 2.0, 3.0]:
                hs = _high_band_sd(ref_flat, sigma=s)
                if hs is not None:
                    ref_hi_sds.append(hs)
            if ref_hi_sds:
                target_hi_sd = sum(ref_hi_sds) / len(ref_hi_sds)

            log_.info("H3ClipRefiner: measured targets from reference_latent: "
                      "mean=%.4f, std=%.4f, hi_sd=%.4f",
                      target_mean, target_std_val, target_hi_sd)

        # --- Build report ---
        report_parts = ["H3ClipRefiner: mean=%.4f->%.4f" % (current_mean, target_mean),
                        "std=%.4f->%.4f" % (current_std, target_std_val),]
        if current_hi_sd is not None:
            report_parts.append("hi_sd=%.4f->%.4f" % (current_hi_sd, target_hi_sd))
        else:
            report_parts.append("hi_sd=N/A")

        # Check if any correction is needed
        needs_correction = False
        if abs(target_mean - current_mean) > 1e-6:
            needs_correction = True
        if abs(target_std_val - current_std) > 1e-6:
            needs_correction = True
        if (target_channel_means - current_channel_means).abs().max() > 1e-6:
            needs_correction = True
        if current_hi_sd is not None and target_hi_sd is not None:
            if abs(target_hi_sd - current_hi_sd) > 1e-6:
                needs_correction = True
        
        # Nothing to do at all: no refine requested AND the statistics already
        # match (or strength is 0). Pass the latent through untouched.
        if refined_body is None and (strength <= 0 or not needs_correction):
            report = ", ".join(report_parts)
            report += " [MEASURE ONLY - latent passed through]"
            return latent, report

        band_gain = None
        corrected_body = body

        # --- 1. Statistical correction (drift): per-channel affine ---
        # Skipped when strength is 0 or the stats already match; the detail
        # transfer below can still run on its own in that case.
        if strength > 0 and needs_correction:
            a = torch.ones(c, device=video_latent.device, dtype=video_latent.dtype)
            b = torch.zeros(c, device=video_latent.device, dtype=video_latent.dtype)

            for ch in range(c):
                ch_data = body[ch]  # [T_body, H, W]
                ch_mean = float(ch_data.mean())
                ch_std = float(ch_data.std())
                if ch_std < 1e-6:
                    ch_std = 1e-6

                tgt_ch_mean = float(target_channel_means[ch])
                tgt_ch_std = float(target_channel_stds[ch])

                scale = tgt_ch_std / ch_std
                shift = tgt_ch_mean - scale * ch_mean

                a[ch] = scale
                b[ch] = shift

            corrected_body = body * a.view(c, 1, 1, 1) + b.view(c, 1, 1, 1)
            log_.info("H3ClipRefiner: per-channel affine: a=%s, b=%s",
                      a.tolist(), b.tolist())

            # 2a. Statistical high-band energy correction (texture ratchet).
            #     Only when there is no refine supplying real detail. Match the
            #     ABSOLUTE high-band std, measured and applied on the same
            #     affine-corrected body so the two stay consistent.
            if ((refined_body is None) and (current_hi_sd is not None)
                    and (target_hi_sd is not None)):
                if abs(target_hi_sd - current_hi_sd) > 1e-6:
                    corr_body_flat = corrected_body.reshape(c * body.shape[1], h, w)
                    corr_split = _band_split(corr_body_flat, sigma=2.0)
                    if corr_split is not None:
                        corr_lo, corr_hi = corr_split
                        cur_hi = float(corr_hi.float().std())
                        if cur_hi > 1e-6:
                            # 1:1 match gain for absolute high-band std:
                            # k * cur_hi = target_hi_sd  =>  k = target / cur
                            k_1to1 = target_hi_sd / cur_hi
                            k_1to1 = min(max(k_1to1, 0.05), 5.0)
                            corrected_body = (corr_lo + corr_hi * k_1to1).reshape(
                                c, body.shape[1], h, w)
                            band_gain = k_1to1
                            report_parts.append("band_gain=%.4f" % band_gain)
                        else:
                            report_parts.append("band_correction_failed")
                    else:
                        report_parts.append("band_correction_failed")

            # 3. Apply strength to the statistical correction delta.
            corrected_body = body + (corrected_body - body) * strength

        # Initialize corrected_head/tail to original head/tail for detail transfer
        if has_overlap:
            corrected_head = head
            corrected_tail = tail

        # --- 2b. Detail transfer from the refine resample (generative) ---
        # Keep the (possibly affine-corrected) LOW band -- the structure, so
        # the background cannot shift -- and blend in the refined HIGH band
        # (regenerated micro-detail). detail_strength dials 0..1. This is what
        # restores detail the statistical half cannot, without the dissolve a
        # whole-latent blend would introduce.
        # Apply to body, head, and tail for consistency across the entire clip.
        if refined_body is not None:
            log_.info("H3ClipRefiner: performing detail transfer (strength=%.2f)...",
                      detail_strength)
            d = float(min(max(detail_strength, 0.0), 1.0))

            # Helper to apply detail transfer to a region
            # noinspection PyShadowingNames
            def _detail_transfer(corrected_reg, refined_reg, region_name):
                if corrected_reg is None or refined_reg is None:
                    return corrected_reg
                corr_flat = corrected_reg.reshape(c * corrected_reg.shape[1], h, w)
                ref_flat = refined_reg.reshape(c * refined_reg.shape[1], h, w)
                corr_split = _band_split(corr_flat, sigma=2.0)
                ref_split = _band_split(ref_flat, sigma=2.0)
                if corr_split is not None and ref_split is not None:
                    corr_lo, corr_hi = corr_split
                    _, ref_hi = ref_split
                    mixed_hi = corr_hi * (1.0 - d) + ref_hi.to(corr_hi.dtype) * d
                    result = (corr_lo + mixed_hi).reshape(c, corrected_reg.shape[1], h, w)
                    log_.info("H3ClipRefiner: detail transfer applied to %s", region_name)
                    return result
                else:
                    log_.warning("H3ClipRefiner: detail transfer failed for %s "
                                 "- band split returned None", region_name)
                    return corrected_reg

            corrected_body = _detail_transfer(corrected_body, refined_body, "body")
            if has_overlap:
                # noinspection PyUnboundLocalVariable
                corrected_head = _detail_transfer(corrected_head, refined_head, "head")
                # noinspection PyUnboundLocalVariable
                corrected_tail = _detail_transfer(corrected_tail, refined_tail, "tail")
            report_parts.append("detail_transfer=%.2f" % d)
            log_.info("H3ClipRefiner: detail transfer applied successfully")

        # --- Correct head/tail to match corrected_body statistics ---
        # Only run if there's an actual correction (statistical or detail transfer).
        body_changed = not torch.allclose(corrected_body, body, rtol=1e-4, atol=1e-5)
        if has_overlap and (body_changed or refined_body is not None):
            # Measure corrected_body statistics (the target for head/tail)
            target_ch_means = corrected_body.mean(dim=(1, 2, 3))  # [C]
            target_ch_stds = corrected_body.std(dim=(1, 2, 3))    # [C]

            # Helper to correct a region (head or tail) to match target stats
            def _correct_region(region, region_name):
                if region is None:
                    return None
                reg_ch_means = region.mean(dim=(1, 2, 3))
                reg_ch_stds = region.std(dim=(1, 2, 3)).clamp(min=1e-6)

                # Per-channel affine to match corrected_body
                a_reg = target_ch_stds / reg_ch_stds
                b_reg = target_ch_means - a_reg * reg_ch_means
                corrected_reg = region * a_reg.view(c, 1, 1, 1) + b_reg.view(c, 1, 1, 1)

                # Apply strength to the correction delta (0 = no statistical correction)
                corrected_reg = region + (corrected_reg - region) * strength

                # Apply band_gain if it was computed (statistical path only)
                # Apply to the AFFINE-CORRECTED region's high band (consistent with body)
                if band_gain is not None:
                    reg_flat = corrected_reg.reshape(c * region.shape[1], h, w)
                    reg_split = _band_split(reg_flat, sigma=2.0)
                    if reg_split is not None:
                        reg_lo, reg_hi = reg_split
                        corrected_reg = (reg_lo + reg_hi * band_gain).reshape(
                            c, region.shape[1], h, w)

                log_.info("H3ClipRefiner: %s corrected to match body (a=%s, b=%s)",
                          region_name, a_reg.tolist(), b_reg.tolist())
                return corrected_reg

            # noinspection PyUnboundLocalVariable
            corrected_head = _correct_region(corrected_head, "head")
            # noinspection PyUnboundLocalVariable
            corrected_tail = _correct_region(corrected_tail, "tail")
        elif has_overlap:
            # No correction needed for body, keep head/tail as-is (already set above)
            pass

        # --- Reassemble: head/body/tail all corrected, no ramp needed ---
        # Statistics match exactly at boundaries, so no step discontinuity.
        # The crossfade sees consistently-corrected overlap regions on both sides.
        if has_overlap:
            # noinspection PyUnboundLocalVariable
            corrected = torch.cat([corrected_head, corrected_body, corrected_tail], dim=1)
        else:
            corrected = corrected_body

        # --- Build final report ---
        report = ", ".join(report_parts)
        tags = []
        if refined_body is not None:
            tags.append("refine+detail")
        if band_gain is not None:
            # noinspection PyStringFormat
            tags.append("gain=%.4f" % band_gain)
        tags.append("strength=%.2f" % strength)
        tags.append("ctx=%df/%dt" % (int(context_length), overlap))
        report += " [CORRECTED - " + ", ".join(tags) + "]"
        if has_overlap:
            report += " [overlap-corrected]"

        log_.info("H3ClipRefiner: applied correction, strength=%.2f, "
                  "ctx=%d frames/%d tokens", strength, int(context_length), overlap)
        
        # --- Reconstruct AV latent ---
        if had_batch:
            corrected = corrected.unsqueeze(0)
        
        out = dict(latent)
        out["samples"] = _AVStreamPair(corrected, audio_latent)
        
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
            lines = ["H3 clip purge (DRY RUN) in %s - nothing was deleted:" % d]  # type: list[str]
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
