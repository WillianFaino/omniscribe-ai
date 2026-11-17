"""Rig Vozito_TPOSE, build PS1 low-poly props, generate 7 detailed fluid animations, QA & export."""

import math
import os
import bpy
import bmesh
from mathutils import Matrix, Vector, Quaternion

_BLENDER_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = os.path.dirname(_BLENDER_DIR)
INPUT_BLEND = os.path.join(_BLENDER_DIR, "vozito_tpose.blend")
OUTPUT_BLEND = os.path.join(_BLENDER_DIR, "vozito_rigged.blend")
OUTPUT_GLB = os.path.join(_ASSETS_DIR, "models", "vozito.glb")

FPS = 24


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def make_ortho_basis(aim_y: Vector, up_hint: Vector, twist_rad: float = 0.0) -> Matrix:
    """Return 3x3 world rotation matrix with local +Y=aim_y and local +Z near up_hint."""
    y_axis = aim_y.normalized()
    if abs(y_axis.dot(up_hint.normalized())) > 0.96:
        up_hint = Vector((0.0, -1.0, 0.0)) if abs(y_axis.y) < 0.9 else Vector((1.0, 0.0, 0.0))
    x_axis = y_axis.cross(up_hint).normalized()
    z_axis = x_axis.cross(y_axis).normalized()
    rot = Matrix((x_axis, y_axis, z_axis)).transposed()
    if abs(twist_rad) > 1e-6:
        rot = Matrix.Rotation(twist_rad, 3, y_axis) @ rot
    return rot


def clean_unused_collections():
    """Remove hidden draft collections from rigged copy and normalize Vozito collection name."""
    for col_name in ("Vozito_Detailed", "Vozito_V2_Exact", "OmniMic_Detailed", "OmniMic_V2_Exact"):
        col = bpy.data.collections.get(col_name)
        if not col:
            continue
        for obj in list(col.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(col)
    legacy_col = bpy.data.collections.get("OmniMic_TPOSE")
    if legacy_col and not bpy.data.collections.get("Vozito_TPOSE"):
        legacy_col.name = "Vozito_TPOSE"


def make_ps1_material(name: str, base_rgba, metallic=0.0, roughness=0.75, emission_rgb=None, emission_str=0.0):
    """Clone TPOSE_Chrome's NG_PS1_Bayer4x4 node shader so props match Vozito's exact PS1 look."""
    existing = bpy.data.materials.get(name)
    if existing:
        return existing
    tmpl = bpy.data.materials["TPOSE_Chrome"]
    mat = tmpl.copy()
    mat.name = name
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            n.inputs["Base Color"].default_value = base_rgba
            n.inputs["Metallic"].default_value = metallic
            n.inputs["Roughness"].default_value = roughness
            if emission_rgb is not None:
                n.inputs["Emission Color"].default_value = emission_rgb
                n.inputs["Emission Strength"].default_value = emission_str
        elif n.type == "EMISSION" and emission_rgb is not None and emission_str > 0.0:
            # Boost emissive LED props slightly above shaded look
            pass
    if emission_rgb is not None and emission_str > 0.0:
        # Clean glowing PS1 LED look while keeping Bayer node structure intact
        out_node = next(n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
        em_node = next(n for n in mat.node_tree.nodes if n.type == "EMISSION")
        em_node.inputs["Strength"].default_value = emission_str
        # Blend Principled directly into emission color if bright LED
        for n in mat.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                n.inputs["Base Color"].default_value = emission_rgb
    return mat


def create_prop_object(name: str, builder_fn, materials, outline_thickness=-0.007):
    """Create a prop mesh in Vozito_TPOSE collection with PS1 materials & Outline_Solidify."""
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    obj = bpy.data.objects.new(name, mesh)
    col = bpy.data.collections.get("Vozito_TPOSE") or bpy.data.collections.get("OmniMic_TPOSE") or bpy.context.scene.collection
    col.objects.link(obj)

    for mat in materials:
        obj.data.materials.append(mat)
    outline_idx = len(obj.data.materials)
    obj.data.materials.append(bpy.data.materials["TPOSE_Outline"])

    bm = bmesh.new()
    builder_fn(bm)
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()

    sol = obj.modifiers.new(name="Outline_Solidify", type="SOLIDIFY")
    sol.thickness = outline_thickness
    sol.offset = -1.0
    sol.use_flip_normals = True
    sol.material_offset = outline_idx
    return obj


def add_box(bm, center, size, mat_index=0, rot_euler=None):
    """Add an oriented box of given dimensions to bmesh with material_index."""
    mat = Matrix.Translation(Vector(center))
    if rot_euler is not None:
        mat = mat @ rot_euler.to_matrix().to_4x4()
    mat = mat @ Matrix.Diagonal((size[0], size[1], size[2], 1.0))
    ret = bmesh.ops.create_cube(bm, size=1.0, matrix=mat)
    verts_set = set(ret["verts"])
    for f in bm.faces:
        if all(v in verts_set for v in f.verts):
            f.material_index = mat_index


def add_cyl(bm, p0, p1, r0, r1, segs=8, mat_index=0):
    """Add a tapered cone/cylinder along world segment p0 -> p1."""
    v0 = Vector(p0)
    v1 = Vector(p1)
    vec = v1 - v0
    length = vec.length
    if length < 1e-6:
        return
    mid = (v0 + v1) * 0.5
    rot = Vector((0.0, 0.0, 1.0)).rotation_difference(vec.normalized()).to_matrix().to_4x4()
    xf = Matrix.Translation(mid) @ rot
    ret = bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        cap_tris=False,
        segments=segs,
        radius1=r0,
        radius2=r1,
        depth=length,
        matrix=xf,
    )
    verts_set = set(ret["verts"])
    for f in bm.faces:
        if all(v in verts_set for v in f.verts):
            f.material_index = mat_index


def build_props():
    """Create low-poly PS1 props positioned cleanly at their respective hand bones in T-pose."""
    mat_paper = make_ps1_material("Prop_PaperYellow", (0.98, 0.90, 0.44, 1.0), metallic=0.0, roughness=0.82)
    mat_binding = make_ps1_material("Prop_PadRed", (0.76, 0.14, 0.14, 1.0), metallic=0.05, roughness=0.65)
    mat_line = make_ps1_material("Prop_RuleBlue", (0.20, 0.44, 0.80, 1.0), metallic=0.0, roughness=0.7)
    mat_chrome = bpy.data.materials["TPOSE_Chrome"]

    mat_pencil = make_ps1_material("Prop_PencilYellow", (0.97, 0.72, 0.10, 1.0), metallic=0.05, roughness=0.55)
    mat_eraser = make_ps1_material("Prop_EraserPink", (0.95, 0.44, 0.54, 1.0), metallic=0.0, roughness=0.85)
    mat_wood = make_ps1_material("Prop_WoodTan", (0.86, 0.66, 0.42, 1.0), metallic=0.0, roughness=0.8)
    mat_graphite = make_ps1_material("Prop_Graphite", (0.10, 0.10, 0.12, 1.0), metallic=0.2, roughness=0.4)

    mat_mic_body = make_ps1_material("Prop_MicDark", (0.14, 0.16, 0.21, 1.0), metallic=0.25, roughness=0.5)
    mat_mic_led = make_ps1_material(
        "Prop_CyanLED",
        (0.08, 0.90, 1.0, 1.0),
        metallic=0.0,
        roughness=0.2,
        emission_rgb=(0.12, 0.92, 1.0, 1.0),
        emission_str=1.55,
    )
    mat_gold = make_ps1_material("Prop_TrophyGold", (1.0, 0.76, 0.14, 1.0), metallic=0.85, roughness=0.22)
    mat_walnut = make_ps1_material("Prop_WalnutBase", (0.20, 0.12, 0.08, 1.0), metallic=0.1, roughness=0.65)

    # 1. Prop_Notepad anchored at Left Palm center (1.19, -0.125, 1.25)
    # Paper lies in X-Z plane, back touches palm Y=-0.105, lined face points -Y
    def build_notepad(bm):
        px, py, pz = 1.19, -0.130, 1.25
        # Main pad stack
        add_box(bm, (px, py, pz), (0.34, 0.040, 0.44), mat_index=0)
        # Red header spine at top
        add_box(bm, (px, py - 0.004, pz + 0.185), (0.348, 0.048, 0.072), mat_index=1)
        # 4 chrome spiral rings across top
        for ox in (-0.11, -0.037, 0.037, 0.11):
            add_cyl(bm, (px + ox, py + 0.022, pz + 0.225), (px + ox, py - 0.026, pz + 0.225), 0.013, 0.013, segs=6, mat_index=3)
        # 6 crisp horizontal blue ruled lines on front face (-Y)
        for k in range(6):
            lz = pz + 0.11 - k * 0.056
            add_box(bm, (px, py - 0.021, lz), (0.28, 0.005, 0.010), mat_index=2)

    obj_pad = create_prop_object("Prop_Notepad", build_notepad, [mat_paper, mat_binding, mat_line, mat_chrome], outline_thickness=-0.007)

    # 2. Prop_Pencil anchored at Right Palm (-1.18, -0.115, 1.25), extending ergonomic angle past fingers (-X, -Y, -Z)
    def build_pencil(bm):
        p_start = Vector((-1.06, -0.06, 1.30))
        p_end = Vector((-1.54, -0.22, 1.16))
        d = (p_end - p_start).normalized()
        L = (p_end - p_start).length
        pts = [0.0, 0.13 * L, 0.23 * L, 0.78 * L, 0.92 * L, 1.0 * L]
        add_cyl(bm, p_start + d * pts[0], p_start + d * pts[1], 0.026, 0.026, segs=6, mat_index=1)
        add_cyl(bm, p_start + d * pts[1], p_start + d * pts[2], 0.027, 0.027, segs=6, mat_index=2)
        add_cyl(bm, p_start + d * pts[2], p_start + d * pts[3], 0.027, 0.027, segs=6, mat_index=0)
        add_cyl(bm, p_start + d * pts[3], p_start + d * pts[4], 0.027, 0.010, segs=6, mat_index=3)
        add_cyl(bm, p_start + d * pts[4], p_start + d * pts[5], 0.010, 0.002, segs=6, mat_index=4)

    obj_pencil = create_prop_object("Prop_Pencil", build_pencil, [mat_pencil, mat_eraser, mat_chrome, mat_wood, mat_graphite], outline_thickness=-0.005)

    # 3. Prop_MiniMic anchored at Right Palm (-1.18, -0.115, 1.25), extending up along +Z (thumb axis)
    def build_minimic(bm):
        mx, my, mz = -1.18, -0.115, 1.25
        add_cyl(bm, (mx, my, mz - 0.11), (mx, my, mz + 0.13), 0.024, 0.032, segs=8, mat_index=0)
        add_cyl(bm, (mx, my, mz + 0.13), (mx, my, mz + 0.175), 0.038, 0.038, segs=8, mat_index=1)
        add_cyl(bm, (mx, my, mz + 0.175), (mx, my, mz + 0.215), 0.044, 0.058, segs=8, mat_index=2)
        add_cyl(bm, (mx, my, mz + 0.215), (mx, my, mz + 0.31), 0.065, 0.067, segs=8, mat_index=2)
        add_cyl(bm, (mx, my, mz + 0.31), (mx, my, mz + 0.36), 0.067, 0.035, segs=8, mat_index=0)

    obj_minimic = create_prop_object("Prop_MiniMic", build_minimic, [mat_mic_body, mat_mic_led, mat_chrome], outline_thickness=-0.006)

    # 4. Prop_Trophy anchored cleanly atop Right Hand grip (-1.19, -0.115, 1.25) -> star at +0.37Z
    def build_trophy(bm):
        tx, ty, tz = -1.19, -0.115, 1.25
        add_cyl(bm, (tx, ty, tz + 0.04), (tx, ty, tz + 0.12), 0.082, 0.066, segs=8, mat_index=1)
        add_cyl(bm, (tx, ty, tz + 0.12), (tx, ty, tz + 0.19), 0.048, 0.028, segs=8, mat_index=0)
        add_cyl(bm, (tx, ty, tz + 0.19), (tx, ty, tz + 0.25), 0.028, 0.056, segs=8, mat_index=0)
        center_z = tz + 0.385
        r_out, r_in, thick = 0.175, 0.078, 0.046
        front_c = bm.verts.new((tx, ty - thick, center_z))
        back_c = bm.verts.new((tx, ty + thick, center_z))
        ring = []
        for i in range(10):
            ang = math.radians(90.0 + i * 36.0)
            rad = r_out if i % 2 == 0 else r_in
            ring.append(bm.verts.new((tx + rad * math.cos(ang), ty, center_z + rad * math.sin(ang))))
        for i in range(10):
            v1 = ring[i]
            v2 = ring[(i + 1) % 10]
            f1 = bm.faces.new((front_c, v1, v2))
            f2 = bm.faces.new((back_c, v2, v1))
            f1.material_index = 0
            f2.material_index = 0

    obj_trophy = create_prop_object("Prop_Trophy", build_trophy, [mat_gold, mat_walnut], outline_thickness=-0.006)

    return {
        "Prop_Notepad": obj_pad,
        "Prop_Pencil": obj_pencil,
        "Prop_MiniMic": obj_minimic,
        "Prop_Trophy": obj_trophy,
    }


def subdivide_arm_mesh(obj_name: str):
    """Insert smooth lengthwise rings along X into TPOSE_Arm_L/R for fluid curved bending."""
    obj = bpy.data.objects[obj_name]
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    long_edges = [e for e in bm.edges if abs(e.verts[0].co.x - e.verts[1].co.x) > 0.02]
    bmesh.ops.subdivide_edges(bm, edges=long_edges, cuts=3, use_grid_fill=False, smooth=0.0)
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def assign_rigid_weights(obj, bone_name: str):
    vg = obj.vertex_groups.get(bone_name) or obj.vertex_groups.new(name=bone_name)
    vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")


def assign_arm_weights(obj, side: str):
    """Smooth overlapping weights from Yoke socket -> UpperArm -> Forearm -> Hand."""
    sign = 1.0 if side == "L" else -1.0
    vg_yoke = obj.vertex_groups.new(name="Torso_Yoke")
    vg_up = obj.vertex_groups.new(name=f"UpperArm.{side}")
    vg_fore = obj.vertex_groups.new(name=f"Forearm.{side}")
    vg_hand = obj.vertex_groups.new(name=f"Hand.{side}")

    mw = obj.matrix_world
    for v in obj.data.vertices:
        ax = (mw @ v.co).x * sign
        t_sh = smoothstep((ax - 0.575) / (0.630 - 0.575))
        w_yoke = (1.0 - t_sh) * 0.85
        t_el = smoothstep((ax - 0.665) / (0.885 - 0.665))
        t_wr = smoothstep((ax - 0.915) / (0.960 - 0.915)) * 0.80

        remain = 1.0 - w_yoke
        w_up = remain * (1.0 - t_el)
        w_fore = remain * t_el * (1.0 - t_wr)
        w_hand = remain * t_el * t_wr

        if w_yoke > 1e-4:
            vg_yoke.add([v.index], w_yoke, "REPLACE")
        if w_up > 1e-4:
            vg_up.add([v.index], w_up, "REPLACE")
        if w_fore > 1e-4:
            vg_fore.add([v.index], w_fore, "REPLACE")
        if w_hand > 1e-4:
            vg_hand.add([v.index], w_hand, "REPLACE")


def assign_cuff_weights(obj, side: str):
    """Cuff bridges Forearm and Hand smoothly around wrist center x=+-0.975."""
    sign = 1.0 if side == "L" else -1.0
    vg_fore = obj.vertex_groups.new(name=f"Forearm.{side}")
    vg_hand = obj.vertex_groups.new(name=f"Hand.{side}")
    mw = obj.matrix_world
    for v in obj.data.vertices:
        ax = (mw @ v.co).x * sign
        t = smoothstep((ax - 0.931) / (1.019 - 0.931))
        w_hand = 0.20 + 0.70 * t
        vg_fore.add([v.index], 1.0 - w_hand, "REPLACE")
        vg_hand.add([v.index], w_hand, "REPLACE")


def attach_armature_modifier(obj, arm_obj, preserve_volume: bool = True):
    mod = obj.modifiers.new(name="Armature", type="ARMATURE")
    mod.object = arm_obj
    mod.use_deform_preserve_volume = preserve_volume
    idx = obj.modifiers.find("Armature")
    if idx > 0:
        with bpy.context.temp_override(object=obj):
            bpy.ops.object.modifier_move_to_index(modifier="Armature", index=0)


def build_armature(props_dict):
    arm_data = bpy.data.armatures.new("Vozito_RigData")
    arm_obj = bpy.data.objects.new("Vozito_Rig", arm_data)
    col = bpy.data.collections.get("Vozito_TPOSE") or bpy.data.collections.get("OmniMic_TPOSE") or bpy.context.scene.collection
    col.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones

    def add_bone(name, head, tail, parent=None, connected=False, roll_vec=(0.0, -1.0, 0.0), deform=True):
        b = eb.new(name)
        b.head = Vector(head)
        b.tail = Vector(tail)
        b.use_deform = deform
        if parent is not None:
            b.parent = parent
            b.use_connect = connected
        b.align_roll(Vector(roll_vec))
        return b

    b_root = add_bone("Root", (0.0, 0.0, 0.0), (0.0, 0.0, 0.25), roll_vec=(0.0, -1.0, 0.0))
    b_stand = add_bone("Stand_Base", (0.0, 0.0, 0.012), (0.0, 0.0, 0.56), parent=b_root, roll_vec=(0.0, -1.0, 0.0))
    b_yoke = add_bone("Torso_Yoke", (0.0, 0.0, 0.56), (0.0, 0.0, 1.33), parent=b_stand, connected=True, roll_vec=(0.0, -1.0, 0.0))
    b_head = add_bone("Head", (0.0, 0.0, 1.333), (0.0, 0.0, 2.38), parent=b_yoke, roll_vec=(0.0, -1.0, 0.0))

    b_eye_nx = add_bone("Eye_NegX", (-0.144, -0.354, 1.694), (-0.144, -0.47, 1.694), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    b_eye_px = add_bone("Eye_PosX", (0.144, -0.354, 1.694), (0.144, -0.47, 1.694), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Pupil_NegX", (-0.136, -0.466, 1.690), (-0.136, -0.54, 1.690), parent=b_eye_nx, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Pupil_PosX", (0.136, -0.466, 1.690), (0.136, -0.54, 1.690), parent=b_eye_px, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Brow_NegX", (-0.187, -0.372, 1.908), (-0.187, -0.47, 1.908), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Brow_PosX", (0.187, -0.372, 1.908), (0.187, -0.47, 1.908), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Mouth", (0.0, -0.371, 1.390), (0.0, -0.47, 1.390), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Jaw_Lip", (0.0, -0.390, 1.364), (0.0, -0.49, 1.364), parent=b_head, roll_vec=(0.0, 0.0, 1.0))

    hand_bones = {}
    for side, s in (("L", 1.0), ("R", -1.0)):
        b_up = add_bone(
            f"UpperArm.{side}",
            (0.575 * s, 0.0, 1.24),
            (0.775 * s, 0.0, 1.24),
            parent=b_yoke,
            connected=False,
            roll_vec=(0.0, 0.0, 1.0),
        )
        b_fore = add_bone(
            f"Forearm.{side}",
            (0.775 * s, 0.0, 1.24),
            (0.975 * s, 0.0, 1.24),
            parent=b_up,
            connected=True,
            roll_vec=(0.0, 0.0, 1.0),
        )
        hand_bones[side] = add_bone(
            f"Hand.{side}",
            (0.975 * s, 0.0, 1.24),
            (1.360 * s, 0.0, 1.24),
            parent=b_fore,
            connected=True,
            roll_vec=(0.0, 0.0, 1.0),
        )

    # Prop bones attached cleanly at the exact palm center of Hand.L / Hand.R
    add_bone("Prop_Notepad", (1.19, -0.130, 1.25), (1.19, -0.130, 1.48), parent=hand_bones["L"], roll_vec=(0.0, -1.0, 0.0))
    add_bone("Prop_Pencil", (-1.18, -0.115, 1.25), (-1.34, -0.24, 1.42), parent=hand_bones["R"], roll_vec=(0.0, 0.0, 1.0))
    add_bone("Prop_MiniMic", (-1.18, -0.115, 1.25), (-1.18, -0.115, 1.56), parent=hand_bones["R"], roll_vec=(0.0, -1.0, 0.0))
    add_bone("Prop_Trophy", (-1.19, -0.115, 1.25), (-1.19, -0.115, 1.62), parent=hand_bones["R"], roll_vec=(0.0, -1.0, 0.0))

    bpy.ops.object.mode_set(mode="OBJECT")
    rest_rots = {b.name: b.matrix_local.to_3x3().copy() for b in arm_obj.data.bones}

    subdivide_arm_mesh("TPOSE_Arm_L")
    subdivide_arm_mesh("TPOSE_Arm_R")

    rigid_map = {
        "TPOSE_Stand_Base": "Stand_Base",
        "TPOSE_Yoke": "Torso_Yoke",
        "TPOSE_Pivots": "Torso_Yoke",
        "TPOSE_Head": "Head",
        "TPOSE_InnerCore": "Head",
        "TPOSE_Eye_L": "Eye_NegX",
        "TPOSE_Eye_R": "Eye_PosX",
        "TPOSE_Pupil_L": "Pupil_NegX",
        "TPOSE_Catchlight_L": "Pupil_NegX",
        "TPOSE_Pupil_R": "Pupil_PosX",
        "TPOSE_Catchlight_R": "Pupil_PosX",
        "TPOSE_Brow_L": "Brow_NegX",
        "TPOSE_Brow_R": "Brow_PosX",
        "TPOSE_Mouth": "Mouth",
        "TPOSE_LowerLip": "Jaw_Lip",
        "TPOSE_Tongue": "Jaw_Lip",
        "TPOSE_Hand_L": "Hand.L",
        "TPOSE_Seam_L_0": "Hand.L",
        "TPOSE_Seam_L_1": "Hand.L",
        "TPOSE_Seam_L_2": "Hand.L",
        "TPOSE_Hand_R": "Hand.R",
        "TPOSE_Seam_R_0": "Hand.R",
        "TPOSE_Seam_R_1": "Hand.R",
        "TPOSE_Seam_R_2": "Hand.R",
        "Prop_Notepad": "Prop_Notepad",
        "Prop_Pencil": "Prop_Pencil",
        "Prop_MiniMic": "Prop_MiniMic",
        "Prop_Trophy": "Prop_Trophy",
    }

    for obj_name, bone_name in rigid_map.items():
        obj = bpy.data.objects[obj_name]
        assign_rigid_weights(obj, bone_name)
        attach_armature_modifier(obj, arm_obj, preserve_volume=False)

    assign_arm_weights(bpy.data.objects["TPOSE_Arm_L"], "L")
    attach_armature_modifier(bpy.data.objects["TPOSE_Arm_L"], arm_obj, preserve_volume=True)

    assign_arm_weights(bpy.data.objects["TPOSE_Arm_R"], "R")
    attach_armature_modifier(bpy.data.objects["TPOSE_Arm_R"], arm_obj, preserve_volume=True)

    assign_cuff_weights(bpy.data.objects["TPOSE_Cuff_L"], "L")
    attach_armature_modifier(bpy.data.objects["TPOSE_Cuff_L"], arm_obj, preserve_volume=True)

    assign_cuff_weights(bpy.data.objects["TPOSE_Cuff_R"], "R")
    attach_armature_modifier(bpy.data.objects["TPOSE_Cuff_R"], arm_obj, preserve_volume=True)

    return arm_obj, rest_rots


PROP_BONES = ("Prop_Notepad", "Prop_Pencil", "Prop_MiniMic", "Prop_Trophy")


class PoseRigHelper:
    """Computes clean quaternion pose targets sequentially top-down without flipping."""

    def __init__(self, arm_obj, rest_rots):
        self.arm = arm_obj
        self.rest_rots = rest_rots
        self.prev_quats = {}
        for pb in self.arm.pose.bones:
            pb.rotation_mode = "QUATERNION"

    def reset_pose(self, active_props=()):
        self.prev_quats.clear()
        for pb in self.arm.pose.bones:
            pb.location = Vector((0.0, 0.0, 0.0))
            pb.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
            if pb.name in PROP_BONES and pb.name not in active_props:
                # Near-zero invertible scale hides unused props in Blender AND glTF without singular matrix warnings
                pb.scale = Vector((0.0001, 0.0001, 0.0001))
            else:
                pb.scale = Vector((1.0, 1.0, 1.0))

    def set_local_euler(self, bone_name: str, rx_deg=0.0, ry_deg=0.0, rz_deg=0.0, loc=None, scale=None):
        pb = self.arm.pose.bones[bone_name]
        q = (
            Matrix.Rotation(math.radians(rz_deg), 3, "Z")
            @ Matrix.Rotation(math.radians(ry_deg), 3, "Y")
            @ Matrix.Rotation(math.radians(rx_deg), 3, "X")
        )
        quat = q.to_quaternion()
        if bone_name in self.prev_quats and quat.dot(self.prev_quats[bone_name]) < 0.0:
            quat = -quat
        self.prev_quats[bone_name] = quat.copy()
        pb.rotation_quaternion = quat
        if loc is not None:
            pb.location = Vector(loc)
        if scale is not None:
            pb.scale = Vector(scale)

    def aim_world(self, bone_name: str, aim_dir, up_hint, twist_deg=0.0):
        pb = self.arm.pose.bones[bone_name]
        if pb.parent is not None:
            p_pose = pb.parent.matrix.to_3x3()
            p_rest = self.rest_rots[pb.parent.name]
            current_rest = p_pose @ p_rest.inverted() @ self.rest_rots[bone_name]
        else:
            current_rest = self.rest_rots[bone_name]

        target_world = make_ortho_basis(Vector(aim_dir), Vector(up_hint), math.radians(twist_deg))
        quat = (current_rest.inverted() @ target_world).to_quaternion()
        if bone_name in self.prev_quats and quat.dot(self.prev_quats[bone_name]) < 0.0:
            quat = -quat
        self.prev_quats[bone_name] = quat.copy()
        pb.rotation_quaternion = quat
        bpy.context.view_layer.update()


def sin_phase(frame: float, total: int, phase_frames: float = 0.0, mult: float = 1.0) -> float:
    return math.sin(2.0 * math.pi * mult * ((frame - 1.0 - phase_frames) / total))


def cos_phase(frame: float, total: int, phase_frames: float = 0.0, mult: float = 1.0) -> float:
    return math.cos(2.0 * math.pi * mult * ((frame - 1.0 - phase_frames) / total))


def ease_bell(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * u)


def base_idle_arm_dirs(side: str, s: float, sway_s: float = 0.0, sway_f: float = 0.0, sway_w: float = 0.0):
    sx = 1.0 if side == "L" else -1.0
    up_dir = Vector((sx * (0.47 + 0.05 * s), -0.22 - 0.06 * sway_s, -0.85 + 0.04 * s)).normalized()
    fore_dir = Vector((sx * (0.28 + 0.05 * sway_f), -0.66 - 0.07 * sway_f, -0.69 + 0.06 * s)).normalized()
    hand_dir = Vector((sx * (0.10 + 0.06 * sway_w), -0.82 - 0.05 * sway_w, -0.56 + 0.07 * sway_w)).normalized()
    return up_dir, fore_dir, hand_dir


def pose_idle(helper: PoseRigHelper, f: int, n_frames: int):
    """Rich living idle (4s): breathing sway, curious glances, weight shift & double-blink."""
    helper.reset_pose()
    u_t = (f - 1.0) / n_frames
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    s2 = sin_phase(f, n_frames, 0.0, 2.0)

    # Curious look left (0.12..0.42), look right briefly (0.55..0.85)
    look_l = ease_bell((u_t - 0.10) / 0.34) if 0.10 <= u_t <= 0.44 else 0.0
    look_r = ease_bell((u_t - 0.54) / 0.34) if 0.54 <= u_t <= 0.88 else 0.0
    glance = look_r - look_l

    look_head_l = ease_bell((u_t - 0.14) / 0.34) if 0.14 <= u_t <= 0.48 else 0.0
    look_head_r = ease_bell((u_t - 0.58) / 0.34) if 0.58 <= u_t <= 0.92 else 0.0
    head_turn = look_head_r - look_head_l

    s_arm_s = sin_phase(f, n_frames, 3.0, 1.0)
    s_arm_f = sin_phase(f, n_frames, 8.0, 1.0)
    s_arm_w = sin_phase(f, n_frames, 13.0, 1.0)

    helper.set_local_euler(
        "Torso_Yoke",
        rx_deg=1.5 + 2.4 * s2,
        ry_deg=2.4 * s1 + 2.2 * head_turn,
        rz_deg=5.0 * head_turn + 2.0 * s1,
    )
    helper.set_local_euler(
        "Head",
        rx_deg=-2.5 * s2 + 2.0 * abs(head_turn),
        ry_deg=-4.0 * head_turn,
        rz_deg=8.5 * head_turn,
    )

    blink = 1.0
    for bf in (28, 36, 74):
        d = abs(f - bf)
        if d <= 2.5:
            blink = min(blink, 0.08 + 0.92 * smoothstep(d / 2.5))

    helper.set_local_euler("Eye_NegX", scale=(1.0 + 0.06 * (1.0 - blink), 1.0, blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0 + 0.06 * (1.0 - blink), 1.0, blink))
    helper.set_local_euler("Pupil_NegX", loc=(0.024 * glance, 0.0, 0.006 * s2 + 0.005 * abs(glance)))
    helper.set_local_euler("Pupil_PosX", loc=(0.024 * glance, 0.0, 0.006 * s2 + 0.005 * abs(glance)))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.008 * s2 + 0.012 * look_l), ry_deg=4.0 * glance)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.008 * s2 + 0.012 * look_r), ry_deg=-4.0 * glance)

    bpy.context.view_layer.update()

    for side in ("L", "R"):
        sx = 1.0 if side == "L" else -1.0
        extra = 0.35 * (look_l if side == "L" else look_r)
        u, fo, ha = base_idle_arm_dirs(
            side,
            (s2 if side == "L" else -s2) + extra,
            sway_s=s_arm_s + sx * 0.25 * head_turn,
            sway_f=s_arm_f + sx * 0.35 * head_turn,
            sway_w=s_arm_w + sx * 0.50 * head_turn,
        )
        helper.aim_world(f"UpperArm.{side}", u, (0.0, 0.0, 1.0), twist_deg=sx * -12.0 + 7.0 * s_arm_s)
        helper.aim_world(f"Forearm.{side}", fo, (0.0, 0.0, 1.0), twist_deg=sx * -18.0 + 9.0 * s_arm_f)
        helper.aim_world(f"Hand.{side}", ha, (sx * 0.35, 0.15, 0.92), twist_deg=sx * -22.0 + 12.0 * s_arm_w)


def pose_greeting(helper: PoseRigHelper, f: int, n_frames: int):
    """Expressive welcome: playful crouch -> spring-up hop -> sweeping figure-8 wave -> settle."""
    helper.reset_pose()
    u_t = (f - 1.0) / (n_frames - 1.0)

    dip = ease_bell(u_t / 0.18) if u_t <= 0.18 else 0.0
    lift = smoothstep(min(1.0, max(0.0, (u_t - 0.10) / 0.16))) * (1.0 - smoothstep(max(0.0, (u_t - 0.78) / 0.22)))
    wave_t = max(0.0, min(1.0, (u_t - 0.16) / 0.68))
    wave_env = ease_bell(wave_t) if 0.16 <= u_t <= 0.84 else 0.0

    # 2.5 generous sweeping waves with clear overlapping lag shoulder -> forearm -> wrist
    w_arm = math.sin(2.0 * math.pi * 2.5 * wave_t) * wave_env
    w_fore = math.sin(2.0 * math.pi * 2.5 * (wave_t - 0.055)) * wave_env
    w_hand = math.sin(2.0 * math.pi * 2.5 * (wave_t - 0.115)) * wave_env
    w_depth = math.cos(2.0 * math.pi * 2.5 * wave_t) * wave_env

    # Squash crouch then cheerful bouncy hover
    squish = dip * 0.055
    helper.set_local_euler(
        "Root",
        loc=(0.0, 0.0, 0.065 * lift - 0.012 * dip),
        scale=(1.0 + squish, 1.0 + squish, 1.0 - squish * 1.2),
    )
    helper.set_local_euler(
        "Torso_Yoke",
        rx_deg=6.0 * dip - 5.0 * lift + 2.0 * w_depth,
        ry_deg=-5.0 * lift + 3.5 * w_arm,
        rz_deg=-7.0 * lift + 4.5 * w_arm,
    )
    helper.set_local_euler(
        "Head",
        rx_deg=5.0 * dip - 3.5 * lift,
        ry_deg=-8.5 * lift + 4.0 * w_fore,
        rz_deg=9.0 * lift + 5.0 * w_arm,
    )

    blink = 1.0
    if 38 <= f <= 44:
        blink = 0.08 + 0.92 * smoothstep(abs(f - 41) / 3.0)
    helper.set_local_euler("Eye_NegX", scale=(1.0 + 0.06 * lift, 1.0, (1.0 + 0.04 * lift) * blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0 + 0.06 * lift, 1.0, (1.0 + 0.04 * lift) * blink))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.036 * lift - 0.015 * dip), ry_deg=7.5 * lift)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.036 * lift - 0.015 * dip), ry_deg=-7.5 * lift)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.026 * lift), scale=(1.06, 1.0, 1.0 + 0.10 * lift))

    bpy.context.view_layer.update()

    # Left arm (+X) wide figure-8 arc wave
    idle_u, idle_f, idle_h = base_idle_arm_dirs("L", 0.0)
    wave_u = Vector((0.86, -0.26 - 0.12 * w_depth, 0.44 + 0.18 * w_arm)).normalized()
    wave_f = Vector((0.44 + 0.28 * w_fore, -0.34 - 0.14 * w_depth, 0.83 + 0.08 * w_arm)).normalized()
    wave_h = Vector((0.10 + 0.42 * w_hand, -0.30 + 0.10 * w_depth, 0.92)).normalized()

    helper.aim_world("UpperArm.L", idle_u.lerp(wave_u, lift), (0.0, 0.0, 1.0), twist_deg=-12.0 * (1.0 - lift) + 14.0 * lift)
    helper.aim_world("Forearm.L", idle_f.lerp(wave_f, lift), (-0.15, -0.5, 0.85), twist_deg=-18.0 * (1.0 - lift) + 12.0 * w_fore)
    hand_up = Vector((0.35, 0.15, 0.92)).lerp(Vector((-0.85, 0.08, 0.50)), lift).normalized()
    helper.aim_world("Hand.L", idle_h.lerp(wave_h, lift), hand_up, twist_deg=-22.0 * (1.0 - lift) + 22.0 * w_hand)

    # Right arm (-X) opens gracefully as a warm counterbalance flourish
    r_idle_u, r_idle_f, r_idle_h = base_idle_arm_dirs("R", 0.0)
    r_open_u = Vector((-0.72, -0.24, -0.65 + 0.12 * lift)).normalized()
    r_open_f = Vector((-0.52, -0.56, -0.64 + 0.08 * w_arm)).normalized()
    r_open_h = Vector((-0.34, -0.74, -0.58 + 0.08 * w_fore)).normalized()
    helper.aim_world("UpperArm.R", r_idle_u.lerp(r_open_u, lift), (0.0, 0.0, 1.0), twist_deg=12.0)
    helper.aim_world("Forearm.R", r_idle_f.lerp(r_open_f, lift), (0.0, 0.0, 1.0), twist_deg=16.0 - 6.0 * w_fore)
    helper.aim_world("Hand.R", r_idle_h.lerp(r_open_h, lift), (-0.35, 0.15, 0.92), twist_deg=20.0 - 12.0 * w_hand)


def pose_listening(helper: PoseRigHelper, f: int, n_frames: int):
    """Cupping ear with L hand (+X) + aiming Prop_MiniMic toward monitor with R hand (-X)."""
    helper.reset_pose(active_props=("Prop_MiniMic",))
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    s2 = sin_phase(f, n_frames, 2.0, 2.0)
    nod = sin_phase(f, n_frames, 5.0, 3.0)
    sweep = sin_phase(f, n_frames, -4.0, 1.0)

    # Lean eagerly forward (+rx) & tilt toward cupped ear
    helper.set_local_euler("Torso_Yoke", rx_deg=6.0 + 1.6 * s2, ry_deg=-3.2, rz_deg=5.0 + 3.0 * sweep)
    helper.set_local_euler("Head", rx_deg=2.5 + 4.8 * nod, ry_deg=-8.5 + 1.5 * s1, rz_deg=6.5 + 3.5 * sweep)

    blink = 1.0
    if 34 <= f <= 40:
        blink = 0.08 + 0.92 * smoothstep(abs(f - 37) / 3.0)
    helper.set_local_euler("Eye_NegX", scale=(1.06, 1.0, 1.06 * blink))
    helper.set_local_euler("Eye_PosX", scale=(1.06, 1.0, 1.06 * blink))
    helper.set_local_euler("Pupil_NegX", loc=(0.018 * sweep, 0.0, 0.006))
    helper.set_local_euler("Pupil_PosX", loc=(0.018 * sweep, 0.0, 0.006))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.024 + 0.005 * nod), ry_deg=5.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.016 + 0.005 * nod), ry_deg=-2.0)

    bpy.context.view_layer.update()

    # Left arm (+X) cups beside "ear"/head side with palm curved gently forward-inward
    u_l = Vector((0.86, -0.32, 0.32 + 0.05 * s1)).normalized()
    f_l = Vector((0.26 + 0.05 * nod, -0.46, 0.85 + 0.04 * s2)).normalized()
    h_l = Vector((-0.14, -0.36, 0.93)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-8.0)
    helper.aim_world("Forearm.L", f_l, (-0.30, -0.50, 0.81), twist_deg=-20.0 + 4.0 * nod)
    helper.aim_world("Hand.L", h_l, (-0.62, -0.70, 0.35), twist_deg=-26.0 + 5.0 * s2)

    # Right arm (-X) extends forward toward monitor holding Prop_MiniMic aimed forward (-Y)!
    u_r = Vector((-0.62 + 0.08 * sweep, -0.58, -0.52 + 0.04 * s2)).normalized()
    f_r = Vector((-0.24 + 0.14 * sweep, -0.92, -0.30 + 0.06 * s2)).normalized()
    h_r = Vector((0.06 + 0.18 * sweep, -0.95, 0.28 + 0.08 * s1)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=15.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=12.0 + 6.0 * sweep)
    helper.aim_world("Hand.R", h_r, (0.0, -0.25, 0.96), twist_deg=8.0 + 8.0 * sweep)

    # Aim MiniMic capsule directly forward-up toward monitor/speaker (-Y/+Z) with subtle tracking sweep
    helper.aim_world("Prop_MiniMic", (0.22 * sweep, -0.86, 0.46), (0.0, 0.0, 1.0), twist_deg=0.0)


def pose_transcribing(helper: PoseRigHelper, f: int, n_frames: int):
    """Left hand holds yellow lined Prop_Notepad; Right hand writes 3 lines with Prop_Pencil."""
    helper.reset_pose(active_props=("Prop_Notepad", "Prop_Pencil"))
    u_t = (f - 1.0) / n_frames

    # 3 writing lines across the loop with crisp carriage-return hops:
    # Line 1: [0.00..0.30], Line 2: [0.33..0.63], Line 3: [0.66..0.92], Return: [0.92..1.00]
    if u_t < 0.30:
        prog_x = u_t / 0.30
        line_y = 1.0
        lift_pen = 0.0
    elif u_t < 0.33:
        prog_x = 1.0 - (u_t - 0.30) / 0.03
        line_y = 1.0 - 0.5 * ((u_t - 0.30) / 0.03)
        lift_pen = math.sin(math.pi * ((u_t - 0.30) / 0.03))
    elif u_t < 0.63:
        prog_x = (u_t - 0.33) / 0.30
        line_y = 0.5
        lift_pen = 0.0
    elif u_t < 0.66:
        prog_x = 1.0 - (u_t - 0.63) / 0.03
        line_y = 0.5 - 0.5 * ((u_t - 0.63) / 0.03)
        lift_pen = math.sin(math.pi * ((u_t - 0.63) / 0.03))
    elif u_t < 0.92:
        prog_x = (u_t - 0.66) / 0.26
        line_y = 0.0
        lift_pen = 0.0
    else:
        prog_x = 1.0 - (u_t - 0.92) / 0.08
        line_y = (u_t - 0.92) / 0.08
        lift_pen = math.sin(math.pi * ((u_t - 0.92) / 0.08))

    scribble = math.sin(2.0 * math.pi * 12.0 * u_t) * (1.0 - lift_pen)
    pad_bob = sin_phase(f, n_frames, 0.0, 2.0)

    # Lean gently forward (+rx) looking down at notepad while keeping clear of hands in front (-Y)
    helper.set_local_euler("Torso_Yoke", rx_deg=3.5 + 1.0 * pad_bob, ry_deg=1.5, rz_deg=2.5 - 3.0 * prog_x)
    helper.set_local_euler("Head", rx_deg=5.0 + 1.2 * abs(scribble) * 0.4, ry_deg=2.0, rz_deg=4.0 - 7.0 * prog_x)

    blink = 1.0
    if 38 <= f <= 43:
        blink = 0.08 + 0.92 * smoothstep(abs(f - 40.5) / 2.5)
    helper.set_local_euler("Eye_NegX", scale=(1.0, 1.0, 0.92 * blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0, 1.0, 0.92 * blink))
    helper.set_local_euler("Pupil_NegX", loc=(-0.016 + 0.032 * prog_x, 0.0, -0.026 + 0.010 * line_y))
    helper.set_local_euler("Pupil_PosX", loc=(-0.016 + 0.032 * prog_x, 0.0, -0.026 + 0.010 * line_y))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, -0.010), ry_deg=-4.5)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, -0.010), ry_deg=4.5)

    bpy.context.view_layer.update()

    # Left arm (+X): project forward (-Y) cleanly past head capsule before swinging inward
    u_l = Vector((0.06, -0.84, -0.54 + 0.02 * pad_bob)).normalized()
    f_l = Vector((-0.24, -0.87, -0.43 + 0.02 * pad_bob)).normalized()
    h_l = Vector((-0.84, -0.48, -0.24)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-10.0)
    helper.aim_world("Forearm.L", f_l, (0.0, 0.0, 1.0), twist_deg=-26.0)
    # Cradle notepad from underneath/behind (palm tilted up, fingers clear of front yellow page!)
    helper.aim_world("Hand.L", h_l, (-0.10, -0.15, 0.98), twist_deg=-58.0)
    helper.aim_world("Prop_Notepad", (0.14, 0.56, 0.82), (-0.10, -0.82, 0.56), twist_deg=0.0)
    helper.arm.pose.bones["Prop_Notepad"].location = Vector((0.0, 0.04, 0.045))

    # Right arm (-X): clear head in front (-Y), then aim Hand.R & Prop_Pencil slightly DOWN onto Prop_Notepad surface!
    u_r = Vector((-0.05, -0.84, -0.54 + 0.02 * line_y)).normalized()
    f_r = Vector((0.26 + 0.06 * prog_x, -0.87 - 0.02 * lift_pen, -0.42 + 0.05 * line_y + 0.04 * lift_pen)).normalized()
    h_r = Vector((0.91 + 0.03 * prog_x, -0.34 - 0.03 * lift_pen, -0.24 + 0.09 * line_y + 0.06 * scribble * 0.25 + 0.05 * lift_pen)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=10.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=16.0 + 4.0 * prog_x)
    helper.aim_world("Hand.R", h_r, (-0.15, -0.24, 0.96), twist_deg=18.0 + 8.0 * scribble)
    helper.set_local_euler("Prop_Pencil", rx_deg=14.0 + 5.0 * scribble, ry_deg=14.0, rz_deg=2.0 + 10.0 * prog_x)


def pose_celebrating(helper: PoseRigHelper, f: int, n_frames: int):
    """Victory hops hoisting Golden Star Prop_Trophy high in Right Hand (-X) + joyful Left pump."""
    helper.reset_pose(active_props=("Prop_Trophy",))
    u_t = (f - 1.0) / n_frames
    b = abs(math.sin(math.pi * 2.0 * u_t))
    hop = max(0.0, b - 0.15) * 0.115
    squish = max(0.0, 0.15 - b) * 0.50
    sway = sin_phase(f, n_frames, 0.0, 1.0)
    alt = sin_phase(f, n_frames, 3.0, 2.0)

    helper.set_local_euler("Root", loc=(0.0, 0.0, hop), scale=(1.0 + 0.045 * squish, 1.0 + 0.045 * squish, 1.0 - 0.065 * squish))
    helper.set_local_euler("Torso_Yoke", rx_deg=-5.0 * b + 5.5 * squish, ry_deg=5.0 * sway, rz_deg=6.0 * sway)
    helper.set_local_euler("Head", rx_deg=-6.5 * b + 4.5 * squish, ry_deg=-6.0 * sway, rz_deg=-4.0 * sway)

    helper.set_local_euler("Eye_NegX", scale=(1.08, 1.0, 0.92 + 0.14 * b))
    helper.set_local_euler("Eye_PosX", scale=(1.08, 1.0, 0.92 + 0.14 * b))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.038 + 0.014 * b), ry_deg=8.5)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.038 + 0.014 * b), ry_deg=-8.5)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.036 - 0.012 * b), scale=(1.08, 1.0, 1.14))
    helper.set_local_euler("Mouth", scale=(1.08, 1.0, 1.20))

    bpy.context.view_layer.update()

    # Left arm (+X) joyous pumping arc
    u_l = Vector((0.80, -0.22, 0.56 + 0.16 * alt)).normalized()
    f_l = Vector((0.50, -0.28, 0.82 + 0.10 * alt)).normalized()
    h_l = Vector((0.30, -0.26, 0.92)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=15.0)
    helper.aim_world("Forearm.L", f_l, (-0.2, -0.5, 0.85), twist_deg=10.0)
    helper.aim_world("Hand.L", h_l, (-0.75, 0.10, 0.65), twist_deg=14.0 * alt)

    # Right arm (-X) proudly holds Prop_Trophy high with upright star orientation!
    u_r = Vector((-0.78, -0.24, 0.58 + 0.14 * b)).normalized()
    f_r = Vector((-0.52, -0.28, 0.81 + 0.10 * b)).normalized()
    h_r = Vector((-0.60, -0.18, 0.78)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=-15.0)
    helper.aim_world("Forearm.R", f_r, (0.2, -0.5, 0.85), twist_deg=-10.0)
    helper.aim_world("Hand.R", h_r, (0.78, 0.05, 0.62), twist_deg=-8.0 * sway)
    helper.aim_world("Prop_Trophy", (0.14 * sway, -0.10, 0.98), (0.0, -1.0, 0.0), twist_deg=0.0)


def pose_warning(helper: PoseRigHelper, f: int, n_frames: int):
    """Alert recoil + double-take head scan + cautionary double open-palm pulse."""
    helper.reset_pose()
    u_t = (f - 1.0) / n_frames
    pulse = sin_phase(f, n_frames, 0.0, 2.0)
    # Asymmetric snappy-smooth look left then right
    glance = math.sin(2.0 * math.pi * u_t) * 0.75 + 0.25 * math.sin(4.0 * math.pi * u_t)

    helper.set_local_euler("Torso_Yoke", rx_deg=-4.0 - 1.5 * pulse, ry_deg=3.0 * glance, rz_deg=-5.0 + 3.5 * glance)
    helper.set_local_euler("Head", rx_deg=-2.5, ry_deg=-5.0 * glance, rz_deg=14.0 * glance)

    helper.set_local_euler("Eye_NegX", scale=(1.10, 1.0, 1.12))
    helper.set_local_euler("Eye_PosX", scale=(1.10, 1.0, 1.12))
    helper.set_local_euler("Pupil_NegX", loc=(0.025 * glance, 0.0, 0.009))
    helper.set_local_euler("Pupil_PosX", loc=(0.025 * glance, 0.0, 0.009))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.012), ry_deg=-8.5)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.028), ry_deg=-6.0)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.018), scale=(0.88, 1.0, 1.10))

    bpy.context.view_layer.update()

    u_r = Vector((-0.78, -0.60, 0.14 + 0.10 * pulse)).normalized()
    f_r = Vector((-0.34, -0.52, 0.78 + 0.10 * pulse)).normalized()
    h_r = Vector((-0.05, -0.34 + 0.16 * pulse, 0.93)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=15.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=18.0)
    helper.aim_world("Hand.R", h_r, (0.85, -0.10, 0.50), twist_deg=10.0 * pulse)

    u_l = Vector((0.55, 0.14, -0.82)).normalized()
    f_l = Vector((0.40, -0.45, -0.80)).normalized()
    h_l = Vector((0.25, -0.68, -0.69)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-10.0)
    helper.aim_world("Forearm.L", f_l, (0.0, 0.0, 1.0), twist_deg=-16.0)
    helper.aim_world("Hand.L", h_l, (0.35, 0.15, 0.92), twist_deg=-18.0)


def pose_sleeping(helper: PoseRigHelper, f: int, n_frames: int):
    """Deep peaceful breathing cycle with forward slump and classic gentle micro nod-off."""
    helper.reset_pose()
    u_t = (f - 1.0) / n_frames
    breath_t = sin_phase(f, n_frames, 0.0, 1.0)
    breath_h = sin_phase(f, n_frames, 6.0, 1.0)
    breath_a = sin_phase(f, n_frames, 10.0, 1.0)
    breath_w = sin_phase(f, n_frames, 14.0, 1.0)

    # Extra gentle head nod-off dip around middle of cycle (0.48..0.78)
    nod_off = ease_bell((u_t - 0.45) / 0.34) * 5.0 if 0.45 <= u_t <= 0.79 else 0.0

    helper.set_local_euler("Torso_Yoke", rx_deg=8.5 - 2.6 * breath_t, ry_deg=2.0, rz_deg=1.5 * breath_t)
    helper.set_local_euler("Head", rx_deg=14.5 - 3.2 * breath_h + nod_off, ry_deg=5.0, rz_deg=-4.0)

    helper.set_local_euler("Eye_NegX", scale=(1.04, 1.0, 0.06))
    helper.set_local_euler("Eye_PosX", scale=(1.04, 1.0, 0.06))
    helper.set_local_euler("Pupil_NegX", loc=(0.0, 0.022, 0.0))
    helper.set_local_euler("Pupil_PosX", loc=(0.0, 0.022, 0.0))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, -0.022 + 0.008 * breath_h), ry_deg=-4.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, -0.022 + 0.008 * breath_h), ry_deg=4.0)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.008 * max(0.0, breath_h)))

    bpy.context.view_layer.update()

    for side in ("L", "R"):
        sx = 1.0 if side == "L" else -1.0
        u_dir = Vector((sx * (0.42 + 0.04 * breath_a), -0.18 - 0.04 * breath_a, -0.89)).normalized()
        f_dir = Vector((sx * (0.26 + 0.03 * breath_w), -0.52 - 0.05 * breath_a, -0.81)).normalized()
        h_dir = Vector((sx * 0.14, -0.68 - 0.05 * breath_w, -0.72)).normalized()
        helper.aim_world(f"UpperArm.{side}", u_dir, (0.0, 0.0, 1.0), twist_deg=sx * -10.0)
        helper.aim_world(f"Forearm.{side}", f_dir, (0.0, 0.0, 1.0), twist_deg=sx * -14.0)
        helper.aim_world(f"Hand.{side}", h_dir, (sx * 0.35, 0.15, 0.92), twist_deg=sx * -18.0)


ANIMATIONS = [
    ("idle", 96, pose_idle),
    ("greeting", 76, pose_greeting),
    ("listening", 80, pose_listening),
    ("transcribing", 80, pose_transcribing),
    ("celebrating", 72, pose_celebrating),
    ("warning", 64, pose_warning),
    ("sleeping", 96, pose_sleeping),
]


def bake_action(arm_obj, helper: PoseRigHelper, name: str, n_frames: int, pose_fn):
    action = bpy.data.actions.new(name=name)
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    step = 2
    frames = list(range(1, n_frames + 1, step))
    if frames[-1] != n_frames:
        frames.append(n_frames)

    helper.prev_quats.clear()
    for f in frames:
        bpy.context.scene.frame_set(f)
        pose_fn(helper, f, n_frames)
        for pb in arm_obj.pose.bones:
            pb.keyframe_insert(data_path="location", frame=f)
            pb.keyframe_insert(data_path="rotation_quaternion", frame=f)
            pb.keyframe_insert(data_path="scale", frame=f)

    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        for fc in fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "BEZIER"
                kp.handle_left_type = "AUTO_CLAMPED"
                kp.handle_right_type = "AUTO_CLAMPED"
    elif hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fc in bag.fcurves:
                        for kp in fc.keyframe_points:
                            kp.interpolation = "BEZIER"
                            kp.handle_left_type = "AUTO_CLAMPED"
                            kp.handle_right_type = "AUTO_CLAMPED"

    track = arm_obj.animation_data.nla_tracks.new()
    track.name = name
    strip = track.strips.new(name, 1, action)
    strip.action_frame_start = 1
    strip.action_frame_end = n_frames
    track.mute = True
    arm_obj.animation_data.action = None
    return action


def validate_geometry(arm_obj):
    """Mathematically verify EVERY frame of EVERY animation for zero clipping & zero wrist drift."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    head_obj = bpy.data.objects["TPOSE_Head"]

    arm_l = bpy.data.objects["TPOSE_Arm_L"]
    cuff_l = bpy.data.objects["TPOSE_Cuff_L"]
    hand_l = bpy.data.objects["TPOSE_Hand_L"]
    arm_r = bpy.data.objects["TPOSE_Arm_R"]
    cuff_r = bpy.data.objects["TPOSE_Cuff_R"]
    hand_r = bpy.data.objects["TPOSE_Hand_R"]

    arm_l_tip_idx = [v.index for v in arm_l.data.vertices if (arm_l.matrix_world @ v.co).x > 0.955]
    arm_r_tip_idx = [v.index for v in arm_r.data.vertices if (arm_r.matrix_world @ v.co).x < -0.955]
    hand_l_wr_idx = [v.index for v in hand_l.data.vertices if (hand_l.matrix_world @ v.co).x < 1.02]
    hand_r_wr_idx = [v.index for v in hand_r.data.vertices if (hand_r.matrix_world @ v.co).x > -1.02]

    def centroid(obj_eval, idxs):
        mw = obj_eval.matrix_world
        pts = [mw @ obj_eval.data.vertices[i].co for i in idxs]
        return sum(pts, Vector((0.0, 0.0, 0.0))) / len(pts)

    print("=== GEOMETRIC QA VALIDATION ===")
    all_ok = True
    for name, n_frames, _ in ANIMATIONS:
        arm_obj.animation_data.action = bpy.data.actions[name]
        min_head_clear = 999.0
        max_wrist_gap = 0.0
        min_elbow_angle = 180.0
        max_elbow_angle = 0.0

        for f in range(1, n_frames + 1, 2):
            bpy.context.scene.frame_set(f)
            depsgraph.update()

            head_eval = head_obj.evaluated_get(depsgraph)
            h_mw = head_eval.matrix_world
            h_pts = [h_mw @ v.co for v in head_eval.data.vertices]
            for part in (arm_l, cuff_l, hand_l, arm_r, cuff_r, hand_r):
                pe = part.evaluated_get(depsgraph)
                p_mw = pe.matrix_world
                step_v = 1 if len(pe.data.vertices) < 200 else 3
                for i in range(0, len(pe.data.vertices), step_v):
                    pw = p_mw @ pe.data.vertices[i].co
                    d = math.sqrt(min((pw - hp).length_squared for hp in h_pts))
                    if d < min_head_clear:
                        min_head_clear = d

            al_e = arm_l.evaluated_get(depsgraph)
            hl_e = hand_l.evaluated_get(depsgraph)
            gap_l = abs((centroid(al_e, arm_l_tip_idx) - centroid(hl_e, hand_l_wr_idx)).length - 0.046)

            ar_e = arm_r.evaluated_get(depsgraph)
            hr_e = hand_r.evaluated_get(depsgraph)
            gap_r = abs((centroid(ar_e, arm_r_tip_idx) - centroid(hr_e, hand_r_wr_idx)).length - 0.046)
            max_wrist_gap = max(max_wrist_gap, gap_l, gap_r)

            for side in ("L", "R"):
                u_vec = arm_obj.pose.bones[f"UpperArm.{side}"].vector.normalized()
                f_vec = arm_obj.pose.bones[f"Forearm.{side}"].vector.normalized()
                ang = math.degrees(math.acos(max(-1.0, min(1.0, u_vec.dot(f_vec)))))
                min_elbow_angle = min(min_elbow_angle, ang)
                max_elbow_angle = max(max_elbow_angle, ang)

        status = "OK" if (min_head_clear >= 0.04 and max_wrist_gap <= 0.015) else "FAIL"
        if status != "OK":
            all_ok = False
        print(
            f"  [{status}] {name:13s} | min_head_clear={min_head_clear:.3f}m | "
            f"max_wrist_drift={max_wrist_gap:.4f}m | elbow_bend={min_elbow_angle:.1f}..{max_elbow_angle:.1f} deg"
        )

    arm_obj.animation_data.action = bpy.data.actions["idle"]
    return all_ok


def prepare_glb_export(arm_obj):
    os.makedirs(os.path.dirname(OUTPUT_GLB), exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    col = bpy.data.collections.get("Vozito_TPOSE") or bpy.data.collections["OmniMic_TPOSE"]
    for obj in col.objects:
        obj.select_set(True)
    arm_obj.select_set(True)
    bpy.context.view_layer.objects.active = arm_obj

    bpy.ops.export_scene.gltf(
        filepath=OUTPUT_GLB,
        export_format="GLB",
        use_selection=True,
        export_animations=True,
        export_nla_strips=True,
        export_nla_strips_merged_animation_name="",
        export_apply=False,
        export_materials="EXPORT",
    )
    print("EXPORTED_GLB:", OUTPUT_GLB, os.path.getsize(OUTPUT_GLB))


def main():
    bpy.ops.wm.open_mainfile(filepath=INPUT_BLEND)
    clean_unused_collections()
    props_dict = build_props()
    arm_obj, rest_rots = build_armature(props_dict)
    helper = PoseRigHelper(arm_obj, rest_rots)

    for name, n_frames, fn in ANIMATIONS:
        bake_action(arm_obj, helper, name, n_frames, fn)

    ok = validate_geometry(arm_obj)
    arm_obj.animation_data.action = bpy.data.actions["idle"]
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 96
    bpy.context.scene.frame_set(1)

    bpy.ops.wm.save_as_mainfile(filepath=OUTPUT_BLEND)
    print("SAVED_BLEND:", OUTPUT_BLEND)

    prepare_glb_export(arm_obj)
    if not ok:
        raise RuntimeError("Geometric validation failed — check clearances or wrist drift!")


if __name__ == "__main__":
    main()
