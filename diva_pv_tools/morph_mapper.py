"""Decide where each source morph goes, and be able to explain it.

The mapping is **effect first, name second**:

    source morph -> SourceMorphDefinition (what it deforms)
                 -> MorphEffectSignature   (measured, in interocular units)
                 -> MMD semantic category  (catalog + panel + geometry, with evidence)
                 -> DIVA capability        (measured slot table + corpus statistics)
                 -> target + parameters
                 -> MappingDecision        (why, with the score broken down)

Nothing here writes a number nobody can vouch for: a target the shipping corpus never cued is
offered only as an *unverified* suggestion and is never emitted unless the caller explicitly asks
for it, and a source morph that DIVA has no dimension for is reported `TARGET_UNSUPPORTED` rather
than bent onto a lookalike slot.

The five quality grades are an ordinal scale of *what was actually established*, not of how good
the mapping sounds:

    EXACT        same semantic category, same direction, and the measured signatures agree
    NEAR_EXACT   measured signatures agree closely on every weighted feature
    SEMANTIC     the semantics agree and the name is a shipped alias or an exact slot name
    GEOMETRIC    the semantics are unknown or unrelated but the measured shapes match
    APPROXIMATE  a target was chosen and the effect differs measurably; the error is reported

No Blender import.
"""
import os

from . import mmd_morph
from . import morph_effect as fx

# Grades, ordered from what was proven to what was assumed.
EXACT = "EXACT"
NEAR_EXACT = "NEAR_EXACT"
SEMANTIC = "SEMANTIC"
GEOMETRIC = "GEOMETRIC"
APPROXIMATE = "APPROXIMATE"
TARGET_UNSUPPORTED = "TARGET_UNSUPPORTED"
SOURCE_DEFINITION_UNAVAILABLE = "SOURCE_DEFINITION_UNAVAILABLE"
INVALID_SOURCE = "INVALID_SOURCE"

GRADE_ORDER = (EXACT, NEAR_EXACT, SEMANTIC, GEOMETRIC, APPROXIMATE, TARGET_UNSUPPORTED,
               SOURCE_DEFINITION_UNAVAILABLE, INVALID_SOURCE)

# How a target was chosen.  Reported next to the grade so "it scored best" and "a shipped table
# says so" are distinguishable at a glance.
METHOD_EXACT_NAME = "EXACT_NAME"
METHOD_ALIAS_TABLE = "ALIAS_TABLE"
METHOD_USER_ANSWER = "USER_ANSWER"
METHOD_SEMANTIC = "SEMANTIC_MAPPING"
METHOD_EFFECT = "EFFECT_MAPPING"
METHOD_COMPOSITE = "COMPOSITE_MAPPING"
METHOD_NONE = "NO_ROUTE"

# Weights of the mapping score.  Each is a deliberate statement about what matters, and the
# breakdown is printed for every decision, so a disputed mapping can be argued with numbers.
SCORE_WEIGHTS = {
    "semantic": 0.40,      # the source category against the target's semantic dimension
    "effect": 0.30,        # measured signature distance, when both sides were measured
    "name": 0.15,          # an explicit shipped table or an exact slot name
    "usage": 0.10,         # prefer the target SEGA's own charts actually send
    "attested": 0.05,      # prefer a target a shipping script is known to cue
}
# A measured signature distance at or below this is treated as agreement; above it, the mapping is
# an approximation and the distance is reported.
EFFECT_AGREE = 0.35
EFFECT_NEAR = 0.15


class MappingDecision(object):
    """One source morph's whole story: what it was, what it can be, how well, and why."""

    __slots__ = ("name", "norm", "stem", "source_definition", "signature", "classification",
                 "evaluation", "grade", "method", "target", "target_kind", "target_slot",
                 "target_idx", "confidence", "score", "breakdown", "effect_distance",
                 "effect_features", "notes", "reason", "source_keys", "blocked_by")

    def __init__(self, name, norm="", stem=""):
        self.name = name
        self.norm = norm
        self.stem = stem
        self.source_definition = None
        self.signature = None
        self.classification = None
        self.evaluation = None
        self.grade = INVALID_SOURCE
        self.method = METHOD_NONE
        self.target = None
        self.target_kind = None
        self.target_slot = None
        self.target_idx = None
        self.confidence = 0.0
        self.score = 0.0
        self.breakdown = {}
        self.effect_distance = None
        self.effect_features = {}
        self.notes = []
        self.reason = ""
        self.source_keys = 0
        self.blocked_by = None

    @property
    def mapped(self):
        return self.target is not None and self.grade not in (
            TARGET_UNSUPPORTED, SOURCE_DEFINITION_UNAVAILABLE, INVALID_SOURCE)

    @property
    def emits(self):
        """Whether the exporter may write this mapping without a human decision."""
        return self.mapped and self.grade in (EXACT, NEAR_EXACT, SEMANTIC, GEOMETRIC)

    def describe(self):
        return {
            "source_name": self.name, "normalized_name": self.norm, "stem": self.stem,
            "grade": self.grade, "method": self.method, "target": self.target,
            "target_kind": self.target_kind, "target_slot": self.target_slot,
            "target_idx": self.target_idx, "confidence": round(self.confidence, 4),
            "score": round(self.score, 4), "breakdown": {k: round(v, 4)
                                                         for k, v in self.breakdown.items()},
            "effect_distance": (None if self.effect_distance is None
                                else round(self.effect_distance, 5)),
            "effect_features": self.effect_features,
            "semantic": self.classification.describe() if self.classification else None,
            "source_morph": (self.source_definition.describe() if self.source_definition
                             else None),
            "effect_signature": (self.signature.describe() if self.signature else None),
            "group_evaluation": self.evaluation.describe() if self.evaluation else None,
            "source_keyframes": self.source_keys, "notes": list(self.notes),
            "reason": self.reason, "blocked_by": self.blocked_by,
        }


def _semantic_score(classification, capability, model):
    """0..1 agreement between a source category and a target capability."""
    if classification is None or capability is None:
        return 0.0
    dimension, weights = model["dimension_for"](classification.category)
    if dimension is None:
        return 0.0
    if dimension != capability.dimension:
        return 0.0
    if dimension in ("gaze", "eyebrow"):
        # these two blocks carry a DIRECTION as well as a block, and the direction is what the
        # source name states; a direction mismatch must sink the candidate, not merely weaken it
        want = model["direction_prior_for"](classification.name_hit or "", classification.category)
        if want is None:
            return 0.0 if capability.label == "RESET" else 0.25
        if capability.label == want:
            return 1.0
        if capability.label.startswith(want) or want.startswith(capability.label):
            return 0.6
        if capability.label.startswith("MOVE_") and want[:1] in capability.label:
            return 0.35
        return 0.0
    if capability.label in weights:
        return float(weights[capability.label])
    # the emotion family decides its mood from the name's own wording
    mood = model["mood_prior_for"](classification.name_hit or classification.category,
                                  classification.category)
    if mood and capability.label == mood:
        return 1.0
    return 0.0


def _name_score(classification, capability, alias_target):
    if alias_target and alias_target == capability.name:
        return 1.0
    if classification is None:
        return 0.0
    if classification.name_hit and mmd_morph.normalize(capability.name) == classification.name_hit:
        return 1.0
    return 0.0


def _usage_score(capability, usage_max):
    if not capability.usage or not usage_max:
        return 0.0
    return min(1.0, float(capability.usage) / float(usage_max))


class Mapper(object):
    """Maps a set of source morphs onto a capability model.

    `definitions` are `MorphDefinition`s (from `source_model`), `signatures` the measured
    `MorphEffectSignature`s, `model` the `CapabilityModel`, `alias` an optional
    ``{normalised_name: capability_name}`` from the shipped tables or the user's worklist.
    """

    def __init__(self, model, definitions=None, signatures=None, alias=None, user=None,
                 allow_unattested=False, target_signatures=None):
        self.model = model
        self.definitions = dict(definitions or {})
        self.signatures = dict(signatures or {})
        self.alias = dict(alias or {})
        self.user = dict(user or {})
        self.allow_unattested = bool(allow_unattested)
        self.target_signatures = dict(target_signatures or {})
        caps = [c for c in model.capabilities if c.dimension]
        self._by_name = {c.name: c for c in model.capabilities}
        self._candidate_pool = caps
        self._usage_max = max([c.usage or 0.0 for c in model.capabilities] or [1.0]) or 1.0
        # a small adapter so `_semantic_score` can reach the module-level helpers without a
        # circular import between the effect model and the capability model
        self._model_api = {"dimension_for": _dimension_for,
                           "mood_prior_for": _mood_prior,
                           "direction_prior_for": _direction_prior}

    # ------------------------------------------------------------------ the per-morph decision
    def _lookup(self, mapping, name):
        """A definition/signature lookup that accepts either the exact name or its normalised key.

        Both spellings turn up: a PMX definition is keyed by the model's own name, a VMD track by
        the motion's, and the two differ in width (`ｳｨﾝｸ２右` vs `ウィンク2右`) often enough that
        requiring the caller to normalise every key would be a trap.
        """
        if name in mapping:
            return mapping[name]
        key = mmd_morph.normalize(name)
        for k, v in mapping.items():
            if mmd_morph.normalize(k) == key:
                return v
        return None

    def decide(self, name, keyframes=0):
        from . import diva_capability
        d = MappingDecision(name, mmd_morph.normalize(name), mmd_morph.normalize_stem(name))
        d.source_keys = keyframes
        definition = self._lookup(self.definitions, name)
        sig = self._lookup(self.signatures, name)
        d.source_definition = definition
        d.signature = sig

        if not name:
            d.grade = INVALID_SOURCE
            d.reason = ("the VMD record's name decoded to nothing - MMD overwrites the first byte "
                        "of a name it cannot encode in Shift-JIS, and that byte is what makes the "
                        "rest of the name readable, so this morph exists in the motion and cannot "
                        "be attributed")
            return d

        geometry = sig.describe() if sig else None
        d.classification = mmd_morph.classify(name, panel=(definition.panel if definition else None),
                                             geometry=geometry)

        if definition is not None and definition.group_children:
            d.evaluation = fx.evaluate(self.definitions, name, 1.0)

        # 1. an explicit answer from the human outranks everything, exactly as in `morph_core`
        user_target = self.user.get(d.norm) or self.user.get(d.stem)
        alias_target = user_target or self.alias.get(d.norm) or self.alias.get(d.stem)

        # 2. what DIVA can do with this category at all
        dimension, _weights = diva_capability.dimension_for(d.classification.category)
        if dimension in ("drop", "report"):
            d.grade = TARGET_UNSUPPORTED
            d.method = METHOD_NONE
            d.reason = ("DIVA has no face dimension for a %s morph; it is reported, not mapped "
                        "(a lookalike slot would be a wrong face, not an approximation)"
                        % d.classification.category)
            return d
        if dimension == "lane":
            # The eyelid lane is not an expression slot and must never become one: an expression
            # is a state the engine holds, so a wink sent as `MIK_FACE_WINK_OLD` leaves the eye
            # shut until the next cue.  Since 3.4.0 the eyelid is not written through `LOOK_ANIM`
            # either - the exporter drives it with EXPRESSION 22/21 (or hands it to the engine's
            # AUTO_BLINK), and a wink, which that face command cannot express per-eye, is reported
            # rather than emitted.  The lane record below is the audit's account of where the morph
            # belongs, not a promise about the bytes.
            lane = {"eyelid_blink": "both", "eyelid_wink_left": "left",
                    "eyelid_wink_right": "right"}[d.classification.category]
            d.grade = SEMANTIC
            d.method = METHOD_SEMANTIC
            d.target = "LOOK_ANIM"
            d.target_kind = "lane"
            d.target_slot = 12 if lane == "left" else (13 if lane == "right" else None)
            d.target_idx = None
            d.confidence = 0.95
            d.reason = ("the eyelid lane, not an expression: LOOK_ANIM channel %s drives the %s "
                        "eyelid, and each shut line is closed by an open cue so the eye can never "
                        "be left shut" % (d.target_slot if d.target_slot else "12 and 13",
                                          "both" if lane == "both" else lane))
            d.notes.append("the eyelid follows the dance's まばたき curve rather than the base "
                           "script's own blink for this performer")
            return d
        if dimension is None and alias_target is None:
            d.grade = (SOURCE_DEFINITION_UNAVAILABLE if definition is None else TARGET_UNSUPPORTED)
            d.method = METHOD_NONE
            d.reason = ("the name matches no catalog entry and no shipped table, so neither its "
                        "semantics nor its effect can be pointed at a DIVA target")
            if definition is None:
                d.reason += " (and no source definition was supplied, so its geometry is unknown)"
            d.blocked_by = "SOURCE_DEFINITION_UNAVAILABLE" if definition is None else "SEMANTICS"
            return d

        # 3. rank the candidates
        best = self._rank(d, alias_target)
        if best is None:
            d.grade = TARGET_UNSUPPORTED
            d.method = METHOD_NONE
            d.reason = ("no capability matches: the category is %s, whose DIVA dimension is %s, "
                        "and no candidate reached a usable score"
                        % (d.classification.category, dimension))
            return d
        cap, breakdown, score = best
        d.target = cap.name
        d.target_kind = cap.kind
        d.target_slot = cap.slot
        d.target_idx = cap.idx
        d.breakdown = breakdown
        d.score = score
        d.confidence = min(0.99, score * (1.0 if cap.attested else 0.6))
        if not cap.attested:
            d.notes.append("this target exists in the game's own table but no shipping script "
                           "cues it, so writing it is an experiment; it is emitted only when "
                           "'allow unattested targets' is on")
        # The effect comparison must happen BEFORE the grade is decided: a measured agreement is
        # the strongest evidence there is, and computing it afterwards silently demotes every
        # proven mapping to the naming tier.  (That ordering bug was caught by the unit tests.)
        if sig is not None and cap.effect is not None:
            cmp = fx.compare(sig, cap.effect)
            d.effect_distance = cmp.get("distance")
            d.effect_features = cmp.get("top") or []
        d.grade, d.method, d.reason = self._grade(d, cap, alias_target, user_target, breakdown)
        return d

    def _candidates(self, classification, alias_target):
        from . import diva_capability
        out = []
        if alias_target:
            cap = self._by_name.get(alias_target)
            if cap is not None:
                out.append(cap)
        dimension, _weights = diva_capability.dimension_for(classification.category)
        if dimension in ("viseme", "mood"):
            for cap in self._candidate_pool:
                if cap.dimension == dimension:
                    out.append(cap)
        elif dimension in ("gaze", "eyebrow"):
            for cap in self._candidate_pool:
                if cap.dimension == dimension:
                    out.append(cap)
        elif dimension == "lane":
            out.append(None)                     # the eyelid lane is not a capability
        seen = set()
        uniq = []
        for cap in out:
            if cap is None or cap.name in seen:
                continue
            seen.add(cap.name)
            uniq.append(cap)
        return uniq

    def _rank(self, decision, alias_target):
        from . import diva_capability
        if diva_capability.dimension_for(decision.classification.category)[0] == "lane":
            return None                          # handled by the caller's eyelid lane
        best = None
        for cap in self._candidates(decision.classification, alias_target):
            sem = _semantic_score(decision.classification, cap, self._model_api)
            nm = _name_score(decision.classification, cap, alias_target)
            use = _usage_score(cap, self._usage_max)
            att = 1.0 if cap.attested else 0.0
            eff = None
            if decision.signature is not None and cap.effect is not None:
                cmp = fx.compare(decision.signature, cap.effect)
                dist = cmp.get("distance")
                if dist is not None:
                    eff = max(0.0, 1.0 - min(1.0, dist / 2.0))
            if sem <= 0.0 and nm <= 0.0 and (eff is None or eff < 0.5):
                continue
            breakdown = {"semantic": SCORE_WEIGHTS["semantic"] * sem,
                         "name": SCORE_WEIGHTS["name"] * nm,
                         "usage": SCORE_WEIGHTS["usage"] * use,
                         "attested": SCORE_WEIGHTS["attested"] * att}
            if eff is not None:
                breakdown["effect"] = SCORE_WEIGHTS["effect"] * eff
            score = sum(breakdown.values())
            # weight the breakdown so the reported numbers add to the score
            total_w = sum(SCORE_WEIGHTS[k] for k in breakdown)
            score = score / total_w if total_w else 0.0
            if not cap.attested and not self.allow_unattested:
                # still rank it so the report can name the near miss, but a verified target wins
                score *= 0.35
            breakdown["total"] = score
            if best is None or score > best[2]:
                best = (cap, breakdown, score)
        return best

    def _grade(self, decision, cap, alias_target, user_target, breakdown):
        """The grade, ordered so the strongest *available* evidence decides.

        Measured agreement outranks name agreement: when both sides have a real signature and they
        match, the mapping is proven even if the naming tier would have called it merely semantic.
        The `name` breakdown term is non-zero only for an exact asset-name match, which is what
        keeps a hand-written alias from being called EXACT on that basis alone.
        """
        sem = breakdown.get("semantic", 0.0)
        nm = breakdown.get("name", 0.0)
        if user_target and cap.name == user_target:
            return SEMANTIC, METHOD_USER_ANSWER, "your worklist answer"
        if decision.effect_distance is not None:
            if decision.effect_distance <= EFFECT_NEAR and sem > 0.0:
                return (EXACT, METHOD_EFFECT,
                        "the measured source signature and the target's own measured signature "
                        "agree to within %.3f, and the semantics match" % EFFECT_NEAR)
            if decision.effect_distance <= EFFECT_AGREE:
                return (NEAR_EXACT, METHOD_EFFECT,
                        "measured signatures agree to %.3f" % decision.effect_distance)
            if sem <= 0.0:
                return (GEOMETRIC, METHOD_EFFECT,
                        "the semantics are unrelated, but the measured shapes are the closest "
                        "available (distance %.3f)" % decision.effect_distance)
            return (APPROXIMATE, METHOD_EFFECT,
                    "the semantics match but the measured effect differs by %.3f - this is an "
                    "approximation and the error is reported" % decision.effect_distance)
        if alias_target and cap.name == alias_target:
            return SEMANTIC, METHOD_ALIAS_TABLE, "a shipped alias table names this target"
        if sem > 0.0 and nm > 0.0:
            return (SEMANTIC, METHOD_SEMANTIC,
                    "the catalog's category and the target's own asset name agree")
        if sem > 0.0:
            grade = EXACT if decision.classification.source == mmd_morph.SOURCE_CORROBORATED \
                else SEMANTIC
            return (grade, METHOD_SEMANTIC,
                    "the catalog places this morph in %s and the target is DIVA's %s dimension"
                    % (decision.classification.category, cap.dimension))
        if nm > 0.0:
            return (SEMANTIC, METHOD_ALIAS_TABLE, "the target is named by a shipped table")
        return (APPROXIMATE, METHOD_SEMANTIC,
                "a target was chosen on a weak signal; the measured effect is unknown on one "
                "side, so the error cannot be quantified")

    # ------------------------------------------------------------------------------ the whole set
    def run(self, keyframe_counts=None):
        """One decision per distinct source morph name, never two.

        The name set is the union of the definitions and the VMD's own tracks, and it is walked
        once: a morph that has a definition but no animation still gets a decision (so the audit
        can say it was not animated rather than omit it), and a morph the VMD animates with no
        definition gets one too (so it can be reported as `SOURCE_DEFINITION_UNAVAILABLE`).
        """
        counts = dict(keyframe_counts or {})
        names = set(self.definitions or {}) | set(counts)
        decisions = []
        for name in sorted(names, key=lambda n: (-counts.get(n, 0), str(n))):
            decisions.append(self.decide(name, counts.get(name, 0)))
        return decisions


def _dimension_for(category):
    from . import diva_capability
    return diva_capability.dimension_for(category)


def _mood_prior(name, category):
    from . import diva_capability
    return diva_capability.mood_prior_for(name, category)


def _direction_prior(name, category):
    from . import diva_capability
    return diva_capability.direction_prior_for(name, category)


# ------------------------------------------------------------------------------ coverage report
def coverage(decisions, lane_decisions=None):
    """The counts every export must produce, with silent drops provably at zero.

    `lane_decisions` are the morphs that go somewhere other than an expression slot (the eyelid
    lane, the gaze lane); they are counted, not folded into "mapped", because conflating the two
    is how a wink once shipped as a held expression.
    """
    by_grade = {g: 0 for g in GRADE_ORDER}
    by_method = {}
    unmapped = []
    total_keys = 0
    mapped_keys = 0
    for d in decisions:
        by_grade[d.grade] = by_grade.get(d.grade, 0) + 1
        by_method[d.method] = by_method.get(d.method, 0) + 1
        total_keys += d.source_keys
        if d.mapped:
            mapped_keys += d.source_keys
        else:
            unmapped.append(d)
    return {
        "source_morphs": len(decisions),
        "source_keyframes": total_keys,
        "mapped_morphs": sum(by_grade[g] for g in (EXACT, NEAR_EXACT, SEMANTIC, GEOMETRIC)),
        "mapped_keyframes": mapped_keys,
        "exact": by_grade[EXACT], "near_exact": by_grade[NEAR_EXACT],
        "semantic": by_grade[SEMANTIC], "geometric": by_grade[GEOMETRIC],
        "approximate": by_grade[APPROXIMATE],
        "target_unsupported": by_grade[TARGET_UNSUPPORTED],
        "source_definition_unavailable": by_grade[SOURCE_DEFINITION_UNAVAILABLE],
        "invalid_source": by_grade[INVALID_SOURCE],
        "route": dict(sorted(by_method.items())),
        "lane_morphs": len(lane_decisions or []),
        "dropped_morphs": sum(by_grade[g] for g in (TARGET_UNSUPPORTED,
                                                    SOURCE_DEFINITION_UNAVAILABLE, INVALID_SOURCE)),
        "dropped_keyframes": total_keys - mapped_keys,
        "silent_drops": 0,          # every unmapped decision carries a reason and a grade
        "unmapped": [d.describe() for d in unmapped],
    }


def summarise(decisions):
    cov = coverage(decisions)
    lines = ["%d source morph(s), %d keyframe(s)" % (cov["source_morphs"], cov["source_keyframes"]),
             "mapped %d (%d keyframes): exact %d, near-exact %d, semantic %d, geometric %d"
             % (cov["mapped_morphs"], cov["mapped_keyframes"], cov["exact"], cov["near_exact"],
                cov["semantic"], cov["geometric"]),
             "approximate %d, unsupported target %d, no source definition %d, invalid source %d"
             % (cov["approximate"], cov["target_unsupported"],
                cov["source_definition_unavailable"], cov["invalid_source"]),
             "silent drops: %d" % cov["silent_drops"]]
    for d in decisions:
        if not d.mapped:
            lines.append("  %-24r %-30s %s" % (d.name, d.grade, d.reason))
    return "\n".join(lines)
