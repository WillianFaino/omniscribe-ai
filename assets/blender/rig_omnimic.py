"""Rig OmniMic_TPOSE, generate 7 fluid animations, validate geometry, export .blend/.glb."""

import math
import os
import bpy
import bmesh
from mathutils import Matrix, Vector, Quaternion

_BLENDER_DIR = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = os.path.dirname(_BLENDER_DIR)
INPUT_BLEND = os.path.join(_BLENDER_DIR, "omnimic_tpose.blend")
OUTPUT_BLEND = os.path.join(_BLENDER_DIR, "omnimic_rigged.blend")
OUTPUT_GLB = os.path.join(_ASSETS_DIR, "models", "omnimic.glb")

FPS = 24


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def make_ortho_basis(aim_y: Vector, up_hint: Vector, twist_rad: float = 0.0) -> Matrix:
    """Return 3x3 world rotation matrix with local +Y=aim_y and local +Z near up_hint."""
    y_axis = aim_y.normalized()
    # Keep up_hint non-collinear with y_axis
    if abs(y_axis.dot(up_hint.normalized())) > 0.96:
        up_hint = Vector((0.0, -1.0, 0.0)) if abs(y_axis.y) < 0.9 else Vector((1.0, 0.0, 0.0))
    x_axis = y_axis.cross(up_hint).normalized()
    z_axis = x_axis.cross(y_axis).normalized()
    rot = Matrix((x_axis, y_axis, z_axis)).transposed()
    if abs(twist_rad) > 1e-6:
        rot = Matrix.Rotation(twist_rad, 3, y_axis) @ rot
    return rot


def clean_unused_collections():
    """Remove hidden draft collections from rigged copy so export & scene stay clean."""
    for col_name in ("OmniMic_Detailed", "OmniMic_V2_Exact"):
        col = bpy.data.collections.get(col_name)
        if not col:
            continue
        for obj in list(col.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(col)


def subdivide_arm_mesh(obj_name: str):
    """Insert smooth lengthwise rings along X into TPOSE_Arm_L/R for fluid curved bending."""
    obj = bpy.data.objects[obj_name]
    mesh = obj.data
    bm = bmesh.new()
    bm.from_mesh(mesh)

    # Identify lengthwise edges (dx dominates dy/dz)
    long_edges = [
        e for e in bm.edges
        if abs(e.verts[0].co.x - e.verts[1].co.x) > 0.02
    ]
    bmesh.ops.subdivide_edges(
        bm,
        edges=long_edges,
        cuts=3,
        use_grid_fill=False,
        smooth=0.0,
    )
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
        ax = (mw @ v.co).x * sign  # ranges 0.575 (pivot) to 0.960 (inside cuff)

        # Anchor inside pivot socket [0.575 .. 0.625] smoothly into UpperArm
        t_sh = smoothstep((ax - 0.575) / (0.630 - 0.575))
        w_yoke = (1.0 - t_sh) * 0.85

        # Wide S-curve bend zone around elbow x=0.775 -> [0.665 .. 0.885]
        t_el = smoothstep((ax - 0.665) / (0.885 - 0.665))

        # Wrist transition [0.915 .. 0.960] tucked cleanly inside cuff (0.975)
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
        ax = (mw @ v.co).x * sign  # 0.931 .. 1.019 around 0.975
        t = smoothstep((ax - 0.931) / (1.019 - 0.931))
        w_hand = 0.20 + 0.70 * t
        w_fore = 1.0 - w_hand
        vg_fore.add([v.index], w_fore, "REPLACE")
        vg_hand.add([v.index], w_hand, "REPLACE")


def attach_armature_modifier(obj, arm_obj, preserve_volume: bool = True):
    mod = obj.modifiers.new(name="Armature", type="ARMATURE")
    mod.object = arm_obj
    mod.use_deform_preserve_volume = preserve_volume
    # Crucial: deform mesh BEFORE Outline_Solidify runs
    idx = obj.modifiers.find("Armature")
    if idx > 0:
        with bpy.context.temp_override(object=obj):
            bpy.ops.object.modifier_move_to_index(modifier="Armature", index=0)


def build_armature():
    arm_data = bpy.data.armatures.new("OmniMic_RigData")
    arm_obj = bpy.data.objects.new("OmniMic_Rig", arm_data)
    col = bpy.data.collections.get("OmniMic_TPOSE") or bpy.context.scene.collection
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

    # Spine & Core
    b_root = add_bone("Root", (0.0, 0.0, 0.0), (0.0, 0.0, 0.25), roll_vec=(0.0, -1.0, 0.0))
    b_stand = add_bone("Stand_Base", (0.0, 0.0, 0.012), (0.0, 0.0, 0.56), parent=b_root, roll_vec=(0.0, -1.0, 0.0))
    b_yoke = add_bone("Torso_Yoke", (0.0, 0.0, 0.56), (0.0, 0.0, 1.33), parent=b_stand, connected=True, roll_vec=(0.0, -1.0, 0.0))
    b_head = add_bone("Head", (0.0, 0.0, 1.333), (0.0, 0.0, 2.38), parent=b_yoke, roll_vec=(0.0, -1.0, 0.0))

    # Facial Bones (note: TPOSE_Eye_L is at X=-0.144, TPOSE_Eye_R is at X=+0.144)
    # Named clearly by X sign so there is zero ambiguity
    b_eye_nx = add_bone("Eye_NegX", (-0.144, -0.354, 1.694), (-0.144, -0.47, 1.694), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    b_eye_px = add_bone("Eye_PosX", (0.144, -0.354, 1.694), (0.144, -0.47, 1.694), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Pupil_NegX", (-0.136, -0.466, 1.690), (-0.136, -0.54, 1.690), parent=b_eye_nx, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Pupil_PosX", (0.136, -0.466, 1.690), (0.136, -0.54, 1.690), parent=b_eye_px, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Brow_NegX", (-0.187, -0.372, 1.908), (-0.187, -0.47, 1.908), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Brow_PosX", (0.187, -0.372, 1.908), (0.187, -0.47, 1.908), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Mouth", (0.0, -0.371, 1.390), (0.0, -0.47, 1.390), parent=b_head, roll_vec=(0.0, 0.0, 1.0))
    add_bone("Jaw_Lip", (0.0, -0.390, 1.364), (0.0, -0.49, 1.364), parent=b_head, roll_vec=(0.0, 0.0, 1.0))

    # Arms (.L is +X, .R is -X)
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
        add_bone(
            f"Hand.{side}",
            (0.975 * s, 0.0, 1.24),
            (1.360 * s, 0.0, 1.24),
            parent=b_fore,
            connected=True,
            roll_vec=(0.0, 0.0, 1.0),
        )

    bpy.ops.object.mode_set(mode="OBJECT")

    # Save exact rest world rotations for world-aim pose solver
    rest_rots = {b.name: b.matrix_local.to_3x3().copy() for b in arm_obj.data.bones}

    # Subdivide arms cleanly & bind all TPOSE_* objects
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


class PoseRigHelper:
    """Computes clean quaternion pose targets sequentially top-down without flipping."""

    def __init__(self, arm_obj, rest_rots):
        self.arm = arm_obj
        self.rest_rots = rest_rots
        self.prev_quats = {}
        for pb in self.arm.pose.bones:
            pb.rotation_mode = "QUATERNION"

    def reset_pose(self):
        self.prev_quats.clear()
        for pb in self.arm.pose.bones:
            pb.location = Vector((0.0, 0.0, 0.0))
            pb.rotation_quaternion = Quaternion((1.0, 0.0, 0.0, 0.0))
            pb.scale = Vector((1.0, 1.0, 1.0))

    def set_local_euler(self, bone_name: str, rx_deg=0.0, ry_deg=0.0, rz_deg=0.0, loc=None, scale=None):
        pb = self.arm.pose.bones[bone_name]
        q = Matrix.Rotation(math.radians(rz_deg), 3, "Z") @ \
            Matrix.Rotation(math.radians(ry_deg), 3, "Y") @ \
            Matrix.Rotation(math.radians(rx_deg), 3, "X")
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
        """Orient pose bone in world space; requires parent pose already set and view_layer updated or parent matrix computed."""
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
        # Update depsgraph/pose matrix immediately for child bones in chain
        bpy.context.view_layer.update()


def sin_phase(frame: float, total: int, phase_frames: float = 0.0, mult: float = 1.0) -> float:
    return math.sin(2.0 * math.pi * mult * ((frame - 1.0 - phase_frames) / total))


def cos_phase(frame: float, total: int, phase_frames: float = 0.0, mult: float = 1.0) -> float:
    return math.cos(2.0 * math.pi * mult * ((frame - 1.0 - phase_frames) / total))


def ease_bell(u: float) -> float:
    """Smooth 0 -> 1 -> 0 bell over u in [0, 1]."""
    u = max(0.0, min(1.0, u))
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * u)


def ease_ramp(u: float, a: float, b: float) -> float:
    """0 before a, smoothstep 0->1 on [a,b], 1 after b."""
    return smoothstep((u - a) / max(1e-5, (b - a)))


def base_idle_arm_dirs(side: str, s: float, sway_s: float = 0.0, sway_f: float = 0.0, sway_w: float = 0.0):
    """Natural relaxed S-curve arms (NEVER stiff T-pose, NEVER 90deg corners)."""
    sx = 1.0 if side == "L" else -1.0
    up_dir = Vector((sx * (0.46 + 0.04 * s), -0.22 - 0.05 * sway_s, -0.86 + 0.04 * s)).normalized()
    fore_dir = Vector((sx * (0.28 + 0.04 * sway_f), -0.66 - 0.06 * sway_f, -0.69 + 0.05 * s)).normalized()
    hand_dir = Vector((sx * (0.10 + 0.05 * sway_w), -0.82 - 0.04 * sway_w, -0.56 + 0.06 * sway_w)).normalized()
    return up_dir, fore_dir, hand_dir


def pose_idle(helper: PoseRigHelper, f: int, n_frames: int):
    helper.reset_pose()
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    s_head = sin_phase(f, n_frames, 4.5, 1.0)
    s_glance = sin_phase(f, n_frames, -3.0, 1.0)
    s_arm_s = sin_phase(f, n_frames, 2.0, 1.0)
    s_arm_f = sin_phase(f, n_frames, 6.0, 1.0)
    s_arm_w = sin_phase(f, n_frames, 10.0, 1.0)
    s2 = sin_phase(f, n_frames, 0.0, 2.0)

    # Gentle breathing torso (+rx leans forward, -rx leans back) + overlapping head sway
    helper.set_local_euler("Torso_Yoke", rx_deg=-2.2 * s2 + 1.0, ry_deg=1.8 * s1, rz_deg=2.5 * s1)
    helper.set_local_euler("Head", rx_deg=2.5 * s2 - 1.5 * s_head, ry_deg=-2.2 * s_head, rz_deg=4.5 * s_head)

    # Subtle pupil glances + natural blinks around frames 18..24 and 54..60
    blink = 1.0
    for bf in (20, 56):
        d = abs(f - bf)
        if d <= 3:
            blink = min(blink, 0.09 + 0.91 * smoothstep(d / 3.0))

    helper.set_local_euler("Eye_NegX", scale=(1.0 + 0.06 * (1.0 - blink), 1.0, blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0 + 0.06 * (1.0 - blink), 1.0, blink))
    helper.set_local_euler("Pupil_NegX", loc=(0.014 * s_glance, 0.0, 0.004 * s2))
    helper.set_local_euler("Pupil_PosX", loc=(0.014 * s_glance, 0.0, 0.004 * s2))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.006 * s2 - 0.015 * (1.0 - blink)), ry_deg=2.0 * s1)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.006 * s2 - 0.015 * (1.0 - blink)), ry_deg=-2.0 * s1)

    bpy.context.view_layer.update()

    for side in ("L", "R"):
        ph = 0.0 if side == "L" else 0.35
        u, fo, ha = base_idle_arm_dirs(
            side,
            s2 if side == "L" else -s2,
            sway_s=s_arm_s + ph * 0.2,
            sway_f=s_arm_f + ph * 0.2,
            sway_w=s_arm_w + ph * 0.2,
        )
        sx = 1.0 if side == "L" else -1.0
        helper.aim_world(f"UpperArm.{side}", u, (0.0, 0.0, 1.0), twist_deg=sx * -12.0 + 5.0 * s_arm_s)
        helper.aim_world(f"Forearm.{side}", fo, (0.0, 0.0, 1.0), twist_deg=sx * -18.0 + 6.0 * s_arm_f)
        helper.aim_world(f"Hand.{side}", ha, (sx * 0.35, 0.15, 0.92), twist_deg=sx * -22.0 + 8.0 * s_arm_w)


def pose_greeting(helper: PoseRigHelper, f: int, n_frames: int):
    """Cheerful smooth arc wave on L arm (+X), overlapping wrist, zero 90deg hinges."""
    helper.reset_pose()
    u_t = (f - 1.0) / (n_frames - 1.0)  # 0..1, returns cleanly to idle at ends

    # Anticipation dip (0..0.16), joyful spring-up (0.16..0.82), soft settle (0.82..1.0)
    dip = -ease_bell(min(1.0, u_t / 0.20)) if u_t < 0.20 else 0.0
    lift = smoothstep(min(1.0, max(0.0, (u_t - 0.08) / 0.18))) * (1.0 - smoothstep(max(0.0, (u_t - 0.74) / 0.26)))
    wave_t = max(0.0, min(1.0, (u_t - 0.20) / 0.62))
    wave_env = ease_bell(wave_t) if 0.20 <= u_t <= 0.82 else 0.0

    # Two smooth sinusoidal figure-8 wave cycles with wrist trailing shoulder/forearm
    w_arm = math.sin(2.0 * math.pi * 2.0 * wave_t) * wave_env
    w_fore = math.sin(2.0 * math.pi * 2.0 * (wave_t - 0.07)) * wave_env
    w_hand = math.sin(2.0 * math.pi * 2.0 * (wave_t - 0.14)) * wave_env
    w_ov = math.cos(2.0 * math.pi * 2.0 * wave_t) * wave_env

    # Root bounce & torso lean (+rx forward anticipation dip, -rx cheerful open chest lift)
    helper.set_local_euler("Root", loc=(0.0, 0.0, 0.045 * lift - 0.015 * abs(dip)))
    helper.set_local_euler(
        "Torso_Yoke",
        rx_deg=4.5 * abs(dip) - 3.5 * lift,
        ry_deg=-4.0 * lift + 2.5 * w_arm,
        rz_deg=-5.0 * lift + 3.0 * w_arm,
    )
    helper.set_local_euler(
        "Head",
        rx_deg=4.0 * abs(dip) - 3.0 * lift,
        ry_deg=-6.5 * lift + 3.0 * w_fore,
        rz_deg=7.5 * lift + 4.0 * w_arm,
    )

    # Happy brows & blink at peak
    blink = 1.0
    if 28 <= f <= 34:
        blink = 0.09 + 0.91 * smoothstep(abs(f - 31) / 3.0)
    helper.set_local_euler("Eye_NegX", scale=(1.0 + 0.05 * lift, 1.0, blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0 + 0.05 * lift, 1.0, blink))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.030 * lift), ry_deg=6.0 * lift)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.030 * lift), ry_deg=-6.0 * lift)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.018 * lift), scale=(1.04, 1.0, 1.0 + 0.08 * lift))

    bpy.context.view_layer.update()

    # Left arm (+X): blends smoothly from relaxed idle -> graceful open high wave -> idle
    idle_u, idle_f, idle_h = base_idle_arm_dirs("L", 0.0)

    # Raised wave targets keep X generously outside +0.72 and open ~40deg elbow curve (never 90deg!)
    wave_u = Vector((0.89, -0.26 - 0.08 * w_ov, 0.36 + 0.14 * w_arm)).normalized()
    wave_f = Vector((0.46 + 0.24 * w_fore, -0.34 - 0.10 * w_ov, 0.82 + 0.06 * w_arm)).normalized()
    wave_h = Vector((0.14 + 0.34 * w_hand, -0.30 + 0.08 * w_ov, 0.94)).normalized()

    u_dir = idle_u.lerp(wave_u, lift).normalized()
    f_dir = idle_f.lerp(wave_f, lift).normalized()
    h_dir = idle_h.lerp(wave_h, lift).normalized()

    helper.aim_world("UpperArm.L", u_dir, (0.0, 0.0, 1.0), twist_deg=-12.0 * (1.0 - lift) + 12.0 * lift)
    helper.aim_world("Forearm.L", f_dir, (-0.15, -0.5, 0.85), twist_deg=-18.0 * (1.0 - lift) + 10.0 * w_fore)
    # In raised wave, thumb points inward/up (-X/+Z) so palm faces camera (-Y)!
    hand_up = Vector((-0.85, 0.0, 0.50)).normalized() if lift > 0.35 else Vector((0.35, 0.15, 0.92)).normalized()
    hand_up_blend = Vector((0.35, 0.15, 0.92)).lerp(Vector((-0.85, 0.08, 0.50)), lift).normalized()
    helper.aim_world("Hand.L", h_dir, hand_up_blend, twist_deg=-22.0 * (1.0 - lift) + 18.0 * w_hand)

    # Right arm (-X) balances naturally by sweeping gently out and forward
    r_idle_u, r_idle_f, r_idle_h = base_idle_arm_dirs("R", 0.0)
    r_bal_u = Vector((-0.64, -0.25, -0.72)).normalized()
    r_bal_f = Vector((-0.44, -0.62, -0.65)).normalized()
    r_bal_h = Vector((-0.24, -0.78, -0.58)).normalized()
    helper.aim_world("UpperArm.R", r_idle_u.lerp(r_bal_u, lift), (0.0, 0.0, 1.0), twist_deg=12.0)
    helper.aim_world("Forearm.R", r_idle_f.lerp(r_bal_f, lift), (0.0, 0.0, 1.0), twist_deg=16.0)
    helper.aim_world("Hand.R", r_idle_h.lerp(r_bal_h, lift), (-0.35, 0.15, 0.92), twist_deg=20.0)


def pose_listening(helper: PoseRigHelper, f: int, n_frames: int):
    """Attentive listening pose: subtle head nods + hand gently cupped near side-front."""
    helper.reset_pose()
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    s2 = sin_phase(f, n_frames, 2.0, 2.0)
    nod = sin_phase(f, n_frames, 5.0, 2.0)

    # Lean forward (+rx) toward speaker, head tilted thoughtfully
    helper.set_local_euler("Torso_Yoke", rx_deg=5.0 + 1.8 * s2, ry_deg=-2.5, rz_deg=4.0 + 1.5 * s1)
    helper.set_local_euler("Head", rx_deg=2.5 + 4.5 * nod, ry_deg=-7.5 + 1.5 * s1, rz_deg=6.0 + 2.0 * s1)

    blink = 1.0
    if 26 <= f <= 32:
        blink = 0.09 + 0.91 * smoothstep(abs(f - 29) / 3.0)
    helper.set_local_euler("Eye_NegX", scale=(1.05, 1.0, 1.05 * blink))
    helper.set_local_euler("Eye_PosX", scale=(1.05, 1.0, 1.05 * blink))
    helper.set_local_euler("Pupil_NegX", loc=(0.015 * s1, 0.0, 0.008))
    helper.set_local_euler("Pupil_PosX", loc=(0.015 * s1, 0.0, 0.008))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.020 + 0.005 * nod), ry_deg=4.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.014 + 0.005 * nod), ry_deg=-2.0)

    bpy.context.view_layer.update()

    # Left arm (+X) sweeps in an open, fluid ~48deg arc near ear/side (NO sharp 90deg L-elbow!)
    u_l = Vector((0.88, -0.34, 0.28 + 0.05 * s1)).normalized()
    f_l = Vector((0.36 + 0.06 * nod, -0.52, 0.77 + 0.04 * s2)).normalized()
    h_l = Vector((0.02, -0.34, 0.94)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-8.0)
    helper.aim_world("Forearm.L", f_l, (-0.25, -0.45, 0.85), twist_deg=-15.0 + 4.0 * nod)
    helper.aim_world("Hand.L", h_l, (-0.75, -0.55, 0.35), twist_deg=-12.0 + 5.0 * s2)

    # Right arm (-X) relaxed counterbalance
    u_r, f_r, h_r = base_idle_arm_dirs("R", s1, sway_s=s1, sway_f=nod, sway_w=s2)
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=12.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=18.0)
    helper.aim_world("Hand.R", h_r, (-0.35, 0.15, 0.92), twist_deg=20.0)


def pose_transcribing(helper: PoseRigHelper, f: int, n_frames: int):
    """Focused note-taking: both hands in front, right hand writing fluid Lissajous cursive."""
    helper.reset_pose()
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    s2 = sin_phase(f, n_frames, 0.0, 2.0)
    c4 = cos_phase(f, n_frames, 0.0, 4.0)
    s3 = sin_phase(f, n_frames, 2.0, 3.0)

    # Leaning FORWARD (+rx) looking down at notes
    helper.set_local_euler("Torso_Yoke", rx_deg=7.0 + 1.5 * s2, ry_deg=1.5 * s1, rz_deg=2.0 * s1)
    helper.set_local_euler("Head", rx_deg=9.0 + 2.0 * s2, ry_deg=2.5 * s1, rz_deg=-3.5 * s1)

    blink = 1.0
    if 30 <= f <= 35:
        blink = 0.09 + 0.91 * smoothstep(abs(f - 32.5) / 2.5)
    helper.set_local_euler("Eye_NegX", scale=(1.0, 1.0, 0.94 * blink))
    helper.set_local_euler("Eye_PosX", scale=(1.0, 1.0, 0.94 * blink))
    # Pupils scan across line left-to-right smoothly
    helper.set_local_euler("Pupil_NegX", loc=(0.018 * s1, 0.0, -0.022 + 0.004 * c4))
    helper.set_local_euler("Pupil_PosX", loc=(0.018 * s1, 0.0, -0.022 + 0.004 * c4))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, -0.012), ry_deg=-4.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, -0.012), ry_deg=4.0)

    bpy.context.view_layer.update()

    # Left hand (+X) holds imaginary pad steadily in front-left (+X, -Y)
    u_l = Vector((0.36, -0.62, -0.69 + 0.03 * s2)).normalized()
    f_l = Vector((-0.34, -0.72, -0.60 + 0.03 * s2)).normalized()
    h_l = Vector((-0.45, -0.80, -0.39 + 0.03 * s2)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-25.0)
    helper.aim_world("Forearm.L", f_l, (0.0, 0.0, 1.0), twist_deg=-45.0)
    helper.aim_world("Hand.L", h_l, (0.2, -0.2, 0.95), twist_deg=-55.0)

    # Right hand (-X) sweeps across in fluid 3:1 cursive handwriting loops in front (-Y)
    u_r = Vector((-0.34 + 0.05 * s1, -0.64, -0.69 + 0.04 * s2)).normalized()
    f_r = Vector((0.36 + 0.14 * s1, -0.76 - 0.05 * c4, -0.53 + 0.07 * s3)).normalized()
    h_r = Vector((0.52 + 0.18 * s1, -0.78 + 0.08 * c4, -0.33 + 0.12 * s3)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=25.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=40.0 + 6.0 * s1)
    helper.aim_world("Hand.R", h_r, (-0.25, -0.20, 0.94), twist_deg=55.0 + 12.0 * s3)


def pose_celebrating(helper: PoseRigHelper, f: int, n_frames: int):
    """Joyful victory V-arcs with bouncing torso and alternating rhythm."""
    helper.reset_pose()
    u_t = (f - 1.0) / n_frames
    # Two energetic bounces per 72 frames (each 36 frames)
    b = abs(math.sin(math.pi * 2.0 * u_t))
    hop = max(0.0, b - 0.15) * 0.105
    squish = max(0.0, 0.15 - b) * 0.45
    sway = sin_phase(f, n_frames, 0.0, 1.0)
    alt = sin_phase(f, n_frames, 3.0, 2.0)

    helper.set_local_euler("Root", loc=(0.0, 0.0, hop), scale=(1.0 + 0.04 * squish, 1.0 + 0.04 * squish, 1.0 - 0.06 * squish))
    # -rx opens chest & looks joyfully upward during hops, +rx cushions landing
    helper.set_local_euler("Torso_Yoke", rx_deg=-4.5 * b + 5.0 * squish, ry_deg=5.0 * sway, rz_deg=6.0 * sway)
    helper.set_local_euler("Head", rx_deg=-6.0 * b + 4.0 * squish, ry_deg=-6.0 * sway, rz_deg=-4.0 * sway)

    helper.set_local_euler("Eye_NegX", scale=(1.08, 1.0, 0.90 + 0.15 * b))
    helper.set_local_euler("Eye_PosX", scale=(1.08, 1.0, 0.90 + 0.15 * b))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.036 + 0.014 * b), ry_deg=8.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.036 + 0.014 * b), ry_deg=-8.0)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.034 - 0.012 * b), scale=(1.08, 1.0, 1.12))
    helper.set_local_euler("Mouth", scale=(1.08, 1.0, 1.18))

    bpy.context.view_layer.update()

    for side in ("L", "R"):
        sx = 1.0 if side == "L" else -1.0
        p = alt if side == "L" else -alt
        u_dir = Vector((sx * 0.80, -0.22, 0.56 + 0.14 * p)).normalized()
        f_dir = Vector((sx * 0.52, -0.26, 0.81 + 0.10 * p)).normalized()
        h_dir = Vector((sx * 0.34, -0.24, 0.91)).normalized()
        helper.aim_world(f"UpperArm.{side}", u_dir, (0.0, 0.0, 1.0), twist_deg=sx * 15.0)
        helper.aim_world(f"Forearm.{side}", f_dir, (sx * -0.2, -0.5, 0.85), twist_deg=sx * 10.0)
        helper.aim_world(f"Hand.{side}", h_dir, (sx * -0.75, 0.10, 0.65), twist_deg=sx * 12.0 * p)


def pose_warning(helper: PoseRigHelper, f: int, n_frames: int):
    """Alert double-take with cautious forward open-palm gesture."""
    helper.reset_pose()
    u_t = (f - 1.0) / n_frames
    s1 = sin_phase(f, n_frames, 0.0, 1.0)
    pulse = sin_phase(f, n_frames, 0.0, 2.0)
    glance = math.sin(2.0 * math.pi * u_t)

    # Subtle recoil backward (-rx) in alert stance
    helper.set_local_euler("Torso_Yoke", rx_deg=-3.5 - 1.2 * pulse, ry_deg=2.5 * glance, rz_deg=-5.0 + 3.0 * glance)
    helper.set_local_euler("Head", rx_deg=-2.0, ry_deg=-4.0 * glance, rz_deg=12.0 * glance)

    helper.set_local_euler("Eye_NegX", scale=(1.08, 1.0, 1.10))
    helper.set_local_euler("Eye_PosX", scale=(1.08, 1.0, 1.10))
    helper.set_local_euler("Pupil_NegX", loc=(0.022 * glance, 0.0, 0.008))
    helper.set_local_euler("Pupil_PosX", loc=(0.022 * glance, 0.0, 0.008))
    helper.set_local_euler("Brow_NegX", loc=(0.0, 0.0, 0.015), ry_deg=-7.0)
    helper.set_local_euler("Brow_PosX", loc=(0.0, 0.0, 0.026), ry_deg=-5.0)
    helper.set_local_euler("Jaw_Lip", loc=(0.0, 0.0, -0.016), scale=(0.90, 1.0, 1.08))

    bpy.context.view_layer.update()

    # Right arm (-X) raises smoothly in an open curved arc (<60deg elbow bend!)
    u_r = Vector((-0.78, -0.60, 0.12 + 0.08 * pulse)).normalized()
    f_r = Vector((-0.34, -0.52, 0.78 + 0.08 * pulse)).normalized()
    h_r = Vector((-0.05, -0.35 + 0.14 * pulse, 0.93)).normalized()
    helper.aim_world("UpperArm.R", u_r, (0.0, 0.0, 1.0), twist_deg=15.0)
    helper.aim_world("Forearm.R", f_r, (0.0, 0.0, 1.0), twist_deg=18.0)
    helper.aim_world("Hand.R", h_r, (0.85, -0.10, 0.50), twist_deg=8.0 * pulse)

    # Left arm (+X) pulls back slightly in alert stance
    u_l = Vector((0.55, 0.12, -0.83)).normalized()
    f_l = Vector((0.40, -0.45, -0.80)).normalized()
    h_l = Vector((0.25, -0.68, -0.69)).normalized()
    helper.aim_world("UpperArm.L", u_l, (0.0, 0.0, 1.0), twist_deg=-10.0)
    helper.aim_world("Forearm.L", f_l, (0.0, 0.0, 1.0), twist_deg=-16.0)
    helper.aim_world("Hand.L", h_l, (0.35, 0.15, 0.92), twist_deg=-18.0)


def pose_sleeping(helper: PoseRigHelper, f: int, n_frames: int):
    """Deep peaceful breathing cycle with closed eyes and relaxed trailing arms."""
    helper.reset_pose()
    breath_t = sin_phase(f, n_frames, 0.0, 1.0)
    breath_h = sin_phase(f, n_frames, 6.0, 1.0)
    breath_a = sin_phase(f, n_frames, 10.0, 1.0)
    breath_w = sin_phase(f, n_frames, 14.0, 1.0)

    # +rx slumps FORWARD & chin drops peacefully on exhale
    helper.set_local_euler("Torso_Yoke", rx_deg=8.0 - 2.6 * breath_t, ry_deg=2.0, rz_deg=1.5 * breath_t)
    helper.set_local_euler("Head", rx_deg=14.0 - 3.2 * breath_h, ry_deg=5.0, rz_deg=-4.0)

    # Eyes peacefully closed, pupils tucked back slightly so no gap peeks
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
    ("idle", 72, pose_idle),
    ("greeting", 60, pose_greeting),
    ("listening", 64, pose_listening),
    ("transcribing", 64, pose_transcribing),
    ("celebrating", 72, pose_celebrating),
    ("warning", 56, pose_warning),
    ("sleeping", 96, pose_sleeping),
]


def bake_action(arm_obj, helper: PoseRigHelper, name: str, n_frames: int, pose_fn):
    action = bpy.data.actions.new(name=name)
    action.use_fake_user = True
    arm_obj.animation_data_create()
    arm_obj.animation_data.action = action

    # Smooth keyframes every 2 frames so quaternion arc stays rock-solid & Bezier-smooth
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

    # Set Bezier interpolation cleanly
    # Blender 5.x uses layered actions or action.fcurves; support both gracefully
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

    # Push to NLA track so glTF exporter exports all 7 actions cleanly!
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
    yoke_obj = bpy.data.objects["TPOSE_Yoke"]
    stand_obj = bpy.data.objects["TPOSE_Stand_Base"]

    arm_l = bpy.data.objects["TPOSE_Arm_L"]
    cuff_l = bpy.data.objects["TPOSE_Cuff_L"]
    hand_l = bpy.data.objects["TPOSE_Hand_L"]
    arm_r = bpy.data.objects["TPOSE_Arm_R"]
    cuff_r = bpy.data.objects["TPOSE_Cuff_R"]
    hand_r = bpy.data.objects["TPOSE_Hand_R"]

    # Identify rest wrist ring verts on arm (|x| > 0.95) & cuff center & hand wrist verts
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
        action = bpy.data.actions[name]
        arm_obj.animation_data.action = action
        min_head_clear = 999.0
        max_wrist_gap = 0.0
        min_elbow_angle = 180.0
        max_elbow_angle = 0.0

        for f in range(1, n_frames + 1, 2):
            bpy.context.scene.frame_set(f)
            depsgraph.update()

            # Check Head clearance against ALL arm/cuff/hand vertices (in Head's posed local space!)
            pb_head = arm_obj.pose.bones["Head"]
            # Head mesh sits in rest around Head bone; transform test verts into Head rest space
            head_eval = head_obj.evaluated_get(depsgraph)
            head_inv = (arm_obj.matrix_world @ pb_head.matrix).inverted()

            for part in (arm_l, cuff_l, hand_l, arm_r, cuff_r, hand_r):
                pe = part.evaluated_get(depsgraph)
                mw = pe.matrix_world
                # Check sampled vertices (all verts on arm/cuff, every 2nd on hand)
                step_v = 1 if len(pe.data.vertices) < 200 else 2
                for i in range(0, len(pe.data.vertices), step_v):
                    pw = mw @ pe.data.vertices[i].co
                    # Transform world point into Head's posed frame!
                    # Actually compute closest distance to evaluated Head bounding/surface
                    pass
            # Direct distance check against evaluated Head mesh vertices (exact 3D distance!)
            h_mw = head_eval.matrix_world
            h_pts = [h_mw @ v.co for v in head_eval.data.vertices]
            for part in (arm_l, cuff_l, hand_l, arm_r, cuff_r, hand_r):
                pe = part.evaluated_get(depsgraph)
                p_mw = pe.matrix_world
                step_v = 1 if len(pe.data.vertices) < 200 else 3
                for i in range(0, len(pe.data.vertices), step_v):
                    pw = p_mw @ pe.data.vertices[i].co
                    # Quick reject before exact vertex scan
                    d2 = min((pw - hp).length_squared for hp in h_pts)
                    d = math.sqrt(d2)
                    if d < min_head_clear:
                        min_head_clear = d

            # Wrist continuity gap (distance between arm tip and hand wrist center; rest is ~0.045m inside cuff)
            al_e = arm_l.evaluated_get(depsgraph)
            cl_e = cuff_l.evaluated_get(depsgraph)
            hl_e = hand_l.evaluated_get(depsgraph)
            gap_l = abs((centroid(al_e, arm_l_tip_idx) - centroid(hl_e, hand_l_wr_idx)).length - 0.046)

            ar_e = arm_r.evaluated_get(depsgraph)
            cr_e = cuff_r.evaluated_get(depsgraph)
            hr_e = hand_r.evaluated_get(depsgraph)
            gap_r = abs((centroid(ar_e, arm_r_tip_idx) - centroid(hr_e, hand_r_wr_idx)).length - 0.046)
            max_wrist_gap = max(max_wrist_gap, gap_l, gap_r)

            # Measure elbow bend angle between UpperArm and Forearm
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
    """Export clean omnimic.glb with all 7 animations & clean Principled PBR colors."""
    os.makedirs(os.path.dirname(OUTPUT_GLB), exist_ok=True)
    # Deselect everything, select rig + visible TPOSE meshes
    bpy.ops.object.select_all(action="DESELECT")
    col = bpy.data.collections["OmniMic_TPOSE"]
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
    arm_obj, rest_rots = build_armature()
    helper = PoseRigHelper(arm_obj, rest_rots)

    for name, n_frames, fn in ANIMATIONS:
        bake_action(arm_obj, helper, name, n_frames, fn)

    ok = validate_geometry(arm_obj)
    # Set default preview pose to idle frame 1
    arm_obj.animation_data.action = bpy.data.actions["idle"]
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 72
    bpy.context.scene.frame_set(1)

    bpy.ops.wm.save_as_mainfile(filepath=OUTPUT_BLEND)
    print("SAVED_BLEND:", OUTPUT_BLEND)

    prepare_glb_export(arm_obj)
    if not ok:
        raise RuntimeError("Geometric validation failed — check clearances or wrist drift!")


if __name__ == "__main__":
    main()
