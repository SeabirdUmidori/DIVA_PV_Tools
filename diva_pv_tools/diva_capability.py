"""What the DIVA face can actually express: a capability per slot, with its measured range.

`morph_core` answers "what slot name do I write"; this module answers "what can that slot DO, over
what range of parameters, and with what evidence".  The distinction matters because a mapping is
only as honest as the target's known capability: `MIK_FACE_SMILE` looks like the obvious home for
`にっこり` and no shipping script ever sends it, so it is not a capability - it is a name.

Every field here is measured, and the measurement is named:

  * ``slot`` / ``idx`` / ``anim``  - the game's own slot table (`expression_slots.json` or the
    rom's ``rob_mot_tbl.bin`` + ``mot_db.farc``, whichever `morph_core` resolved).
  * ``attested`` - an exact framing of a SEGA shipping script contains this value.  An
    unattested slot is a capability the engine may or may not honour, and `effects_from_game`
    refuses to emit it, exactly as `morph_core` already does.
  * ``mood`` / ``viseme`` - the semantic dimension, derived from SEGA's own asset name by
    stripping the dialect qualifiers (``_OLD``, ``_CL``, ...); see `face_retarget`'s tables.
  * ``weight_range`` / ``hold_values`` - the value sets the shipping corpus actually contains.
  * ``usage`` - how often shipping scripts send it, which is the tie-break when two slots are
    semantically equal.
  * ``effect`` - an optional `MorphEffectSignature` for the slot, present only when the character
    model's own morph definitions could be resolved (see `morph_effect`).  Absent means
    "capability known from the script corpus, visual effect NOT measured" and every report says so.

No Blender import.
"""
import json
import os

from . import dsc_core as dsc
from . import morph_core

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE = os.path.join(HERE, "data")

# The viseme / mood normalisation `face_retarget` uses, kept in one place so the capability model
# and the solver cannot drift apart.
VISEME_QUALIFIERS = ("_OLD_CL", "_OLD", "_CL", "_DOWN", "_S", "_L")
VISEME_ALIASES = {
    "A": "A", "I": "I", "U": "U", "E": "E", "O": "O",
    "RESET": "CLOSED", "NEUTRAL": "CLOSED",
    "SMILE": "SMILE", "NIYA": "SMIRK", "NIYARI": "SMIRK",
    "CHU": "POUT", "SURPRISE": "OPEN", "SAKEBI": "OPEN",
    "HE": "LAUGH_OPEN", "HAMISE": "TEETH", "NEKO": "CAT", "MOGUMOGU": "CHEW",
    "HERAHERA": "SLACK", "SANKAKU": "TRIANGLE", "SHIKAKU": "SQUARE",
}
MOOD_QUALIFIERS = ("_OLD_CL", "_OLD", "_CL")
MOOD_ALIASES = {
    "RESET": "NEUTRAL", "CLOSE": "NEUTRAL",
    "SAD": "SAD", "LAUGH": "HAPPY", "SURPRISE": "SURPRISE", "WINK": "WINK",
    "SETTLED": "CALM", "DAZZLING": "SHY", "LASCIVIOUS": "ALLURE", "STRONG": "ANGRY",
    "CLARIFYING": "PUZZLED", "GENTLE": "SOFT", "CRY": "CRY", "NAGASI": "GAZE",
    "KIRI": "PROUD", "UTURO": "DAZE",
}

# Which MMD semantic families each DIVA dimension can plausibly stand in for.  This is the bridge
# between the source vocabulary (`mmd_morph` categories) and the target vocabulary, and every
# entry is a *claim about meaning*, deliberately kept separate from the numbers.
MMD_TO_DIVA = {
    # mpseech / mouth
    "mouth_phoneme": ("viseme", {"A": 1.0, "I": 1.0, "U": 1.0, "E": 1.0, "O": 1.0}),
    "mouth_close": ("viseme", {"CLOSED": 1.0}),
    "mouth_open": ("viseme", {"OPEN": 0.8, "LAUGH_OPEN": 0.5}),
    "mouth_smile": ("viseme", {"SMILE": 1.0}),
    "mouth_frown": ("viseme", {"TRIANGLE": 0.4}),
    "mouth_smirk": ("viseme", {"SMIRK": 1.0}),
    "mouth_pucker": ("viseme", {"POUT": 1.0}),
    "mouth_wide": ("viseme", {}),             # no measured DIVA dimension: reported unsupported
    "mouth_shape_special": ("viseme", {"CAT": 0.7, "SQUARE": 0.5, "TRIANGLE": 0.5, "SMIRK": 0.3}),
    "mouth_teeth": ("viseme", {"TEETH": 0.7}),
    "tongue": ("viseme", {}),                 # DIVA carries no tongue dimension we can attest
    # mood
    "emotion": ("mood", {}),                  # decided by the name, see mood_priors below
    "eye_smile": ("mood", {"HAPPY": 0.9, "SOFT": 0.3}),
    "eye_narrow": ("mood", {"SOFT": 0.6, "DAZE": 0.5, "CALM": 0.3}),
    "eye_wide": ("mood", {"SURPRISE": 0.9}),
    "eye_shape": ("mood", {"PROUD": 0.6, "SOFT": 0.4}),
    "brow_up": ("mood", {"SURPRISE": 0.8}),
    "brow_down": ("mood", {"SAD": 0.8}),
    "brow_angry": ("mood", {"ANGRY": 1.0, "PROUD": 0.3}),
    "brow_sad": ("mood", {"SAD": 0.9, "CRY": 0.5, "PUZZLED": 0.3}),
    "brow_smile": ("mood", {"HAPPY": 0.7}),
    "brow_neutral": ("mood", {"NEUTRAL": 1.0}),
    "cheek_blush": ("mood", {"SHY": 0.9}),
    "tears": ("mood", {"CRY": 0.9, "GAZE": 0.3}),
    "sweat": ("mood", {"PUZZLED": 0.7}),
    "face_neutral": ("mood", {"NEUTRAL": 1.0}),
    # things DIVA has no measured dimension for: reported, never invented
    "eye_pupil_small": ("none", {}),
    "eye_pupil_large": ("none", {}),
    "eye_pupil_special": ("none", {}),
    "eye_highlight": ("none", {}),
    # A gaze morph's target is the game's own EYES_* block (slots 0xA6..0xBF: UP, DOWN, LEFT,
    # RIGHT, the four diagonals and the U_D / L_R / UL_DR / UR_DL sweeps).  Those slots are real
    # and named in the table, and they are exactly what an MMD 上/下/左/右/目線 morph means - but
    # **no shipping script cues one**, and no command in the 1097-file corpus takes a slot index
    # from that block, so how the engine is asked for them is not established.  The capability is
    # therefore modelled (with the direction labels so a mapping can be ranked and explained) and
    # marked unattested, which is what stops the exporter from emitting it.  The weights say a
    # direction morph prefers its own direction and dislikes the opposite one; the sweeps are
    # partial matches for the directions they pass through.
    "gaze": ("gaze", {
        "UP": 1.0, "DOWN": 1.0, "LEFT": 1.0, "RIGHT": 1.0,
        "UP_LEFT": 1.0, "UP_RIGHT": 1.0, "DOWN_LEFT": 1.0, "DOWN_RIGHT": 1.0,
        "MOVE_U_D": 0.5, "MOVE_L_R": 0.5, "MOVE_UL_DR": 0.4, "MOVE_UR_DL": 0.4,
        "RESET": 0.6,
    }),
    # The eyebrow block only ever carries "up" (per side), so a raise *could* point there - but
    # `MIK_FACE_EYEBROW_UP_LEFT/RIGHT` is a *unilateral* raise, while MMD's 眉上げ/上 raises both,
    # and no shipping script cues either slot.  A bilateral raise therefore goes through the mood
    # dimension (which SEGA does cue) and the per-side block stays available for an explicit user
    # answer; putting a both-brows morph on a one-brow slot would be a visible, wrong asymmetry.
    "brow_up": ("mood", {"SURPRISE": 0.8}),
    "brow_angry": ("mood", {"ANGRY": 1.0, "PROUD": 0.3}),
    "brow_smile": ("mood", {"HAPPY": 0.7}),
    "eyelid_blink": ("lane", {"LOOK_ANIM": 1.0}),
    "eyelid_wink_left": ("lane", {"LOOK_ANIM": 1.0}),
    "eyelid_wink_right": ("lane", {"LOOK_ANIM": 1.0}),
    "accessory": ("drop", {}),
    "plumbing": ("drop", {}),
    "body": ("drop", {}),
    "unknown": ("report", {}),
}

# Direction labels for the EYES_* block, keyed by the suffix the asset name carries.  A gaze
# morph's *direction* is the one thing its name states unambiguously, so this is a name-level
# mapping rather than a guess about geometry.
GAZE_LABELS = {
    "RESET": "RESET", "UP": "UP", "DOWN": "DOWN", "LEFT": "LEFT", "RIGHT": "RIGHT",
    "UP_LEFT": "UP_LEFT", "UP_RIGHT": "UP_RIGHT", "DOWN_LEFT": "DOWN_LEFT",
    "DOWN_RIGHT": "DOWN_RIGHT", "MOVE_U_D": "MOVE_U_D", "MOVE_L_R": "MOVE_L_R",
    "MOVE_UL_DR": "MOVE_UL_DR", "MOVE_UR_DL": "MOVE_UR_DL",
}

# Direction labels for the eyebrow block (0xEC..0xEF): per side, raised.
EYEBROW_LABELS = {"FACE_EYEBROW_UP_LEFT": "UP_LEFT", "FACE_EYEBROW_UP_RIGHT": "UP_RIGHT"}

# A mood name for the MMD `emotion` family, by the fine-grained category the catalog detected.
# Kept as data so a caller can see exactly which MMD names reach which DIVA mood.
EMOTION_MOOD = {
    "笑": "HAPPY", "喜": "HAPPY", "うれし": "HAPPY", "嬉": "HAPPY", "楽": "HAPPY", "はっぴ": "HAPPY",
    "ハッピ": "HAPPY", "happy": "HAPPY", "smile": "HAPPY", "laugh": "HAPPY", "joy": "HAPPY",
    "fun": "HAPPY", "grin": "HAPPY", "元気": "HAPPY", "げんき": "HAPPY", "genki": "HAPPY",
    "cheerful": "HAPPY",
    "悲": "SAD", "かなし": "SAD", "さみし": "SAD", "寂": "SAD", "切な": "SAD", "せつな": "SAD",
    "つら": "SAD", "辛": "SAD", "苦": "SAD", "くるし": "SAD", "メソメソ": "SAD", "sad": "SAD",
    "sorrow": "SAD", "unhappy": "SAD", "pain": "SAD", "hurt": "SAD", "worried": "SAD",
    "びっくり": "SURPRISE", "ビックリ": "SURPRISE", "驚": "SURPRISE", "おどろき": "SURPRISE",
    "仰天": "SURPRISE", "はっ": "SURPRISE", "ハッ": "SURPRISE", "surprise": "SURPRISE",
    "shock": "SURPRISE", "wow": "SURPRISE",
    "なごみ": "CALM", "和み": "CALM", "なごむ": "CALM", "落ち着": "CALM", "穏": "CALM",
    "ほんわか": "CALM", "リラックス": "CALM", "calm": "CALM", "gentle": "CALM", "soft": "CALM",
    "relaxed": "CALM",
    "うっとり": "DAZE", "ウットリ": "DAZE", "陶酔": "DAZE", "見とれ": "DAZE", "見惚": "DAZE",
    "ぼんやり": "GAZE", "放心": "GAZE", "遠い目": "GAZE", "遠目": "GAZE", "blank": "GAZE",
    "だいすき": "ALLURE", "大好き": "ALLURE", "好き": "ALLURE", "ラブ": "ALLURE",
    "メロメロ": "ALLURE", "色気": "ALLURE", "love": "ALLURE", "amorous": "ALLURE",
    "得意": "PROUD", "どや": "PROUD", "ドヤ": "PROUD", "自慢": "PROUD", "勝ち誇": "PROUD",
    "confident": "PROUD", "proud": "PROUD",
    "悩": "PUZZLED", "考": "PUZZLED", "うーん": "PUZZLED", "迷": "PUZZLED", "とまど": "PUZZLED",
    "困惑": "PUZZLED", "puzzled": "PUZZLED", "thinking": "PUZZLED", "question": "PUZZLED",
    "はぅ": "PUZZLED", "はう": "PUZZLED", "hau": "PUZZLED", "キョトン": "PUZZLED",
    "きょとん": "PUZZLED",
    "怒": "ANGRY", "いかり": "ANGRY", "おこ": "ANGRY", "ムカ": "ANGRY", "むか": "ANGRY",
    "プンプン": "ANGRY", "激怒": "ANGRY", "強気": "ANGRY", "闘志": "ANGRY", "いらいら": "ANGRY",
    "angry": "ANGRY", "anger": "ANGRY", "mad": "ANGRY", "furious": "ANGRY",
    "やる気": "ANGRY", "やるき": "ANGRY", "真面目": "NEUTRAL", "真剣": "NEUTRAL",
    "serious": "NEUTRAL",
}


def _strip(name, qualifiers):
    out = name
    for q in qualifiers:
        if out.endswith(q):
            return out[:-len(q)]
    return out


def load_knowledge(data_dir=KNOWLEDGE):
    """The measured corpus statistics, or {} when a file is missing (capabilities degrade)."""
    out = {}
    for key, name in (("mouth", "mouth_prototypes.json"),
                      ("expression", "expression_prototypes.json"),
                      ("motion", "motion_expression.json")):
        path = os.path.join(data_dir, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                out[key] = json.load(handle)
    return out


class Capability(object):
    """One target: what it is, what it means, over what range, and how well it is attested."""

    __slots__ = ("name", "slot", "idx", "anim", "kind", "attested", "viseme", "mood",
                 "weight_range", "hold_values", "usage", "effect", "notes", "label_override")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.notes = list(kw.get("notes") or [])

    @property
    def dimension(self):
        if self.label_override:
            return "gaze" if self.kind == "gaze" else ("eyebrow" if self.kind == "eyebrow" else None)
        return ("viseme" if self.kind == "mouth" else "mood") if self.viseme or self.mood else None

    @property
    def label(self):
        return self.label_override or self.viseme or self.mood or ""

    def describe(self):
        return {"name": self.name, "slot": self.slot, "idx": self.idx, "anim": self.anim,
                "kind": self.kind, "attested": bool(self.attested), "dimension": self.dimension,
                "label": self.label, "weight_range": self.weight_range,
                "hold_values": self.hold_values, "usage": self.usage,
                "effect_measured": self.effect is not None, "notes": list(self.notes)}


class CapabilityModel(object):
    """Every target the game was measured to accept, plus what is known about each.

    `unsupported` lists MMD semantic categories for which no measured DIVA dimension exists.  It
    is part of the model on purpose: "DIVA has no tongue dimension we can attest" is a result,
    and a mapper that cannot ask the question ends up inventing an answer.
    """

    def __init__(self, capabilities, unsupported, source, warnings=None):
        self.capabilities = capabilities
        self.unsupported = unsupported
        self.source = source
        self.warnings = list(warnings or [])
        self._by_name = {c.name: c for c in capabilities}

    def by_name(self, name):
        return self._by_name.get(name)

    def by_label(self, dimension, label, attested_only=True):
        """Capabilities carrying `label` on `dimension` (viseme / mood / gaze / eyebrow).

        Ranked by measured usage, because when two slots mean the same thing the one SEGA's own
        charts actually send is the one to prefer - and an attested-only query returns nothing for
        the feature blocks, which is the honest answer, not a bug.
        """
        out = [c for c in self.capabilities
               if c.dimension == dimension and c.label == label
               and (c.attested or not attested_only)]
        out.sort(key=lambda c: (-(c.usage or 0), c.slot))
        return out

    def of_kind(self, kind):
        return [c for c in self.capabilities if c.kind == kind]

    def describe(self):
        return {"source": self.source, "capabilities": len(self.capabilities),
                "attested": sum(1 for c in self.capabilities if c.attested),
                "with_measured_effect": sum(1 for c in self.capabilities
                                            if c.effect is not None),
                "mouth": sum(1 for c in self.capabilities if c.kind == "mouth"),
                "expression": sum(1 for c in self.capabilities if c.kind == "expression"),
                "unsupported": sorted(self.unsupported), "warnings": list(self.warnings)}


def _usage_counts(knowledge, kind):
    """Observed usage per shape index / expression id, from the corpus knowledge base.

    `mouth_prototypes.json` stores it as a `states` list of ``{id, events, share, ...}``;
    `expression_prototypes.json` stores it as an ``ids`` dict keyed by the id.  Both are read
    here, and a missing file yields an empty map rather than a fabricated default.
    """
    counts = {}
    blob = knowledge.get(kind) or {}
    for row in blob.get("states") or []:
        try:
            counts[int(row["id"])] = float(row.get("share", row.get("events", 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
    ids = blob.get("ids")
    if isinstance(ids, dict):
        for key, val in ids.items():
            try:
                if isinstance(val, dict):
                    counts[int(key)] = float(val.get("share", val.get("events", 0.0)))
                else:
                    counts[int(key)] = float(val)
            except (TypeError, ValueError):
                continue
    return counts


def _ranges(knowledge, kind):
    blob = knowledge.get(kind) or {}
    return blob.get("value_ranges") or {}


def build(game=None, knowledge=None, effects=None):
    """Assemble the capability model from whatever sources are available.

    `game` is `morph_core.game_names()` (default: resolve it), `knowledge` the corpus statistics,
    `effects` an optional {name: MorphEffectSignature} for the *character model* - when present a
    capability's `effect` is filled and the mapper can compare real deformation instead of names.
    """
    warnings = []
    if game is None:
        game = morph_core.game_names()
    if knowledge is None:
        knowledge = load_knowledge()
        if not knowledge:
            warnings.append("the corpus knowledge base is missing: parameter ranges and usage "
                            "counts are unavailable, so slots cannot be ranked by usage")
    mouth_ok = dsc.attested_mouth_shapes()
    expr_ok = dsc.attested_expression_ids()
    targets = dsc.targets()
    shape_names = {int(k): v for k, v in (targets.get("mouth_shape_names") or {}).items()}
    expr_names = {int(k): v for k, v in (targets.get("expression_id_names") or {}).items()}

    shape_usage = _usage_counts(knowledge, "mouth")
    expr_usage = _usage_counts(knowledge, "expression")
    shape_range = _ranges(knowledge, "mouth")
    expr_range = _ranges(knowledge, "expression")

    caps = []
    unsupported = set()
    for row in game["mouth"]:
        name = row["name"]
        base = _strip(name.replace("MIK_KUCHI_", ""), VISEME_QUALIFIERS)
        viseme = VISEME_ALIASES.get(base)
        attested = row["idx"] in mouth_ok
        notes = []
        if viseme is None:
            notes.append("SEGA's asset name does not map onto a known viseme; the slot exists "
                         "but its articulation is unmeasured, so it is never chosen")
        if not attested:
            notes.append("no shipping script sends this shape index")
        rng = shape_range.get(str(row["idx"])) or shape_range.get(row["idx"])
        caps.append(Capability(
            name=name, slot=row["slot"], idx=row["idx"], anim=row["anim"], kind="mouth",
            attested=attested, viseme=viseme, mood=None,
            weight_range=rng, hold_values=(knowledge.get("mouth") or {}).get("hold_values"),
            usage=shape_usage.get(row["idx"]), effect=(effects or {}).get(name), notes=notes))
        if viseme is None:
            unsupported.add("mouth:%s" % name)

    for row in game["expression"]:
        name = row["name"]
        base = _strip(name.replace("MIK_FACE_", ""), MOOD_QUALIFIERS)
        mood = MOOD_ALIASES.get(base)
        attested = row["slot"] in expr_ok
        notes = []
        if mood is None:
            notes.append("SEGA's asset name does not map onto a known mood; the slot exists but "
                         "its expression is unmeasured, so it is never chosen")
        if not attested:
            notes.append("no shipping script sends this expression id")
        rng = expr_range.get(str(row["slot"])) or expr_range.get(row["slot"])
        caps.append(Capability(
            name=name, slot=row["slot"], idx=row["idx"], anim=row["anim"], kind="expression",
            attested=attested, viseme=None, mood=mood,
            weight_range=rng, hold_values=(knowledge.get("expression") or {}).get("hold_values"),
            usage=expr_usage.get(row["slot"]), effect=(effects or {}).get(name), notes=notes))
        if mood is None:
            unsupported.add("expression:%s" % name)

    # The two feature blocks the named-target lists do not contain.  They are modelled because a
    # mapping needs somewhere to point and an audit needs something to report against; they are
    # `attested=False` because no shipping script cues them, which is exactly what keeps the
    # exporter from writing a number nobody can vouch for.
    for row in (game.get("feature") or {}).get("eyes", []):
        base = row["name"].replace("MIK_", "").replace("CMN_", "").replace("_OLD", "")
        label = GAZE_LABELS.get(base.replace("EYES_", ""))
        notes = ["the EYES_* block is real and named in the game's own table, but no shipping "
                 "script cues any slot from it and no command in the corpus takes a slot index "
                 "from it, so how the engine is asked for a gaze is NOT verified"]
        if label is None:
            notes.append("no direction label for this asset name")
        caps.append(Capability(
            name=row["name"], slot=row["slot"], idx=None, anim=row["anim"], kind="gaze",
            attested=False, viseme=None, mood=None, weight_range=None, hold_values=None,
            usage=None, effect=(effects or {}).get(row["name"]), notes=notes, label_override=label))
    for row in (game.get("feature") or {}).get("eyebrow", []):
        base = row["name"].replace("MIK_", "").replace("_CL", "")
        label = EYEBROW_LABELS.get(base)
        notes = ["the per-side eyebrow slots exist in the MEGA39's era table, but no shipping "
                 "script cues them, so their parameter encoding is NOT verified"]
        caps.append(Capability(
            name=row["name"], slot=row["slot"], idx=None, anim=row["anim"], kind="eyebrow",
            attested=False, viseme=None, mood=None, weight_range=None, hold_values=None,
            usage=None, effect=(effects or {}).get(row["name"]), notes=notes, label_override=label))

    for category, (dimension, _weights) in MMD_TO_DIVA.items():
        if dimension in ("none", "drop", "report"):
            unsupported.add(category)
    source = morph_core.which_slot_source()
    return CapabilityModel(caps, unsupported, source, warnings)


def dimension_for(category):
    """(dimension, {label: weight}) for an MMD category, or (None, {}) when DIVA cannot do it."""
    return MMD_TO_DIVA.get(category, (None, {}))


def mood_prior_for(name, category):
    """A mood label for an `emotion`-family morph, from the fine-grained substring tables.

    Returns None when nothing matches, which the mapper reports as an unmapped emotion rather
    than defaulting to a neutral face (a wrong cheerful face is worse than no change).
    """
    if category != "emotion":
        return None
    for token, mood in EMOTION_MOOD.items():
        if token in name:
            return mood
    return None


# Direction words, in the same order the catalog's gaze family tests them.  Two-letter compass
# terms come first so `上左` / `downleft` resolve to DOWN_LEFT and not to UP.
_GAZE_TOKENS = (
    ("UP_LEFT", ("上左", "左上", "upleft", "up_left", "up-left")),
    ("UP_RIGHT", ("上右", "右上", "upright", "up_right", "up-right")),
    ("DOWN_LEFT", ("下左", "左下", "downleft", "down_left", "down-left")),
    ("DOWN_RIGHT", ("下右", "右下", "downright", "down_right", "down-right")),
    ("UP", ("上", "up", "ue")),
    ("DOWN", ("下", "down", "sita", "shita")),
    ("LEFT", ("左", "left", "hidari")),
    ("RIGHT", ("右", "right", "migi")),
)


def direction_prior_for(name, category):
    """The gaze direction a morph's own name states, or None.

    A gaze morph's *direction* is the one thing its name states unambiguously, and getting it
    backwards is the classic silent failure ( `下` = "down" ranking the UP slot first ).  This is a
    name-level reading and is reported as such; it never overrides a measured effect, because the
    mapper only uses it as a semantic weight.
    """
    if category != "gaze":
        return None
    key = morph_core.normalize(name) if hasattr(morph_core, "normalize") else name
    low = key.lower()
    for label, tokens in _GAZE_TOKENS:
        for token in tokens:
            if token in low:
                return label
    return None


def summarise(model=None):
    model = model or build()
    lines = ["capability source: %s" % model.source,
             "%d target(s): %d mouth, %d expression; %d attested; %d with measured effect"
             % (len(model.capabilities), sum(1 for c in model.capabilities if c.kind == "mouth"),
                sum(1 for c in model.capabilities if c.kind == "expression"),
                sum(1 for c in model.capabilities if c.attested),
                sum(1 for c in model.capabilities if c.effect is not None))]
    for w in model.warnings:
        lines.append("warning: %s" % w)
    lines.append("MMD categories with no measured DIVA dimension (%d): %s"
                 % (len(model.unsupported), ", ".join(sorted(model.unsupported))))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    m = build()
    print(summarise(m))
    print()
    for dim, label in (("viseme", "A"), ("viseme", "SMIRK"), ("mood", "HAPPY"),
                       ("mood", "ANGRY")):
        rows = m.by_label(dim, label)
        print("%s %-10s -> %s" % (dim, label,
                                  ", ".join("%s(slot=%d usage=%s)" % (c.name, c.slot, c.usage)
                                            for c in rows) or "NONE"))
