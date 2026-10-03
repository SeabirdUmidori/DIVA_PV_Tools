"""MMD Camera.vmd -> Project DIVA auth_3d camera (CAMPV<pv>_BASE.a3da).

The numbers here are not guesses:

  frame            DIVA runs at 60 fps, an MMD project at 30, so every MMD key lands on
                   frame*2.  Confirmed twice: the pv_70204 motion keys its channels every 2
                   frames, and diva-camtool (thtrandomlurker) does `Frame = ThirtyFrame * 2`.
  position         DIVA = MMD * 0.08 with Z mirrored.  diva-camtool multiplies by 0.08 and
                   negates Z; fitting pv_70204's shipped motion against the MMD file it came
                   from gave 0.0776..0.0800 on every position channel, so one MMD movement
                   unit is ~8 cm in both files.
  fov              DIVA's `fov` channel is the horizontal angle in radians and MMD's 視野角 is
                   a half-angle: diva-camtool emits `rot_x * 2 * pi/180`, and pv_70204's
                   focal_length 36.93 mm against its 42.67 mm film width is exactly
                   21.33 / tan(30 deg) = 2 x 30 deg, which is MMD's default.
  roll             = the camera's Z rotation in radians, no sign flip (diva-camtool).
  channel shape    view_point.trans + interest.trans + roll + fov (or focal_length + the two
                   apertures), everything else at its zero/one default, one key per frame with
                   track type 3 - the shape pv_70204 ships, which is known to load.

An MMD camera VMD carries an eye position and an orientation but no target, and DIVA needs a
target, so the aim point is reconstructed as eye + forward * d.  Only the direction reaches the
rendered image, so the shot comes out as edited even though d is a chosen number.

Two source shapes turn up and they need different handling:

  world   the VMD holds a real world transform (a camera 5-25 MMD units up, orbiting 19-75
          units out, i.e. 0.4-2 m high at 1.5-6 m from the singer).  Direct conversion.
  follow  the VMD's transform is not usable as a world path.  One shipped Camera.vmd keeps |y| at
          0.15 units - 1 cm above the floor - while x runs to -387, and its rotation is in
          degrees, so it is a Blender-side parented/local transform.  Without the parent the
          world path cannot be recovered, so this mode keeps the edit's rhythm (cut points and
          zoom come from the source fov curve) and drives a camera around the singer instead.

usage: python -m diva_pv_tools.camera_a3da --vmd Camera.vmd --out CAMPV8327_BASE.a3da --pv 8327 \
           --frames 12172 [--mode auto|world|follow] [--unit 0.08]
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from . import a3da as a3da  # noqa: E402
from . import vmd_reader as vmd  # noqa: E402

FPS = 60.0
SRC_FPS = 30.0
UNIT = 0.08                                  # MMD movement unit -> DIVA metre (see docstring)
ASPECT = 1.77778
MIN_FOV, MAX_FOV = 8.0, 55.0                 # degrees, the sane band for MMD's half-angle
YAW_CYCLE = (0.0, -34.0, 25.0, -62.0, 12.0, 48.0, -15.0, 68.0)   # degrees, front-biased
BODY_Y = 1.0 / UNIT                           # a performer's chest height, in MMD units


def read_cam(path):
    """Camera keys sorted by frame (exporters do write them out of order)."""
    d = vmd.read(path)
    keys = [{"frame": c["frame"], "pos": c["pos"], "rot": c["rot"], "fov": c["fov"],
             "dist": c.get("extra", 0.0)} for c in d["camera"]]
    keys.sort(key=lambda k: k["frame"])
    return keys, d["dialect"]


def looks_world_space(keys):
    """True when the transform reads as a camera standing in the scene, not as an offset.

    An MMD dancer is ~20 movement units tall, so a real camera path keeps its height in the
    5-25 unit band and orbits 19-75 units out.  The reference camera's 2004 keys never rise above 0.15 units -
    a lens on the floor - while x runs to -387, which is a parented offset, not a path.
    """
    height = sorted(abs(k["pos"][1]) for k in keys)
    radius = sorted(math.hypot(k["pos"][0], k["pos"][2]) for k in keys)
    return height[len(height) // 2] > 2.0 and radius[len(radius) // 2] > 8.0


def rot_is_degrees(keys):
    """MMD writes camera rotation in radians; Blender-side exporters write degrees.

    The median, not the maximum: MMD authors type extra full turns (a reference file holds 183.1 rad, which
    is 29 turns plus 0.19), so the tail of a radians file is large while its middle is small.
    A degrees file sits at tens to hundreds throughout.
    """
    worst = sorted(max(abs(r) for r in k["rot"]) for k in keys)
    return worst[len(worst) // 2] > 10.0


def wrap(a):
    """Fold an angle to (-pi, pi], so a 29-turn camera reads as the orientation it performs."""
    return math.atan2(math.sin(a), math.cos(a))


def at(keys, t, pick):
    """Linear read of the source curve at source frame t, clamped outside the key range."""
    if t <= keys[0]["frame"]:
        return pick(keys[0])
    if t >= keys[-1]["frame"]:
        return pick(keys[-1])
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid]["frame"] <= t:
            lo = mid
        else:
            hi = mid
    a, b = keys[lo], keys[hi]
    u = (t - a["frame"]) / max(1.0, b["frame"] - a["frame"])
    pa, pb = pick(a), pick(b)
    if isinstance(pa, tuple):
        return tuple(p + (q - p) * u for p, q in zip(pa, pb))
    return pa + (pb - pa) * u


def forward(rx, ry):
    """Where an MMD camera points: local +Z, pitch about X (positive looks down), yaw about Y."""
    cx, sx, cy, sy_ = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry)
    return (sy_ * cx, -sx, cy * cx)


def diva_point(pos, unit):
    """MMD position -> DIVA metres, with the axis mirror the ports use."""
    return (pos[0] * unit, pos[1] * unit, -pos[2] * unit)


def fov_rad(deg):
    """MMD's 視野角 is a half-angle; DIVA's fov channel is the full horizontal angle in radians."""
    return math.radians(min(MAX_FOV, max(MIN_FOV, float(deg))) * 2.0)


def shots(keys):
    """Split the source into shots at hard edits: a big fov step or a flip in pitch."""
    bounds = [0]
    for i in range(1, len(keys)):
        d_fov = abs(keys[i]["fov"] - keys[i - 1]["fov"])
        d_rx = abs(keys[i]["rot"][0] - keys[i - 1]["rot"][0])
        gap = keys[i]["frame"] - keys[i - 1]["frame"]
        if gap <= 2 and (d_fov > 8.0 or d_rx > 60.0):
            bounds.append(i)
    bounds.append(len(keys))
    return [(keys[a]["frame"], keys[b - 1]["frame"], keys[a:b]) for a, b in zip(bounds, bounds[1:])
            if b > a]


def _tracks(frames):
    """Six animated channels, keyed every frame with the shape pv_70204 ships."""
    def ch():
        return a3da.Channel(kind=3, kind_anim=3, max_frames=int(frames))
    return dict(trans=[ch() for _ in "xyz"], interest=[ch() for _ in "xyz"], fov=ch(), roll=ch())


def floor_clamp(v):
    """Keep a camera out of the stage floor; DIVA's y=0 is the floor and ports stay above it."""
    return min(6.0, max(0.12, v))


def build_world(keys, frames, unit, degrees):
    """Direct conversion: eye from the source position, target along the source aim."""
    scale = 180.0 / math.pi if degrees else 1.0
    # fold first, then interpolate: between +6.2 and -6.2 radians the camera holds its pose, it
    # does not spin half a turn backwards
    src = [dict(k, rot=tuple(wrap(v * scale) for v in k["rot"])) for k in keys]
    out = _tracks(frames)
    for f in range(frames):
        t = f / FPS * SRC_FPS
        pos = at(src, t, lambda k: k["pos"])
        rx, ry, rz = at(src, t, lambda k: k["rot"])
        deg = at(src, t, lambda k: k["fov"])
        ex, ey, ez = diva_point(pos, unit)
        ey = floor_clamp(ey)
        # aim depth = how far the performer is, so the target lands on her plane of depth
        d = min(60.0, max(10.0, math.hypot(pos[0], pos[1] - BODY_Y, pos[2]))) * unit
        fx, fy, fz = forward(rx, ry)
        out["trans"][0].add(f, ex)
        out["trans"][1].add(f, ey)
        out["trans"][2].add(f, ez)
        out["interest"][0].add(f, ex + fx * d)
        out["interest"][1].add(f, floor_clamp(ey + fy * d))
        out["interest"][2].add(f, ez + fz * d)
        out["fov"].add(f, fov_rad(deg))
        out["roll"].add(f, rz)
    return out


def build_follow(keys, frames, unit):
    """Keep the edit's cut rhythm and zoom, aim a camera that actually frames the singer.

    The singer stays at the origin, so a shot is described by where the camera stands (a yaw
    around her), how high, and how tight the lens is.  Yaw comes per shot from a front-biased
    cycle - she faces -Z, so every angle stays in the audience half of the stage - and the crop
    follows the source fov, which is what the original edit frames with.
    """
    out = _tracks(frames)
    ratio = FPS / SRC_FPS
    for si, (f0, f1, sub) in enumerate(shots(keys)):
        f1 = max(f1, f0 + 5)
        d0, d1 = int(round(f0 * ratio)), int(round(f1 * ratio))   # shot bounds on the 60 fps clock
        yaw0 = YAW_CYCLE[si % len(YAW_CYCLE)]
        drift = 7.0 * (1.0 if si % 2 else -1.0)
        base = 2.35 + 0.17 * (((si * 5) % 4) - 1.5)
        for f in range(d0, min(frames, d1 + 1)):
            u = (f - d0) / max(1.0, d1 - d0)
            deg = min(MAX_FOV, max(MIN_FOV, float(at(sub, f / FPS * SRC_FPS, lambda k: k["fov"]))))
            tight = (40.0 - deg) / 32.0                       # 1 = close-up, 0 = wide
            yaw = math.radians(yaw0 + drift * u)
            d = base + 0.35 - 0.5 * tight
            out["trans"][0].add(f, d * math.sin(yaw))
            out["trans"][1].add(f, 0.98 + 0.28 * tight)
            out["trans"][2].add(f, -d * math.cos(yaw))
            out["interest"][0].add(f, -0.12 * math.sin(yaw) * tight)
            out["interest"][1].add(f, 0.92 + 0.30 * tight)
            out["interest"][2].add(f, 0.12 * math.cos(yaw) * tight)
            out["fov"].add(f, fov_rad(deg))
            out["roll"].add(f, 0.0)
    return out


def write_a3da(out_path, pv_id, frames, built):
    """Assemble through the a3da module, whose writer round-trips shipped files byte-exact."""
    size = int(frames)
    got = a3da.new("CAMPV%d_BASE.a3da" % pv_id, size, ASPECT)
    P = "camera_root.0."
    for i, ax in enumerate("xyz"):
        got.channels[P + "view_point.trans." + ax] = built["trans"][i]
        got.channels[P + "interest.trans." + ax] = built["interest"][i]
    focal = a3da.Channel(kind=3, kind_anim=3, max_frames=size)
    for frame, horizontal_fov in built["fov"].keys:
        focal.add(frame, 1.67979002 / (2.0 * math.tan(horizontal_fov / 2.0)))
    got.channels[P + "view_point.focal_length"] = focal
    got.channels[P + "view_point.roll"] = built["roll"]
    got.props[P + "view_point.camera_aperture_w"] = "1.67979002"
    got.props[P + "view_point.camera_aperture_h"] = "0.944881916"
    channels = list(built["trans"]) + list(built["interest"]) + [focal, built["roll"]]
    for ch in channels:
        # shot boundaries can leave a frame or two unkeyed; the ports key every frame, and a
        # hole would make the game interpolate across it with whatever curve type it likes
        if not ch.keys:
            ch.kind = ch.kind_anim = 1
            ch.const = 0.0
            continue
        ch.keys = sorted(dict(ch.keys).items())
        ch.keys = [(f, ch.at(f)) for f in range(size)]
        ch.max_frames = size
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    return got.write(out_path)


def framing(built):
    """Widest and tightest shot in the output and what the lens sees there.

    Worth having as a check: a 0.2 m tall frame crops the singer to an eyeball and a 12 m one
    makes her a speck, and neither shows up in a range printout.
    """
    eye = list(zip(built["trans"][0].keys, built["trans"][1].keys, built["trans"][2].keys))
    if not eye:
        return []
    fov = dict(built["fov"].keys)

    def away(trip):
        return math.hypot(trip[0][1], trip[1][1] - 1.0, trip[2][1])

    rows = []
    for tag, trip in (("widest", max(eye, key=away)), ("tightest", min(eye, key=away))):
        d = math.hypot(trip[0][1], trip[2][1])
        h = math.degrees(fov[trip[0][0]])
        rows.append((tag, d, h, 2.0 * d * math.tan(math.radians(h) / 2.0)))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vmd", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pv", type=int, required=True)
    ap.add_argument("--frames", type=int, required=True)
    ap.add_argument("--mode", choices=("auto", "world", "follow"), default="auto")
    ap.add_argument("--unit", type=float, default=UNIT, help="MMD movement unit in metres")
    a = ap.parse_args(argv)

    keys, dialect = read_cam(a.vmd)
    if not keys:
        raise SystemExit("%s: no camera keys in this VMD" % a.vmd)
    degrees = rot_is_degrees(keys)
    world = looks_world_space(keys)
    mode = a.mode if a.mode != "auto" else ("world" if world else "follow")
    print("camera: %d key(s) (%s dialect), %d shot(s); rotation=%s, position reads as %s "
          "-> %s camera" % (len(keys), dialect, len(shots(keys)),
                            "degrees" if degrees else "radians",
                            "world-space" if world else "offset-only", mode))
    built = build_world(keys, a.frames, a.unit, degrees) if mode == "world" \
        else build_follow(keys, a.frames, a.unit)
    n = write_a3da(a.out, a.pv, a.frames, built)
    for label, tr in (("cam x", built["trans"][0]), ("cam y", built["trans"][1]),
                      ("cam z", built["trans"][2]), ("aim y", built["interest"][1]),
                      ("roll", built["roll"]), ("fov", built["fov"])):
        v = tr.values()
        print("   %-6s %+.3f .. %+.3f  (%d keys)" % (label, min(v), max(v), len(v)))
    for tag, d, h, tall in framing(built):
        print("   %s shot: %.1f m away, %.1f deg horizontal -> frame %.2f m tall (singer 1.6 m)"
              % (tag, d, h, tall))
    print("   -> %s (%d lines)" % (a.out, n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
