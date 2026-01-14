# noEmbryo Nodes
A diverse set of nodes for ComfyUI.  
- [Json Prompt Loader](#json-prompt-loader)
- [Resolution Scale](#resolution-scale)
- [Regex Text Chopper](#regex-text-chopper)
- [Auto Save Workflow](#auto-save-workflow)
- [PromptTermList (1-6)](#prompttermlist-1-6)

You can access them through "Add node > noEmbryo" submenu.  

[![made-with-python][Python]](https://www.python.org/)
[![License: MIT][MIT]](LICENSE)

---
## Json Prompt Loader
![Example](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/JsonLoader.png)  

A node that can load a `.json` file with `item:prompt` pairs and outputs the selected item's prompt, while combining it with a custom prompt.  
It can load `.json` files from any directory, not just the node's directory.  
For the custom text integration, there is a variable (can be specified by the user), that can be used in the item's prompt text to insert the custom text anywhere in the body of the prompt.  

- **UI controls**
  - **json_path** is the path to the `.json` file.  
      It can be an absolute path of your hard drive, or a relative path to the ComfyUI's installation directory.
  - **selected_item** selects one of the `.json` file's items (prompts).
  - **variable** is the variable that will be used to insert the custom text into the item's prompt text.
  - **custom_text** is the custom text that will be inserted into the item's prompt text.
- **Advanced Usage**  
  ![Example](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/JsonLoader2.png) 
  - **Adding new items**  
    To add a new item, we have to write it in the `custom_text` input, using the following format:  
    ```
    item=The prompt's title/keyword
    value=The prompt's text, including our variable
    ```
  - **Updating items**  
    To update an item, we use the same method, just write the specific item we want to update.
    ```
    item=An existing title/keyword
    value=The new prompt's text, including our variable
    ```
  - **Deleting items**  
    To delete an item, we use the same format, but we just leave the `value` empty.
    ```
    item=An existing title/keyword
    value=
    ```
  All of these methods will update the `.json` file, and the node will output a message about the success or failure of the operation.  
  We can also load an empty `.json`file and use this method to populate it.  

<u>_**After changing the `.json` file, we must reload the ComfyUI page, so the node can reload the updated file.**_</u>

---
## Resolution Scale
![Demo workflow](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/res_scale1.png)
A simple node that outputs the resolution of an image using the dimensions of an input image or some custom user-defined dimensions, using a Scale Factor.  

If there is an input image connected, setting either `width` or `height` to 0 will use the other dimension to scale the image (but always multiple of 4).

---
## Regex Text Chopper
![Demo workflow](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/regex_text.png)
A node that "chops" a text using a regular expression and outputs the chopped parts of the text. 

---
## Auto Save Workflow
![Demo workflow](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/auto_save_workflow.png)  
This node can save the current workflow to a `.json` file, every time a generation job is run.
- **UI controls**
  - **save_directory** is the directory where the `.json` file will be saved.  
      It can be a relative path inside the ComfyUI's output directory, or an absolute path of your hard drive.
  - **filename** is the name of the `.json` file.  
      It can contain the `{timestamp}` placeholder, which will be replaced with the current timestamp (for unique filenames).
  - **trigger** switch will enable (or not) the saving of the workflow.
- **Outputs**
  - **status** outputs a text with the status of the saving process (the path of the saved file, or an error message).
  - **✳️trigger** is a dummy output that's used to trigger the saving process, even if nothing is connected to the node.

---
## PromptTermList 1-6

<u>**The PromptTermList nodes are now obsolete, and can mostly be replaced by the [Json Prompt Loader](#json-prompt-loader) node.  
I won't remove them for compatibility reasons, but I would recommend using the [JsonPromptLoader](#json-prompt-loader) node instead.**</u>

![Demo workflow](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen2.png)
These are some nodes that help with the creation of Prompts inside [ComfyUI](https://github.com/comfyanonymous/ComfyUI).

### Usage

Every one of these 6 nodes have a different `json` file that stores its Prompt Terms in "label"/"value" pairs.  
The "label" part is what we see at the node's dropdown menu, and the "value" part is what it produces at its `Term` output when we run a generation job.  
  
These `json` files are located inside the `TermLists` directory, in the node's folder.  
There are two ways to add a new term.  
- From within ComfyUI:
  - Connect a text box to the node's `text` input.  
  - Write the "label"/"value" part in the box using the following format:  
  ```
  label=Descriptive text
  value=masterpiece, artful and cozy
  ```
  - Enable the `store_input` switch.  
  - Run a generation job.  
  - Refresh the page.  
- Manually:
  - Just open the `json` file and add/remove/change entries. 

  Ofcourse we must be very careful with this, to keep the `json` format of labels/values (with the appropriate commas), otherwise the file will not be parsed.  

This `text` input is also useful if we want to manually add something *after* our term, or as the *only* term if we select the `None` label of the dropdown.  
The `strength` value is changing the impact of the term by using the parenthesis format like this: `(a great term:1.3)`  
  
We can delete a term by sending an empty value to the `text` input like this:
```
label=The label to be deleted
value=
```

---
## Installation
* Use the ComfyUI Manager
* Or manually
  * cd to `ComfyUI\custom_nodes`
  * git clone https://github.com/noembryo/ComfyUI-noEmbryo.git
  * Restart ComfyUI  

---
[Python]:https://img.shields.io/badge/Made%20with-Python-1f425f.svg
[MIT]:https://img.shields.io/badge/License-MIT-green.svg
