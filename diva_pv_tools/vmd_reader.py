"""MikuMikuDance .vmd reader.

Two dialects turn up in practice and both are handled; the file itself decides
which one it is, because the bone+morph+camera+light+shadow chain must land
exactly on the end of the file, which a wrong guess cannot do.

  classic   bone  64 B = name[20] frame pos3 rot3(euler) interp[16]
            morph 28 B = name[20] frame weight
  quat      bone 111 B = name[15] frame pos3 rot4(quaternion x,y,z,w) interp[64]
            morph 23 B = name[15] frame weight          (verified against shipped MMD vmd files)

The light, shadow and post-MMD-6 addendum blocks are deliberately not parsed: their record
sizes differ between exporters and nothing here reads them, so the chain only requires that
bones+morphs+camera end within a kilobyte of the end of file.

usage: python -m diva_pv_tools.vmd_reader <file.vmd> [...]
"""
import math
import struct
import sys

CAM_REC = struct.Struct("<I3f3fIB3x")       # frame, xyz, rxyz(euler), fov, perspective
# Reference Camera.vmd layout: frame, pos3, rot3, one more float, 24 interp bytes, fov u32, flag byte
CAM_REC_Q = struct.Struct("<I7f24xIB")

# (name length, record size, frame offset, pos offset, rot offset, rot float count, interp offset)
DIALECTS = {
    "classic": {"bone": (20, 64, 20, 24, 36, 3), "morph": (20, 28, 20, None, None, 0),
                "cam": CAM_REC},
    "quat": {"bone": (15, 111, 15, 19, 31, 4), "morph": (15, 23, 15, None, None, 0),
             "cam": CAM_REC_Q},
}


def _name(raw):
    z = raw.find(b"\x00")
    return raw[:len(raw) if z < 0 else z].decode("sjis", "replace")


def _chain(blob, dialect):
    bn, bs, bfr, bpos, brot, brotn = dialect["bone"]
    mn, ms, mfr, _, _, _ = dialect["morph"]
    p = 50
    (n,) = struct.unpack_from("<I", blob, p)
    p += 4
    if p + n * bs > len(blob):
        return None
    bones = []
    for _ in range(n):
        fr, = struct.unpack_from("<I", blob, p + bfr)
        pos = struct.unpack_from("<3f", blob, p + bpos)
        rot = struct.unpack_from("<%df" % brotn, blob, p + brot)
        if fr > 1 << 20 or any(v != v or abs(v) > 1e5 for v in pos + rot):
            return None
        if brotn == 4 and abs(math.sqrt(sum(v * v for v in rot)) - 1) > 0.02:
            return None
        bones.append({"frame": fr, "name": _name(blob[p:p + bn]), "pos": pos, "rot": rot})
        p += bs

    (n,) = struct.unpack_from("<I", blob, p)
    p += 4
    if p + n * ms > len(blob):
        return None
    morphs = []
    for _ in range(n):
        fr, w = struct.unpack_from("<If", blob, p + mfr)
        if fr > 1 << 20 or w != w or abs(w) > 2:
            return None
        morphs.append({"frame": fr, "name": _name(blob[p:p + mn]), "weight": w})
        p += ms

    def block(size, unpack):
        nonlocal p
        if p + 4 > len(blob):
            return []
        (n,) = struct.unpack_from("<I", blob, p)
        if n > 200000 or p + 4 + n * size > len(blob):
            return None
        p += 4
        out = []
        for _ in range(n):
            out.append(unpack(blob, p))
            p += size
        return out

    cam = dialect["cam"]

    def un_cam(bl, o):
        rec = {"frame": struct.unpack_from("<I", bl, o)[0]}
        if cam is CAM_REC:
            _, x, y, z, rx, ry, rz, fov, persp = cam.unpack_from(bl, o)
            rec.update({"pos": (x, y, z), "rot": (rx, ry, rz), "fov": float(fov)})
        else:
            vals = struct.unpack_from("<7f", bl, o + 4)
            rec.update({"pos": vals[0:3], "rot": vals[3:6], "extra": vals[6],
                        "fov": struct.unpack_from("<I", bl, o + 56)[0]})
        return rec

    blocks = []
    got = block(cam.size, un_cam)
    if got is None:
        return None
    # MMD 6 and later append blocks nobody here reads - the light and shadow lists, a
    # self-shadow colour, camera-to-bone parents, IPVMD rig data - and their record sizes vary
    # between exporters, so parsing them would only add a way to fail.  What matters is that the
    # three blocks above land within a kilobyte of the end of file; a wrong stride cannot get
    # this far, because the bone block is validated by its unit quaternions.
    if len(blob) - p > 1024:
        return None
    return {"bones": bones, "morphs": morphs, "camera": got,
            "lights": [], "shadow": [], "tail": len(blob) - p}


def read(path):
    blob = open(path, "rb").read()
    magic = blob[:30].rstrip(b"\x00").decode("ascii", "replace")
    model = _name(blob[30:50])
    for dname, dialect in DIALECTS.items():
        got = _chain(blob, dialect)
        if got:
            got.update({"magic": magic, "model": model, "dialect": dname})
            return got
    raise ValueError(f"{path}: no VMD dialect chain reaches EOF (magic={magic!r})")


# ---------------------------------------------------------------------------- incremental reading
# The interactive import cannot call `read()`: on a 30 MB file it is a second or two of unbroken work
# (measured), and more importantly the *result* of it - one dict per bone with its keyframes - is then
# walked again to build the action, which is the expensive half.  So the record loop is exposed as a
# generator of time-bounded groups, and the same validation is applied inside it.
#
# The one thing the incremental form cannot do is the fallback in `read`: `_chain` tries the classic
# dialect, discovers half way through that the quaternions are not unit length, and restarts with the
# quat dialect - which is impossible once records have been handed to a consumer.  `detect` therefore
# settles the dialect first, from the header and a sample of the records, and `records` commits to it.
# The sample is every 97th record rather than the first N: a stride coprime with the record size walks
# the whole block, so a file that only looks well formed at its start cannot pass.
SAMPLE_STRIDE = 97

# The 50-byte header (30 magic + 20 model) plus the 4-byte record count: the address of the first
# bone record.  Every offset in `DIALECTS` is relative to a record, so this is the base they are
# added to - being four bytes out here parses the whole file into plausible garbage, with frame
# numbers in the billions rather than an exception.
BONE_BASE = 50 + 4


def _record_ok_bone(blob, p, dialect):
    _bn, _bs, bfr, bpos, brot, brotn = dialect["bone"]
    fr, = struct.unpack_from("<I", blob, p + bfr)
    pos = struct.unpack_from("<3f", blob, p + bpos)
    rot = struct.unpack_from("<%df" % brotn, blob, p + brot)
    if fr > 1 << 20 or any(v != v or abs(v) > 1e5 for v in pos + rot):
        return False
    if brotn == 4 and abs(math.sqrt(sum(v * v for v in rot)) - 1) > 0.02:
        return False
    return True


def _record_ok_morph(blob, p, dialect):
    _mn, _ms, mfr, _, _, _ = dialect["morph"]
    fr, w = struct.unpack_from("<If", blob, p + mfr)
    return not (fr > 1 << 20 or w != w or abs(w) > 2)


def detect(blob):
    """Which dialect is this file, and how many records does each block hold?  `None` if neither.

    Cheap by construction: it unpacks the block headers and a fixed-size sample of records, never the
    whole file, and it answers the same question `read` answers by trying - without the retry that
    `records` cannot afford.

    The block addresses follow `_chain` exactly: the 50-byte header, then a 4-byte count, then the
    records.  `BONE_BASE` is the address of the *first record*, which is the count word plus four, and
    every offset in `DIALECTS` is relative to a record and not to the count in front of it.  Being one
    word out here does not raise: the records parse into plausible garbage and the frame numbers come
    out in the billions.
    """
    for dname, dialect in DIALECTS.items():
        bn, bs, _bfr, _, _, _ = dialect["bone"]
        mn, ms, _mfr, _, _, _ = dialect["morph"]
        p = BONE_BASE
        if p + 4 > len(blob):
            continue
        (n_bones,) = struct.unpack_from("<I", blob, BONE_BASE - 4)
        if p + n_bones * bs > len(blob):
            continue
        ok = True
        for k in range(0, n_bones, SAMPLE_STRIDE):
            if not _record_ok_bone(blob, p + k * bs, dialect):
                ok = False
                break
        if not ok:
            continue
        p += n_bones * bs
        if p + 4 > len(blob):
            continue
        (n_morphs,) = struct.unpack_from("<I", blob, p)
        p += 4
        if p + n_morphs * ms > len(blob):
            continue
        for k in range(0, n_morphs, SAMPLE_STRIDE):
            if not _record_ok_morph(blob, p + k * ms, dialect):
                ok = False
                break
        if not ok:
            continue
        p += n_morphs * ms
        # the camera block, and the "lands within a kilobyte of EOF" test `read` uses to reject a
        # wrong stride
        if p + 4 > len(blob):
            continue
        (n_cam,) = struct.unpack_from("<I", blob, p)
        cam_size = dialect["cam"].size
        if n_cam > 200000 or p + 4 + n_cam * cam_size > len(blob):
            continue
        p += 4 + n_cam * cam_size
        if len(blob) - p > 1024:
            continue
        return {"dialect": dname, "bones": n_bones, "morphs": n_morphs, "camera": n_cam,
                "tail": len(blob) - p, "bn": bn, "bs": bs, "mn": mn, "ms": ms,
                "bfr": dialect["bone"][2], "bpos": dialect["bone"][3], "brot": dialect["bone"][4],
                "brotn": dialect["bone"][5], "mfr": dialect["morph"][2]}
    return None


def records(blob, info, group=4096):
    """Yield time-bounded groups of bone and morph records, then the camera block.

    Same bytes, same order, same fields as `_chain` produces - the only difference is that the caller
    gets them in pieces instead of a finished dict, which is what lets a several-second parse be
    stopped, resumed and cancelled.  `info` comes from `detect`, so no dialect guessing happens here.
    """
    bn, bs = info["bn"], info["bs"]
    mn, ms = info["mn"], info["ms"]
    dialect = DIALECTS[info["dialect"]]
    _bn, _bs, bfr, bpos, brot, brotn = dialect["bone"]
    _mn, _ms, mfr, _, _, _ = dialect["morph"]
    # the offsets below are absolute within a record, exactly as `_chain` applies them, so `p` is the
    # first record's own address (`BONE_BASE`) and not the address of the count word in front of it
    p = BONE_BASE
    for start in range(0, info["bones"], group):
        batch = []
        for k in range(start, min(start + group, info["bones"])):
            off = p + k * bs
            batch.append({"frame": struct.unpack_from("<I", blob, off + bfr)[0],
                          "name": _name(blob[off:off + bn]),
                          "pos": struct.unpack_from("<3f", blob, off + bpos),
                          "rot": struct.unpack_from("<%df" % brotn, blob, off + brot)})
        yield "bones", batch
    p += info["bones"] * bs + 4
    for start in range(0, info["morphs"], group):
        batch = []
        for k in range(start, min(start + group, info["morphs"])):
            off = p + k * ms
            fr, w = struct.unpack_from("<If", blob, off + mfr)
            batch.append({"frame": fr, "name": _name(blob[off:off + mn]), "weight": w})
        yield "morphs", batch
    p += info["morphs"] * ms + 4
    cam = dialect["cam"]
    got = []
    for k in range(info["camera"]):
        off = p + k * cam.size
        rec = {"frame": struct.unpack_from("<I", blob, off)[0]}
        if cam is CAM_REC:
            _, x, y, z, rx, ry, rz, fov, persp = cam.unpack_from(blob, off)
            rec.update({"pos": (x, y, z), "rot": (rx, ry, rz), "fov": float(fov)})
        else:
            vals = struct.unpack_from("<7f", blob, off + 4)
            rec.update({"pos": vals[0:3], "rot": vals[3:6], "extra": vals[6],
                        "fov": struct.unpack_from("<I", blob, off + 56)[0],
                        # the 24 interpolation bytes mmd_tools splits over 6 channels of
                        # 4 bezier controls (0..127); cam_json needs them for the orbit model
                        "interp": bytes(blob[off + 32:off + 56])})
        got.append(rec)
    yield "camera", got
    yield "properties", read_properties(blob, p + info["camera"] * cam.size, info["tail"])


def read_properties(blob, p, tail):
    """The IK-toggle keyframes, from the property block at the end of an MMD 6+ file.

    Losing these is not a cosmetic loss: `mmd_ik_toggle` is the boolean that decides whether a leg is
    solved by its IK chain or held at its basis, so a file whose toggles are dropped imports as a
    dance whose knees never bend.  The layout is mmd_tools' (`PropertyFrameKey`): a frame number, a
    visibility byte, an IK count, then that many 20-byte names each followed by a state byte.

    The block is found from the *end*, not by walking the light and shadow lists, because their record
    sizes differ between exporters and nothing here reads them - the same reason `_chain` only requires
    the three blocks it does understand to land within a kilobyte of EOF.  A file with no property
    block, or one whose bytes do not parse as a whole number of records, returns `[]`: absent means
    "no IK keys", which is the normal state of a motion-only file.
    """
    out = []
    end = len(blob)
    # the property block is the last block, so its own count lives at the start of whatever remains
    # inside the tail budget; try each possible start rather than assuming a light/shadow size
    for start in range(p, end - 4):
        try:
            (count,) = struct.unpack_from("<I", blob, start)
            if count < 1 or count > 100000:
                continue
            q = start + 4
            frames = []
            for _ in range(count):
                fr, visible = struct.unpack_from("<Ib", blob, q)
                q += 5
                (n_ik,) = struct.unpack_from("<I", blob, q)
                q += 4
                if n_ik > 4096 or q + n_ik * 21 > end:
                    raise struct.error("ik count %d overruns the block" % n_ik)
                states = []
                for _k in range(n_ik):
                    states.append((_name(blob[q:q + 20]), bool(blob[q + 20])))
                    q += 21
                if fr > 1 << 20:
                    raise struct.error("implausible frame %d" % fr)
                frames.append({"frame": fr, "visible": bool(visible), "ik": states})
            # a candidate is accepted only if it consumes the bytes up to EOF exactly, or within the
            # same kilobyte slack the rest of this module allows - anything else is a false positive
            # that happened to look like a count
            if end - q <= 1024:
                return frames
        except (struct.error, IndexError):
            continue
    return out


def tracks(records):
    """Group morph records by name: ``{name: [{'frame', 'weight'}, ...]}``."""
    """Group keyframes into {name: [(frame, payload)...]} sorted by frame."""
    out = {}
    for r in records:
        out.setdefault(r["name"], []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["frame"])
    return out


def last_frame(path):
    """The last frame any morph keyframe in this VMD sits on.

    `export_face` needs it to size the output grid: a dance longer than its borrowed base
    script must not have its late cues clamped onto the base's last frame and dropped.
    """
    return max((r["frame"] for r in read(path)["morphs"]), default=0)


def summarize(path):
    d = read(path)
    print(f"{path}\n  magic={d['magic']!r} model={d['model']!r} dialect={d['dialect']}")
    for k in ("bones", "morphs", "camera", "lights", "shadow"):
        fr = [x["frame"] for x in d[k]] or [0]
        print(f"  {k:<7} n={len(d[k]):<7} frames {min(fr)}..{max(fr)}  tracks={len(tracks(d[k]))}")
    bt = tracks(d["bones"])
    if bt:
        print("  bones:")
        for n in sorted(bt, key=lambda k: -len(bt[k])):
            v = bt[n]
            r0 = v[0]["rot"]
            print(f"    {len(v):6d}  {n:<18} first rot={['%.3f' % x for x in r0]}")
    mt = tracks(d["morphs"])
    if mt:
        print("  morphs: " + ", ".join(f"{n}({len(v)})" for n, v in
                                       sorted(mt.items(), key=lambda kv: -len(kv[1]))))
    return d


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for a in sys.argv[1:]:
        summarize(a)
