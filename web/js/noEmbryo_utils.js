import { ComfyApp, app } from "../../scripts/app.js";

app.registerExtension({
	name: "Comfy.noEmbryo",

	nodeCreated(node, app) {
		console.log("nodeCreated", node.comfyClass);

// 		if(node.comfyClass === "LoadImage From Dir /noEmbryo") {
//
// // 			async function listAllFiles(dirHandle) {
// // 				const files = [];
// // 				for await (let [name, handle] of dirHandle) {
// // 					const {kind} = handle;
// // 					{
// // 						files.push({name, handle, kind});
// // 					}
// // 				}
// // 				return files;
// // }
// //
// // 			async function onClickHandler(e) {
// // 				try {
// // 					const directoryHandle = await window.showDirectoryPicker()
// // 					console.log(directoryHandle)
// // 					const data = await listAllFiles(directoryHandle);
// // 					// console.log('files', files);
// // 					return data;
// // 				}catch(e) {
// // 					console.log(e);
// // 				}
// // 			}
//
// 		    let path_i = 0;
// 		    let images_i = 1;
// 			node._value = "None";
//
// 			Object.defineProperty(node.widgets[path_i], "value", {
// 				set: (value) => {
// 				        const stackTrace = new Error().stack;
//                         if(stackTrace.includes('inner_value_change')) {
//                             if(value !== "") {
// 								console.log(node.widgets[images_i])
// 								let values = node.widgets[images_i].options.values;
// 								console.log(values)
//                                 node.widgets[images_i].value = values[1];
// 	                            // if(node.widgets_values) {
// 	                            //     node.widgets_values[images_i] = node.widgets[path_i].value;
//                                 // }
//                             }
//                         }
//
// 						node._value = value;
// 					},
// 				get: () => {
//                         return node._value;
// 					 }
// 			});
// 		}

		if(node.comfyClass === "JsonPromptLoader -noEmbryo") {
			console.log('Setting up JsonPromptLoader');
			let json_path_i = 0;
			let selected_title_i = 1;
			const originalCallback = node.widgets[json_path_i].callback;
			node.widgets[json_path_i].callback = async (value) => {
				// console.log('Callback called with path:', value);
				if(originalCallback) originalCallback(value);
				if(value) {
					try {
						const url = '/noembryo/get_json?path=' + encodeURIComponent(value);
						// console.log('Fetching:', url);
						const response = await fetch(url);
						// console.log('Response status:', response.status);
						const data = await response.json();
						// console.log('Data:', data);
						if(data.error) {
							console.log('Error:', data.error);
							node.widgets[selected_title_i].options.values = ["None"];
							node.widgets[selected_title_i].value = "None";
							app.graph.setDirtyCanvas(true, true);
						} else {
							const titles = ["None", ...Object.keys(data)];
							// console.log('Titles:', titles);
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
						console.log('Error loading JSON:', e);
						node.widgets[selected_title_i].options.values = ["None"];
						node.widgets[selected_title_i].value = "None";
					}
				} else {
					node.widgets[selected_title_i].options.values = ["None"];
					node.widgets[selected_title_i].value = "None";
				}
			};
			// Trigger callback if path is already set (e.g., from loaded workflow)
			setTimeout(() => {
				if(node.widgets[json_path_i].value) {
					node.widgets[json_path_i].callback(node.widgets[json_path_i].value);
				}
			}, 100);
		}
	}
});
