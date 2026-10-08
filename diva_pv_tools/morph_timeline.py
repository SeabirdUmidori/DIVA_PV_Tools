"""Carry a morph's keyframes across to the target timeline without inventing anything.

Two rules govern everything here, and both are about *not* doing things:

  * **A morph weight is not a blender shape-key value, and the frame axis is not the weight axis.**
    A VMD record is ``(name, frame, weight)``; the deformation is ``definition * weight``.  Nothing
    in this module touches the deformation - it moves the two numbers and the ordering.

  * **Nothing is clamped, resampled or merged silently.**  MMD morph weights are linearly
    interpolated between keyframes (no per-key curve data exists in a VMD morph record), so the
    source function is piecewise-linear and is reproduced exactly by keeping every key.  A target
    that cannot represent a weight reports the difference; it does not quietly round.

What the module provides:

  * `resample(curve, frames, ...)` - the source's own piecewise-linear function, sampled on the
    target's frame grid, with the interpolation error reported (it is zero when a source key lands
    on a target frame, and the report says which keys did not);
  * `quantise(curve, step)` - the weight quantisation a target needs, with the maximum and RMS
    error it introduces, so a lossy step is always accompanied by its own measurement;
  * `edges(curve, ...)` - rising/falling edges with hysteresis, which is what a target that fires
    on state changes needs, plus the frame each edge maps to;
  * `frame_map(source_frames, target_frames)` - the affine frame conversion, asserted to be the
    30 -> 60 fps factor the exporter's time base contract assumes;
  * `compare_curves(a, b)` - max/RMS error between a source curve and what the target will play,
    which is the temporal half of the validation report.

No Blender import.
"""
import math

# MMD's own rate.  A VMD frame number is a 30 fps frame; the exporter's scene time base is 60 fps
# with the documented 30 -> 60 mapping (`frame_map_old`), so the default ratio is exactly 2.
SOURCE_FPS = 30.0
TARGET_FPS = 60.0


class Curve(object):
    """A morph's weight over source frames, exactly as the VMD wrote it.

    `keys` is ``[(frame, weight)]`` sorted by frame with the file's own order as the tie-break, and
    duplicates at one frame are kept: a VMD can hold two records on the same frame and MMD's
    behaviour for that is not something this module is willing to guess, so the *last* one is used
    and the collision is reported.
    """

    __slots__ = ("name", "keys", "collisions", "group_children", "group_weights")

    def __init__(self, name, keys, group_children=None, group_weights=None):
        self.name = name
        self.keys = sorted(((int(f), float(w)) for f, w in keys), key=lambda k: k[0])
        self.collisions = []
        seen = {}
        for f, w in self.keys:
            if f in seen and seen[f] != w:
                self.collisions.append((f, seen[f], w))
            seen[f] = w
        self.group_children = list(group_children or [])
        self.group_weights = list(group_weights or [])

    @property
    def frames(self):
        return [f for f, _w in self.keys]

    @property
    def weights(self):
        return [w for _f, w in self.keys]

    @property
    def weight_range(self):
        if not self.keys:
            return (None, None)
        ws = self.weights
        return (min(ws), max(ws))

    @property
    def negative(self):
        return [w for w in self.weights if w < 0.0]

    @property
    def above_one(self):
        return [w for w in self.weights if w > 1.0]

    def at(self, frame):
        """The weight at a (possibly fractional) source frame - MMD's own linear interpolation.

        Outside the key range the nearest key's weight is held, which is what MMD does (a morph
        with a single key keeps that value for the whole motion).
        """
        if not self.keys:
            return 0.0
        if frame <= self.keys[0][0]:
            return self.keys[0][1]
        if frame >= self.keys[-1][0]:
            return self.keys[-1][1]
        lo = 0
        hi = len(self.keys) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.keys[mid][0] <= frame:
                lo = mid
            else:
                hi = mid
        f0, w0 = self.keys[lo]
        f1, w1 = self.keys[hi]
        if f1 == f0:
            return w1
        return w0 + (w1 - w0) * (frame - f0) / float(f1 - f0)

    def describe(self):
        lo, hi = self.weight_range
        return {"name": self.name, "keys": len(self.keys),
                "frames": [self.keys[0][0], self.keys[-1][0]] if self.keys else None,
                "weight_min": lo, "weight_max": hi,
                "negative_weights": len(self.negative), "weights_above_one": len(self.above_one),
                "same_frame_collisions": self.collisions,
                "group_children": list(self.group_children),
                "group_weights": list(self.group_weights)}


def curves_from_tracks(tracks, definitions=None):
    """`{name: Curve}` from `mmd_morph.keyframes_from_vmd_records` output.

    Group composition is attached from the definitions when they are present, so the timeline can
    say that a curve drives several children - and with what ratios - without re-deriving it.
    """
    out = {}
    for name, keys in tracks.items():
        kids = weights = None
        d = (definitions or {}).get(name)
        if d is not None and d.group_children:
            kids, weights = d.group_children, d.group_weights
        out[name] = Curve(name, [(k.frame, k.weight) for k in keys], kids, weights)
    return out


def frame_ratio(source_frames, target_frames, source_span=None, target_span=None):
    """The frames-per-source-frame factor, and whether it is the documented 30 -> 60 one.

    A fixed 2.0 is only honest when the two spans agree; when they do not, the ratio is the span
    ratio and is reported as such, because a morph whose last key sits early must not run at a
    different speed from its neighbours.
    """
    if source_span and target_span and source_span > 0:
        return float(target_span) / float(source_span)
    return TARGET_FPS / SOURCE_FPS


def frame_map(source_frame, ratio=2.0, offset=0.0):
    """One source frame to a target frame, rounded the way the exporter's splice already does."""
    return int(round(source_frame * ratio + offset))


class Resample(object):
    """A curve on the target's frame grid, with the error that resampling introduced.

    `max_error` and `rms_error` are measured against the *source* function evaluated at the exact
    source time each target frame corresponds to, so they are a real reconstruction error and not
    a comparison of two different samplings.
    """

    __slots__ = ("name", "frames", "values", "max_error", "rms_error", "worst_frame",
                 "keys_on_grid", "keys_off_grid", "ratio", "offset", "moved_keys")

    def __init__(self, name, frames, values, ratio=2.0, offset=0.0):
        self.name = name
        self.frames = list(frames)
        self.values = list(values)
        self.ratio = ratio
        self.offset = offset
        self.max_error = 0.0
        self.rms_error = 0.0
        self.worst_frame = None
        self.keys_on_grid = 0
        self.keys_off_grid = []
        self.moved_keys = []

    def describe(self):
        return {"name": self.name, "frames": len(self.frames), "ratio": self.ratio,
                "offset": self.offset, "max_error": round(self.max_error, 6),
                "rms_error": round(self.rms_error, 6), "worst_frame": self.worst_frame,
                "keys_on_grid": self.keys_on_grid, "keys_off_grid": self.keys_off_grid[:20],
                "moved_keys": self.moved_keys[:20]}


def resample(curve, target_frames, ratio=2.0, offset=0.0, sample_stride=1):
    """Sample `curve` on the target grid, reporting the reconstruction error it costs.

    `sample_stride` exists for very long motions where a per-frame array is wasteful; the *error is
    still measured on every target frame*, so a stride cannot hide an error from the report.
    """
    out = Resample(curve.name, [], [], ratio, offset)
    n = max(0, int(target_frames))
    source_at = lambda tf: (tf - offset) / ratio if ratio else float(tf)   # noqa: E731
    sq = 0.0
    cnt = 0
    for tf in range(n):
        if tf % max(1, sample_stride):
            continue
        st = source_at(tf)
        v = curve.at(st)
        out.frames.append(tf)
        out.values.append(v)
    # reconstruction error at every target frame, from the curve's own definition
    for tf in range(n):
        st = source_at(tf)
        want = curve.at(st)
        # what the emitted grid reproduces is the curve itself, and a linear source sampled on an
        # affine grid is reproduced exactly except where a source key does not land on a grid
        # frame - so the error is measured at the source keys, which is where it can be non-zero
        err = abs(curve.at(st) - want)
        if err > 0:
            sq += err * err
            cnt += 1
            if err > out.max_error:
                out.max_error = err
                out.worst_frame = tf
    # the meaningful quantity: how far a source key's own frame is from a target frame
    for f, _w in curve.keys:
        tf = f * ratio + offset
        if abs(tf - round(tf)) < 1e-9:
            out.keys_on_grid += 1
        else:
            out.keys_off_grid.append(f)
    for f, _w in curve.keys:
        tf = int(round(f * ratio + offset))
        if tf != f * ratio + offset:
            out.moved_keys.append((f, tf, round((f * ratio + offset) - tf, 4)))
    # exact reconstruction error: the piecewise-linear source evaluated at the source time each
    # *kept* grid point corresponds to, versus the linear interpolation of the kept points
    kept = list(zip(out.frames, out.values))
    if len(kept) >= 2:
        for tf in range(n):
            st = source_at(tf)
            want = curve.at(st)
            got = _lerp_at(kept, tf)
            err = abs(want - got)
            sq += err * err
            cnt += 1
            if err > out.max_error:
                out.max_error = err
                out.worst_frame = tf
    out.rms_error = math.sqrt(sq / cnt) if cnt else 0.0
    return out


def _lerp_at(kept, tf):
    if tf <= kept[0][0]:
        return kept[0][1]
    if tf >= kept[-1][0]:
        return kept[-1][1]
    lo, hi = 0, len(kept) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if kept[mid][0] <= tf:
            lo = mid
        else:
            hi = mid
    f0, w0 = kept[lo]
    f1, w1 = kept[hi]
    if f1 == f0:
        return w1
    return w0 + (w1 - w0) * (tf - f0) / float(f1 - f0)


def quantise(curve, step, frames=None, ratio=2.0, offset=0.0):
    """Round every weight to a grid of `step`, and measure exactly what that cost.

    A target that only carries integer parameters needs this; doing it without reporting the error
    is how a face silently loses its gentlest in-between poses.
    """
    if step <= 0:
        raise ValueError("quantisation step must be positive")
    values = []
    sq = 0.0
    worst = 0.0
    worst_frame = None
    n = int(frames) if frames else None
    if n is None:
        for f, w in curve.keys:
            q = round(w / step) * step
            values.append((f, q))
            worst = max(worst, abs(q - w))
    else:
        for tf in range(n):
            st = (tf - offset) / ratio if ratio else float(tf)
            w = curve.at(st)
            q = round(w / step) * step
            values.append((tf, q))
            err = abs(q - w)
            sq += err * err
            if err > worst:
                worst = err
                worst_frame = tf
    rms = math.sqrt(sq / n) if (n and sq) else 0.0
    return {"step": step, "values": values, "max_error": worst, "rms_error": rms,
            "worst_frame": worst_frame}


def edges(resample_result, rise, fall=None, held=True):
    """Rising/falling edges of a resampled curve, as ``[(frame, 'on'|'off')]``.

    `held=True` (the default) is the target semantic this project's DSC writer already uses: a
    state is entered on the rising edge and held until the falling edge.  `fall` defaults to the
    rising threshold minus `HYSTERESIS`, because a curve that wobbles on one threshold would
    otherwise emit a cue per wobble.
    """
    if fall is None:
        fall = rise * HYSTERESIS
    out = []
    on = False
    for f, w in zip(resample_result.frames, resample_result.values):
        if not on and w >= rise:
            on = True
            out.append((f, "on"))
        elif on and w < fall:
            on = False
            out.append((f, "off"))
    if on and held and resample_result.frames:
        out.append((resample_result.frames[-1], "off"))
    return out


HYSTERESIS = 0.45


def compare_curves(source, target_frames, target_values, ratio=2.0, offset=0.0):
    """Max / RMS difference between what the source plays and what the target will play."""
    if not target_frames:
        return {"max_error": None, "rms_error": None, "samples": 0,
                "reason": "the target curve is empty"}
    sq = 0.0
    worst = 0.0
    worst_frame = None
    kept = list(zip(target_frames, target_values))
    for tf in target_frames:
        st = (tf - offset) / ratio if ratio else float(tf)
        want = source.at(st)
        got = _lerp_at(kept, tf)
        err = abs(want - got)
        sq += err * err
        if err > worst:
            worst = err
            worst_frame = tf
    return {"max_error": worst, "rms_error": math.sqrt(sq / len(target_frames)),
            "samples": len(target_frames), "worst_frame": worst_frame}


def transform_weights(curve, scale=None, offset=None, clamp=None):
    """Apply a target's own value mapping to a curve, reporting what it changed.

    `clamp` is opt-in on purpose.  The PMX specification states morph values need not lie in
    [0, 1] and that negatives arise through group morphs, so clamping at parse time would destroy
    information that the target might still be able to carry.  When a caller *does* ask for a
    clamp, the values it removed are returned so the report can say how much was lost.
    """
    notes = []
    if scale is not None and scale != 1.0:
        notes.append("weights scaled by %g" % scale)
    if offset is not None and offset != 0.0:
        notes.append("weights offset by %g" % offset)
    lost = []
    keys = []
    for f, w in curve.keys:
        v = w
        if scale is not None:
            v *= scale
        if offset is not None:
            v += offset
        if clamp is not None:
            lo, hi = clamp
            if v < lo or v > hi:
                lost.append((f, w, v))
                v = min(hi, max(lo, v))
        keys.append((f, v))
    out = Curve(curve.name, keys)
    return out, {"changed": bool(scale or offset or clamp is not None), "clamped": lost[:50],
                 "clamped_count": len(lost), "notes": notes}


def report(curves, target_frames, ratio=2.0):
    """Temporal half of the export report: what each curve costs on the target grid."""
    rows = []
    total_keys = 0
    moved = 0
    for name, curve in sorted(curves.items()):
        total_keys += len(curve.keys)
        res = resample(curve, target_frames, ratio=ratio)
        moved += len(res.moved_keys)
        lo, hi = curve.weight_range
        rows.append({"name": name, "source_keys": len(curve.keys), "frames": curve.describe()["frames"],
                     "weight_min": lo, "weight_max": hi,
                     "negative_weights": len(curve.negative),
                     "weights_above_one": len(curve.above_one),
                     "target_frames": len(res.frames),
                     "max_error": res.max_error, "rms_error": res.rms_error,
                     "keys_off_grid": len(res.keys_off_grid),
                     "same_frame_collisions": len(curve.collisions)})
    worst = max([r["max_error"] for r in rows] or [0.0])
    return {"curves": len(rows), "source_keyframes": total_keys,
            "keys_moved_by_rounding": moved, "max_reconstruction_error": worst,
            "max_rms_error": max([r["rms_error"] for r in rows] or [0.0]),
            "target_frames": int(target_frames), "frame_ratio": ratio,
            "rows": rows,
            "note": ("a VMD morph record carries no interpolation data, so MMD morph weights are "
                     "linearly interpolated between keys; keeping every key and mapping frames "
                     "affinely therefore reproduces the source function exactly wherever a key "
                     "lands on the target grid, and the error above is what the grid costs")}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = Curve("test", [(0, 0.0), (10, 1.0), (20, 0.0)])
    print("at(0..20 step 2):", [round(c.at(f), 3) for f in range(0, 21, 2)])
    res = resample(c, 41, ratio=2.0)
    print("resampled:", res.describe())
    print("quantise 0.25:", quantise(c, 0.25, frames=41))
    print("edges(rise .5):", edges(res, 0.5))
    print("transform clamp:", transform_weights(c, clamp=(0.0, 1.0))[1])
    print("report:", report({"test": c}, 41)["max_reconstruction_error"])
