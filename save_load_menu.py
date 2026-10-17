"""
noEmbryo Save/Load Menu Extension - Python Backend
Provides API routes for the Save/Load menu frontend extension.
"""

import json
import io
import os
from os.path import isfile, join, isdir, normpath
from aiohttp import web
from server import PromptServer

# Directory where saved workflows are stored (ComfyUI's user/default)
_DEFAULT_WORKFLOWS_DIR = join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "user", "default")


def _resolve_dir(base_dir, rel_path):
    """Resolve a relative path against the base directory, preventing traversal."""
    if not base_dir or not isinstance(base_dir, str):
        return None
    if not rel_path:
        return base_dir
    # Normalize and strip any leading slashes or backslashes
    rel_path = rel_path.lstrip('/\\')
    # Prevent path traversal
    if '..' in rel_path.split('/') or '..' in rel_path.split('\\'):
        return None
    resolved = join(base_dir, rel_path)
    # Ensure the resolved path is within base_dir
    base_dir_norm = normpath(base_dir)
    resolved_norm = normpath(resolved)
    if not resolved_norm.startswith(base_dir_norm + os.sep) and resolved_norm != base_dir_norm:
        return None
    return resolved


def _list_directory_contents(dir_path):
    """List both subdirectories and JSON files in a directory."""
    if not dir_path or not isdir(dir_path):
        return {"dirs": [], "files": [], "dir": dir_path or ""}
    dirs = []
    files = []
    try:
        for fname in os.listdir(dir_path):
            fpath = join(dir_path, fname)
            if isdir(fpath):
                dirs.append({"name": fname, "path": fpath})
            elif fname.lower().endswith(".json") and isfile(fpath):
                files.append({
                    "name": fname[:-5],  # strip .json
                    "path": fpath,
                })
    except Exception:
        pass
    dirs.sort(key=lambda x: x["name"])
    files.sort(key=lambda x: x["name"])
    return {"dirs": dirs, "files": files, "dir": dir_path}


def _safe_workflow_path(name, wf_dir):
    """Return a safe path for a workflow file, preventing path traversal."""
    if not name or not isinstance(name, str):
        return None
    if not wf_dir or not isinstance(wf_dir, str):
        return None
    # Strip any directory components
    safe_name = normpath(name)
    # Remove any directory separators
    safe_name = safe_name.replace(os.sep, "").replace("/", "").replace("\\", "")
    if not safe_name or safe_name in (".", ".."):
        return None
    if not safe_name.lower().endswith(".json"):
        safe_name += ".json"
    return join(wf_dir, safe_name)


@PromptServer.instance.routes.get('/noembryo/workflows_dir')
async def workflows_dir(request):
    """Return the current workflows directory path."""
    try:
        wf_dir = request.query.get('dir', '') or _DEFAULT_WORKFLOWS_DIR
        return web.json_response({"dir": wf_dir or ""})
    except Exception as e:
        return web.json_response({"error": str(e), "dir": ""})


@PromptServer.instance.routes.get('/noembryo/list_workflows')
async def list_workflows(request):
    """List directory contents (subdirs + workflow JSON files) for recursive browsing."""
    try:
        # Use the dir parameter from the request (frontend-configured directory),
        # falling back to the default
        base_dir = request.query.get('dir', '') or _DEFAULT_WORKFLOWS_DIR
        rel_path = request.query.get('path', '')
        wf_dir = _resolve_dir(base_dir, rel_path)
        result = _list_directory_contents(wf_dir)
        result["base_dir"] = base_dir
        result["rel_path"] = rel_path or ""
        print(f"[noEmbryo.SaveLoadMenu] list_workflows: base_dir={base_dir}, rel_path={rel_path}, resolved={wf_dir}, dirs={len(result['dirs'])}, files={len(result['files'])}")
        return web.json_response(result)
    except Exception as e:
        print(f"[noEmbryo.SaveLoadMenu] list_workflows error: {e}")
        return web.json_response({"error": str(e), "dirs": [], "files": [], "dir": "", "base_dir": "", "rel_path": ""})


@PromptServer.instance.routes.post('/noembryo/save_workflow')
async def save_workflow(request):
    """Save a workflow JSON to the specified directory."""
    try:
        data = await request.json()
        name = data.get("name", "")
        workflow = data.get("workflow", None)
        rel_path = data.get("path", "") or ""
        wf_dir = data.get("dir", "") or _DEFAULT_WORKFLOWS_DIR
        wf_dir = _resolve_dir(wf_dir, rel_path) or _DEFAULT_WORKFLOWS_DIR
        if not name or workflow is None:
            return web.json_response({"error": "Missing name or workflow"})
        fpath = _safe_workflow_path(name, wf_dir)
        if fpath is None:
            return web.json_response({"error": "Invalid workflow name or directory"})
        try:
            os.makedirs(wf_dir, exist_ok=True)
        except Exception:
            pass
        with io.open(fpath, "w", encoding="utf-8") as f:
            json.dump(workflow, f, ensure_ascii=False, indent=2)
        return web.json_response({"ok": True, "path": fpath, "name": name, "dir": wf_dir})
    except Exception as e:
        return web.json_response({"error": str(e)})


@PromptServer.instance.routes.get('/noembryo/load_workflow')
async def load_workflow(request):
    """Load a workflow JSON by full path."""
    try:
        full_path = request.query.get('full_path', '')
        dir_param = request.query.get('dir', '') or _DEFAULT_WORKFLOWS_DIR
        print(f"[noEmbryo.SaveLoadMenu] load_workflow: full_path={full_path}, dir={dir_param}")
        if not full_path:
            return web.json_response({"error": "Missing full_path"})
        # Security: ensure the path is within the specified directory
        base_dir_norm = normpath(dir_param)
        full_path_norm = normpath(full_path)
        print(f"[noEmbryo.SaveLoadMenu] load_workflow: base_dir_norm={base_dir_norm}, full_path_norm={full_path_norm}")
        if not full_path_norm.startswith(base_dir_norm + os.sep) and full_path_norm != base_dir_norm:
            return web.json_response({"error": "Access denied: path outside directory"})
        if not isfile(full_path):
            return web.json_response({"error": "Workflow not found: " + full_path})
        with io.open(full_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"[noEmbryo.SaveLoadMenu] load_workflow: loaded {os.path.basename(full_path)}")
        return web.json_response({"workflow": data, "name": os.path.basename(full_path), "full_path": full_path})
    except Exception as e:
        print(f"[noEmbryo.SaveLoadMenu] load_workflow error: {e}")
        return web.json_response({"error": str(e)})