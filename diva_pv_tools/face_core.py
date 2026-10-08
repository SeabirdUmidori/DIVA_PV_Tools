"""VMD morphs -> the mouth (lip-sync) stream of a DIVA PV script (.dsc).

DIVA does not carry a face animation the way MMD does.  A PV script fires ``MOUTH_ANIM`` records at
instants and the character holds each one until the next, so this is not a per-key conversion - it
is the rising edges.  Everything below is measured, not assumed, from the shipping scripts
(``main/rom``, ``main/rom_switch``, ``region``, ``dlcregion``, ``dlc00``: 774 files, every one
framed by ``dsc_core.decode_fixed``):

    MOUTH_ANIM(chara, 0, shape, weight, hold)
        ``shape`` is the mouth table's own index (0..42).  Shipping scripts use 37 distinct values:
        0..33 plus 39, 41, 42.  34..38 and 40 exist in the table and are never sent - so they are
        not sent here either.  The ten most common are 28, 24, 25, 31, 32, 29, 33, 23, 30 - the
        ``MIK_KUCHI_*_OLD`` family (23..33), with 28 = ``MIK_KUCHI_RESET_OLD`` the shape SEGA
        returns to most.  ``weight`` spans 60..300 with 100 dominant; ``hold`` is a duration whose
        observed values run -1, 200..1000 (``hold`` = -1 and 1000 are both staples).

**This module transplants the mouth and nothing else.**  MMD expression morphs (笑い, 怒り, 涙 ...)
are deliberately *not* converted to ``EXPRESSION`` records: a DIVA face is a held state that carries
the eyes, so a wrong - or merely stale - face is far more visible than a missing one, and the
shipping corpus has 24 scripts that cue ``MOUTH_ANIM`` with no ``EXPRESSION`` at all, which is the
evidence that a mouth-only script is a shape the engine accepts.  The expression morphs stay on the
worklist, where a human can decide about them.

The one expression the exporter does write on its own account is the **blink**: the dance's まばたき
curve drives the eye directly, with ``EXPRESSION(chara, 22, 100, 1000)`` at every frame the morph sits
at full closure and ``EXPRESSION(chara, 21, 100, 0)`` the frame it leaves that value.  The engine's own
procedural blink (``AUTO_BLINK``) is deliberately **not** used: the two are mutually exclusive drivers
of one eyelid, and following the dance is the point - a ``.vmd`` that blinks is a ``.vmd`` whose blink
the character should reproduce.  ``AUTO_BLINK`` is never written.

The two ids are not in ``diva_face_targets.json``: that table is a corpus measurement of the
``rob_mot_tbl`` slot ids the shipping charts send, and 22 is not among them.  They are attested by
in-game testing instead (confirmed on built-up scripts that were loaded in game), so they are passed
to the validator as an explicit, named exemption rather than
folded into a measurement they are not part of.  See ``dsc_core.validate(extra_expression_ids=...)``.

Every rule the emitter enforces has a counterpart in the file it writes, and ``dsc_core.validate``
re-reads the result and refuses it if any of them broke:

  * no bare ``TIME`` - a TIME whose block holds no records.  Only eight exist across all 774
    shipping scripts (all in SEGA's dlc00), and a block the replacement empties loses its TIME
    rather than keeping one;
  * TIME strictly increasing and never repeated;
  * no face cue after ``PV_END``;
  * END last, exactly once, and not one word after it;
  * every shape/id inside the measured sets (plus the blink exemption above).

The source morph names come from `morph_core` (DIVA name -> user answer -> shipped alias ->
approximation -> keyword -> worklist), so a mouth morph that has no game shape never becomes a
guessed number: it stays on the worklist.

Timing follows the same rule as the rest of the exporter: the VMD is 30 fps, the script's TIME is
absolute ticks at 1e-5 s, and every frame number here is 60 fps, because that is what the exported
motion is sampled at.
"""
import collections
import json
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
# Kept for callers that pass an explicit threshold and for the worklist's "is this morph animated"
# question; the per-morph rule above is what actually schedules cues.
MOUTH_RISE, MOUTH_FALL = MOUTH_FLOOR, MOUTH_FLOOR * MOUTH_FALL_RATIO
# `hold` is the duration the shape is pinned for, in the same milliseconds the shipping charts use -
# and the engine **releases the shape when it runs out**.  The rule is: a record has to stand at least
# until its successor, and no further.  Both ends of that are corpus values - `1000` when 1000 ms
# reaches the next cue (the 20 3DPV conversions of this dialect carry 1000 in all 19182 of their
# MOUTH_ANIM), and `-1` ("stand until the next cue") when it does not.
#
# Writing the *measured* gap instead was wrong in the other direction and was visible in game as a
# mouth that barely moves: on a fast dance the gap between one morph's cues is ~67 ms, so every shape
# was pinned for 67 ms, released, and left the mouth at rest until the next cue.  1000 always covered
# the gap, which is why the fixed value worked everywhere except a shape held longer than a second
# (the reference FACIAL dance holds `あ` for 13.1 s and `困る` for 13.2 s).
MOUTH_WEIGHT, MOUTH_HOLD, EXPR_INTENSITY = 100, 1000, 100
# 0 = no thinning (the default; the measured densities say it is not needed).  A positive value
# drops a cue that would land within that many milliseconds of the previous one for the same morph
# family.  It thins *shape* events only, and it applies to the edge exporter - the solver's own
# duration model is what bounds its event rate.  The rate a finished chart usually has to be brought
# down by is the strength re-statement interval instead; see `STRENGTH_STEP_MS`.
MOUTH_MIN_GAP_MS = 0
HEADER_WORDS = 3
FACE_COMMANDS = ("MOUTH_ANIM", "EXPRESSION")

# ------------------------------------------------------------------------------------------------
# The blink.  Everything else an MMD face carries is left on the worklist; the eyelid is the one
# piece the exporter still translates, under the panel's own switch.
#
# `まばたき` has no DIVA slot (`morph_core`'s docstring records the measurement: the MIK set holds no
# BLINK / MABATAKI / LID name), so the eye is driven through the *face* command instead:
#
#   EXPRESSION(chara, EXPR_LID_SHUT, EXPR_INTENSITY, EXPR_BLINK_HOLD)   eyes shut
#   EXPRESSION(chara, EXPR_LID_OPEN, EXPR_INTENSITY, EXPR_BLINK_CLEAR)  back to normal
#
# Both ids and both hold values are the ones a game-verified script carries (a built-up script with
# 146 expression records, ids {21, 22}, alternating `(0, 22, 100, 1000)` and `(0, 21, 100, 0)`).
EXPR_LID_OPEN, EXPR_LID_SHUT = 21, 22
EXPR_BLINK_HOLD, EXPR_BLINK_CLEAR = 1000, 0
#: What the validator is told to accept beyond the corpus measurement, and nothing else.
BLINK_EXPRESSION_IDS = (EXPR_LID_OPEN, EXPR_LID_SHUT)

#: The pv_expression switch.  Without this one record the engine never reads the eye-motion file
#: written beside the script, so the eyes do not move however good that file is.  Measured over the
#: 349 shipping scripts plus the mod scripts on this machine: **all 114** scripts whose PV ships a
#: `pv_expression/exp_PV*.bin` carry it and **not one** that lacks the bin has it.  527 records in
#: total, always at tick 0, always exactly `[chara, 23, -1, -1]`, one per performer.
EXPR_PV_EXPRESSION_SWITCH = 23

# ------------------------------------------------------------------------------------------------
# The expression transplant.
#
# A rule is a CONDITION on the dance's morph weights - `困る` not zero, `まばたき` at or above 0.2 -
# and a face is worn while its condition holds.  The rules live in `data/expression_rules.json` as
# data; this is only the evaluator.
RULES_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                          "expression_rules.json")
CONFIDENCE_ORDER = {"guess": 0, "probe": 1}


def _build_banner():
    """The running build's id and version, for a message that has to say *which* copy is talking."""
    try:
        from . import BUILD_ID, bl_info
        return "%s (%s)" % (BUILD_ID, ".".join(str(v) for v in bl_info.get("version", ())))
    except Exception:                      # pragma: no cover - never fatal
        return "unknown"


def _rule_threshold(req):
    """The number a requirement tests against, for the specificity comparison."""
    return float(req.get("min", req.get("gt", 0.0)))


def _rule_specificity(all_of):
    """`(summed thresholds, requirement count)`.  Higher means harder to satisfy.

    Used *inside* one tier.  Thresholds are compared before the count, and that order is
    load-bearing: `MIK_FACE_CLOSE` is one requirement (`まばたき >= 1`) while `MIK_FACE_UTURO` is two.
    Counting first let every two-condition rule beat CLOSE, so the shut-eye face could never be
    reached at a full blink - it won zero times in a 200 000-sample sweep.  (The tier table now puts
    CLOSE above UTURO anyway, but the ordering still has to be sane within a tier.)

    An `any` group contributes its *easiest* alternative, because that is what the rule demands.
    """
    count, total = 0, 0.0
    for req in all_of:
        count += 1
        if "any" in req:
            total += min((_rule_threshold(r) for r in req["any"]), default=0.0)
        else:
            total += _rule_threshold(req)
    return round(total, 6), count


def _check_requirement(req, where, problems, morphs):
    """Validate one requirement and collect the morphs it names."""
    if not isinstance(req, dict):
        problems.append("%s: %r is not a requirement" % (where, req))
        return
    if "any" in req:
        options = req["any"]
        if not isinstance(options, list) or not options:
            problems.append("%s: `any` needs a non-empty list" % where)
            return
        for option in options:
            _check_requirement(option, where + "/any", problems, morphs)
        return
    name = req.get("morph")
    if not name:
        problems.append("%s: a requirement needs `morph` or `any`" % where)
        return
    has = [k for k in ("min", "gt") if k in req]
    if len(has) != 1:
        problems.append("%s: %s needs exactly one of `min` or `gt`, found %s"
                        % (where, name, has or "neither"))
        return
    value = req[has[0]]
    if not isinstance(value, (int, float)) or value < 0:
        problems.append("%s: %s %s=%r is not a threshold" % (where, name, has[0], value))
        return
    morphs.add(name)


def expression_rules(path=None):
    """The rule table, validated - a bad table is refused loudly, never half-applied."""
    path = path or RULES_JSON
    if not os.path.isfile(path):
        raise FaceError("%s is missing - the expression transplant has no rules. Reinstall the "
                        "add-on, or point --rules at your own table" % path)
    with open(path, encoding="utf-8") as handle:
        blob = json.load(handle)
    settings = dict(blob.get("settings") or {})
    floor = settings.get("min_confidence", "probe")
    if floor not in CONFIDENCE_ORDER:
        raise FaceError("min_confidence is %r, which is not one of %s"
                        % (floor, ", ".join(sorted(CONFIDENCE_ORDER))))
    rules, problems, morphs = [], [], set()
    seen_cond, skipped = {}, []
    for row in blob.get("rules") or []:
        idx = row.get("idx")
        name = row.get("name") or ("idx %s" % idx)
        if not isinstance(idx, int) or idx < 0:
            problems.append("rule %r has no usable idx" % name)
            continue
        all_of = row.get("all")
        if not all_of:
            continue                      # a face with no condition yet: the user will fill it
        if not isinstance(all_of, list):
            problems.append("%s: `all` has to be a list of requirements" % name)
            continue
        for req in all_of:
            _check_requirement(req, name, problems, morphs)
        if CONFIDENCE_ORDER.get(row.get("confidence", "guess"), 0) < CONFIDENCE_ORDER[floor]:
            skipped.append(name)
            continue                      # below the floor: kept in the file, not emitted
        signature = json.dumps(all_of, sort_keys=True, ensure_ascii=False)
        if signature in seen_cond:
            problems.append("%s and %s are satisfied by exactly the same weights - only the first "
                            "can ever win; narrow one of them" % (seen_cond[signature], name))
        seen_cond.setdefault(signature, name)
        tier = row.get("tier", 4)
        if not isinstance(tier, int) or tier < 1:
            problems.append("%s: `tier` is %r, which is not a positive whole number" % (name, tier))
            continue
        rules.append({"idx": idx, "name": name, "all": all_of, "from": row.get("from", ""),
                      "tier": tier,
                      "priority": int(row.get("priority", 0)),
                      "specificity": _rule_specificity(all_of),
                      "morphs": sorted({r.get("morph") for r in _flatten(all_of)
                                        if isinstance(r, dict) and r.get("morph")}),
                      "confidence": row.get("confidence", "guess")})
    if problems:
        raise FaceError("the expression rule table is inconsistent:\n  - %s" % "\n  - ".join(problems))
    if not rules:
        raise FaceError("no rule in %s is at or above min_confidence %r - the transplant would "
                        "write nothing" % (path, floor))
    neutral = blob.get("neutral") or {}
    return {"path": path, "settings": settings, "rules": rules, "morphs": sorted(morphs),
            "epsilon": float(settings.get("epsilon", 0.001)),
            "neutral": int(neutral.get("idx", EXPR_LID_OPEN)),
            "neutral_name": neutral.get("name", "MIK_FACE_RESET"),
            "skipped_unverified": skipped}


def _flatten(all_of):
    """Every leaf requirement, with `any` groups opened out."""
    for req in all_of:
        if "any" in req:
            for option in _flatten(req["any"]):
                yield option
        else:
            yield req


def _morph_grid(tracks, frames):
    """`{morph name: [weight per 60 fps frame]}` for the morphs the rules actually ask about."""
    return {name: _sample_grid(keys, frames) for name, keys in tracks.items()}


def _met(req, weights, frame, epsilon):
    if "any" in req:
        return any(_met(option, weights, frame, epsilon) for option in req["any"])
    series = weights.get(req["morph"])
    value = series[frame] if series else 0.0
    if "min" in req:
        return value >= req["min"]
    return value > req["gt"] + epsilon


def best_rule(table, weights, frame):
    """The face to wear at `frame`, or None.  The single place the winner is decided.

    `tier` comes first: while any face in a higher tier is satisfied, nothing in a lower tier can
    take over, which is the whole point of the tiers - a wink or a shut eye outranks every expression,
    a named expression with its own second condition outranks a bare one, and the eyelid at a low
    weight is last.  Within one tier: `priority`, then the largest sum of thresholds, then the most
    requirements, then the earlier rule in the file.
    """
    epsilon = table.get("epsilon", 0.001)
    best, best_key = None, None
    for order, rule in enumerate(table["rules"]):
        if not all(_met(req, weights, frame, epsilon) for req in rule["all"]):
            continue
        key = (-rule["tier"], rule["priority"], rule["specificity"][0],
               rule["specificity"][1], -order)
        if best_key is None or key > best_key:
            best, best_key = rule, key
    return best


def bridge_gaps(winners, neutral, bridge_frames):
    """Hold a face across a moment where nothing matches, when another face is about to.

    A DIVA face is a held state, so the naive reading - "the condition stopped being true, clear
    it" - writes `A`, then `neutral`, then `B` where the dance meant to go straight from `A` to
    `B`.  Every one of those `neutral` cues is a visible flicker through the rest face, and on the
    reference dance they were 65 of 215 cues.  A moment with no matching blend is a gap *between*
    two faces, not a face of its own: this holds the outgoing face across it, so the next face is
    reached by a single switch.

    A gap is only bridged when a face is on **both** sides of it and the gap is no longer than
    `bridge_frames`.  Anything else - a gap at the start, a gap at the end, or simply nothing
    matching for a while - is left alone, because there the rest face is the honest answer.

    Returns `(winners, bridged_frame_count)`.
    """
    out = list(winners)
    bridged, i, n = 0, 0, len(out)
    while i < n:
        if out[i] != neutral:
            i += 1
            continue
        j = i
        while j < n and out[j] == neutral:
            j += 1
        left = out[i - 1] if i > 0 else None
        right = out[j] if j < n else None
        if (left is not None and left != neutral and right is not None and right != neutral
                and (j - i) <= bridge_frames):
            for k in range(i, j):
                out[k] = left
            bridged += j - i
        i = j
    return out, bridged


def expression_events(tracks, table, frames, chara=0, min_hold_s=None,
                      intensity=None, hold=None, explain=None, bridge_s=None):
    """The dance's morphs -> `EXPRESSION` cues, by testing the rule table.

    Returns ``(events, info)`` with events as ``(frame, "EXPRESSION", [chara, idx, intensity,
    hold])``.  A cue is written only when the *winning rule changes*, which is the whole point: a
    DIVA face is a state the engine holds, so writing it every frame would be 11 000 records of the
    same number.

    Three passes, in this order, and the order is the behaviour:

      1. **debounce** - a rule has to keep the lead for `min_hold_s` before it counts, so a two-frame
         wobble between two conditions does not become two cues;
      2. **bridge** - a moment where nothing is satisfied but another face is coming is held across,
         so the face goes `A -> B` instead of `A -> neutral -> B` (`bridge_gaps`);
      3. **edges** - only the changes are written, and a dance that ends on a face gets the neutral
         cue so the face is not left on.
    """
    settings = table["settings"]
    min_hold_s = settings.get("min_hold_s", 0.2) if min_hold_s is None else min_hold_s
    intensity = settings.get("intensity", EXPR_INTENSITY) if intensity is None else intensity
    hold = settings.get("hold", MOUTH_HOLD) if hold is None else hold
    bridge_s = settings.get("bridge_s", 0.4) if bridge_s is None else bridge_s

    wanted = set(table.get("morphs") or [m for r in table["rules"] for m in r["morphs"]])
    weights = _morph_grid({n: k for n, k in tracks.items() if n in wanted}, frames)
    missing = sorted(m for m in wanted if m not in weights)

    # Winner per frame, then debounce: a rule has to hold the lead for min_hold_s before it counts.
    hold_frames = max(1, int(round(min_hold_s * DST_FPS)))
    winner, streak, current = [], 0, None
    for f in range(frames):
        best = best_rule(table, weights, f)
        candidate = best["idx"] if best is not None else table["neutral"]
        if candidate == current:
            streak += 1
        else:
            current, streak = candidate, 1
        winner.append(current if streak >= hold_frames else
                      (winner[-1] if winner else table["neutral"]))

    # A gap between two faces is not a face: hold the outgoing one across it and switch straight to
    # the next, instead of flickering through the rest face and back.
    winner, bridged = bridge_gaps(winner, table["neutral"],
                                  max(0, int(round(bridge_s * DST_FPS))))

    # Edges only.
    events, rows = [], []
    previous = None
    for f, idx in enumerate(winner):
        if idx == previous:
            continue
        if previous is not None or idx != table["neutral"]:
            events.append((f, "EXPRESSION", [int(chara), int(idx), int(intensity), int(hold)]))
        previous = idx
    # A dance that ends on a face must not leave it on: EXPRESSION is a held state.
    if previous not in (None, table["neutral"]):
        events.append((frames - 1, "EXPRESSION",
                       [int(chara), int(table["neutral"]), int(intensity), 0]))
    if explain is not None:
        for f, _name, params in events:
            rule = next((r for r in table["rules"] if r["idx"] == params[1]), None)
            explain.append({"frame": f, "idx": params[1],
                            "name": rule["name"] if rule else table["neutral_name"],
                            "condition": rule["from"] if rule else ""})
    info = {"rules": len(table["rules"]), "matched_morphs": len(wanted) - len(missing),
            "missing_morphs": missing, "neutral": table["neutral"],
            "epsilon": table.get("epsilon", 0.001),
            "min_hold_s": min_hold_s, "bridge_s": bridge_s, "rules_file": table["path"],
            "resets": sum(1 for _f, _n, p in events if p[1] == table["neutral"]),
            "bridged_frames": bridged, "emitted": len(events),
            "faces_used": sorted({p[1] for _f, _n, p in events})}
    return events, info

#: The morphs that mean "eyelid down".  `morph_core.eyelid_role` owns the wink spellings; only the
#: both-eyes blink is translated here, because DIVA's face command has no per-eye form.
BLINK_MORPHS = ("まばたき", "瞬き", "blink")
#: `まばたき` counts as shut only at **full** closure.  MMD authors a blink as a spike to 1.0, and a
#: half-close is deliberately not treated as a blink: the face command is a held state, so acting on
#: 0.4 would leave the character squinting until the next cue.
BLINK_SHUT_LEVEL = 1.0
BLINK_EPS = 1e-6


class FaceError(Exception):
    """A plain message the UI layer can show; never a traceback."""


#: `hold` is a **duration in milliseconds** after which the engine releases the shape, so a shape is
#: only still on the character while its own record has not expired.  Two attempts to make a long
#: hold work by changing this number both failed in game:
#:
#:   * writing the **measured** gap to the next cue pinned fast-dance shapes for ~67 ms, which
#:     released them almost immediately - a mouth that barely moves;
#:   * writing `-1` for a gap longer than 1000 ms relied on the stock dialect's "stand until the next
#:     cue", and the mouth still would not hold.
#:
#: So the number stays at the value that demonstrably works and the **invariant is enforced instead**:
#: no two `MOUTH_ANIM` records may be more than `MOUTH_HOLD` apart, and `repeat_held_shapes` re-issues
#: the current shape whenever they would be.  That depends on nothing but the 1000 ms that the 20 3DPV
#: MMD conversions of this dialect carry in all 19182 of their cues.


#: How far apart two `MOUTH_ANIM` records may be before `repeat_held_shapes` re-issues the shape.
#: 900 ms, deliberately under `MOUTH_HOLD`, so the replacement always lands before the one it replaces
#: expires even if a frame rounds the wrong way.
MOUTH_REPEAT_MS = 900

#: How often `apply_strength` may re-state a cue's strength while a shape is held.
#:
#: `0` would be exact: the engine holds a record's value until the next one, so a record wherever the
#: rounded value changed is the fewest records that make every single frame agree with the `.vmd`.
#: It is not the default, and the reason is a measurement with two independent halves:
#:
#:   * the working corpus re-states a strength **sparsely**.  Over the 135 MMD->DIVA conversions
#:     shipped in this machine's game mods, 45.7% of consecutive `MOUTH_ANIM` records keep the same
#:     shape and 77% of those change only the weight - so the corpus *does* track a strength, at a
#:     median of **123 re-statements per minute** (p90 158, max 189, pooled 121 over 446 min of
#:     music, and their most common same-shape interval is 133 ms).  Exact per-frame tracking is
#:     ~1900/min, about 15x that;
#:   * the same project's `tests/check_corpus_bounds.py` is a **gate**, not a report: it refuses a
#:     script that falls outside the envelope of all 1097 shipping scripts on record count, TIME
#:     count or face-cue density, because those are the axes a loader can trip on.  Exact tracking
#:     puts the export at 17222 records / 8065 TIME / 461 cues-per-second against limits of
#:     6951 / 2526 / 122.  At 133 ms it is inside all three.
#:
#: 133 ms is therefore the default: the corpus's own most common same-shape re-statement interval
#: (8 frames on the 60 fps output grid, 4 source frames at 30 fps), and the finest interval measured
#: to keep the export inside the envelope.  A strength is still read from the dance's own curve at
#: every cue; this only decides how many cues carry one.  Set it to 0 for literal per-frame
#: agreement, knowing what it costs on the axes above.
STRENGTH_STEP_MS = 133


def repeat_held_shapes(events, hold=MOUTH_HOLD, repeat=MOUTH_REPEAT_MS):
    """Re-issue the current mouth shape wherever `hold` would otherwise lapse mid-dance.

    The rule the engine imposes is per record: a shape is on the character for `hold` ms and then it
    is released.  Nothing carries a shape from one record to the next except the next record, so a
    dance that holds one shape for 13 seconds is only held if something re-states it inside every
    second of that.  This walks the `MOUTH_ANIM` stream and inserts copies of the shape in force
    wherever the gap to the next real cue is longer than `repeat`.

    Returns `(events, inserted)`.
    """
    mouth = [e for e in events if e[1] == "MOUTH_ANIM"]
    if len(mouth) < 2:
        return events, 0
    step = max(1, int(round(repeat / 1000.0 * DST_FPS)))
    added = []
    for prev, nxt in zip(mouth, mouth[1:]):
        gap = nxt[0] - prev[0]
        if gap <= step:
            continue
        # Evenly spaced, and never further apart than `step` <= `hold`.
        count = -(-gap // step) - 1
        for i in range(1, count + 1):
            frame = prev[0] + int(round(gap * i / float(count + 1)))
            added.append((frame, "MOUTH_ANIM", list(prev[2])))
    if not added:
        return events, 0
    return sorted(list(events) + added, key=lambda e: e[0]), len(added)



def plan_from_match(matched, game):
    """Turn `morph_core`'s matched rows into ``{vmd_name: ("mouth", idx, game_name, how)}``.

    ``param`` is what the runtime command takes: the mouth table's index.  A target outside the
    measured sets is refused here rather than written - that check is the last line of defence and it
    is deliberately redundant with `morph_core`'s.

    Expression rows are **dropped, not refused**: `morph_core.match` no longer offers one, but a
    caller that hands over an old match result (a saved worklist, a probe) must not be able to put
    an ``EXPRESSION`` cue back into the stream through this door.  The morph stays out of the plan,
    so `events_from_tracks` reports it on the worklist like any other unresolved name.
    """
    by_name = {r["name"]: r for r in game["mouth"] + game["expression"]}
    shapes = dsc.attested_mouth_shapes()
    plan = {}
    for vmd_name, game_name, _slot, how in matched:
        row = by_name.get(game_name)
        if row is None:
            raise FaceError("matched morph %s is not in the game's slot table" % game_name)
        if row["kind"] != "mouth":
            continue
        if row["idx"] not in shapes:
            raise FaceError("%s is mouth shape %d, which no shipping script sends - refusing "
                            "to write a number the engine was never seen to accept"
                            % (game_name, row["idx"]))
        plan[vmd_name] = ("mouth", row["idx"], game_name, how)
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
    ``script_format`` - a mismatch with the script's own magic is reported as a note, not refused
    (MEGA39's PLUS ships official pairs that disagree; see export_face) - and ``performer.num``,
    which bounds the ``chara`` argument.  Returns ``{}``
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


#: The engine's own scale for the strength of a worn shape, measured from 346 995 `MOUTH_ANIM`
#: weight values in **484 of SEGA's own shipping scripts** (`main/rom` plus the X-edition scripts
#: shipped in the game's mods):
#:
#:     p10 48   p25 100   MEDIAN 107   p75 130   p90 200
#:     42.7% of every value in the corpus is in [100, 125); only 14.9% is below 50.
#:
#: So `weight` is **not** a 0..100 blend fraction of which a dance uses the bottom fifth: a worn
#: shape is fed at about **100**, and the band the engine reads is the one this module already
#: documents above - `60..300 with 100 dominant`.  A dance's MMD morph weight is a different quantity
#: on a different scale, which is the same reason the trigger threshold is already taken relative to
#: each morph's own peak (`MOUTH_REL`) rather than in absolute terms, and the reason a reference
#: dance's lip tracks peaking at 0.356..0.725 could never arm a flat 0.45.
#:
#: Writing the raw morph weight put our cues at a median of **17** with **97.9%** of them below 50 -
#: a band SEGA uses 14.9% of the time - so every shape was fed too faint to form and the character
#: kept its default mouth instead of showing the shape the dance asked for.  The measured fix is to
#: take the level relative to that morph's own peak and map it onto the engine's band:
#:
#:     weight = MOUTH_WEIGHT_TOP * level / peak_of_that_morph
#:
#: With the top at the band's own 300, the export's per-cue weight distribution is p25 72,
#: median 100, p75 118 against the corpus's 100 / 107 / 130, and 35.0% of its cues fall in the
#: corpus's dominant [100, 125) band against the corpus's 42.7%.  A hill-climb on that comparison
#: puts the best fit at 250 (median 92); 300 is taken because it is the corpus's own measured top and
#: because it puts the median exactly where the corpus's is.
MOUTH_WEIGHT_TOP = 300

#: What a rest cue carries.  "The mouth is at rest" is a shape the engine wears, not zero of one:
#: SEGA feeds `MIK_KUCHI_RESET` at p25 83 / median 100 / p75 130, and a rest cue written **0**
#: applies nothing at all, which leaves the mouth wherever the previous shape put it.  Every cue is
#: written at this value before `apply_strength` runs, so a cue with no curve behind it keeps it.
MOUTH_REST_WEIGHT = MOUTH_WEIGHT


def _strength_percent(relative):
    """A dance's level **relative to that morph's own peak**, as the value the engine reads."""
    return max(0, min(MOUTH_WEIGHT_TOP, int(round(float(relative) * MOUTH_WEIGHT_TOP))))


#: `EXPRESSION`'s intensity is the same kind of held value, but SEGA's own idiom for it is far
#: tighter than the mouth's.  Over the same 484 scripts, of 6241 expression intensities:
#:
#:     100 (exactly): 64.2%      200..249: 7.3%      below 50: 0.4%      -1 (sentinel): 28.1%
#:
#: A worn face is fed at **100**; nothing below 50 essentially ever occurs (0.4%).  That matters
#: here because the rule table's conditions are *thresholds* (`困る > 0`, `まばたき >= 1`): a rule
#: that has only just been armed has its condition at ~0.001, so a linear 0..300 scale wrote it an
#: intensity of **0** - an armed face fed too faint to show, which was **24.1%** of the reference
#: export's expression cues.  The level now modulates *within* the band the engine uses rather than
#: down to nothing: an armed face is at least `EXPR_FLOOR`, and a condition at full weight reaches
#: `EXPR_TOP`.
EXPR_FLOOR, EXPR_TOP = 100, 200


def _intensity_percent(relative):
    """A rule's condition level, as the `EXPRESSION` intensity the engine reads."""
    span = EXPR_TOP - EXPR_FLOOR
    return max(EXPR_FLOOR, min(EXPR_TOP, int(round(EXPR_FLOOR + float(relative) * span))))


def _rule_morphs(rule, out=None):
    """Every morph name a rule's condition reads, at any nesting depth (`any` included)."""
    out = [] if out is None else out
    for req in (rule.get("all") or []):
        if "any" in req:
            _rule_morphs({"all": req["any"]}, out)
        elif req.get("morph"):
            out.append(req["morph"])
    return out


def apply_strength(events, track, plan, frames, rules_path=None, step_ms=STRENGTH_STEP_MS):
    """Write the dance's own strength on every face cue, and split a cue wherever it changes.

    The engine holds a record's value until the next record, so a cue whose strength has to *track*
    the dance needs a record wherever the value moves.  Each cue first takes the strength of its own
    frame, then a record is inserted at every later frame where the rounded value differs - which is
    the fewest records that make every frame agree with the `.vmd`.

    `step_ms` bounds how often that happens; see `STRENGTH_STEP_MS` for why the default is 133 ms
    rather than 0.  It is a different clock from `MOUTH_MIN_GAP_MS`, which thins *shape* events: the
    corpus thins those and re-states strengths at different rates, so one number cannot serve both.

    A `MOUTH_ANIM` reads the planned morphs that produce its shape; an `EXPRESSION` reads the morphs
    its own rule's condition is written on.  Neither is ever changed: only the strength is.

    A shape is looked up through the **viseme it realises**, not by its own id.  One articulation has
    several measured assets (`MIK_KUCHI_SMILE` and `MIK_KUCHI_SMILE_L`, `A` and `A_OLD`) and the
    solver's variation layer picks between them on purpose, so a cue can carry an id the plan never
    named directly.  Keyed by id, such a cue found no morphs at all and its strength came out **0**
    whatever the dance said - measured on the reference export, 34 of 4122 cues: every one of them an
    asset variant, every one of them written 0 against a curve that was not.  Keyed by viseme, the
    variant reads the same evidence the decision was made from.

    A rest cue keeps `MOUTH_REST_WEIGHT` rather than being written 0, and a viseme with no moving
    morph behind it is the rest state.

    An **`EXPRESSION` id the rule table has no condition for is left exactly as the layer that
    produced it wrote it.**  There is no curve to read, so the only thing this function could put
    there is a number it invented - and the two ids in that position are precisely the ones whose
    value is not a strength at all: the eye-motion switch `(chara, 23, -1, -1)`, whose -1 is a
    sentinel, and the **eyelid-open cue `(chara, 21, 100, 0)`**, which is the game-verified record
    that opens an eye left shut.  Zeroing the latter is not a cosmetic error - the exporter's own
    promise is that a dance can never end with the eyes closed, and that promise is carried by this
    number.

    Returns `(events, inserted)`.
    """
    from . import face_retarget           # lazy: face_retarget imports this module back
    grid = {}
    for name, keys in track.items():
        if keys:
            grid[name] = _sample_grid(keys, frames)
    # Each morph's own peak: the level a cue carries is read *relative* to it, for the same measured
    # reason the trigger threshold is (see `_strength_percent`).
    peaks = {name: max(curve) for name, curve in grid.items() if curve}

    def value_at(names, frame):
        best = 0.0
        for name in names:
            curve = grid.get(name)
            peak = peaks.get(name, 0.0)
            if curve and peak > 0.0 and 0 <= frame < len(curve):
                best = max(best, curve[frame] / peak)
        return best

    # viseme -> the morphs the plan feeds it, then shape id -> those same morphs.
    semantics = face_retarget.build_semantics(dsc.targets())
    viseme_morphs = {}
    for name, row in plan.items():
        viseme_morphs.setdefault(semantics.get(row[1]), []).append(name)
    shape_morphs = {}
    for shape, viseme in semantics.items():
        if viseme in viseme_morphs:
            shape_morphs[shape] = viseme_morphs[viseme]
    # A viseme none of whose morphs ever *moves* is the rest state - the closing morph of a dance
    # that never animates it is a flat zero, and `MIK_KUCHI_RESET` is what the solver reaches for
    # when nothing is raised.  Such a cue is not a blend amount at all, so it keeps the value its
    # own layer wrote (`MOUTH_REST_WEIGHT`) instead of being scaled down to nothing.
    moving = {v for v, names in viseme_morphs.items()
              if any(max(grid[n]) - min(grid[n]) > 0.0 for n in names if n in grid)}
    expr_morphs = {}
    for rule in expression_rules(rules_path)["rules"]:
        expr_morphs[int(rule["idx"])] = _rule_morphs(rule)

    def curve_of(name, params):
        """The morphs this cue's strength comes from, or **None** when the dance has no curve for it.

        `None` and `[]` are different answers and the difference is the point: `[]` means "a curve
        exists for this cue and it says nothing is raised" (a rule whose morphs are all at zero),
        while `None` means "no curve exists at all", where writing anything would be inventing the
        number rather than reading it.  A mouth shape whose viseme has no *moving* morph is the rest
        state, which is the second kind: it keeps `MOUTH_REST_WEIGHT`.
        """
        if name == "MOUTH_ANIM":
            if semantics.get(params[2]) not in moving:
                return None
            return shape_morphs.get(params[2], [])
        if int(params[1]) in BLINK_EXPRESSION_IDS:
            # The eyelid pair is not a face read off the dance's curve: `(chara, 22, 100, 1000)` and
            # `(chara, 21, 100, 0)` are the records a **game-verified** script carries, and their 100
            # is attested in game rather than derived.  Scaling them would replace a measurement with
            # an inference - and `まばたき` sits at 1.0 exactly when 22 fires, so the rule-derived
            # mapping would have doubled it to 200.
            return None
        return expr_morphs.get(int(params[1]))

    def slot_of(name):
        return 3 if name == "MOUTH_ANIM" else 2

    def scale_of(name):
        """The two commands read the same level, on the two bands the engine measures for them."""
        return _strength_percent if name == "MOUTH_ANIM" else _intensity_percent

    out = []
    for frame, name, params in events:
        p = list(params)
        if name in ("MOUTH_ANIM", "EXPRESSION"):
            names = curve_of(name, params)
            if names is not None:
                p[slot_of(name)] = scale_of(name)(value_at(names, frame))
        out.append((frame, name, p))

    min_gap_frames = int(round(step_ms / 1000.0 * DST_FPS)) if step_ms else 0
    added = 0
    # The two commands are **independent lanes**, and each has to be tracked across its own gaps.
    # Walking the merged stream instead - taking the parameter of whichever cue came last, whichever
    # command it belonged to - lets an `EXPRESSION` cue that lands inside a mouth gap truncate the
    # mouth's tracking: the walk then re-states the *face* for the rest of the gap and the mouth
    # holds one weight until the next mouth cue.  Measured on the reference dance, that cost 11.6% of
    # the song's frames their correct mouth weight even with no thinning at all.
    for lane in ("MOUTH_ANIM", "EXPRESSION"):
        lane_cues = [e for e in out if e[1] == lane]
        for (prev, _n, pparams), (nxt, _n2, _n3) in zip(lane_cues, lane_cues[1:]):
            if nxt - prev <= 1:
                continue
            names = curve_of(lane, pparams)
            if names is None:
                continue                   # no curve: nothing to track, and nothing to invent
            last = pparams[slot_of(lane)]
            last_frame = prev
            for frame in range(prev + 1, nxt):
                if min_gap_frames and frame - last_frame < min_gap_frames:
                    continue
                value = scale_of(lane)(value_at(names, frame))
                if value == last:
                    continue
                q = list(pparams)
                q[slot_of(lane)] = value
                out.append((frame, lane, q))
                last = value
                last_frame = frame
                added += 1
    return sorted(out, key=lambda e: e[0]), added


def events_from_tracks(tracks, plan, frames, rest, chara=0, weight=MOUTH_WEIGHT, hold=MOUTH_HOLD,
                       min_gap_ms=MOUTH_MIN_GAP_MS):
    """Rising and falling edges of every planned morph, as 60 fps ``(frame, name, params)``.

    The frame map is **fixed at x2**: a VMD is 30 fps and the output grid is 60 fps, so source
    frame `f` lands on output frame `2f`, whatever the base script's length is.  Deriving the
    scale from the base length instead was the cause of a measured +4.9 s drift by the end of a
    dance whose borrowed base script was 52 s shorter than the motion - every cue slowly fell
    behind the body animation.  `frames` now only bounds the output: the splice extends the
    script's timeline past its own last TIME to hold cues that land beyond it.

    Edges are collected per morph, so a re-trigger before release only replaces that morph's own
    tail rather than cutting into another one's events.  `min_gap_ms` thins a morph's own cues that
    land closer together than that; 0 keeps every edge, which is the default because the measured
    densities say the stream does not need thinning.

    `hold` is written on every cue and is how long that cue stands; keeping a shape alive longer than
    it is `repeat_held_shapes`' job, not this one's.

    MOUTH_ANIM only.  Every morph in `plan` is a mouth - `plan_from_match` drops expression rows -
    so the loop below has no expression branch to take.
    """
    if frames <= 0:
        raise FaceError("the timeline is %d frames long - nothing to schedule" % frames)
    last = max((k["frame"] for keys in tracks.values() for k in keys), default=0)
    if last <= 0:
        return [], [], {"thinned": 0, "density": 0.0}      # no animated morph at all:
        # events_from_tracks is not the layer that reports it, export_face refuses an empty list
    # The VMD is 30 fps and the output grid is 60 fps, so the frame map is x2 - fixed.
    # It must NOT be derived from the base script's length: a borrowed base whose song is
    # shorter or longer than the dance would stretch or squeeze every cue (measured: a
    # 141 s base under a 193 s dance drifted +4.9 s by the end).  The base length only
    # bounds the output below; cues past it still land, the splice extends the timeline.
    scale = DST_FPS / 30.0
    events, skipped = [], []
    thinned = 0
    min_gap_frames = int(round(min_gap_ms / 1000.0 * DST_FPS)) if min_gap_ms else 0
    for name, keys in tracks.items():
        if name not in plan:
            if keys and max(k["weight"] for k in keys) >= MOUTH_FLOOR:
                skipped.append(name)          # animated but unresolved: it belongs on the worklist
            continue
        _kind, param, _game_name, _how = plan[name]
        keys = sorted(keys, key=lambda k: k["frame"])
        frame60 = lambda k: min(frames - 1, int(round(k["frame"] * scale)))     # noqa: E731
        peak = max(k["weight"] for k in keys)
        rise = max(MOUTH_FLOOR, MOUTH_REL * peak)
        fall = rise * MOUTH_FALL_RATIO
        own = []
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
        events += own
    events.sort(key=lambda e: e[0])
    span_min = (max((e[0] for e in events), default=0)) / DST_FPS / 60.0
    return ([(f, n, p) for f, n, p in events], sorted(skipped),
            {"thinned": thinned, "density": (len(events) / span_min) if span_min else 0.0})


def blink_events(tracks, chara, frames, weight=EXPR_INTENSITY):
    """The dance's ``まばたき`` curve as ``EXPRESSION`` cues on the 60 fps grid.

    Used when the panel's *automatic blink* switch is **off**: DIVA's own blink is left unwritten
    (no ``AUTO_BLINK`` in the bootstrap) and the eyelid follows the MMD morph instead.

    The rule is the one the user specified and the one the in-game-verified PV 8331 files carry:

      * the morph sits at **full closure** (weight 1.0) -> ``EXPRESSION(chara, 22, 100, 1000)``;
      * the frame it leaves 1.0 for anything else -> ``EXPRESSION(chara, 21, 100, 0)``.

    Only the edges are written, because ``EXPRESSION`` is a held state: a run of frames all at 1.0
    is one cue, not one per frame.  Frames where the morph is partly closed (0.4, 0.8) are
    deliberately *not* cues - see ``BLINK_SHUT_LEVEL``.  A curve that ends while still shut is
    opened on the last frame, so a dance can never leave the character with its eyes closed.

    MMD morph interpolation is linear and a VMD carries no interpolation parameters, so the curve
    is the source function itself, sampled by ``_sample_grid``.
    """
    if frames <= 0:
        return []
    grids = [keys for name, keys in tracks.items()
             if name.strip() in BLINK_MORPHS or name.strip().lower() in BLINK_MORPHS]
    if not grids:
        return []
    curves = [_sample_grid(keys, frames) for keys in grids]
    shut = [max(values[f] for values in curves) >= BLINK_SHUT_LEVEL - BLINK_EPS
            for f in range(frames)]
    out, is_shut = [], False
    for f in range(frames):
        if shut[f] == is_shut:
            continue
        is_shut = shut[f]
        if is_shut:
            out.append([f, "EXPRESSION", [chara, EXPR_LID_SHUT, weight, EXPR_BLINK_HOLD]])
        else:
            out.append([f, "EXPRESSION", [chara, EXPR_LID_OPEN, weight, EXPR_BLINK_CLEAR]])
    if is_shut:                                  # never end a dance with the eyes shut
        out.append([frames - 1, "EXPRESSION",
                    [chara, EXPR_LID_OPEN, weight, EXPR_BLINK_CLEAR]])
    out.sort(key=lambda e: e[0])
    return [(f, n, p) for f, n, p in out]


def _sample_grid(keys, frames):
    """One morph track's weight at every 60 fps frame, in a single walk over its keys.

    Straight linear interpolation between keys (MMD's own morph interpolation); the two-pointer
    walk keeps a 12 000-frame dance linear instead of quadratic in the key count.
    """
    out = [0.0] * frames
    n = len(keys)
    if n == 0:
        return out
    if n == 1:
        return [keys[0]["weight"]] * frames
    first, last = keys[0], keys[-1]
    k = 0
    for f in range(frames):
        t = f / 2.0
        if t <= first["frame"]:
            out[f] = first["weight"]
            continue
        if t >= last["frame"]:
            out[f] = last["weight"]
            continue
        while k + 1 < n and keys[k + 1]["frame"] <= t:
            k += 1
        lo, hi = keys[k], keys[k + 1]
        span = hi["frame"] - lo["frame"]
        if span <= 0:
            out[f] = lo["weight"]
        else:
            u = (t - lo["frame"]) / span
            out[f] = lo["weight"] + (hi["weight"] - lo["weight"]) * u
    return out


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


def splice(base_recs, event_list, frames, drop_existing=False, drop_names=FACE_COMMANDS):
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
        head = [(n, p) for n, p in head if n not in drop_names]
        blocks = [(t, [(n, p) for n, p in rs if n not in drop_names]) for t, rs in blocks]
    end_tick = None
    for tick, records in blocks:
        if any(n == "PV_END" for n, _p in records):
            end_tick = tick
            break
    last_tick = blocks[-1][0] if blocks else 0
    if last_tick <= 0:
        raise FaceError("the base script has no TIME block - cannot place events on it")
    ticks = collections.OrderedDict((t, []) for t, _rs in blocks)
    for tick, records in blocks:
        for position, (name, params) in enumerate(records):
            ticks[tick].append(((0, position), name, params))
    # An event frame is already on the 60 fps grid, so its tick is frame/60 s: a fixed
    # conversion, not a ratio against the base script's length.  Scaling by the base length
    # here was the second half of the drift bug - the same one events_from_tracks had - and
    # it rescaled every cue onto the borrowed base's own timeline.
    # PV_END is NOT a drop boundary any more: a dance longer than its borrowed base has cues
    # past the base's end marker, and the marker is relocated below to follow the last cue.
    # Nothing is dropped for being late.
    for frame, name, params in event_list:
        tick = int(round(frame / DST_FPS * TICKS_PER_SEC))
        # A face cue with no block of its own opens one rather than joining the nearest existing
        # TIME: two cues 2 frames apart would otherwise land on one tick.
        ticks.setdefault(tick, []).append(((1, frame), name, params))
    # A dance longer than its borrowed base pushes cues past the base's own PV_END.  The end
    # marker has to follow the last cue - the song is not over at the base's length just because
    # the base was borrowed from a shorter PV - so before anything is emitted, the original
    # PV_END is removed wherever it sits and a replacement is scheduled just past the last cue.
    # This must happen before the blocks are written, or the old marker stays in the stream and
    # the validator counts every later cue as after-the-end.
    if end_tick is not None:
        for tick in list(ticks):
            ticks[tick] = [r for r in ticks[tick] if r[1] != "PV_END"]
            if not ticks[tick]:
                del ticks[tick]
        tail = [(n, p) for n, p in tail if n != "PV_END"]
        used = [t for t in sorted(ticks) if ticks[t]]
        if used:
            tail = [("TIME", [used[-1] + TICKS_PER_SEC // 60]), ("PV_END", [])] + tail
    body = []
    for name, params in head:
        body += encode(name, params)
    for tick in used:
        body += encode("TIME", [tick])
        for _order, name, params in sorted(ticks[tick], key=lambda r: r[0]):
            body += encode(name, params)
    for name, params in tail:
        body += encode(name, params)
    return body


def script_bytes(magic, version, body):
    return struct.pack("<III", magic, version, 0) + struct.pack("<%dI" % len(body), *body)



def _tail_moved_to(kept, now):
    """Where a relocated PV_END makes the two tails diverge, if that is the only difference.

    Returns the index the tails re-converge at (so the caller may compare up to it), or None
    when the difference is not a PV_END relocation.
    """
    pv = [i for i, (n, _p) in enumerate(now) if n == "PV_END"]
    if not pv:
        return None
    cut = pv[0]
    # everything after the moved PV_END in `now` must be exactly the tail of `kept` starting
    # one TIME later (the base's own TIME+PV_END pair)
    tail_now = now[cut + 1:]
    tail_kept = kept[len(kept) - len(tail_now):] if len(tail_now) <= len(kept) else []
    if tail_now and tail_now == tail_kept and kept[:cut] == now[:cut]:
        return cut
    return None


def check_output(base_recs, out_recs, added, replaced, face_names=FACE_COMMANDS):
    """Prove the spliced script kept what it should and only changed the face stream.

    ``replaced`` is the base's own face count that was dropped on the way in, so the expectation is
    ``base - replaced + added`` per command rather than a bare difference.  ``face_names`` is the set
    of commands the splice is allowed to rewrite: ``MOUTH_ANIM`` and ``EXPRESSION``, which is also
    what the blink is written as.  Everything else - notes, stage cues, lyrics, a base's own
    ``LOOK_ANIM`` eye lines - has to come through record for record.
    """
    kept = [(n, tuple(p)) for n, p in base_recs if n not in face_names and n != "TIME"]
    now = [(n, tuple(p)) for n, p in out_recs if n not in face_names and n != "TIME"]
    if kept != now:
        # A dance longer than its base pushes cues past the base's PV_END, and the splice moves
        # that end marker to follow the last cue.  That relocation - one TIME and one PV_END
        # swapping position near the tail - is the only permitted difference; everything before
        # it must still match record for record.
        moved = _tail_moved_to(kept, now)
        if moved is not None:
            kept, now = kept[:moved], now[:moved]
    if kept != now:
        first = next((i for i in range(min(len(kept), len(now))) if kept[i] != now[i]), 0)
        raise FaceError("the base script's records changed at %d: %s became %s"
                        % (first, kept[first] if first < len(kept) else None,
                           now[first] if first < len(now) else None))
    # Every instant the base had that still carries a record has to still be there.  An instant the
    # replacement emptied loses its TIME on purpose (that is what removes the bare TIMEs); an
    # instant that held a note, a stage cue or PV_END may not move or vanish.
    must_keep = {tick for tick, records in split(base_recs)[1]
                 if any(n not in face_names and n != "PV_END" for n, _p in records)}
    out_times = {p[0] for n, p in out_recs if n == "TIME"}
    if not must_keep <= out_times:
        raise FaceError("the base script lost %d of its own TIME blocks"
                        % len(must_keep - out_times))
    # TIME may grow, since a cue with no block of its own opens one, and may shrink by the marks the
    # replacement emptied; the two checks above are what prove the notes, stage cues and their order
    # came through untouched.
    retained = collections.Counter(n for n, _p in base_recs if n in face_names) - replaced
    expect = collections.Counter(n for _f, n, _p in added)
    expect.update(retained)
    face = collections.Counter(n for n, _p in out_recs if n in face_names)
    if dict(+face) != dict(+expect):
        raise FaceError("the script carries %s, expected %s" % (dict(+face), dict(+expect)))


def _generate_base_script(vmd_path, side_path, chara=0):
    """A PV script skeleton for the mouth stream to land in, written beside the output.

    Every command and value below is copied from **`pv_032_extreme.dsc`** - the base the export
    that demonstrably worked in game was spliced into (eyes moved, mouth matched, fingers intact).
    Its whole TIME-0 bootstrap is:

        MUSIC_PLAY() / BAR_TIME_SET(240,3) / FACE_TYPE(1) / CHANGE_FIELD(1)
        DATA_CAMERA_START(0,1) / DATA_CAMERA(0,1)
        MIKU_DISP(0,1) / SET_PLAYDATA(0,1) / SET_MOTION(0,1,-1,1000)
        HAND_SCALE(0,0,1220) / HAND_SCALE(0,1,1220) / EYE_ANIM(0,2,0)
        HAND_ANIM(0,0,9,-1,-1) / HAND_ANIM(0,1,9,-1,-1) / AUTO_BLINK(0,1)
        TIME 499 / TARGET(2,156000,204000,10000,303000,500,1)

    `AUTO_BLINK` is **not** written: it is the engine's procedural blink, and the eyelid is already
    driven by the dance's own まばたき curve.  Writing `(chara, 0)` instead is not an option either -
    the corpus has no bare `AUTO_BLINK(chara, 0)` in a bootstrap, and a disable needs a companion
    `EYE_ANIM`.  Nothing here writes `LOOK_ANIM` either.

    `DATA_CAMERA_START` is one deliberate addition the stock base does not carry: without it the
    engine ignores `DATA_CAMERA` and the PV sits on a fixed default camera (verified twice on the
    reference mod - once with a working camera farc that only animated once the line was spliced in,
    once when a regenerated mod shipped without it and the camera died again).

    Two earlier versions of this function got this wrong in different ways, and both defects were
    visible in game:

    * the first invented a short bootstrap from plausible-looking zeros and omitted CHANGE_FIELD,
      DATA_CAMERA, LYRIC, HAND_ANIM and TARGET - each present in 99-100% of the 349 scripts that
      decode - so the engine was handed a script with no stage and no camera;
    * the second copied `pv_092_easy.dsc` instead, whose `HAND_ANIM` third parameter is **1** where
      pv_032's is **9**.  That parameter selects a DIVA hand animation and it **overrides the
      fingers the mot set carries**, so the dance's finger animation was replaced by a different
      hand pose.  The value here is the one the working base uses.

    `MIKU_DISP(chara, 1)` is the one deliberate change from pv_032's own `(0, 1)` only in that it
    follows the requested character slot.  `LOOK_ANIM` is deliberately **absent**: its 12/13
    channels are the encoding confirmed in game to leave the eyes turned, and the eye motion rides
    the pv_expression file instead.

    What is still not here, because a face exporter cannot invent it: the per-frame `TARGET`
    choreography, camera switches, stage changes and lyrics of a real PV.  This scaffold gives the
    engine a valid, complete *setup*; the motion comes from the mot set, the eyes from the exp bin.
    """
    frames = int(round(vmd.last_frame(vmd_path) / 30.0 * DST_FPS)) + 2
    last_tick = int(round((frames - 2) / DST_FPS * TICKS_PER_SEC))
    body = []
    body += encode("TIME", [0])
    bootstrap = [
        ("LYRIC", [0, -1]),
        ("MUSIC_PLAY", []),
        ("BAR_TIME_SET", [240, 3]),
        ("CHANGE_FIELD", [1]),
        ("DATA_CAMERA_START", [chara, 1]),
        ("DATA_CAMERA", [0, 1]),
        ("MIKU_DISP", [chara, 1]),
        ("SET_PLAYDATA", [chara, 1]),
        ("SET_MOTION", [chara, 1, -1, 1000]),
        ("HAND_SCALE", [chara, 0, 1220]),
        ("HAND_SCALE", [chara, 1, 1220]),
        ("HAND_ANIM", [chara, 0, 9, -1, -1]),
        ("HAND_ANIM", [chara, 1, 9, -1, -1]),
    ]
    for name, params in bootstrap:
        body += encode(name, params)
    body += encode("TIME", [499])
    body += encode("TARGET", [2, 156000, 204000, 10000, 303000, 500, 1])
    body += encode("TIME", [last_tick])
    body += encode("PV_END", [])
    body += encode("END", [])
    path = side_path + ".base.dsc"
    tmp = path + ".%d.tmp" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(script_bytes(0x14050921, 65, body))
    problems = dsc.validate(tmp, magic=0x14050921, version=65)
    if problems:
        os.unlink(tmp)
        raise FaceError("the generated base script breaks the shipping rules: %s" % problems[:3])
    os.replace(tmp, path)
    print("Face export: generated a base script at %s (removed once the export publishes)"
          % os.path.basename(path))
    return path


def face_report(stats):
    """The one-line summary `ui` prints after a face export.

    It lives here, next to the stats it formats, for a plain reason: `ui` cannot be imported without
    Blender, so a mistake in a format string there reaches the user as a `NameError` *after* a
    successful export.  Two did.  Here the line is built by a plain function that any check can call
    with a real export's stats.
    """
    blink = ("from the dance's まばたき (%d cue(s), %d eye-close)"
             % (stats.get("blink_cues", 0), stats.get("blink_events", 0)))
    hold = "%d ms on every cue" % stats.get("hold", MOUTH_HOLD)
    if stats.get("repeats"):
        hold += ", %d re-stated to cover a longer hold" % stats["repeats"]
    if stats.get("strengthened"):
        hold += (", strength taken from the dance's curve (%d cue(s) split to track it, every %d ms)"
                 % (stats["strengthened"],
                    stats.get("strength_step_ms", STRENGTH_STEP_MS)))
    if stats.get("static_morphs"):
        names = stats["static_morphs"]
        hold += (", %d morph(s) flat for the whole dance left out of the mouth (%s)"
                 % (len(names), ", ".join(names[:4]) + ("..." if len(names) > 4 else "")))
    eyes = stats.get("eye_motion")
    return ("Face export: %s -> %d records, %d mouth cue(s) and %d expression cue(s) at %.0f/min, "
            "blink %s, %d bare TIME, %d cue(s) dropped past PV_END, %d thinned, hold %s, "
            "eyes %s, validated against every shipping-script rule"
            % (os.path.basename(stats.get("out", "")), stats.get("records", 0),
               stats.get("mouth", 0), stats.get("expression", 0),
               stats.get("cues_per_minute", 0.0), blink, stats.get("bare_time", 0),
               stats.get("dropped_after_pv_end", 0), stats.get("thinned", 0), hold,
               eyes or "not written"))


def export_face(base_dsc, vmd_path, out_dsc, plan, game, frames=None, chara=0,
                weight=MOUTH_WEIGHT, hold=MOUTH_HOLD, overwrite=False, replace=True,
                min_gap_ms=MOUTH_MIN_GAP_MS, check_pv_db=True, solver=True, config=None,
                explain=None, cancel=None, expressions=True,
                rules_path=None, strength_step_ms=STRENGTH_STEP_MS, **obsolete):
    """Splice the vmd's mouth stream into `base_dsc` and publish it as `out_dsc`.

    Reads the base with the verified widths, writes to a temp name, re-reads that, runs the full
    ``dsc_core.validate`` rule set plus the record-by-record comparison, and only then moves it onto
    the target.  ``replace`` drops the base's own MOUTH_ANIM / EXPRESSION records first, because a
    stock script carries its own song's lip sync.  ``frames`` is the 60 fps length of the dance;
    left None it comes from the base script's last TIME, which is only honest when the motion and
    that script were built for the same song.

    ``solver`` picks the decision layer and defaults to **on**: `face_retarget` solves the mouth as
    one performance against priors measured from the shipping scripts, which is what makes it move
    on a real dance.  Off, it is one rising/falling edge per morph - a literal transcription of the
    .vmd's own thresholds, which on the reported material barely moves.  Either way everything after
    the event list is the same code.  ``explain`` optionally receives one record
    per decided event.

    The eyelid always follows the dance's own ``まばたき``; the engine's procedural blink is never
    used.  Every rule in ``data/expression_rules.json`` is applied - that table *is* the expression
    transplant and there is no switch for it.  ``rules_path`` overrides the file.
    """
    if obsolete:
        # A half-updated Blender session, named as such.  Blender imports an add-on's modules once
        # and never re-imports them when the files on disk change, so after an in-place update an
        # old caller can still be calling into a new function - which Python reports as a bare
        # `unexpected keyword argument`.  That reads like a defect in the new code and sends you
        # looking in the wrong place, so say what it actually is.
        raise FaceError(
            "this export was started by an older copy of the add-on than the one answering it: "
            "export_face() was called with %s, which no longer exists (3.3.1 had `blink=` and "
            "`release=`; 3.4.0 split them into `auto_blink=`, and 3.6.0 removed that too - the eyelid "
            "always follows the dance now).  Blender loads an add-on's modules "
            "once per session and does not reload them when the files change, so simply installing "
            "the update is not enough inside a session that was already running.\n"
            "  Restart Blender, or - if you cannot - open the Python Console and run:\n"
            "    import sys; [sys.modules.pop(k) for k in list(sys.modules) "
            "if k.startswith('diva_pv_tools')]\n"
            "  then disable and re-enable the add-on.\n"
            "  Build answering this call: %s" % (", ".join(sorted(obsolete)), _build_banner()))
    if not os.path.isfile(vmd_path):
        raise FaceError("motion file not found: %s" % vmd_path)
    if not base_dsc:
        # No base script: generate the scaffold instead of refusing.  A borrowed base brings its
        # own song's camera, stage and lyric timeline, which is exactly what a custom PV does not
        # want; the generated script carries the bootstrap a shipping script opens with and puts
        # PV_END after the last cue, so the mouth stream stands on the VMD's own clock with
        # nothing borrowed to contradict it.
        base_dsc = _generate_base_script(vmd_path, out_dsc, chara)
        borrowed = False
    else:
        borrowed = True
    if not os.path.isfile(base_dsc):
        raise FaceError("base file not found: %s" % base_dsc)
    if os.path.exists(out_dsc) and not overwrite:
        raise FaceError("%s already exists - pass overwrite to replace it" % out_dsc)
    vals, base_recs = dsc.decode_fixed(base_dsc)
    last_time = max((p[0] for n, p in base_recs if n == "TIME"), default=0)
    if frames is None:
        frames = int(round(last_time / float(TICKS_PER_SEC) * DST_FPS)) + 1
        # The base script's length bounds the *script*, not the *motion*.  A dance longer than
        # its borrowed base must still fit: extend the frame budget to the VMD's own span so no
        # cue is clamped onto the last frame and dropped.  The splice extends the timeline past
        # the base's last TIME to hold them, and PV_END follows the last cue.
        vmd_last = vmd.last_frame(vmd_path)
        if vmd_last > 0:
            need = int(round(vmd_last / 30.0 * DST_FPS)) + 2
            if need > frames:
                frames = need
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
    format_note = None
    if db.get("difficulty.extreme.0.script_format"):
        try:
            want = int(db["difficulty.extreme.0.script_format"], 16)
        except ValueError:
            want = None
        if want is not None and want != vals[0]:
            # MEGA39's PLUS ships official DLC whose script magic is newer than the
            # script_format the pv_db beside it still declares (measured on the user's
            # own pv_2580_extreme: script 0x15122517 vs database 0x14050921) and the
            # game loads those pairs happily - the loader reads the script's own magic.
            # A mismatch is therefore a note to report, not a reason to refuse; the
            # export keeps the base's magic either way, so nothing can drift.
            format_note = ("the base script is %#x while the PV database next to it declares "
                           "%#x - the export keeps the script's own magic" % (vals[0], want))

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
    # The eyelid, from the dance's own まばたき curve.  This is the only blink driver the exporter
    # has: the engine's procedural one (AUTO_BLINK) is never written, because the two are mutually
    # exclusive drivers of one eyelid and following the .vmd is the point.
    blink_cues = blink_events(track, chara, frames)
    if blink_cues:
        event_list = sorted(list(event_list) + blink_cues, key=lambda e: e[0])

    # The expression transplant.  Always on: the rule table in `data/expression_rules.json` *is* the
    # transplant, and it is the user's own in-game readings of what each DIVA face is made of.
    table = expression_rules(rules_path)
    cues, expression_info = expression_events(track, table, frames, chara=chara)
    # Every id the table may write has to be declared to the validator, for the same reason the blink
    # ids are: they are attested by *in-game observation* - the rules were read off the character -
    # and the corpus measurement in `diva_face_targets.json` is a different thing that must not be
    # padded.  Without this, one rule whose id the corpus never happened to send (idx 16,
    # `MIK_FACE_UTURO`, needs `なごみ`) fails the whole export at publish time.
    allowed_expression_ids = set(BLINK_EXPRESSION_IDS) | {r["idx"] for r in table["rules"]}
    if cues:
        event_list = sorted(list(event_list) + cues, key=lambda e: e[0])

    # A shape is on the character for `hold` ms and then the engine releases it, so a shape the dance
    # holds for longer than that has to be re-stated inside it.  This runs last so it sees the whole
    # merged stream, and it only looks at MOUTH_ANIM.
    #
    event_list, repeats = repeat_held_shapes(event_list, hold)

    # The dance's own strength, last, so it sees the final stream including the re-statements.
    event_list, strengthened = apply_strength(event_list, track, plan, frames, rules_path,
                                              step_ms=strength_step_ms)

    # The pv_expression switch, at tick 0, where all 527 corpus records sit.  An ordinary event and
    # not a bootstrap line: `replace` drops the base's own EXPRESSION records - including one in a
    # scaffold - so a bootstrap line would be deleted again on the way out.
    event_list.append((0, "EXPRESSION",
                       [int(chara), EXPR_PV_EXPRESSION_SWITCH, -1, -1]))
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
        raise FaceError(
            "nothing to export: %d morph name(s) matched a game slot and none of them is "
            "raised above %.2f in this vmd, so every cue would be a rest.%s The resolved "
            "names are: %s"
            % (len(plan), MOUTH_RISE,
               # the tell-tale of a motion-only .vmd: exporters drop one weight-0
               # placeholder key per morph, so every "matched" name exists and nothing
               # moves - and the dance's face lives in a separate FACIAL .vmd
               "  Every matched key in this file sits at weight 0.0 - that is the shape of a "
               "motion .vmd whose morphs are zero-weight placeholders: dances usually ship "
               "their face in a separate .vmd (a 'FACIAL' file next to the motion one); "
               "point the source at that instead. "
               if all(abs(k["weight"]) < 1e-6 for n in plan for k in track.get(n, [])) else "",
               ", ".join(sorted(plan)[:6])))
    dropped = (collections.Counter(n for n, _p in base_recs if n in FACE_COMMANDS)
               if replace else collections.Counter())
    body = splice(base_recs, event_list, frames, replace)
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
        problems = dsc.validate(tmp, magic=vals[0], version=vals[1], chara_limit=chara_limit,
                                extra_expression_ids=frozenset(allowed_expression_ids))
        if problems:
            raise FaceError("refusing to publish: the written script breaks %d rule(s) that every "
                            "shipping script obeys:\n  - %s"
                            % (len(problems), "\n  - ".join(problems[:8])))
        if cancel is not None and cancel():
            raise FaceError("the export was cancelled before publishing: %s was left "
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
    # One file out: the generated scaffold was only the splice's input - it exists beside the
    # output between generation and publish, and a mod that keeps it ships two scripts the game
    # will never read.  A borrowed base is the user's own file and is left alone.
    if not borrowed and os.path.isfile(base_dsc):
        os.remove(base_dsc)
    # The final stream's own rate, not the solver's: the repeat pass adds records, so
    # reporting the pre-insertion number would understate what the file carries.
    mouth_frames = [f for f, n, _p in event_list if n == "MOUTH_ANIM"]
    mouth_span_min = (max(mouth_frames, default=0)) / DST_FPS / 60.0
    stats = {
        "base": os.path.abspath(base_dsc), "vmd": os.path.abspath(vmd_path),
            "out": os.path.abspath(out_dsc), "bytes": os.path.getsize(out_dsc),
            "records": len(out_recs), "base_records": len(base_recs), "frames": int(frames),
            "last_time": int(last_time), "mouth": counts.get("MOUTH_ANIM", 0),
            "expression": counts.get("EXPRESSION", 0), "planned": dict(planned),
            "unmapped_animated": unmapped, "replaced_base_face": dict(+dropped),
            "chara": int(chara), "plan_tiers": dict(how), "time_blocks": len(times),
            "bare_time": bare, "targets": dict(targets),
            "dropped_after_pv_end": 0, "thinned": thin.get("thinned", 0),
            "expression_info": expression_info,
            "blink_events": sum(1 for _f, n, p in event_list
                                if n == "EXPRESSION" and p[1] == EXPR_LID_SHUT),
            "blink_cues": len(blink_cues),
            "cues_per_minute": (round(len(mouth_frames) / mouth_span_min, 1)
                                if mouth_span_min else 0.0), "min_gap_ms": min_gap_ms,
            "hold": hold, "repeats": repeats, "strengthened": strengthened,
            "strength_step_ms": strength_step_ms,
            # The morphs the solver refused to treat as articulation because their curve never
            # moves (see face_retarget.canonical_vectors).  Reported, not dropped in silence.
            "static_morphs": list(thin.get("static_morphs") or []),
            "weight": weight, "pv_db": db.get("_path", ""),
            "script_format_note": format_note,
            "pv_db_performers": chara_limit, "validated": True}
    return stats
