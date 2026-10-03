"""MMD Camera.vmd -> Project DIVA camera json (the shape PD_Tool eats).

Pure python: no bpy, no Blender.  Only the bundled ``vmd_reader`` module is borrowed
for the byte reading, everything past that is arithmetic on the floats it already decoded.

Calibration (inherited from the ``camera_a3da`` module, which was fitted against shipped
pv_70204 motion, and re-confirmed here against a hand-accepted Camera.json):

  unit        DIVA metre = MMD movement unit * 0.08           (diva_point:123)
  z mirror    DIVA z = -MMD z                                (diva_point:123)
  fps         an MMD project is 30 fps, DIVA is 60, so every source key lands on
              frame*2 and PlayControl.Size = last_frame*2 + 1  (build_world:158)
  fov         MMD's 視野角 is a half angle, ViewPoint.FOV is the full horizontal
              angle in radians -> radians(deg * 2)             (fov_rad:128)
  roll        ViewPoint.Roll = the camera's Z rotation in radians, no sign flip
  tangents    Trans entries are [frame, value, in_t, out_t] with in_t/out_t =
              d(value)/d(frame) at that key, i.e. the 4-tuple convention of
              BlenderDivaTools/utilities/utils.py:calculate_hermite_tangents
              (in_t = 3*(value-left_handle)/dt).  That function is ported verbatim
              below and the handles are built so its output equals the tangents we
              want, so a json written here can be re-read by the same convention.

Which point is the eye?  An MMD camera key carries 距離 + 位置 + 回転 (in that
order) and 位置 is the *aim* point: the eye sits 距離 back along the view axis.
The bundled reader's 61-byte camera record reads the seven floats straight into
pos[0..2]/rot[0..2]/extra and so puts 距離 in pos.x - see the layout probe below -
so this module re-assigns the already-decoded floats (it never touches bytes).
Proven against shipped data: with pos=(F1,F2,F3) and dist=F0 the reconstructed Interest and
ViewPoint match the accepted Camera.json to 8e-7 m on every keyed frame.

  layout "dist"  (61-byte camera records, vmd dialect 'quat')
      dist = F0, position = (F1,F2,F3), rotation = (F4,F5,F6)  [radians, or degrees
      when they are obviously too big for radians]
      Interest  = unit * ( P.x,  P.y, -P.z)
      ViewPoint = unit * ( (P + dist*u).x, (P + dist*u).y, -(P + dist*u).z )
      with u = (-sin(ry)cos(rx), sin(rx), cos(ry)cos(rx)) the aim->eye direction,
      i.e. camera_a3da.forward(rx,ry) read off the mirrored axis (x and y flipped).
  layout "pos"   (33-byte camera records, no 距離 slot: MMD wrote the eye itself)
      ViewPoint = unit * ( P.x, P.y, -P.z)
      Interest  = ViewPoint + forward * d,  d from camera_a3da.build_world's rule

knobs on build_camera_report, all defaulting to what reproduces the
accepted json:  unit=0.08, layout="auto"|"dist"|"pos", simplify=True (drop the interior
keys of a run that never moves, the way the shipped file does), band=(8,55) the 視野角
half-angle clamp from camera_a3da, min_y=None - DIVA's floor is y=0 and one reference dips to
-0.34 m and another to -0.97 m, so pass min_y=0.12 (camera_a3da.floor_clamp's floor) for
those.  FOVIsHorizontal is true and Aspect 16:9 because that is what the shipped json says.
"""
import bisect
import math
import os
import sys

try:
    from . import vmd_reader as vmd
except ImportError:                                    # running outside the repo root
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools"))
    from . import vmd_reader as vmd

UNIT = 0.08
SRC_FPS = 30.0
DST_FPS = 60.0
FRAME_STEP = int(DST_FPS / SRC_FPS)                   # 2
ASPECT = 1.77778005599976
CONVERTER_VERSION = 20050823
PROPERTY_VERSION = 20050706
FOV_BAND = (8.0, 55.0)                                # degrees, MMD half angle
STATIC_DECIMALS = 6                                   # utilities/utils.py:is_static
BODY_Y = 1.0 / UNIT                                   # a performer's chest, MMD units


# ---------------------------------------------------------------- hermite 4-tuples
def calculate_hermite_tangents(keyframes):
    """utilities/utils.py:calculate_hermite_tangents, copied without the bpy import.

    keyframes: [(frame, value, left_handle_value, right_handle_value), ...]
    returns:   [(frame, value, in_t, out_t), ...]   tangents per frame, 0 at the ends.
    """
    length = len(keyframes)
    result = []
    for i in range(length):
        frame, value, leftp, rightp = keyframes[i]
        if i == 0:
            in_t = 0.0
        else:
            t = frame - keyframes[i - 1][0]
            in_t = 3 * (value - leftp) / t
        if i == length - 1:
            out_t = 0.0
        else:
            t = keyframes[i + 1][0] - frame
            out_t = 3 * (rightp - value) / t
        result.append((frame, value, in_t, out_t))
    return result


def is_static(values, decimals=STATIC_DECIMALS):
    """utilities/utils.py:is_static - a channel whose rounded values never move is Static."""
    if not values:
        return True
    rounded = [round(v, decimals) for v in values]
    first = rounded[0]
    return all(v == first for v in rounded)


def tangents_to_frames(pairs):
    """[(frame, value)] -> [[frame, value, in_t, out_t]] through the ported function.

    The desired tangent is the central difference of the surviving keys; handles are
    back-solved from in_t = 3*(value-left)/t so the ported function returns exactly it.
    """
    n = len(pairs)
    if n == 1:
        return [[pairs[0][0], pairs[0][1], 0.0, 0.0]]
    grad = []
    for i in range(n):
        if i == 0:
            grad.append((pairs[1][1] - pairs[0][1]) / max(1, pairs[1][0] - pairs[0][0]))
        elif i == n - 1:
            grad.append((pairs[-1][1] - pairs[-2][1]) / max(1, pairs[-1][0] - pairs[-2][0]))
        else:
            dt = pairs[i + 1][0] - pairs[i - 1][0]
            grad.append((pairs[i + 1][1] - pairs[i - 1][1]) / dt if dt else 0.0)
    keyed = []
    for i, (f, v) in enumerate(pairs):
        t_prev = f - pairs[i - 1][0] if i else 1
        t_next = pairs[i + 1][0] - f if i + 1 < n else 1
        left = v - grad[i] * t_prev / 3.0
        right = v + grad[i] * t_next / 3.0
        keyed.append((f, v, left, right))
    return [[f, v, it, ot] for f, v, it, ot in calculate_hermite_tangents(keyed)]


# ---------------------------------------------------------------- source decoding
def wrap(a):
    """Fold to (-pi, pi], the way camera_a3da.wrap does before interpolating."""
    return math.atan2(math.sin(a), math.cos(a))


def _seven(rec):
    """The seven decoded floats of a camera key, in the order vmd.py stored them."""
    return (float(rec["pos"][0]), float(rec["pos"][1]), float(rec["pos"][2]),
            float(rec["rot"][0]), float(rec["rot"][1]), float(rec["rot"][2]),
            float(rec.get("extra", 0.0)))


def pick_layout(recs):
    """Decide whether the 距離 float sits before the position triple or is absent.

    Both readings are arithmetic on the same seven floats, so let the data vote: the
    rotation triple must stay inside a turn, and whichever slot is used for rotation
    has to keep the smaller worst-case magnitude.  the reference camera's slot 3 runs to 224 (that is
    位置.z, not a 224 radian pitch), one to 183, another to 15.6 - and their
    slots 4..6 never leave +-1.6 rad.  Ties go to "dist" because the 61-byte record it
    describes is the one with the extra float and the 24-byte interpolation block.
    """
    def worst(idxs):
        vals = sorted(max(abs(_seven(r)[i]) for i in idxs) for r in recs)
        return vals[len(vals) // 2]
    dist_first, pos_first = worst((4, 5, 6)), worst((3, 4, 5))
    return ("dist" if dist_first <= pos_first else "pos"), dist_first, pos_first


def read_camera(path, layout="auto"):
    """Camera keys from a vmd, de-duplicated by frame and split into per-frame scalars."""
    d = vmd.read(path)
    recs = sorted(d["camera"], key=lambda r: r["frame"])
    if not recs:
        raise ValueError("%s: this vmd has no camera keys" % path)
    by_frame = {}
    dup = 0
    for r in recs:                                     # exporters do repeat a frame
        if r["frame"] in by_frame:
            dup += 1
        by_frame[r["frame"]] = r
    keys = [by_frame[f] for f in sorted(by_frame)]
    # the seventh float only exists in the 61-byte record; a classic 33-byte camera key has
    # no 距離 slot at all, so there is nothing to re-assign and "pos" is the only reading
    has_extra = all("extra" in r for r in keys)
    if layout in ("dist", "pos"):
        want, dist_first, pos_first = layout, 0.0, 0.0
    elif has_extra:
        want, dist_first, pos_first = pick_layout(keys)
    else:
        want, dist_first, pos_first = "pos", 0.0, 0.0
    ridx = (4, 5, 6) if want == "dist" else (3, 4, 5)
    # camera_a3da.rot_is_degrees: the median of the widest rotation, never its maximum,
    # because authors type extra full turns (a reference holds 183 rad at one key)
    worst = sorted(max(abs(_seven(r)[i]) for i in ridx) for r in keys)
    degrees = worst[len(worst) // 2] > 10.0
    scale = math.pi / 180.0 if degrees else 1.0
    out = []
    for r in keys:
        f = _seven(r)
        if want == "dist":
            dist, pos, rot = f[0], (f[1], f[2], f[3]), (f[4], f[5], f[6])
        else:
            dist, pos, rot = 0.0, (f[0], f[1], f[2]), (f[3], f[4], f[5])
        out.append({"frame": int(r["frame"]), "dist": dist, "pos": pos,
                    "rot": tuple(wrap(v * scale) for v in rot), "fov": float(r["fov"])})
    info = {"vmd": os.path.abspath(path), "dialect": d["dialect"], "model": d["model"],
            "layout": want, "record_has_distance_slot": has_extra,
            "layout_median_abs_rot": {"dist": dist_first, "pos": pos_first},
            "unit": UNIT, "rot_unit": "degrees" if degrees else "radians",
            "keys_in": len(recs), "keys": len(out), "duplicate_frames": dup,
            "first_frame": out[0]["frame"], "last_frame": out[-1]["frame"],
            "fov_deg": [min(k["fov"] for k in out), max(k["fov"] for k in out)],
            "dist": [min(k["dist"] for k in out), max(k["dist"] for k in out)]}
    return out, info


# ---------------------------------------------------------------- source sampling
class Curve(object):
    """One scalar of the source, clamped outside the key range, linear inside.

    Linear is not laziness: the accepted Camera.json is itself piecewise linear
    between its keys, and sampling the source linearly reproduces 50% of its keyed
    frames to under 1e-6 m, where a Catmull-Rom (central-difference) resampling
    reproduces none and drifts up to 1.7 m.  MMD interpolates each key pair on its
    own and never smooths across a cut, which is what the shipped json shows.
    """

    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x, self.y = x, y

    def at(self, t):
        x = self.x
        if len(x) == 1 or t <= x[0]:
            return self.y[0]
        if t >= x[-1]:
            return self.y[-1]
        hi = bisect.bisect_right(x, t)
        lo = hi - 1
        a, b = x[lo], x[hi]
        return self.y[lo] + (self.y[hi] - self.y[lo]) * (t - a) / (b - a)


def curves(keys):
    """One Curve per source scalar; rotations stay folded, the samplers are periodic."""
    fr = [k["frame"] for k in keys]
    return {n: Curve(fr, [pick(k) for k in keys]) for n, pick in (
        ("dist", lambda k: k["dist"]),
        ("px", lambda k: k["pos"][0]),
        ("py", lambda k: k["pos"][1]),
        ("pz", lambda k: k["pos"][2]),
        ("rx", lambda k: k["rot"][0]),
        ("ry", lambda k: k["rot"][1]),
        ("rz", lambda k: k["rot"][2]),
        ("fov", lambda k: k["fov"]),
    )}


def eye_and_aim(layout, px, py, pz, rx, ry, dist, unit=UNIT):
    """(eye, aim) in DIVA metres for one source sample.  See the module docstring."""
    cx, sx, cy, sy_ = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry)
    aim = (px * unit, py * unit, -pz * unit)
    if layout == "dist" and abs(dist) > 1e-4:
        ux, uy, uz = -sy_ * cx, sx, cy * cx
        return ((px + dist * ux) * unit, (py + dist * uy) * unit,
                -(pz + dist * uz) * unit), aim
    # no 距離 to work from: the position is the eye, aim down the view axis (build_world)
    fx, fy, fz = sy_ * cx, -sx, cy * cx
    d = min(60.0, max(10.0, math.sqrt(px * px + (py - BODY_Y) ** 2 + pz * pz))) * unit
    return aim, (aim[0] + fx * d, aim[1] + fy * d, aim[2] + fz * d)


def fov_rad(deg, band=FOV_BAND):
    """MMD's 視野角 is a half angle; PD's FOV is the full horizontal angle in radians."""
    return math.radians(min(band[1], max(band[0], float(deg))) * 2.0)


# ---------------------------------------------------------------- json assembly
def _none():
    return {"Type": "None"}


def _static(value):
    return {"Type": "Static", "Value": value}


def collapse_flat(pairs, decimals=STATIC_DECIMALS):
    """Drop interior keys of a run that never moves, the way the accepted json does."""
    r = [round(v, decimals) for _, v in pairs]
    keep = [True] * len(pairs)
    changed = True
    while changed:
        changed = False
        for i in range(1, len(pairs) - 1):
            if keep[i] and keep[i - 1] and i + 1 < len(pairs) and keep[i + 1] \
                    and r[i - 1] == r[i] == r[i + 1]:
                keep[i] = False
                changed = True
    out = [p for p, k in zip(pairs, keep) if k]
    return out if len(out) >= 2 else list(pairs)


def channel(pairs, size, simplify=True, decimals=STATIC_DECIMALS):
    """One channel: None-free, Static when the curve never moves, else dense Hermite."""
    if simplify:
        pairs = collapse_flat(pairs, decimals)
    if is_static([v for _, v in pairs], decimals):
        return {"Type": "Static", "Value": pairs[0][1]}, len(pairs)
    return ({"Type": "Hermite", "Max": int(size), "Trans": tangents_to_frames(pairs)},
            len(pairs))


def sample_streams(keys, info, unit=UNIT, band=FOV_BAND, min_y=None):
    """Per-output-frame samples of the six position channels + roll + fov."""
    size = info["last_frame"] * FRAME_STEP + 1
    c = curves(keys)
    rows = []
    for f in range(size):
        t = f / float(DST_FPS) * SRC_FPS
        eye, aim = eye_and_aim(info["layout"], c["px"].at(t), c["py"].at(t), c["pz"].at(t),
                               c["rx"].at(t), c["ry"].at(t), c["dist"].at(t), unit)
        if min_y is not None:                       # camera_a3da.floor_clamp, off by default
            eye = (eye[0], max(min_y, eye[1]), eye[2])
            aim = (aim[0], max(min_y, aim[1]), aim[2])
        rows.append({"frame": f, "eye": eye, "aim": aim,
                     "roll": c["rz"].at(t), "fov": fov_rad(c["fov"].at(t), band)})
    return rows, size


def build_camera_report(vmd_path, file_name=None, unit=UNIT, layout="auto", simplify=True,
                        band=FOV_BAND, min_y=None):
    """(document, stats) - the two things build_camera_json and write_camera_json need."""
    keys, info = read_camera(vmd_path, layout)
    info["unit"] = unit
    info["min_y"] = min_y
    rows, size = sample_streams(keys, info, unit, band, min_y)
    frames = [r["frame"] for r in rows]
    ch, counts = {}, {}
    for ax, i in zip("XYZ", range(3)):
        ch["ViewPoint.Trans." + ax], counts["ViewPoint.Trans." + ax] = channel(
            list(zip(frames, [r["eye"][i] for r in rows])), size, simplify)
        ch["Interest.Trans." + ax], counts["Interest.Trans." + ax] = channel(
            list(zip(frames, [r["aim"][i] for r in rows])), size, simplify)
    ch["ViewPoint.Roll"], counts["ViewPoint.Roll"] = channel(
        list(zip(frames, [r["roll"] for r in rows])), size, simplify)
    ch["ViewPoint.FOV"], counts["ViewPoint.FOV"] = channel(
        list(zip(frames, [r["fov"] for r in rows])), size, simplify)

    def trans_triple(prefix):
        return {"X": ch[prefix + ".X"], "Y": ch[prefix + ".Y"], "Z": ch[prefix + ".Z"]}

    doc = {"A3D": {
        "_": {"ConverterVersion": str(CONVERTER_VERSION),
              "FileName": file_name or (os.path.splitext(os.path.basename(vmd_path))[0]
                                        + ".a3da"),
              "PropertyVersion": str(PROPERTY_VERSION)},
        "CameraRoot": [{
            "Interest": {"Rot": {"X": _none(), "Y": _none(), "Z": _none()},
                         "Scale": {"X": _static(1), "Y": _static(1), "Z": _static(1)},
                         "Trans": trans_triple("Interest.Trans"),
                         "Visibility": _static(1)},
            "ViewPoint": {"Aspect": ASPECT,
                          "FOVIsHorizontal": True,
                          "FOV": ch["ViewPoint.FOV"],
                          "Roll": ch["ViewPoint.Roll"],
                          "Rot": {"X": _none(), "Y": _none(), "Z": _none()},
                          "Scale": {"X": _static(1), "Y": _static(1), "Z": _static(1)},
                          "Trans": trans_triple("ViewPoint.Trans"),
                          "Visibility": _static(1)},
            "Rot": {"X": _none(), "Y": _none(), "Z": _none()},
            "Scale": {"X": _static(1), "Y": _static(1), "Z": _static(1)},
            "Trans": {"X": _none(), "Y": _none(), "Z": _none()},
            "Visibility": _static(1)}],
        "PlayControl": {"Begin": 0, "FPS": float(DST_FPS), "Size": int(size)}}}
    info["size"] = size
    info["keys_out"] = counts
    info["channels"] = {k: v["Type"] for k, v in sorted(ch.items())}
    info["bands"] = {k: [min(v), max(v)] for k, v in
                     (("viewpoint.x", [r["eye"][0] for r in rows]),
                      ("viewpoint.y", [r["eye"][1] for r in rows]),
                      ("viewpoint.z", [r["eye"][2] for r in rows]),
                      ("interest.x", [r["aim"][0] for r in rows]),
                      ("interest.y", [r["aim"][1] for r in rows]),
                      ("interest.z", [r["aim"][2] for r in rows]),
                      ("roll", [r["roll"] for r in rows]),
                      ("fov_rad", [r["fov"] for r in rows]))}
    return doc, info

