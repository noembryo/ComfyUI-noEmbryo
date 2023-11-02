## General
<!-- ![kohighlights128w](https://user-images.githubusercontent.com/24675403/234561476-97283ff8-5437-49cd-b4c5-3929886cf182.png) -->

[![made-with-python][Python]](https://www.python.org/)
[![License: MIT][MIT]](LICENSE)
<!-- [![Generic badge][OS]][ReleaseLink] -->
<!-- [![GitHub release][Release]][ReleaseLink] -->
<!-- [![Github all releases][TotalDown]][ReleaseLink] -->
<!-- [![Github Releases (by Release)][VersionDown]][ReleaseLink] -->


**PromptTermList** (1-6) are some nodes that help with the creation of Prompts inside [ComfyUI](https://github.com/comfyanonymous/ComfyUI).


### Screenshots

<!--suppress HtmlDeprecatedAttribute -->
<p align="center">
  <a href="https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen1.png">
    <!--suppress HtmlRequiredAltAttribute -->
<img src="https://raw.githubusercontent.com/noembryo/ComfyUI-noEmbryo/master/stuff/Screen1.png" height="180"></a>
</p>

## Usage
Every one of the 6 nodes have a different `json` file that stores its Prompt Terms in "label"/"value" pairs.  
The "label" part is what we see at the node's dropdown menu, and the "value" part is what it produces at its `Term` output when we run a generation job.  
  
These `json` files are located inside the `TermLists` directory, in the node's folder.  
There are two ways to add a new term.  
* From within ComfyUI, connect a text box to the node's `text` input.  
  Write the "label"/"value" part in the box using the following format:  
  label=bla blabla  
  value=blabla bla blabla blab  
  Enable the `store_input` switch.  
  Run a generation job.  
  Refresh the page.  
* Manually, open the `json` file and add/remove/change entries.  
  Of course we must be very careful with this, to keep the `json` format of labels/values (with the appropriate commas), otherwise the file will not be parsed.  

This `text` input is also useful if we want to manually add something *after* our term, or as the *only* term if we select the `None` label of the dropdown.


## Installation
* cd to `ComfyUI\custom_nodes`
* git clone https://github.com/noembryo/ComfyUI-noEmbryo.git
* Restart ComfyUI


[Python]:https://img.shields.io/badge/Made%20with-Python-1f425f.svg
[MIT]:https://img.shields.io/badge/License-MIT-green.svg
