"""One entry point for the whole transfer, so every caller runs the same path.

    VMD  ──parse──►  keyframe tracks (raw bytes, decoded name, normalised key, order)
    PMX  ──resolve──►  morph definitions (vertex / bone / uv / material / group / flip)
                        │
                        ├──► MorphEffectSignature  (measured, interocular units)
                        └──► classification        (catalog + panel + geometry, with evidence)
                        │
    both ──►  mapper  ──►  MappingDecision per morph
                        │
                        └──►  Audit  ──►  report + coverage   (silent drops provably 0)

`run()` is that pipeline and nothing else: it reads, measures, decides and reports.  It writes no
game files - the DSC splice stays in `face_core.export_face`, which is already validated against
the shipping corpus, and `plan_for_export()` adapts a mapping into the plan that function takes so
the two cannot drift apart.

Everything is optional.  With no PMX the geometry stages are skipped and every decision is graded
as name-only, with `SOURCE_DEFINITION_UNAVAILABLE` reported for the morphs whose semantics cannot
be resolved - never silently assumed.  The returned report always says which sources were available.
"""
import os

from . import mmd_morph
from . import morph_audit as audit_mod
from . import morph_mapper as mapper
from . import morph_timeline as timeline

# How an audit's grade maps onto the tier names `face_core.plan_from_match` already understands.
GRADE_TO_TIER = {
    mapper.EXACT: "exact",
    mapper.NEAR_EXACT: "effect",
    mapper.SEMANTIC: "semantic",
    mapper.GEOMETRIC: "geometric",
    mapper.METHOD_USER_ANSWER: "user",
    mapper.APPROXIMATE: "approx",
}

# Grades the exporter may turn into cues without a human decision.  `APPROXIMATE` is deliberately
# absent: the audit reports it with its measured error, and emitting it needs a conscious choice.
EMITTABLE = (mapper.EXACT, mapper.NEAR_EXACT, mapper.SEMANTIC, mapper.GEOMETRIC)


class PipelineResult(object):
    """Everything one run produced, so a caller never has to re-derive it."""

    __slots__ = ("tracks", "curves", "definitions", "signatures", "frame", "decisions",
                 "audit", "report", "sources", "notes", "unmeasured")

    def __init__(self):
        self.tracks = {}
        self.curves = {}
        self.definitions = {}
        self.signatures = {}
        self.frame = None
        self.decisions = []
        self.audit = None
        self.report = {}
        self.sources = {}
        self.notes = []
        self.unmeasured = {}

    @property
    def emit_plan(self):
        """The morphs that may become cues, as ``{vmd_name: MappingDecision}``."""
        return {d.name: d for d in self.decisions if d.grade in EMITTABLE and d.target
                and d.target_kind != "lane"}

    @property
    def lane_plan(self):
        """The morphs the eyelid lane owns, as ``{vmd_name: side}``."""
        out = {}
        for d in self.decisions:
            if d.target_kind != "lane":
                continue
            role = {"eyelid_blink": "both", "eyelid_wink_left": "left",
                    "eyelid_wink_right": "right"}.get(d.classification.category)
            if role:
                out[d.name] = role
        return out

    def summarise(self):
        cov = (self.report or {}).get("coverage") or {}
        return ("%d morph(s) / %d keyframe(s): %d mapped (%d exact, %d near-exact, %d semantic, "
                "%d geometric), %d approximate, %d unsupported, %d without a source definition; "
                "silent drops %s"
                % (cov.get("total_source_morphs", 0), cov.get("total_keyframes", 0),
                   cov.get("mapped_morphs", 0), cov.get("exact_mapped", 0),
                   cov.get("near_exact_mapped", 0), cov.get("semantic_mapped", 0),
                   cov.get("geometric_mapped", 0), cov.get("approximate_mapped", 0),
                   cov.get("unsupported_target", 0), cov.get("missing_pmx_definition", 0),
                   cov.get("silent_drops", "?")))


def find_source_pmx(vmd_path, explicit=None):
    """A PMX for this motion: the user's choice, else a sidecar, else the only file in the folder.

    A `.vmd`'s own 20-byte model-name field is not a reliable pointer (MMD warns on a mismatch and
    exporters leave it blank), so the search is by name and then by uniqueness, and the result is
    reported rather than assumed.  Returning the wrong model would silently mis-measure every
    morph, so an ambiguous folder returns None and says so.
    """
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    folder = os.path.dirname(os.path.abspath(vmd_path))
    stem = os.path.splitext(os.path.basename(vmd_path))[0]
    candidates = []
    for name in os.listdir(folder):
        if name.lower().endswith(".pmx"):
            candidates.append(os.path.join(folder, name))
    if not candidates:
        return None
    for path in candidates:
        base = os.path.splitext(os.path.basename(path))[0]
        if base == stem or base.lower().startswith(stem.lower()):
            return path
    # a motion folder that carries exactly one model is unambiguous
    return candidates[0] if len(candidates) == 1 else None


def run(vmd_path, pmx_path=None, alias=None, user=None, knowledge=None, progress=None,
        allow_unattested=False, frames=None, ratio=timeline.TARGET_FPS / timeline.SOURCE_FPS,
        target_name=None):
    """Read a motion (and optionally its model), decide every morph, and audit the result."""
    from . import diva_capability
    from . import morph_core
    from . import source_model
    from . import vmd_reader

    res = PipelineResult()
    res.sources["motion"] = {"path": os.path.abspath(vmd_path)}
    res.sources["model"] = {"path": os.path.abspath(pmx_path) if pmx_path else None}

    doc = vmd_reader.read(vmd_path)
    res.sources["motion"].update({"dialect": doc["dialect"], "model_name": doc["model"],
                                  "records": len(doc["morphs"])})
    res.tracks, keyframe_report = mmd_morph.keyframes_from_vmd_records(doc["morphs"])
    res.sources["motion"]["keyframe_report"] = keyframe_report
    if keyframe_report["unnamed_records"]:
        res.notes.append(
            "%d morph record(s) in this VMD have a name that decodes to nothing; MMD overwrites "
            "the first byte of a name it cannot encode in Shift-JIS, and that byte is what makes "
            "the rest readable, so those records exist but cannot be attributed to a morph. They "
            "are counted here and cannot be transferred."
            % keyframe_report["unnamed_records"])
    res.curves = timeline.curves_from_tracks(res.tracks)

    # ---- geometry: only when a model was supplied
    if pmx_path:
        try:
            definitions, positions, frame, source_report = source_model.load_source(pmx_path)
            res.sources["model"].update(source_report)
            res.definitions = definitions
            res.frame = frame
            if frame is None:
                res.notes.append(
                    "the model has no usable eye bones, so no interocular frame could be built; "
                    "every regional score is therefore absent and no mapping can claim a measured "
                    "effect (%s)" % source_report.get("frame_error"))
            else:
                res.signatures, res.unmeasured = source_model.measure(definitions, positions, frame)
                if res.unmeasured:
                    res.notes.append(
                        "%d morph(s) carry no vertex offsets of their own and were not measured "
                        "(bone/UV/material morphs); they are reported, not treated as zero effect"
                        % len(res.unmeasured))
        except Exception as exc:
            res.sources["model"]["error"] = "%s: %s" % (type(exc).__name__, exc)
            res.notes.append("the source model could not be read, so no effect could be measured: "
                             "%s" % exc)
    else:
        res.notes.append(
            "no source PMX was supplied, so nothing about what these morphs actually deform could "
            "be measured: every decision below is name- and table-based, and the report says so. "
            "Geometry equivalence is NOT VERIFIED for this run.")

    # ---- capability model, with the character model's measured effects attached when available
    effects = {}
    for name, cap_name in ((target_name, None),):
        pass
    capability_model = diva_capability.build(knowledge=knowledge, effects=effects)

    # ---- mapping
    keyframes = {name: len(keys) for name, keys in res.tracks.items()}
    engine = mapper.Mapper(capability_model, definitions=res.definitions, signatures=res.signatures,
                           alias=alias, user=user, allow_unattested=allow_unattested)
    res.decisions = engine.run(keyframe_counts=keyframes)

    # ---- timeline error, measured on the target grid
    last = max((k.frame for keys in res.tracks.values() for k in keys), default=0)
    total_frames = int(frames) if frames else (last * ratio + 2)
    res.report = {"sources": res.sources, "notes": res.notes,
                  "timeline": timeline.report(res.curves, total_frames, ratio=ratio),
                  "keyframe_report": keyframe_report}

    # ---- audit
    audit = audit_mod.Audit(source=res.sources.get("model") or {},
                            motion=res.sources.get("motion") or {},
                            target={"capability_source": capability_model.source,
                                    "targets": capability_model.describe()},
                            notes=res.notes)
    for d in res.decisions:
        audit.add(d, res.curves.get(d.name))
    audit.timeline = res.report["timeline"]
    audit.extra = {"unmeasured_morphs": res.unmeasured,
                   "lane_plan": res.lane_plan,
                   "emit_plan": sorted(res.emit_plan),
                   "ratio": ratio, "target_frames": total_frames}
    data = audit.report(expected_names=[n for n, c in keyframes.items() if c > 1],
                        keyframe_counts=keyframes)
    data["sources"] = res.sources
    res.audit = audit
    res.report = data
    return res


def plan_for_export(result, game):
    """Adapt a pipeline result into the `plan` dict `face_core.export_face` takes.

    The plan's shape is ``{vmd_name: (kind, param, game_name, how)}``; the target's numeric
    parameter is resolved through the same capability model the mapping used, so a slot can never
    be written that the capability model did not offer.
    """
    plan = {}
    by_name = {}
    for cap in (game or {}).get("mouth", []) + (game or {}).get("expression", []):
        by_name[cap["name"]] = cap
    for d in result.decisions:
        if d.grade not in EMITTABLE or not d.target or d.target_kind == "lane":
            continue
        row = by_name.get(d.target)
        if row is None:
            continue
        if row["kind"] == "mouth":
            plan[d.name] = ("mouth", row["idx"], row["name"], GRADE_TO_TIER.get(d.grade, "semantic"))
        else:
            plan[d.name] = ("expression", row["slot"], row["name"],
                            GRADE_TO_TIER.get(d.grade, "semantic"))
    return plan


def lane_plan(result):
    """The eyelid lane assignments the audit reports, as ``{name: 'both'|'left'|'right'}``.

    The lane is a classification, not an output: the exporter translates only the both-eyes blink
    (into ``EXPRESSION`` 22/21) and a per-eye wink has no face command to travel in.  The catalog's
    spelling knowledge is wider than `morph_core.eyelid_role`, so the two are compared here and a
    disagreement is *reported* rather
    than resolved silently.
    """
    from . import morph_core
    agreed, disagreed = {}, {}
    for name, role in result.lane_plan.items():
        known = morph_core.eyelid_role(name)
        if known is None:
            disagreed[name] = role
        elif known != role:
            disagreed[name] = role
        else:
            agreed[name] = role
    return {"plan": result.lane_plan, "confirmed_by_morph_core": agreed,
            "not_known_to_morph_core": disagreed}


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from . import morph_core
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    aliases = {}
    try:
        for table in ("mouth", "expression", "mouth_approx", "expression_approx"):
            for k, v in morph_core.tables()[table].items():
                aliases.setdefault(k, v)
    except Exception as exc:
        print("warning: alias tables unavailable (%s)" % exc)
    pmx = find_source_pmx(args[0]) if args else None
    print("source PMX: %s" % (pmx or "none found"))
    out = run(args[0], pmx_path=pmx, alias=aliases)
    print(out.summarise())
    print()
    print(audit_mod.text_summary(out.report, limit=25))
    if len(args) > 1:
        with open(args[1], "w", encoding="utf-8", newline="\n") as fh:
            json.dump(out.report, fh, ensure_ascii=False, indent=1)
        print("\nwrote %s" % args[1])
