import { app } from "../../scripts/app.js";

/* ================== Embellishment Node Frontend ==================
   Standalone frontend for EmbellishmentNode (nodes.py).
   Provides: color text + pick button, height_px control,
   and divider/spacer rendering via a custom UI_SUB_DIVIDER widget.
================================================================== */

(function() {
    try {
        /* ---------------- module-level utils ---------------- */
        function propKey(name) { return `__ui_${name}`; }

        function getProp(node, k, fallback) {
            node.properties = node.properties || {};
            return Object.prototype.hasOwnProperty.call(node.properties, k)
                ? node.properties[k]
                : fallback;
        }

        function setProp(node, k, v) {
            node.properties = node.properties || {};
            node.properties[k] = v;
        }

        function parseHex(v, fallback = "#ffffff") {
            if (!v) return fallback;
            let s = String(v).trim();
            if (!s.startsWith("#")) s = "#" + s;
            if (s.length === 4) {
                s = "#" + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
            }
            if (s.length !== 7) return fallback;
            return s.toLowerCase();
        }

        function fitNodeHeight(n) {
            if (!n || typeof n.computeSize !== "function" || typeof n.setSize !== "function") return;
            const sz = n.computeSize();
            sz[0] = n.size[0];
            n.setSize(sz);
            n.setDirtyCanvas?.(true, true);
        }

        /* ---------------- color math helpers ---------------- */
        function hexToRgb(hex) {
            hex = (hex || "#ffffff").replace(/^#/, "");
            if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
            const m = /^([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
            if (!m) return { r: 255, g: 255, b: 255 };
            return { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) };
        }
        function rgbToHex(r, g, b) {
            const toHex = (x) => {
                const c = Math.max(0, Math.min(255, Math.round(x)));
                const s = c.toString(16);
                return s.length === 1 ? "0" + s : s;
            };
            return "#" + toHex(r) + toHex(g) + toHex(b);
        }
        function rgbToHsv(r, g, b) {
            r /= 255; g /= 255; b /= 255;
            const max = Math.max(r, g, b), min = Math.min(r, g, b);
            const d = max - min;
            let h = 0, s = max === 0 ? 0 : d / max, v = max;
            if (d !== 0) {
                if (max === r) h = ((g - b) / d + (g < b ? 6 : 0));
                else if (max === g) h = ((b - r) / d + 2);
                else h = ((r - g) / d + 4);
                h /= 6;
            }
            return { h: h * 360, s, v };
        }
        function hsvToRgb(h, s, v) {
            h = (h % 360 + 360) % 360;
            s = Math.max(0, Math.min(1, s));
            v = Math.max(0, Math.min(1, v));
            const c = v * s;
            const x = c * (1 - Math.abs((h / 60) % 2 - 1));
            const m = v - c;
            let r1 = 0, g1 = 0, b1 = 0;
            if (h < 60) { r1 = c; g1 = x; b1 = 0; }
            else if (h < 120) { r1 = x; g1 = c; b1 = 0; }
            else if (h < 180) { r1 = 0; g1 = c; b1 = x; }
            else if (h < 240) { r1 = 0; g1 = x; b1 = c; }
            else if (h < 300) { r1 = x; g1 = 0; b1 = c; }
            else { r1 = c; g1 = 0; b1 = x; }
            return { r: (r1 + m) * 255, g: (g1 + m) * 255, b: (b1 + m) * 255 };
        }

        /* ---------------- color controls (hex text + pick button) ---------------- */
        function addColorControls(node) {
            const kColor = propKey("divider_color");
            const initialHex = parseHex(getProp(node, kColor, "#ffffff"), "#ffffff");

            const hexWidget = node.addWidget("text", "color", initialHex, (v) => {
                const parsed = parseHex(v, null);
                if (!parsed) return;
                setProp(node, kColor, parsed);
                node.setDirtyCanvas(true, true);
                if (app.canvas) app.canvas.setDirty(true, true);
            }, { serialize: false, multiline: false, placeholder: "#ffffff" });
            hexWidget.tooltip = "Divider color in hex (e.g. #ffffff).\nOnly used when height_px is 0.";

            function openColorPopup() {
                const currentHex = parseHex(getProp(node, kColor, "#ffffff"), "#ffffff");
                const { r, g, b } = hexToRgb(currentHex);
                let { h, s, v } = rgbToHsv(r, g, b);

                const overlay = document.createElement("div");
                Object.assign(overlay.style, {
                    position: "fixed", left: "0", top: "0", width: "100vw", height: "100vh",
                    zIndex: "999999", background: "rgba(0,0,0,0.25)", display: "flex",
                    alignItems: "center", justifyContent: "center"
                });
                const panel = document.createElement("div");
                Object.assign(panel.style, {
                    background: "#222", border: "1px solid #555", borderRadius: "6px",
                    padding: "8px", minWidth: "220px", color: "#ddd",
                    display: "flex", flexDirection: "column", gap: "6px"
                });
                const canvas = document.createElement("canvas");
                canvas.width = 180; canvas.height = 120;
                Object.assign(canvas.style, {
                    width: "180px", height: "120px", background: "#000", cursor: "crosshair"
                });
                const ctx = canvas.getContext("2d");
                function redraw() {
                    ctx.clearRect(0, 0, 180, 120);
                    const { r: rh, g: gh, b: bh } = hsvToRgb(h, 1, 1);
                    const gradX = ctx.createLinearGradient(0, 0, 156, 0);
                    gradX.addColorStop(0, "#ffffff");
                    gradX.addColorStop(1, `rgb(${rh},${gh},${bh})`);
                    ctx.fillStyle = gradX;
                    ctx.fillRect(0, 0, 156, 120);
                    const gradY = ctx.createLinearGradient(0, 0, 0, 120);
                    gradY.addColorStop(0, "rgba(0,0,0,0)");
                    gradY.addColorStop(1, "rgba(0,0,0,1)");
                    ctx.fillStyle = gradY;
                    ctx.fillRect(0, 0, 156, 120);
                    const cx = s * 156;
                    const cy = (1 - v) * 120;
                    ctx.strokeStyle = "white";
                    ctx.lineWidth = 1;
                    ctx.beginPath(); ctx.arc(cx, cy, 4, 0, Math.PI * 2); ctx.stroke();
                    ctx.strokeStyle = "black";
                    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI * 2); ctx.stroke();
                    for (let i = 0; i < 120; i++) {
                        const col = hsvToRgb(i / 120 * 360, 1, 1);
                        ctx.fillStyle = `rgb(${col.r},${col.g},${col.b})`;
                        ctx.fillRect(162, i, 18, 1);
                    }
                    const hy = (h / 360) * 120;
                    ctx.strokeStyle = "white";
                    ctx.strokeRect(161, hy - 2, 20, 4);
                }
                let draggingSV = false, draggingHue = false;
                function handleCanvasDown(ev) {
                    const rect = canvas.getBoundingClientRect();
                    const x = ev.clientX - rect.left;
                    const y = ev.clientY - rect.top;
                    if (x > 160) { draggingHue = true; h = Math.max(0, Math.min(1, y / 120)) * 360; }
                    else { draggingSV = true; s = Math.max(0, Math.min(1, x / 156)); v = 1 - Math.max(0, Math.min(1, y / 120)); }
                    update(); redraw();
                }
                function handleCanvasMove(ev) {
                    if (!draggingSV && !draggingHue) return;
                    const rect = canvas.getBoundingClientRect();
                    const x = ev.clientX - rect.left;
                    const y = ev.clientY - rect.top;
                    if (draggingHue) h = Math.max(0, Math.min(1, y / 120)) * 360;
                    else if (draggingSV) { s = Math.max(0, Math.min(1, x / 156)); v = 1 - Math.max(0, Math.min(1, y / 120)); }
                    update(); redraw();
                }
                function handleCanvasUp() { draggingSV = false; draggingHue = false; }
                function update() {
                    const rgb = hsvToRgb(h, s, v);
                    const hex = rgbToHex(rgb.r, rgb.g, rgb.b);
                    hexWidget.value = hex;
                    if (hexWidget.callback) hexWidget.callback(hex);
                }
                canvas.onmousedown = handleCanvasDown;
                window.addEventListener("mousemove", handleCanvasMove);
                window.addEventListener("mouseup", handleCanvasUp);
                const closeBtn = document.createElement("button");
                closeBtn.textContent = "Close";
                closeBtn.onclick = () => {
                    document.body.removeChild(overlay);
                    window.removeEventListener("mousemove", handleCanvasMove);
                    window.removeEventListener("mouseup", handleCanvasUp);
                };
                panel.appendChild(canvas);
                panel.appendChild(closeBtn);
                overlay.appendChild(panel);
                document.body.appendChild(overlay);
                redraw();
            }
            node.addWidget("button", "pick", "pick", () => openColorPopup(), { serialize: false });
            setProp(node, kColor, initialHex);
        }

        /* ---------------- height control ---------------- */
        function addHeightControl(node) {
            const kHeight = propKey("spacer_height");
            const initSize = Number(getProp(node, kHeight, 0));
            const heightWidget = node.addWidget(
                "number",
                "height_px",
                initSize,
                (v) => {
                    const nv = Math.max(0, Math.min(1000, Math.round(Number(v) || 0)));
                    setProp(node, kHeight, nv);
                    fitNodeHeight(node);
                    if (app.canvas) app.canvas.setDirty(true, true);
                },
                { serialize: false, precision: 0, min: 0, max: 1000, step: 1 }
            );
            heightWidget.tooltip = "Height of the Spacer in pixels.\nWhen 0 the node acts as a Divider.\n" +
                "When >0 the node acts as a Spacer";
        }

        /* ---------------- add all controls to EmbellishmentNode ---------------- */
        function addEmbellishmentControls(node) {
            // Prevent duplicate controls when both nodeCreated and loadedGraphNode fire
            if (node.widgets.some(w => w.name === "height_px")) return;
            addColorControls(node);
            addHeightControl(node);
        }

        /* ---------------- custom widget: divider line or spacer ---------------- */
        function makeEmbellishmentDecoratorFactory() {
            return (node, inputName, _inputData, _app) => {
                const w = node.addCustomWidget({
                    type: "ui_embellishment_decorator",
                    name: inputName,
                    serialize: false,
                    draw: function (ctx, n, widgetWidth, y) {
                        const isEmb = node?.comfyClass === "Embellishment -noEmbryo";
                        const height = isEmb
                            ? Number(getProp(node, propKey("spacer_height"), 0))
                            : 0;
                        if (height > 0) {
                            ctx.save();
                            ctx.strokeStyle = "#444";
                            ctx.setLineDash([2, 4]);
                            ctx.beginPath();
                            ctx.moveTo(4, y);
                            ctx.lineTo(8, y);
                            ctx.moveTo(4, y);
                            ctx.lineTo(4, y + height);
                            ctx.lineTo(8, y + height);
                            ctx.stroke();
                            ctx.restore();
                        } else {
                            const color = isEmb
                                ? parseHex(getProp(node, propKey("divider_color"), "#ffffff"))
                                : "#ffffff";
                            ctx.save();
                            ctx.strokeStyle = color;
                            ctx.lineWidth = 1;
                            ctx.beginPath();
                            ctx.moveTo(8, y + 3);
                            ctx.lineTo(widgetWidth - 8, y + 3);
                            ctx.stroke();
                            ctx.restore();
                        }
                    },
                    computeSize: function (widgetWidth) {
                        const isEmb = node?.comfyClass === "Embellishment -noEmbryo";
                        const height = isEmb
                            ? Number(getProp(node, propKey("spacer_height"), 0))
                            : 0;
                        return height > 0 ? [200, height] : [200, 5];
                    },
                });
                w.serialize = true;
                w.serializeValue = () => null;
                w.__uiDecorKind = "embellishment";
                w.__uiDecorNode = node;
                return { widget: w };
            };
        }

        /* ---------------- register extensions ---------------- */
        app.registerExtension({
            name: "Comfy.ui.embellishment.widgets",
            async getCustomWidgets() {
                return {
                    "UI_SUB_DIVIDER": makeEmbellishmentDecoratorFactory(),
                };
            },
        });

        app.registerExtension({
            name: "Comfy.ui.embellishment",
            async nodeCreated(node) {
                if (node.comfyClass === "Embellishment -noEmbryo") {
                    addEmbellishmentControls(node);
                }
            },
            loadedGraphNode(node) {
                if (node.comfyClass === "Embellishment -noEmbryo") {
                    addEmbellishmentControls(node);
                }
            },
        });

    } catch (e) {
        console.error("[Embellishment] JS initialization error:", e);
    }
})();
