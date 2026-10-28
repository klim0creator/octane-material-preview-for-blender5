# octane-material-preview-for-blender5

tool which allows you to preview materials using only octane nodes in (solid or material preview mode) for octane renderer for blender 5.0+

## Installation

1. Download `octane_material_preview_0_2_0.zip`. Do not extract it.
2. In Blender, open **Edit → Preferences → Add-ons → dropdown menu → Install from Disk**.
3. Select the ZIP and enable **Octane Solid Preview** if it is not enabled automatically.
4. Switch the 3D Viewport to **Solid** or **Material Preview**. The scene's Render Engine can remain set to **Octane**. For Solid shading, the add-on automatically selects **Color → Texture**.
5. Your mesh needs a UV map. In **Object Properties → Viewport Display**, keep **Display As** set to **Textured**.

## Usage

- Select an **Octane RGB Image / Greyscale Image** node in the Shader Editor to preview its image on the mesh.
- Select a **Universal / Diffuse / Standard Surface** material node to preview its Albedo / Diffuse / Base color source.
- Deselect all nodes (**Alt+A** with Blender's default keymap) to preview the Albedo of the material connected to Material Output.
- When several nodes are selected, the active node—the last selected node—takes priority.
- For supported intermediate nodes, such as Color Correction, the add-on searches upstream for the source image. It does not evaluate the correction itself.
- Changing the image in an Octane node updates the preview automatically, normally within 0.4 seconds.
- Selection is tracked separately for each material node tree. All faces sharing a material show the same preview; material assignments are unchanged.
- Closing the Shader Editor preserves its node selection, as usual in Blender. To return to Albedo, deselect the nodes or turn off **Follow selected node**.
