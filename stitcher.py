"""MiniMax H3 Motion Context archive stitcher for ComfyUI.

Loads NikoDemon80/ComfyUI-H3-Motion-Context v0.3.x archive files
(h3_motion_context_av_v1), decodes each approved clip once, removes the
carried Motion Context head from clips after the first, and concatenates the
remaining picture/audio into one IMAGE + AUDIO pair.

This intentionally does NOT reconstruct a NestedTensor and feed the saved
files back into Motion Context. The archive format is the sampler output,
and this node is a final-media assembly tool.
"""

import fnmatch
import glob
import logging
import os
import re

import torch
import torch.nn.functional as F

import folder_paths

try:
    from safetensors.torch import load_file as st_load
except Exception:
    st_load = None

try:
    import torchaudio
except Exception:
    torchaudio = None

_LOG = logging.getLogger("h3_motion_context_archive")


INDEX_RE = re.compile(r"(?:^|_)(\d{5})(?:\.safetensors)$", re.IGNORECASE)


def _resolve_folder(path):
    p = (path or "").strip().strip('"').strip("'")
    if not p:
        p = "h3_context"
    candidates = [p, os.path.join(folder_paths.get_output_directory(), p)]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    raise FileNotFoundError(
        "H3 Motion Context Archive Stitcher: folder not found: %s\n"
        "You can use an absolute path or a path relative to ComfyUI's output folder."
        % p
    )


def _clip_number(path):
    name = os.path.basename(path)
    m = INDEX_RE.search(name)
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
        if int(last_clip) > 0 and idx > int(last_clip):
            continue
        paths.append((idx, p))
    paths.sort(key=lambda x: x[0])
    if not paths:
        raise FileNotFoundError(
            "H3 Motion Context Archive Stitcher: no numbered .safetensors files "
            "matched '%s' in %s." % (pattern, folder)
        )

    # Do not silently skip a missing numbered clip. A gap usually means an
    # approved clip was not saved, and silently stitching around it would make
    # a misleading final timeline.
    expected = paths[0][0]
    for idx, _ in paths:
        if idx != expected:
            raise ValueError(
                "H3 Motion Context Archive Stitcher: missing clip %05d between "
                "the selected archive files." % expected
            )
        expected += 1
    return paths


def _load_archive(path):
    if st_load is None:
        raise RuntimeError(
            "safetensors is unavailable in this ComfyUI Python environment."
        )
    data = st_load(path, device="cpu")
    if "video" not in data or "audio" not in data:
        raise ValueError(
            "%s is not an h3_motion_context_av_v1 archive: expected 'video' and 'audio'."
            % path
        )
    video = data["video"]
    audio = data["audio"]
    if video.ndim != 5:
        raise ValueError("%s: expected video [B,C,T,H,W], got %s" % (path, tuple(video.shape)))
    if audio.ndim != 4:
        raise ValueError("%s: expected audio [B,C,2,T], got %s" % (path, tuple(audio.shape)))
    if video.shape[0] != 1 or audio.shape[0] != 1:
        raise ValueError("%s: only batch size 1 archive clips are supported." % path)
    return video, audio


def _decode_video(vae, video_latent):
    """Decode the H3 video stream and normalize to ComfyUI IMAGE format."""
    images = vae.decode(video_latent)
    # H3's VAE normally returns [B,T,H,W,C]. Some VAE implementations can
    # return [T,H,W,C], so accept both.
    if images.ndim == 5:
        images = images.reshape(-1, *images.shape[-3:])
    elif images.ndim != 4:
        raise RuntimeError("H3 video VAE returned unexpected shape %s" % (tuple(images.shape),))
    return images.to(torch.float32).clamp(0, 1).cpu()


def _decode_audio(audio_vae, audio_latent, normalize=True):
    """Decode the H3 audio stream using the same convention as ComfyUI's VAEDecodeAudio."""
    audio = audio_vae.decode(audio_latent)
    # Current ComfyUI audio VAE returns [B,L,C]. Convert to [B,C,L].
    if audio.ndim != 3:
        raise RuntimeError("H3 audio VAE returned unexpected shape %s" % (tuple(audio.shape),))
    audio = audio.movedim(-1, 1)
    if normalize:
        std = torch.std(audio, dim=[1, 2], keepdim=True) * 5.0
        std[std < 1.0] = 1.0
        audio = audio / std
    sr = int(getattr(audio_vae, "audio_sample_rate_output",
                     getattr(audio_vae, "audio_sample_rate", 32000)))
    return {"waveform": audio.to(torch.float32).cpu(), "sample_rate": sr}


def _trim_clip(images, audio, trim_frames, fps, match_tail, trim_mode):
    """Trim a Motion Context overlap from the requested side of a decoded clip."""
    n = int(trim_frames)
    if n <= 0:
        return images, audio
    total = int(images.shape[0])
    if n >= total:
        raise ValueError(
            "Cannot trim %d frames from a decoded clip containing %d frames."
            % (n, total)
        )

    if trim_mode == "TRIM_BACK":
        out_images = images[:-n]
    else:
        out_images = images[n:]

    if audio is None:
        return out_images, None

    waveform = audio["waveform"]
    sr = int(audio["sample_rate"])
    cut = int(round((n / float(fps)) * sr))
    if cut >= waveform.shape[-1]:
        raise ValueError(
            "Audio is too short to remove the %d-frame (%0.4fs) Motion Context "
            "%s." % (n, n / float(fps),
                      "tail" if trim_mode == "TRIM_BACK" else "head")
        )

    if trim_mode == "TRIM_BACK":
        waveform = waveform[..., :-cut]
    else:
        waveform = waveform[..., cut:]

    if match_tail:
        frames_left = total - n
        want = int(round(frames_left / float(fps) * sr))
        have = int(waveform.shape[-1])
        if have > want:
            waveform = waveform[..., :want]
        elif have < want:
            waveform = F.pad(waveform, (0, want - have))

    return out_images, {"waveform": waveform, "sample_rate": sr}


def _resample_audio(audio, target_sr):
    if audio is None:
        return None
    sr = int(audio["sample_rate"])
    if sr == int(target_sr):
        return audio
    if torchaudio is None:
        raise RuntimeError(
            "Audio sample rates differ (%d vs %d), but torchaudio is unavailable "
            "to resample them." % (sr, int(target_sr))
        )
    waveform = torchaudio.functional.resample(audio["waveform"], sr, int(target_sr))
    return {"waveform": waveform, "sample_rate": int(target_sr)}


def _crossfade_boundary(prev_tail_images, cur_images, prev_tail_wave, cur_wave,
                        overlap_frames, cross_samples):
    """Crossfade the previous clip's tail with the current clip's head.

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
            theta = torch.linspace(0.0, 1.5707963267948966, n,
                                   dtype=prev_tail_wave.dtype,
                                   device=prev_tail_wave.device).view(1, 1, n)
            blend_wave = (prev_tail_wave[..., :n] * torch.cos(theta)
                          + cur_wave[..., :n] * torch.sin(theta))
    return blend_images, blend_wave


class MiniMaxH3ContextStitcher:
    """Load, decode, trim, and concatenate approved H3 Motion Context clips."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder": ("STRING", {
                    "default": "h3_context",
                    "tooltip": "Folder containing clip_00001.safetensors, clip_00002.safetensors, etc. "
                               "Absolute paths and paths relative to ComfyUI/output are accepted."
                }),
                "pattern": ("STRING", {
                    "default": "clip_*.safetensors",
                    "tooltip": "Filename glob. The final five-digit number is treated as the clip index."
                }),
                "first_clip": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "First approved clip to include."
                }),
                "last_clip": ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "Last clip to include. 0 = every clip from first_clip onward."
                }),
                "context_length": ("INT", {
                    "default": 22, "min": 0, "max": 4096,
                    "tooltip": "Number of decoded frames to remove at each clip boundary. "
                               "For NikoDemon80 v0.3.1 the normal setting is 22 frames. "
                               "In CROSSFADE mode this is the overlap length that is dissolved "
                               "between adjacent clips instead of being removed."
                }),
                "trim_mode": (["TRIM_FRONT", "TRIM_BACK", "CROSSFADE"], {
                    "default": "TRIM_FRONT",
                    "tooltip": "TRIM_FRONT: remove the first context_length frames from clips 2..N.\n"
                               "TRIM_BACK: remove the last context_length frames from clips 1..N-1.\n"
                               "CROSSFADE: keep the context_length overlap and dissolve it between "
                               "adjacent clips (video + synchronized audio) instead of removing it."
                }),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                    "tooltip": "H3 native output rate. Keep this at 24 unless your workflow deliberately changes it."
                }),
            },
            "optional": {
                "video_vae": ("VAE", {
                    "tooltip": "MiniMax H3 video VAE (FP16 or INT8 ConvRot)."
                }),
                "audio_vae": ("VAE", {
                    "tooltip": "MiniMax H3 audio VAE FP32. Required for the AUDIO output."
                }),
                "match_audio_tail": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "After removing the context head, force each remaining audio chunk to exactly "
                               "match its remaining picture duration. This follows NikoDemon80 v0.3.1's Trim node. "
                               "Ignored in CROSSFADE mode (no head/tail is removed)."
                }),
                "normalize_audio_per_clip": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Use ComfyUI's standard VAEDecodeAudio per-clip normalization. Disable if you want "
                               "raw VAE waveform levels before concatenation."
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT", "STRING")
    RETURN_NAMES = ("images", "audio", "frame_count", "report")
    FUNCTION = "stitch"
    CATEGORY = "video/minimax"
    DESCRIPTION = ("Final assembly for NikoDemon80 H3 Motion Context AV archives. "
                   "Loads numbered h3_motion_context_av_v1 files, decodes one clip at a "
                   "time, removes the carried context from the selected side of each "
                   "clip boundary, synchronizes audio, and concatenates.\n"
                   "CROSSFADE mode dissolves the overlap between adjacent clips (video "
                   "+ synchronized audio) instead of removing it.")

    @classmethod
    def IS_CHANGED(cls, folder, pattern, first_clip, last_clip, context_length, trim_mode, fps,
                   video_vae=None, audio_vae=None, match_audio_tail=True,
                   normalize_audio_per_clip=True):
        try:
            d = _resolve_folder(folder)
            files = _find_files(d, pattern, first_clip, last_clip)
            return tuple((p, os.stat(p).st_mtime_ns, os.path.getsize(p)) for _, p in files) + (
                int(context_length), str(trim_mode), float(fps), bool(match_audio_tail),
                bool(normalize_audio_per_clip),
            )
        except Exception:
            return float("NaN")

    def stitch(self, folder, pattern, first_clip, last_clip, context_length, trim_mode, fps,
               video_vae=None, audio_vae=None, match_audio_tail=True,
               normalize_audio_per_clip=True):
        if video_vae is None:
            raise ValueError("Connect your MiniMax H3 video VAE to 'video_vae'.")
        if st_load is None:
            raise RuntimeError("safetensors is not available in this ComfyUI environment.")

        d = _resolve_folder(folder)
        files = _find_files(d, pattern, first_clip, last_clip)
        _LOG.info("H3 archive stitcher: %d clip(s) selected from %s", len(files), d)

        image_parts = []
        audio_parts = []
        report_lines = []
        target_sr = None

        is_crossfade = trim_mode == "CROSSFADE"
        overlap = int(context_length)
        # Degenerate crossfade (no overlap or a single clip) falls back to a
        # plain concatenation, which is exactly what the trim modes would do.
        crossfade_active = is_crossfade and overlap > 0 and len(files) > 1

        prev_tail_img = None
        prev_tail_wave = None

        for pos, (idx, path) in enumerate(files):
            video_latent, audio_latent = _load_archive(path)
            _LOG.info("H3 archive stitcher: clip %05d latent video=%s audio=%s",
                      idx, tuple(video_latent.shape), tuple(audio_latent.shape))

            # Decode one clip at a time. The decoded result is immediately moved
            # to CPU, so a long chain does not keep every VAE result on VRAM.
            images = _decode_video(video_vae, video_latent)
            del video_latent

            audio = None
            if audio_vae is not None:
                audio = _decode_audio(audio_vae, audio_latent, normalize=normalize_audio_per_clip)
            del audio_latent

            decoded_frames = int(images.shape[0])
            is_last = pos == len(files) - 1

            if crossfade_active:
                if decoded_frames < 2 * overlap:
                    raise ValueError(
                        "CROSSFADE requires each clip to have at least 2*context_length "
                        "(%d) frames; clip %05d has %d." % (overlap, idx, decoded_frames)
                    )

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
                        raise ValueError(
                            "Audio is too short to extract a %d-frame (%0.4fs) "
                            "crossfade head/tail for clip %05d." % (overlap, overlap / float(fps), idx)
                        )

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
                    # First clip: emit head+body raw, buffer the tail for the
                    # next boundary.
                    image_parts.append(torch.cat([head_img, body_img], dim=0))
                    if audio is not None:
                        audio_parts.append(torch.cat(
                            [head_wave["waveform"], body_wave["waveform"]], dim=-1))
                    prev_tail_img = tail_img
                    prev_tail_wave = tail_wave
                else:
                    blend_img, blend_wave = _crossfade_boundary(
                        prev_tail_img, images,
                        prev_tail_wave["waveform"] if prev_tail_wave is not None else None,
                        audio["waveform"] if audio is not None else None,
                        overlap, n
                    )
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
                audio_sec = 0.0 if audio is None else audio["waveform"].shape[-1] / float(audio["sample_rate"])
                report_lines.append(
                    "clip_%05d: decoded=%d frames, crossfade=%d frames (%.4fs), kept=%d, audio=%.4fs" %
                    (idx, decoded_frames, overlap, overlap / float(fps), kept_frames, audio_sec)
                )

                del images
                if audio is not None:
                    del audio
                continue

            # --- TRIM_FRONT / TRIM_BACK path ---
            if trim_mode == "TRIM_FRONT":
                # Preserve the first clip; remove the carried context head from clips 2..N.
                should_trim = pos > 0
            else:
                # Preserve the final clip; remove the trailing context from clips 1..N-1.
                should_trim = pos < len(files) - 1
            trim = int(context_length) if should_trim else 0
            images, audio = _trim_clip(
                images, audio, trim, fps, match_audio_tail, trim_mode
            )

            image_parts.append(images)
            if audio is not None:
                if target_sr is None:
                    target_sr = int(audio["sample_rate"])
                audio = _resample_audio(audio, target_sr)
                audio_parts.append(audio["waveform"])

            kept_frames = int(images.shape[0])
            audio_sec = 0.0 if audio is None else audio["waveform"].shape[-1] / float(audio["sample_rate"])
            report_lines.append(
                "clip_%05d: decoded=%d frames, trimmed=%d, kept=%d, audio=%.4fs" %
                (idx, decoded_frames, trim, kept_frames, audio_sec)
            )

            # Explicitly drop local references before the next VAE decode.
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
        audio_seconds = (final_audio["waveform"].shape[-1] / float(final_audio["sample_rate"])
                         if final_audio is not None else 0.0)

        report_lines.append(
            "TOTAL: %d frames = %.4fs at %.3f fps; audio=%.4fs%s" %
            (frame_count, video_seconds, float(fps), audio_seconds,
             "" if final_audio is not None else " (no audio_vae connected)")
        )
        report = "\n".join(report_lines)
        _LOG.info("H3 archive stitcher finished: %d frames (%.3fs), audio %.3fs",
                  frame_count, video_seconds, audio_seconds)

        return (final_images, final_audio, frame_count, report)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3MotionContextArchiveStitcher": MiniMaxH3ContextStitcher,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3MotionContextArchiveStitcher": "MiniMax H3 Motion Context Archive Stitcher",
}