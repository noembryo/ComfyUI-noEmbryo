// noinspection JSUnresolvedReference

import {app} from "../../scripts/app.js";
import {api} from "../../scripts/api.js";

// Store last browsed path
let lastBrowsedPath = '';

/** Return the directory of a path string (handles Windows/Unix + [input]/[output]/[temp] suffixes). */
function dirnameOf(pathStr) {
    if (!pathStr || typeof pathStr !== 'string') return '';
    // Strip annotation like " foo.png [output]"
    let p = pathStr.replace(/\s*\[[^\]]+\]\s*$/, '').trim();
    if (!p) return '';
    // Remove trailing slash
    p = p.replace(/[/\\]+$/, '');
    const lastSep = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
    if (lastSep <= 0) return p;           // bare filename or drive root
    return p.substring(0, lastSep);       // directory part
}

// File browser dialog
class FileBrowserDialog {
    constructor(initialPath) {
        // Prefer the path that is already in the node, then fall back to last browsed.
        // A URL value (http/https) isn't a filesystem path — treating its host/path as a
        // directory would produce an "Invalid path" error, so skip it and use the last
        // browsed path (or the drive list) instead.
        const fromWidget = /^https?:\/\//i.test(initialPath || '')
            ? ''
            : dirnameOf(initialPath);
        this.currentPath = fromWidget || lastBrowsedPath || '';
        this.initialFilePath = initialPath || null;   // ← keep the full path
        this.selectedFile = null;
        this.callback = null;
    }

    async show(callback) {
        this.callback = callback;

        // Create dialog overlay
        const overlay = document.createElement('div');
        overlay.style.cssText = `
		position: fixed;
		top: 0;
		left: 0;
		width: 100%;
		height: 100%;
		background: rgba(0, 0, 0, 0.8);
		display: flex;
		justify-content: center;
		align-items: center;
		z-index: 10000;
	`;

        // Make overlay focusable
        overlay.tabIndex = -1;
        overlay.focus();

        // Create dialog container
        const dialog = document.createElement('div');
        dialog.style.cssText = `
            background: #2a2a2a;
            border: 1px solid #555;
            border-radius: 6px;
            width: 85%;
            max-width: 900px;
            height: 75%;
            display: flex;
            flex-direction: column;
            color: #eee;
            font-family: system-ui, sans-serif;
            font-size: 13px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        `;

        // Create header with view controls
        const header = document.createElement('div');
        header.style.cssText = `
			padding: 12px 15px;
			border-bottom: 1px solid #444;
			display: flex;
			justify-content: space-between;
			align-items: center;
			background: #333;
		`;
        header.innerHTML = `
			<h3 style="margin: 0; font-size: 14px; font-weight: 600;">Select Image</h3>
			<div style="display: flex; gap: 8px; align-items: center;">
				<select id="sortMethod" style="background: #444; border: 1px solid #666; color: white; padding: 4px 8px; border-radius: 3px; font-size: 11px; cursor: pointer;">
					<option value="name_asc">A → Z</option>
					<option value="name_desc">Z → A</option>
					<option value="date_desc">Newest</option>
					<option value="date_asc">Oldest</option>
				</select>
				<button id="closeBrowser" style="background: #c00; border: none; color: white; padding: 4px 8px; cursor: pointer; border-radius: 3px; font-size: 12px; font-weight: bold;">✕</button>
			</div>
		`;

        // Create path display - EDITABLE VERSION
        const pathDisplay = document.createElement('div');
        pathDisplay.id = 'pathDisplay';
        pathDisplay.style.cssText = `
			padding: 8px 15px;
			background: #333;
			border-bottom: 1px solid #444;
			font-family: 'Courier New', monospace;
			font-size: 11px;
			display: flex;
			align-items: center;
			gap: 8px;
			color: #ccc;
		`;

        // Create browser content area
        const content = document.createElement('div');
        content.id = 'browserContent';
        content.style.cssText = `
            flex: 1;
            overflow-y: auto;
            padding: 4px;
            background: #1a1a1a;
        `;

        // Create preview area
        const previewArea = document.createElement('div');
        previewArea.id = 'previewArea';
        previewArea.style.cssText = `
            border-top: 1px solid #444;
            padding: 12px 15px;
            display: flex;
            gap: 12px;
            align-items: center;
            min-height: 80px;
            background: #2a2a2a;
            font-size: 12px;
        `;

        // Create footer with buttons
        const footer = document.createElement('div');
        footer.style.cssText = `
            padding: 12px 15px;
            border-top: 1px solid #444;
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            background: #333;
        `;
        footer.innerHTML = `
            <button id="selectFile" style="background: #2a7a2a; border: none; color: white; padding: 6px 16px; cursor: pointer; border-radius: 3px; font-size: 12px;" disabled>Select</button>
            <button id="cancelBrowser" style="background: #555; border: none; color: white; padding: 6px 16px; cursor: pointer; border-radius: 3px; font-size: 12px;">Cancel</button>
        `;

        dialog.appendChild(header);
        dialog.appendChild(pathDisplay);
        dialog.appendChild(content);
        dialog.appendChild(previewArea);
        dialog.appendChild(footer);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);


        const escHandler = (e) => {
            // Don't steal keys while typing in the path box, except Esc
            const inPathInput = e.target && e.target.id === 'pathInput';

            if (e.key === 'Escape') {
                e.preventDefault();
                close();
                return;
            }

            if (inPathInput) return;   // let Enter in the path box still mean "Go"

            if (e.key === 'Enter') {
                e.preventDefault();
                if (this.selectedFile) {
                    selectAndClose();   // same as clicking "Select"
                }
                return;
            }

            if (e.key === 'ArrowLeft') {
                e.preventDefault();
                document.getElementById('prevImg')?.click();
            } else if (e.key === 'ArrowRight') {
                e.preventDefault();
                document.getElementById('nextImg')?.click();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                document.getElementById('prevImg')?.click();
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                document.getElementById('nextImg')?.click();
            }
        };
        document.addEventListener('keydown', escHandler);


        // Close function
        const close = () => {
            document.removeEventListener('keydown', escHandler);
            document.body.removeChild(overlay);
        };

        // Event handlers
        overlay.querySelector('#closeBrowser').onclick = close;
        overlay.querySelector('#cancelBrowser').onclick = close;

        const selectAndClose = () => {
            if (this.selectedFile) {
                const separator = this.selectedFile.includes('\\') ? '\\' : '/';
                lastBrowsedPath = this.selectedFile.substring(0, this.selectedFile.lastIndexOf(separator));
                this.callback(this.selectedFile);
                close();
            }
        };

        overlay.querySelector('#selectFile').onclick = selectAndClose;

        // Add the sort change handler
        const sortSelect = overlay.querySelector('#sortMethod');
        sortSelect.onchange = () => {
            this.loadDirectory(this.currentPath);
        };

        // Load initial directory
        await this.loadDirectory(this.currentPath);
    }

    async loadDirectory(path) {
        this.currentPath = path;
        const content = document.getElementById('browserContent');
        const pathDisplay = document.getElementById('pathDisplay');
        const previewArea = document.getElementById('previewArea');

        content.innerHTML = '<div style="text-align: center; padding: 40px; color: #888; font-size: 12px;">Loading...</div>';
        previewArea.innerHTML = '';
        this.selectedFile = null;
        document.getElementById('selectFile').disabled = true;

        try {
            const sortMethod = document.getElementById('sortMethod') ? document.getElementById('sortMethod').value : 'name_asc';
            const response = await api.fetchApi(`/noembryo/browse_directory?path=${encodeURIComponent(path)}&sort=${encodeURIComponent(sortMethod)}`);
            const data = await response.json();

            if (data.error) {
                content.innerHTML = `<div style="color: #f55; padding: 20px; text-align: center; font-size: 12px;">Error: ${data.error}</div>`;
                return;
            }

            const sortSelect = document.getElementById('sortMethod');
            if (sortSelect && data.sort_method) {
                sortSelect.value = data.sort_method;
            }

            // Update path display
            pathDisplay.innerHTML = `
				<button id="goUp" style="background: #444; border: none; color: white; padding: 3px 8px; cursor: pointer; border-radius: 2px; font-size: 11px;">↑ Up</button>
				<input type="text" id="pathInput" value="${data.current_path || ''}" style="
					flex: 1;
					background: #1a1a1a;
					border: 1px solid #555;
					color: #eee;
					padding: 4px 8px;
					border-radius: 3px;
					font-family: 'Courier New', monospace;
					font-size: 11px;
				" />
				<button id="goPath" style="background: #444; border: none; color: white; padding: 3px 8px; cursor: pointer; border-radius: 2px; font-size: 11px;">Go</button>
			`;

            // Add event handler for the Go button and Enter key
            const pathInput = document.getElementById('pathInput');
            const goPathBtn = document.getElementById('goPath');

            const goToPath = () => {
                const newPath = pathInput.value.trim();
                if (newPath) {
                    this.loadDirectory(newPath);
                }
            };

            goPathBtn.onclick = goToPath;
            pathInput.onkeypress = (e) => {
                if (e.key === 'Enter') {
                    goToPath();
                }
            };

            const goUpBtn = document.getElementById('goUp');
            if (data.parent_path !== undefined && data.parent_path !== null) {
                // "" is valid → opens the drive list on Windows
                goUpBtn.onclick = () => this.loadDirectory(data.parent_path);
                goUpBtn.disabled = false;
                goUpBtn.style.opacity = '1';
                goUpBtn.style.cursor = 'pointer';
            } else {
                goUpBtn.disabled = true;
                goUpBtn.style.opacity = '0.4';
                goUpBtn.style.cursor = 'not-allowed';
            }

            // Build file list - LIST VIEW ONLY
            let html = '<div style="display: flex; flex-direction: column; gap: 2px;">';

            // Directories in list view
            for (const dir of data.directories) {
                const fullPath = data.current_path ? `${data.current_path}${data.current_path.endsWith('/') || data.current_path.endsWith('\\') ? '' : (path.includes('\\') ? '\\' : '/')}${dir}` : dir;
                html += `
					<div class="browser-item directory" data-path="${fullPath}" style="
						padding: 4px 8px;
						background: #333;
						border-radius: 3px;
						cursor: pointer;
						display: flex;
						align-items: center;
						gap: 6px;
						font-size: 12px;
						border: 1px solid transparent;
					">
						<span style="font-size: 16px;">📁</span>
						<span style="color: #eee;">${dir}</span>
					</div>
				`;
            }

            this.fileList = [];   // reset every time the folder changes

            // Files in list view
            for (const file of data.files) {
                // const fullPath = data.current_path ? `${data.current_path}${data.current_path.endsWith('/') || data.current_path.endsWith('\\') ? '' : (data.current_path.includes('\\') ? '\\' : '/')}${file}` : file;
                const fullPath = data.current_path
                    ? `${data.current_path}${data.current_path.endsWith('/') || data.current_path.endsWith('\\') ? '' : (data.current_path.includes('\\') ? '\\' : '/')}${file}`
                    : file;
                this.fileList.push(fullPath);
                html += `
					<div class="browser-item file" data-path="${fullPath}" style="
						padding: 4px 8px;
						background: #2a2a2a;
						border-radius: 3px;
						cursor: pointer;
						display: flex;
						align-items: center;
						gap: 6px;
						font-size: 12px;
						border: 1px solid transparent;
					">
						<span style="font-size: 16px;">🖼️</span>
						<span style="color: #ccc;">${file}</span>
					</div>
				`;
            }

            html += '</div>';
            content.innerHTML = html;

            // Add click handlers
            this.attachEventHandlers(content);

            // If we opened from a pasted path, pre-select that file (once)
            if (this.initialFilePath) {
                const target = this.initialFilePath.replace(/\s*\[[^\]]+\]\s*$/, '').trim();
                const files = content.querySelectorAll('.browser-item.file');
                for (const item of files) {
                    const p = item.getAttribute('data-path') || '';
                    // loose match: ignore slash direction and case (Windows)
                    if (p.replace(/\//g, '\\').toLowerCase() === target.replace(/\//g, '\\').toLowerCase()) {
                        item.click();  // reuses existing select + preview logic
                        item.scrollIntoView({block: 'nearest'});
                        break;
                    }
                }
                this.initialFilePath = null;                // only on the first open
            }

        } catch (error) {
            content.innerHTML = `<div style="color: #f55; padding: 20px; text-align: center; font-size: 12px;">Error loading directory: ${error.message}</div>`;
        }
    }

    attachEventHandlers(content) {
        // Click handlers for directories
        content.querySelectorAll('.browser-item.directory').forEach(item => {
            item.onclick = () => {
                const path = item.getAttribute('data-path');
                this.loadDirectory(path);
            };
            item.onmouseenter = () => {
                item.style.background = '#3a3a3a';
                item.style.border = '1px solid #666';
            };
            item.onmouseleave = () => {
                item.style.background = '#333';
                item.style.border = '1px solid transparent';
            };
        });

        // Click handlers for files
        content.querySelectorAll('.browser-item.file').forEach(item => {
            let clickTimer;
            let clickCount = 0;

            const selectFile = async () => {
                // Remove selection from other items
                content.querySelectorAll('.browser-item.file').forEach(i => {
                    i.style.background = '#2a2a2a';
                    i.style.border = '1px solid transparent';
                });

                // Highlight this item
                item.style.background = '#2a4a2a';
                item.style.border = '1px solid #3a7a3a';

                const path = item.getAttribute('data-path');
                this.selectedFile = path;
                document.getElementById('selectFile').disabled = false;

                // Load preview
                await this.loadPreview(path);
            };

            // Click handler for both single and double click
            item.onclick = (e) => {
                clickCount++;

                if (clickCount === 1) {
                    clickTimer = setTimeout(() => {
                        // Single click behavior
                        selectFile();
                        clickCount = 0;
                    }, 300);
                } else if (clickCount === 2) {
                    // Double click behavior
                    clearTimeout(clickTimer);
                    const path = item.getAttribute('data-path');
                    this.selectedFile = path;
                    const separator = path.includes('\\') ? '\\' : '/';
                    lastBrowsedPath = path.substring(0, path.lastIndexOf(separator));
                    this.callback(path);
                    document.body.querySelector('[style*="z-index: 10000"]')?.remove();
                    clickCount = 0;
                }
            };

            item.onmouseenter = () => {
                if (item.style.background !== '#2a4a2a') {
                    item.style.background = '#3a3a3a';
                    item.style.border = '1px solid #666';
                }
            };
            item.onmouseleave = () => {
                if (item.style.background !== '#2a4a2a') {
                    item.style.background = '#2a2a2a';
                    item.style.border = '1px solid transparent';
                }
            };
        });
    }


    async selectByPath(path) {
        const content = document.getElementById('browserContent');
        if (!content) return;
        const item = [...content.querySelectorAll('.browser-item.file')]
            .find(el => (el.getAttribute('data-path') || '').replace(/\//g, '\\').toLowerCase()
                === path.replace(/\//g, '\\').toLowerCase());
        if (!item) return;
        item.click();
        item.scrollIntoView({block: 'nearest'});
    }

    async loadPreview(path) {
        const previewArea = document.getElementById('previewArea');
        previewArea.innerHTML = '<div style="color: #888; font-size: 11px;">Loading preview...</div>';

        try {
            const response = await api.fetchApi(`/noembryo/get_image_preview?path=${encodeURIComponent(path)}`);
            const data = await response.json();

            if (data.error) {
                previewArea.innerHTML = `<div style="color: #f55; font-size: 11px;">Preview error: ${data.error}</div>`;
                return;
            }


            const idx = (this.fileList || []).findIndex(p =>
                p.replace(/\//g, '\\').toLowerCase() === path.replace(/\//g, '\\').toLowerCase()
            );
            const hasPrev = idx > 0;
            const hasNext = idx >= 0 && idx < (this.fileList.length - 1);

            previewArea.innerHTML = `
                <!--suppress ALL -->
<div style="display:flex; align-items:center; gap:6px;">
                    <button id="prevImg" ${hasPrev ? '' : 'disabled'}
                        style="background:#444; border:none; color:white; width:28px; height:28px;
                               border-radius:3px; cursor:${hasPrev ? 'pointer' : 'not-allowed'};
                               opacity:${hasPrev ? 1 : 0.35}; font-size:14px;">◀</button>
                    <img src="${data.preview}"
                         style="max-height:60px; max-width:100px; border:1px solid #555; border-radius:3px;" />
                    <button id="nextImg" ${hasNext ? '' : 'disabled'}
                        style="background:#444; border:none; color:white; width:28px; height:28px;
                               border-radius:3px; cursor:${hasNext ? 'pointer' : 'not-allowed'};
                               opacity:${hasNext ? 1 : 0.35}; font-size:14px;">▶</button>
                </div>
                <div style="font-size:11px; color:#ccc;">
                    <div>${data.width} × ${data.height}</div>
                    <div style="margin-top:3px; word-break:break-all; max-width:400px; color:#999;">
                        ${path.split(/[\\/]/).pop()}
                    </div>
                </div>
            `;

            document.getElementById('prevImg').onclick = () => {
                if (hasPrev) this.selectByPath(this.fileList[idx - 1]);
            };
            document.getElementById('nextImg').onclick = () => {
                if (hasNext) this.selectByPath(this.fileList[idx + 1]);
            };


        } catch (error) {
            previewArea.innerHTML = `<div style="color: #f55; font-size: 11px;">Error loading preview</div>`;
        }
    }
}


// ---------------------------------------------------------------------------
// Interactive crop editor (adapted from obvpm/comfyui-obvpm, Apache-2.0)
// Crop, plus a max_megapixels downscale cap (downscale-only, aspect preserved).
// ---------------------------------------------------------------------------

/** Return [newW, newH] if (w, h) exceeds maxMp megapixels, else null.
 * Downscale-only, aspect-preserving. 1.0 MP == 1024x1024 px. maxMp <= 0 disables it. */
function computeDownscaledSize(w, h, maxMp) {
    const mp = parseFloat(maxMp);
    if (!mp || mp <= 0 || !w || !h) return null;
    const maxPixels = mp * 1024 * 1024;
    const current = w * h;
    if (current <= maxPixels) return null;
    const scale = Math.sqrt(maxPixels / current);
    const nw = Math.max(1, Math.round(w * scale));
    const nh = Math.max(1, Math.round(h * scale));
    return [nw, nh];
}

const MARGIN = 10;
const HANDLE = 8;
const MIN_SEL = 6;
const MIN_EDITOR_H = 80;
const RESIZE_ZONE = 15;
const PREVIEW_TOOLTIP =
    "Drag to crop · Drag inside to move · Drag corners to resize · Click outside selection to clear";

app.registerExtension({
    name: "noEmbryo.LoadImageFromPath",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name !== "Load Image (from path) -noEmbryo") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const node = this;

            const imageWidget = node.widgets.find((w) => w.name === "image");
            const cropWidget = node.widgets.find((w) => w.name === "crop");
            // Looked up fresh (not cached) wherever it's read — if ComfyUI ever
            // replaces node.widgets rather than mutating it in place (reconfigure,
            // resize, Vue/Nodes 2.0 re-render), a cached reference here would go
            // stale and silently keep reading a detached widget forever.
            function getMaxMegapixels() {
                const w = node.widgets?.find((x) => x.name === "max_megapixels");
                return w ? parseFloat(w.value) : 0;
            }

            // --- TEMPORARY DIAGNOSTIC ---------------------------------------
            // Logs once per distinct (dims, maxMp, result) combo to the browser
            // console so we can see exactly what the widget/math report at
            // draw time. Safe to delete once the label issue is confirmed fixed.
            let _lastDebugKey = null;
            function debugDownscale(label, w, h, maxMp, result) {
                const key = `${label}|${w}x${h}|${maxMp}|${result ? result.join("x") : "none"}`;
                if (key === _lastDebugKey) return;
                _lastDebugKey = key;
            }
            // -----------------------------------------------------------------

            // Hide the crop JSON widget (canvas + Nodes 2.0)
            if (cropWidget) {
                cropWidget.hidden = true;
                cropWidget.options = cropWidget.options || {};
                cropWidget.options.hidden = true;
            }

            const isVueMode = () =>
                typeof LiteGraph !== "undefined" && !!LiteGraph.vueNodesMode;
            const ui = () =>
                isVueMode()
                    ? { font: 13, row: 18, handle: 12 }
                    : { font: 10, row: 14, handle: HANDLE };

            // Suppress stock node.imgs preview so only the crop editor draws
            Object.defineProperty(node, "imgs", {
                get: () => undefined,
                set: () => {},
            });

            const state = {
                img: null,
                rect: null, // normalized {x,y,w,h} or null = full image
                drag: null,
                box: null,
            };

            try {
                const saved = cropWidget?.value ? JSON.parse(cropWidget.value) : null;
                if (saved && saved.w > 0 && saved.h > 0) state.rect = saved;
            } catch (e) {
                state.rect = null;
            }

            function syncCrop() {
                if (!cropWidget) return;
                let value = "";
                if (state.rect && state.rect.w > 0.001 && state.rect.h > 0.001) {
                    const r = state.rect;
                    if (!(r.x < 0.002 && r.y < 0.002 && r.w > 0.996 && r.h > 0.996)) {
                        value = JSON.stringify({
                            x: +r.x.toFixed(4),
                            y: +r.y.toFixed(4),
                            w: +r.w.toFixed(4),
                            h: +r.h.toFixed(4),
                        });
                    }
                }
                if (cropWidget.value !== value) {
                    cropWidget.value = value;
                }
            }

            function previewHeight(width) {
                if (!state.img) return 100;
                return Math.round(width * (state.img.height / state.img.width));
            }

            function cropDims() {
                const iw = state.img.width;
                const ih = state.img.height;
                const r = state.rect;
                const x0 = Math.max(0, Math.min(iw - 1, Math.round(r.x * iw)));
                const y0 = Math.max(0, Math.min(ih - 1, Math.round(r.y * ih)));
                const x1 = Math.max(x0 + 1, Math.min(iw, Math.round((r.x + r.w) * iw)));
                const y1 = Math.max(y0 + 1, Math.min(ih, Math.round((r.y + r.h) * ih)));
                return [x1 - x0, y1 - y0];
            }

            function hitTest(px, py) {
                if (!state.rect || !state.box) return { mode: "new" };
                const handle = ui().handle;
                const { bx, by, bw, bh } = state.box;
                const sx = bx + state.rect.x * bw;
                const sy = by + state.rect.y * bh;
                const sw = state.rect.w * bw;
                const sh = state.rect.h * bh;
                const corners = {
                    nw: [sx, sy],
                    ne: [sx + sw, sy],
                    sw: [sx, sy + sh],
                    se: [sx + sw, sy + sh],
                };
                for (const [name, [cx, cy]] of Object.entries(corners)) {
                    if (Math.abs(px - cx) <= handle && Math.abs(py - cy) <= handle) {
                        return { mode: "resize", corner: name };
                    }
                }
                if (px >= sx && px <= sx + sw && py >= sy && py <= sy + sh) {
                    return { mode: "move", offX: px - sx, offY: py - sy };
                }
                return { mode: "new" };
            }

            let allocHeight;

            function boxHeight(widget, widgetY, fallback) {
                if (isVueMode()) return fallback;
                const nodeH = node.size?.[1];
                const visible = node.widgets?.filter((w) => !w.hidden);
                const isLast = !!visible && visible[visible.length - 1] === widget;
                if (nodeH == null || widgetY == null || !isLast) return fallback;
                return Math.max(MIN_EDITOR_H, nodeH - widgetY);
            }

            const editor = {
                name: "crop_editor",
                type: "noembryo_cropeditor",
                value: "",
                serialize: false,
                options: { serialize: false },

                // Do not lock height to the image aspect — keep the user's
                // node size and letterbox the preview inside the allocated area.
                computeLayoutSize: function (n) {
                    return { minHeight: MIN_EDITOR_H, maxHeight: 100000, minWidth: 0 };
                },

                draw: function (ctx, _node, widgetWidth, y, H, lowQuality) {
                    const u = ui();
                    const h = boxHeight(this, y, allocHeight ?? H) - 8;
                    const x = MARGIN;
                    const nodeW = _node?.size?.[0];
                    const effWidth =
                        !isVueMode() && nodeW ? Math.min(widgetWidth, nodeW) : widgetWidth;
                    state.lastDrawW = effWidth;
                    const w = effWidth - MARGIN * 2;
                    const imgAreaH = Math.max(1, h - u.row);

                    ctx.save();

                    if (!state.img) {
                        ctx.fillStyle = "#00000033";
                        ctx.fillRect(x, y, w, h);
                        ctx.fillStyle = "#888";
                        ctx.font = `${u.font + 2}px sans-serif`;
                        ctx.textAlign = "center";
                        ctx.textBaseline = "middle";
                        ctx.fillText("no image", x + w / 2, y + h / 2);
                        ctx.restore();
                        return;
                    }

                    const scale = Math.min(w / state.img.width, imgAreaH / state.img.height);
                    const bw = state.img.width * scale;
                    const bh = state.img.height * scale;
                    const bx = x + (w - bw) / 2;
                    const by = y + (imgAreaH - bh) / 2;
                    state.box = { bx, by, bw, bh };
                    ctx.drawImage(state.img, bx, by, bw, bh);

                    if (state.rect && !lowQuality) {
                        const sx = bx + state.rect.x * bw;
                        const sy = by + state.rect.y * bh;
                        const sw = state.rect.w * bw;
                        const sh = state.rect.h * bh;

                        ctx.beginPath();
                        ctx.rect(bx, by, bw, bh);
                        ctx.rect(sx, sy, sw, sh);
                        ctx.fillStyle = "rgba(0,0,0,0.55)";
                        ctx.fill("evenodd");

                        ctx.strokeStyle = "#4af";
                        ctx.lineWidth = 1;
                        ctx.strokeRect(sx, sy, sw, sh);
                        ctx.fillStyle = "#4af";
                        for (const [hx, hy] of [
                            [sx, sy],
                            [sx + sw, sy],
                            [sx, sy + sh],
                            [sx + sw, sy + sh],
                        ]) {
                            ctx.fillRect(hx - 2.5, hy - 2.5, 5, 5);
                        }

                        ctx.font = `${u.font}px sans-serif`;
                        ctx.textAlign = "left";
                        ctx.textBaseline = "alphabetic";
                        const pillH = u.font + 2;
                        const drawPill = (segments, ty) => {
                            const widths = segments.map((s) => ctx.measureText(s[0]).width);
                            const tw = widths.reduce((a, b) => a + b, 0);
                            const tx = Math.max(
                                bx,
                                Math.min(sx + (sw - tw - 6) / 2, bx + bw - tw - 6)
                            );
                            ctx.fillStyle = "rgba(0,0,0,0.6)";
                            ctx.fillRect(tx, ty - pillH + 3, tw + 6, pillH);
                            let cx = tx + 3;
                            for (const [i, [text, color]] of segments.entries()) {
                                ctx.fillStyle = color;
                                ctx.fillText(text, cx, ty);
                                cx += widths[i];
                            }
                        };
                        const [pw, ph] = cropDims();
                        const _maxMp1 = getMaxMegapixels();
                        const cropDown = computeDownscaledSize(pw, ph, _maxMp1);
                        debugDownscale("crop-pill", pw, ph, _maxMp1, cropDown);
                        const cropSegments = cropDown
                            ? [
                                  ["Downscaled to: ", "#fff"],
                                  [`${cropDown[0]} x ${cropDown[1]}`, "#4af"],
                              ]
                            : [[`${pw} x ${ph}`, "#fff"]];
                        drawPill(
                            cropSegments,
                            sy > y + pillH + 2 ? sy - 3 : sy + pillH - 1
                        );
                    }

                    if (!lowQuality) {
                        const lg = typeof LiteGraph !== "undefined" ? LiteGraph : {};
                        const textColor = lg.WIDGET_TEXT_COLOR || "#ddd";
                        const MUTED_ALPHA = 0.45;
                        const iw = state.img.width;
                        const ih = state.img.height;
                        const segments = [
                            ["Full: ", true],
                            [`${iw} x ${ih}`, false],
                        ];
                        // With no crop drawn, the full image IS the output — show the
                        // megapixel cap here too, same as the per-crop pill does.
                        if (!state.rect) {
                            const _maxMp2 = getMaxMegapixels();
                            const fullDown = computeDownscaledSize(iw, ih, _maxMp2);
                            debugDownscale("full-label", iw, ih, _maxMp2, fullDown);
                            if (fullDown) {
                                segments.push([" — Downscaled to: ", true]);
                                segments.push([
                                    `${fullDown[0]} x ${fullDown[1]}`,
                                    false,
                                ]);
                            }
                        }
                        ctx.font = `${u.font}px sans-serif`;
                        ctx.textBaseline = "alphabetic";
                        ctx.textAlign = "left";
                        ctx.fillStyle = textColor;
                        const ty = y + h - 3;
                        const total = segments.reduce(
                            (sum, s) => sum + ctx.measureText(s[0]).width,
                            0
                        );
                        // Clamp instead of pure-centering (mirrors the crop pill's
                        // clamp) — with the downscale text appended this line can
                        // exceed the available width, and an unclamped center
                        // pushes cx negative, drawing it clipped off the node.
                        let cx = Math.max(
                            x,
                            Math.min(x + (w - total) / 2, x + w - total)
                        );
                        const prevAlpha = ctx.globalAlpha;
                        for (const [text, muted] of segments) {
                            ctx.globalAlpha = muted ? prevAlpha * MUTED_ALPHA : prevAlpha;
                            ctx.fillText(text, cx, ty);
                            cx += ctx.measureText(text).width;
                        }
                        ctx.globalAlpha = prevAlpha;
                    }

                    ctx.restore();
                },

                mouse: function (event, pos, _node) {
                    if (!state.img || !state.box) return false;
                    const t = event.type;
                    const px = pos[0];
                    const py = pos[1];
                    const { bx, by, bw, bh } = state.box;
                    const clampX = (v) => Math.max(bx, Math.min(bx + bw, v));
                    const clampY = (v) => Math.max(by, Math.min(by + bh, v));

                    if (t === "pointerdown" || t === "mousedown") {
                        if (px < bx || px > bx + bw || py < by || py > by + bh) {
                            return false;
                        }
                        state.drag = {
                            ...hitTest(px, py),
                            startX: px,
                            startY: py,
                            moved: false,
                        };
                        const el = event.target;
                        if (el?.style) {
                            el.style.cursor =
                                state.drag.mode === "move"
                                    ? "grabbing"
                                    : state.drag.mode === "resize"
                                      ? state.drag.corner === "nw" ||
                                        state.drag.corner === "se"
                                          ? "nwse-resize"
                                          : "nesw-resize"
                                      : "crosshair";
                        }
                        this.triggerDraw?.();
                        return true;
                    }

                    const drag = state.drag;
                    if (!drag) return false;

                    if (t === "pointermove" || t === "mousemove") {
                        if (Math.abs(px - drag.startX) + Math.abs(py - drag.startY) > 2) {
                            drag.moved = true;
                        }
                        if (drag.mode === "new") {
                            const x0 = clampX(Math.min(drag.startX, px));
                            const y0 = clampY(Math.min(drag.startY, py));
                            const x1 = clampX(Math.max(drag.startX, px));
                            const y1 = clampY(Math.max(drag.startY, py));
                            if (x1 - x0 >= MIN_SEL && y1 - y0 >= MIN_SEL) {
                                state.rect = {
                                    x: (x0 - bx) / bw,
                                    y: (y0 - by) / bh,
                                    w: (x1 - x0) / bw,
                                    h: (y1 - y0) / bh,
                                };
                            }
                        } else if (drag.mode === "move" && state.rect) {
                            let nx = (clampX(px - drag.offX) - bx) / bw;
                            let ny = (clampY(py - drag.offY) - by) / bh;
                            nx = Math.max(0, Math.min(1 - state.rect.w, nx));
                            ny = Math.max(0, Math.min(1 - state.rect.h, ny));
                            state.rect.x = nx;
                            state.rect.y = ny;
                        } else if (drag.mode === "resize" && state.rect) {
                            const r = state.rect;
                            let x0 = bx + r.x * bw;
                            let y0 = by + r.y * bh;
                            let x1 = x0 + r.w * bw;
                            let y1 = y0 + r.h * bh;
                            if (drag.corner.includes("w")) x0 = clampX(px);
                            if (drag.corner.includes("e")) x1 = clampX(px);
                            if (drag.corner.includes("n")) y0 = clampY(py);
                            if (drag.corner.includes("s")) y1 = clampY(py);
                            if (
                                Math.abs(x1 - x0) >= MIN_SEL &&
                                Math.abs(y1 - y0) >= MIN_SEL
                            ) {
                                state.rect = {
                                    x: (Math.min(x0, x1) - bx) / bw,
                                    y: (Math.min(y0, y1) - by) / bh,
                                    w: Math.abs(x1 - x0) / bw,
                                    h: Math.abs(y1 - y0) / bh,
                                };
                            }
                        }
                        this.triggerDraw?.();
                        return true;
                    }

                    if (t === "pointerup" || t === "mouseup") {
                        if (drag.mode === "new" && !drag.moved) {
                            state.rect = null;
                        }
                        state.drag = null;
                        if (event.target?.style) event.target.style.cursor = "";
                        syncCrop();
                        this.triggerDraw?.();
                        return true;
                    }
                    return false;
                },
            };

            // Browse button under the path field and above the crop preview.
            // Must be added BEFORE the crop editor so creation order matches
            // visual order (and the editor stays the last, growable widget).
            node.addWidget("button", "🔍 Browse...", null, () => {
                const currentPath = imageWidget ? imageWidget.value : "";
                const browser = new FileBrowserDialog(currentPath);
                browser.show(async (filePath) => {
                    if (imageWidget) {
                        imageWidget.value = filePath;
                        state.rect = null;
                        syncCrop();
                        // Keep current node size; letterbox the new image
                        loadImageFromPath();
                        app.graph.setDirtyCanvas(true, true);
                    }
                });
            });

            const editorWidget = node.addCustomWidget(editor);

            function cursorOutside(px, py) {
                const w = node.size?.[0];
                const h = node.size?.[1];
                if (w == null || h == null) return "default";
                if (py <= h && py >= h - RESIZE_ZONE) {
                    if (px >= w - RESIZE_ZONE) return "nwse-resize";
                    if (px <= RESIZE_ZONE) return "nesw-resize";
                }
                return "default";
            }
            function cursorFor(px, py) {
                if (!state.img || !state.box) return cursorOutside(px, py);
                const { bx, by, bw, bh } = state.box;
                if (px < bx || px > bx + bw || py < by || py > by + bh) {
                    return cursorOutside(px, py);
                }
                const hit = hitTest(px, py);
                if (hit.mode === "resize") {
                    return hit.corner === "nw" || hit.corner === "se"
                        ? "nwse-resize"
                        : "nesw-resize";
                }
                if (hit.mode === "move") return "grab";
                return state.rect ? "not-allowed" : "crosshair";
            }
            const prevMouseMove = node.onMouseMove;
            node.onMouseMove = function (e, pos, graphCanvas) {
                prevMouseMove?.apply(this, arguments);
                const el = graphCanvas?.canvas || app.canvas?.canvas;
                if (!el) return;
                if (!state.drag) el.style.cursor = cursorFor(pos[0], pos[1]);
                // Native tooltip while the pointer is over the image preview
                if (state.img && state.box) {
                    const { bx, by, bw, bh } = state.box;
                    const x = pos[0], y = pos[1];
                    const over =
                        x >= bx && x <= bx + bw && y >= by && y <= by + bh;
                    el.title = over ? PREVIEW_TOOLTIP : "";
                } else {
                    el.title = "";
                }
            };
            const prevMouseLeave = node.onMouseLeave;
            node.onMouseLeave = function () {
                prevMouseLeave?.apply(this, arguments);
                const el = app.canvas?.canvas;
                if (el) {
                    el.style.cursor = "";
                    el.title = "";
                }
            };

            Object.defineProperty(editorWidget, "computedHeight", {
                configurable: true,
                get() {
                    if (isVueMode() || allocHeight == null) return undefined;
                    const box = boxHeight(this, this.y, allocHeight);
                    return Math.max(0, box - RESIZE_ZONE);
                },
                set(v) {
                    allocHeight = v;
                },
            });
            Object.defineProperty(editorWidget, "width", {
                configurable: true,
                get: () => undefined,
                set: () => {},
            });

            // Load preview from arbitrary filesystem path via our serve endpoint
            let loadSeq = 0;
            function loadImageFromPath() {
                const seq = ++loadSeq;
                const raw = imageWidget?.value || "";
                const path = String(raw).replace(/\s*\[[^\]]+\]\s*$/, "").trim();
                if (!path) {
                    state.img = null;
                    node.setDirtyCanvas?.(true, true);
                    editorWidget.triggerDraw?.();
                    return;
                }
                // URLs are loaded directly by the browser (cross-origin <img>
                // display + canvas draw works fine — no read-back is used, so
                // there's no canvas taint issue). Local paths go through our
                // serve proxy endpoint.
                const url =
                    /^https?:\/\//i.test(path)
                        ? path
                        : `/noembryo/serve_image?path=${encodeURIComponent(path)}` +
                          `&filename=${encodeURIComponent(path.split(/[\\/]/).pop())}` +
                          `&t=${Date.now()}`;

                const img = new Image();
                img.onload = () => {
                    if (seq !== loadSeq) return;
                    state.img = img;
                    // Never resize the node to the image ratio — letterbox only.
                    node.setDirtyCanvas?.(true, true);
                    editorWidget.triggerDraw?.();
                };
                img.onerror = () => {
                    if (seq !== loadSeq) return;
                    state.img = null;
                    node.setDirtyCanvas?.(true, true);
                    editorWidget.triggerDraw?.();
                };
                img.src = url;
            }

            // Redraw the preview labels when the megapixel cap changes
            const maxMpWidgetForCallback = node.widgets.find(
                (w) => w.name === "max_megapixels"
            );
            if (maxMpWidgetForCallback) {
                const prevMaxMpCallback = maxMpWidgetForCallback.callback;
                maxMpWidgetForCallback.callback = function () {
                    const r = prevMaxMpCallback?.apply(this, arguments);
                    node.setDirtyCanvas?.(true, true);
                    editorWidget.triggerDraw?.();
                    return r;
                };
            }

            // Path typed / changed → clear crop and reload preview
            if (imageWidget) {
                const prevCallback = imageWidget.callback;
                imageWidget.callback = function () {
                    const r = prevCallback?.apply(this, arguments);
                    state.rect = null;
                    syncCrop();
                    loadImageFromPath();
                    return r;
                };
            }

            // Workflow load restores crop + image without widget callbacks
            const prevOnConfigure = node.onConfigure;
            node.onConfigure = function () {
                const r = prevOnConfigure?.apply(this, arguments);
                try {
                    const saved = cropWidget?.value ? JSON.parse(cropWidget.value) : null;
                    state.rect = saved && saved.w > 0 && saved.h > 0 ? saved : null;
                } catch (e) {
                    state.rect = null;
                }
                loadImageFromPath();
                return r;
            };

            loadImageFromPath();
            return result;
        };
    },
});
