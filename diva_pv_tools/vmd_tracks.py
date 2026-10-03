"""Vectorised conversion of VMD bone records into Blender keyframe values.

This is the half of the import that carries mmd_tools' numeric conventions, factored out of
`task_ops.VmdImportOperation` so it can be tested without a task, a timer or an operator.  Nothing
here creates or reads a datablock: it takes arrays in and gives arrays out, which is what makes it
possible to check the conventions directly instead of inferring them from whether a dance looks right.

**The transform convention is borrowed, not derived.**  `BoneConverter` in the installed mmd_tools
extension knows it: the armature-space rest matrix with rows 1 and 2 swapped (MMD is Y-up, Blender is
Z-up), transposed, applied as `q_mat * q * q_mat⁻¹` for a rotation and as `mat * v * scale` for a
translation.  A second implementation of that, written from a description, is a second chance to get
it subtly wrong - so the converter itself is imported and only the *loop* around it is ours.  When
mmd_tools is absent this module raises rather than guessing: a wrong convention moves the whole dance
instead of failing.

**Sign compatibility is not optional.**  A quaternion and its negation are the same rotation, so a VMD
may store either.  Blender interpolates the stored numbers, so two consecutive keys that describe a
small turn but have opposite signs sweep the long way round, which reads as a limb flicking through
the body whenever the source crosses 180 degrees.  mmd_tools' rule is kept exactly: choose the sign
closer to the previous *converted* rotation, continuing the running choice along the track.

**The handles follow mmd_tools' `__setInterpolation`.**  VMD stores four bezier control values per
channel in 0..127 at a fixed stride.  mmd_tools does not parse them for bones - it passes zeros - so
what the curves actually get is its `[20, 20, 107, 107]` fallback, which is also what it substitutes
whenever a key's value barely moves.  Both branches are reproduced here, and the guard matters: a flat
key with a handle derived from a near-zero delta is an enormous overshoot rather than an ease.
"""
import numpy as np

try:                                        # Blender 4.2+ extension layout
    from bl_ext.blender_org.mmd_tools.core.vmd.importer import BoneConverter
except ImportError:                         # legacy add-on layout
    try:
        from mmd_tools.core.vmd.importer import BoneConverter
    except ImportError:
        BoneConverter = None

FALLBACK_BEZIER = (20, 20, 107, 107)


class ConverterMissing(RuntimeError):
    """Raised instead of inventing a transform convention mmd_tools already defines."""


# ---------------------------------------------------------------------------- the converter
def converter_for(pose_bone, scale):
    if BoneConverter is None:
        raise ConverterMissing(
            "mmd_tools is not installed, and its bone transform convention is what maps an MMD "
            "rotation onto this rig's basis; importing without it would place the dance wrongly")
    return BoneConverter(pose_bone, scale)


def _mangled(converter, *names):
    for name in names:
        value = getattr(converter, name, None)
        if value is not None:
            return value
    raise ConverterMissing("the installed mmd_tools bone converter no longer exposes %s; "
                           "diva_pv_tools.vmd_tracks needs updating for it" % (names[0],))


def converter_matrix(converter):
    """The converter's fixed 3x3 matrix, read through the name-mangled attribute it really has."""
    return _mangled(converter, "_BoneConverter__mat", "_BoneConverterPoseMode__mat")


def converter_quaternion(converter):
    return converter_matrix(converter).to_quaternion()[:]


def converter_scale(converter):
    return float(_mangled(converter, "_BoneConverter__scale", "_BoneConverterPoseMode__scale"))


# ---------------------------------------------------------------------------- the conversion
def convert_locations(converter, loc):
    """`(N, 3)` VMD positions -> `(N, 3)` Blender basis translations, in one pass.

    `BoneConverter.convert_location` is `(mat @ Vector(v)) * scale` and `mat` is fixed for the bone,
    so the whole track is one matrix product.  Per key it costs a `Vector` construction and a matrix
    multiply each - measurable at 272k records, and free as an array operation.
    """
    mat = np.asarray(converter_matrix(converter), dtype=np.float64)
    return (np.asarray(loc, dtype=np.float64) @ mat.T) * converter_scale(converter)


def convert_rotations(converter, rot):
    """`(N, 4)` VMD quaternions (x, y, z, w) -> `(N, 4)` Blender quaternions (w, x, y, z).

    The sandwich `q_mat * q * q_mat⁻¹`, with `q_mat⁻¹ = conjugate(q_mat)` because it is a rotation.
    The result is normalised because mmd_tools normalises it, and because a source quaternion that is
    1.9% off unit - which the reader accepts - would otherwise scale the pose.
    """
    q = np.asarray(rot, dtype=np.float64)
    qm = converter_quaternion(converter)
    w2, x2, y2, z2 = qm
    # (x, y, z, w) -> (w, x, y, z)
    w1, x1, y1, z1 = q[:, 3].copy(), q[:, 0].copy(), q[:, 1].copy(), q[:, 2].copy()
    norm = np.sqrt(w1 * w1 + x1 * x1 + y1 * y1 + z1 * z1)
    norm[norm == 0.0] = 1.0
    w1, x1, y1, z1 = w1 / norm, x1 / norm, y1 / norm, z1 / norm
    # qm * q
    aw = w2 * w1 - x2 * x1 - y2 * y1 - z2 * z1
    ax = w2 * x1 + x2 * w1 + y2 * z1 - z2 * y1
    ay = w2 * y1 - x2 * z1 + y2 * w1 + z2 * x1
    az = w2 * z1 + x2 * y1 - y2 * x1 + z2 * w1
    # (qm * q) * conjugate(qm)
    out = np.empty((len(q), 4), dtype=np.float64)
    out[:, 0] = aw * w2 + ax * x2 + ay * y2 + az * z2
    out[:, 1] = -aw * x2 + ax * w2 - ay * z2 + az * y2
    out[:, 2] = -aw * y2 + ax * z2 + ay * w2 - az * x2
    out[:, 3] = -aw * z2 - ax * y2 + ay * x2 + az * w2
    norm = np.sqrt(out[:, 0] ** 2 + out[:, 1] ** 2 + out[:, 2] ** 2 + out[:, 3] ** 2)
    norm[norm == 0.0] = 1.0
    out /= norm[:, None]
    return out


def make_compatible(quats):
    """Make consecutive quaternions sign-compatible in place, exactly as mmd_tools does.

    The comparison is against the previous *stored* value, not the raw source, which is what makes the
    choice a running one: a track that crosses 180 degrees twice is flipped twice and ends where it
    started.  `t1` is the squared distance to `+q` and `t2` to `-q`; the smaller wins.
    """
    if len(quats) < 2:
        return quats
    for i in range(1, len(quats)):
        prev = quats[i - 1]
        curr = quats[i]
        t1 = ((prev[0] - curr[0]) ** 2 + (prev[1] - curr[1]) ** 2
              + (prev[2] - curr[2]) ** 2 + (prev[3] - curr[3]) ** 2)
        t2 = ((prev[0] + curr[0]) ** 2 + (prev[1] + curr[1]) ** 2
              + (prev[2] + curr[2]) ** 2 + (prev[3] + curr[3]) ** 2)
        if t2 < t1:
            quats[i] = -curr
    return quats


def bezier_for(converter, interpolation, index):
    """The four control values for one curve index, or mmd_tools' fallback."""
    if interpolation is None:
        return FALLBACK_BEZIER
    try:
        picked = list(converter.convert_interpolation(interpolation))
        if not picked:
            return FALLBACK_BEZIER
        return tuple(int(picked[index][k]) for k in range(4))
    except (TypeError, IndexError, ValueError, AttributeError):
        return FALLBACK_BEZIER


def _enum_code(prop, name):
    """The integer Blender stores for an enum name, read from the property itself.

    `foreach_set` wants the integer, and these integers are not in declaration order - BEZIER is 2
    while SINE is 12, FREE is 0 while AUTO_CLAMPED is 4 - so hard-coding them would be a silent wrong
    answer.  The RNA metadata is the definition, so it is what this reads.
    """
    for item in prop.enum_items:
        if item.identifier == name:
            return item.value
    raise ValueError("%s has no %r" % (prop.identifier, name))


def keyframe_enums(any_curve):
    """`(interpolation_code, handle_type_code)` for BEZIER / FREE, from the collection's own RNA.

    The property lives on the *Keyframe* struct, not on the collection, so it is read from an element;
    `keyframe_points.bl_rna` has no such property and asking it raises a KeyError.  The caller
    computes this once for the whole import rather than once per curve, because it
    is the same two integers every time.
    """
    points = any_curve.keyframe_points
    points.add(1)
    element = points[len(points) - 1]
    props = element.bl_rna.properties
    codes = (_enum_code(props["interpolation"], "BEZIER"),
             _enum_code(props["handle_left_type"], "FREE"))
    points.remove(points[len(points) - 1])
    any_curve.update()
    return codes


def fill_curve(fcurve, frames, values, bezier=FALLBACK_BEZIER, codes=None):
    """Write one whole curve: keys, handles, interpolation - vectorised end to end.

    This exists because in the straight per-key loop every `kp.co`, `kp.handle_right` and
    `kp.interpolation` access is an RNA round trip, and there are seven of them per key.  `foreach_set`
    does the same work in one call per array, which is what makes one bone a viable scheduler unit
    instead of a 70 ms stall.

    The handle arithmetic is mmd_tools' `__setInterpolation`, with its fallback applied where it
    applies: a key whose value barely moves, or a control tuple that is already linear, gets
    `[20, 20, 107, 107]` rather than a handle derived from a near-zero delta.  `dy` is the real step
    between two finished keys, which is why this runs after every `co` is written.
    """
    points = fcurve.keyframe_points
    count = len(frames)
    if count == 0:
        return
    if len(points) != count:
        raise ValueError("curve has %d keyframe(s) but %d value(s) were given"
                         % (len(points), count))
    co = np.empty(2 * count, dtype=np.float64)
    co[0::2] = frames
    co[1::2] = values
    b = tuple(bezier)
    if b[0] == b[1] and b[2] == b[3]:
        b = FALLBACK_BEZIER
    fb = FALLBACK_BEZIER
    dx = co[2::2] - co[0:-2:2]
    dy = co[3::2] - co[1:-2:2]
    use_fallback = np.abs(dy) < 1e-4
    c0 = np.where(use_fallback, fb[0], b[0])
    c1 = np.where(use_fallback, fb[1], b[1])
    c2 = np.where(use_fallback, fb[2], b[2])
    c3 = np.where(use_fallback, fb[3], b[3])
    right = np.empty(2 * (count - 1), dtype=np.float64)
    left = np.empty(2 * (count - 1), dtype=np.float64)
    right[0::2] = co[0:-2:2] + dx * c0 / 127.0
    right[1::2] = co[1:-2:2] + dy * c1 / 127.0
    left[0::2] = co[0:-2:2] + dx * c2 / 127.0
    left[1::2] = co[1:-2:2] + dy * c3 / 127.0
    # `handle_left` / `handle_right` exist on every key, but the interior loop only produces one per
    # *pair*, so the arrays are indexed by the key they belong to: `handle_right` fills 0..n-2 and
    # leaves the last (which has none), `handle_left` fills 1..n-1 and leaves the first.  The two
    # unused ends are set to the key's own position below, which is the only value that cannot make
    # the curve move - `fix_end_handles` then points them outward, as mmd_tools does.
    right_full = np.empty(2 * count, dtype=np.float64)
    left_full = np.empty(2 * count, dtype=np.float64)
    right_full[0:2 * (count - 1)] = right
    left_full[2:] = left
    right_full[-2:] = co[-2:]
    left_full[0:2] = co[0:2]
    bezier_code, free_code = codes if codes is not None else keyframe_enums(fcurve)
    points.foreach_set("co", co)
    points.foreach_set("interpolation", np.full(count, bezier_code, dtype=np.int32))
    points.foreach_set("handle_left_type", np.full(count, free_code, dtype=np.int32))
    points.foreach_set("handle_right_type", np.full(count, free_code, dtype=np.int32))
    points.foreach_set("handle_left", left_full)
    points.foreach_set("handle_right", right_full)
    fcurve.update()
    fix_end_handles(fcurve)


def set_interpolation(fcurves, bezier):
    """Set every handle the way `__setInterpolation` does, from `co` and the value delta.

    The per-key form, kept because it is the readable statement of the rule and because `fill_curve`
    has to agree with it.

    The order matters: `dy` is read *after* both endpoints have their final `co`, so it is the real
    step the curve makes, and the fallback bezier replaces a control tuple that would divide by it.
    Multiplying before dividing is mmd_tools' own note - the controls are integers in 0..127 and the
    other order loses precision on exactly the long curves where it shows.
    """
    b = tuple(bezier)
    for fc in fcurves:
        points = fc.keyframe_points
        for i in range(len(points) - 1):
            kp0 = points[i]
            kp1 = points[i + 1]
            kp0.interpolation = "BEZIER"
            kp0.handle_right_type = "FREE"
            kp1.handle_left_type = "FREE"
            dx = kp1.co[0] - kp0.co[0]
            dy = kp1.co[1] - kp0.co[1]
            use = FALLBACK_BEZIER if (abs(dy) < 1e-4 or (b[0] == b[1] and b[2] == b[3])) else b
            kp0.handle_right = (kp0.co[0] + dx * use[0] / 127.0, kp0.co[1] + dy * use[1] / 127.0)
            kp1.handle_left = (kp0.co[0] + dx * use[2] / 127.0, kp0.co[1] + dy * use[3] / 127.0)
        # the last key owns no right handle, but it still has to carry the interpolation mode: a key
        # left at the default CONSTANT would hold its value instead of easing out of the one before it
        if len(points):
            points[-1].interpolation = "BEZIER"


def fix_end_handles(fcurve, span=1.0):
    """Flat handles at both ends, following `__fixFcurveHandles`.

    Without this the first key's right handle is whatever the bezier maths produced and the curve
    overshoots before its own first key - a visible pop at frame 0.
    """
    points = fcurve.keyframe_points
    if len(points) == 0:
        return
    first = points[0]
    first.handle_left_type = "FREE"
    first.handle_left = (first.co[0] - span, first.co[1])
    last = points[-1]
    last.handle_right_type = "FREE"
    last.handle_right = (last.co[0] + span, last.co[1])
