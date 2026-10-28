# SPDX-License-Identifier: GPL-3.0-or-later
"""Small, disposable Eevee-only branch; never edits a user's shader connections."""
from .resolver import TAG, helper

ROLE = "_osp_role_v1"
SHADER = "EEVEE_SHADER"
OUTPUT = "EEVEE_OUTPUT"


def image_helper(tree):
    return next((n for n in tree.nodes if helper(n)
                 and n.bl_idname == "ShaderNodeTexImage"), None)


def remove_eevee(tree):
    owned = [n for n in tree.nodes if helper(n) and n.get(ROLE) in {SHADER, OUTPUT}]
    for node in owned:
        tree.nodes.remove(node)
    return bool(owned)


def new_helper(tree, node_type, role):
    active = tree.nodes.active
    try:
        node = tree.nodes.new(node_type)
        node[TAG] = True
        node[ROLE] = role
        node.name = "OSP • Eevee " + ("Output" if role == OUTPUT else "Shader")
        node.label = "Eevee Preview — managed automatically"
        node.select = False
        return node
    finally:
        tree.nodes.active = active


def sync_link(tree, socket, source):
    current = list(socket.links)
    if source is None:
        for link in current:
            tree.links.remove(link)
        return bool(current)
    if len(current) == 1 and current[0].from_socket == source:
        return False
    # Destination belongs to this add-on. User sockets are never rewired.
    for link in current:
        tree.links.remove(link)
    tree.links.new(source, socket)
    return True


def set_value(socket, value):
    old = socket.default_value
    try:
        same = len(old) == len(value) and all(abs(a-b) < 1e-6 for a, b in zip(old, value))
    except TypeError:
        same = abs(old-value) < 1e-6
    if same:
        return False
    socket.default_value = value
    return True


def sync_eevee(tree, preview, fallback_color, mode, hide, place_helper):
    # A hand-built Eevee branch is a deliberate user choice. Do not replace or
    # deactivate it: doing so can also unset the active Octane/ALL output.
    external = [n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"
                and not helper(n) and getattr(n, "target", "").upper() == "EEVEE"]
    if external:
        return remove_eevee(tree), "Eevee: existing user output preserved"
    image = image_helper(tree)
    if image is None:
        return False, "Eevee: no preview image node"
    changed = False
    output = next((n for n in tree.nodes if helper(n) and n.get(ROLE) == OUTPUT), None)
    if output is None:
        output = new_helper(tree, "ShaderNodeOutputMaterial", OUTPUT)
        changed = True
    if output.target != "EEVEE":
        output.target = "EEVEE"
        changed = True
    # Do NOT assign output.is_active_output = True. Its setter clears the user's
    # active ALL output, breaking materials with multiple Octane outputs.
    # The unique engine-specific output wins in Eevee even with this flag False.
    node_type = "ShaderNodeEmission" if mode == "FLAT" else "ShaderNodeBsdfPrincipled"
    shader = next((n for n in tree.nodes if helper(n) and n.get(ROLE) == SHADER), None)
    if shader is not None and shader.bl_idname != node_type:
        tree.nodes.remove(shader)
        shader = None
        changed = True
    if shader is None:
        shader = new_helper(tree, node_type, SHADER)
        changed = True
    for node in (shader, output):
        changed |= place_helper(tree, node, hide)
    color_socket = shader.inputs["Color" if mode == "FLAT" else "Base Color"]
    changed |= sync_link(tree, color_socket, image.outputs["Color"] if preview.image else None)
    if preview.image is None:
        changed |= set_value(color_socket, tuple(preview.rgba or fallback_color))
    if mode == "FLAT":
        changed |= set_value(shader.inputs["Strength"], 1.0)
    else:
        changed |= set_value(shader.inputs["Metallic"], 0.0)
        changed |= set_value(shader.inputs["Roughness"], 0.6)
        # This is a color/texture preview, not an opacity/material conversion.
        changed |= set_value(shader.inputs["Alpha"], 1.0)
    changed |= sync_link(tree, output.inputs["Surface"], shader.outputs[0])
    return changed, ""
