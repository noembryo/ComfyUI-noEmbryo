import hashlib
from pathlib import Path
from typing import Iterable
import os
import json
import shutil

from PIL import (Image, ImageOps, ImageSequence, ImageFile, UnidentifiedImageError, )
import numpy as np
import torch

import folder_paths
from aiohttp import web
from server import PromptServer


def _pillow(fn, arg):
    prev_value = None
    try:
        x = fn(arg)
    except (OSError, UnidentifiedImageError, ValueError):
        # PIL issues #4472 and #2445, also fixes ComfyUI issue #3416
        prev_value = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        x = fn(arg)
    finally:
        if prev_value is not None:
            ImageFile.LOAD_TRUNCATED_IMAGES = prev_value
    return x


def _pil_to_image_mask(img: 'Image.Image | Iterable[Image.Image]',
                       output_image: 'list[torch.Tensor] | None',
                       output_mask: 'list[torch.Tensor] | None'):
    output_images = []
    output_masks = []
    w, h = None, None

    excluded_formats = ['MPO']

    if not isinstance(img, Iterable):
        if img.format not in excluded_formats:
            img = ImageSequence.Iterator(img)
        else:
            img = [img]

    for i in img:
        i: Image.Image
        i = _pillow(ImageOps.exif_transpose, i)

        if i.mode == 'I':
            i = i.point(lambda x: x * (1 / 255))

        if len(output_images) == 0 and len(output_masks) == 0:
            w = i.size[0]
            h = i.size[1]
        elif i.size[0] != w or i.size[1] != h:
            continue

        if output_image is not None:
            image = i.convert("RGB")

            image = np.array(image).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]
            output_images.append(image)

        if output_mask is not None:
            if 'A' in i.getbands():
                mask = np.array(i.getchannel('A')).astype(np.float32) / 255.0
                mask = 1. - torch.from_numpy(mask)
            elif i.mode == 'P' and 'transparency' in i.info:
                # https://github.com/comfyanonymous/ComfyUI/pull/7539
                mask = np.array(i.convert('RGBA').getchannel('A')).astype(
                    np.float32) / 255.0
                mask = 1. - torch.from_numpy(mask)
            else:
                mask = torch.zeros((64, 64), dtype=torch.float32, device="cpu")
            # (H, W) -> (1, H, W)
            mask = mask.unsqueeze(0)
            output_masks.append(mask)

    if len(output_images) > 1:
        if output_image is not None:
            output_image[:] = [torch.cat(output_images, dim=0)]
        if output_mask is not None:
            output_mask[:] = [torch.cat(output_masks, dim=0)]
    else:
        if output_image is not None:
            output_image[:] = [output_images[0]]
        if output_mask is not None:
            output_mask[:] = [output_masks[0]]


def _parse_crop(crop, width, height):
    """ Return (x0, y0, x1, y1) pixel box, or None for full image.
    Crop is JSON with normalized coords: {"x","y","w","h"} in 0..1.
    Empty / invalid crop means no crop.
    """
    if not crop:
        return None
    try:
        data = json.loads(crop)
        x = float(data["x"])
        y = float(data["y"])
        w = float(data["w"])
        h = float(data["h"])
    except (ValueError, KeyError, TypeError):
        return None
    x0 = max(0, min(width - 1, round(x * width)))
    y0 = max(0, min(height - 1, round(y * height)))
    x1 = max(x0 + 1, min(width, round((x + w) * width)))
    y1 = max(y0 + 1, min(height, round((y + h) * height)))
    if x0 == 0 and y0 == 0 and x1 == width and y1 == height:
        return None
    return x0, y0, x1, y1


def _compute_downscaled_size(width, height, max_megapixels):
    """ Return (new_w, new_h) if (width, height) exceeds max_megapixels, else None.
    Downscale-only (never upscales) and aspect-preserving.
    1.0 megapixels == 1024 x 1024 px, matching ComfyUI's ImageScaleToTotalPixels
    convention. A max_megapixels of 0 (or falsy) disables the cap entirely.
    """
    try:
        max_megapixels = float(max_megapixels)
    except (TypeError, ValueError):
        return None
    if max_megapixels <= 0:
        return None
    max_pixels = max_megapixels * 1024 * 1024
    current_pixels = width * height
    if current_pixels <= max_pixels:
        return None
    scale = (max_pixels / current_pixels) ** 0.5
    new_w = max(1, round(width * scale))
    new_h = max(1, round(height * scale))
    return new_w, new_h


def _compute_center_crop_size(src_w, src_h, dst_w, dst_h):
    """ Return (crop_w, crop_h) — the aspect-fit central box of the source that,
    when resized to exactly (dst_w, dst_h), forces the output size with a center
    crop around the middle. Returns None when forcing is disabled (either target
    dim non-positive) or the source is already exactly the target size.
    """
    if dst_w <= 0 or dst_h <= 0:
        return None  # forcing disabled — both width and height must be > 0
    if src_w == dst_w and src_h == dst_h:
        return None  # already exact size — no-op
    src_ratio = src_w / src_h
    dst_ratio = dst_w / dst_h
    if src_ratio > dst_ratio:
        # Source is wider than the target ratio → crop the sides, keep full height.
        crop_h = src_h
        crop_w = max(1, round(src_h * dst_ratio))
    else:
        # Source is taller → crop the top/bottom, keep full width.
        crop_w = src_w
        crop_h = max(1, round(src_w / dst_ratio))
    return crop_w, crop_h


class LoadImageFromPath:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("STRING", {"default": r"ComfyUI_00001_-assets\ComfyUI_00001_.png [output]"})},
        }

    CATEGORY = "noEmbryo"

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load_image"

    @staticmethod
    def load_image(image):
        image_path = LoadImageFromPath._resolve_path(image)

        i = _pillow(Image.open, image_path)

        image = []
        mask = []
        _pil_to_image_mask(i, image, mask)
        return image[0], mask[0]

    @staticmethod
    def _resolve_path(image) -> Path:
        # Keep support for the old annotated forms
        name, base_dir = folder_paths.annotated_filepath(image)
        if base_dir is not None:
            # Annotated path – still go through the secure helper
            return Path(folder_paths.get_annotated_filepath(image))

        # noinspection PyTypeChecker
        p = Path(image).expanduser()  # No annotation → treat as a real filesystem path
        if not p.is_absolute():
            # Relative path without annotation → relative to input (old behaviour)
            p = Path(folder_paths.get_input_directory()) / p
        return p.resolve()

    @classmethod
    def IS_CHANGED(cls, image):
        image_path = LoadImageFromPath._resolve_path(image)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(cls, image):
        if image is None:
            return True
        try:
            image_path = LoadImageFromPath._resolve_path(image)
        except ValueError as e:
            return str(e)
        if not image_path.exists():
            return "Invalid image path: {}".format(image_path)
        if not image_path.is_file():
            return "Path is not a file: {}".format(image_path)
        return True


# Global cache to track image paths and ClipSpace mappings
_image_path_cache = {}
_clipspace_mappings = {}  # Maps expected filename -> actual filename


class LoadImageFromPathEnhanced(LoadImageFromPath):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("STRING", {"default": "",
            "tooltip": 'Paste an absolute path, (or a relative one with a prefix input/) '
                       'to an image file. Or use "Browse" to pick a file.', }),
                             "crop": ("STRING",
                                      {"default": "",
                                       "tooltip": "Managed by the crop editor on the node"
                                                  " — no need to edit by hand.",}),
            "max_megapixels": ("FLOAT",
                               {"default": 0.0, "min": 0.0, "max": 100.0, "step": 0.01,
                "tooltip": "Cap the output (crop, or full image if uncropped) to this many "
                           "megapixels, downscaling only if it's bigger.\nSmaller images are "
                           "left untouched.\n1.0 = 1024x1024 px. 0 disables the cap.",}), },
            "optional": {"width": ("INT", {"forceInput": True, "min": 0, "max": 100000,
                                           "step": 1,
                            "tooltip": "Force the output width in px (upscale or downscale), "
                                       "center-cropping first if the aspect ratio differs.\n"
                                       "Leave disconnected (None) to keep natural width.\n"
                                       "Only applies if BOTH width and height are connected, and when "
                                       "set (not 0), and it overrides max_megapixels.", }),
                        "height": ("INT", {"forceInput": True, "min": 0, "max": 100000, "step": 1,
                            "tooltip": "Force the output height in px (upscale or"
                                       " downscale), center-cropping first if the aspect"
                                       " ratio differs.\nLeave disconnected (None) to keep"
                                       " natural height.\nOnly takes effect if BOTH width"
                                       " and height are connected, when set (not 0), and"
                                       " it overrides max_megapixels.", }), }, }

    CATEGORY = "image"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("IMAGE", "MASK", "path")
    FUNCTION = "load_image_enhanced"
    DESCRIPTION = (
        " Load an image from any path (paste or Browse). Drag on the preview to"
        " crop; drag inside to move; drag corners to resize; click outside the"
        " selection to clear. With no crop drawn, the full image is output.\n"
        " If max_megapixels is greater than 0, the output (crop or full image)"
        " is downscaled to fit within it, aspect ratio preserved; images already"
        " at or under the cap are left untouched. A value of 0 disables the cap.\n"
        " If both width and height are connected, the output (crop or full"
        " image) is forced to exactly that size — upscaling or downscaling, with"
        " a center crop first if the aspect ratio differs. This overrides"
        " max_megapixels.")

    def load_image_enhanced(self, image, crop="", max_megapixels=0.0, width=None,
                            height=None):
        # Optional inputs arrive as None when unconnected — treat as "disabled".
        width = 0 if width is None else int(width)
        height = 0 if height is None else int(height)

        # Get the full path
        image_path = LoadImageFromPath._resolve_path(image)

        # Call the parent class's load_image method
        image_tensor, mask = super().load_image(image)

        # When there's no alpha channel, load_image's fallback mask is a fixed
        # 64x64 "null mask" (ComfyUI's usual convention) — it does NOT match
        # the image's pixel dimensions. Cropping/resizing below index into the
        # mask using the image's own coordinates, so normalize it to the
        # image's size first (still an all-zero mask, just the right shape).
        img_h, img_w = image_tensor.shape[1], image_tensor.shape[2]
        if mask.shape[1] != img_h or mask.shape[2] != img_w:
            mask = torch.zeros((mask.shape[0], img_h, img_w), dtype=mask.dtype,
                device=mask.device)

        # Apply interactive crop (normalized coords from the frontend)
        box = _parse_crop(crop, image_tensor.shape[2], image_tensor.shape[1])
        if box is not None:
            x0, y0, x1, y1 = box
            image_tensor = image_tensor[:, y0:y1, x0:x1, :]
            mask = mask[:, y0:y1, x0:x1]

        # If BOTH width and height are > 0, the output is forced to exactly that
        # size — upscaling or downscaling, with a center crop first if the aspect
        # ratio differs. This overrides max_megapixels (the user's explicit
        # target size IS the final size).
        out_h, out_w = image_tensor.shape[1], image_tensor.shape[2]
        force = _compute_center_crop_size(out_w, out_h, width, height)
        if force is not None:
            crop_w, crop_h = force
            # Center-crop the source to the target aspect ratio.
            x0 = max(0, (out_w - crop_w) // 2)
            y0 = max(0, (out_h - crop_h) // 2)
            image_tensor = image_tensor[:, y0:y0 + crop_h, x0:x0 + crop_w, :]
            mask = mask[:, y0:y0 + crop_h, x0:x0 + crop_w]
            # Resize the center-cropped box to exactly width x height.
            new_w, new_h = int(width), int(height)
            image_tensor = torch.nn.functional.interpolate(
                image_tensor.permute(0, 3, 1, 2), size=(new_h, new_w), mode="bilinear",
                antialias=True, ).permute(0, 2, 3, 1).clamp(0.0, 1.0)
            mask = torch.nn.functional.interpolate(mask.unsqueeze(1), size=(new_h, new_w),
                mode="bilinear", antialias=True, ).squeeze(1).clamp(0.0, 1.0)
        else:
            # Cap the output (crop, or full image if uncropped) to max_megapixels.
            # Downscale-only: images already at or under the cap pass through
            # untouched. Skipped entirely when size forcing is active above.
            target = _compute_downscaled_size(out_w, out_h, max_megapixels)
            if target is not None:
                new_w, new_h = target
                image_tensor = torch.nn.functional.interpolate(
                    image_tensor.permute(0, 3, 1, 2), size=(new_h, new_w),
                    mode="bilinear", antialias=True, ).permute(0, 2, 3, 1).clamp(0.0, 1.0)
                mask = torch.nn.functional.interpolate(mask.unsqueeze(1),
                    size=(new_h, new_w), mode="bilinear", antialias=True, ).squeeze(
                    1).clamp(0.0, 1.0)

        # Register this image in our cache for mask editor support
        filename = os.path.basename(str(image_path))
        _image_path_cache[filename] = str(image_path)
        _image_path_cache[str(image_path)] = str(image_path)

        # Return image, mask, AND the original input path string
        return image_tensor, mask, image

    @classmethod
    def IS_CHANGED(cls, image, crop="", max_megapixels=0.0, width=0, height=0):
        image_path = LoadImageFromPath._resolve_path(image)
        m = hashlib.sha256()
        with open(image_path, 'rb') as f:
            m.update(f.read())
        m.update(str(crop).encode("utf-8"))
        m.update(str(max_megapixels).encode("utf-8"))
        m.update(str(width).encode("utf-8"))
        m.update(str(height).encode("utf-8"))
        return m.digest().hex()

    @classmethod
    def VALIDATE_INPUTS(cls, image, crop="", max_megapixels=0.0, width=0, height=0):
        return LoadImageFromPath.VALIDATE_INPUTS(image)


# Middleware to handle clipspace file resolution
@web.middleware
async def clipspace_resolver_middleware(request, handler):
    """ Middleware to intercept /api/view requests and resolve clipspace filename
    mismatches.
    This fixes the issue where mask editor looks for 'clipspace-mask-X.png' but 
    the actual file is 'clipspace-painted-masked-X.png'
    """
    if request.path == '/api/view':
        filename = request.query.get('filename', '')
        subfolder = request.query.get('subfolder', '')

        # Only intercept clipspace requests looking for 'clipspace-mask-' files
        if subfolder == 'clipspace' and filename.startswith('clipspace-mask-'):
            input_dir = folder_paths.get_input_directory()
            clipspace_dir = os.path.join(input_dir, 'clipspace')

            # Check if the requested file exists
            requested_path = os.path.join(clipspace_dir, filename)

            if not os.path.exists(requested_path):
                # Try to find the actual file with 'painted-masked' naming
                number = filename.replace('clipspace-mask-', '').replace('.png', '')
                alternative_filename = f'clipspace-painted-masked-{number}.png'
                alternative_path = os.path.join(clipspace_dir, alternative_filename)

                if os.path.exists(alternative_path):
                    # Create a symlink or copy to the expected filename
                    try:
                        # Try symlink first (faster)
                        if os.name != 'nt':  # Unix-like systems
                            if not os.path.exists(requested_path):
                                os.symlink(alternative_path, requested_path)
                        else:  # Windows - use copy instead
                            shutil.copy2(alternative_path, requested_path)
                    except Exception as e:
                        print(f"[IB] Could not create link/copy: {e}")

    # Continue with normal handling
    return await handler(request)


# Register middleware
PromptServer.instance.app.middlewares.append(clipspace_resolver_middleware)


# Server endpoints for file browsing
@PromptServer.instance.routes.get("/noembryo/browse_directory")
async def browse_directory(request):
    """Browse directories and return file listings"""
    try:
        path = request.query.get('path', '')
        sort_method = request.query.get('sort', 'name_asc')

        if not path:
            if os.name == 'nt':
                drives = [f"{d}:\\" for d in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if
                          os.path.exists(f"{d}:\\")]
                return web.json_response(
                    {'directories': drives, 'files': [], 'current_path': '',
                     'parent_path': None,  # Up stays disabled on the drive list
                     'sort_method': sort_method})
            else:  # Unix-like
                path = os.path.expanduser('~')

        path = os.path.abspath(path)

        if not os.path.exists(path) or not os.path.isdir(path):
            return web.json_response({'error': 'Invalid path'}, status=400)

        directories = []
        files = []

        try:
            items = []
            for item in os.listdir(path):
                if item.startswith('.'):
                    continue

                item_path = os.path.join(path, item)
                try:
                    if os.path.isdir(item_path):
                        item_type = 'directory'
                        stat = os.stat(item_path)
                    elif os.path.isfile(item_path):
                        ext = os.path.splitext(item)[1].lower()
                        if ext in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp',
                                   '.tiff', '.tif']:
                            item_type = 'file'
                            stat = os.stat(item_path)
                        else:
                            continue
                    else:
                        continue
                except (PermissionError, OSError):
                    continue

                items.append({'name': item, 'type': item_type, 'path': item_path,
                              'modified': stat.st_mtime})

            # Apply sorting
            if sort_method == 'name_asc':
                items.sort(key=lambda x: x['name'].lower())
            elif sort_method == 'name_desc':
                items.sort(key=lambda x: x['name'].lower(), reverse=True)
            elif sort_method == 'date_desc':
                items.sort(key=lambda x: x['modified'], reverse=True)
            elif sort_method == 'date_asc':
                items.sort(key=lambda x: x['modified'])

            for item in items:
                if item['type'] == 'directory':
                    directories.append(item['name'])
                else:
                    files.append(item['name'])

        except PermissionError:
            return web.json_response({'error': 'Permission denied'}, status=403)

        # parent_path = os.path.dirname(path) if path != os.path.dirname(path) else None
        parent = os.path.dirname(path)
        if parent == path:  # At a filesystem root (e.g. "D:\" on Windows or "/" on Unix)
            if os.name == 'nt':
                parent_path = ''  # empty string → show drive list
            else:
                parent_path = None  # already at /
        else:
            parent_path = parent

        return web.json_response(
            {'directories': directories, 'files': files, 'current_path': path,
             'parent_path': parent_path, 'sort_method': sort_method})

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)


@PromptServer.instance.routes.get("/noembryo/get_image_preview")
async def get_image_preview(request):
    """Get a preview of an image at the given path"""
    try:
        image_path = request.query.get('path', '')

        if not image_path or not os.path.exists(image_path):
            return web.json_response({'error': 'Invalid image path'}, status=400)

        img = _pillow(Image.open, image_path)
        img = _pillow(ImageOps.exif_transpose, img)

        original_width, original_height = img.size   # ← capture BEFORE downscale

        max_size = (512, 512)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)

        from io import BytesIO
        import base64

        buffer = BytesIO()
        img.save(buffer, format='PNG')
        img_str = base64.b64encode(buffer.getvalue()).decode()

        return web.json_response(
            {'preview': f'data:image/png;base64,{img_str}', 'width': original_width,
             'height': original_height})

    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)


@PromptServer.instance.routes.get("/noembryo/serve_image")
async def serve_image(request):
    """Serve image file directly"""
    try:
        image_path = request.query.get('path', '')

        if not image_path or not os.path.exists(image_path):
            return web.Response(status=404, text='Image not found')

        response = web.FileResponse(image_path)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = '*'
        return response

    except Exception as e:
        return web.Response(status=500, text=f'Error serving image: {str(e)}')
