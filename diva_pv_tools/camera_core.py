"""MMD Camera.vmd -> Project DIVA camera artifacts, with no Blender import at all.

This is the pure-python core a Blender add-on modal operator can call directly and a
headless test can import.  It owns two things and nothing else:

    export_camera_a3da(vmd, out_a3da, *, file_name=None, base_a3da=None, fps=None, scale=0.08,
                       min_y=None) -> stats
    describe(vmd) -> panel summary

The numbers are not re-derived here: the ``cam_json`` module in this package already converts
a camera VMD into the accepted ``Camera.json`` schema (validated against a hand-accepted
shipped ``Camera.json``), and it fixes the fact that the bundled VMD reader mis-reads the
61-byte camera record (it puts MMD's 距離 in ``pos.x``).  This module re-sorts the
already-decoded floats the way cam_json does -- it never re-parses bytes and never edits the
reader.  The a3da path is the same calibrated eye / aim / roll / 視野角 samples laid into a
DIVA ``.a3da`` (the idiom the ``camera_a3da`` module shows the game actually loads: dense one
key per frame, ``view_point.focal_length`` + ``camera_aperture_w/h``).  It is not wrapped in
a FArC: the camera this branch writes is a loose ``.a3da``, and packing is the job of the
``farctool`` module that already knows the member names.

Every public name raises ``CameraError`` with a plain English message so the UI layer can map
it to a translation; no UI text lives here.

Conventions inherited from cam_json (see its docstring for the calibration evidence):
    unit        DIVA metre = MMD movement unit * scale (default 0.08)
    z mirror    DIVA z = -MMD z
    fps         an MMD project is 30 fps, DIVA is 60, so a source key at frame f lands on
                output frame f * (60 / source_fps); PlayControl.Size = last_frame * step + 1
    fov         MMD's 視野角 is a half angle, FOV / focal_length is the full horizontal angle
    roll        the camera's Z rotation in radians, no sign flip

usage: python -m diva_pv_tools.camera_core a3da    <Camera.vmd> <out.a3da> [--name FILE_NAME] [--base IN.a3da]
       python -m diva_pv_tools.camera_core describe <Camera.vmd>
"""
import math
import os
import sys
import tempfile

# ------------------------------------------------------------------ import the read-only tools
# Everything this module needs is bundled next to it (cam_json / a3da / camera_a3da / vmd_reader),
# so the add-on works from any install directory.
from . import cam_json as cam_json                                             # noqa: E402
from . import a3da as a3da                                                 # noqa: E402
from . import camera_a3da as camera_a3da                                          # noqa: E402

# The 3DPV ports' film, as camera_a3da.write_a3da emits it.
# the shipped cameras carry view_point.fov (+ fov_is_horizontal) and no aperture at all; these are
# kept only so a caller can ask for the aperture a given focal length implies
APERTURE_W_IN = 1.67979002   # 36 mm sensor, the idiom the shipped ports preserve
APERTURE_H_IN = 0.944881916  # 16:9 at that width
SRC_FPS_DEFAULT = cam_json.SRC_FPS                          # 30.0, the MMD project rate
DST_FPS = cam_json.DST_FPS                                  # 60.0, DIVA always plays 60
FOV_BAND = cam_json.FOV_BAND                                # (8.0, 55.0) degrees, half angle


class CameraError(Exception):
    """A conversion could not be done.  The message is English; the UI maps it to a locale."""


# ------------------------------------------------------------------ shared: read + sample once
def _samples(vmd_path, source_fps, scale, min_y, layout="auto", band=FOV_BAND):
    """(keys, info, rows, size) for one VMD, with the source rate and floor applied.

    cam_json reads the module-level SRC_FPS / FRAME_STEP for its 30->60 doubling, so a
    non-default source rate is set on the module for the duration of the call and restored
    afterwards; the default (30) leaves the globals untouched, which is the accepted path.
    """
    if not os.path.exists(vmd_path):
        raise CameraError("camera source not found: %s" % vmd_path)
    patch = source_fps is not None and abs(source_fps - SRC_FPS_DEFAULT) > 1e-9
    old = (cam_json.SRC_FPS, cam_json.FRAME_STEP)
    if patch:
        if source_fps <= 0:
            raise CameraError("source fps must be positive, got %r" % source_fps)
        cam_json.SRC_FPS = float(source_fps)
        cam_json.FRAME_STEP = max(1, int(round(DST_FPS / float(source_fps))))
    try:
        keys, info = cam_json.read_camera(vmd_path, layout)
        rows, size = cam_json.sample_streams(keys, info, unit=scale, band=band, min_y=min_y)
    except ValueError as exc:                                # cam_json raises ValueError, not ours
        raise CameraError(str(exc))
    finally:
        if patch:
            cam_json.SRC_FPS, cam_json.FRAME_STEP = old
    return keys, info, rows, size


def _step(fps):
    return max(1, int(round(DST_FPS / float(fps if fps is not None else SRC_FPS_DEFAULT))))


# ------------------------------------------------------------------ a3da assembly (reuses camera_a3da)
def _rows_to_channels(rows, size):
    """Fill camera_a3da._tracks()'s channels from cam_json's per-frame rows."""
    built = camera_a3da._tracks(size)
    for f, r in enumerate(rows):
        built["trans"][0].add(f, r["eye"][0])
        built["trans"][1].add(f, r["eye"][1])
        built["trans"][2].add(f, r["eye"][2])
        built["interest"][0].add(f, r["aim"][0])
        built["interest"][1].add(f, r["aim"][1])
        built["interest"][2].add(f, r["aim"][2])
        built["fov"].add(f, r["fov"])          # already radians (cam_json.fov_rad)
        built["roll"].add(f, r["roll"])
    return built


def _build_a3da(rows, size, file_name, base_a3da=None):
    """Assemble the .a3da text in the shape the shipped cameras use: dense type-3 tracks for the
    eye, aim and roll, and the field of view as view_point.fov (+ fov_is_horizontal)."""
    built = _rows_to_channels(rows, size)
    P = "camera_root.0."
    if base_a3da:
        if not os.path.exists(base_a3da):
            raise CameraError("base a3da template not found: %s" % base_a3da)
        try:
            got = a3da.A3DA.read(base_a3da)
        except Exception as exc:
            raise CameraError("could not read base a3da %s: %s" % (base_a3da, exc))
        got.props["_.file_name"] = file_name
        got.props["play_control.size"] = str(int(size))
    else:
        got = a3da.new(file_name, int(size), camera_a3da.ASPECT)
    for i, ax in enumerate("xyz"):
        got.channels[P + "view_point.trans." + ax] = built["trans"][i]
        got.channels[P + "interest.trans." + ax] = built["interest"][i]
    # The field of view goes out as view_point.fov in radians, horizontal (SEGA's own cameras:
    # 229/229 sampled members carry view_point.fov + fov_is_horizontal, 114 of them constant and
    # 111 animated; none of them has focal_length or camera_aperture_*).  Writing focal_length
    # instead leaves the loader without a field of view at all, even while every position channel
    # still looks right.
    fovs = built["fov"]
    if cam_json.is_static(fovs.values()):
        got.channels[P + "view_point.fov"] = a3da.Channel(kind=1, kind_anim=1, const=fovs.values()[0])
    else:
        got.channels[P + "view_point.fov"] = fovs
    got.props[P + "view_point.fov_is_horizontal"] = "1"
    got.channels.pop(P + "view_point.focal_length", None)
    got.props.pop(P + "view_point.camera_aperture_w", None)
    got.props.pop(P + "view_point.camera_aperture_h", None)
    got.channels[P + "view_point.roll"] = built["roll"]
    # only the animated tracks get re-densified; a constant must keep its single value (running the
    # loop below over a kind-1 channel would zero it out through the "no keys" branch)
    animated = list(built["trans"]) + list(built["interest"]) + [built["roll"]]
    if got.channels[P + "view_point.fov"].animated:
        animated.append(got.channels[P + "view_point.fov"])
    for ch in animated:
        if not ch.keys:
            ch.kind = ch.kind_anim = 1
            ch.const = 0.0
            continue
        ch.keys = sorted(dict(ch.keys).items())
        ch.keys = [(f, ch.at(f)) for f in range(int(size))]
        ch.max_frames = int(size)
    return got, built


def export_camera_a3da(vmd_path, out_a3da_path, *, file_name=None, base_a3da=None,
                       fps=None, scale=cam_json.UNIT, min_y=None, cancel=None):
    """Convert VMD -> a3da directly (the form the panel exports; no FArC wrapper).

    ``file_name`` is the name written into the a3da's own ``_.file_name`` property, which is what the
    DIVA camera loader reads; None derives ``<vmd stem>.a3da``.  ``base_a3da`` is an optional existing
    a3da whose scaffold and props the output mirrors.  The text is written to a temp name, re-read with
    ``a3da.A3DA.read`` and compared channel-by-channel against what was built, and only then moved onto
    ``out_a3da_path`` - a file that fails validation never replaces anything.

    ``cancel`` is an optional zero-argument predicate, checked once immediately before the publish
    step: a cancel in the middle of a conversion costs the work already done, but it must not cost
    the file the user already had.  The temp name is removed either way.
    """
    keys, info, rows, size = _samples(vmd_path, fps, scale, min_y)
    if file_name is None:
        file_name = os.path.splitext(os.path.basename(vmd_path))[0] + ".a3da"
    got, built = _build_a3da(rows, size, file_name, base_a3da)
    out_a3da_path = os.path.abspath(out_a3da_path)
    if os.path.dirname(out_a3da_path):
        os.makedirs(os.path.dirname(out_a3da_path), exist_ok=True)
    tmp = out_a3da_path + ".%d.tmp" % os.getpid()
    try:
        got.write(tmp)
        _read_and_validate_a3da(tmp, built, size, info["layout"])
        if cancel is not None and cancel():
            raise CameraError("camera export was cancelled before publishing: %s was left exactly "
                              "as it was" % os.path.basename(out_a3da_path))
        os.replace(tmp, out_a3da_path)
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)
    v = built["trans"][1].values()
    return {"vmd": os.path.abspath(vmd_path), "out": out_a3da_path,
            "bytes": os.path.getsize(out_a3da_path), "file_name": file_name, "size": int(size),
            "keys": info["keys"], "layout": info["layout"], "dialect": info["dialect"],
            "rot_unit": info["rot_unit"],
            "channels": {n: ("Static" if cam_json.is_static(b.values()) else "Hermite")
                         for n, b in (("ViewPoint.Trans.X", built["trans"][0]),
                                       ("ViewPoint.Trans.Y", built["trans"][1]),
                                       ("ViewPoint.Trans.Z", built["trans"][2]),
                                       ("Interest.Trans.X", built["interest"][0]),
                                       ("Interest.Trans.Y", built["interest"][1]),
                                       ("Interest.Trans.Z", built["interest"][2]),
                                       ("ViewPoint.Roll", built["roll"]),
                                       ("ViewPoint.FOV", built["fov"]))},
            "view_y_band": [min(v), max(v)] if v else [0.0, 0.0]}


def _read_and_validate_a3da(path, built, size, layout):
    """Re-parse the a3da we just wrote and prove the six tracks came back matching the rows."""
    blob = open(path, "rb").read()
    got = a3da.A3DA.read(path)
    P = "camera_root.0."
    checks = [(P + "view_point.trans." + ax, built["trans"][i]) for i, ax in enumerate("xyz")]
    checks += [(P + "interest.trans." + ax, built["interest"][i]) for i, ax in enumerate("xyz")]
    checks += [(P + "view_point.roll", built["roll"])]
    worst = 0.0
    # the field of view comes back either as a dense track or as one constant, exactly as the shipped
    # cameras do it, so it is checked on its own terms instead of as a track that must have keys
    fov_ref, fov_got = built["fov"], got.channels.get(P + "view_point.fov")
    if fov_got is None:
        raise CameraError("a3da lost channel %sview_point.fov on write/read (layout=%s)" % (P, layout))
    if fov_got.animated:
        fov_written = dict(fov_got.keys)
        for f, want in fov_ref.keys:
            worst = max(worst, abs(fov_written.get(f, float("nan")) - want))
    else:
        worst = max(worst, abs(fov_got.const - fov_ref.values()[0]))
    for name, ref in checks:
        ch = got.channels.get(name)
        if ch is None or not ch.keys:
            raise CameraError("a3da lost channel %s on write/read (layout=%s)" % (name, layout))
        rd = dict(ch.keys)
        for f, want in ref.keys:
            worst = max(worst, abs(rd.get(f, float("nan")) - want))
    if not (worst < 5e-6):
        raise CameraError("a3da value drifted %.3g on write/read - the .10g encoding lost a value"
                          % worst)
    return blob


# ------------------------------------------------------------------ describe (what the panel shows first)
def describe(vmd_path):
    """A summary the panel can render *before* converting; converts nothing to disk."""
    _, _, raw, _ = _samples(vmd_path, None, cam_json.UNIT, None)
    try:
        doc, info = cam_json.build_camera_report(vmd_path)
    except ValueError as exc:
        raise CameraError(str(exc))
    roll = info["bands"]["roll"]
    vp_y = info["bands"]["viewpoint.y"]
    in_y = info["bands"]["interest.y"]
    raw_min_y = min(min(r["eye"][1] for r in raw), min(r["aim"][1] for r in raw))
    return {
        "vmd": os.path.abspath(vmd_path),
        "dialect": info["dialect"],
        "model": info["model"],
        "source_fps": float(SRC_FPS_DEFAULT),
        "diva_fps": float(DST_FPS),
        "frame_step": cam_json.FRAME_STEP,
        "first_frame": info["first_frame"],
        "last_frame": info["last_frame"],
        "frame_count": info["keys"],
        "frame_count_including_duplicates": info["keys_in"],
        "output_size": info["size"],
        "fov_deg_range": list(info["fov_deg"]),
        "fov_clamped_to_band": bool(info["fov_deg"][0] < FOV_BAND[0]
                                    or info["fov_deg"][1] > FOV_BAND[1]),
        "fov_band": list(FOV_BAND),
        "roll_animated": info["channels"]["ViewPoint.Roll"] != "Static",
        "roll_range_rad": [roll[0], roll[1]],
        "any_y_below_floor": bool(min(vp_y[0], in_y[0]) < 0.0),
        "lowest_y_metres": raw_min_y,
        "record_layout_matched": bool(info["record_has_distance_slot"]),
        "record_layout": info["layout"],
        "rotation_unit": info["rot_unit"],
        "distance_range": list(info["dist"]),
        "channels": info["channels"],
    }


# ------------------------------------------------------------------ CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] not in ("a3da", "describe"):
        sys.stderr.write(__doc__.split("usage:", 1)[-1])
        return 2
    cmd, src = argv[0], argv[1]
    try:
        if cmd == "describe":
            d = describe(src)
            print("source   %s  (%s dialect, model %r)"
                  % (os.path.basename(d["vmd"]), d["dialect"], d["model"]))
            print("frames   %d key(s) over %d..%d source frames -> Size=%d at %g fps (x%d)"
                  % (d["frame_count"], d["first_frame"], d["last_frame"], d["output_size"],
                     d["diva_fps"], d["frame_step"]))
            print("record   layout=%s (distance slot: %s), rotation=%s, 距離 %g..%g"
                  % (d["record_layout"], d["record_layout_matched"], d["rotation_unit"],
                     d["distance_range"][0], d["distance_range"][1]))
            print("視野角    %g..%g deg half-angle%s"
                  % (d["fov_deg_range"][0], d["fov_deg_range"][1],
                     "  (CLAMPED into band %g..%g)" % tuple(d["fov_band"])
                     if d["fov_clamped_to_band"] else ""))
            print("roll     %s  (%.3f..%.3f rad)"
                  % ("animated" if d["roll_animated"] else "static",
                     d["roll_range_rad"][0], d["roll_range_rad"][1]))
            print("floor    %s  (lowest eye/aim y %+.3f m)"
                  % ("y<0 clipping risk" if d["any_y_below_floor"] else "clear of the floor",
                     d["lowest_y_metres"]))
            return 0
        if len(argv) < 3:
            sys.stderr.write("usage: python -m diva_pv_tools.camera_core %s <vmd> <out>\n" % cmd)
            return 2
        dst = argv[2]
        opts = argv[3:]
        name = opts[opts.index("--name") + 1] if "--name" in opts else None
        base = opts[opts.index("--base") + 1] if "--base" in opts else None
        st = export_camera_a3da(src, dst, file_name=name, base_a3da=base)
        print("a3da     %s -> %s  (%d bytes, %s, Size=%d, %d channels)"
              % (os.path.basename(src), st["out"], st["bytes"], st["file_name"], st["size"],
                 len(st["channels"])))
        return 0
    except CameraError as exc:
        sys.stderr.write("camera error: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
