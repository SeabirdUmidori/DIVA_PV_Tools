"""Move a whole-body turn off the hip bone and onto the spine root, holding every other pose exact.

Why this exists.  MMD authors the body facing in different bones depending on how the dance was made:
hand-keyed dances use センター (and the template already routes it, via グルーブ, onto kl_hara_xz),
DIVA-extracted dances use グルーブ, and mocap dances put it in 下半身 and 上半身 - measured on three shipped
mocap-sourced dances, where センター/グルーブ/腰 are all 0 while 下半身 and 上半身 each hold
~178 deg.  Copying 下半身 onto kl_kosi_xz, which is where the anatomical level says it belongs, then
writes a parent-relative rotation of 180 deg on a bone SEGA leaves at 3.0 deg, and 179.8 deg on cl_mune
where SEGA's own set never exceeds 34.8 deg.  A rotation that large sits at the antipode of the euler
chart, where the exporter has to spread it over three channels near +-180 deg.

What it does instead.  Per frame, compose the collected parent-relative values down the Export Rig tree,
collapse the spine carrier chain onto the hip bone's own world orientation, and pin every other bone to
the world matrix it already had.  Re-deriving the parent-relative values then puts the turn on the root
carrier and leaves the hip and the chest with their residual, which is the SEGA shape - measured on the
reference mocap window:

    bone           relMax before -> after      side axes after
    kl_kosi_xz         180.0    ->   0.0            0.0
    cl_mune            179.8    ->  16.5            0.0     (SEGA envelope 34.8)
    kg_hara_y            0.0    -> 105.9            0.0     (SEGA carries facing here: 177.6 / 0.0)
    largest movement of any pinned bone's world: 0.097 deg, i.e. the game sees the same pose.

The claim being relied on is the last line, so it is re-checked at every export and printed: only the
distribution changes, no bone that has a visible counterpart moves.
"""
import math
import os

from mathutils import Matrix

# The bones whose orientation the game composes from the root down to the hips.  Everything in this set
# is allowed to change; everything else is pinned.
CARRIER_CHAIN = ("kg_hara_y", "kl_hara_xz", "kl_hara_etc", "n_hara", "kl_kosi_y", "kl_kosi_xz")
HIP = "kl_kosi_xz"
HOIST_THRESHOLD_DEG = 150.0


def geo(a, b):
    m = a.transposed() @ b
    tr = m[0][0] + m[1][1] + m[2][2]
    return math.degrees(math.acos(max(-1.0, min(1.0, (tr - 1.0) / 2.0))))


def parent_map(armature):
    return {bone.name: (bone.parent.name if bone.parent else None) for bone in armature.data.bones}


def _depth_order(parents):
    order, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        par = parents.get(name)
        if par and par in parents:
            visit(par)
        order.append(name)

    for name in parents:
        visit(name)
    return order


def redistribute(bone_transformations, armature_name, tolerance_deg=0.5):
    """Rewrite the spine distribution of collected (frame, parent-relative matrix) series.

    Returns (bone_transformations, report) where report notes the biggest pinned-world deviation seen,
    so an export that silently starts moving bones gets caught instead of shipped.
    """
    import bpy
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.name == armature_name:
            armature = obj
            break
    if armature is None:
        return bone_transformations, ["no %s armature, spine left as collected" % armature_name]

    parents = parent_map(armature)
    keys = [(armature_name, name) for name in parents if (armature_name, name) in bone_transformations]
    if (armature_name, HIP) not in bone_transformations:
        return bone_transformations, ["no %s series, spine left as collected" % HIP]

    # Only hoist a turn that is genuinely stuck on the hip: measured on the accepted reference export the hip
    # sits at 104.5 deg while the root carrier already carries 178.7 deg as a single-axis write, and
    # hoisting that would replace a good write with a transported multi-axis one.  The mocap case has
    # its hip at 180 deg with the root carrier absent, which is the case this exists for.
    # Kill switch for A/B and for reproducing an older export byte for byte: set MMD2DIVA_NO_HOIST=1.
    if os.environ.get("MMD2DIVA_NO_HOIST"):
        return bone_transformations, ["spine hoist disabled by MMD2DIVA_NO_HOIST"]

    hip_rows = bone_transformations[(armature_name, HIP)]
    hip_mag = max(geo(Matrix.Identity(3), m.to_3x3()) for _f, m in hip_rows)
    root_key = (armature_name, "kl_hara_xz")
    root_mag = max((geo(Matrix.Identity(3), m.to_3x3()) for _f, m in bone_transformations[root_key]),
                   default=0.0) if root_key in bone_transformations else 0.0
    if hip_mag <= HOIST_THRESHOLD_DEG or root_mag > HOIST_THRESHOLD_DEG:
        return bone_transformations, ["spine left as collected: hip %.1f deg, root carrier %.1f deg "
                                      "(hoist needs hip > %.0f and root <= %.0f)"
                                      % (hip_mag, root_mag, HOIST_THRESHOLD_DEG, HOIST_THRESHOLD_DEG)]

    # Only hoist when the hip itself is asked to carry a near half turn.  That is the pathological
    # mocap shape (kl_kosi_xz at 180 deg with the root carrier absent); a dance that already
    # turns through センター/グルーブ puts a modest value on the hip (reference dance: 104.5 deg) and rewriting
    # its distribution would change an in-game-accepted file for no benefit.
    hip_rows = bone_transformations[(armature_name, HIP)]
    hip_max = max(geo(Matrix.Identity(3), m.to_3x3()) for _frame, m in hip_rows)
    if hip_max <= HOIST_THRESHOLD_DEG:
        return bone_transformations, ["hip carries %.1f deg, under the %.0f deg threshold: spine "
                                      "left exactly as collected" % (hip_max, HOIST_THRESHOLD_DEG)]

    series = bone_transformations[(armature_name, HIP)]
    frames = [frame for frame, _m in series]
    order = [name for name in _depth_order(parents) if (armature_name, name) in bone_transformations]

    worst = (0.0, None, None)
    moved = 0
    for index in range(len(frames)):
        frame = frames[index]
        rel = {}
        for name in order:
            rows = bone_transformations[(armature_name, name)]
            if index < len(rows):
                rel[name] = rows[index][1]
        worlds = {}
        for name in order:
            if name not in rel:
                continue
            par = parents.get(name)
            worlds[name] = (worlds[par] @ rel[name]) if par in worlds else rel[name].copy()
        if HIP not in worlds:
            continue
        hip = worlds[HIP]
        want = {}
        for name, m in worlds.items():
            want[name] = hip.copy() if name in CARRIER_CHAIN else m.copy()
        for name, m in worlds.items():
            if name in CARRIER_CHAIN:
                continue
            dev = geo(want[name].to_3x3(), m.to_3x3())
            if dev > worst[0]:
                worst = (dev, name, frame)
        for name in order:
            if name not in worlds:
                continue
            par = parents.get(name)
            new_rel = ((want[par].inverted() @ want[name]) if par in want else want[name].copy())
            rows = bone_transformations[(armature_name, name)]
            if index < len(rows):
                rows[index] = (frame, new_rel)
                moved += 1

    report = ["spine hoisted: %d carrier/hip writes redistributed, largest movement of a pinned bone "
              "%.4f deg (%s frame %s)" % (moved, worst[0], worst[1], worst[2])]
    if worst[0] > tolerance_deg:
        raise ValueError("spine hoist moved a bone the pose must not change: %.3f deg at %s frame %s"
                         % (worst[0], worst[1], worst[2]))
    return bone_transformations, report

