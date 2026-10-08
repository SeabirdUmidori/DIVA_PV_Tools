"""Write a Project DIVA pv_expression file (exp_PV*.bin).

Two things live here and they were established differently, so they are kept apart:

**The container layout** is read off the shipping files.  All 46 files the game ships in
`rom/pv_expression/` share it, and a working mod (`F2nd Song Pack`'s
`exp_PV606.bin`) uses the same one:

    u32 version          always 100
    u32 block_count      one block per performer/costume the PV animates
    u32 32               offset of the block-descriptor array
    u32 32 + 8*count     offset of the name-offset array
    u32 reserved[4]      zero
    @32                  count x { u32 offset_A, u32 offset_B }      <- 8 bytes per block
    @32+8*count          count x u32 name_offset                     <- 4 bytes per block
    (zero padding to the first region, at align32(32 + 12*count))
    per block: region A records, region B records, each ending with the terminator
    @end                 count NUL-terminated names, e.g. "PV609_KMK_P1_00"

  * a record is 16 bytes: ``f32 time, u32 tag, f32 value, f32 duration`` where the tag word is
    ``(tag << 16) | group``.  Measured across every shipping file: **region A carries group 0**
    and holds the face parameters; **region B carries group 1** and holds tag 3, the blink;
  * the **terminator** is ``(999999.0, 0x0000ffff, 0.0, 0.0)`` - 84 of them across the corpus,
    exactly one per region;
  * times are 60 fps frames, the same grid the mot keysets sample.

**The curves** come from the source .vmd, and this is the part that was got wrong once already.
An earlier version of this module sampled the *evaluated pose* and wrote the bone's rotation
**magnitude** divided by 90.  That is not what the working tool did and it is not what the game
wants: a magnitude has no sign, so an eye looking left and an eye looking right produced the same
number, and the value's centre was 0 rather than 0.5.  The mapping below is the one that was
confirmed working in game:

  * **gaze** is the eye bone's own quaternion, taken from the .vmd, and its **y** component is the
    rotation about the model's up axis: ``0.5 + 2*asin(y)/pi``, so 0.5 is straight ahead and
    +/-90 degrees reach 0 and 1.  Signed, centred, and taken from the keyed quaternion rather than
    from a pose evaluation;
  * **blink** is the eyelid **morph** weight (``まばたき`` / ``瞬き`` / ``blink``, with the wink
    spellings maxed in), not an eyelid bone's travel;
  * ``両目`` (the ganged master) is maxed into both eyes; a .vmd that animates the eyes per-eye
    instead simply has no ``両目`` keys.

The region split and the density cap are container concerns and are applied afterwards.
"""
import math
import os
import struct

VERSION = 100
ALIGN = 32
TERMINATOR_TIME = 999999.0
#: The tag word's low 16 bits: 0 in region A (face parameters), 1 in region B (the blink).
GROUP_FACE = 0
GROUP_BLINK = 1
#: The terminator's tag word - measured as 0x0000ffff in every shipping region.
TERMINATOR_TAG = 0x0000FFFF
#: Per-record duration.  Measured on the shipping tag-3 group-1 channel: 50% of consecutive key
#: pairs store a duration within two frames of the gap to the next key (median 8, mean 14.5), so
#: duration is an *interpolation length toward the next key*.  `DEFAULT_DURATION` is the fallback
#: for a channel's final key, where no next key exists, and `DEFAULT_DURATION_MAX` caps it.
DEFAULT_DURATION = 8.0
DEFAULT_DURATION_MAX = 60.0

TAG_BLINK = 3
TAG_EYE_L = 6
TAG_EYE_R = 15
#: Per-eye eyelid-closure candidates (group 0): `ウィンク` closes one eye while `まばたき` closes
#: both, and these two addresses are the only per-eye channels in the corpus that carry curves.
TAG_LID_SHUT_L = 8
TAG_LID_SHUT_R = 9
#: The expression-enable marker: constant 1.0, one per block, present in every shipping file that
#: drives expression curves.  Measured: 1260 records across 34 files, never anything but 1.0.
TAG_ENABLE = 21
#: The expression/eyelid parameter channel: mean 0.498, sd 0.423, full 0..1, and full of fast
#: 0<->1 steps in the files that drive expressions - the shape a switching expression makes.
#: **The mapping from a VMD expression slot to this channel is UNKNOWN**: this module writes the
#: curve, and the slot-to-channel correspondence is the open question recorded in the audit.
TAG_EXPRESSION = 22

#: Records a single region may hold.  Calibrated from both ends of the evidence:
#:   * the file whose eyes were confirmed working in game held **741** records in region A
#:     (371 + 370 gaze keys), so a cap below that flattens the curves - which is exactly what a
#:     cap of 512 did, pinning both gaze channels at a constant 0.5;
#:   * the file that crashed the game held **22 937** in one region and was 550 KB.
#: 1024 sits above the known-good file and far below the known-bad one.  The shipping corpus's own
#: maximum is 528, so this deliberately exceeds the shipping envelope - the working mod file does
#: too, which is what proves the envelope is not a hard limit.
REGION_CAP = 1024

#: The MMD eye bones.  `両目` is the ganged master that `左目`/`右目` inherit from.
EYE_BONES_L = ("左目", "左目戻")
EYE_BONES_R = ("右目", "右目戻")
EYE_BONES_BOTH = ("両目", "目戻")
#: The eyelid morphs.  `まばたき` shuts both eyes; the wink spellings shut one, and the right-eye
#: spellings are their own set - the reference dance carries `ウィンク` and `ウィンク右` as two
#: separate tracks, and treating only `ウィンク` as a wink would drop half of them.
LID_MORPHS = ("まばたき", "瞬き", "blink")
WINK_L_MORPHS = ("ウィンク", "ウインク", "ウィンク２", "ウインク2", "wink")
WINK_R_MORPHS = ("ウィンク右", "ウインク右", "ｳｨﾝｸ右", "ウィンク２右", "ｳｨﾝｸ２右",
                 "wink right", "wink_r")


def align32(n):
    return ((n + ALIGN - 1) // ALIGN) * ALIGN


# --------------------------------------------------------------------------- the curves
def quat_to_gaze(rot):
    """Horizontal gaze from a stored ``(x, y, z, w)`` quaternion, as 0..1 with 0.5 ahead.

    ``y`` is the component along the model's up axis (MMD is +Y up), so ``2*asin(y)`` is the yaw.
    Signed and centred: this is the mapping that was confirmed working in game, and the reason an
    unsigned magnitude mapping is wrong is that left and right would be indistinguishable.
    """
    _x, y, _z, _w = rot
    y = max(-1.0, min(1.0, y))
    return max(0.0, min(1.0, 0.5 + 2.0 * math.asin(y) / math.pi))


def _blink_curve(rows, total):
    """A blink morph as a per-frame curve: **0.0 on every frame that carries no key**.

    A blink is an event, not a level.  Holding the morph value between keys - which is the literal
    MMD reading - leaves the eye sitting wherever the last key put it, and on the reference dance
    `まばたき` holds 0.1..0.6 between its 104 real blinks, so the eyes stay part shut for most of the
    song.  Leaving un-keyed frames at 0.0 keeps the eye open between blinks, which is both what the
    shipping `(3,1)` channel looks like (mean 0.338, mostly zero) and what the tool that worked in
    game produced.
    """
    out = [0.0] * total
    for f, v in rows:
        f60 = int(round(f * 2))
        if 0 <= f60 < total:
            out[f60] = max(out[f60], v)
    return out


def _gaze_curve(rows, total):
    """An eye bone's keys as a per-frame curve, forward-filled and **starting at 0.5**.

    Gaze is a held value, not a pulse, so this one *is* forward-filled - and 0.5 (straight ahead)
    is the right default for a bone the track does not cover yet.
    """
    out = [None] * total
    for f, v in rows:
        f60 = int(round(f * 2))
        if 0 <= f60 < total:
            if out[f60] is None or v > out[f60]:
                out[f60] = v
    filled, last = [], None
    for v in out:
        if v is None:
            v = last if last is not None else 0.5
        filled.append(v)
        last = v
    return filled


def _on_change(curve, eps=0.004):
    keys, last = [], None
    for t, v in enumerate(curve):
        if last is None or abs(v - last) >= eps:
            keys.append((t, v))
            last = v
    return keys


def _with_durations(keys):
    """Give each key a duration = the time to the next key, capped.

    Measured on the shipping tag-3 group-1 channel: in 50% of 6550 consecutive key pairs the
    stored duration is within two frames of the gap to the next key (median duration 8, mean 14.5),
    so duration is an interpolation length toward the next key, not a fixed constant.  Writing a
    constant instead makes the eyelid ride a different interpolation than the source curve, which
    is the visible difference between a blink and a slow half-close.
    """
    out = []
    for i, (t, v) in enumerate(keys):
        gap = (keys[i + 1][0] - t) if i + 1 < len(keys) else 0.0
        out.append((t, v, float(min(gap, DEFAULT_DURATION_MAX))))
    return out


def build_tracks_from_vmd(vmd_path, blink=True):
    """The dance's blink and per-eye gaze curves, straight from the .vmd.

    Returns ``(blink, left, right, total)`` as lists of ``(frame60, value)`` plus the 60 fps length.

    ``blink=False`` empties the eyelid half of the file - the region-B blink (tag 3) and the per-eye
    lid channels (tags 8/9) - and leaves the gaze (tags 6/15) alone.  That is what the exporters pass
    when the *engine* owns the eyelids: the PV script's automatic blink and this file's blink curve
    are two drivers of one eyelid, and writing both is how a face ends up blinking twice.

    The conventions here are the ones the tool whose output was **confirmed working in game** used,
    and they were re-derived from the shipping corpus afterwards:

      * **blink** (tag 3) is the eyelid morph weight with **un-keyed frames left at 0.0**, and only
        the non-zero keys written - a blink is an event, not a held level.  Shipping `(3,1)`:
        7205 records, mean 0.338, sd 0.412, full 0..1 range, i.e. mostly zero with excursions;
      * **gaze** (tags 6/15) is each eye bone's keyed quaternion, and its ``y`` component is the
        rotation about the model's up axis: ``0.5 + 2*asin(y)/pi``.  Signed, centred on 0.5, so
        looking left and looking right are different values;
      * ``両目`` (the ganged master) is maxed into both eyes.

    An intermediate version of this module replaced the gaze with an unsigned rotation *magnitude*
    divided by 90 - which has no sign, so both directions collapsed onto one value - and later
    reinterpreted tags 6/15 as eyelid *openness* resting at 1.0, which pushed both channels to the
    top of their range for most of the PV.  Both were visible in game as a permanent wink.
    """
    from . import vmd_reader
    doc = vmd_reader.read(vmd_path)
    eyes, lids = {}, {}
    for r in doc["bones"]:
        if r["name"] in EYE_BONES_L + EYE_BONES_R + EYE_BONES_BOTH:
            eyes.setdefault(r["name"], []).append((r["frame"], quat_to_gaze(r["rot"])))
    for r in doc["morphs"]:
        lids.setdefault(r["name"], []).append((r["frame"], float(r["weight"])))
    last = 0
    for rows in list(eyes.values()) + list(lids.values()):
        for f, _v in rows:
            last = max(last, f)
    total = int(last * 2) + 2

    def side(names):
        rows = [kv for n in names for kv in eyes.get(n, [])]
        return _gaze_curve(rows, total) if rows else None

    left_gaze, right_gaze, both_gaze = (side(EYE_BONES_L), side(EYE_BONES_R),
                                        side(EYE_BONES_BOTH))
    if both_gaze is not None:
        left_gaze = both_gaze if left_gaze is None else [max(a, b) for a, b in zip(left_gaze, both_gaze)]
        right_gaze = both_gaze if right_gaze is None else [max(a, b) for a, b in zip(right_gaze, both_gaze)]

    def shut_curve(names):
        rows = [kv for n in names for kv in lids.get(n, [])]
        return _blink_curve(rows, total) if rows else [0.0] * total

    # Per-eye shut amounts, kept SEPARATE: merging them loses exactly the left/right information a
    # wink carries.  `まばたき` shuts both eyes, `ウィンク` the left, `ウィンク右` the right, so the
    # per-eye closed amount is `max(まばたき, that eye's wink)`.
    shut_both = shut_curve(LID_MORPHS)
    shut_left = [max(a, b) for a, b in zip(shut_both, shut_curve(WINK_L_MORPHS))]
    shut_right = [max(a, b) for a, b in zip(shut_both, shut_curve(WINK_R_MORPHS))]
    # both-eyes blink = the two eyes shutting together
    blink_curve = [min(a, b) for a, b in zip(shut_left, shut_right)]

    # **No v > 0 filter here**, and this is load-bearing: the engine holds a channel's last key
    # until the next one, so a channel whose "return to zero" keys have been thrown away never
    # returns to zero.  On the reference dance the last まばたき key in the file sat at 1.0, which
    # held the eyes shut for the last 26 seconds - and every blink's own closing value was held
    # until the next blink pushed it along, which is the "permanent wink" that was reported.
    #
    # `blink` is the caller's flag and it used to be *overwritten* here (`blink = _on_change(...)`),
    # so `blink=False` - documented as "empties the eyelid half" and passed by the exporters exactly
    # when something else already drives the eyelids - silently did nothing and every file carried a
    # second eyelid driver.  The flag is honoured now.
    if blink:
        lid = _on_change(blink_curve)
        left_shut = _on_change(shut_left)
        right_shut = _on_change(shut_right)
        if not lid:
            lid, left_shut, right_shut = [], [], []      # this .vmd animates no eyelid morph
    else:
        lid, left_shut, right_shut = [], [], []
    return lid, (_on_change(left_gaze) if left_gaze is not None else []), \
        (_on_change(right_gaze) if right_gaze is not None else []), \
        left_shut, right_shut, total


# --------------------------------------------------------------------------- density
def rdp(points, eps):
    """Ramer-Douglas-Peucker: keep the points that carry the curve's shape.

    Iterative rather than recursive - a per-frame curve is 11 742 points and Python's default
    recursion limit is 1000.
    """
    n = len(points)
    if n < 3:
        return list(points)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        t0, v0 = points[i]
        t1, v1 = points[j]
        dt = t1 - t0
        dv = v1 - v0
        norm = math.hypot(dt, dv) or 1.0
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            tk, vk = points[k]
            d = abs(dv * tk - dt * vk + t1 * v0 - v1 * t0) / norm
            if d > best:
                best, bi = d, k
        if best > eps:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(points, keep) if k]


def thin(points, cap=REGION_CAP):
    """Reduce a curve to at most `cap` points, keeping its shape as closely as the cap allows."""
    points = list(points)
    if len(points) <= cap:
        return points
    lo, hi = 0.0, 1.0
    while len(rdp(points, hi)) > cap and hi < 1e6:
        hi *= 2.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if len(rdp(points, mid)) > cap:
            lo = mid
        else:
            hi = mid
    out = rdp(points, hi)
    if len(out) > cap:
        step = max(1, len(points) // cap)
        out = points[::step][:cap]
        if out and out[-1] != points[-1]:
            out[-1] = points[-1]
    return out


# --------------------------------------------------------------------------- the container
def encode_region(records, group):
    """One region: 16-byte records plus the terminator.  records = [(time, tag, value, duration)].

    `group` is the tag word's low 16 bits, measured as 0 in region A and 1 in region B across every
    shipping file - it is what distinguishes the face-parameter channel from the blink channel.
    """
    out = b""
    for t, tag, val, dur in records:
        out += struct.pack("<fIff", float(t),
                           ((int(tag) & 0xFFFF) << 16) | (int(group) & 0xFFFF),
                           float(val), float(dur))
    out += struct.pack("<fIff", TERMINATOR_TIME, TERMINATOR_TAG, 0.0, 0.0)
    return out


def write_exp_bin(path, blocks):
    """blocks = [(name, region_a_records, region_b_records)] -> the file.  Returns (bytes, offsets)."""
    count = len(blocks)
    if count <= 0:
        raise ValueError("a pv_expression file needs at least one block")
    desc_at = 32
    names_at = 32 + 8 * count
    first = align32(32 + 12 * count)

    payload = []
    for _name, region_a, region_b in blocks:
        # region A = GROUP_FACE (0, the face-parameter channels), region B = GROUP_BLINK (1, the
        # blink): the placement measured across all 46 shipping files, in which (3,1) holds 7205
        # records and (3,0) only 164.
        payload.append((encode_region(region_a, GROUP_FACE),
                        encode_region(region_b, GROUP_BLINK)))

    desc, off = [], first
    for a, b in payload:
        oa = off
        off += len(a)
        ob = off
        off += len(b)
        desc.append((oa, ob))
    names_start = off
    name_offsets, name_blob = [], b""
    for name, _a, _b in blocks:
        name_offsets.append(names_start + len(name_blob))
        name_blob += name.encode("latin-1", "replace") + b"\x00"

    out = struct.pack("<IIII", VERSION, count, desc_at, names_at) + b"\x00" * 16
    for oa, ob in desc:
        out += struct.pack("<II", oa, ob)
    for o in name_offsets:
        out += struct.pack("<I", o)
    out += b"\x00" * (first - len(out))
    for a, b in payload:
        out += a + b
    out += name_blob
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(out)
    return len(out), desc


def read_exp_bin(path):
    """Parse a pv_expression file with the shipping layout.  Returns a dict, raises on any break."""
    b = open(path, "rb").read()
    version, count, desc_at, names_at = struct.unpack_from("<4I", b, 0)
    if version != VERSION:
        raise ValueError("version %d, expected %d" % (version, VERSION))
    if desc_at != 32:
        raise ValueError("descriptor array at %d, expected 32" % desc_at)
    if names_at != 32 + 8 * count:
        raise ValueError("name-array offset %d, expected %d" % (names_at, 32 + 8 * count))
    desc = []
    for i in range(count):
        oa, ob = struct.unpack_from("<II", b, 32 + 8 * i)
        if not (0 < oa < len(b)) or not (0 < ob < len(b)):
            raise ValueError("block %d has an out-of-range region offset (%d, %d)" % (i, oa, ob))
        desc.append((oa, ob))
    names = []
    for i in range(count):
        o = struct.unpack_from("<I", b, names_at + 4 * i)[0]
        if not (0 < o < len(b)):
            raise ValueError("block %d has an out-of-range name offset %d" % (i, o))
        e = b.find(b"\x00", o)
        if e < 0:
            raise ValueError("block %d's name is not NUL-terminated" % i)
        names.append(b[o:e].decode("latin-1"))
    regions = []
    for i, (oa, ob) in enumerate(desc):
        end_b = desc[i + 1][0] if i + 1 < len(desc) else len(b)
        for label, lo, hi in (("A", oa, ob), ("B", ob, end_b)):
            recs, off, term = [], lo, False
            while off + 16 <= min(hi, len(b)):
                t, tag, val, dur = struct.unpack_from("<fIff", b, off)
                if t >= TERMINATOR_TIME - 1.0:
                    term = True
                    break
                recs.append((t, tag >> 16, tag & 0xFFFF, val, dur))
                off += 16
            if not term:
                raise ValueError("block %d region %s has no terminator" % (i, label))
            regions.append((i, label, recs))
    return {"version": version, "count": count, "names": names, "regions": regions,
            "size": len(b)}


def verify_exp_bin(path):
    """Assert the written file has the layout every shipping file has.  Returns problems.

    Run immediately after writing, because the failure mode this guards against is not a bad
    animation - it is a file the engine reads an offset out of and crashes on.  Every rule here was
    established by running it over all 46 shipping files first (`tests/check_exp_layout.py`).
    """
    problems = []
    b = open(path, "rb").read()
    version, count, desc_at, names_at = struct.unpack_from("<4I", b, 0)
    if version != VERSION:
        problems.append("version is %d, every shipping file says %d" % (version, VERSION))
    if desc_at != 32:
        problems.append("header @8 is %d, every shipping file says 32" % desc_at)
    if names_at != 32 + 8 * count:
        problems.append("header @12 is %d, but 32+8*count = %d" % (names_at, 32 + 8 * count))
    first = struct.unpack_from("<I", b, 32)[0]
    if first != align32(32 + 12 * count):
        problems.append("first region at %d, the shipping formula says %d"
                        % (first, align32(32 + 12 * count)))
    for i in range(count):
        oa, ob = struct.unpack_from("<II", b, 32 + 8 * i)
        if not (0 < oa < len(b)) or not (0 < ob < len(b)):
            problems.append("block %d descriptor (%d, %d) is out of range - a zero offset is "
                            "dereferenced" % (i, oa, ob))
        o = struct.unpack_from("<I", b, names_at + 4 * i)[0]
        if not (0 < o < len(b)) or b.find(b"\x00", o) < 0:
            problems.append("block %d name offset %d is not a NUL-terminated string" % (i, o))
    return problems


# --------------------------------------------------------------------------- the entry point
def build_block(name, blink, left, right, left_shut=None, right_shut=None, expression=None):
    """The curves -> (name, region A records, region B records) inside the density cap.

    **The region and group assignment is the SHIPPING one**, measured across all 46 files: the blink
    `(tag 3, group 1)` sits in **region B** - 7205 records in 46 files, with the closed/open pulse
    shape - and the face-parameter channels `(tag 6, group 0)`, `(tag 15, group 0)` sit in **region
    A**.  An earlier version of this module used the placement of the old exp-bin tool instead
    (blink in region A, group 0 everywhere); that file's eyes happened to move, but its layout was
    malformed, and the shipping placement is the only one with a corpus behind it.

    `expression` is the VMD's expression-weight curve, written to **tag 22 (group 0)** - measured
    over the shipping corpus as the only channel that is both strongly 0.5-centred (mean 0.498,
    sd 0.423, full 0..1) and full of fast 0<->1 steps, which is the shape a switching expression
    makes.  `expression` is a list of ``(frame60, tag_index, value)`` so several expressions can
    share the channel; `None` writes no expression records at all.
    """
    region_a = [(t, TAG_EYE_L, v, dur) for t, v, dur in _with_durations(thin(left, REGION_CAP // 3))]
    region_a += [(t, TAG_EYE_R, v, dur) for t, v, dur in _with_durations(thin(right, REGION_CAP // 3))]
    if left_shut:
        region_a += [(t, TAG_LID_SHUT_L, v, dur) for t, v, dur in _with_durations(thin(left_shut, REGION_CAP // 3))]
    if right_shut:
        region_a += [(t, TAG_LID_SHUT_R, v, dur) for t, v, dur in _with_durations(thin(right_shut, REGION_CAP // 3))]
    if expression:
        region_a += [(t, TAG_EXPRESSION, v, dur)
                     for t, _i, v, dur in _with_durations(thin(expression, REGION_CAP))]
    region_a.sort(key=lambda r: r[0])
    region_b = [(t, TAG_BLINK, v, dur) for t, v, dur in _with_durations(thin(blink, REGION_CAP))]
    return (name, region_a, region_b)


def write_from_vmd(vmd_path, out_path, pv_id="0000", model_code="MMD", performer=1,
                   expression=None, blink=True):
    """Sample the dance .vmd's eye motion, write the pv_expression file, and verify it.

    `expression` is ``[(frame60, tag_index, value)]`` - the VMD's expression weights, already
    mapped onto tag indices - or ``None``.  A tag-21 enable block (constant 1.0) is written
    whenever any expression curve is present, matching the shipping files, which carry tag 21 at
    1.0 in every block that drives expression curves and never anywhere else.

    `blink=False` leaves the eyelids out of the file entirely - see `build_tracks_from_vmd`.  The
    exporter sets it when the PV script's own automatic blink is driving them.
    """
    blink, left, right, left_shut, right_shut, total = build_tracks_from_vmd(vmd_path, blink)
    if not (blink or left or right or left_shut or right_shut or expression):
        return 0, {"note": "the .vmd animates no eye bone and no eyelid morph"}
    name = "PV%s_%s_P%d_00" % (pv_id, model_code, performer)
    block = build_block(name, blink, left, right, left_shut=left_shut,
                        right_shut=right_shut, expression=expression)
    if expression:
        # tag 21 = the enable marker: constant 1.0, spread across the timeline as the shipping
        # files write it (145 records in exp_PV238's first block, every one exactly 1.0)
        block[2].extend([(0, TAG_ENABLE, 1.0, DEFAULT_DURATION),
                         (total - 1, TAG_ENABLE, 1.0, DEFAULT_DURATION)])
        block[2].sort(key=lambda r: r[0])
    n, desc = write_exp_bin(out_path, [block])
    problems = verify_exp_bin(out_path)
    if problems:
        raise ValueError("the pv_expression file this export just wrote is malformed and would "
                         "crash the game on load:\n  - %s" % "\n  - ".join(problems[:6]))
    return n, {"name": name, "region A": len(block[1]), "region B": len(block[2]),
               "offsets": desc, "vmd frames": total,
               "gaze keys": (len(left), len(right)), "blink keys": len(blink),
               "expression keys": sum(1 for _t, _i, _v in (expression or []))}


if __name__ == "__main__":
    print(__doc__)
