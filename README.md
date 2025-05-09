# noEmbryo Nodes
A diverse set of nodes for ComfyUI.  
- [PromptTermList (1-6)](#prompttermlist-1-6)
- [Resolution Scale](#resolution-scale)
- [Regex Text Chopper](#regex-text-chopper)
- [Auto Save Workflow](#auto-save-workflow)

You can access them through "Add node > noEmbryo" submenu.  

[![made-with-python][Python]](https://www.python.org/)
[![License: MIT][MIT]](LICENSE)

---
## PromptTermList (1-6)
These are some nodes that help with the creation of Prompts inside [ComfyUI](https://github.com/comfyanonymous/ComfyUI).


![Demo workflow](https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen2.png)

<!-- <p align="center">
  <a href="https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen2.png">
<img src="https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen2.png" height="180"></a>
</p> -->

### Usage
Every one of the 6 nodes have a different `json` file that stores its Prompt Terms in "label"/"value" pairs.  
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
## Installation
* Use the ComfyUI Manager
* Or manually
  * cd to `ComfyUI\custom_nodes`
  * git clone https://github.com/noembryo/ComfyUI-noEmbryo.git
  * Restart ComfyUI  


[Python]:https://img.shields.io/badge/Made%20with-Python-1f425f.svg
[MIT]:https://img.shields.io/badge/License-MIT-green.svg
