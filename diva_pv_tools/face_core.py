"""VMD morphs -> the expression stream of a DIVA PV script (.dsc).

DIVA does not carry a face animation the way MMD does.  A PV script fires ``MOUTH_ANIM`` and
``EXPRESSION`` records at instants and the character holds each one until the next, so this is not
a per-key conversion - it is the rising edges.  Everything below is measured, not assumed, from the
shipping scripts (``main/rom``, ``main/rom_switch``, ``region``, ``dlcregion``, ``dlc00``: 774
files, every one framed by ``dsc_core.decode_fixed``):

    MOUTH_ANIM(chara, 0, shape, weight, hold)
        ``shape`` is the mouth table's own index (0..42).  Shipping scripts use 37 distinct values:
        0..33 plus 39, 41, 42.  34..38 and 40 exist in the table and are never sent - so they are
        not sent here either.  The ten most common are 28, 24, 25, 31, 32, 29, 33, 23, 30 - the
        ``MIK_KUCHI_*_OLD`` family (23..33), with 28 = ``MIK_KUCHI_RESET_OLD`` the shape SEGA
        returns to most.  ``weight`` spans 60..300 with 100 dominant; ``hold`` is a duration whose
        observed values run -1, 200..1000 (``hold`` = -1 and 1000 are both staples).
    EXPRESSION(chara, id, intensity, -1)
        ``id`` is the ``rob_mot_tbl`` **slot**, not a mouth-style index: shipping ids run 0..73 and
        include 4, 23 and 52..73, i.e. slot 0x04, 0x17 and 0x34..0x49 - 23 is ``MIK_FACE_WINK_OLD``
        and 55 ``MIK_FACE_GENTLE_OLD``, while those same two faces sit at index 4 and 14 of the
        expression list.  The two commands do not share a numbering, which is why
        ``plan_from_match`` picks ``idx`` for one and ``slot`` for the other.
        **Slot 29 is ``MIK_FACE_SMILE`` and no shipping script ever sends it**: an id that merely
        looks right is exactly the thing that must not be invented, so the emitter only accepts the
        57 ids in ``diva_face_targets.json``.

Every rule the emitter enforces has a counterpart in the file it writes, and
``dsc_core.validate`` re-reads the result and refuses it if any of them broke:

  * no bare ``TIME`` - a TIME whose block holds no records.  Only eight exist across all 774
    shipping scripts (all in SEGA's dlc00), and a block the replacement empties loses its TIME
    rather than keeping one;
  * TIME strictly increasing and never repeated;
  * no face cue after ``PV_END``;
  * END last, exactly once, and not one word after it;
  * every shape/id inside the measured sets.

The source morph names come from `morph_core` (DIVA name -> user answer -> shipped alias ->
approximation -> keyword -> worklist), so an expression that has no game slot never becomes a
guessed number: it stays on the worklist.

Timing follows the same rule as the rest of the exporter: the VMD is 30 fps, the script's TIME is
absolute ticks at 1e-5 s, and every frame number here is 60 fps, because that is what the exported
motion is sampled at.
"""
import collections
import os
import struct

from . import dsc_core as dsc            # noqa: E402  (the command table, the widths, the validator)
from . import morph_core                 # noqa: E402  (the name tables and the match tiers)
from . import vmd_reader as vmd          # noqa: E402  (the SJIS byte parser)

DST_FPS = 60.0
TICKS_PER_SEC = 100000          # TIME's unit, same scale as positions

# When a cue fires.  A fixed threshold cannot work across sources: the reference dance's lip tracks peak at
# 0.356..0.725, so a flat 0.45 would never once fire the い shape and a whole vowel would be lost,
# while a model that authors to 1.0 would trip on its gentlest in-between pose.  So each morph is
# judged against its own peak, with a floor so a morph that barely moves still has to do something
# real, and a hysteresis for the release.  The resulting density is what has to sit in SEGA's band:
# measured over the shipping charts, the 0x15122517 dialect runs 55..499 mouth cues per minute
# (median 192) and the converted MMD charts 136..928, so the reference dance's 302/min is inside it - reported on
# every export, and cappable with MOUTH_MIN_GAP_MS below if a chart ever needs thinning.
MOUTH_REL, MOUTH_FLOOR, MOUTH_FALL_RATIO = 0.45, 0.12, 0.45
EXPR_REL, EXPR_FLOOR = 0.50, 0.20
# Kept for callers that pass an explicit threshold and for the worklist's "is this morph animated"
# question; the per-morph rule above is what actually schedules cues.
MOUTH_RISE, EXPR_RISE, MOUTH_FALL = MOUTH_FLOOR, EXPR_FLOOR, MOUTH_FLOOR * MOUTH_FALL_RATIO
# `hold` is the duration the shape is pinned for, in the same milliseconds the shipping charts use.
# -1 is what the stock 0x14050921 scripts mostly carry, but in the 0x15122517 dialect this mod's
# chart belongs to it is rare (1022 of 21159 MOUTH_ANIM, all SEGA's own three charts) and the 20
# 3DPV MMD conversions of that same dialect use 1000 in all 19182 of theirs - so 1000 is the value
# with precedent here.
MOUTH_WEIGHT, MOUTH_HOLD, EXPR_INTENSITY = 100, 1000, 100
# 0 = no thinning (the default; the measured densities say it is not needed).  A positive value
# drops a cue that would land within that many milliseconds of the previous one for the same morph
# family, which is the knob to reach for if a chart ever comes out denser than the band above.
MOUTH_MIN_GAP_MS = 0
HEADER_WORDS = 3
FACE_COMMANDS = ("MOUTH_ANIM", "EXPRESSION")


class FaceError(Exception):
    """A plain message the UI layer can show; never a traceback."""


def plan_from_match(matched, game):
    """Turn `morph_core`'s matched rows into ``{vmd_name: (kind, param, game_name, how)}``.

    ``param`` is what the runtime command takes: the mouth table's index for ``MOUTH_ANIM``, the
    rob_mot_tbl slot for ``EXPRESSION`` (see the module docstring for the measurements).  A target
    outside the measured sets is refused here rather than written - that check is the last line of
    defence and it is deliberately redundant with `morph_core`'s.
    """
    by_name = {r["name"]: r for r in game["mouth"] + game["expression"]}
    shapes = dsc.attested_mouth_shapes()
    exprs = dsc.attested_expression_ids()
    plan = {}
    for vmd_name, game_name, _slot, how in matched:
        row = by_name.get(game_name)
        if row is None:
            raise FaceError("matched expression %s is not in the game's slot table" % game_name)
        if row["kind"] == "mouth":
            if row["idx"] not in shapes:
                raise FaceError("%s is mouth shape %d, which no shipping script sends - refusing "
                                "to write a number the engine was never seen to accept"
                                % (game_name, row["idx"]))
            plan[vmd_name] = ("mouth", row["idx"], game_name, how)
        else:
            if row["slot"] not in exprs:
                raise FaceError("%s is expression id %d, which no shipping script sends - "
                                "refusing to write a number the engine was never seen to accept"
                                % (game_name, row["slot"]))
            plan[vmd_name] = ("expression", row["slot"], game_name, how)
    return plan


def rest_shape(game):
    """The closed-mouth shape, i.e. what a mouth returns to between vowels.

    MIK_KUCHI_RESET (mouth index 8), the same default the packaged `diva_face_targets.json`
    records as `mouth_rest`, and one of the two most used shapes in the shipping charts.  Checked
    against the measured set like everything else.
    """
    for row in game["mouth"]:
        if row["name"].upper().endswith("KUCHI_RESET") and row["idx"] in dsc.attested_mouth_shapes():
            return row["idx"]
    raise FaceError("no attested MIK_KUCHI_RESET in the mouth table - refusing to guess the rest "
                    "shape")


def read_pv_db(base_dsc):
    """The `mod_pv_db.txt` that ships next to a mod's script, if there is one.

    A mod's script lives at ``<mod>/rom/script/pv_<id>_<difficulty>.dsc`` and its row is
    ``<mod>/rom/mod_pv_db.txt``.  Two things in it are worth checking before overwriting the file:
    ``script_format`` has to equal the script's own magic (the loader matches them and a mismatch is
    a PV that will not start) and ``performer.num`` bounds the ``chara`` argument.  Returns ``{}``
    when there is no db next to the script - a stock script has none, and that is not an error.
    """
    here = os.path.dirname(os.path.abspath(base_dsc))                 # .../rom/script
    rom = os.path.dirname(here)                                       # .../rom
    stem = os.path.splitext(os.path.basename(base_dsc))[0]
    pv = "_".join(stem.split("_")[:2]) if stem.startswith("pv_") else stem
    return _parse_pv_db([os.path.join(rom, "mod_pv_db.txt"), os.path.join(rom, "pv_db.txt")], pv)


def _parse_pv_db(paths, pv):
    for path in paths:
        if not os.path.isfile(path):
            continue
        out = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key.startswith(pv + "."):
                        out[key[len(pv) + 1:]] = value.strip()
        except OSError:
            continue
        if out:
            return {"_path": path, "_pv": pv, **out}
    return {}


def events_from_tracks(tracks, plan, frames, rest, chara=0, weight=MOUTH_WEIGHT, hold=MOUTH_HOLD,
                       min_gap_ms=MOUTH_MIN_GAP_MS):
    """Rising and falling edges of every planned morph, as 60 fps ``(frame, name, params)``.

    One scale for the whole file: lip sync is a *time* phenomenon, so a source frame is mapped by
    its fraction of the VMD's own span, which is exactly 2 for a 30 fps vmd stretched to 60 fps.
    Per-morph scaling would let a morph whose last key sits early run at the wrong speed.

    Edges are collected per morph, so a re-trigger before release only replaces that morph's own
    tail rather than cutting into another one's events.  `min_gap_ms` thins a morph's own cues that
    land closer together than that; 0 keeps every edge, which is the default because the measured
    densities say the stream does not need thinning.
    """
    if frames <= 0:
        raise FaceError("the timeline is %d frames long - nothing to schedule" % frames)
    last = max((k["frame"] for keys in tracks.values() for k in keys), default=0)
    if last <= 0:
        return [], [], {"thinned": 0, "density": 0.0}      # no animated morph at all:
        # events_from_tracks is not the layer that reports it, export_face refuses an empty list
    scale = frames / float(last + 1)
    events, skipped = [], []
    thinned = 0
    min_gap_frames = int(round(min_gap_ms / 1000.0 * DST_FPS)) if min_gap_ms else 0
    for name, keys in tracks.items():
        if name not in plan:
            if keys and max(k["weight"] for k in keys) >= MOUTH_FLOOR:
                skipped.append(name)          # animated but unresolved: it belongs on the worklist
            continue
        kind, param, game_name, _how = plan[name]
        if kind == "expression" and game_name.upper().endswith("_RESET"):
            # The default face.  Stock scripts never send it (MIK_FACE_RESET's slot 0x07 appears 0
            # times in the 3245 EXPRESSION records of the 0x15122517 dialect), and sending it would
            # wipe a face the chart is holding for no gain.
            continue
        keys = sorted(keys, key=lambda k: k["frame"])
        frame60 = lambda k: min(frames - 1, int(round(k["frame"] * scale)))     # noqa: E731
        peak = max(k["weight"] for k in keys)
        if kind == "mouth":
            rise, fall = max(MOUTH_FLOOR, MOUTH_REL * peak), max(MOUTH_FLOOR, MOUTH_REL * peak) * MOUTH_FALL_RATIO
        else:
            rise = max(EXPR_FLOOR, EXPR_REL * peak)
            fall = rise * 0.5
        own = []
        if kind == "mouth":
            open_at = None
            for k in keys:
                if k["weight"] >= rise:
                    if open_at is not None:
                        del own[open_at:]           # re-triggered before releasing: the later one wins
                    frame = frame60(k)
                    if min_gap_frames and own and frame - own[-1][0] < min_gap_frames:
                        thinned += 1                # same morph, still inside the gap: keep the earlier
                        open_at = len(own)
                        continue
                    own.append([frame, "MOUTH_ANIM", [chara, 0, param, weight, hold]])
                    open_at = len(own)
                elif k["weight"] < fall and open_at is not None:
                    open_at = None
                    own.append([frame60(k), "MOUTH_ANIM", [chara, 0, rest, weight, hold]])
        else:
            on = False
            for k in keys:
                if k["weight"] >= rise and not on:
                    on = True
                    own.append([frame60(k), "EXPRESSION", [chara, param, EXPR_INTENSITY, -1]])
                elif k["weight"] < fall:
                    on = False
        events += own
    events.sort(key=lambda e: e[0])
    span_min = (max((e[0] for e in events), default=0)) / DST_FPS / 60.0
    return ([(f, n, p) for f, n, p in events], sorted(skipped),
            {"thinned": thinned, "density": (len(events) / span_min) if span_min else 0.0})


def split(recs):
    """(prologue, [(tick, records)], tail) - the script's own block structure.

    Only END is pulled out: a face cue landing on the script's last instant must not push the
    ending off the end of the file, which is the one thing `dsc.decode_fixed` refuses to read.
    """
    head, blocks, cur = [], [], None
    for name, params in recs:
        if name == "END":
            return head, blocks, [("END", [])]
        if name == "TIME":
            cur = (params[0], [])
            blocks.append(cur)
            continue
        (head if cur is None else cur[1]).append((name, list(params)))
    return head, blocks, [("END", [])]


def encode(name, params):
    entry = dsc.NAME2ID.get(name)
    width = dsc.WIDTHS.get(name)
    if entry is None or width is None:
        raise FaceError("%s is not in the game's command table" % name)
    if len(params) != width:
        raise FaceError("%s takes %d params, given %d - a wrong count makes the engine read the "
                        "next command id as an argument" % (name, width, len(params)))
    return [entry] + [p & 0xFFFFFFFF for p in params]


def splice(base_recs, event_list, frames, drop_existing=False):
    """Base script + face events, every other record kept where it was.

    ``drop_existing`` removes the base's own ``MOUTH_ANIM``/``EXPRESSION`` records first.  A stock
    script carries its song's lip sync (pv_032 holds 755 of them), so splicing a second one on top
    plays two mouths at once - which is why the panel default is to replace.

    Two rules the splice enforces:

      * a block the replacement emptied loses its TIME.  A TIME whose block holds nothing is a
        **bare TIME**, and only 8 exist across all 774 shipping scripts.  Dropping the TIME is not
        a change to the timeline: TIME only sets the clock, and a mark with no records after it is
        overwritten by the next one.  Records are what carry the animation, and not one of them
        moves.
      * no cue lands on or after the instant that holds ``PV_END``, i.e. after the song's ending
        has been declared.  Cues at or after PV_END are dropped and counted.

    Returns ``(body, dropped_after_end)``; the caller reports the second number.
    """
    head, blocks, tail = split(base_recs)
    if drop_existing:
        head = [(n, p) for n, p in head if n not in FACE_COMMANDS]
        blocks = [(t, [(n, p) for n, p in rs if n not in FACE_COMMANDS]) for t, rs in blocks]
    end_tick = None
    for tick, records in blocks:
        if any(n == "PV_END" for n, _p in records):
            end_tick = tick
            break
    last_tick = blocks[-1][0] if blocks else 0
    per_frame = last_tick / float(max(1, frames - 1))
    if per_frame <= 0:
        raise FaceError("the base script's last TIME is %d - cannot place events on it" % last_tick)
    ticks = collections.OrderedDict((t, []) for t, _rs in blocks)
    for tick, records in blocks:
        for position, (name, params) in enumerate(records):
            ticks[tick].append(((0, position), name, params))
    dropped_after_end = 0
    for frame, name, params in event_list:
        tick = int(round(frame * per_frame))
        if end_tick is not None and tick >= end_tick:
            dropped_after_end += 1
            continue
        # A face cue with no block of its own joins the script's own spacing rather than the
        # nearest existing TIME: two cues 2 frames apart would otherwise land on one tick.
        ticks.setdefault(tick, []).append(((1, frame), name, params))
    body = []
    for name, params in head:
        body += encode(name, params)
    for tick in sorted(ticks):
        if not ticks[tick]:
            continue                     # would be a bare TIME - see the docstring
        body += encode("TIME", [tick])
        for _order, name, params in sorted(ticks[tick], key=lambda r: r[0]):
            body += encode(name, params)
    for name, params in tail:
        body += encode(name, params)
    return body, dropped_after_end


def script_bytes(magic, version, body):
    return struct.pack("<III", magic, version, 0) + struct.pack("<%dI" % len(body), *body)


def check_output(base_recs, out_recs, added, replaced):
    """Prove the spliced script kept what it should and only changed the face stream.

    ``replaced`` is the base's own face count that was dropped on the way in, so the expectation is
    ``base - replaced + added`` per command rather than a bare difference.
    """
    kept = [(n, tuple(p)) for n, p in base_recs if n not in FACE_COMMANDS and n != "TIME"]
    now = [(n, tuple(p)) for n, p in out_recs if n not in FACE_COMMANDS and n != "TIME"]
    if kept != now:
        first = next((i for i in range(min(len(kept), len(now))) if kept[i] != now[i]), 0)
        raise FaceError("the base script's records changed at %d: %s became %s"
                        % (first, kept[first] if first < len(kept) else None,
                           now[first] if first < len(now) else None))
    # Every instant the base had that still carries a record has to still be there.  An instant the
    # replacement emptied loses its TIME on purpose (that is what removes the bare TIMEs); an
    # instant that held a note, a stage cue or PV_END may not move or vanish.
    must_keep = {tick for tick, records in split(base_recs)[1]
                 if any(n not in FACE_COMMANDS for n, _p in records)}
    out_times = {p[0] for n, p in out_recs if n == "TIME"}
    if not must_keep <= out_times:
        raise FaceError("the base script lost %d of its own TIME blocks"
                        % len(must_keep - out_times))
    # TIME may grow, since a cue with no block of its own opens one, and may shrink by the marks the
    # replacement emptied; the two checks above are what prove the notes, stage cues and their order
    # came through untouched.
    retained = collections.Counter(n for n, _p in base_recs if n in FACE_COMMANDS) - replaced
    expect = collections.Counter(n for _f, n, _p in added)
    expect.update(retained)
    face = collections.Counter(n for n, _p in out_recs if n in FACE_COMMANDS)
    if dict(+face) != dict(+expect):
        raise FaceError("the script carries %s, expected %s" % (dict(+face), dict(+expect)))


def export_face(base_dsc, vmd_path, out_dsc, plan, game, frames=None, chara=0,
                weight=MOUTH_WEIGHT, hold=MOUTH_HOLD, overwrite=False, replace=True,
                min_gap_ms=MOUTH_MIN_GAP_MS, check_pv_db=True, solver=False, config=None,
                explain=None, cancel=None):
    """Splice the vmd's face stream into `base_dsc` and publish it as `out_dsc`.

    Reads the base with the verified widths, writes to a temp name, re-reads that, runs the full
    ``dsc_core.validate`` rule set plus the record-by-record comparison, and only then moves it onto
    the target.  ``replace`` drops the base's own MOUTH_ANIM / EXPRESSION records first, because a
    stock script carries its own song's lip sync.  ``frames`` is the 60 fps length of the dance;
    left None it comes from the base script's last TIME, which is only honest when the motion and
    that script were built for the same song.

    ``solver`` switches the *decision* layer only: instead of one rising/falling edge per morph it
    runs the sequence model in `face_retarget`, which sees every morph at once and scores each
    candidate against the priors measured from the shipping scripts.  Everything after the event
    list - splice, encode, validate, publish - is the same code either way, which is the point: the
    verified part stays verified.  ``explain`` optionally receives one record per decided event.
    """
    for path, what in ((base_dsc, "base"), (vmd_path, "motion")):
        if not os.path.isfile(path):
            raise FaceError("%s file not found: %s" % (what, path))
    if os.path.exists(out_dsc) and not overwrite:
        raise FaceError("%s already exists - pass overwrite to replace it" % out_dsc)
    vals, base_recs = dsc.decode_fixed(base_dsc)
    last_time = max((p[0] for n, p in base_recs if n == "TIME"), default=0)
    if frames is None:
        frames = int(round(last_time / float(TICKS_PER_SEC) * DST_FPS)) + 1
    db = read_pv_db(base_dsc) if check_pv_db else {}
    chara_limit = None
    if db.get("performer.num"):
        try:
            chara_limit = int(db["performer.num"])
        except ValueError:
            chara_limit = None
    if chara_limit is not None and not 0 <= chara < chara_limit:
        raise FaceError("the PV database next to the base script declares %d performer(s), so "
                        "character slot %d cannot exist - fix 'Character slot in the script'"
                        % (chara_limit, chara))
    if db.get("difficulty.extreme.0.script_format"):
        try:
            want = int(db["difficulty.extreme.0.script_format"], 16)
        except ValueError:
            want = None
        if want is not None and want != vals[0]:
            raise FaceError("the base script is a %#x script but the PV database next to it "
                            "declares script_format %#x - the game matches the two, so that script "
                            "would not load" % (vals[0], want))

    track = vmd.tracks(vmd.read(vmd_path)["morphs"])
    planned = collections.Counter(kind for kind, _param, _name, _how in plan.values())
    if solver:
        from . import face_retarget
        event_list, unmapped, thin = face_retarget.events_from_solver(
            track, plan, frames, cfg=config, chara=chara, weight=weight, hold=hold,
            explain=explain)
    else:
        event_list, unmapped, thin = events_from_tracks(track, plan, frames, rest_shape(game),
                                                       chara, weight, hold, min_gap_ms)
    if not event_list:
        # Say which of the two possible causes it is, because the fix is different for each: a plan
        # that is empty means nothing in this vmd has a name the slot table knows (usually the wrong
        # 'Slot table of' singer, or a model whose morphs nobody has aliased yet), while a non-empty
        # plan means the names resolved but never move.
        if not plan:
            seen = sorted(track, key=lambda n: -len(track[n]))[:6]
            raise FaceError(
                "nothing to export: not one of this vmd's %d morph names resolves against the %s "
                "slot table, so there is no cue to write. Either the morphs belong to a model "
                "nobody has aliased yet - export once, then answer the worklist and export again - "
                "or 'Slot table of' names the wrong singer for this dance. The heaviest names in "
                "the file are: %s" % (len(track), morph_core.CHARA, ", ".join(seen) or "none"))
        raise FaceError("nothing to export: %d morph name(s) matched a game slot and none of them "
                        "is raised above %.2f in this vmd, so every cue would be a rest. The "
                        "resolved names are: %s"
                        % (len(plan), MOUTH_RISE, ", ".join(sorted(plan)[:6])))
    dropped = (collections.Counter(n for n, _p in base_recs if n in FACE_COMMANDS)
               if replace else collections.Counter())
    body, after_end = splice(base_recs, event_list, frames, replace)
    blob = script_bytes(vals[0], vals[1], body)
    tmp = out_dsc + ".%d.tmp" % os.getpid()
    outdir = os.path.dirname(out_dsc)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    try:
        with open(tmp, "wb") as handle:
            handle.write(blob)
        _v2, out_recs = dsc.decode_fixed(tmp)
        check_output(base_recs, out_recs, event_list, dropped)
        problems = dsc.validate(tmp, magic=vals[0], version=vals[1], chara_limit=chara_limit)
        if problems:
            raise FaceError("refusing to publish: the written script breaks %d rule(s) that every "
                            "shipping script obeys:\n  - %s"
                            % (len(problems), "\n  - ".join(problems[:8])))
        if cancel is not None and cancel():
            raise FaceError("expression export was cancelled before publishing: %s was left "
                            "exactly as it was" % os.path.basename(out_dsc))
        os.replace(tmp, out_dsc)
    finally:
        if os.path.isfile(tmp):
            os.remove(tmp)
    counts = collections.Counter(n for _f, n, _p in event_list)
    how = collections.Counter(h for _k, _p, _n, h in plan.values())
    times = [p[0] for n, p in out_recs if n == "TIME"]
    bare = sum(1 for i in range(len(out_recs) - 1)
               if out_recs[i][0] == "TIME" and out_recs[i + 1][0] == "TIME")
    targets = collections.Counter(n for _k, _p, n, _h in plan.values())
    return {"base": os.path.abspath(base_dsc), "vmd": os.path.abspath(vmd_path),
            "out": os.path.abspath(out_dsc), "bytes": os.path.getsize(out_dsc),
            "records": len(out_recs), "base_records": len(base_recs), "frames": int(frames),
            "last_time": int(last_time), "mouth": counts.get("MOUTH_ANIM", 0),
            "expression": counts.get("EXPRESSION", 0), "planned": dict(planned),
            "unmapped_animated": unmapped, "replaced_base_face": dict(+dropped),
            "chara": int(chara), "plan_tiers": dict(how), "time_blocks": len(times),
            "bare_time": bare, "targets": dict(targets),
            "dropped_after_pv_end": after_end, "thinned": thin["thinned"],
            "cues_per_minute": round(thin["density"], 1), "min_gap_ms": min_gap_ms,
            "hold": hold, "weight": weight, "pv_db": db.get("_path", ""),
            "pv_db_performers": chara_limit, "validated": True}
