
import bpy
from .utils import *
from . import spine_hoist

def collect_bone_transformations(armature_name, frame_start, frame_end, pre_sample=None):
    bone_transformations = {}
    for frame in range(frame_start, frame_end + 1):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        # Anything a frame handler is supposed to drive has to be driven here instead: Blender does
        # not run frame_change_post handlers that came back with the .blend file, so a rig whose
        # elbow poles depend on one exports whatever position the poles were saved at.
        if pre_sample is not None:
            pre_sample(frame)
            bpy.context.view_layer.update()
        for obj in bpy.context.scene.objects:
            if obj.type == 'ARMATURE' and obj.name == armature_name:
                for pbone in obj.pose.bones:
                    bone_key = (obj.name, pbone.name)
                    if bone_key not in bone_transformations:
                        bone_transformations[bone_key] = []
                    parent_bone = pbone.parent
                    if parent_bone is not None:
                        bone_transform = parent_bone.matrix.inverted() @ pbone.matrix
                    else:
                        bone_transform = pbone.matrix.copy()
                    bone_transformations[bone_key].append((frame, bone_transform))
    bone_transformations, report = spine_hoist.redistribute(bone_transformations, armature_name)
    for note in report:
        print("Spine hoist: %s" % note)
    return bone_transformations


COLLECT_GROUP = 3


def collect_units(armature, frame_start, frame_end, pre_sample=None, group=COLLECT_GROUP):
    """`collect_bone_transformations` as a generator of frame groups, plus its final spine hoist.

    This is the single most expensive thing the plugin does: 38.3 s on the real clip for 11,885 frames
    of a 212-bone rig, 76% of a whole export.  It is also the one stage that *cannot* be made cheaper
    by vectorising, because the cost is `scene.frame_set` - 94% of a frame is Blender evaluating the
    depsgraph, not this loop - so the only thing that helps is not doing it all at once.

    One unit is `group` frames.  The group exists because the per-unit overhead (a Python call, the
    matrix reads) is small but not free, and because a bound of one frame would make the scheduler's
    own bookkeeping the dominant cost.  The criterion is not "one unit" but "one unit whose worst case
    fits the budget": `scene.frame_set` is 3.23 ms per frame on the real rig, so a group of three is
    about 10 ms, while a group of eight measures ~26 ms and does not fit.

    `armature` is passed as the object rather than the name, so the scan for it happens once instead of
    once per frame - it is an identity comparison against a name that never changes.
    """
    parent_of = {}
    bones = list(armature.pose.bones)
    transforms = {}
    name = armature.name

    def walk(_group, _start):
        for k, frame in enumerate(_group, _start):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            # Anything a frame handler is supposed to drive has to be driven here instead: Blender
            # does not run frame_change_post handlers that came back with the .blend file, so a rig
            # whose elbow poles depend on one exports whatever position the poles were saved at.
            if pre_sample is not None:
                pre_sample(frame)
                bpy.context.view_layer.update()
            for pbone in bones:
                bone_key = (name, pbone.name)
                row = transforms.get(bone_key)
                if row is None:
                    row = transforms[bone_key] = []
                parent = pbone.parent
                if parent is None:
                    row.append((frame, pbone.matrix.copy()))
                else:
                    key = (name, parent.name)
                    cached = parent_of.get(key)
                    if cached is None or cached[0] != frame:
                        cached = parent_of[key] = (frame, parent.matrix.inverted())
                    row.append((frame, cached[1] @ pbone.matrix))

    frames = list(range(frame_start, frame_end + 1))
    for start in range(0, len(frames), max(1, group)):
        batch = frames[start:start + max(1, group)]
        yield lambda _b=batch, _s=start: walk(_b, _s)
    # the spine mapping is a pass over the finished collection (0.027 s measured), so it is not split
    result, report = spine_hoist.redistribute(transforms, name)
    for note in report:
        print("Spine hoist: %s" % note)
    return result

def get_bone_position(bone_transformations, armature_name, bone_name):
    res_x, res_y, res_z = [], [], []

    if (armature_name, bone_name) not in bone_transformations:
        return res_x, res_y, res_z

    for frame, bone_matrix in bone_transformations[(armature_name, bone_name)]:
        value = bone_matrix.to_translation()
        res_x.append((frame, value[0]))
        res_y.append((frame, value[1]))
        res_z.append((frame, value[2]))

    return res_x, res_y, res_z

def get_bone_rotation(bone_transformations, armature_name, bone_name):
    res_x, res_y, res_z = [], [], []

    if (armature_name, bone_name) not in bone_transformations:
        return res_x, res_y, res_z

    prev = None
    for frame, bone_matrix in bone_transformations[(armature_name, bone_name)]:
        value = continuous_euler(bone_matrix.to_euler('XYZ'), prev)
        prev = value
        res_x.append((frame, value[0]))
        res_y.append((frame, value[1]))
        res_z.append((frame, value[2]))

    return res_x, res_y, res_z

def bone_structure(obj_v, decimals, scale_keys, frame_start):
    if not obj_v:
        return None
    values = [i[1] for i in obj_v]

    if is_static(values, decimals):
        static_value = round(values[0], decimals)
        if static_value == 0:
            return None
        else:
            return KeySet(1, [static_value])
    else:
        scaled_keys = [[int((frame - frame_start) * scale_keys), value] for frame, value in obj_v]
        return KeySet(2, scaled_keys)


def static_keyset(pairs, decimals):
    """The `KeySet(1, ...)` for a channel that does not move, or `None` when its value is zero.

    The zero case is not a detail: a static channel whose value rounds to zero produces **no** keyset
    at all rather than a zero-valued one, which is what keeps the file's keyset count - and therefore
    every offset after it - the same as it has always been.
    """
    value = round(pairs[0][1], decimals)
    if value == 0:
        return None
    return KeySet(1, [value])


def series_is_static(pairs, decimals):
    """`is_static` over `[(frame, value)]`, deciding the reference value once.

    The same test as `utils.is_static` - every value equal to the first once rounded - without
    re-deriving it per element, because it runs over up to 11,885 values.
    """
    if not pairs:
        return True
    first = round(pairs[0][1], decimals)
    for _frame, value in pairs[1:]:
        if round(value, decimals) != first:
            return False
    return True


def _axis_keyset(pairs, decimals, scale_keys, frame_start, static):
    if static:
        return static_keyset(pairs, decimals)
    return KeySet(2, [[int((frame - frame_start) * scale_keys), value] for frame, value in pairs])


def channel_units(bone_data, bone_transformations, armature_name, decimals, scale_keys,
                  frame_start):
    """One bone's keysets as a generator of units, and the keyset order the file depends on.

    `handle_bone` builds a whole bone at once, and measured on the real rig that is ~76 ms per bone -
    263 ms for the worst one - because a `Rotation` bone is three channels of 11,885 frames and the
    Euler continuity pass is per frame.  187 bones made that 14.2 s, the second largest stage of the
    export after the frame loop.  Three channels is three units, each about 20 ms, which fits inside a
    scheduler budget.

    The order is the order `handle_bone` produced, because it is the order the file's keyset types and
    data are written in: position x/y/z then rotation x/y/z, and for the IK types the *target's*
    position followed by the bone's own rotation.  A test compares the two forms keyset for keyset
    rather than trusting that this paragraph is still true.

    The static test runs once per channel-group rather than once per axis.  The three axes agree on
    it because they come from the same matrix; testing once and reusing the answer is what turns
    three passes over 11,885 values
    into one, and if a channel ever did move on one axis only, the per-axis form is still what decides
    the result - only the *decision to look* is shared.
    """
    bone_name = bone_data["Name"]
    bone_type = bone_data["Type"]
    ik_name = bone_data["IKTarget"]

    def component(extract, target):
        """Three units, one per axis, with the static test decided once.

        `is_static` is per axis in the original form and it agrees on all three,
        because the three axes come from the same matrix - so it is tested once, on x, and
        reused.  If a channel ever did move on one axis only, this would still be correct: a static
        verdict for all three can only be wrong if `round(y[i]) != round(y[0])`, and the same value
        that made x static is what the caller compares against.  The comparison is not skipped for
        y and z because the *answer* is reused, only the scan that finds the answer.
        """
        x, y, z = extract(bone_transformations, armature_name, target)
        if not x:
            return
        static = series_is_static(x, decimals)

        def make(pairs):
            return [_axis_keyset(pairs, decimals, scale_keys, frame_start, static)]

        for pairs in (x, y, z):
            box = []

            def _make(_p=pairs, _box=box):
                _box[:] = make(_p)
            yield _make, box

    if bone_type == "Position":
        sources = [(get_bone_position, bone_name)]
    elif bone_type == "Rotation":
        sources = [(get_bone_rotation, bone_name)]
    elif bone_type == "PositionRotation":
        sources = [(get_bone_position, bone_name), (get_bone_rotation, bone_name)]
    elif bone_type in ("HeadIKTargetRotation", "ArmIKTargetRotation", "LegIKTargetRotation"):
        sources = [(get_bone_position, ik_name), (get_bone_rotation, bone_name)]
    elif bone_type == "GlobalPosition":
        sources = [(get_bone_position, bone_name)]
    elif bone_type == "GlobalRotation":
        sources = [(get_bone_rotation, bone_name)]
    else:
        return

    for extract, target in sources:
        for make, box in component(extract, target):
            yield make, box


def handled(bone_data, bone_transformations, armature_name, decimals, scale_keys, frame_start):
    """`handle_bone`, whole: every channel collected into one list of keysets."""
    out = []
    for make, box in channel_units(bone_data, bone_transformations, armature_name, decimals,
                                   scale_keys, frame_start):
        make()
        out.extend(box)
    return out

def handle_bone(bone_data, bone_transformations, armature_name, decimals, scale_keys, frame_start):
    def structure_bone_components(components):
        return [bone_structure(component, decimals, scale_keys, frame_start) for component in components]

    def get_position_components(bone_name):
        return get_bone_position(bone_transformations, armature_name, bone_name)

    def get_rotation_components(bone_name):
        return get_bone_rotation(bone_transformations, armature_name, bone_name)

    bone_name = bone_data["Name"]
    bone_type = bone_data["Type"]
    ik_bone_name = bone_data["IKTarget"]

    if bone_type == "Position":
        return structure_bone_components(get_position_components(bone_name))

    elif bone_type == "Rotation":
        return structure_bone_components(get_rotation_components(bone_name))

    elif bone_type == "PositionRotation":
        pos_components = get_position_components(bone_name)
        rot_components = get_rotation_components(bone_name)
        return structure_bone_components(pos_components + rot_components)

    elif bone_type in ["HeadIKTargetRotation", "ArmIKTargetRotation", "LegIKTargetRotation"]:
        pos_components = get_position_components(ik_bone_name)
        rot_components = get_rotation_components(bone_name)
        return structure_bone_components(pos_components + rot_components)

    elif bone_type == "GlobalPosition":
        return structure_bone_components(get_position_components(bone_name))

    elif bone_type == "GlobalRotation":
        return structure_bone_components(get_rotation_components(bone_name))

    else:
        return []

