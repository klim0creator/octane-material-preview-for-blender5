"""Read-only graph resolution. No Octane imports and no Blender operators."""
from dataclasses import dataclass

TAG = "_osp_helper_v1"
COLOR_INPUTS = ("albedo", "basecolor", "diffuse", "diffusecolor")
SKIP_INPUTS = {"normal", "bump", "displacement", "roughness", "opacity", "amount",
               "factor", "power", "gamma", "legacygamma", "projection", "uvtransform",
               "transform", "colorspace", "metallic", "metalness", "ior"}


def norm(s):
    return "".join(c for c in str(s).lower() if c.isalnum())


def pointer(item):
    return item.as_pointer() if hasattr(item, "as_pointer") else id(item)


def helper(node):
    return bool(node.get(TAG, False))


def octane(node):
    return "octane" in node.bl_idname.lower()


def group(node):
    return getattr(node, "type", "") == "GROUP" and getattr(node, "node_tree", None)


def image_of(node):
    # OctaneBaseImageNode.image is a Blender Image PointerProperty.
    # Never load a guessed filename or duplicate an image datablock.
    image = getattr(node, "image", None)
    return image if image is not None and hasattr(image, "filepath") else None


def is_image_node(node):
    name = node.bl_idname.lower()
    return (name == "shadernodeteximage" or
            ("octane" in name and ("image" in name or hasattr(node, "image"))))


def color(value):
    if isinstance(value, (float, int)):
        return (float(value),) * 3 + (1.0,)
    try:
        v = tuple(float(x) for x in value)
        if len(v) in (3, 4):
            return v[:3] + (1.0,)
    except (TypeError, ValueError):
        pass
    return None


@dataclass
class Preview:
    image: object = None
    rgba: object = None
    source: str = ""
    note: str = ""
    node: object = None


class Resolver:
    def __init__(self):
        self.seen = set()
        self.steps = 0

    def socket(self, socket, stack=()):
        if socket is None:
            return None
        links = [x for x in socket.links if getattr(x, "is_valid", True)
                 and not getattr(x, "is_muted", False)]
        for link in links:
            result = self.node(link.from_node, stack, link.from_socket)
            if result is not None:
                return result
        if not links:
            rgba = color(getattr(socket, "default_value", None))
            if rgba is not None:
                return Preview(rgba=rgba, source=socket.name)
        return None

    def node(self, node, stack=(), output=None):
        if node is None or helper(node):
            return None
        key = (pointer(node), getattr(output, "identifier", ""),
               tuple(pointer(x) for x in stack))
        if key in self.seen or self.steps >= 128:
            return None
        self.seen.add(key)
        self.steps += 1
        if is_image_node(node):
            image = image_of(node)
            # Empty image node is explicit; do not fall through to its Power input.
            return Preview(image=image, source=node.name, node=node,
                           note="" if image else "Image node has no image")
        if getattr(node, "type", "") == "GROUP_INPUT":
            if stack and output:
                parent = stack[-1]
                ext = next((s for s in parent.inputs if s.identifier == output.identifier), None)
                return self.socket(ext, stack[:-1])
            return None
        if group(node):
            outs = [n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT"]
            outs.sort(key=lambda n: not getattr(n, "is_active_output", False))
            for out in outs[:1]:
                sockets = list(out.inputs)
                if output:
                    sockets = [s for s in sockets if s.identifier == output.identifier]
                for socket in sockets:
                    if socket.is_linked:
                        result = self.socket(socket, stack + (node,))
                        if result is not None:
                            return result
            return None
        if getattr(node, "mute", False):
            for link in getattr(node, "internal_links", ()):
                if output is None or link.to_socket == output:
                    result = self.socket(link.from_socket, stack)
                    if result is not None:
                        return result
        # A material must only inspect its color input, never its normal/roughness maps.
        inputs = list(node.inputs)
        for name in COLOR_INPUTS:
            socket = next((s for s in inputs if norm(s.name) == name), None)
            if socket is not None:
                return self.socket(socket, stack)
        if "material" in node.bl_idname.lower() and "mix" not in node.bl_idname.lower() \
                and "composite" not in node.bl_idname.lower() \
                and "layered" not in node.bl_idname.lower() \
                and "output" not in node.bl_idname.lower():
            return None
        # RGB/Greyscale color nodes expose constants on Octane's a_value attribute.
        if octane(node) and any(t in node.bl_idname.lower() for t in ("rgbcolor", "greyscalecolor")):
            rgba = color(getattr(node, "a_value", None))
            if rgba is not None:
                return Preview(rgba=rgba, source=node.name)
        # Follow upstream images through Reroute / Color Correction / Mix etc.
        # This deliberately returns the source bitmap, not evaluated shader math.
        for socket in inputs:
            if not socket.is_linked or norm(socket.name) in SKIP_INPUTS:
                continue
            result = self.socket(socket, stack)
            if result is not None:
                if getattr(node, "type", "") != "REROUTE":
                    result.note = result.note or "Source image only; shader effects are not evaluated"
                return result
        return None


def active_path(tree):
    """Top-level and nested active group trees; keep group-instance context."""
    result, stack, seen = [], (), set()
    while tree and pointer(tree) not in seen and len(seen) < 32:
        seen.add(pointer(tree))
        result.append((tree, stack))
        node = tree.nodes.active
        if not node or not group(node):
            break
        stack = stack + (node,)
        tree = node.node_tree
    return result


def selected_preview(tree):
    path = active_path(tree)
    # If top-level has nothing selected, stale selection inside a group is ignored.
    if not tree.nodes.active or not tree.nodes.active.select:
        return None
    for edit_tree, stack in reversed(path):
        active = edit_tree.nodes.active
        if active and active.select and not helper(active):
            result = Resolver().node(active, stack)
            if result is not None:
                return result
    return None


def default_preview(tree):
    outputs = [n for n in tree.nodes if not helper(n) and
               getattr(n, "target", "ALL").upper() != "EEVEE" and
               (n.bl_idname == "ShaderNodeOutputMaterial" or
                (octane(n) and "output" in n.bl_idname.lower()))]
    outputs.sort(key=lambda n: (not getattr(n, "is_active_output", True),
                                getattr(n, "target", "ALL") not in {"ALL", "OCTANE"}))
    for out in outputs:
        for s in out.inputs:
            if norm(s.name) in {"surface", "material"} and s.is_linked:
                result = Resolver().socket(s)
                if result is not None:
                    return result
    # Unconnected material graphs: only accept an unambiguous material node.
    candidates = [n for n in tree.nodes if octane(n) and
                  any(norm(s.name) in COLOR_INPUTS for s in n.inputs)]
    if not outputs and len(candidates) == 1:
        return Resolver().node(candidates[0])
    return Preview(source="Albedo", note="No supported Albedo image or color found")


def contains_octane(tree, seen=None):
    seen = set() if seen is None else seen
    if pointer(tree) in seen or len(seen) > 128:
        return False
    seen.add(pointer(tree))
    return any(octane(n) or (group(n) and contains_octane(n.node_tree, seen))
               for n in tree.nodes if not helper(n))
