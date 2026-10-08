"""MMD VMD mouth animation -> Project DIVA mouth slot transfer.

The whole job in one sentence: read which MMD morph names a VMD actually animates, line them up
against the mouth slots *this build of the game really ships*, match the ones we can be confident
about, and hand the human a worklist of the rest to fill in by hand.

**This module resolves mouth shapes.**  The exporter transplants the lip sync and the blink.  MMD
*expression* morphs (笑い, 怒り, 涙 ...) are **no longer resolved into cues here**: the faces the
export writes come from ``data/expression_rules.json``, which states each DIVA face as a threshold
condition on the dance's morph weights.  The ``expression`` / ``expression_approx`` halves of
``diva_face_alias.json`` are still shipped and still read - but only as name knowledge, because the
morph *audit* reports against them; nothing in this module emits an ``EXPRESSION`` cue from them.

Two facts shape everything below:

  * The game's mouth is a finite, *numbered* slot list, not a set of names.  At runtime the PV script
    fires ``MOUTH_ANIM(chara, 0, shape, weight, hold)``, where ``shape`` is the mouth table's index.
    The slot -> shape/animId tables are read from ``mot_db.farc`` + ``rob_mot_tbl.bin``; the same
    parsing is reproduced here so this module is self-contained, and every name it prints is sourced
    from those files - never invented.
  * The mouth and the expression command do **not** share a numbering - mouth takes the mouth table's
    index, ``EXPRESSION`` takes the ``rob_mot_tbl`` slot - which is one more reason the two are kept
    in separate tables and only one of them is emitted.

Matching runs in the order the worklist is meant to be read: exact -> shipped alias -> the user's
own answers -> the keyword tier -> a legal DIVA fallback -> "unresolved, ask the human".  A name is
never auto-accepted on a low-confidence resemblance: it is reported *ambiguous* with ranked
suggestions instead.

Not a mapping problem at all, and handled one layer down: blinking (``まばたき``) has no mouth slot.
Measured: the MIK set in ``mot_db`` contains no BLINK/MABATAKI/MABU/LID name - so ``まばたき`` is
flagged ``blink``, reported, and ``face_core.blink_events`` translates its curve into the blink
``EXPRESSION`` cues (see that module).

The twin failure this module also surfaces: a target whose *value* the engine has never been seen
to accept.  A mouth index outside the measured set is not offered.  ``diva_face_targets.json`` holds
the measured sets and ``dsc_core.attested_*`` is the single source of truth for them.

No Blender import.  usage:
    python -m diva_pv_tools.morph_core names
    python -m diva_pv_tools.morph_core vmd     <file.vmd>
    python -m diva_pv_tools.morph_core match   <file.vmd> [--alias FILE] [--json]
    python -m diva_pv_tools.morph_core worklist <file.vmd> <out.txt> [--alias FILE]
"""
import argparse
import json
import os
import re
import struct
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

from . import dsc_core as dsc            # noqa: E402  (the measured target sets live here)
from . import farctool                   # noqa: E402  (farc reader, mot_db.farc)
from . import vmd_reader as vmd          # noqa: E402  (SJIS VMD reader)

# The rom is normally split across unpacked trees; a Steam install keeps the tables inside
# diva_main.cpk and has no loose file at all, which is why the packaged tables shipped with this
# add-on are the normal path and these roots are only consulted when the user has an extracted
# rom.  Set the environment variable DIVA_DATA_ROOT to the directory that contains the rom trees
# (`main/rom/...`, `main/rom_switch/rom/...`, ...).
#
# The tree layout is NOT uniform and the probes below must cover both shapes: `main/rom` puts the
# tables at `rob/`, while `main/rom_switch` adds a `rom/` level.  Probing only one shape is a
# silent failure - `game_names()` falls back to the packaged table and the report says "packaged"
# without saying that the user's own rom was skipped - so every layout is probed and the one that
# answered is reported.
DATA_ROOTS = tuple(p for p in (os.environ.get("DIVA_DATA_ROOT", ""),) if p)
_TBL_REL = ("main/rom/rob/rob_mot_tbl.bin", "main/rom_switch/rom/rob/rob_mot_tbl.bin",
            "main/rom_ps4/rom/rob/rob_mot_tbl.bin", "main/rom_steam/rom/rob/rob_mot_tbl.bin")
_DB_REL = ("main/rom/rob/mot_db.farc", "main/rom_switch/rom/rob/mot_db.farc",
           "main/rom_ps4/rom/rob/mot_db.farc", "main/rom_steam/rom/mot_db.farc",
           "main/rom_steam/rom/rob/mot_db.farc")
_ROB_TBL = tuple(os.path.join(r, rel) for r in DATA_ROOTS for rel in _TBL_REL)
_MOT_DB = tuple(os.path.join(r, rel) for r in DATA_ROOTS for rel in _DB_REL)
SLOTS_JSON = os.path.join(HERE, "expression_slots.json")
ALIAS_JSON = os.path.join(HERE, "diva_face_alias.json")

CHARA = "MIK"            # runtime default performer (the game's default slot name)
PERF = ["MIK", "RIN", "LEN", "LUK", "NER", "HAK", "KAI", "MEI", "SAK", "TET", "CMN"]

# The slot orderings measured from rob_mot_tbl animId names.  Index == the numeric shape the
# MOUTH_ANIM command wants; the value is the rob_mot_tbl slot whose animId names the shape.
# MOUTH: measured slot 0x83 (idx 9) carries no named anim -> that idx is dropped from game_names,
# so MOUTH resolves to 42 of the 43 listed slots.  EXP: 0x07 (MIK_FACE_RESET) is listed twice and
# 0x06 has no named anim -> EXP resolves to 22 of the 32 listed slots.
MOUTH_SLOTS = [0x86, 0x8C, 0x8E, 0x92, 0x90, 0x94, 0x96, 0x98, 0x84, 0x83, 0x88, 0x8A, 0x9A,
               0x9B, 0x9C, 0x9D, 0x9E, 0x9F, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0x97, 0x87, 0x8F,
               0x93, 0x91, 0x85, 0x89, 0x8B, 0x8D, 0x95, 0x99, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
               0xF9, 0xFA, 0xFB, 0xFC]
EXP_SLOTS = [0x0B, 0x0F, 0x39, 0x13, 0x17, 0x19, 0x1D, 0x21, 0x25, 0x29, 0x2D, 0x31, 0x35,
             0x41, 0x07, 0x45, 0x49, 0x4D, 0x51, 0x55, 0x59, 0x07, 0x3D]

# The list above stops at 22 because that is where the evidence stops.  It used to carry nine more
# entries (0x06 and 0xD6..0xDD, all slots the name table gives no animId) - those were a *guess* at
# the tail, and the expression probe falsified it: `EXPRESSION(0, 42)` came back as ウィンク and
# `(0, 43)` as ウィンク右, so the engine's table is longer and is made of real faces, not of unnamed
# slots.  The guess is therefore removed rather than kept, and what the probe actually established
# is recorded on its own:
#
#   * ids 0..22 - every one read back in game as the face this list names (21 of 21 readable);
#   * ids 42 and 43 - the per-eye winks;
#   * ids 23..41  - exist, never observed, deliberately not guessed at.
EXP_WINKS = {42: "MIK_FACE_WINK_L", 43: "MIK_FACE_WINK_R"}
EXP_UNOBSERVED = (23, 41)


def expression_id_name(idx):
    """What `EXPRESSION(chara, idx, …)` is, as far as the game itself has been made to say.

    Returns the asset name, or `None` for the ids that live in the engine's table but were never
    observed - naming those would be inventing the very thing the probe exists to measure.
    """
    if idx in EXP_WINKS:
        return EXP_WINKS[idx]
    if 0 <= idx < len(EXP_SLOTS):
        return None            # slot known, name comes from `game_names()`; see the caller
    return None


EXACT, ALIAS, APPROX, USER, KEYWORD = "exact", "alias", "approx", "user", "keyword"
CONFIDENT = (EXACT, ALIAS, USER)
TIER_ORDER = (EXACT, USER, ALIAS, APPROX, KEYWORD)


# --- what a slot's name says it belongs to ------------------------------------------------
# The game's slots fall into blocks that the *name* announces, measured from rob_mot_tbl +
# mot_db (1097 shipping scripts and the Switch rom's own table): 132 face, 42 mouth (KUCHI),
# 35 common, 13 other, for every performer.  Two blocks matter here and were previously invisible:
#
#   * `EYES_*` (0xA6..0xBF) - the gaze block.  A real, named, per-performer set of eye-direction
#     animations (UP/DOWN/LEFT/RIGHT and the diagonals), which is where an MMD gaze morph belongs.
#     Whether the engine reads it through EYE_ANIM or through an expression id is NOT established
#     by the corpus (no command in it takes a slot index from this block), so the capability model
#     reports it as available-but-encoding-unverified rather than emitting it.
#   * `FACE_EYEBROW_UP_*` (0xEC..0xEF) - per-side raised eyebrows, added by the MEGA39's era table.
#     No shipping script cues them either.  They are exactly what MMD's 眉上げ/上 need, and
#     are exposed here so a mapping can point at them and be *reported* instead of guessed at.
FEATURE_BLOCKS = ("eyes", "eyebrow")
FEATURE_PREFIXES = {
    "eyes": ("CMN_EYES_", "_EYES_"),
    "eyebrow": ("_FACE_EYEBROW_",),
}


def feature_block(name):
    """`'eyes'` / `'eyebrow'` / None - which feature block a slot name belongs to."""
    for block, prefixes in FEATURE_PREFIXES.items():
        if any(p in name for p in prefixes):
            return block
    return None


def anim_family(name):
    """The name's own block: face / mouth / eyes / eyebrow / hand / common / other.

    Read from the asset name, which is the only thing the table carries.  This is a *classification
    of names*, not a claim about behaviour; behaviour is what `diva_capability` measures.
    """
    if "_KUCHI_" in name:
        return "mouth"
    if "_FACE_EYEBROW_" in name:
        return "eyebrow"
    if "_EYES_" in name:
        return "eyes"
    if "_FACE_" in name:
        return "face"
    if "_HAND_" in name:
        return "hand"
    if name.startswith("CMN_"):
        return "common"
    return "other"


class MorphSourceError(RuntimeError):
    """Raised when the game's own data files cannot be found / parsed, rather than returning a
    silently partial slot list."""


# --- game data parsing ------------------------------------------------------------------------
def _first_existing(paths):
    return next((p for p in paths if os.path.exists(p)), None)


def _rob_tbl(path, skip):
    blob = open(path, "rb").read()[skip:]
    perf_count, anim_count, perf_off = struct.unpack_from("<3I", blob, 0)
    q = perf_off
    out = {}
    for i in range(perf_count):
        anim_off, _unk = struct.unpack_from("<2I", blob, q)
        q += 8
        data_off = struct.unpack_from("<I", blob, anim_off)[0]
        vals = struct.unpack_from("<%dI" % anim_count, blob, data_off)
        out[PERF[i]] = list(vals)
    return out


def _mot_names(path):
    data = [e.data for e in farctool.read(path)[2] if e.name.endswith("mot_db.bin")][0]
    magic, setOff, setIdOff, setCount, boneOff, boneCount = struct.unpack_from("<6I", data, 0)

    def s(off):
        return data[off:data.index(b"\x00", off)].decode("ascii", "ignore")

    id2name = {}
    for i in range(setCount):
        _name_o, names_o, cnt, ids_o = struct.unpack_from("<4I", data, setOff + 16 * i)
        for k in range(cnt):
            nm = s(struct.unpack_from("<I", data, names_o + 4 * k)[0])
            mid = struct.unpack_from("<I", data, ids_o + 4 * k)[0]
            id2name.setdefault(mid, nm)
    return id2name


_CACHE = {}


def _from_shipped(why):
    """The packaged dump of the same tables, for installs without a loose rom folder.

    `why` is kept in `_source` so an export report can say the numbers came from the packaged copy
    and not from the user's own game files - the two differ if their game is a different version.
    """
    if not os.path.isfile(SLOTS_JSON):
        raise MorphSourceError("%s, and the packaged slot table is missing too: %s" % (why, SLOTS_JSON))
    with open(SLOTS_JSON, encoding="utf-8") as handle:
        blob = json.load(handle)
    rows = blob["characters"].get(CHARA)
    if not rows:
        raise MorphSourceError("%s, and the packaged table has no character %r (it has %s)"
                               % (why, CHARA, ", ".join(sorted(blob["characters"]))))
    game = {"_source": dict(blob.get("_source", {}), reason=why, slots_json=SLOTS_JSON)}
    game.update({k: rows[k] for k in ("mouth", "expression", "other")})
    # The packaged file is a dump of the same tables, so the feature blocks can be rebuilt from
    # it: `other` is deduplicated by name (that is how the dump was made) but it still contains
    # one row per distinct name, which is enough to enumerate the EYES_* and eyebrow slots.  A
    # slot whose name repeats at a higher index is genuinely missing from this source, and the
    # capability model says so rather than pretending the block is complete.
    feature = {block: [] for block in FEATURE_BLOCKS}
    for row in game["other"]:
        block = feature_block(row.get("name", ""))
        if block:
            feature[block].append(dict(row, kind="feature", category=anim_family(row["name"])))
    for block in feature:
        feature[block].sort(key=lambda r: r["slot"])
    game["feature"] = feature
    game["_source"] = dict(game["_source"],
                           slots_named_feature={k: len(v) for k, v in feature.items()})
    return game


def which_slot_source():
    """Where the last `game_names()` call read its numbers from - shown in the export report."""
    source = (_CACHE.get("game") or {}).get("_source", {})
    if source.get("slots_json"):
        return "packaged table (%s): %s" % (os.path.basename(source["slots_json"]),
                                            source.get("reason", ""))
    if source.get("rob_mot_tbl"):
        return source["rob_mot_tbl"]
    return "not read yet"


def game_names(refresh=False):
    """Enumerate the slots THIS game ships for the default performer, keeping the numeric id next
    to each name.

    Returns ``{"mouth": [...], "expression": [...], "other": [...]}``; each entry is a dict with
    ``name`` (the ASCII slot name straight from mot_db), ``slot`` (the rob_mot_tbl slot), ``idx``
    (the shape/id index the runtime command takes, only for mouth/expression) and ``anim`` (animId).
    Raises MorphSourceError if a source file is missing or yields nothing, never a partial list.
    """
    if "game" in _CACHE and not refresh:
        return _CACHE["game"]
    tbl_path, db_path = _first_existing(_ROB_TBL), _first_existing(_MOT_DB)
    if not tbl_path or not db_path:
        hint = ("DIVA_DATA_ROOT is not set" if not DATA_ROOTS
                else "DIVA_DATA_ROOT=%s has neither %s nor %s"
                     % (", ".join(DATA_ROOTS), _TBL_REL[0], _DB_REL[1]))
        game = _from_shipped("no extracted rom here (%s)" % hint)
        _CACHE["game"] = game
        return game
    try:
        tbl = _rob_tbl(tbl_path, 0x40)
        id2 = _mot_names(db_path)
    except Exception as exc:  # a truncated / mis-framed archive is a source failure, not a short list
        game = _from_shipped("could not parse the game data at %s (%s)" % (db_path, exc))
        _CACHE["game"] = game
        return game
    if CHARA not in tbl or not tbl[CHARA]:
        raise MorphSourceError("character %r absent from rob_mot_tbl" % CHARA)
    if not id2:
        raise MorphSourceError("mot_db gave 0 animId names")
    mik = tbl[CHARA]

    def resolve(slots, kind):
        seen_slot = set()
        rows = []
        for idx, slot in enumerate(slots):
            if slot in seen_slot:            # MIK_FACE_RESET (0x07) is listed twice -> keep first
                continue
            seen_slot.add(slot)
            anim = mik[slot] if slot < len(mik) else -1
            name = id2.get(anim)
            if not name:
                continue                     # slot with no named anim (MOUTH idx 9, EXP 0x06 ...)
            rows.append({"name": name, "slot": slot, "idx": idx, "anim": anim, "kind": kind})
        return rows

    mouth = resolve(MOUTH_SLOTS, "mouth")
    expression = resolve(EXP_SLOTS, "expression")
    named_mouth = {r["slot"] for r in mouth}
    named_exp = {r["slot"] for r in expression}
    other = []
    for slot, anim in enumerate(mik):
        if slot in named_mouth or slot in named_exp:
            continue
        name = id2.get(anim)
        if name:
            other.append({"name": name, "slot": slot, "idx": None, "anim": anim, "kind": "other",
                          "category": anim_family(name)})
    other_by_name = {}
    for r in sorted(other, key=lambda r: r["slot"]):
        other_by_name.setdefault(r["name"], r)
    # The `other` list is deduplicated by name, which hides the highest-numbered slots: nine of the
    # ten named slots above 0x74 are repeats of MIK_FACE_RESET.  Slots a *feature* actually needs
    # must not be lost that way, so the slots whose names carry a distinct semantic block are also
    # exposed unabridged, keyed by block.
    feature_slots = {block: [] for block in FEATURE_BLOCKS}
    for slot, anim in enumerate(mik):
        name = id2.get(anim)
        if not name:
            continue
        block = feature_block(name)
        if block:
            feature_slots[block].append({"name": name, "slot": slot, "idx": None, "anim": anim,
                                         "kind": "feature", "category": anim_family(name)})
    for block in feature_slots:
        feature_slots[block].sort(key=lambda r: r["slot"])
    game = {"mouth": mouth, "expression": expression, "other": list(other_by_name.values()),
            "feature": feature_slots}
    src = {"rob_mot_tbl": tbl_path, "mot_db": db_path, "slots_named_mouth": len(mouth),
           "slots_named_expression": len(expression), "slots_named_other": len(game["other"]),
           "slots_named_feature": {k: len(v) for k, v in feature_slots.items()}}
    game = {"_source": src, **game}
    _CACHE["game"] = game
    return game


# --- normalisation (the exact tier) ------------------------------------------------------------
# Every character that authors write as a separator and no author writes as a meaning.  Dropped
# before lookup, so 'mouth_open', 'Mouth-Open', 'mouth open' and 'ＭＯＵＴＨ　ＯＰＥＮ' are one key.
_SEPARATORS = str.maketrans({c: None for c in "_-. /\\\u30fb\uff0e\uff0f\uff3c\uff65\u00b7"})


def normalize(name):
    """Fold the shapes MMD morph names vary in so 'exact' really means 'same morph'.

    NFKC collapses half-width katakana (ｱ) to full-width (ア) and half/full-width digits, so
    'ｳｨﾝｸ２右' and 'ウィンク2右' become the same string.  Consecutive whitespace collapses,
    leading/trailing is dropped, separators are removed and Latin is case-folded
    (MIK_face_smile == MIK_FACE_SMILE).  Iteration marks are stripped.  This is deliberately
    conservative: it makes *dialect* forms equal, not *semantically* similar ones - 'ウィンク' and
    'ウィンク右' stay different strings, so the right wink is decided by the alias table, not by a
    substring accident.
    """
    s = unicodedata.normalize("NFKC", name or "")
    s = s.replace("\u30fb", "\u30fc")          # ・ vs long mark, both show up
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # drop combining/iteration
    s = re.sub(r"\s+", " ", s).strip()
    s = s.translate(_SEPARATORS)
    return s.casefold()


# --- the shipped name tables -------------------------------------------------------------------
_TABLES = None


def tables():
    """`diva_face_alias.json`, normalised into lookup dicts, validated against the target sets.

    Loaded once.  The validation is not decoration: if the file names a target the engine has never
    been seen to accept, the export stops here with the offending key named, rather than writing a
    number nobody can vouch for.
    """
    global _TABLES
    if _TABLES is not None:
        return _TABLES
    with open(ALIAS_JSON, encoding="utf-8") as handle:
        blob = json.load(handle)
    game = game_names()
    by_kind = {}
    for kind in ("mouth", "expression"):
        by_kind[kind] = {r["name"]: r for r in game[kind]}
    out = {"mouth": {}, "expression": {}, "mouth_approx": {}, "expression_approx": {},
           "junk": [(re.compile(rx), why) for rx, why in blob["junk"]], "_raw": blob}
    bad = []
    for table in ("mouth", "expression", "mouth_approx", "expression_approx"):
        kind = table.split("_")[0]
        for name, target in blob[table].items():
            row = by_kind[kind].get(target)
            attested = (row is not None and (row["idx"] in dsc.attested_mouth_shapes()
                                             if kind == "mouth"
                                             else row["slot"] in dsc.attested_expression_ids()))
            if not attested:
                bad.append("%s: %r -> %s (not a target any shipping script uses)" % (table, name,
                                                                                      target))
                continue
            out[table][normalize(name)] = target
    if bad:
        raise MorphSourceError("diva_face_alias.json names targets the game was never seen to "
                               "accept; fix these before exporting:\n  " + "\n  ".join(bad[:20]))
    both = set(out["mouth"]) & set(out["expression"])
    if both:
        raise MorphSourceError("these names are in both the mouth and the expression table, so "
                               "which command they belong to is undefined: %s" % sorted(both)[:10])
    _TABLES = out
    return out


def keyword_tiers():
    """The last-resort semantic tier, straight out of the measured target file."""
    tiers = dsc.targets()["fallback"]["keyword_tiers"]
    out = {}
    for kind, rows in tiers.items():
        out[kind] = [(re.compile(rx, re.IGNORECASE), target) for rx, target in rows]
    return out


# --- junk (pseudo-track) filter ---------------------------------------------------------------
def junk_reason(name):
    """Return a short reason string if `name` is MMD plumbing to filter out of the match, else None.

    The rules are data now (`diva_face_alias.json`'s `junk` list) rather than five regexes in the
    middle of this module, and they are consulted *after* the alias tables - an author who names a
    morph 怒り眉 means an expression, so the alias wins and the plumbing filter only sees what is
    left over.  The bare blink 'まばたき' is not junk: it is a real, heavily animated morph with no
    slot, so it is tagged `blink` and reported on its own.
    """
    for rx, why in tables()["junk"]:
        if rx.search(name):
            return why
    return None


def is_blink(name):
    """A morph that shuts both eyelids.

    Answered from the shared catalog (`mmd_morph`), which covers the half-width spellings
    (``ｳｨﾝｸ２右``), the trailing-ordinal variants (``ウィンク２``) and the English names that a local
    tuple of spellings kept missing.  The tuple this used to consult is kept below as
    `_BLINK_FALLBACK` only so a future catalog failure degrades instead of raising; the catalog is
    the source of truth because `face_core`'s eyelid lane depends on this answer, and getting it
    wrong is what leaves an eye shut for the rest of a song.
    """
    try:
        from . import mmd_morph
        return mmd_morph.classify(name).category == "eyelid_blink"
    except Exception:
        return normalize(name) in (normalize("まばたき"), normalize("瞬き"), normalize("blink"))


def is_wink(name):
    """'left' | 'right' | None - which eyelid a wink morph closes."""
    try:
        from . import mmd_morph
        category = mmd_morph.classify(name).category
        if category == "eyelid_wink_left":
            return "left"
        if category == "eyelid_wink_right":
            return "right"
        return None
    except Exception:
        key = normalize(name)
        if key in tuple(normalize(x) for x in _BLINK_FALLBACK["right"]):
            return "right"
        if key in tuple(normalize(x) for x in _BLINK_FALLBACK["left"]):
            return "left"
        return None


# Kept only as a degraded-mode fallback for `is_blink`/`is_wink`; the catalog is authoritative.
_BLINK_FALLBACK = {
    "left": ("ウィンク", "ウインク", "ウィンク２", "ウインク２", "ウィンク2", "wink"),
    "right": ("ウィンク右", "ウインク右", "ウィンク右２", "右ウィンク", "wink right", "wink_r"),
}


def eyelid_role(name):
    """'both' for a blink, 'left'/'right' for a wink, None for everything else."""
    if is_blink(name):
        return "both"
    return is_wink(name)


# --- VMD side ---------------------------------------------------------------------------------
def vmd_names(vmd_path):
    """Per distinct morph name in a VMD: keyframe count, weight range and first/last frame.

    Names are already SJIS-decoded by vmd_reader; this only aggregates and classifies, it never
    re-reads the raw bytes, so the console codepage cannot get involved.
    """
    doc = vmd.read(vmd_path)
    agg = {}
    for r in doc["morphs"]:
        a = agg.setdefault(r["name"], {"keys": 0, "wmin": None, "wmax": None,
                                       "fmin": None, "fmax": None})
        a["keys"] += 1
        w, f = r["weight"], r["frame"]
        a["wmin"] = w if a["wmin"] is None else min(a["wmin"], w)
        a["wmax"] = w if a["wmax"] is None else max(a["wmax"], w)
        a["fmin"] = f if a["fmin"] is None else min(a["fmin"], f)
        a["fmax"] = f if a["fmax"] is None else max(a["fmax"], f)
    out = []
    for name, a in sorted(agg.items(), key=lambda kv: (-kv[1]["keys"], kv[0])):
        jr = junk_reason(name)
        out.append({"name": name, "keys": a["keys"], "weight_min": a["wmin"], "weight_max": a["wmax"],
                    "first_frame": a["fmin"], "last_frame": a["fmax"],
                    "peak": (a["wmax"] or 0.0) > 0.05 and a["keys"] > 1,   # carries real animation
                    "junk": jr is not None, "junk_reason": jr, "blink": is_blink(name)})
    return out


# --- matching ---------------------------------------------------------------------------------
def _load_alias_file(path):
    """Read a persisted alias answer file into {normalised_name: game_name}.

    Accepts both shapes the human may leave behind: the worklist's answer form
    `"<name>" = [ MIK_SOMETHING ]` (bracket-wrapped) and a bare `"<name>" = MIK_SOMETHING`.
    Comments (#), blank lines and unanswered `[ ]` are skipped.  A human answer is the *highest*
    tier there is: it is consulted before everything this module ships.
    """
    answers = {}
    if not path or not os.path.exists(path):
        return answers
    txt = open(path, encoding="utf-8").read()
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        left, _, right = line.partition("=")
        left = left.strip().strip('"').strip("'")
        right = right.strip()
        m = re.match(r"\[\s*(.*?)\s*\]", right)
        val = (m.group(1) if m else right).split("#", 1)[0].strip()
        if val:
            answers[normalize(left)] = val
    return answers


def _candidates(name, game, tbl):
    """Ranked suggestions against the game's slot list, reported as *ambiguous* only.

    Two sources of candidate, both principled and neither auto-accepted:
      * romanisation overlap - a query that shares a Latin token with a slot's ASCII stem
        (e.g. 'smile' vs MIK_KUCHI_SMILE);
      * a shipped alias key *contained* in the query - e.g. 'あ' inside 'あいうえお', so a vowel is
        suggested, not assumed.

    Scoring prefers the longer overlap; at most four are returned for the human to pick from, and
    only targets in the measured sets are offered.  **Mouth slots only**: the exporter transplants
    the lip sync, so an expression suggestion would be advice the panel cannot act on.
    """
    key = normalize(name)
    mouth_ok = dsc.attested_mouth_shapes()

    rows = [r for r in game["mouth"] if r["idx"] in mouth_ok]
    by_name = {r["name"]: r for r in rows}
    scored = {}

    def offer(row, s):
        prev = scored.get(row["name"])
        if prev is None or s > prev:
            scored[row["name"]] = s

    for row in rows:
        stem = row["name"].split("_")[-1].casefold()
        if stem and stem in key:
            offer(row, 3 * len(stem))
        for tok in re.split(r"[^0-9a-z]+", key):
            if len(tok) >= 3 and tok in stem:
                offer(row, 2 * len(tok))
    for table in ("mouth", "mouth_approx"):
        for jp, an in tables()[table].items():
            if len(jp) >= 2 and jp in key and jp != key and an in by_name:
                offer(by_name[an], 2 * len(jp))
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    return [dict(by_name[n], score=sc) for n, sc in ranked[:4]]


def match(vmd_path, alias_file=None, approx=True):
    """Tiered transfer decision for one VMD.

    Returns ``{"matched": [(vmd_name, game_name, slot, how)], "unmatched": [names],
    "ambiguous": [{name, suggestions}], "rows": [...], "stats": {...}}``.

    ``how`` is one of exact / user / alias / approx / keyword; the first three are confident and
    become cues, ``approx`` and ``keyword`` are still emitted but counted separately so the report
    can say how much of the mouth is a reading rather than a translation.  Nothing that reaches the
    *candidate* layer is auto-accepted: those rows are reported ambiguous for the human.

    **Every tier here targets a mouth shape.**  The exporter transplants lip sync and the blink, and
    nothing else: an expression morph is reported on the worklist instead of being sent as an
    ``EXPRESSION`` cue, so the expression and expression-approximation tables are never consulted.
    ``approx`` therefore widens the *mouth* mapping only - it turns on the shipped mouth
    approximation table and the keyword tier.
    """
    game = game_names()
    tbl = tables()
    exact_index = {}
    attested = {}
    for row in game["mouth"]:
        if row["idx"] not in dsc.attested_mouth_shapes():
            continue      # unusable: never offered as an exact match either
        exact_index.setdefault(normalize(row["name"]), row)
        attested[row["name"]] = row
    answers = {normalize(k): v for k, v in _load_alias_file(alias_file).items()}
    tiers = keyword_tiers()

    matched, unmatched, ambiguous, rows = [], [], [], []
    skipped_junk, blink, wink = 0, [], []

    def add(info, target, how, why):
        row = attested.get(target)
        rows.append(dict(info, target=target, how=how, why=why, kind=row["kind"]))
        matched.append((info["name"], target, row["slot"], how))

    for info in vmd_names(vmd_path):
        name = info["name"]
        key = normalize(name)
        if info["blink"]:
            blink.append(name)
            rows.append(dict(info, target=None, how="blink", kind=None,
                             why="drives the eyelid through the blink EXPRESSION cues"))
            continue
        side = is_wink(name)
        if side is not None:
            # A wink is per-eye eyelid motion and DIVA's face command has no per-eye form: the
            # blink ids shut *both* lids.  Reported, never emitted, so a wink cannot silently
            # become a both-eyes blink.
            wink.append((name, side))
            rows.append(dict(info, target=None, how="wink", kind=None,
                             why="per-eye eyelid motion, which the blink EXPRESSION cues "
                                 "(%s) cannot express" % (side,)))
            continue
        # 1/2. exact DIVA name, or the user's own answer, or the shipped alias table.  These run
        # before the junk filter: an explicit name in a table is a statement about what the morph
        # is, and it beats a heuristic about what it looks like.
        target = answers.get(key)
        how, why = USER, "your worklist answer"
        if target is None:
            row = exact_index.get(key)
            if row is not None:
                add(info, row["name"], EXACT, "the morph already carries a DIVA morph name")
                continue
            for table, tier, note in (("mouth", ALIAS, "shipped alias (mouth)"),
                                      ("mouth_approx", APPROX, "shipped approximation (mouth)")):
                if not approx and tier == APPROX:
                    continue
                target = tbl[table].get(key)
                if target:
                    how, why = tier, note
                    break
        if target is not None:
            row = attested.get(target)
            if row is not None:
                add(info, target, how, why)
                continue
            unmatched.append(name)
            rows.append(dict(info, target=target, how="rejected", kind=None,
                             why="%s is not a mouth shape any shipping script uses" % target))
            continue
        # 3. plumbing
        jr = junk_reason(name)
        if jr:
            skipped_junk += 1
            rows.append(dict(info, target=None, how="junk", kind=None, why=jr))
            continue
        if not info["peak"]:
            # single baseline key at zero weight - MMD noise, not a morph the choreographer used
            skipped_junk += 1
            rows.append(dict(info, target=None, how="junk", kind=None,
                             why="a single baseline key at zero weight"))
            continue
        # 4. keyword tier: semantics, on attested mouth shapes only.  Tested against the *normalised*
        # name, which is what makes the anchored patterns ('^mouthopen$') reach 'mouth_open'.
        hit = None
        for rx, cand in tiers["mouth"]:
            if rx.search(key):
                hit = (cand, "keyword tier: /%s/" % rx.pattern)
                break
        if hit and approx:
            add(info, hit[0], KEYWORD, hit[1])
            continue
        # 5. ask the human
        cands = _candidates(name, game, tbl)
        if cands:
            ambiguous.append({"name": name, "suggestions": cands})
        else:
            unmatched.append(name)
        rows.append(dict(info, target=None, how="ambiguous" if cands else "unmatched", kind=None,
                         why="needs a human choice" if cands else "no candidate at all"))

    counts = {t: sum(1 for r in rows if r["how"] == t) for t in TIER_ORDER}
    stats = {
        "vmd": vmd_path,
        "distinct_names": len(rows),
        "animated": sum(1 for r in rows if r["peak"]),
        "matched": len(matched), "ambiguous": len(ambiguous), "unmatched": len(unmatched),
        "junk_filtered": skipped_junk, "blink_names": blink,
        "wink_names": [n for n, _s in wink],
        "exact": counts[EXACT], "user": counts[USER], "alias": counts[ALIAS],
        "approx": counts[APPROX], "keyword": counts[KEYWORD],
        "rejected": sum(1 for r in rows if r["how"] == "rejected"),
    }
    return {"matched": matched, "unmatched": unmatched, "ambiguous": ambiguous,
            "rows": rows, "stats": stats, "blink": blink,
            "wink": [n for n, _s in wink], "wink_sides": dict(wink)}


# --- worklist (human-in-the-loop) --------------------------------------------------------------
def write_worklist(match_result, out_path):
    """A UTF-8 file the human edits: one line per unmatched / ambiguous name with its keyframe
    count and top ranked suggestions.  The human drops a game name inside [ ... ] (or replaces it);
    read_alias_answers() turns the filled file into a persisted alias table the tool reuses."""
    game = game_names()
    src = game["_source"]
    where = src.get("rob_mot_tbl") or src.get("slots_json")
    lines = [
        "# DIVA morph transfer worklist - fill in by hand, never edited by the tool silently.",
        "# For each line, put the chosen game slot name inside [ ].  Leave [ ] empty to skip.",
        "# Valid names come from `python -m diva_pv_tools.morph_core names`.  Source: %s" % where,
        "# A name answered here wins over every table that ships with the add-on.",
        "#",
    ]
    if match_result.get("blink") or match_result.get("wink"):
        lines.append("# EYE: blink/wink morphs need no slot - the export drives the eyelid"
                     " channels (LOOK_ANIM 12/13) from them:")
        for b in match_result.get("blink", []):
            lines.append('#   blink  "%s"   (both eyelids, no worklist entry needed)' % b)
        for w in match_result.get("wink", []):
            lines.append('#   wink   "%s"   (one eyelid, no worklist entry needed)' % w)
        lines.append("#")
    lines.append("# ---- ambiguous: pick one of the ranked suggestions ----")
    for a in match_result["ambiguous"]:
        sug = ", ".join(s["name"] for s in a["suggestions"])
        lines.append('"%s" = [ ]   # suggestions: %s' % (a["name"], sug))
    lines.append("# ---- unmatched: type the correct slot name (or leave empty) ----")
    for u in match_result["unmatched"]:
        lines.append('"%s" = [ ]   # no confident candidate' % u)
    if match_result.get("rows"):
        lines.append("# ---- everything this vmd carries, for reference ----")
        for r in match_result["rows"]:
            lines.append('# %-28s %-24s %-9s %s'
                         % (r["name"], r["target"] or "-", r["how"], r["why"]))
    body = "\n".join(lines) + "\n"
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return len(match_result["ambiguous"]) + len(match_result["unmatched"])


def read_alias_answers(path):
    """Parse a filled-in worklist (or any `"<name>" = MIK_NAME` file) into an alias dict usable as
    match(alias_file=...): returns {name: game_name}.  Unanswered [ ] lines are ignored."""
    answers = {}
    if not path or not os.path.exists(path):
        return answers
    for line in open(path, encoding="utf-8").read().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        left, _, right = s.partition("=")
        left = left.strip().strip('"').strip("'")
        m = re.match(r"\[\s*(.*?)\s*\]", right.strip())
        val = m.group(1) if m else right.strip()
        val = val.split("#", 1)[0].strip()
        if val:
            answers[unicodedata.normalize("NFKC", left)] = val
    return answers


def report(match_result):
    """The diagnostic block the panel shows: how every name was resolved, and by what.

    Built so that "the alias table needs one more entry" is a visible, specific statement rather
    than a suspicion: unresolved names are listed with their keyframe count, and every row says
    which tier decided it.
    """
    st = match_result["stats"]
    lines = ["found %d morph name(s), %d of them animated; %d junk, %d blink"
             % (st["distinct_names"], st["animated"], st["junk_filtered"], len(st["blink_names"])),
             "resolved: exact %d, alias %d, approximation %d, keyword %d, your answers %d"
             % (st["exact"], st["alias"], st["approx"], st["keyword"], st["user"]),
             "for the worklist: ambiguous %d, unmatched %d, refused %d"
             % (st["ambiguous"], st["unmatched"], st["rejected"])]
    for r in match_result["rows"]:
        if r["how"] in ("ambiguous", "unmatched", "rejected"):
            lines.append("  UNRESOLVED %-26s keys=%-4d target=%-22s reason=%s"
                         % (r["name"], r["keys"], r["target"] or "-", r["why"]))
    for b in match_result["blink"]:
        lines.append("  BLINK      %-26s drives both eyelids (LOOK_ANIM 12/13)" % b)
    for w in match_result.get("wink", []):
        side = match_result.get("wink_sides", {}).get(w, "left")
        lines.append("  WINK       %-26s drives the %s eyelid (LOOK_ANIM %d)"
                     % (w, side, 12 if side == "left" else 13))
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------------------------
def _cmd_names(_a):
    g = game_names()
    print("source: %s" % json.dumps(g["_source"], ensure_ascii=False))
    print("attested mouth shapes : %s" % sorted(dsc.attested_mouth_shapes()))
    print("attested expression ids: %s" % sorted(dsc.attested_expression_ids()))
    for kind in ("mouth", "expression", "other"):
        print("\n== %s (%d) ==" % (kind, len(g[kind])))
        for r in sorted(g[kind], key=lambda x: (x["idx"] if x["idx"] is not None else 1 << 30,
                                                x["slot"])):
            ok = "-"
            if kind == "mouth":
                ok = "yes" if r["idx"] in dsc.attested_mouth_shapes() else "NOT ATTESTED"
            elif kind == "expression":
                ok = "yes" if r["slot"] in dsc.attested_expression_ids() else "NOT ATTESTED"
            print("   %-22s slot=0x%02X idx=%-4s anim=%-7d %s"
                  % (r["name"], r["slot"], r["idx"], r["anim"], ok))
    return 0


def _cmd_vmd(a):
    rows = vmd_names(a.vmd)
    print("%d distinct morph names in %s" % (len(rows), a.vmd))
    for r in rows:
        tag = "BLINK" if r["blink"] else ("junk:" + r["junk_reason"] if r["junk"]
                                          else ("animated" if r["peak"] else "baseline"))
        print("  %5d keys  w[%.2f..%.2f]  f%d..%d  %-8s %r"
              % (r["keys"], r["weight_min"], r["weight_max"], r["first_frame"], r["last_frame"],
                 tag, r["name"]))
    return 0


def _cmd_match(a):
    res = match(a.vmd, alias_file=a.alias)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(report(res))
    print("\n-- matched --")
    for n, g, slot, how in res["matched"]:
        print("  %-16s -> %-24s slot=%-4d (%s)" % (n, g, slot, how))
    print("\n-- ambiguous (needs a human choice) --")
    for x in res["ambiguous"]:
        print("  %-16s ?-> %s" % (x["name"], ", ".join(s["name"] for s in x["suggestions"])))
    print("\n-- unmatched --")
    for n in res["unmatched"]:
        print("  %r" % n)
    return 0


def _cmd_worklist(a):
    res = match(a.vmd, alias_file=a.alias)
    n = write_worklist(res, a.out)
    print("wrote %d lines for human fill-in -> %s" % (n, a.out))
    return 0


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # never let the codepage decide
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m diva_pv_tools.morph_core", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("names")
    p = sub.add_parser("vmd");        p.add_argument("vmd")
    p = sub.add_parser("match");      p.add_argument("vmd"); p.add_argument("--alias"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("worklist");   p.add_argument("vmd"); p.add_argument("out"); p.add_argument("--alias")
    a = ap.parse_args(argv)
    return {"names": _cmd_names, "vmd": _cmd_vmd, "match": _cmd_match, "worklist": _cmd_worklist}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
