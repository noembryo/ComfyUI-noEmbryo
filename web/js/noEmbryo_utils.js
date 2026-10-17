import { ComfyApp, app } from "../../scripts/app.js";

app.registerExtension({
	name: "Comfy.noEmbryo",

	nodeCreated(node, app) {
		// console.log("nodeCreated", node.comfyClass);

		if (node.comfyClass === "JsonPromptLoader -noEmbryo") {
			console.log('Setting up JsonPromptLoader');
			let json_path_i = 0;
			let selected_title_i = 1;
			const originalCallback = node.widgets[json_path_i].callback;
			node.widgets[json_path_i].callback = async (value) => {
				if (originalCallback) originalCallback(value);
				if (value) {
					try {
						const url = '/noembryo/get_json?path=' + encodeURIComponent(value);
						const response = await fetch(url);
						const data = await response.json();
						if (data.error) {
							node.widgets[selected_title_i].options.values = ["None"];
							node.widgets[selected_title_i].value = "None";
							app.graph.setDirtyCanvas(true, true);
						} else {
							const titles = ["None", ...Object.keys(data)];
							node.widgets[selected_title_i].options.values = titles;
							const currentValue = node.widgets[selected_title_i].value;
							if (titles.includes(currentValue)) {
								node.widgets[selected_title_i].value = currentValue;
							} else {
								node.widgets[selected_title_i].value = "None";
							}
							app.graph.setDirtyCanvas(true, true);
						}
					} catch (e) {
						node.widgets[selected_title_i].options.values = ["None"];
						node.widgets[selected_title_i].value = "None";
					}
				} else {
					node.widgets[selected_title_i].options.values = ["None"];
					node.widgets[selected_title_i].value = "None";
				}
			};
			setTimeout(() => {
				if (node.widgets[json_path_i].value) {
					node.widgets[json_path_i].callback(node.widgets[json_path_i].value);
				}
			}, 100);
		}
	}
});
