"""Decode / encode DIVA PV scripts (rom/script/*.dsc).

Grammar (recovered from DivaMegaMix.exe's command descriptor table, mirrored in the shipped
pv_commands.json):
    header   u32 format_magic, u32 version, u32 0
    records  u32 command_id followed by n params, n in [pmin, pmax]
    TIME(id 1) carries an absolute timestamp in microseconds; the commands that
    follow it fire at that moment.

The exact parameter count is ambiguous for a few commands, so a framing is chosen
by dynamic programming: only walks that consume the file exactly are accepted.

usage:
  python -m diva_pv_tools.dsc_core dump <file.dsc> [-f]      # -f shows params as float too
  python -m diva_pv_tools.dsc_core profile <file.dsc> [...]  # command counts + param widths
"""
import json
import os
import struct
import sys

CMDS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "pv_commands.json")))
BY_ID = {c["id"]: c for c in CMDS}
NAME2ID = {c["name"]: c["id"] for c in CMDS}
HDR_U32 = 3


def read(path):
    blob = open(path, "rb").read()
    if len(blob) % 4:
        raise ValueError(f"{path}: size {len(blob)} not a multiple of 4")
    return struct.unpack("<%dI" % (len(blob) // 4), blob)


def framing(vals, start=HDR_U32, monotime=True):
    """Return [(pos, id, nparams)] covering vals[start:] exactly, or None.

    Plain fixed-arity walking is ambiguous (a too-wide record happily swallows the
    next command id), so TIME values must never decrease. That constraint alone
    rejects almost every wrong framing.
    """
    n = len(vals)
    TIME = NAME2ID["TIME"]
    ok = [None] * (n + 1)
    ok[n] = (float("inf"), 0)  # (first TIME value at/after pos, chosen width)
    for pos in range(n - 1, start - 1, -1):
        c = BY_ID.get(vals[pos])
        if c is None:
            continue
        for k in range(c["pmin"], c["pmax"] + 1):
            nxt = pos + 1 + k
            if nxt > n or ok[nxt] is None:
                continue
            last_t = ok[nxt][0]
            if vals[pos] == TIME:
                t = vals[pos + 1] if k >= 1 else None
                if k != 1 or t is None or t > last_t:
                    continue
                ok[pos] = (t, k)
                break
            ok[pos] = (last_t, k)
            break
    if ok[start] is None:
        return None
    out = []
    pos = start
    while pos < n:
        cid = vals[pos]
        c = BY_ID[cid]
        k = ok[pos][1]
        out.append((pos, cid, k))
        pos += 1 + k
    return out


def decode(path):
    vals = read(path)
    fr = framing(vals)
    if fr is None:
        raise ValueError(f"{path}: no exact framing (magic={[hex(x) for x in vals[:3]]})")
    return vals, [(BY_ID[cid]["name"], list(vals[pos + 1: pos + 1 + k])) for pos, cid, k in fr]


def framing_strict(vals, start=HDR_U32):
    """The same exact-cover walk, but END may only be the LAST record.

    `framing` above does not say that, so it happily produces framings with an END in the middle of
    the stream: pv_032's head comes out as `EYE_ANIM(0,2), END, HAND_ANIM(...)` where the 0 after
    EYE_ANIM's two params is really its third.  Any width census taken from that framing is wrong
    (it reported "every vanilla script uses SET_PLAYDATA(0,0)" while pv_032 plainly uses (0,1)), so
    readers that care about parameter counts should use this one.
    """
    n = len(vals)
    TIME = NAME2ID["TIME"]
    END = NAME2ID["END"]
    ok = [None] * (n + 1)
    ok[n] = (float("inf"), 0)
    for pos in range(n - 1, start - 1, -1):
        cid = vals[pos]
        c = BY_ID.get(cid)
        if c is None:
            continue
        if cid == END:
            if pos + 1 == n:
                ok[pos] = (float("inf"), 0)
            continue
        for k in range(c["pmin"], c["pmax"] + 1):
            nxt = pos + 1 + k
            if nxt > n or ok[nxt] is None:
                continue
            last_t = ok[nxt][0]
            if cid == TIME:
                if k != 1:
                    continue
                t = vals[pos + 1]
                if t > last_t:
                    continue
                ok[pos] = (t, k)
                break
            ok[pos] = (last_t, k)
            break
    if ok[start] is None:
        return None
    out, pos = [], start
    while pos < n:
        cid = vals[pos]
        k = ok[pos][1]
        out.append((pos, cid, k))
        pos += 1 + k
    return out


# Record widths that have been seen in an actual record, not inferred from the descriptor table's
# pmin/pmax.  Reading a script with pmin silently truncates MOUTH_ANIM(0,0,shape,weight,hold) to
# three words and then has to invent records for the leftovers - which is how a census built on it
# came to claim that every vanilla script uses SET_PLAYDATA(0,0) while pv_032 plainly writes (0,1).
# Verified by pv_032_extreme (2730 records) and pv_70204_extreme framing exactly, END last.
WIDTHS = {
    "AGEAGE_CTRL": 8, "AIM": 3, "AOTO_CAP": 1, "AUTO_BLINK": 2, "BAR_TIME_SET": 2, "BLOOM": 2,
    "CHANGE_FIELD": 1, "CHARA_ALPHA": 4, "CHARA_COLOR": 2, "CHARA_HEIGHT_ADJUST": 2,
    "CHARA_LIGHT": 3, "CHARA_POS_ADJUST": 4, "CHARA_SIZE": 2, "CLOTH_WET": 2, "COLOR_COLLE": 3,
    "DATA_CAMERA": 2, "DATA_CAMERA_START": 2, "DOF": 3, "EDIT_BLUSH": 1, "EDIT_CAMERA": 24,
    "EDIT_DISP": 1, "EDIT_EFFECT": 2, "EDIT_EXPRESSION": 2, "EDIT_EYE": 2, "EDIT_EYELID": 1,
    "EDIT_EYELID_ANIM": 3, "EDIT_EYE_ANIM": 3, "EDIT_FACE": 2, "EDIT_HAND_ANIM": 2,
    "EDIT_INSTRUMENT_ITEM": 2, "EDIT_ITEM": 1, "EDIT_LYRIC": 2, "EDIT_MODE_SELECT": 1,
    "EDIT_MOTION": 4, "EDIT_MOTION_F": 6, "EDIT_MOTION_LOOP": 4, "EDIT_MOUTH": 1,
    "EDIT_MOUTH_ANIM": 2, "EDIT_MOVE": 7, "EDIT_MOVE_XYZ": 9, "EDIT_SHADOW": 1, "EDIT_TARGET": 5,
    "EFFECT": 6, "EFFECT_OFF": 1, "END": 0, "EXPRESSION": 4, "EYE_ANIM": 3, "FACE_TYPE": 1,
    "FADEIN_FIELD": 2, "FADEOUT_FIELD": 2, "FADE_MODE": 1, "FOG": 3, "HAND_ANIM": 5,
    "HAND_ITEM": 3, "HAND_SCALE": 3, "HIDE_FIELD": 1, "ITEM_ALPHA": 4, "ITEM_ANIM": 4,
    "ITEM_ANIM_ATTACH": 3, "LIGHT_POS": 4, "LIGHT_ROT": 3, "LOOK_ANIM": 4, "LOOK_CAMERA": 5,
    "LYRIC": 2, "MAN_CAP": 1, "MIKU_DISP": 2, "MIKU_MOVE": 4, "MIKU_ROT": 2, "MIKU_SHADOW": 2,
    "MODE_SELECT": 2, "MOT_SMOOTH": 2, "MOUTH_ANIM": 5, "MOVE_CAMERA": 21, "MOVE_FIELD": 3,
    "MOVIE_CUT_CHG": 2, "MOVIE_DISP": 1, "MOVIE_PLAY": 1, "MUSIC_PLAY": 0, "NEAR_CLIP": 2,
    "OSAGE_MV_CCL": 3, "OSAGE_STEP": 3, "PARTS_DISP": 3, "PSE": 2, "PV_BRANCH_MODE": 1,
    "PV_END": 0, "PV_END_FADEOUT": 2, "SATURATE": 1, "SCENE_FADE": 6, "SCENE_ROT": 1,
    "SET_CAMERA": 6, "SET_CHARA": 1, "SET_MOTION": 4, "SET_PLAYDATA": 2, "SE_EFFECT": 1,
    "SHADOWHEIGHT": 2, "SHADOWPOS": 3, "SHADOW_CAST": 2, "SHADOW_RANGE": 1, "SHIMMER": 3,
    "STAGE_LIGHT": 3, "TARGET": 7, "TARGET_FLAG": 1, "TARGET_FLYING_TIME": 1, "TIME": 1,
    "TONE_TRANS": 6, "TOON": 3, "WIND": 3
}

_BY_WIDTH_ID = {NAME2ID[n]: (n, w) for n, w in WIDTHS.items() if n in NAME2ID}


def decode_fixed(path):
    """Walk the script at the verified widths.  Refuses anything it cannot read exactly.

    Returns (vals, [(name, params)]) and raises ValueError naming the offending word, so a script
    that uses a command this table has no width for fails loudly instead of silently sliding out of
    step - the failure mode that produced the bad censuses.
    """
    vals = read(path)
    n = len(vals)
    recs, pos, ts, signed = [], 3, -1, []
    while pos < n:
        cid = vals[pos]
        ent = _BY_WIDTH_ID.get(cid)
        if ent is None:
            raise ValueError("%s: word %d is %d, which WIDTHS has no entry for" % (path, pos, cid))
        name, k = ent
        if pos + 1 + k > n:
            raise ValueError("%s: %s at word %d needs %d params, only %d words left"
                             % (path, name, pos, k, n - pos - 1))
        ps = [v - 0x100000000 if v > 0x7FFFFFFF else v for v in vals[pos + 1:pos + 1 + k]]
        if name == "TIME":
            if ps[0] < ts:
                raise ValueError("%s: TIME goes backwards at word %d (%d -> %d)" % (path, pos, ts, ps[0]))
            ts = ps[0]
        recs.append((name, ps))
        pos += 1 + k
        if name == "END":
            if pos != n:
                raise ValueError("%s: END at word %d but %d words remain - the widths are wrong"
                                 % (path, pos - 1, n - pos))
            return vals, recs
    raise ValueError("%s: ran out of words without an END" % path)


def f32(u):
    return struct.unpack("<f", struct.pack("<I", u))[0]


# --- what the engine is known to accept --------------------------------------------------------
# `diva_face_targets.json` is measured from SEGA's own shipping scripts: every mouth shape and
# every expression id in it is a value that a real script contains, reached by an exact framing.  The exporter is only allowed to emit values from these two lists.  This is
# a deliberate restriction, not a completeness claim: `MIK_FACE_SMILE` (slot 29) looks like an
# obvious target and is *not* in the list, because no shipping script ever sends it, so nothing
# may invent it.
_TARGETS = None


def targets():
    """The measured target sets, or a loud failure - never a silent empty list."""
    global _TARGETS
    if _TARGETS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diva_face_targets.json")
        with open(path, encoding="utf-8") as handle:
            _TARGETS = json.load(handle)
    return _TARGETS


def attested_mouth_shapes():
    return set(targets()["mouth_shapes"])


def attested_expression_ids():
    return set(targets()["expression_ids"])


def validate(path, magic=None, version=None, chara_limit=None, player_end=True,
             extra_expression_ids=()):
    """Read the file back and refuse it unless every rule a shipping script obeys holds.

    Returns a list of human-readable problems; empty means the script is the shape the engine is
    known to read.  The rules and where they come from:

      * exact framing with the solved widths, and END as the very last record - `decode_fixed`
        already enforces this, and a script that does not frame is one the engine will run off the
        end of;
      * no word after END.  Not one shipping script has one;
      * TIME strictly increasing and never repeated.  `TIME` sets the clock; a repeat is two
        different instants claiming the same moment and no vanilla file has one (0/754);
      * no bare TIME (a TIME whose block holds no records).  Eight exist in 754 shipping scripts,
        all in SEGA's own dlc00 charts; the face splice is checked against this so a block the
        replacement empties can never leave one behind;
      * MOUTH_ANIM.shape and EXPRESSION.id inside the measured sets;
      * a face cue never after PV_END: the ending has already been played by then.

    ``extra_expression_ids`` admits ids this corpus measurement does not contain but that a caller
    has established some other way.  `diva_face_targets.json` is a *measurement* - the ids a
    shipping chart sends - and the exporter's blink ids (21 and 22) are not in it, because they were
    verified in game instead (`face_core`'s docstring records the file they were confirmed on).
    Keeping them here rather than folding them into the measured table means the table still says
    exactly what was measured, and the exception is visible at the one call site that needs it.
    """
    problems = []
    try:
        vals, recs = decode_fixed(path)
    except ValueError as exc:
        return ["not readable with the verified widths: %s" % exc]
    if len(vals) * 4 != os.path.getsize(path):
        problems.append("size %d is not a multiple of 4" % os.path.getsize(path))
    if magic is not None and vals[0] != magic:
        problems.append("magic is %#x, the base script's is %#x - the loader matches script_format "
                        "against it" % (vals[0], magic))
    if version is not None and vals[1] != version:
        problems.append("version is %d, the base script's is %d" % (vals[1], version))

    times, bare, after_end, ended = [], 0, [], False
    shapes = {r for r in attested_mouth_shapes()}
    exprs = {r for r in attested_expression_ids()} | {int(i) for i in extra_expression_ids}
    pos = 0
    while pos < len(recs):
        name, params = recs[pos]
        if name == "TIME":
            times.append(params[0])
            if pos + 1 >= len(recs):
                problems.append("the file ends on TIME %d with nothing after it" % params[0])
            elif recs[pos + 1][0] == "TIME":
                bare += 1
        elif name == "PV_END":
            ended = True
        elif ended and name in ("MOUTH_ANIM", "EXPRESSION"):
            after_end.append(name)
        elif name == "MOUTH_ANIM" and len(params) == 5 and params[2] not in shapes:
            problems.append("MOUTH_ANIM shape %d at %d is not in the measured set (no shipping "
                            "script uses it)" % (params[2], pos))
        elif name == "EXPRESSION" and len(params) >= 2 and params[1] not in exprs:
            problems.append("EXPRESSION id %d at %d is not in the measured set (no shipping "
                            "script uses it)" % (params[1], pos))
        if name in ("MOUTH_ANIM", "EXPRESSION") and chara_limit is not None:
            if params[0] < 0 or params[0] >= chara_limit:
                problems.append("%s at %d names character %d but the PV declares %d performer(s)"
                                % (name, pos, params[0], chara_limit))
        pos += 1
    if times != sorted(times):
        problems.append("TIME goes backwards")
    if len(times) != len(set(times)):
        problems.append("%d TIME value(s) repeat" % (len(times) - len(set(times))))
    if bare:
        problems.append("%d bare TIME record(s): a TIME with no records after it never appears in "
                        "a shipping chart" % bare)
    if after_end:
        problems.append("%d face cue(s) after PV_END" % len(after_end))
    if not times:
        problems.append("no TIME record at all")
    return problems


def describe(path):
    """One line per problem, for a report; empty string when the file is clean."""
    bad = validate(path)
    return "\n".join("  - %s" % p for p in bad)


def main(argv):
    strict = "--strict" in argv
    argv = [a for a in argv if a != "--strict"]
    dec = decode_fixed if strict else decode
    if argv[0] == "dump":
        vals, recs = dec(argv[1])
        print(f"# {argv[1]} magic={vals[0]:#x} ver={vals[1]} records={len(recs)}")
        showf = "-f" in argv
        for name, args in recs:
            line = f"{name:<18} " + " ".join(f"{a:>12d}" for a in args)
            if showf and args:
                line += "   |f| " + " ".join(f"{f32(a):9.4f}" for a in args)
            print(line)
        return 0
    if argv[0] == "profile":
        for path in argv[1:]:
            vals, recs = decode(path)
            widths = {}
            for name, args in recs:
                widths.setdefault(name, set()).add(len(args))
            print(f"--- {path}  magic={vals[0]:#x} ver={vals[1]} recs={len(recs)}")
            for name in sorted(widths, key=lambda n: -sum(1 for r in recs if r[0] == name)):
                cnt = sum(1 for r in recs if r[0] == name)
                print(f"    {name:<18} x{cnt:<5} widths={sorted(widths[name])}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
