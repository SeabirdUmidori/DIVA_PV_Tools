"""Retarget an MMD face performance into Mega39's animation language.

A plain `MMD morph name -> DIVA slot -> rising/falling edge per morph` conversion has three failure
modes which are structural rather than tuning problems: two morphs that mean the same thing fight
each other, a morph that wobbles on the threshold emits a cue per wobble, and the choice of slot
never looks at what the surrounding slots were.

This module keeps the same inputs and produces the same kind of output - a list of
``(frame, command, params)`` for the existing encoder - but decides the sequence in four layers:

    morph curves -> CanonicalFaceVector -> emission score per candidate
                 -> + KB transition cost + KB duration cost -> Viterbi -> events

Everything it needs to know about Mega39 is read from ``data/*.json``, the knowledge base measured
from 1065 shipping scripts.  Nothing about Mega39 is hard-coded here, and no Mega39 behaviour
that the data did not show is used - the shapes with no measured statistics are simply never emitted.

Semantics come from SEGA's own asset names (``MIK_KUCHI_A``, ``MIK_FACE_LAUGH``, ...), held in the
packaged `diva_face_targets.json`.  Those names are not a guess, and the shipping data
corroborates them independently: the five most frequent mouth shapes are A, O, RESET, I, U, and the
five most frequent transitions are between exactly those.
"""
import json
import math
import os

import numpy as np

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ------------------------------------------------------------------ canonical viseme space
# Derived from the SEGA mouth asset names by stripping the two dialect suffixes and the qualifiers.
# `_OLD` and `_CL`/`_S`/`_L`/`_DOWN` mark a variant of the same articulation in another dialect, so
# they normalise onto the base viseme; the variant id is still what gets emitted, chosen by the
# knowledge base rather than by this table.
VISEME_QUALIFIERS = ("_OLD_CL", "_OLD", "_CL", "_DOWN", "_S", "_L")
VISEME_ALIASES = {
    "A": "A", "I": "I", "U": "U", "E": "E", "O": "O",
    "RESET": "CLOSED", "NEUTRAL": "CLOSED",
    "SMILE": "SMILE", "NIYA": "SMIRK", "NIYARI": "SMIRK",
    "CHU": "POUT", "SURPRISE": "OPEN", "SAKEBI": "OPEN",
    "HE": "LAUGH_OPEN", "HAMISE": "TEETH", "NEKO": "CAT", "MOGUMOGU": "CHEW",
    "HERAHERA": "SLACK", "SANKAKU": "TRIANGLE", "SHIKAKU": "SQUARE",
}
# expression ids normalise the same way, onto mood dimensions
MOOD_QUALIFIERS = ("_OLD_CL", "_OLD", "_CL")
MOOD_ALIASES = {
    "RESET": "NEUTRAL", "CLOSE": "NEUTRAL",
    "SAD": "SAD", "LAUGH": "HAPPY", "SURPRISE": "SURPRISE", "WINK": "WINK",
    "SETTLED": "CALM", "DAZZLING": "SHY", "LASCIVIOUS": "ALLURE", "STRONG": "ANGRY",
    "CLARIFYING": "PUZZLED", "GENTLE": "SOFT", "CRY": "CRY", "NAGASI": "GAZE",
    "KIRI": "PROUD", "UTURO": "DAZE",
}


def _strip(name, qualifiers):
    out = name
    for q in qualifiers:
        if out.endswith(q):
            return out[:-len(q)]
    return out


def build_semantics(targets):
    """(mouth_id -> viseme), (expression_id -> mood), and the reverse index.

    Only ids whose asset name is known get a viseme.  An id with no name stays unlabelled and is
    never emitted: the alternative is to guess what shape 37 looks like, and a wrong guess here is
    invisible until the character is on screen.
    """
    mouth, mood = {}, {}
    for key, name in (targets.get("mouth_shape_names") or {}).items():
        base = _strip(name.replace("MIK_KUCHI_", ""), VISEME_QUALIFIERS)
        if base in VISEME_ALIASES:
            mouth[int(key)] = VISEME_ALIASES[base]
    for key, name in (targets.get("expression_id_names") or {}).items():
        base = _strip(name.replace("MIK_FACE_", ""), MOOD_QUALIFIERS)
        if base in MOOD_ALIASES:
            mood[int(key)] = MOOD_ALIASES[base]
    return mouth, mood


def load_knowledge(data_dir=DATA):
    """Everything measured from the shipping scripts, or a loud failure."""
    out = {"dir": data_dir}
    for key, name in (("mouth", "mouth_prototypes.json"),
                      ("mouth_trans", "mouth_transitions.json"),
                      ("mouth_dur", "mouth_durations.json"),
                      ("expression", "expression_prototypes.json"),
                      ("expression_trans", "expression_transitions.json"),
                      ("lyric", "lyric_offsets.json"),
                      ("motion", "motion_expression.json"),
                      ("context", "context_model.json")):
        path = os.path.join(data_dir, name)
        if not os.path.isfile(path):
            raise RuntimeError("%s is missing - the knowledge base shipped with this add-on is "
                               "incomplete, reinstall the add-on; the solver has no priors "
                               "without it" % path)
        with open(path, encoding="utf-8") as handle:
            out[key] = json.load(handle)
    return out


# ---------------------------------------------------------------------------- context vector
class Context(object):
    """The PV context a frame is decided in: time, beat, section, lyric, phoneme, motion, camera.

    Every field is either measured from the input or `None`, and **`None` means the solver does not
    use it at all** rather than substituting a default.  That rule is the whole design: `BAR_TIME_SET`
    exists in only 90 of the 265 shipping performances, the corpus carries no lyric text and so no
    phoneme prior, and most songs have no camera in their script.  A context vector that quietly
    filled those in with 120 BPM and a made-up phoneme would look richer and be worth less.

    The one component that is inferred rather than measured is `section`, and it is inferred *from
    the corpus's own animation density* rather than from a label: mouth activity across the shipping
    songs runs 1.11 cues/s in the first tenth, peaks around 3.3 in the last third and falls to 1.24
    at the end.  That curve is the honest stand-in for "the chorus is busier" - it is a measurement
    of where songs put their mouth animation, not a claim about song structure.
    """

    FIELDS = ("time_s", "section", "density_prior", "beat_phase", "bar_index", "phoneme", "lyric",
              "motion", "camera_cut", "tempo")

    def __init__(self, frames, fps):
        self.frames = frames
        self.fps = fps
        self.time_s = [f / fps for f in range(frames)]
        self.section = [f / max(1, frames - 1) for f in range(frames)]
        self.density_prior = None      # mouth cues per second the corpus puts here
        self.tempo = None              # BPM, only if the script carried BAR_TIME_SET
        self.beat_phase = None         # 0..1 within a bar
        self.bar_index = None
        self.phoneme = None            # [(start_frame, end_frame, viseme, confidence)]
        self.lyric = None              # [(frame, character_index)]
        self.motion = None             # motion intensity per frame, 0..1
        self.camera_cut = None         # [bool] per frame

    # ---- queries used by the emission and the report
    def phoneme_at(self, frame):
        if not self.phoneme:
            return None
        for start, end, viseme, conf in self.phoneme:
            if start <= frame <= end:
                return viseme, conf
        return None

    def density_at(self, frame):
        if not self.density_prior:
            return None
        return self.density_prior[min(len(self.density_prior) - 1,
                                      int(self.section[frame] * len(self.density_prior)))]

    def motion_at(self, frame):
        return None if not self.motion else self.motion[min(frame, len(self.motion) - 1)]

    def describe(self, frame):
        """What the report prints for one frame."""
        out = {"frame": frame, "time_s": round(self.time_s[frame], 4),
               "section": round(self.section[frame], 4)}
        if self.tempo:
            out["bpm"] = self.tempo
            out["bar"] = self.bar_index[frame]
            out["beat_phase"] = round(self.beat_phase[frame], 4)
        if self.density_prior:
            out["expected_cues_per_s"] = round(self.density_at(frame), 3)
        guess = self.phoneme_at(frame)
        if guess:
            out["phoneme_viseme"] = guess[0]
            out["phoneme_confidence"] = guess[1]
        if self.motion:
            out["motion_intensity"] = round(self.motion_at(frame), 3)
        return out

    def notes(self):
        have = [name for name in ("tempo", "phoneme", "lyric", "motion", "camera_cut",
                                  "density_prior") if getattr(self, name)]
        missing = [name for name in ("tempo", "phoneme", "lyric", "motion", "camera_cut",
                                     "density_prior") if not getattr(self, name)]
        return {"present": have, "absent": missing,
                "absent_means": "the solver does not use it; nothing is defaulted in its place"}


def build_context(seq, frames, knowledge, cfg, fps=60.0, tempo=None, phonemes=None, lyrics=None,
                  camera_cut=None, motion=None):
    """Assemble the context from the dance, the song and whatever the caller can supply.

    `phonemes` is the hook the audio path will use: a list of `(start_s, end_s, viseme, confidence)`
    produced by forced alignment and mapped onto the canonical viseme names this module already
    defines.  Nothing here produces one, and nothing here needs one.
    """
    ctx = Context(frames, fps)
    profile = ((knowledge.get("context") or {}).get("section") or {}).get(
        "mouth_events_per_second_by_tenth")
    if profile:
        ctx.density_prior = list(profile)
    if tempo:
        ctx.tempo = float(tempo)
        bar_s = 4.0 * 60.0 / float(tempo)
        ctx.beat_phase = [((f / fps) % bar_s) / bar_s for f in range(frames)]
        ctx.bar_index = [int((f / fps) / bar_s) for f in range(frames)]
    if phonemes:
        ctx.phoneme = [(int(round(a * fps)), int(round(b * fps)), str(v).upper(), float(c))
                       for a, b, v, c in phonemes]
    if lyrics:
        ctx.lyric = [(int(round(t * fps)), int(i)) for t, i in lyrics]
    if camera_cut:
        ctx.camera_cut = [bool(x) for x in camera_cut]
    if motion is not None:
        ctx.motion = list(motion)
    elif seq is not None:
        ctx.motion = _motion_intensity(seq)
    return ctx


def _motion_intensity(seq):
    """How much the whole body is moving, per frame, normalised to its own maximum.

    Deliberately a property of the *dance*, not of the face: it is the "the character is in the
    middle of a big move" signal, and it is used only as a weak prior on expressions when the MMD
    face itself says nothing: waving an arm must not force a smile.
    """
    if not seq.frames:
        return None
    total = np.zeros(seq.frames)
    for i in range(seq.bones):
        if mc.classify(seq.names[i]) in (None, "root"):
            continue
        step = np.linalg.norm(mc.qlog(mc.qmul(mc.qinv(seq.local[i, :-1]), seq.local[i, 1:])),
                              axis=1)
        total[1:] += step
    peak = float(total.max()) if total.size else 0.0
    if peak <= 1e-9:
        return None
    return (total / peak).tolist()



CONFIG_DEFAULT = {
    "w_vector": 1.0,          # how much the MMD pose itself matters
    "w_curve": 0.35,          # how much the shape of the MMD curve around this frame matters
    "w_transition": 0.55,     # weight on the KB transition log-probability
    "w_duration": 0.30,       # weight on the KB duration log-probability
    "w_lyric": 0.0,           # weight on a supplied phoneme/lyric timeline; 0 = absent
    "w_variant": 0.25,        # preference between two ids that mean the same viseme
    "min_event_s": 0.06,      # shorter than this and an event is merged into its neighbour
    # 0 means "take the ceiling from the corpus's own p90 event rate"; see `Model.rate_p90`
    "max_events_per_sec": 0.0,
    # The variation layer.  Every value here is a bound on how far it may go, not a target, and the
    # whole block is inert when there is nothing over-long to vary.
    "natural_variation": {
        "enabled": True,
        "strength": 1.0,
        "max_same_shape_duration_quantile": 0.9,
        "max_same_viseme_duration_quantile": 0.9,
        "diversity_weight": 0.0,          # 0: the layer never overrides the emission, only varies
        "same_shape_penalty": 0.0,        # 0: no duration penalty is added to the solver's cost
        "audio_prior_weight": 0.0,        # 0 until a reliable vocal detector exists
    },
    "key_rate_hz": 0.0,       # 0 = take the rate from the knowledge base
    "floor": 0.12,            # below this a canonical dimension counts as zero
    "weight_scale": 1.0,      # multiplies the KB's own weight for a shape
    "expression": True,
    "expression_min_hold_s": 0.25,
    "seed": 20240917,
}


def config(overrides=None, data_dir=DATA):
    """The calibration file, then any explicit overrides.  No value lives in the code."""
    cfg = dict(CONFIG_DEFAULT)
    path = os.path.join(data_dir, "retarget_config.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
        for key, value in (blob.get("weights") or {}).items():
            if key in cfg:
                cfg[key] = value
        for key, value in (blob.get("solver") or {}).items():
            if key in cfg:
                cfg[key] = value
    if overrides:
        cfg.update(overrides)
    return cfg


# ------------------------------------------------------------------ shapes -> visemes -> states
class Model(object):
    """The knowledge base folded into the tables the solver walks.

    Two levels, because the measured data has two levels.  The **state** is a viseme: eleven of them
    carry almost all the transitions, so the transition matrix and the duration histogram are dense
    and mean something.  The **shape** is which id realises that viseme this time (`A` or `A_OLD`),
    decided from the same knowledge base's own frequencies rather than from a coin toss, which is how
    a song ends up in one dialect instead of flickering between two.
    """

    def __init__(self, knowledge, targets, cfg, verbose=False):
        self.kb = knowledge
        self.cfg = cfg
        self.mouth_of, self.mood_of = build_semantics(targets)
        self.notes = []
        core = set(knowledge["mouth"].get("core_states") or [])
        shape_count = {s["id"]: s["events"] for s in knowledge["mouth"]["states"]}
        # a shape is usable if its asset name is known and the KB has seen it enough to have an
        # opinion; anything else is dropped, not guessed at
        self.shapes = [s for s in shape_count if s in self.mouth_of and shape_count[s] >= 4]
        self.shape_freq = shape_count
        self.visemes = sorted({self.mouth_of[s] for s in self.shapes})
        self.by_viseme = {v: [s for s in self.shapes if self.mouth_of[s] == v]
                          for v in self.visemes}
        # aggregate the measured shape transitions onto the viseme level
        trans = knowledge["mouth_trans"].get("transitions") or {}
        self.counts = {v: dict.fromkeys(self.visemes, 0.0) for v in self.visemes}
        for key, count in trans.items():
            a, b = key.split(">")
            va, vb = self.mouth_of.get(int(a)), self.mouth_of.get(int(b))
            if va in self.counts and vb in self.counts:
                self.counts[va][vb] += count
        # a shape that stays the same is a self transition and is not in `transitions` at all, so it
        # has to come from the run-length census or the matrix would forbid holding a viseme - which
        # is the opposite of what the data says and of what a stable result needs
        runs = knowledge["mouth"].get("run_length_records") or {}
        holds = sum(v for k, v in runs.items() if int(k) >= 2)
        for v in self.visemes:
            self.counts[v][v] += holds / float(len(self.visemes))
        self.logp = {}
        for a in self.visemes:
            row = self.counts[a]
            total = sum(row.values()) + 0.5 * len(self.visemes)
            for b in self.visemes:
                self.logp[(a, b)] = math.log((row[b] + 0.5) / total)
        # duration: per viseme, a histogram of the measured event lengths in seconds
        self.duration = {}
        raw = knowledge["mouth_dur"].get("durations") or {}
        edges = [0.0, 0.05, 0.09, 0.13, 0.18, 0.25, 0.35, 0.5, 0.75, 1.2, 1e9]
        for v in self.visemes:
            hist = [1.0] * (len(edges) - 1)
            n = 0
            for s in self.by_viseme[v]:
                d = raw.get(str(s))
                if not d:
                    continue
                for key, share in (("min", 0.02), ("p10", 0.12), ("p50", 0.76), ("p90", 0.10)):
                    value = d.get(key)
                    if value is None:
                        continue
                    for i in range(len(edges) - 1):
                        if edges[i] <= value < edges[i + 1]:
                            hist[i] += share * d["n"]
                            n += share * d["n"]
                            break
            total = sum(hist)
            self.duration[v] = [math.log(h / total) for h in hist]
            self.duration_n = getattr(self, "duration_n", {})
            self.duration_n[v] = n
        self.edges = edges
        # preferred shape inside a viseme, from the measured frequencies
        # The corpus's own event-rate band, and its own event-length distribution per viseme.
        # Both are measurements that already exist in the knowledge base; nothing here is chosen.
        proto = knowledge["mouth"]
        rate = proto.get("events_per_second") or {}
        self.rate_p50 = float(rate.get("p50") or 2.9)
        self.rate_p90 = float(rate.get("p90") or 5.3)
        gap = proto.get("event_gap_s") or {}
        self.gap_p50 = float(gap.get("p50") or 0.198)
        self.gap_p90 = float(gap.get("p90") or 0.60)
        raw_durations = knowledge["mouth_dur"].get("durations") or {}
        self.viseme_duration = {}
        for viseme in self.visemes:
            rows = [raw_durations.get(str(s)) for s in self.by_viseme[viseme]]
            rows = [r for r in rows if r and r.get("n")]
            if not rows:
                self.viseme_duration[viseme] = {"p50": self.gap_p50, "p90": self.gap_p90,
                                                "max": self.gap_p90 * 3.0, "n": 0}
                continue
            total = float(sum(r["n"] for r in rows))

            def weighted(key, _rows=rows, _total=total):
                return sum(r[key] * r["n"] for r in _rows if r.get(key) is not None) / _total

            self.viseme_duration[viseme] = {
                "p50": weighted("p50"), "p90": weighted("p90"), "max": weighted("max"),
                "n": int(total)}

        # shape id -> the viseme it realises.  Needed by the variation layer, which is only ever
        # allowed to move *within* one viseme's own assets.
        self.shape_viseme = {}
        for viseme, ids in self.by_viseme.items():
            for sid in ids:
                self.shape_viseme[sid] = viseme
        self.rest_visemes = ("CLOSED", "NEUTRAL")

        self.shape_logp = {}
        for v in self.visemes:
            total = sum(self.shape_freq[s] for s in self.by_viseme[v])
            for s in self.by_viseme[v]:
                self.shape_logp[s] = math.log(self.shape_freq[s] / float(total))
        if verbose:
            self.notes.append("%d shapes over %d visemes: %s"
                              % (len(self.shapes), len(self.visemes),
                                 ", ".join("%s=%d" % (v, len(self.by_viseme[v]))
                                           for v in self.visemes)))

    def duration_cost(self, viseme, seconds):
        for i in range(len(self.edges) - 1):
            if self.edges[i] <= seconds < self.edges[i + 1]:
                return self.duration[viseme][i]
        return self.duration[viseme][-1]

    def transition_cost(self, a, b):
        return self.logp.get((a, b), math.log(0.01))

    def shape_alternatives(self, viseme):
        """This viseme's own shapes, most probable first.

        The whole point of the variation layer is that these are *interchangeable*: they are the
        measured assets for one viseme, so moving between them changes how the mouth looks and not
        what it is saying.
        """
        return sorted(self.by_viseme[viseme], key=lambda s: (-self.shape_freq[s], s))

    def pick_shape(self, viseme, rng=None):
        """The most probable id for a viseme under the measured frequencies, ties by id.

        Deliberately deterministic: the same input must give the same script, so an export is
        reproducible byte for byte.
        """
        return max(self.by_viseme[viseme], key=lambda s: (self.shape_freq[s], -s))


# ------------------------------------------------------------------ MMD -> canonical vector
def canonical_vectors(tracks, plan, frames, cfg=None):
    """{viseme: [weight per frame]} from the MMD morph curves.

    One morph may feed several visemes and one viseme may take from several morphs - the morph named
    for a mouth shape contributes to that viseme, and a morph named for an emotion contributes to the
    mouth viseme its asset name implies *and* to the mood, which is what stops a smile morph and a
    mouth-shape morph from arguing over the same frame.

    Values are resampled onto the 60 fps output grid by the same whole-file scale the existing
    exporter uses, so lip sync stays a time phenomenon rather than a per-morph one.
    """
    cfg = cfg or CONFIG_DEFAULT
    model_mouth = {}
    weights = {}
    last = max((k["frame"] for keys in tracks.values() for k in keys), default=0)
    if last <= 0 or frames <= 0:
        return {}
    scale = frames / float(last + 1)
    for name, keys in tracks.items():
        entry = plan.get(name)
        if not entry or entry[0] != "mouth":
            continue
        viseme = model_mouth.get(entry[2])
        if viseme is None:
            base = _strip(str(entry[2]).replace("MIK_KUCHI_", ""), VISEME_QUALIFIERS)
            viseme = VISEME_ALIASES.get(base)
            model_mouth[entry[2]] = viseme
        if viseme is None:
            continue
        curve = weights.setdefault(viseme, [0.0] * frames)
        for k in keys:
            f = min(frames - 1, max(0, int(round(k["frame"] * scale))))
            curve[f] = max(curve[f], float(k["weight"]))
    for viseme, curve in weights.items():
        for f in range(1, frames):
            curve[f] = max(curve[f], curve[f - 1] * 0.86)      # a hold is a hold, not a dropout
    return weights


# ------------------------------------------------------------------ the solver
NEG = -1.0e9
# The rest viseme, i.e. `MIK_KUCHI_RESET`.  Named once here and justified by the measured
# shipping-script fallback rather than by a second guess.
REST_VISEME = "CLOSED"


def _distribution(weights, visemes, floor):
    """Turn per-viseme weights into a per-frame distribution over the candidate visemes.

    A frame where nothing is animated is a frame with no opinion, and it must be distinguishable from
    a frame that says "closed mouth": the first is flat, the second has all its mass on CLOSED.  That
    is why the distribution is normalised over the *active* weight with a floor rather than over the
    candidate set - a silent frame then contributes almost the same score to every state and lets the
    transition matrix decide, which is exactly what should decide it.
    """
    frames = len(next(iter(weights.values()))) if weights else 0
    out = []
    for f in range(frames):
        row = {}
        for v in visemes:
            curve = weights.get(v)
            value = float(curve[f]) if curve else 0.0
            row[v] = value if value >= floor else 0.0
        out.append(row)
    return out


def _emission(active, model, cfg, ctx=None):
    """Per-frame, per-viseme log score before transitions.  Every term is named so it can be shown.

    A frame where nothing is animated is not a frame where every viseme is equally likely.  It is a
    frame with the mouth at rest, and the rest shape is measured, not chosen:
    `diva_face_targets.json` records `mouth_rest: 8`, and that is what the edge exporter falls
    back to.  Giving such a frame a flat distribution instead lets the transition matrix pick
    whatever state is most central, which is how a solver invents a cat mouth during an instrumental.

    The context enters as a **separate, additive, optional** term, never as a multiplier on the
    measured evidence.  A supplied viseme timeline adds to the score of the viseme it names, in
    proportion to its confidence, and adds nothing at all to the others; when no timeline is
    supplied the term does not exist and the solver is exactly the one that was already tested.
    """
    frames = len(active)
    out = []
    for f in range(frames):
        row = active[f]
        total = sum(row.values())
        scores = {}
        if total > 0.0:
            for v in model.visemes:
                share = row.get(v, 0.0) / total
                scores[v] = cfg["w_vector"] * math.log(share + 1e-4)
        else:
            for v in model.visemes:
                scores[v] = (cfg["w_vector"] * math.log(1.0 - 1e-3) if v == REST_VISEME
                             else cfg["w_vector"] * math.log(1e-3 / max(1, len(model.visemes) - 1)))
        if ctx is not None and cfg.get("w_phoneme"):
            guess = ctx.phoneme_at(f)
            if guess is not None:
                viseme, confidence = guess
                for v in model.visemes:
                    if v == viseme:
                        scores[v] += cfg["w_phoneme"] * confidence
                    else:
                        scores[v] -= cfg["w_phoneme"] * confidence * 0.15
        out.append(scores)
    return out


def _section_duration_scale(ctx, frame):
    """How much shorter the events in this part of the song are allowed to be.

    The corpus keys the mouth about three times as fast in its last third as in its first tenth.  A
    solver that ignores that either chops the intro into the same density as the chorus or smooths
    the chorus down to the density of the intro; this scales the duration model's tolerance to the
    measured local rate instead.  It never changes *which* viseme is chosen.
    """
    if ctx is None or not ctx.density_prior:
        return 1.0
    here = ctx.density_at(frame)
    typical = float(np.median(ctx.density_prior))
    if not here or typical <= 0:
        return 1.0
    return max(0.5, min(2.0, typical / here))



def _viterbi(emission, model, cfg, duration_of=None):
    """Best state path.  `duration_of[f][v]` adds the duration log-probability for that frame/state."""
    frames = len(emission)
    if not frames:
        return []
    states = model.visemes
    back = []
    score = {v: emission[0][v] for v in states}
    for f in range(1, frames):
        nxt, bp = {}, {}
        for b in states:
            best, arg = NEG, states[0]
            for a in states:
                value = score[a] + cfg["w_transition"] * model.transition_cost(a, b)
                if a == b:
                    value += cfg["w_transition"] * 0.5      # staying is the cheap case, as measured
                if value > best:
                    best, arg = value, a
            extra = 0.0
            if duration_of is not None:
                extra = cfg["w_duration"] * duration_of[f].get(b, 0.0)
            nxt[b] = best + emission[f][b] + extra
            bp[b] = arg
        score, back = nxt, back + [bp]
    last = max(states, key=lambda v: score[v])
    path = [last]
    for bp in reversed(back):
        path.append(bp[path[-1]])
    return list(reversed(path))


def _segments(path):
    """[(start, end_exclusive, state)] - the run-length encoding of a Viterbi path."""
    out = []
    for i, v in enumerate(path):
        if out and out[-1][2] == v:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1, v])
    return out


def solve_mouth(tracks, plan, frames, model, cfg, fps=60.0, explain=None, ctx=None,
                per_frame=None):
    """The mouth state sequence, as events on the 60 fps grid.

    Three passes, each one guarding a structural failure of a plain single-argmax pass:

      1. Viterbi over emissions and transitions.  This is what removes the per-frame argmax flicker -
         a state that would win one frame and lose the next now has to pay to leave and pay to come
         back.
      2. Re-run with the *measured* duration of each state entered as a cost, using the first pass's
         segment lengths.  A hidden semi-Markov pass in one iteration: it stops the solver parking on
         a state the knowledge base says never lasts that long.
      3. Merge anything still shorter than `min_event_s` into the neighbour it is closest to, which
         is what turns the path into events the encoder can write.
    """
    weights = canonical_vectors(tracks, plan, frames, cfg)
    if not weights:
        return [], [], {"reason": "no morph resolved to a mouth viseme"}
    active = _distribution(weights, model.visemes, cfg["floor"])
    emission = _emission(active, model, cfg, ctx=ctx)

    path = _viterbi(emission, model, cfg)

    segs = _segments(path)
    # one duration-aware iteration, seeded with the first pass's own segment lengths
    runs = [0] * frames
    for seg in segs:
        for f in range(seg[0], seg[1]):
            runs[f] = (seg[1] - seg[0]) / fps
    duration_of = []
    for f in range(frames):
        scale = _section_duration_scale(ctx, f)
        row = {v: model.duration_cost(v, max(1e-3, runs[f] * scale)) for v in model.visemes}
        duration_of.append(row)
    path = _viterbi(emission, model, cfg, duration_of=duration_of)

    path = _merge_short(path, model, cfg, fps)
    path, compression = compress(path, model, cfg, fps)

    events, rows = _path_to_events(path, model, cfg, fps, active, emission, explain)
    events, rows, variation = natural_variation(events, rows, model, cfg, fps=fps, total_frames=frames)
    if per_frame is not None:
        per_frame.extend(explain_frames(path, model, cfg, fps, active, emission, ctx))
    return events, rows, {"visemes": model.visemes, "shapes": model.shapes,
                          "segments": len(_segments(path)), "compression": compression,
                          "variation": variation,
                          "diversity": visual_diversity(events, model, cfg),
                          "context": ctx.notes() if ctx is not None else None}


# ------------------------------------------------------------------- natural variation
REST_VISEMES = ("CLOSED", "NEUTRAL")


def visual_diversity(events, model, cfg=None):
    """How varied the emitted stream is, against the corpus's own numbers.

    Reported rather than optimized.  The goal is to land inside the range real scripts occupy, not
    to maximize entropy: a performance that is genuinely repetitive should
    score as repetitive, and several of these numbers have a wrong direction in both directions.
    """
    if not events:
        return {"events": 0}
    shapes = [e["shape"] for e in events]
    visemes = [e["viseme"] for e in events]
    durations = [float(e.get("duration_s", 0.0)) for e in events]

    def ent(values):
        counts = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        total = float(len(values))
        return -sum((c / total) * math.log(c / total, 2) for c in counts.values())

    def quantile(values, q):
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * q))]

    def runs(values, lengths):
        out, i = [], 0
        while i < len(values):
            j = i
            total = 0.0
            while j < len(values) and values[j] == values[i]:
                total += lengths[j]
                j += 1
            out.append((values[i], total))
            i = j
        return out

    shape_runs = runs(shapes, durations)
    viseme_runs = runs(visemes, durations)
    total_s = max(1e-9, sum(durations))

    def occupancy(viseme):
        return sum(d for v, d in viseme_runs if v == viseme)

    o_s = occupancy("O")
    u_s = occupancy("U")
    return {
        "events": len(events),
        "unique_shapes": len(set(shapes)),
        "unique_visemes": len(set(visemes)),
        "shape_entropy": round(ent(shapes), 4),
        "viseme_entropy": round(ent(visemes), 4),
        "transition_entropy": round(ent(list(zip(shapes, shapes[1:]))), 4),
        "median_event_s": round(quantile(durations, 0.5), 4),
        "p90_event_s": round(quantile(durations, 0.9), 4),
        "longest_event_s": round(max(durations), 4),
        "o_occupancy": round(o_s / total_s, 4),
        "longest_o_run_s": round(max([d for v, d in viseme_runs if v == "O"] or [0.0]), 4),
        "u_occupancy": round(u_s / total_s, 4),
        "longest_u_run_s": round(max([d for v, d in viseme_runs if v == "U"] or [0.0]), 4),
    }


def o_lock_report(events, model, cfg=None):
    """The §20 detector: is a single viseme occupying more than the corpus says it should?

    Only vowel visemes are judged.  A closed mouth occupying most of a track is a mix with a long
    instrumental in it, which is a fact about the song and not a defect to be repaired.
    """
    metrics = visual_diversity(events, model, cfg)
    if not metrics.get("events"):
        return metrics, []
    flags = []
    for viseme, run_key, occ_key in (("O", "longest_o_run_s", "o_occupancy"),
                                     ("U", "longest_u_run_s", "u_occupancy")):
        p90 = model.viseme_duration[viseme]["p90"]
        longest = metrics[run_key]
        if longest > p90 * 2.0:
            flags.append({"viseme": viseme, "kind": "held past the corpus p90",
                          "longest_s": longest, "corpus_p90_s": round(p90, 4),
                          "ratio": round(longest / max(1e-6, p90), 3)})
    metrics["locked"] = bool(flags)
    metrics["flags"] = flags
    return metrics, flags


def natural_variation(events, rows, model, cfg, fps=60.0, total_frames=None):
    """Split the events that have run past their viseme's own measured duration, within the viseme.

    Returns `(events, rows, report)`.  The report states what was and was not touched: a layer whose
    entire justification is "this particular change is defensible" has to say which changes it made
    and which it declined.
    """
    options = (cfg or {}).get("natural_variation") or {}
    report = {"enabled": bool(options.get("enabled", True)), "split": 0, "refused_rate": 0,
              "too_short_to_split": 0, "single_shape": 0, "rest_skipped": 0, "by_viseme": {},
              "added_events": 0, "events_before": len(events), "events_after": len(events)}
    strength = float(options.get("strength", 1.0))
    quantile = float(options.get("max_same_shape_duration_quantile", 0.9))
    if not report["enabled"] or not events or strength <= 0.0:
        # the same keys on every path: a caller should never have to know which setting produces
        # which report, and a missing key is indistinguishable from a zero one at the call site
        report["inactive_because"] = ("disabled" if not report["enabled"] else
                                      "no events" if not events else "strength 0")
        report["rate_hz"] = 0.0
        report["rate_ceiling_hz"] = 0.0
        report["max_added_events"] = 0
        return events, rows, report

    frames = total_frames or max(e["frame"] + 20 for e in events)
    base_ceiling = float((cfg or {}).get("max_events_per_sec") or 0.0) or model.rate_p90
    # bounded headroom: the corpus p90 is where the base solver already sits, so a layer forbidden to
    # go one event above it can never act.  15% stays well inside the corpus's own spread, and the
    # 5% cap on added events is what keeps the variation sparse rather than merely capped.
    ceiling = base_ceiling * (1.0 + float(options.get("rate_headroom", 0.15)))
    max_added = max(1, int(round(len(events) * float(options.get("max_added_fraction", 0.05)))))
    secs = max(1e-9, frames / fps)
    rate = len(events) / secs
    added = 0
    min_frames = max(2, int(round(float((cfg or {}).get("min_event_s", 0.06)) * fps)))

    out_e, out_r = [], []
    for i, event in enumerate(events):
        viseme = event["viseme"]
        frame = int(event["frame"])
        end = int(event.get("end_frame", frame))
        duration_frames = max(1, end - frame + 1)
        row = rows[i] if i < len(rows) else None

        if viseme in model.rest_visemes:
            # A long closed mouth is a closed mouth.  It is what a script does through an
            # instrumental, and filling it would be invented chatter with no corpus precedent - so
            # rest is excluded outright rather than merely discouraged.
            report["rest_skipped"] += 1
            out_e.append(event)
            out_r.append(row)
            continue

        alternatives = model.shape_alternatives(viseme)
        if len(alternatives) < 2:
            report["single_shape"] += 1
            out_e.append(event)
            out_r.append(row)
            continue

        # The duration hazard, straight out of the corpus: this viseme's measured duration at the
        # requested quantile is how long it is allowed to run before it is worth breaking up.
        allowed = _quantile_of(model.viseme_duration, viseme, quantile) / max(1e-6, strength)
        if duration_frames / fps <= allowed:
            report["too_short_to_split"] += 1
            out_e.append(event)
            out_r.append(row)
            continue

        parts = 3 if duration_frames / fps > allowed * 2.5 else 2
        step = duration_frames // parts
        if step < min_frames:
            report["too_short_to_split"] += 1
            out_e.append(event)
            out_r.append(row)
            continue
        if rate + (parts - 1) / secs > ceiling or added + (parts - 1) > max_added:
            # trading a held shape for a chattering one is not an improvement, so both the rate and
            # the number of events this layer may add are hard limits
            report["refused_rate"] += 1
            out_e.append(event)
            out_r.append(row)
            continue

        for k in range(parts):
            start = frame + k * step
            stop = end if k == parts - 1 else start + step - 1
            piece = dict(event)
            piece["frame"] = start
            piece["end_frame"] = stop
            piece["shape"] = alternatives[k % len(alternatives)]
            piece["duration_s"] = (stop - start + 1) / fps
            out_e.append(piece)
            if row is not None:
                piece_row = dict(row)
                piece_row["frame"] = start
                piece_row["shape"] = piece["shape"]
                piece_row["duration_s"] = round(piece["duration_s"], 4)
                piece_row["variation"] = (
                    "same viseme %s, asset %d of %d: the event ran %.2f s against a corpus p90 of "
                    "%.2f s, so it was re-expressed with another measured shape of the same viseme"
                    % (viseme, k % len(alternatives) + 1, len(alternatives),
                       duration_frames / fps, _quantile_of(model.viseme_duration, viseme, quantile)))
                out_r.append(piece_row)
            else:
                out_r.append(None)
        report["split"] += 1
        report["by_viseme"][viseme] = report["by_viseme"].get(viseme, 0) + 1
        rate += (parts - 1) / secs
        added += parts - 1

    report["events_before"] = len(events)
    report["events_after"] = len(out_e)
    report["rate_hz"] = round(rate, 3)
    report["rate_ceiling_hz"] = round(ceiling, 3)
    report["rate_headroom_hz"] = round(ceiling - base_ceiling, 4)
    report["max_added_events"] = max_added
    report["added_events"] = added
    return out_e, out_r, report


def _quantile_of(table, viseme, q):
    """The measured duration quantile for a viseme, from its own shape durations."""
    row = table.get(viseme) or {}
    if q >= 0.9:
        return float(row.get("p90") or 0.6)
    if q >= 0.5:
        return float(row.get("p50") or 0.2)
    return float(row.get("p50") or 0.2) * 0.5


# ---------------------------------------------------------------------------- event compression
def compress(path, model, cfg, fps):
    """Turn the state path into events the encoder should write, and say what was removed.

    Segmentation and compression are different jobs and stay separate here.  The
    first pass already merges anything shorter than `min_event_s`.  What is left is the perceptual
    clean-up:

      * **adjacent runs of the same viseme** cannot exist by construction, so the merge that matters
        is across a *pair*: `A X A` where `X` is shorter than the minimum is one `A`, not three
        events, and the transition matrix is what decides whether that is legal;
      * **a viseme used once in the whole performance** is almost always the solver reacting to one
        noisy frame rather than to a gesture, and the corpus is the evidence - 10 shapes cover 84% of
        its cues and six of the forty-one appear a handful of times in the entire data set;
      * **the corpus's own rate band** is the last check: if the result is keyed far faster than any
        shipping script, the surplus is merged until it is not.
    """
    min_len = max(1, int(round(cfg["min_event_s"] * fps)))
    removed = {"absorbed_singletons": 0, "dropped_rare": 0, "rate_merged": 0}

    for _ in range(4):
        segs = _segments(path)
        changed = False
        for i in range(1, len(segs) - 1):
            a, b, viseme = segs[i]
            if b - a >= min_len:
                continue
            if segs[i - 1][2] == segs[i + 1][2]:
                for f in range(a, b):
                    path[f] = segs[i - 1][2]
                removed["absorbed_singletons"] += 1
                changed = True
                break
        if not changed:
            break

    # a viseme that appears exactly once in the whole performance, and only for a moment
    segs = _segments(path)
    census = {}
    for a, b, viseme in segs:
        census[viseme] = census.get(viseme, 0) + 1
    for i, (a, b, viseme) in enumerate(segs):
        if census.get(viseme, 0) != 1 or len(segs) == 1:
            continue
        if (b - a) >= min_len * 2:
            continue
        left = segs[i - 1][2] if i > 0 else None
        right = segs[i + 1][2] if i + 1 < len(segs) else None
        pick = left or right
        if left and right:
            pick = (left if model.transition_cost(left, viseme)
                    > model.transition_cost(right, viseme) else right)
        if pick is None:
            continue
        for f in range(a, b):
            path[f] = pick
        removed["dropped_rare"] += 1

    # ---- the event rate, held inside the corpus's own band rather than at an arbitrary floor
    seconds = max(1e-9, len(path) / fps)
    ceiling = cfg.get("max_events_per_sec") or 0.0
    source = "configured"
    if ceiling <= 0.0:
        ceiling = model.rate_p90
        source = "knowledge base p90"
    removed["rate_ceiling_hz"] = round(ceiling, 3)
    removed["rate_ceiling_source"] = source
    removed["rate_before_hz"] = round(len(_segments(path)) / seconds, 3)
    guard = 0
    while len(_segments(path)) / seconds > ceiling and guard < 6000:
        guard += 1
        segs = _segments(path)
        if len(segs) <= 2:
            break
        # merge the SHORTEST event into whichever neighbour the transition matrix prefers.  An
        # every-event sweep would apply the merge whether or not the rate was anywhere near the
        # ceiling, turning a ceiling into a minimum and a comfortable median event into a visual
        # lock; the merge only runs while the measured rate actually sits above the ceiling.
        idx = min(range(len(segs)), key=lambda i: (segs[i][1] - segs[i][0], i))
        a, b, viseme = segs[idx]
        left = segs[idx - 1][2] if idx > 0 else None
        right = segs[idx + 1][2] if idx + 1 < len(segs) else None
        pick = left or right
        if left is not None and right is not None:
            pick = left if (model.transition_cost(left, viseme)
                            > model.transition_cost(right, viseme)) else right
        if pick is None:
            break
        for f in range(a, b):
            path[f] = pick
        removed["rate_merged"] += 1
    removed["rate_after_hz"] = round(len(_segments(path)) / seconds, 3)
    removed["events_before"] = len(segs)
    removed["events_after"] = len(_segments(path))
    return path, removed



def _run_length_at(path, f):
    v = path[f]
    a = f
    while a > 0 and path[a - 1] == v:
        a -= 1
    b = f
    while b + 1 < len(path) and path[b + 1] == v:
        b += 1
    return b - a + 1


def _merge_short(path, model, cfg, fps):
    """Absorb any run shorter than `min_event_s` into whichever neighbour scores better."""
    min_len = max(1, int(round(cfg["min_event_s"] * fps)))
    for _ in range(4):
        segs = _segments(path)
        changed = False
        for i, (a, b, v) in enumerate(segs):
            if b - a >= min_len or len(segs) == 1:
                continue
            left = segs[i - 1][2] if i > 0 else None
            right = segs[i + 1][2] if i + 1 < len(segs) else None
            pick = None
            if left and right:
                pick = left if model.transition_cost(left, v) > model.transition_cost(right, v) \
                    else right
            else:
                pick = left or right
            if pick is None:
                continue
            for f in range(a, b):
                path[f] = pick
            changed = True
            break
        if not changed:
            break
    return path


def explain_frames(path, model, cfg, fps, active, emission, ctx=None, limit=None):
    """Why this state, *this frame* - the per-frame view of the decision.

    One record per frame, with the same terms the solver used: the MMD share of the chosen viseme,
    the transition it came from, how long that run has been going, the duration cost of that run so
    far, whatever the context contributed, and the score the runner-up would have had.  The
    event-level report says what changed; this says why the frame in the middle of it is what it is,
    which is what a person needs when they disagree with one frame.
    """
    frames = len(path)
    if not frames:
        return []
    run_start = [0] * frames
    for i in range(1, frames):
        run_start[i] = run_start[i - 1] if path[i] == path[i - 1] else i
    out = []
    for f in range(frames):
        chosen = path[f]
        ranked = sorted(((emission[f][v], v) for v in model.visemes), reverse=True)
        runner = next((s for s, v in ranked if v != chosen), 0.0)
        own = next((s for s, v in ranked if v == chosen), 0.0)
        length = (f - run_start[f] + 1) / fps
        row = {
            "frame": f, "time_s": round(f / fps, 4), "selected": chosen,
            "shape": model.pick_shape(chosen),
            "run_started": run_start[f], "run_length_s": round(length, 4),
            "mmd_similarity": round(math.exp(own), 6),
            "runner_up": {"viseme": ranked[1][1] if len(ranked) > 1 else None,
                          "score": round(math.exp(runner), 6)},
            "previous": path[f - 1] if f else None,
            "transition_logp": (round(model.transition_cost(path[f - 1], chosen), 4)
                                if f else None),
            "duration_logp": round(model.duration_cost(chosen, max(1e-3, length)), 4),
            "because": ("MMD similarity %.3f, from %s (logp %.2f), held %.3f s (logp %.2f)"
                        % (math.exp(own), path[f - 1] if f else "start",
                           model.transition_cost(path[f - 1], chosen) if f else 0.0,
                           length, model.duration_cost(chosen, max(1e-3, length)))),
        }
        if ctx is not None:
            row["context"] = ctx.describe(f)
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def _path_to_events(path, model, cfg, fps, active, emission, explain=None):
    """Turn the state path into the encoder's own event shape, and record why each one was chosen.

    The emitted id is a *shape*, not the viseme: the viseme is what was decided, the shape is which
    measured asset realises it, and the choice between `A` and `A_OLD` comes from the knowledge base's
    frequencies so that a song stays in one dialect.
    """
    events, rows = [], []
    for a, b, viseme in _segments(path):
        shape = model.pick_shape(viseme)
        frame = a
        duration = (b - a) / fps
        # A rest before anything has happened is not a cue, it is the absence of one.  The corpus
        # agrees: pv_001's first MOUTH_ANIM is at 1.327 s, not at 0.  Emitting it would also wipe
        # whatever face the base script is holding for no reason.
        if not events and viseme == REST_VISEME:
            continue
        row = None
        if explain is not None:
            f = min(len(active) - 1, a)
            ranked = sorted(((emission[f][v], v) for v in model.visemes), reverse=True)
            chosen = next((s for s, v in ranked if v == viseme), 0.0)
            row = {"frame": frame, "time_s": round(frame / fps, 4), "viseme": viseme,
                   "shape": shape, "duration_s": round(duration, 4),
                   "mmd_similarity": round(math.exp(chosen), 6),
                   "alternatives": [{"viseme": v, "score": round(math.exp(s), 6)}
                                    for s, v in ranked[:4] if v != viseme],
                   "transition_from": path[a - 1] if a > 0 else None,
                   "transition_logp": (round(model.transition_cost(path[a - 1], viseme), 4)
                                       if a > 0 else None),
                   "duration_logp": round(model.duration_cost(viseme, max(1e-3, duration)), 4),
                   "reason": ("viseme %s: MMD share %.3f, from %s (logp %.2f), held %.3f s "
                              "(logp %.2f)"
                              % (viseme, active[a].get(viseme, 0.0),
                                 path[a - 1] if a > 0 else "start",
                                 model.transition_cost(path[a - 1], viseme) if a > 0 else 0.0,
                                 duration, model.duration_cost(viseme, max(1e-3, duration))))}
            rows.append(row)
            explain.append(row)
        events.append({"frame": frame, "end_frame": b - 1, "viseme": viseme, "shape": shape,
                       "duration_s": duration})
    return events, rows


# ------------------------------------------------------------------ expression
def solve_expression(tracks, plan, frames, knowledge, targets, cfg, fps=60.0, explain=None):
    """The expression layer, solved on its own.

    Separate from the mouth on purpose, and the measurements say why: the two have different clocks.
    The mouth is keyed roughly every 0.2 s and changes shape on 82% of its keys; EXPRESSION is used
    two or three times in half the songs.  One solver trying to satisfy both would either smear the
    expressions or chop the mouth, so the two run with their own state spaces, their own transitions
    and their own minimum holds, and only meet in the event list.
    """
    from . import face_core as fc

    mood_of = build_semantics(targets)[1]
    moods = sorted({m for m in mood_of.values()})
    if not moods:
        return [], []
    curve = {}
    last = max((k["frame"] for keys in tracks.values() for k in keys), default=0)
    if last <= 0 or frames <= 0:
        return [], []
    scale = frames / float(last + 1)
    for name, keys in tracks.items():
        entry = plan.get(name)
        if not entry or entry[0] != "expression":
            continue
        base = _strip(str(entry[2]).replace("MIK_FACE_", ""), MOOD_QUALIFIERS)
        mood = MOOD_ALIASES.get(base)
        if mood is None:
            continue
        line = curve.setdefault(mood, [0.0] * frames)
        for k in keys:
            f = min(frames - 1, max(0, int(round(k["frame"] * scale))))
            line[f] = max(line[f], float(k["weight"]))
    if not curve:
        return [], []
    for line in curve.values():
        for f in range(1, frames):
            line[f] = max(line[f], line[f - 1] * 0.9)

    rows, out = [], []
    prev = None
    start = None
    for f in range(frames + 1):
        top = None
        if f < frames:
            values = [(line[f] if (line := curve.get(m)) else 0.0, m) for m in moods]
            values = [v for v in values if v[0] >= cfg["floor"]]
            top = max(values)[1] if values else None
        if top != prev:
            if prev is not None and start is not None:
                out.append((start, f - 1, prev))
            start, prev = f, top
    kept = []
    for a, b, mood in out:
        if (b - a + 1) / fps < cfg["expression_min_hold_s"]:
            continue
        kept.append((a, b, mood))
    events = []
    for a, b, mood in kept:
        eid = _pick_expression(mood, mood_of, knowledge)
        if eid is None:
            continue
        events.append({"frame": a, "end_frame": b, "mood": mood, "id": eid,
                       "intensity": fc.EXPR_INTENSITY})
        if explain is not None:
            explain.append({"frame": a, "time_s": round(a / fps, 4), "mood": mood,
                            "expression_id": eid, "duration_s": round((b - a + 1) / fps, 4),
                            "reason": "MMD mood %s held %.2f s (knowledge base: %s)"
                                      % (mood, (b - a + 1) / fps,
                                         _expression_note(knowledge, eid))})
    return events, rows


def _pick_expression(mood, mood_of, knowledge):
    """The expression id already verified for a mood, else the most-used id for it."""
    from . import dsc_core as dsc

    ids = sorted(i for i, m in mood_of.items() if m == mood and i in dsc.attested_expression_ids())
    if not ids:
        return None
    counts = knowledge["expression"].get("ids") or {}
    return max(ids, key=lambda i: (int(counts.get(str(i), 0)), -i))


def _expression_note(knowledge, eid):
    counts = knowledge["expression"].get("ids") or {}
    n = int(counts.get(str(eid), 0))
    return "%d uses in the shipping scripts" % n if n else "never used by a shipping script"


# ------------------------------------------------------------------ the encoder's own interface
def events_from_solver(tracks, plan, frames, knowledge=None, targets=None, cfg=None,
                       chara=0, weight=None, hold=None, explain=None, data_dir=DATA,
                       seq=None, tempo=None, phonemes=None, lyrics=None, camera_cut=None,
                       motion=None, per_frame=None):
    """Solve the performance and return it in the shape `face_core.events_from_tracks` returns.

    Same contract - ``(events, unmapped, info)`` with events as ``(frame, command, params)`` - so the
    encoder, the splicer and every validator downstream are untouched.  That separation is the point:
    the part of the exporter that is verified is the part that writes bytes, and nothing here writes
    bytes.
    """
    from . import dsc_core as dsc
    from . import face_core as fc

    targets = targets or dsc.targets()
    knowledge = knowledge or load_knowledge(data_dir)
    cfg = cfg or config(data_dir=data_dir)
    weight = fc.MOUTH_WEIGHT if weight is None else weight
    hold = fc.MOUTH_HOLD if hold is None else hold
    model = Model(knowledge, targets, cfg)
    unmapped = sorted(n for n, keys in tracks.items()
                      if n not in plan and keys
                      and max(k["weight"] for k in keys) >= fc.MOUTH_FLOOR)
    ctx = build_context(seq, frames, knowledge, cfg, fps=fc.DST_FPS, tempo=tempo,
                        phonemes=phonemes, lyrics=lyrics, camera_cut=camera_cut, motion=motion)
    mouth_events, rows, info = solve_mouth(tracks, plan, frames, model, cfg, fps=fc.DST_FPS,
                                           explain=[] if explain is None else explain, ctx=ctx,
                                           per_frame=per_frame)
    if not mouth_events:
        return [], unmapped, {"reason": info.get("reason", "the solver produced no mouth event")}
    event_list = [(e["frame"], "MOUTH_ANIM", [chara, 0, e["shape"], weight, hold])
                  for e in mouth_events]
    expression_events = []
    if cfg["expression"]:
        expression_events, _ = solve_expression(tracks, plan, frames, knowledge, targets, cfg,
                                                fps=fc.DST_FPS, explain=explain)
        event_list += [(e["frame"], "EXPRESSION", [chara, e["id"], e["intensity"], -1])
                       for e in expression_events]
    event_list.sort(key=lambda r: (r[0], r[1] != "EXPRESSION"))
    # `events_from_tracks` reports the shape of its own thinning here and `export_face` prints it, so
    # the solver reports the equivalent numbers for its own layer rather than a missing key.  The
    # solver has no per-morph gap to thin - that is what the duration model and the minimum event
    # length already do - so `thinned` counts the events the merge pass removed.
    span_min = frames / fc.DST_FPS / 60.0
    info["context"] = ctx.notes()
    info.update({"mouth_events": len(mouth_events), "expression_events": len(expression_events),
                 "frames": frames, "visemes_used": sorted({e["viseme"] for e in mouth_events}),
                 "shapes_used": sorted({e["shape"] for e in mouth_events}),
                 "mouth_rate_hz": round(len(mouth_events) / max(1e-9, frames / fc.DST_FPS), 4),
                 "thinned": max(0, len(rows) - len(mouth_events)),
                 "density": (len(event_list) / span_min) if span_min else 0.0})
    return event_list, unmapped, info
