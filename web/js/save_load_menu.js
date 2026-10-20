// ============================================================================
// noEmbryo Save/Load Menu Extension
// Adds Save and Load buttons to ComfyUI's bottom corner.
// Integrates with pythongosssss comfyui-custom-scripts workflow menus.
// Uses app.loadGraphData() for proper node centering on load.
// ============================================================================
// SAFETY: This extension defaults to OFF. All DOM manipulation is wrapped in
// try/catch. No existing elements are modified — only new elements are added.
// ============================================================================

console.log("[Comfy.noEmbryo.SaveLoadMenu] JS file loaded");

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EXTENSION_NAME = "Comfy.noEmbryo.SaveLoadMenu";
const STORAGE_KEY_ENABLED = "noEmbryo_save_load_menu_enabled";
const STORAGE_KEY_DIR = "noEmbryo_save_load_menu_dir";
const STORAGE_KEY_POS = "noEmbryo_save_load_menu_pos";
const STORAGE_KEY_LAST_NAME = "noEmbryo_save_load_menu_last_name";

// Default: OFF
let enabled = false;
let workflowsDir = "";
let buttonPosition = "bottom-right"; // "bottom-left" or "bottom-right"
let lastWorkflowName = "";

// Current navigation state for recursive browsing
let currentRelPath = "";

// References to injected elements (for cleanup)
let saveBtn = null;
let loadBtn = null;
let saveDropdown = null;
let loadDropdown = null;
let isSaveOpen = false;
let isLoadOpen = false;
let reinjectTimer = null;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(msg) {
    console.log(`[${EXTENSION_NAME}] ${msg}`);
}

function warn(msg) {
    console.warn(`[${EXTENSION_NAME}] ${msg}`);
}

function error(msg, err) {
    console.error(`[${EXTENSION_NAME}] ${msg}`, err || "");
}

// Read settings from localStorage
function readSettings() {
    try {
        const rawEnabled = localStorage.getItem(STORAGE_KEY_ENABLED);
        enabled = rawEnabled !== null ? rawEnabled === "true" : false;

        const rawDir = localStorage.getItem(STORAGE_KEY_DIR);
        workflowsDir = rawDir || "";

        const rawPos = localStorage.getItem(STORAGE_KEY_POS);
        buttonPosition = rawPos || "bottom-right";

        const rawLastName = localStorage.getItem(STORAGE_KEY_LAST_NAME);
        lastWorkflowName = rawLastName || "";

        log("Settings loaded: enabled=" + enabled + ", dir=" + workflowsDir + ", pos=" + buttonPosition);
    } catch (e) {
        warn("Failed to read settings: " + e.message);
        enabled = false;
        workflowsDir = "";
        buttonPosition = "bottom-right";
    }
}

function writeSetting(key, value) {
    try {
        localStorage.setItem(key, String(value));
    } catch (e) {
        warn("Failed to write setting: " + e.message);
    }
}

// Create a styled button (icon-style for bottom corner)
function createButton(icon, title) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = icon;
    btn.title = title;
    btn.style.cssText = `
        box-sizing: border-box;
        background: #333;
        color: #eee;
        border: 1px solid #555;
        border-radius: 4px;
        padding: 0;
        font-size: 14px;
        cursor: pointer;
        font-family: inherit;
        display: flex;
        align-items: center;
        justify-content: center;
        width: ${BUTTON_SIZE}px;
        height: ${BUTTON_SIZE}px;
        margin: 0;
        z-index: 99999;
        position: fixed;
        pointer-events: auto;
    `;
    btn.addEventListener("mouseenter", () => {
        btn.style.background = "#444";
        btn.style.borderColor = "#777";
    });
    btn.addEventListener("mouseleave", () => {
        btn.style.background = "#333";
        btn.style.borderColor = "#555";
    });
    return btn;
}

// Create a dropdown menu container
function createDropdown() {
    const d = document.createElement("div");
    d.style.cssText = `
        position: fixed;
        min-width: 250px;
        max-width: 400px;
        background: #2a2a2a;
        border: 1px solid #555;
        border-radius: 4px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.5);
        z-index: 100000;
        display: none;
        font-family: inherit;
        overflow-y: auto;
        max-height: 400px;
    `;
    return d;
}

// Position dropdown relative to a button
function positionDropdown(dropdown, button) {
    if (!dropdown || !button) return;
    
    // Temporarily show to get correct dimensions
    const wasHidden = dropdown.style.display === "none";
    if (wasHidden) dropdown.style.display = "block";
    
    const rect = button.getBoundingClientRect();
    const dRect = dropdown.getBoundingClientRect();
    
    if (wasHidden) dropdown.style.display = "none";
    
    const isBottom = buttonPosition === "bottom-right" || buttonPosition === "bottom-left";
    
    if (isBottom) {
        // Position dropdown above the button
        dropdown.style.left = rect.left + "px";
        dropdown.style.bottom = (window.innerHeight - rect.top) + "px";
        dropdown.style.top = "auto";
    } else {
        // Position dropdown below the button
        dropdown.style.left = rect.left + "px";
        dropdown.style.top = (rect.bottom + 4) + "px";
        dropdown.style.bottom = "auto";
    }
    
    // Adjust horizontal alignment based on position
    if (buttonPosition === "bottom-right") {
        dropdown.style.left = (rect.right - dRect.width) + "px";
    }
}

// Create a dropdown item
function createDropdownItem(text, onClick, isFolder = false) {
    const item = document.createElement("div");
    const icon = isFolder ? "📁 " : "📄 ";
    item.textContent = icon + text;
    item.title = text;
    item.style.cssText = `
        padding: 6px 12px;
        color: #eee;
        font-size: 13px;
        cursor: pointer;
        border-bottom: 1px solid #333;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    `;
    item.addEventListener("mouseenter", () => {
        item.style.background = "#3a3a3a";
    });
    item.addEventListener("mouseleave", () => {
        item.style.background = "transparent";
    });
    item.addEventListener("click", (e) => {
        e.stopPropagation();
        onClick();
    });
    return item;
}

// Close all dropdowns
function closeAllDropdowns() {
    try {
        if (saveDropdown) saveDropdown.style.display = "none";
        if (loadDropdown) loadDropdown.style.display = "none";
        isSaveOpen = false;
        isLoadOpen = false;
    } catch (e) {
        error("closeAllDropdowns error", e);
    }
}

// Close dropdowns when clicking outside
document.addEventListener("click", (e) => {
    if (!enabled) return;
    try {
        if (saveDropdown && !saveDropdown.contains(e.target) && e.target !== saveBtn) {
            saveDropdown.style.display = "none";
            isSaveOpen = false;
        }
        if (loadDropdown && !loadDropdown.contains(e.target) && e.target !== loadBtn) {
            loadDropdown.style.display = "none";
            isLoadOpen = false;
        }
    } catch (e) {
        error("click handler error", e);
    }
});

// ---------------------------------------------------------------------------
// Save/Load functionality (using pythongosssss-compatible API)
// ---------------------------------------------------------------------------

function getCurrentWorkflow() {
    try {
        if (app && app.graph) {
            return app.graph.serialize();
        }
    } catch (e) {
        error("Failed to serialize graph", e);
    }
    return null;
}

function saveWorkflowAsFile() {
    try {
        const workflow = getCurrentWorkflow();
        if (!workflow) {
            warn("No workflow data available");
            return;
        }
        const json = JSON.stringify(workflow, null, 2);
        const blob = new Blob([json], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `workflow_${new Date().toISOString().slice(0,16).replace(/:/g,"-")}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        log("Workflow saved to file");
    } catch (e) {
        error("Failed to save workflow", e);
    }
}

async function saveWorkflowToWorkflows() {
    try {
        const workflow = getCurrentWorkflow();
        if (!workflow) {
            warn("No workflow data available");
            return;
        }
        const defaultName = lastWorkflowName || "workflow_" + new Date().toISOString().slice(0,16).replace(/:/g,"-");
        const name = prompt("Enter workflow name:", defaultName);
        if (!name) return;
        
        // Split name into directory path and filename
        const lastSlash = name.lastIndexOf("/");
        const lastBackslash = name.lastIndexOf("\\");
        const lastSep = Math.max(lastSlash, lastBackslash);
        let relPath = "";
        let fileName = name;
        if (lastSep >= 0) {
            relPath = name.substring(0, lastSep);
            fileName = name.substring(lastSep + 1);
        }
        
        log("Saving workflow to dir: " + workflowsDir + ", relPath: " + relPath + ", name: " + fileName);
        const response = await fetch("/noembryo/save_workflow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: fileName, workflow: workflow, path: relPath, dir: workflowsDir }),
        });
        const result = await response.json();
        log("Save response: " + JSON.stringify(result).substring(0, 200));
        if (result.error) {
            if (result.error === "file_exists") {
                const confirmOverwrite = confirm("Workflow '" + result.name + "' already exists. Overwrite?");
                if (confirmOverwrite) {
                    // Retry with overwrite flag
                    const response2 = await fetch("/noembryo/save_workflow", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ name: fileName, workflow: workflow, path: relPath, dir: workflowsDir, overwrite: true }),
                    });
                    const result2 = await response2.json();
                    if (result2.error) {
                        warn("Save failed: " + result2.error);
                    } else {
                        log("Workflow saved: " + result2.path);
                        writeSetting(STORAGE_KEY_LAST_NAME, name);
                    }
                }
            } else {
                warn("Save failed: " + result.error);
            }
        } else {
            log("Workflow saved: " + result.path);
            // Save the name for next time
            writeSetting(STORAGE_KEY_LAST_NAME, name);
        }
    } catch (e) {
        error("Failed to save workflow to default", e);
    }
}

function loadWorkflowFromFile() {
    try {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".json";
        input.onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const text = await file.text();
            try {
                const workflow = JSON.parse(text);
                if (app && app.graph) {
                    app.loadGraphData(workflow);
                    log("Workflow loaded from file");
                }
            } catch (err) {
                error("Failed to parse workflow JSON", err);
            }
        };
        input.click();
    } catch (e) {
        error("Failed to open file picker", e);
    }
}

async function loadSavedWorkflow(fullPath) {
    try {
        log("Loading workflow: " + fullPath);
        const response = await fetch(`/noembryo/load_workflow?full_path=${encodeURIComponent(fullPath)}&dir=${encodeURIComponent(workflowsDir)}`);
        const result = await response.json();
        log("Load response: " + JSON.stringify(result).substring(0, 200));
        if (result.error) {
            warn("Load failed: " + result.error);
            return;
        }
        if (app && app.graph) {
            app.loadGraphData(result.workflow);
            log("Workflow loaded: " + result.name);
        } else {
            warn("app.graph not available");
        }
    } catch (e) {
        error("Failed to load workflow", e);
    }
}

// Get the workflows directory from the server
async function getWorkflowsDir() {
    try {
        const response = await fetch("/noembryo/workflows_dir?dir=" + encodeURIComponent(workflowsDir));
        const result = await response.json();
        if (result.error) {
            warn("Get dir failed: " + result.error);
            return workflowsDir || "";
        }
        return result.dir || workflowsDir || "";
    } catch (e) {
        error("Failed to get workflows dir", e);
        return workflowsDir || "";
    }
}

// List directory contents (folders + workflow files) for recursive browsing
async function listDirectoryContents(relPath) {
    try {
        const url = "/noembryo/list_workflows?path=" + encodeURIComponent(relPath) + "&dir=" + encodeURIComponent(workflowsDir);
        log("Fetching: " + url);
        const response = await fetch(url);
        const result = await response.json();
        log("List result: dirs=" + (result.dirs || []).length + ", files=" + (result.files || []).length + ", error=" + result.error);
        if (result.error) {
            warn("List failed: " + result.error);
            return { dirs: [], files: [], dir: "", rel_path: "" };
        }
        return {
            dirs: result.dirs || [],
            files: result.files || [],
            dir: result.dir || "",
            rel_path: result.rel_path || "",
            base_dir: result.base_dir || "",
        };
    } catch (e) {
        error("Failed to list directory", e);
        return { dirs: [], files: [], dir: "", rel_path: "" };
    }
}

// ---------------------------------------------------------------------------
// Build dropdowns
// ---------------------------------------------------------------------------

function buildSaveDropdown() {
    try {
        if (saveDropdown) return saveDropdown;
        saveDropdown = createDropdown();
        document.body.appendChild(saveDropdown);

        const saveAsItem = createDropdownItem("Save As...", () => {
            saveWorkflowAsFile();
            closeAllDropdowns();
        });
        saveDropdown.appendChild(saveAsItem);

        const saveToItem = createDropdownItem("Save to Workflows", async () => {
            await saveWorkflowToWorkflows();
            closeAllDropdowns();
        });
        saveDropdown.appendChild(saveToItem);

        return saveDropdown;
    } catch (e) {
        error("buildSaveDropdown error", e);
        return null;
    }
}

async function buildLoadDropdown() {
    try {
        if (loadDropdown) {
            loadDropdown.innerHTML = "";
        } else {
            loadDropdown = createDropdown();
            document.body.appendChild(loadDropdown);
        }

        const header = document.createElement("div");
        header.style.cssText = "padding: 8px 12px; background: #333; border-bottom: 1px solid #555; display: flex; align-items: center; gap: 8px;";

        const backBtn = document.createElement("button");
        backBtn.textContent = "↑";
        backBtn.title = "Parent directory";
        backBtn.style.cssText = `
            background: #444;
            color: #eee;
            border: 1px solid #666;
            border-radius: 3px;
            padding: 2px 6px;
            font-size: 12px;
            cursor: pointer;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        `;
        backBtn.disabled = currentRelPath === "";
        backBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            if (currentRelPath === "") return;
            const parts = currentRelPath.split(/[/\\]/);
            parts.pop();
            currentRelPath = parts.join("/");
            buildLoadDropdown();
        });
        header.appendChild(backBtn);

        const pathDisplay = document.createElement("div");
        pathDisplay.textContent = currentRelPath || workflowsDir || "Workflows";
        pathDisplay.title = currentRelPath || workflowsDir || "";
        pathDisplay.style.cssText = "flex: 1; font-size: 11px; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;";
        header.appendChild(pathDisplay);

        loadDropdown.appendChild(header);

        const sep = document.createElement("div");
        sep.style.cssText = "height: 1px; background: #444; margin: 4px 0;";
        loadDropdown.appendChild(sep);

        const loadFileItem = createDropdownItem("Load from File", () => {
            loadWorkflowFromFile();
            closeAllDropdowns();
        });
        loadDropdown.appendChild(loadFileItem);

        const sep2 = document.createElement("div");
        sep2.style.cssText = "height: 1px; background: #444; margin: 4px 0;";
        loadDropdown.appendChild(sep2);

        const contents = await listDirectoryContents(currentRelPath);

        if (contents.dirs.length === 0 && contents.files.length === 0) {
            const empty = document.createElement("div");
            empty.textContent = "No workflows or folders";
            empty.title = contents.dir || "";
            empty.style.cssText = "padding: 6px 12px; color: #888; font-size: 12px; font-style: italic;";
            loadDropdown.appendChild(empty);
        } else {
            for (const dir of contents.dirs) {
                const item = createDropdownItem(dir.name, () => {
                    const newPath = currentRelPath ? currentRelPath + "/" + dir.name : dir.name;
                    currentRelPath = newPath;
                    buildLoadDropdown();
                }, true);
                loadDropdown.appendChild(item);
            }

            for (const wf of contents.files) {
                const item = createDropdownItem(wf.name, () => {
                    loadSavedWorkflow(wf.path);
                    closeAllDropdowns();
                }, false);
                loadDropdown.appendChild(item);
            }
        }

        return loadDropdown;
    } catch (e) {
        error("buildLoadDropdown error", e);
        return null;
    }
}

// ---------------------------------------------------------------------------
// Adjust ComfyUI's native toolbars to make room for our buttons
//
//  * New "Top" menu renders a vertical toolbox on the left edge
//    (<nav class="side-tool-bar-container">) and a horizontal action bar
//    in the bottom right corner (<span class="p-buttongroup right-0 bottom-0">).
//  * We make room for the Save/Load buttons by moving the relevant native
//    toolbar instead of moving our buttons away from the corner.
// ---------------------------------------------------------------------------

// Our buttons are 36x36 and stacked with a 4px gap -> 76px tall, 36px wide
const BUTTON_SIZE = 36;
const BUTTON_GAP = 4;
const BUTTONS_HEIGHT = BUTTON_SIZE * 2 + BUTTON_GAP + 8;  // 80px
const BUTTONS_WIDTH = BUTTON_SIZE + 4;                    // 40px

// The element we shifted last, so we can restore it if ComfyUI replaces it
let shiftedRightToolbar = null;

// Never move the graph canvas, a node widget overlay or anything that
// contains the canvas - shifting those would move the whole workflow (or the
// text boxes of a single node) and would not be restored by ComfyUI.
function isGraphElement(el) {
    if (!el) return true;
    if (el.tagName === "CANVAS") return true;
    if (el.id === "graph-canvas") return true;
    try {
        if (el.closest && el.closest(".dom-widget")) return true;
        const canvas = document.getElementById("graph-canvas");
        if (canvas && el.contains && el.contains(canvas)) return true;
    } catch (e) { /* ignore */ }
    return false;
}

// Find the vertical toolbox of the new menu (left edge)
function findLeftToolbar() {
    const nav = document.querySelector("nav.side-tool-bar-container");
    if (nav) return nav;

    // Fallback: the tallest <nav> hugging the left edge (the new menu renders
    // the toolbox as a <nav>). Restricting it to <nav> keeps us from touching
    // unrelated panels (e.g. the legacy queue menu).
    const vh = window.innerHeight;
    let best = null;
    let bestH = 0;
    document.querySelectorAll("nav").forEach((e) => {
        const r = e.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        if (r.left > 80 || r.width > 140) return;
        if (r.height < vh * 0.4) return;
        if (r.height > bestH) {
            bestH = r.height;
            best = e;
        }
    });
    return best;
}

// Find the horizontal action bar of the new menu (bottom right corner)
function findRightToolbar() {
    // Preferred: the PrimeVue button group anchored to the bottom right
    const group = document.querySelector("span.p-buttongroup.right-0.bottom-0")
        || document.querySelector(".p-buttongroup.right-0.bottom-0");
    if (group && !isGraphElement(group)) return group;

    // Fallback: a small horizontal overlay sitting in the bottom right corner.
    // Only elements that are stacked above the canvas (high z-index) qualify,
    // so the graph canvas and the node widget overlays can never be picked.
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let best = null;
    let bestScore = -Infinity;
    document.querySelectorAll("body *").forEach((e) => {
        if (e === saveBtn || e === loadBtn) return;
        if (isGraphElement(e)) return;
        const cs = getComputedStyle(e);
        if (cs.position !== "absolute" && cs.position !== "fixed") return;
        if ((parseInt(cs.zIndex) || 0) < 100) return;   // must be an overlay
        const r = e.getBoundingClientRect();
        if (r.width < 60 || r.width > 400) return;   // small toolbar only
        if (r.height < 20 || r.height > 120) return;
        if (r.width <= r.height) return;             // must be horizontal
        if (r.right < vw - 120 || r.bottom < vh - 120) return; // near the corner
        // Prefer the element closest to the corner
        const score = (r.right + r.bottom);
        if (score > bestScore) {
            bestScore = score;
            best = e;
        }
    });
    return best;
}

// Shift an element, remembering its original inline translate so we can
// restore it later. We use the standalone `translate` property (instead of
// `transform`) so ComfyUI's own transforms keep working.
function shiftElement(el, axis, pixels) {
    if (!el || isGraphElement(el)) return;
    if (el.dataset.noEmbryoOrigTranslate === undefined) {
        el.dataset.noEmbryoOrigTranslate = el.style.translate || "";
    }
    const value = axis === "x" ? (-pixels) + "px 0px" : "0px " + (-pixels) + "px";
    if (el.style.translate !== value) {
        el.style.translate = value;
    }
}

function unshiftElement(el) {
    if (!el) return;
    if (el.dataset.noEmbryoOrigTranslate === undefined) return;
    const original = el.dataset.noEmbryoOrigTranslate;
    if (el.style.translate !== original) {
        el.style.translate = original;
    }
    delete el.dataset.noEmbryoOrigTranslate;
}

// Make room under the vertical toolbox so our buttons can sit at the bottom
// left corner without covering it.
function adjustLeftToolbar() {
    const nav = findLeftToolbar();
    if (!nav) return;

    // The scrollable list inside the toolbox is shortened so the toolbar can
    // never extend into the area used by our buttons.
    const inner = nav.querySelector("div.side-tool-bar-container") || nav;
    const maxH = "calc(100% - " + BUTTONS_HEIGHT + "px)";
    if (inner.style.maxHeight !== maxH) {
        inner.style.maxHeight = maxH;
    }

    // Nudge the toolbox up a little, but never off the top of the screen.
    const current = parseFloat(nav.dataset.noEmbryoShiftY || "0") || 0;
    const rect = nav.getBoundingClientRect();
    const room = Math.max(0, rect.top + current - 4); // undo our own offset
    const shift = Math.min(BUTTONS_HEIGHT, room);
    if (shift !== current) {
        nav.dataset.noEmbryoShiftY = String(shift);
        if (shift > 0) {
            shiftElement(nav, "y", shift);
        } else {
            unshiftElement(nav);
        }
    }
}

// Move the horizontal action bar of the bottom right corner to the left so
// our buttons can sit in the corner. Only as much as needed is moved, so the
// gap between the toolbar and the buttons stays as small as possible.
function adjustRightToolbar() {
    const bar = findRightToolbar();
    if (!bar) return;

    // Restore a previously shifted element if it is not the current one
    if (shiftedRightToolbar && shiftedRightToolbar !== bar) {
        unshiftElement(shiftedRightToolbar);
    }
    shiftedRightToolbar = bar;

    if (!saveBtn || !saveBtn.parentElement) {
        unshiftElement(bar);
        return;
    }

    const current = parseFloat(bar.dataset.noEmbryoShiftX || "0") || 0;
    const barRect = bar.getBoundingClientRect();
    const buttonRect = saveBtn.getBoundingClientRect();
    // Right edge of the bar without our own offset
    const baseRight = barRect.right + current;
    // The toolbar has its own padding, so the last icon sits further left
    const padRight = parseFloat(getComputedStyle(bar).paddingRight) || 0;

    let needed = 0;
    if (baseRight > buttonRect.left) {
        needed = Math.min(BUTTONS_WIDTH, baseRight - buttonRect.left + 4 - padRight);
        if (needed < 0) needed = 0;
    }

    if (needed !== current) {
        bar.dataset.noEmbryoShiftX = String(needed);
        if (needed > 0) {
            shiftElement(bar, "x", needed);
        } else {
            unshiftElement(bar);
            delete bar.dataset.noEmbryoShiftX;
        }
    }
}

function restoreNativeToolbars() {
    try {
        const nav = findLeftToolbar();
        if (nav) {
            const inner = nav.querySelector("div.side-tool-bar-container") || nav;
            inner.style.maxHeight = "";
            delete nav.dataset.noEmbryoShiftY;
            unshiftElement(nav);
        }
        if (shiftedRightToolbar) {
            unshiftElement(shiftedRightToolbar);
            delete shiftedRightToolbar.dataset.noEmbryoShiftX;
            shiftedRightToolbar = null;
        }
    } catch (e) {
        warn("restoreNativeToolbars error: " + e.message);
    }
}

function adjustNativeToolbars() {
    try {
        if (buttonPosition === "bottom-left") {
            adjustLeftToolbar();
            if (shiftedRightToolbar) {
                unshiftElement(shiftedRightToolbar);
                delete shiftedRightToolbar.dataset.noEmbryoShiftX;
                shiftedRightToolbar = null;
            }
        } else {
            adjustRightToolbar();
            const nav = findLeftToolbar();
            if (nav) {
                const inner = nav.querySelector("div.side-tool-bar-container") || nav;
                inner.style.maxHeight = "";
                delete nav.dataset.noEmbryoShiftY;
                unshiftElement(nav);
            }
        }
    } catch (e) {
        warn("adjustNativeToolbars error: " + e.message);
    }
}

// Re-apply the shift when ComfyUI re-creates its toolbars (e.g. when the
// menu mode is switched or the layout is re-rendered).
let toolbarObserver = null;
let toolbarFrame = null;

function observeToolbars() {
    if (toolbarObserver) return;
    try {
        toolbarObserver = new MutationObserver((mutations) => {
            if (!enabled) return;

            // Only react on structural changes or when somebody overwrites the
            // style of the element we shifted. Reacting on every attribute
            // change would fire on every mouse move.
            let relevant = false;
            for (const m of mutations) {
                if (m.type === "childList") {
                    relevant = true;
                    break;
                }
                if (m.type === "attributes"
                    && m.attributeName === "style"
                    && m.target === shiftedRightToolbar) {
                    relevant = true;
                    break;
                }
            }
            if (!relevant || toolbarFrame) return;

            toolbarFrame = requestAnimationFrame(() => {
                toolbarFrame = null;
                if (enabled) adjustNativeToolbars();
            });
        });
        toolbarObserver.observe(document.body, { childList: true, subtree: true, attributes: true });
    } catch (e) {
        warn("Failed to observe toolbars: " + e.message);
    }
}

// Remove a stale offset that an earlier version of this extension may have
// left on the graph canvas (ComfyUI itself never sets an inline translate
// there).
function cleanupCanvasShift() {
    try {
        const canvas = document.getElementById("graph-canvas");
        if (canvas && canvas.style && canvas.style.translate) {
            canvas.style.translate = "";
        }
    } catch (e) {
        warn("cleanupCanvasShift error: " + e.message);
    }
}

function stopObservingToolbars() {
    if (toolbarObserver) {
        try { toolbarObserver.disconnect(); } catch (e) { /* ignore */ }
        toolbarObserver = null;
    }
    if (toolbarFrame) {
        cancelAnimationFrame(toolbarFrame);
        toolbarFrame = null;
    }
}

// ---------------------------------------------------------------------------
// Diagnostic helper (only active with ?noembryoDebug=1 in the URL).
// Logs the geometry of every fixed/absolute element near the bottom corners
// so the native toolbars can be identified reliably.
// ---------------------------------------------------------------------------

function debugScanToolbars() {
    try {
        if (!/[?&]noembryoDebug=1/.test(window.location.search)) return;
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const clsOf = (el) => {
            try {
                return (typeof el.className === "string" ? el.className : (el.className.baseVal || "")).slice(0, 140);
            } catch (e) { return ""; }
        };

        // 1) fixed/absolute elements near the bottom corners
        const out = [];
        document.querySelectorAll("body *").forEach((el) => {
            const cs = getComputedStyle(el);
            if (cs.position !== "fixed" && cs.position !== "absolute") return;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            if (r.bottom < vh - 260) return;
            if (r.left > 320 && r.right < vw - 320) return;
            out.push({
                tag: el.tagName, cls: clsOf(el), id: el.id || "",
                rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                pos: cs.position, top: cs.top, right: cs.right, bottom: cs.bottom, left: cs.left,
                transform: cs.transform, kids: el.children.length
            });
        });
        console.log("[noEmbryoDebug] bottom candidates: " + JSON.stringify(out));

        // 2) anything tall & narrow in the left column (the toolbox), any position
        const left = [];
        document.querySelectorAll("body *").forEach((el) => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            if (r.left > 70 || r.right < 0) return;         // left column
            if (r.height < 80) return;                       // tall elements only
            const cs = getComputedStyle(el);
            left.push({
                tag: el.tagName, cls: clsOf(el), id: el.id || "",
                rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                pos: cs.position, display: cs.display, flexDirection: cs.flexDirection,
                transform: cs.transform, kids: el.children.length,
                parent: el.parentElement ? el.parentElement.tagName + "." + clsOf(el.parentElement).slice(0, 80) : ""
            });
        });
        console.log("[noEmbryoDebug] left column: " + JSON.stringify(left));
    } catch (e) {
        warn("debugScanToolbars error: " + e.message);
    }
}

// ---------------------------------------------------------------------------
// Inject buttons into bottom corner
// ---------------------------------------------------------------------------

function injectButtons() {
    if (!enabled) return;

    try {
        if (saveBtn && loadBtn && saveBtn.parentElement && loadBtn.parentElement) {
            return;
        }

        if (saveBtn && !saveBtn.parentElement) saveBtn = null;
        if (loadBtn && !loadBtn.parentElement) loadBtn = null;

        if (!saveBtn) {
            saveBtn = createButton("💾", "Save workflow (noEmbryo)");
            saveBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                if (isSaveOpen) {
                    saveDropdown.style.display = "none";
                    isSaveOpen = false;
                } else {
                    closeAllDropdowns();
                    buildSaveDropdown();
                    positionDropdown(saveDropdown, saveBtn);
                    saveDropdown.style.display = "block";
                    isSaveOpen = true;
                }
            });
        }

        if (!loadBtn) {
            loadBtn = createButton("📂", "Load workflow (noEmbryo)");
            loadBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                log("Load button clicked, isLoadOpen=" + isLoadOpen);
                if (isLoadOpen) {
                    loadDropdown.style.display = "none";
                    isLoadOpen = false;
                } else {
                    closeAllDropdowns();
                    currentRelPath = "";
                    await buildLoadDropdown();
                    positionDropdown(loadDropdown, loadBtn);
                    log("Load dropdown built, display=" + loadDropdown.style.display);
                    loadDropdown.style.display = "block";
                    isLoadOpen = true;
                    log("Load dropdown shown");
                }
            });
        }

        const isBottomRight = buttonPosition === "bottom-right";

        if (!saveBtn.parentElement) {
            document.body.appendChild(saveBtn);
        }
        if (!loadBtn.parentElement) {
            document.body.appendChild(loadBtn);
        }

        const bottom = 0;
        if (isBottomRight) {
            saveBtn.style.right = "0px";
            saveBtn.style.left = "auto";
            loadBtn.style.right = "0px";
            loadBtn.style.left = "auto";
        } else {
            saveBtn.style.left = "0px";
            saveBtn.style.right = "auto";
            loadBtn.style.left = "0px";
            loadBtn.style.right = "auto";
        }
        // Save on top, Load at the very bottom of the corner
        saveBtn.style.top = "auto";
        saveBtn.style.bottom = (bottom + BUTTON_SIZE + BUTTON_GAP) + "px";
        loadBtn.style.top = "auto";
        loadBtn.style.bottom = bottom + "px";

        // Adjust ComfyUI's native toolbars to make room for our buttons.
        //  - Left toolbar (vertical): move UP so the buttons can go under it.
        //  - Right toolbar (horizontal): move LEFT so the buttons can go beside it.
        try {
            adjustNativeToolbars();
            observeToolbars();
        } catch (e) {
            warn("Failed to adjust native toolbars: " + e.message);
        }

        log("Save/Load buttons injected at bottom-" + buttonPosition);
    } catch (e) {
        error("injectButtons error", e);
    }
}

function removeButtons() {
    try {
        closeAllDropdowns();
        stopObservingToolbars();
        restoreNativeToolbars();
        if (saveBtn && saveBtn.parentElement) {
            saveBtn.parentElement.removeChild(saveBtn);
        }
        if (loadBtn && loadBtn.parentElement) {
            loadBtn.parentElement.removeChild(loadBtn);
        }
        if (saveDropdown && saveDropdown.parentElement) {
            saveDropdown.parentElement.removeChild(saveDropdown);
        }
        if (loadDropdown && loadDropdown.parentElement) {
            loadDropdown.parentElement.removeChild(loadDropdown);
        }
        saveBtn = null;
        loadBtn = null;
        saveDropdown = null;
        loadDropdown = null;
        isSaveOpen = false;
        isLoadOpen = false;
        if (reinjectTimer) {
            clearTimeout(reinjectTimer);
            reinjectTimer = null;
        }
        log("Save/Load buttons removed");
    } catch (e) {
        error("removeButtons error", e);
    }
}

function scheduleReinject() {
    if (reinjectTimer) clearTimeout(reinjectTimer);
    reinjectTimer = setTimeout(() => {
        if (enabled) injectButtons();
    }, 1000);
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------

app.registerExtension({
    name: EXTENSION_NAME,

    init() {
        log("Extension init called");
        cleanupCanvasShift();
        readSettings();
        log("Save/Load menu enabled: " + enabled + ", dir: " + workflowsDir + ", pos: " + buttonPosition);

        try {
            app.ui.settings.addSetting({
                id: "noEmbryo.SaveLoadMenu.position",
                name: "Button Position",
                defaultValue: "bottom-right",
                type: "combo",
                options: (value) => [
                    { value: "bottom-right", text: "Bottom Right", selected: value === "bottom-right" },
                    { value: "bottom-left", text: "Bottom Left", selected: value === "bottom-left" },
                ],
                onChange: (value) => {
                    if (value === undefined || value === null) return;
                    buttonPosition = value;
                    writeSetting(STORAGE_KEY_POS, buttonPosition);
                    log("Settings: position=" + buttonPosition);
                    if (enabled) {
                        removeButtons();
                        injectButtons();
                    }
                },
            });

            app.ui.settings.addSetting({
                id: "noEmbryo.SaveLoadMenu.workflowsDir",
                name: "Workflows Directory",
                defaultValue: "",
                type: "string",
                onChange: (value) => {
                    if (value === undefined || value === null) return;
                    workflowsDir = value;
                    writeSetting(STORAGE_KEY_DIR, workflowsDir);
                    log("Settings: dir=" + workflowsDir);
                },
            });

            app.ui.settings.addSetting({
                id: "noEmbryo.SaveLoadMenu.enabled",
                name: "Enable Save/Load Menu buttons",
                defaultValue: false,
                type: "boolean",
                onChange: (value) => {
                    if (value === undefined || value === null) return;
                    enabled = value;
                    writeSetting(STORAGE_KEY_ENABLED, enabled);
                    log("Settings: enabled=" + enabled);
                    if (enabled) {
                        injectButtons();
                    } else {
                        removeButtons();
                    }
                },
            });
        } catch (e) {
            warn("Failed to register settings: " + e.message);
        }

        if (enabled) {
            injectButtons();
            setTimeout(() => { if (enabled) injectButtons(); }, 500);
            setTimeout(() => { if (enabled) injectButtons(); }, 2000);
            setTimeout(() => { if (enabled) injectButtons(); }, 5000);
        }

        setTimeout(() => debugScanToolbars(), 3000);
    },

    beforeRegisterNodeDef() {
        if (enabled && (!saveBtn || !loadBtn)) {
            setTimeout(() => injectButtons(), 100);
        }
    },
});

// Expose toggle function globally
window.noEmbryoSaveLoadMenu = {
    toggle: () => {
        enabled = !enabled;
        writeSetting(STORAGE_KEY_ENABLED, enabled);
        if (enabled) {
            injectButtons();
        } else {
            removeButtons();
        }
        log("Save/Load menu " + (enabled ? "enabled" : "disabled"));
        return enabled;
    },
    isEnabled: () => enabled,
};

console.log("[Comfy.noEmbryo.SaveLoadMenu] Extension registered");
