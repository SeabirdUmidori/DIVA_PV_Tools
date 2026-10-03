
"""Where the elbow pole goes, from the source rig's own shoulder/elbow/wrist.

Shared by the frame handler (interactive playback) and the exporter (sampling).  The exporter has to
call it itself: Blender does not run frame_change_post handlers restored from a .blend, so in a
template saved with the handler "already activated" the poles never moved, and every arm bone the
game solves from them bent the wrong way.

The placement rule is the add-on author's own, unchanged: the locator sits about 0.3 m beyond the
elbow, blended toward the upper arm's local +-X as the arm straightens.  That is deliberately not
"the elbow itself".  The DIVA arm (shoulder and elbow are solved by the engine from this locator),
so the locator has to stay a comfortable distance outside the joint: SEGA's own motions keep
|tl_up_kata| at 0.36..0.61 m (bone_data.bin gives the bone a rest length of 0.4544 m), and a pole
placed on the elbow drops it to 0.15..0.38 m and shortens the lever to nothing exactly when the arm
straightens - which leaves frames where the hand turns over 90 degrees in one frame.  Measured on
the reference dance, this rule leaves no such frame (worst 43.8 / 45.0 degrees, inside SEGA's 26..60).
"""
import bpy
from mathutils import Vector

# The shipped offset, kept verbatim: 0.3 m along the upper arm's local -X for the left, +X for the
# right, used only while the arm is straight enough that the elbow gives no usable direction.
ADJUSTMENT_VECTORS = {"Left": Vector((-0.3, 0.0, 0.0)), "Right": Vector((0.3, 0.0, 0.0))}

DEFAULT_THRESHOLD = 0.15


def pole_position(side, shoulder, elbow, wrist, arm_matrix, threshold=DEFAULT_THRESHOLD):
    midpoint = (shoulder + wrist) * 0.5
    direction = elbow - midpoint
    length = direction.length
    blend = max(0.0, min(1.0, length / (threshold or DEFAULT_THRESHOLD)))
    normal = direction.normalized() * 0.3 if length > 1e-4 else Vector((0.0, 0.0, 0.0))
    adjusted = arm_matrix.to_3x3() @ ADJUSTMENT_VECTORS[side]
    return elbow + normal * blend + adjusted * (1.0 - blend)


# Everything the driver needs is named by the VMD-import template, so it is resolved by name on
# each drive: the armature the dance lands on, the two pole empties, and the three arm bones per
# side.  A file built from the template therefore needs no manual wiring; a file that is missing
# one of these names gets the missing thing reported, not a guessed substitute.
SOURCE_ARMATURE = "Import Rig"
SIDES = (
    ("Left", "ElbowPole_左", ("左腕", "左ひじ", "左手首")),
    ("Right", "ElbowPole_右", ("右腕", "右ひじ", "右手首")),
)


def drive_scene_poles(_scene=None):
    """Write both elbow poles from the template's own rig, resolved by name.  Returns problems.

    The argument is the scene the frame handler was called with; it is not needed (everything is
    looked up in `bpy.data`), and callers can pass None.  An empty problem list means both poles
    moved; anything else says exactly which template piece this file lacks.
    """
    armature = bpy.data.objects.get(SOURCE_ARMATURE)
    if armature is None or armature.type != 'ARMATURE':
        return ["no %r armature in this file - import a motion first" % SOURCE_ARMATURE]

    problems = []
    world = armature.matrix_world
    for side, empty_name, bone_names in SIDES:
        locator = bpy.data.objects.get(empty_name)
        if locator is None:
            problems.append("no pole target %r in this file" % empty_name)
            continue
        bones = [armature.pose.bones.get(name) for name in bone_names]
        if not all(bones):
            problems.append("bones %s not found on %s" % (list(bone_names), SOURCE_ARMATURE))
            continue
        arm_matrix = world @ bones[0].matrix
        locator.matrix_world.translation = pole_position(
            side,
            arm_matrix.translation,
            (world @ bones[1].matrix).translation,
            (world @ bones[2].matrix).translation,
            arm_matrix,
            DEFAULT_THRESHOLD,
        )
        # matrix_world rather than location: an unparented pole makes the two equal, and a template
        # that parents one would otherwise export a wrong pole without saying so.
    return problems
