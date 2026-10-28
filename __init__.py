# SPDX-License-Identifier: GPL-3.0-or-later
bl_info = {
    "name": "Octane Material Preview by Klim",
    "author": "Klim",
    "version": (0, 2, 0),
    "blender": (5, 0, 0),
    "location": "Shader Editor / 3D View > Sidebar > Octane",
    "description": "Selected Octane textures or Albedo in Solid and Eevee Material Preview",
    "category": "Material",
}

import traceback
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, FloatProperty, EnumProperty
from .resolver import (TAG, pointer, helper, contains_octane, active_path,
                       selected_preview, default_preview)
from .eevee_preview import ROLE, SHADER, OUTPUT, image_helper, sync_eevee, remove_eevee

BACKUP = "_osp_original_viewport_color_v1"
LAST_COLOR = "_osp_last_viewport_color_v1"
_status = {}
_cache = {}
_busy = False
_suspended = False
_last_error = ""
_keymaps = []
_display_pending = False
_cleanup_pending = False
_eevee_cleanup_pending = False
PARKED = "_osp_parked_v1"
PARK_LOCATION = (-100000.0, -100000.0)


def prefs():
    item = bpy.context.preferences.addons.get(__package__)
    return item.preferences if item else None


def editable(data):
    return bool(getattr(data, "is_editable", data.library is None))


def data_available():
    # Add-on registration can run with bpy.data replaced by _RestrictData.
    return hasattr(bpy.data, "materials") and hasattr(bpy.data, "node_groups")


def all_trees():
    seen = set()
    trees = [m.node_tree for m in bpy.data.materials if m.node_tree]
    trees += list(bpy.data.node_groups)
    for tree in trees:
        if pointer(tree) not in seen:
            seen.add(pointer(tree))
            yield tree


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "NODE_EDITOR"}:
                area.tag_redraw()


def restore_color(mat):
    if BACKUP in mat:
        # Do not overwrite a viewport color changed manually after our last write.
        last = mat.get(LAST_COLOR)
        if last is None or all(abs(a-b) < 1e-5 for a, b in zip(mat.diffuse_color, last)):
            mat.diffuse_color = tuple(mat[BACKUP])
        del mat[BACKUP]
    if LAST_COLOR in mat:
        del mat[LAST_COLOR]


def cleanup():
    global _cleanup_pending
    if not data_available():
        _cleanup_pending = True
        return
    _cleanup_pending = False
    for tree in all_trees():
        if not editable(tree):
            continue
        for node in list(tree.nodes):
            if helper(node):
                tree.nodes.remove(node)
    for mat in bpy.data.materials:
        if editable(mat):
            restore_color(mat)
    _cache.clear()
    _status.clear()
    redraw()


def place_helper(tree, node, hide=True):
    """Blender has no invisible-node flag; park the helper off the working canvas."""
    changed = False
    offset = {SHADER: 280.0, OUTPUT: 580.0}.get(node.get(ROLE), 0.0)
    park_location = (PARK_LOCATION[0] + offset, PARK_LOCATION[1])
    if hide:
        if node.parent is not None:
            node.parent = None
            changed = True
        if tuple(node.location) != park_location:
            node.location = park_location
            changed = True
        if not node.get(PARKED, False):
            node[PARKED] = True
            node.select = False
            changed = True
    elif node.get(PARKED, False) or PARKED not in node:
        # When explicitly shown, place below the whole graph, including tall outputs.
        other = [n for n in tree.nodes if not helper(n) and n.parent is None]
        node.parent = None
        node.location = (min((n.location.x for n in other), default=0) + offset,
                         min((n.location.y - max(n.dimensions.y, 300)
                              for n in other), default=0) - 180)
        node[PARKED] = False
        changed = True
    if not node.hide:
        node.hide = True
        changed = True
    return changed


def update_helper_display(self=None, context=None):
    global _display_pending
    if not data_available():
        _display_pending = True
        return
    _display_pending = False
    settings = self if self is not None else prefs()
    hide = settings.hide_helpers if settings else True
    for tree in all_trees():
        if editable(tree):
            for node in tree.nodes:
                if helper(node):
                    place_helper(tree, node, hide)
    redraw()


def ensure_helper(tree, preview):
    """Set Blender's active-texture flag, then restore the user's active node.

    Custom Octane nodes do not clear NODE_ACTIVE_TEXTURE when made active.
    nodes.active and node.select are separate; no selection operator is used.
    """
    active = tree.nodes.active
    node = image_helper(tree)
    created = node is None
    if created:
        node = tree.nodes.new("ShaderNodeTexImage")
        node[TAG] = True
        node.name = "OSP • Solid Preview"
        node.label = "Solid Preview — managed automatically"
        node.hide = True
        node.select = False
    settings = prefs()
    display_changed = place_helper(tree, node, settings.hide_helpers if settings else True)
    # Include active node identity: Blender native texture selection may have
    # changed the active-texture flag since the preceding update.
    signature = (pointer(preview.image) if preview.image else 0,
                 pointer(active) if active else 0,
                 tuple((k, getattr(preview.node, k, None)) for k in
                       ("frame_start", "frame_offset", "frame_duration", "use_cyclic", "use_auto_refresh")))
    key = pointer(tree)
    changed = created or node.image != preview.image or _cache.get(key) != signature
    if changed:
        try:
            if node.image != preview.image:
                node.image = preview.image
            if preview.node is not None:
                source_user = getattr(preview.node, "image_user", preview.node)
                for prop in ("frame_start", "frame_offset", "frame_duration", "use_cyclic", "use_auto_refresh"):
                    if hasattr(source_user, prop):
                        value = getattr(source_user, prop)
                        if prop == "frame_duration":
                            value = max(1, value)
                        if getattr(node.image_user, prop) != value:
                            setattr(node.image_user, prop, value)
            tree.nodes.active = node
        finally:
            tree.nodes.active = active
            node.select = False
        _cache[key] = signature
    return changed or display_changed


def sync_material(mat, follow=True):
    tree = mat.node_tree
    if not tree or not contains_octane(tree):
        return False
    if not editable(mat) or not editable(tree):
        _status[pointer(mat)] = "Linked/read-only material: make local to preview"
        return False
    preview = selected_preview(tree) if follow else None
    mode = "Selected" if preview is not None else "Albedo"
    preview = preview if preview is not None else default_preview(tree)
    changed = False
    # Workbench gives active groups precedence over the root texture node.
    # Mirror the same bitmap along that path, keeping all nodes/connections intact.
    for edit_tree, _stack in active_path(tree):
        if not editable(edit_tree):
            _status[pointer(mat)] = "Active group is read-only; make the group local"
            return False
        if edit_tree != tree and edit_tree.users > 1:
            _status[pointer(mat)] = "Shared active group: select a top-level material/output node or make group single-user"
            return False
    for edit_tree, _stack in active_path(tree):
        changed |= ensure_helper(edit_tree, preview)
    if preview.rgba is not None and preview.image is None:
        desired = tuple(max(0.0, min(1.0, c)) for c in preview.rgba)
        if any(abs(a - b) > 1e-6 for a, b in zip(mat.diffuse_color, desired)):
            if BACKUP not in mat:
                mat[BACKUP] = list(mat.diffuse_color)
            mat.diffuse_color = desired
            mat[LAST_COLOR] = list(mat.diffuse_color)
            changed = True
    elif BACKUP in mat:
        restore_color(mat)
        changed = True
    settings = prefs()
    eevee_note = ""
    if settings is None or getattr(settings, "material_preview", True):
        eevee_changed, eevee_note = sync_eevee(
            tree, preview, mat.diffuse_color,
            getattr(settings, "preview_shading", "LIT"),
            getattr(settings, "hide_helpers", True), place_helper)
        changed |= eevee_changed
    else:
        changed |= remove_eevee(tree)
    name = preview.image.name if preview.image else preview.source
    status = f"{mode}: {name}"
    if preview.note:
        status += f" — {preview.note}"
    if eevee_note:
        status += f" — {eevee_note}"
    if _status.get(pointer(mat)) != status:
        _status[pointer(mat)] = status
        changed = True
    return changed


def materials_in_windows():
    seen = set()
    for window in bpy.context.window_manager.windows:
        for obj in window.view_layer.objects:
            for slot in obj.material_slots:
                mat = slot.material
                if mat and pointer(mat) not in seen:
                    seen.add(pointer(mat))
                    yield mat
        # Include pinned materials even when not assigned to a visible object.
        for area in window.screen.areas:
            if area.type == "NODE_EDITOR":
                space = area.spaces.active
                mat = getattr(space, "id", None)
                if isinstance(mat, bpy.types.Material) and pointer(mat) not in seen:
                    seen.add(pointer(mat))
                    yield mat


def refresh(force=False):
    global _busy, _last_error
    if _busy or _suspended or not data_available():
        return
    settings = prefs()
    if settings and not settings.enabled:
        return
    if bpy.app.is_job_running("RENDER"):
        return
    solid_spaces = [a.spaces.active for w in bpy.context.window_manager.windows
                    for a in w.screen.areas if a.type == "VIEW_3D"
                    and a.spaces.active.shading.type == "SOLID"]
    material_spaces = [a.spaces.active for w in bpy.context.window_manager.windows
                       for a in w.screen.areas if a.type == "VIEW_3D"
                       and a.spaces.active.shading.type == "MATERIAL"]
    # Do not repeatedly update Octane's node graph while rendering interactively.
    if not solid_spaces and not material_spaces and not force:
        return
    _busy = True
    changed = False
    try:
        if force:
            _cache.clear()
        if settings is None or settings.auto_texture:
            for space in solid_spaces:
                if space.shading.color_type != "TEXTURE":
                    space.shading.color_type = "TEXTURE"
                    changed = True
        for mat in materials_in_windows():
            try:
                changed |= sync_material(mat, settings.follow_selection if settings else True)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                _status[pointer(mat)] = message
                if message != _last_error:
                    print("[Octane Solid Preview]", mat.name, message)
                    traceback.print_exc()
                    _last_error = message
        if changed:
            redraw()
    finally:
        _busy = False


def tick():
    global _display_pending, _eevee_cleanup_pending
    if not data_available() or _suspended:
        return 0.4
    try:
        if _eevee_cleanup_pending:
            settings = prefs()
            if settings and not getattr(settings, "material_preview", True):
                for tree in all_trees():
                    if editable(tree):
                        remove_eevee(tree)
                redraw()
            _eevee_cleanup_pending = False
        if _cleanup_pending:
            cleanup()
        if _display_pending:
            settings = prefs()
            if settings is None or settings.enabled:
                update_helper_display()
            else:
                _display_pending = False
        refresh()
    except Exception as exc:
        global _last_error
        message = f"{type(exc).__name__}: {exc}"
        if message != _last_error:
            print("[Octane Solid Preview]", message)
            traceback.print_exc()
            _last_error = message
    settings = prefs()
    return settings.interval if settings else 0.4


@persistent
def on_load(_):
    global _busy, _suspended, _display_pending, _cleanup_pending, _eevee_cleanup_pending
    _busy = _suspended = False
    _cache.clear()
    _status.clear()
    _display_pending = True
    _cleanup_pending = False
    _eevee_cleanup_pending = True
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.5, persistent=True)


@persistent
def on_undo_pre(_):
    global _suspended
    _suspended = True


@persistent
def on_undo_post(_):
    global _suspended
    _cache.clear()
    _status.clear()
    _suspended = False


def on_enabled(self, context):
    if not self.enabled:
        cleanup()
    else:
        _cache.clear()


def on_material_preview(self, context):
    global _eevee_cleanup_pending
    # Preferences can be restored inside Blender's restricted registration context.
    _eevee_cleanup_pending = True


class OSPPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    enabled: BoolProperty(name="Automatic preview", default=True, update=on_enabled)
    follow_selection: BoolProperty(name="Follow selected node", default=True,
        description="Selected image/material first; otherwise connected Albedo")
    auto_texture: BoolProperty(name="Set Solid Color to Texture", default=True)
    material_preview: BoolProperty(name="Eevee Material Preview", default=True,
        description="Create an Eevee-only output for the selected texture or Albedo",
        update=on_material_preview)
    preview_shading: EnumProperty(name="Preview shading", default="LIT", items=(
        ("LIT", "Lit (Principled)", "Basic surface shaded by the Material Preview environment"),
        ("FLAT", "Unlit (Texture)", "Show the source image without lighting, using Emission")))
    hide_helpers: BoolProperty(name="Hide helper nodes", default=True,
        description="Move helpers outside the working canvas; Home and Numpad Period ignore them",
        update=update_helper_display)
    interval: FloatProperty(name="Update interval (seconds)", default=0.4, min=0.15, max=5.0)

    def draw(self, context):
        for prop in ("enabled", "follow_selection", "material_preview", "preview_shading",
                     "hide_helpers", "auto_texture", "interval"):
            self.layout.prop(self, prop)
        self.layout.label(text="Source bitmaps and constant colors only. UV mapping required.")


def frame_user_nodes(tree, selected_only, frame_selected):
    """Filter bounds temporarily, without moving user nodes or changing final selection."""
    snapshot = [(n, n.select) for n in tree.nodes]
    targets = [n for n, selected in snapshot if not helper(n) and (selected or not selected_only)]
    if not targets:
        return {"CANCELLED"}
    target_ptrs = {pointer(n) for n in targets}
    try:
        for node, _selected in snapshot:
            node.select = pointer(node) in target_ptrs
        return frame_selected()
    finally:
        for node, selected in snapshot:
            node.select = selected


class OSP_OT_frame_nodes(bpy.types.Operator):
    bl_idname = "osp.frame_nodes"
    bl_label = "Frame Nodes (Ignore Preview Helpers)"
    bl_description = "Frame user nodes without including off-canvas Solid preview helpers"
    selected_only: BoolProperty(default=False, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == "NODE_EDITOR"

    def execute(self, context):
        tree = context.space_data.edit_tree
        settings = prefs()
        if (tree is None or not any(helper(n) for n in tree.nodes)
                or (settings and not settings.hide_helpers)):
            op = bpy.ops.node.view_selected if self.selected_only else bpy.ops.node.view_all
            return op() if op.poll() else {"CANCELLED"}
        if not bpy.ops.node.view_selected.poll():
            return {"CANCELLED"}
        return frame_user_nodes(tree, self.selected_only, bpy.ops.node.view_selected)


class OSP_OT_refresh(bpy.types.Operator):
    bl_idname = "osp.refresh"
    bl_label = "Refresh Preview"
    bl_description = "Rescan Octane materials and update Solid preview"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        refresh(force=True)
        return {"FINISHED"}


class OSP_OT_cleanup(bpy.types.Operator):
    bl_idname = "osp.remove_helpers"
    bl_label = "Disable and Remove Helpers"
    bl_description = "Stop preview, remove only this add-on's nodes and restore viewport colors"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = prefs()
        if settings:
            settings.enabled = False
        cleanup()
        return {"FINISHED"}


def draw_panel(layout, context):
    settings = prefs()
    if settings:
        layout.prop(settings, "enabled")
        layout.prop(settings, "follow_selection")
        layout.prop(settings, "material_preview")
        if settings.material_preview:
            layout.prop(settings, "preview_shading", text="Shading")
        layout.prop(settings, "hide_helpers")
    layout.operator("osp.refresh", icon="FILE_REFRESH")
    if context.area.type == "NODE_EDITOR":
        layout.operator("osp.frame_nodes", text="Frame User Nodes (Home)", icon="VIEWZOOM")
    mat = getattr(context.space_data, "id", None)
    if not isinstance(mat, bpy.types.Material):
        mat = context.object.active_material if context.object else None
    if mat:
        layout.label(text=mat.name, icon="MATERIAL")
        text = _status.get(pointer(mat), "Switch to Solid or Material Preview to start")
        import textwrap
        for line in textwrap.wrap(text, width=42):
            layout.label(text=line)
    layout.separator()
    layout.label(text="Deselect all nodes: use Albedo")
    layout.label(text="Images use the mesh's active UV map")
    layout.operator("osp.remove_helpers", icon="TRASH")


class OSP_PT_shader(bpy.types.Panel):
    bl_label = "Octane Solid Preview"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Octane"

    @classmethod
    def poll(cls, context):
        return (context.space_data.tree_type == "ShaderNodeTree"
                and context.space_data.shader_type == "OBJECT")

    def draw(self, context):
        draw_panel(self.layout, context)


class OSP_PT_view(bpy.types.Panel):
    bl_label = "Octane Solid Preview"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Octane"

    def draw(self, context):
        draw_panel(self.layout, context)


CLASSES = (OSPPreferences, OSP_OT_frame_nodes, OSP_OT_refresh, OSP_OT_cleanup, OSP_PT_shader, OSP_PT_view)
HANDLERS = (("load_post", on_load), ("undo_pre", on_undo_pre),
            ("undo_post", on_undo_post), ("redo_pre", on_undo_pre), ("redo_post", on_undo_post))


def register():
    global _display_pending, _cleanup_pending, _busy, _suspended, _eevee_cleanup_pending
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for name, fn in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if fn not in handlers:
            handlers.append(fn)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
        for key, selected in (("HOME", False), ("NUMPAD_PERIOD", True)):
            item = keymap.keymap_items.new("osp.frame_nodes", key, "PRESS")
            item.properties.selected_only = selected
            _keymaps.append((keymap, item))
    # Registration must only register UI, handlers and timers. Data migration
    # runs after Blender has restored the normal data/context objects.
    _display_pending = True
    _cleanup_pending = False
    _eevee_cleanup_pending = True
    _busy = _suspended = False
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.8, persistent=True)


def unregister():
    global _display_pending, _cleanup_pending, _eevee_cleanup_pending
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()
    for name, fn in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if fn in handlers:
            handlers.remove(fn)
    if data_available():
        cleanup()
    # Restricted shutdown cannot edit datablocks; existing helpers are harmless
    # and can be removed with Disable and Remove Helpers on the next enable.
    _display_pending = _cleanup_pending = False
    _eevee_cleanup_pending = False
    _cache.clear()
    _status.clear()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
