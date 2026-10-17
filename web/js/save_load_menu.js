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

// Default: OFF
let enabled = false;
let workflowsDir = "";
let buttonPosition = "bottom-right"; // "bottom-left" or "bottom-right"

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
        background: #333;
        color: #eee;
        border: 1px solid #555;
        border-radius: 4px;
        padding: 6px 8px;
        font-size: 14px;
        cursor: pointer;
        font-family: inherit;
        display: flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
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
        const name = prompt("Enter workflow name:", "workflow_" + new Date().toISOString().slice(0,16).replace(/:/g,"-"));
        if (!name) return;
        log("Saving workflow to dir: " + workflowsDir);
        const response = await fetch("/noembryo/save_workflow", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name, workflow: workflow, dir: workflowsDir }),
        });
        const result = await response.json();
        log("Save response: " + JSON.stringify(result).substring(0, 200));
        if (result.error) {
            warn("Save failed: " + result.error);
        } else {
            log("Workflow saved: " + result.path);
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

        const bottom = 10;
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
        saveBtn.style.top = "auto";
        saveBtn.style.bottom = bottom + "px";
        loadBtn.style.top = "auto";
        loadBtn.style.bottom = (bottom + 40) + "px";

        log("Save/Load buttons injected at bottom-" + buttonPosition);
    } catch (e) {
        error("injectButtons error", e);
    }
}

function removeButtons() {
    try {
        closeAllDropdowns();
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
