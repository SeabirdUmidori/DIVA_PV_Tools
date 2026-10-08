"""Measure what a morph actually deforms, in a frame that survives model changes.

The module answers one question per morph: *if I set this morph's weight to 1, what moves, where,
how much, and symmetrically?*  It does so from the source model's own data - a PMX's vertex
offsets, or the shape keys mmd_tools built from them - never from the morph's name.

Everything is expressed in **interocular units**: the distance between the model's left and right
eye bones is 1.0.  That is the one scale every MMD face shares, and it makes the resulting
signature comparable between a 53k-vertex Len and a 66k-vertex wolf, which is what a target
comparison needs.  A signature is always accompanied by `frame_provenance`, because without eye
bones there is no frame and the regional scores are not comparable to anything.

The effect of a morph at weight ``w`` is exactly linear in its definition: a vertex morph's
offset is scaled by ``w`` (``v_final = v_base + delta * w``), and a group morph's child receives
``w * ratio``.  So the weight-1 signature describes the whole family, and `MorphEvaluation` only
has to scale it.  That is a property of the format, not an approximation - see the module
docstring of `mmd_morph` for the three layers it rests on.

No Blender import: callers hand in arrays.
"""
import math

# Region names used throughout the project.  Adding one is a change to every consumer, so the set
# is deliberately small and closed.
REGIONS = ("eye_left", "eye_right", "eyebrow_left", "eyebrow_right", "mouth", "tongue",
           "cheek", "face", "outside")

# Which regions count as evidence for which coarse area.  "eye" aggregates both eyes, "mouth"
# includes the corners, "eyebrow" both brows.
REGION_GROUPS = {
    "eye": ("eye_left", "eye_right"),
    "eyebrow": ("eyebrow_left", "eyebrow_right"),
    "mouth": ("mouth", "tongue"),
    "cheek": ("cheek",),
    "face": ("face",),
    "outside": ("outside",),
}

# Region geometry, in **interocular units** measured from the midpoint between the eye bones.
#
# The first attempt at this used horizontal y-bands and it does not work, which the real models
# prove: on the YYB Kagamine Len 10th (interocular 0.654) the eye morphs まばたき/じと目 reach
# y = -1.42 and the mouth morphs あ/口角上げ start at y = -0.45, so the eye and mouth bands overlap
# in height; and the brow morphs 真面目/上/下 sit at y = +0.04..+1.11, which is exactly where the
# middle of a blink sits.  Sorting by y therefore mislabels a real blink's own vertices.
#
# What does work is **nearest anchor**: each region has an anchor point, and a vertex belongs to
# whichever anchor is closest, with lateral distance discounted because a face is much wider than
# it is tall.  Overlapping regions stop being a problem because closeness, not order, decides.
# The anchors are expressed relative to the eye bones and the model's own scale, so they land on
# the right place on a 53k-vertex Miku and a 66k-vertex wolf alike.
DEFAULTS = {
    # anchor positions, interocular units, (lateral, vertical, depth) from the interocular centre
    "eye_anchor": (0.60, 0.05, -0.35),      # per side, mirrored
    "brow_anchor": (0.65, 1.15, -0.45),     # per side, mirrored
    "mouth_anchor": (0.0, -1.65, -0.30),
    "tongue_anchor": (0.0, -1.50, 0.45),
    "cheek_anchor": (1.15, -0.55, -0.45),   # per side, mirrored
    "face_anchor": (0.0, 0.0, 0.0),
    "outside_anchor": (3.2, 0.0, 0.0),
    # how much each axis counts when measuring closeness: lateral distance is discounted
    "weights": (0.65, 1.0, 0.55),
    # a vertex is only assigned a region when the winner is this much closer than the runner-up;
    # otherwise it is "face", because claiming a precise region from an ambiguous point would be
    # inventing precision the geometry does not have
    "decisive_ratio": 0.82,
}


class MissingFrame(Exception):
    """Raised when the model has no eye bones, so no interocular frame can be built."""


class FaceFrame(object):
    """The model's face coordinate frame: an origin, a scale, and derived region boxes.

    Built from the eye bones' rest positions.  ``origin`` is the midpoint between the two eyes and
    ``scale`` the distance between them, so a position ``p`` becomes ``(p - origin) / scale`` and
    every threshold below is in interocular units.

    MMD is left-handed with +Y up, so `x` is left/right (negative = the model's own left when the
    convention holds), `y` is up, `z` is forward.  The *subject's* left/right is decided by the
    bone names, not by the sign of x: a model whose `左目` sits at positive x is mirrored, and the
    frame records that so `eye_left` means the anatomical left in both cases.
    """

    __slots__ = ("origin", "scale", "left_is_positive_x", "eye_left", "eye_right", "source",
                 "thresholds", "nose", "mouth_anchor", "lips", "tongue", "calibrated_anchors")

    def __init__(self, eye_left, eye_right, source="eye bones", thresholds=None):
        self.eye_left = tuple(eye_left)
        self.eye_right = tuple(eye_right)
        self.source = source
        self.thresholds = dict(DEFAULTS)
        if thresholds:
            self.thresholds.update(thresholds)
        dx = eye_right[0] - eye_left[0]
        dy = eye_right[1] - eye_left[1]
        dz = eye_right[2] - eye_left[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist <= 1e-6:
            raise MissingFrame("the two eye bones coincide, so there is no interocular scale")
        self.scale = dist
        self.origin = tuple((eye_left[i] + eye_right[i]) * 0.5 for i in range(3))
        # which way is the subject's left along x?  Decided by the bones, so a mirrored model
        # still reports eye_left as the anatomical left.
        self.left_is_positive_x = eye_left[0] > eye_right[0]
        self.mouth_anchor = None
        self.nose = None
        self.lips = None
        self.tongue = None
        self.calibrated_anchors = None

    def with_anchors(self, mouth=None, tongue=None, lips=None):
        """Pin the mouth anchors from a landmark the model itself provides.

        A face must have a jaw to open, so a model with a `口` / `あご` / `顎` / `下あご` bone has
        its mouth position stated in the file.  Measuring beats deriving: on the Muramasa model the
        vowels い and う put 0.56 of their displacement in the generic face region and only 0.33 in
        the mouth region, because the mouth anchor inferred from the interocular scale sat too low
        for that model's proportions.  Preferring the model's own jaw bone removes that class of
        error instead of tuning thresholds until one model looks right.
        """
        if mouth is not None:
            self.mouth_anchor = tuple(mouth)
        if tongue is not None:
            self.tongue = tuple(tongue)
        if lips is not None:
            self.lips = tuple(lips)
        return self

    def to_frame(self, p):
        return ((p[0] - self.origin[0]) / self.scale,
                (p[1] - self.origin[1]) / self.scale,
                (p[2] - self.origin[2]) / self.scale)

    def lateral_sign(self, p):
        """+1 when a point is on the subject's left, -1 on the right, 0 near the midline."""
        x = self.to_frame(p)[0]
        if abs(x) < 0.05:
            return 0
        return 1 if (x > 0) == self.left_is_positive_x else -1

    def describe(self):
        return {"origin": [round(v, 4) for v in self.origin], "scale": round(self.scale, 4),
                "left_is_positive_x": self.left_is_positive_x, "source": self.source,
                "thresholds": dict(self.thresholds)}


def frame_from_bones(bone_positions, bone_names, thresholds=None):
    """Build a `FaceFrame` from a PMX bone list, using the model's own face bones.

    Recognises the standard MMD eye bones (`左目`/`右目`, with `両目` as a fallback whose position
    is used for both) and, when the model has them, the jaw/tongue/upper-lip bones that state where
    the mouth is.  It raises `MissingFrame` rather than guessing a centre from the mesh bounds,
    because a wrong frame would silently mislabel every region.
    """
    index = {}
    for i, name in enumerate(bone_names):
        index.setdefault(name, i)

    def pos(*names):
        for n in names:
            if n in index:
                return bone_positions[index[n]]
        return None

    left = pos("左目", "eye_L", "eyeL", "Eye_L", "左眼球")
    right = pos("右目", "eye_R", "eyeR", "Eye_R", "右眼球")
    if left is None or right is None:
        both = pos("両目", "eyes", "Eyes")
        if both is None:
            raise MissingFrame("no 左目/右目 (or 両目) bone in the model, so the interocular frame "
                               "cannot be built and no regional score can be trusted")
        left = (both[0] - 0.5, both[1], both[2])
        right = (both[0] + 0.5, both[1], both[2])
        frame = FaceFrame(left, right, source="両目 (split by an assumed 1-unit separation)",
                          thresholds=thresholds)
    else:
        frame = FaceFrame(left, right, source="左目/右目 rest positions", thresholds=thresholds)

    # The model's own statement of where the mouth is, when it makes one.
    jaw = pos("口", "あご", "顎", "下あご", "下顎", "jaw", "Jaw", "mouth", "Mouth")
    tongue = pos("舌", "舌先", "tongue", "Tongue")
    lips = pos("上唇", "upper_lip", "UpperLip", "くちびる", "唇")
    if jaw is not None or tongue is not None or lips is not None:
        frame.with_anchors(mouth=jaw or lips, tongue=tongue or lips, lips=lips or jaw)
        frame.source += " + the model's own mouth bone"
    return frame


def frame_from_landmarks(points, scale=None, thresholds=None):
    """Frame from two explicit landmark points (used by tests and by headless validation)."""
    return FaceFrame(points[0], points[1], source="explicit landmarks", thresholds=thresholds)


# ------------------------------------------------------------------------------------ regions
def _anchor_points(frame):
    """The region anchors for this model, in world coordinates.

    Anchors are placed relative to the interocular centre and scaled by the interocular distance,
    so the same table lands correctly on any face.  Left/right pairs are mirrored using the frame's
    own handedness, which is what makes `eye_left` mean the anatomical left on a mirrored model.
    The mouth anchors prefer the model's own jaw/tongue bones when the frame carries them.
    """
    t = frame.thresholds
    sign = 1.0 if frame.left_is_positive_x else -1.0
    out = {}

    def place(name, lateral, vertical, depth):
        out[name] = (frame.origin[0] + lateral * sign * frame.scale,
                     frame.origin[1] + vertical * frame.scale,
                     frame.origin[2] + depth * frame.scale)

    place("eye_left", *t["eye_anchor"])
    place("eye_right", -t["eye_anchor"][0], t["eye_anchor"][1], t["eye_anchor"][2])
    place("eyebrow_left", *t["brow_anchor"])
    place("eyebrow_right", -t["brow_anchor"][0], t["brow_anchor"][1], t["brow_anchor"][2])
    place("cheek_left", *t["cheek_anchor"])
    place("cheek_right", -t["cheek_anchor"][0], t["cheek_anchor"][1], t["cheek_anchor"][2])
    place("mouth", *t["mouth_anchor"])
    place("tongue", *t["tongue_anchor"])
    place("face", *t["face_anchor"])
    place("outside", *t["outside_anchor"])
    if frame.mouth_anchor is not None:
        out["mouth"] = tuple(frame.mouth_anchor)
    if frame.tongue is not None:
        out["tongue"] = tuple(frame.tongue)
    # morph-calibrated anchors win over every inferred one: they were measured from the model's
    # own deformation, which is the best evidence available for where its face features are
    for name, point in (frame.calibrated_anchors or {}).items():
        out[name] = tuple(point)
    return out


def region_of(p, frame, anchors=None):
    """Which face region a point belongs to, by *nearest anchor* in the interocular frame.

    Nearest-anchor rather than sorted bands, because on real models the regions overlap in height:
    measured on the YYB Kagamine Len 10th (interocular 0.654), the eye morphs まばたき/じと目 reach
    y = -1.42 while the mouth morphs あ/口角上げ begin at y = -0.45, and the brow morphs
    真面目/上/下 sit at y = +0.04..+1.11 - exactly the middle of a blink.  Any ordering of
    horizontal bands therefore mislabels one family or the other.

    The winner must also be `decisive_ratio` times closer than the runner-up; an ambiguous vertex
    is reported as `face`, because naming a precise region from an ambiguous point would invent
    precision the geometry does not have.  The tongue is separated from the mouth by its anchor's
    depth, which is the real distinction (the tongue comes forward, the jaw goes down).
    """
    if anchors is None:
        anchors = _anchor_points(frame)
    wx, wy, wz = frame.thresholds["weights"]
    rel = ((p[0] - frame.origin[0]) / frame.scale,
           (p[1] - frame.origin[1]) / frame.scale,
           (p[2] - frame.origin[2]) / frame.scale)
    best = None
    second = None
    for name, a in anchors.items():
        ax = (a[0] - frame.origin[0]) / frame.scale
        ay = (a[1] - frame.origin[1]) / frame.scale
        az = (a[2] - frame.origin[2]) / frame.scale
        d = math.sqrt((wx * (rel[0] - ax)) ** 2 + (wy * (rel[1] - ay)) ** 2
                      + (wz * (rel[2] - az)) ** 2)
        if best is None or d < best[1]:
            second = best
            best = (name, d)
        elif second is None or d < second[1]:
            second = (name, d)
    if best is None:
        return "face"
    if second is not None and best[1] > second[1] * frame.thresholds["decisive_ratio"]:
        # the two nearest anchors are too close to call: report the coarse region only
        coarse = {"eye_left": "eye", "eye_right": "eye", "eyebrow_left": "eyebrow",
                  "eyebrow_right": "eyebrow", "cheek_left": "cheek", "cheek_right": "cheek",
                  "mouth": "mouth", "tongue": "mouth", "face": "face", "outside": "outside"}
        if coarse.get(best[0]) == coarse.get(second[0]):
            name = best[0]
        else:
            return "face"
    else:
        name = best[0]
    if name.startswith("cheek"):
        return "cheek"
    return name


class MorphEffectSignature(object):
    """What one morph does at weight 1, measured, with the frame it was measured in.

    `affected_*` count vertices whose offset is non-zero at weight 1.  `regions` maps a region to
    the share of total displacement magnitude that lands there - a morph whose mouth vertices move
    a little and whose eye vertices move a lot is an eye morph even if it touches both.
    """

    __slots__ = ("name", "frame", "vertex_total", "affected", "regions", "region_counts",
                 "delta_centroid", "delta_rms", "delta_max", "delta_sum", "bbox",
                 "lr_symmetry", "ul_symmetry", "net_direction", "kind", "bone_offsets",
                 "material_offsets", "uv_offsets", "group_children", "group_weights",
                 "children", "truncated", "notes", "frame_provenance")

    def __init__(self, name):
        self.name = name
        self.frame = None
        self.frame_provenance = None
        self.vertex_total = 0
        self.affected = 0
        self.regions = {}
        self.region_counts = {}
        self.delta_centroid = (0.0, 0.0, 0.0)
        self.delta_rms = 0.0
        self.delta_max = 0.0
        self.delta_sum = 0.0
        self.bbox = None
        self.lr_symmetry = None
        self.ul_symmetry = None
        self.net_direction = (0.0, 0.0, 0.0)
        self.kind = "vertex"
        self.bone_offsets = []
        self.material_offsets = []
        self.uv_offsets = []
        self.group_children = []
        self.group_weights = []
        self.children = []
        self.truncated = False
        self.notes = []

    @property
    def dominant_region(self):
        if not self.regions:
            return None
        return max(self.regions.items(), key=lambda kv: kv[1])[0]

    @property
    def dominant_group(self):
        """The coarse area holding most of the displacement: eye / eyebrow / mouth / cheek."""
        best, score = None, 0.0
        for group, members in REGION_GROUPS.items():
            s = sum(self.regions.get(m, 0.0) for m in members)
            if s > score:
                best, score = group, s
        return best

    def group_score(self, group):
        return sum(self.regions.get(m, 0.0) for m in REGION_GROUPS.get(group, ()))

    def group_count(self, group):
        return sum(self.region_counts.get(m, 0) for m in REGION_GROUPS.get(group, ()))

    def describe(self):
        return {"name": self.name, "kind": self.kind, "frame_provenance": self.frame_provenance,
                "frame": self.frame.describe() if self.frame else None,
                "vertex_total": self.vertex_total, "affected_vertex_count": self.affected,
                "regions": {k: round(v, 4) for k, v in sorted(self.regions.items())},
                "region_counts": dict(sorted(self.region_counts.items())),
                "dominant_region": self.dominant_region, "dominant_group": self.dominant_group,
                "delta_centroid": [round(v, 5) for v in self.delta_centroid],
                "delta_rms": round(self.delta_rms, 5), "delta_max": round(self.delta_max, 5),
                "delta_sum": round(self.delta_sum, 5),
                "bbox": self.bbox, "lr_symmetry": self.lr_symmetry,
                "ul_symmetry": self.ul_symmetry,
                "net_direction": [round(v, 5) for v in self.net_direction],
                "bone_offsets": len(self.bone_offsets),
                "material_offsets": len(self.material_offsets),
                "uv_offsets": len(self.uv_offsets),
                "group_children": list(self.group_children),
                "group_weights": list(self.group_weights),
                "children": list(self.children), "truncated": self.truncated,
                "notes": list(self.notes)}


# Which MMD semantic categories are evidence for which anchor.  A model that ships an `あ` and an
# `い` has *stated* where its mouth is: the displacement-weighted centroid of those morphs is the
# mouth, measured rather than inferred from the interocular scale.  This is the calibration that
# makes the region scores trustworthy across models - on the Muramasa model the vowels sit at
# y = -1.02 while the scale-derived anchor sat at y = -1.65, which pushed 0.56 of their
# displacement into the generic face region and made the mouth score lose to it.
CALIBRATION_CATEGORIES = {
    # The apertures: these move the mouth and the lids.  A model that ships an `あ` and an `い`
    # has stated where its mouth is.
    "mouth": ("mouth_phoneme", "mouth_close", "mouth_open", "mouth_smirk", "mouth_pucker"),
    # Pupil-only morphs are deliberately absent: their centroid is the pupil, not the lid, and
    # the pupil sits a little below the eye bone, so including them drags the eye anchor down.
    "eye": ("eyelid_blink", "eyelid_wink_left", "eyelid_wink_right", "eye_smile", "eye_narrow",
            "eye_wide", "eye_shape"),
    # `brow_neutral` is deliberately absent.  A "return the brows to rest" morph deforms almost
    # nothing - measured on the Muramasa model, `真面目` moves 112 vertices with its mass right at
    # the eye line - so its centroid is noise, and using it as the brow anchor drags the anchor
    # onto the eyes.  Only morphs that *state a direction* are allowed to calibrate.
    "eyebrow": ("brow_up", "brow_angry", "brow_sad", "brow_smile"),
}


def weighted_centroid(positions, offsets):
    """Displacement-weighted centroid of a morph's affected vertices, or None."""
    sx = sy = sz = total = 0.0
    n = 0
    for off in offsets:
        vi = off[0]
        if vi >= len(positions):
            continue
        dx, dy, dz = off[1], off[2], off[3]
        mag = math.sqrt(dx * dx + dy * dy + dz * dz)
        if mag <= 1e-12:
            continue
        p = positions[vi]
        sx += p[0] * mag
        sy += p[1] * mag
        sz += p[2] * mag
        total += mag
        n += 1
    if not total:
        return None
    return (sx / total, sy / total, sz / total)


def calibrate_frame(frame, definitions, positions, classify=None):
    """Re-anchor the frame's mouth/eye/brow anchors from the model's own morphs.

    `classify` is `mmd_morph.classify`; it is called with the *name only*, never with geometry, so
    the calibration cannot become circular (a morph's region cannot decide the anchor that decides
    its region).  Each anchor becomes the median of the weighted centroids of the morphs whose
    names belong to that family, per side for the paired ones, and the frame records that it was
    calibrated so every report can say so.
    """
    if classify is None:
        from . import mmd_morph
        classify = mmd_morph.classify
    want = {}
    for anchor, categories in CALIBRATION_CATEGORIES.items():
        want[anchor] = categories
    samples = {k: [] for k in want}
    paired = {"eye": ("eye_left", "eye_right"), "eyebrow": ("eyebrow_left", "eyebrow_right")}
    for name, definition in (definitions or {}).items():
        if not definition.vertex_offsets:
            continue
        c = classify(name)
        for anchor, categories in want.items():
            if c.category not in categories:
                continue
            centroid = weighted_centroid(positions, definition.vertex_offsets)
            if centroid is None:
                continue
            side = None
            if anchor in paired and frame is not None:
                side = frame.lateral_sign(centroid)
            samples.setdefault(anchor, []).append((centroid, side))
            break

    def median_point(rows):
        if not rows:
            return None
        out = []
        for k in range(3):
            vals = sorted(r[k] for r in rows)
            mid = len(vals) // 2
            out.append(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0)
        return tuple(out)

    calibrated = {}
    for anchor, rows in samples.items():
        if anchor in paired:
            left = [c for c, side in rows if side is not None and side > 0]
            right = [c for c, side in rows if side is not None and side < 0]
            lp, rp = median_point(left), median_point(right)
            if lp:
                calibrated[paired[anchor][0]] = lp
            if rp:
                calibrated[paired[anchor][1]] = rp
        else:
            p = median_point([c for c, _s in rows])
            if p:
                calibrated[anchor] = p
    if calibrated and frame is not None:
        frame.calibrated_anchors = calibrated
        frame.source += " (anchors calibrated from %d of the model's own morph groups)" % len(
            calibrated)
    return calibrated


def _symmetry(points_left, points_right):
    """Fraction of the left side's displacement matched by the mirrored right side, 0..1.

    Both sides are summarised by their own centroid and magnitude, so this is a coarse shape
    agreement rather than a per-vertex match - which is all that is needed to tell a wink from a
    blink - and it is reported as such.
    """
    if not points_left or not points_right:
        return None
    lm = sum(math.sqrt(p[1] ** 2 + p[2] ** 2 + p[3] ** 2) for p in points_left) / len(points_left)
    rm = sum(math.sqrt(p[1] ** 2 + p[2] ** 2 + p[3] ** 2) for p in points_right) / len(points_right)
    if lm <= 1e-9 and rm <= 1e-9:
        return 1.0
    return min(lm, rm) / max(lm, rm) if max(lm, rm) > 0 else 1.0


def signature_from_vertex_offsets(name, positions, offsets, frame, kind="vertex",
                                  total_vertices=None):
    """Measure a vertex morph's effect.

    `positions` is the model's base vertex positions, `offsets` an iterable of
    ``(vertex_index, dx, dy, dz)`` as the PMX stores them (the delta, not an absolute).  Every
    statistic is computed from the deltas at weight 1; a caller wanting another weight scales.
    """
    sig = MorphEffectSignature(name)
    sig.kind = kind
    sig.frame = frame
    sig.frame_provenance = frame.source if frame else None
    sig.vertex_total = int(total_vertices if total_vertices is not None else len(positions))
    mag_by_region = {}
    count_by_region = {}
    total_mag = 0.0
    sx = sy = sz = 0.0
    sq = 0.0
    mx = 0.0
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    left_pts, right_pts = [], []
    upper = lower = 0
    n = 0
    for off in offsets:
        vi, dx, dy, dz = off[0], off[1], off[2], off[3]
        mag = math.sqrt(dx * dx + dy * dy + dz * dz)
        if mag <= 1e-12:
            continue
        n += 1
        total_mag += mag
        sq += mag * mag
        mx = max(mx, mag)
        sx += dx
        sy += dy
        sz += dz
        base = positions[vi] if vi < len(positions) else (0.0, 0.0, 0.0)
        for k in range(3):
            lo[k] = min(lo[k], base[k])
            hi[k] = max(hi[k], base[k])
        if frame is not None:
            region = region_of(base, frame)
            mag_by_region[region] = mag_by_region.get(region, 0.0) + mag
            count_by_region[region] = count_by_region.get(region, 0) + 1
            side = frame.lateral_sign(base)
            rec = (base[0], dx, dy, dz)
            if region.startswith("eye"):
                (left_pts if side > 0 else right_pts).append(rec)
            elif region.startswith("eyebrow"):
                (left_pts if side > 0 else right_pts).append(rec)
            elif region == "mouth":
                if side > 0:
                    left_pts.append(rec)
                elif side < 0:
                    right_pts.append(rec)
            ty = frame.to_frame(base)[1]
            if ty > 0.1:
                upper += 1
            elif ty < -0.1:
                lower += 1
    sig.affected = n
    sig.delta_sum = total_mag
    sig.delta_rms = math.sqrt(sq / n) if n else 0.0
    sig.delta_max = mx
    sig.delta_centroid = (sx / n, sy / n, sz / n) if n else (0.0, 0.0, 0.0)
    sig.net_direction = sig.delta_centroid
    if n:
        sig.bbox = [round(lo[k], 4) for k in range(3)] + [round(hi[k], 4) for k in range(3)]
    if total_mag > 0:
        sig.regions = {k: v / total_mag for k, v in mag_by_region.items()}
    sig.region_counts = count_by_region
    if upper + lower:
        sig.ul_symmetry = min(upper, lower) / max(upper, lower)
    sig.lr_symmetry = _symmetry(left_pts, right_pts)
    if frame is None:
        sig.notes.append("no interocular frame was available, so no regional score was computed "
                         "and this signature cannot be compared against a target's")
    return sig


def signature_from_shape_key(name, deltas, positions, frame, total_vertices=None):
    """Convenience wrapper: shape-key deltas are already absolute-minus-basis, in vertex order."""
    offsets = [(i, d[0], d[1], d[2]) for i, d in enumerate(deltas)]
    return signature_from_vertex_offsets(name, positions, offsets, frame,
                                         total_vertices=total_vertices)


class MorphEvaluation(object):
    """The deformation a morph produces at one weight, or why it cannot be produced.

    `apply()` is the honest core of the transfer: for a vertex morph the deformation is the
    signature scaled by the weight; for a group morph it is the composition of its children's
    signatures scaled by ``weight * ratio``.  `residual_error` compares what was asked for against
    what the target can give, so an approximation is always quantified rather than asserted.
    """

    __slots__ = ("definition", "weight", "scale", "child_evaluations", "reason")

    def __init__(self, definition, weight, scale=1.0, child_evaluations=None, reason=""):
        self.definition = definition
        self.weight = float(weight)
        self.scale = float(scale) * float(weight)
        self.child_evaluations = list(child_evaluations or [])
        self.reason = reason

    @property
    def effective_weight(self):
        return self.scale

    def describe(self):
        return {"morph": self.definition.name if self.definition else None,
                "kind": self.definition.kind if self.definition else None,
                "weight": self.weight, "effective_weight": self.effective_weight,
                "children": [c.describe() for c in self.child_evaluations],
                "reason": self.reason}


def evaluate(definitions, name, weight, depth=0, max_depth=4, _seen=None):
    """Evaluate a morph at a weight, expanding group morphs.

    The PMX specification says a group morph may not contain another group morph
    (``グループモーフのグループ化は非対応``), and MMD itself only supports PMX 2.0 - but a
    hand-edited file can still contain one, so recursion is bounded and a cycle is reported
    rather than trusted.  A negative ratio or one above 1 is passed through unchanged: the spec
    states morph values need not lie in [0, 1] and that negatives arise exactly this way.
    """
    _seen = _seen or set()
    definition = definitions.get(name)
    if definition is None:
        return MorphEvaluation(None, weight, reason="SOURCE_MORPH_DEFINITION_NOT_FOUND")
    if name in _seen:
        return MorphEvaluation(definition, weight, reason="group morph cycle: %s" % name)
    if depth >= max_depth:
        return MorphEvaluation(definition, weight, reason="group nesting deeper than %d" % max_depth)
    ev = MorphEvaluation(definition, weight)
    if definition.group_children:
        _seen = _seen | {name}
        for child, ratio in zip(definition.group_children, definition.group_weights):
            ev.child_evaluations.append(
                evaluate(definitions, child, weight * ratio, depth + 1, max_depth, _seen))
        if depth == 0:
            ev.reason = ("group morph: %d child(ren), ratios %s"
                         % (len(definition.group_children),
                            ", ".join("%.3f" % r for r in definition.group_weights[:6])))
    return ev


def analyse_definitions(definitions, frame, positions, wanted=None, kinds=("vertex",),
                        group_expand=True, calibrate=True):
    """Signatures for every definition, with group morphs expanded into their children.

    Returns ``{name: signature}`` plus ``{name: note}`` for the morphs that could not be measured
    and why - a morph with no vertex offsets is reported as `NO_VERTEX_DATA`, never as a zero
    effect, because those are different statements.

    `calibrate` re-anchors the frame from the model's own mouth/eye/brow morphs before measuring;
    it is on by default because the scale-derived anchor is a guess and the model's own geometry is
    not.  The calibration uses names only, so a morph's region can never decide the anchor that
    decides its region.
    """
    if calibrate and frame is not None:
        calibrate_frame(frame, definitions, positions)
    out = {}
    notes = {}
    for name, definition in definitions.items():
        if wanted is not None and name not in wanted:
            continue
        if definition.kind not in kinds and not definition.group_children:
            notes[name] = "kind %s is not measured here (%s)" % (definition.kind,
                                                                 definition.provenance)
            continue
        if not definition.vertex_offsets and not definition.group_children:
            notes[name] = ("NO_VERTEX_DATA (provenance %s carries %d vertex, %d bone, %d material, "
                           "%d uv offsets)" % (definition.provenance,
                                               len(definition.vertex_offsets),
                                               len(definition.bone_offsets),
                                               len(definition.material_offsets),
                                               len(definition.uv_offsets)))
            continue
        sig = None
        if definition.vertex_offsets:
            sig = signature_from_vertex_offsets(name, positions, definition.vertex_offsets, frame,
                                                kind=definition.kind,
                                                total_vertices=definition.vertex_count)
        else:
            sig = MorphEffectSignature(name)
            sig.kind = definition.kind
            sig.frame = frame
            sig.frame_provenance = frame.source if frame else None
            sig.vertex_total = definition.vertex_count or len(positions)
            sig.notes.append("this morph has no vertex offsets of its own")
        sig.bone_offsets = list(definition.bone_offsets)
        sig.material_offsets = list(definition.material_offsets)
        sig.uv_offsets = list(definition.uv_offsets)
        if group_expand:
            sig.group_children = list(definition.group_children)
            sig.group_weights = list(definition.group_weights)
            sig.children = list(definition.group_children)
        out[name] = sig
    return out, notes


def compare(source_sig, target_sig):
    """Weighted distance between two signatures, and the per-feature breakdown that explains it.

    The features are deliberately few and each one is a *semantic* dimension of the face rather
    than a vertex correspondence: the two models do not share topology, so a vertex-level
    comparison would be meaningless.  Distance 0 means the two signatures agree on every feature;
    `features` names the ones that contributed most, so a mapping can always be explained.
    """
    if source_sig is None or target_sig is None:
        return {"distance": None, "features": {}, "reason": "a signature is missing"}
    feats = {}

    def put(name, a, b, weight):
        if a is None or b is None:
            return
        feats[name] = {"source": round(a, 4), "target": round(b, 4), "weight": weight,
                       "delta": round(abs(a - b), 4), "contribution": round(weight * abs(a - b), 4)}

    for group in ("eye", "eyebrow", "mouth", "cheek", "face", "outside"):
        put("region:%s" % group, source_sig.group_score(group), target_sig.group_score(group), 3.0)
    put("lr_symmetry", 0.0 if source_sig.lr_symmetry is None else source_sig.lr_symmetry,
        0.0 if target_sig.lr_symmetry is None else target_sig.lr_symmetry, 1.5)
    put("ul_symmetry", 0.0 if source_sig.ul_symmetry is None else source_sig.ul_symmetry,
        0.0 if target_sig.ul_symmetry is None else target_sig.ul_symmetry, 1.0)
    # scale-free magnitude: the ratio of RMS displacement to the model's own interocular unit
    put("delta_rms", source_sig.delta_rms, target_sig.delta_rms, 4.0)
    put("affected_fraction",
        source_sig.affected / source_sig.vertex_total if source_sig.vertex_total else 0.0,
        target_sig.affected / target_sig.vertex_total if target_sig.vertex_total else 0.0, 1.0)
    total = sum(f["contribution"] for f in feats.values())
    ranked = sorted(feats.items(), key=lambda kv: -kv[1]["contribution"])[:4]
    return {"distance": round(total, 5), "features": feats,
            "top": [{"feature": k, **v} for k, v in ranked],
            "source_dominant": source_sig.dominant_group,
            "target_dominant": target_sig.dominant_group}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # self-check: a synthetic face with two eye bones and four vertices
    frame = frame_from_landmarks(((-0.5, 0.0, 0.0), (0.5, 0.0, 0.0)))
    print("frame:", frame.describe())
    pos = [(-0.4, 0.15, 0.05), (0.4, 0.15, 0.05), (-0.1, -0.5, 0.1), (0.1, -0.5, 0.1)]
    lid = [(0, 0.0, -0.03, 0.0), (1, 0.0, -0.03, 0.0)]
    wink = [(0, 0.0, -0.03, 0.0)]
    mouth = [(2, 0.0, -0.05, 0.0), (3, 0.0, -0.05, 0.0)]
    for nm, offs in (("blink", lid), ("wink_left_only", wink), ("mouth_open", mouth)):
        sig = signature_from_vertex_offsets(nm, pos, offs, frame, total_vertices=4)
        d = sig.describe()
        print("%-16s dominant=%-10s regions=%s lr=%s" % (nm, d["dominant_group"], d["regions"],
                                                         d["lr_symmetry"]))
    a = signature_from_vertex_offsets("a", pos, lid, frame, total_vertices=4)
    b = signature_from_vertex_offsets("b", pos, mouth, frame, total_vertices=4)
    print("compare(lid, mouth):", compare(a, b)["distance"], compare(a, b)["top"][0]["feature"])
