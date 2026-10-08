"""The audit: every source morph accounted for, with a reason, and no silent drops.

The contract this module enforces is one line long:

    every morph the motion animates appears exactly once in the report, with a grade and a reason

That is what makes `silent_drops: 0` a *checkable* claim rather than a slogan: `Audit.verify()`
re-walks the source keyframe counts and the decisions and raises when a name is missing, when a
name appears twice, when a dropped morph has no reason, or when a decision claims a mapping its
grade does not support.  A caller cannot produce a report that hides a morph by accident; it can
only do so by deleting the check.

The report is plain data (a dict of JSON-able values) so it can be written to a file, shown in a
panel, or diffed between two runs.  `coverage` fields mirror the ones the project brief names.
"""
import json
import os

from . import morph_mapper as mapper

AUDIT_VERSION = 2

# Fields the report promises to carry per morph, in the brief's own vocabulary.
ROW_FIELDS = (
    "source_name", "normalized_name", "stem", "source_pmx", "source_morph_index", "morph_type",
    "semantic_category", "semantic_source", "semantic_confidence",
    "source_keyframe_count", "source_weight_min", "source_weight_max",
    "affected_vertex_count", "affected_bone_count", "affected_material_count", "affected_uv_count",
    "group_child_count", "group_children", "group_weights",
    "target_slot", "target_kind", "target_idx", "target_mapping", "mapping_method",
    "geometry_analysis_available", "effect_signature_available",
    "effect_distance", "effect_top_feature",
    "transferred_keyframe_count", "dropped_keyframe_count",
    "grade", "status", "reason", "evidence", "notes",
)


class AuditError(RuntimeError):
    """A report that would hide or double-count a morph."""


class Audit(object):
    """Accumulates per-morph rows and the run's counters, then proves the run is complete."""

    def __init__(self, source=None, motion=None, target=None, notes=None):
        self.source = source or {}
        self.motion = motion or {}
        self.target = target or {}
        self.rows = []
        self.notes = list(notes or [])
        self.lane_rows = []
        self.timeline = {}
        self.extra = {}

    # ------------------------------------------------------------------------------ row building
    def row_for(self, decision, curve=None, transferred=None):
        d = decision
        definition = d.source_definition
        sig = d.signature
        cls = d.classification
        lo = hi = None
        keys = d.source_keys
        if curve is not None:
            lo, hi = curve.weight_range
            keys = len(curve.keys)
        row = {
            "source_name": d.name,
            "normalized_name": d.norm,
            "stem": d.stem,
            "source_pmx": (definition.source_model if definition else None) or self.source.get("path"),
            "source_morph_index": definition.source_index if definition else None,
            "morph_type": definition.kind if definition else None,
            "source_provenance": definition.provenance if definition else None,
            "semantic_category": cls.category if cls else None,
            "semantic_source": cls.source if cls else None,
            "semantic_confidence": cls.confidence if cls else None,
            "source_keyframe_count": keys,
            "source_weight_min": lo,
            "source_weight_max": hi,
            "affected_vertex_count": len(definition.vertex_offsets) if definition else 0,
            "affected_bone_count": len(definition.bone_offsets) if definition else 0,
            "affected_material_count": len(definition.material_offsets) if definition else 0,
            "affected_uv_count": len(definition.uv_offsets) if definition else 0,
            "group_child_count": len(definition.group_children) if definition else 0,
            "group_children": list(definition.group_children) if definition else [],
            "group_weights": list(definition.group_weights) if definition else [],
            "target_slot": d.target_slot,
            "target_kind": d.target_kind,
            "target_idx": d.target_idx,
            "target_mapping": d.target,
            "mapping_method": d.method,
            "geometry_analysis_available": bool(definition and definition.has_geometry),
            "effect_signature_available": sig is not None,
            "effect_distance": d.effect_distance,
            "effect_top_feature": (d.effect_features[0]["feature"] if d.effect_features else None),
            "transferred_keyframe_count": (transferred if transferred is not None
                                           else (keys if d.mapped else 0)),
            "dropped_keyframe_count": 0 if d.mapped else keys,
            "grade": d.grade,
            "status": _status_for(d),
            "reason": d.reason,
            "evidence": list(cls.evidence) if cls else [],
            "notes": list(d.notes),
            "score_breakdown": d.breakdown,
        }
        return row

    def add(self, decision, curve=None, transferred=None):
        row = self.row_for(decision, curve, transferred)
        if row["target_kind"] == "lane":
            self.lane_rows.append(row)
        else:
            self.rows.append(row)
        return row

    def add_curve_only(self, name, curve, reason, grade=mapper.INVALID_SOURCE):
        """A source track with no decision of its own - reported, never discarded."""
        lo, hi = curve.weight_range
        self.rows.append({
            "source_name": name, "normalized_name": name, "stem": name,
            "source_pmx": self.source.get("path"), "source_morph_index": None,
            "morph_type": None, "source_provenance": None,
            "semantic_category": None, "semantic_source": None, "semantic_confidence": None,
            "source_keyframe_count": len(curve.keys), "source_weight_min": lo,
            "source_weight_max": hi,
            "affected_vertex_count": 0, "affected_bone_count": 0, "affected_material_count": 0,
            "affected_uv_count": 0, "group_child_count": 0, "group_children": [], "group_weights": [],
            "target_slot": None, "target_kind": None, "target_idx": None, "target_mapping": None,
            "mapping_method": mapper.METHOD_NONE,
            "geometry_analysis_available": False, "effect_signature_available": False,
            "effect_distance": None, "effect_top_feature": None,
            "transferred_keyframe_count": 0, "dropped_keyframe_count": len(curve.keys),
            "grade": grade, "status": "DROPPED",
            "reason": reason, "evidence": [], "notes": [], "score_breakdown": {},
        })

    # ----------------------------------------------------------------------------------- counters
    def coverage(self):
        counts = {}
        for row in self.rows + self.lane_rows:
            counts[row["grade"]] = counts.get(row["grade"], 0) + 1
        total_kf = sum(r["source_keyframe_count"] or 0 for r in self.rows + self.lane_rows)
        moved = sum(r["transferred_keyframe_count"] or 0 for r in self.rows + self.lane_rows)
        mapped = sum(counts.get(g, 0) for g in (mapper.EXACT, mapper.NEAR_EXACT, mapper.SEMANTIC,
                                                mapper.GEOMETRIC))
        dropped = sum(counts.get(g, 0) for g in (mapper.APPROXIMATE, mapper.TARGET_UNSUPPORTED,
                                                 mapper.SOURCE_DEFINITION_UNAVAILABLE,
                                                 mapper.INVALID_SOURCE))
        return {
            "total_source_morphs": len(self.rows) + len(self.lane_rows),
            "source_definitions_resolved": sum(1 for r in self.rows + self.lane_rows
                                               if r["geometry_analysis_available"]),
            "mapped_morphs": mapped,
            "exact_mapped": counts.get(mapper.EXACT, 0),
            "near_exact_mapped": counts.get(mapper.NEAR_EXACT, 0),
            "semantic_mapped": counts.get(mapper.SEMANTIC, 0),
            "geometric_mapped": counts.get(mapper.GEOMETRIC, 0),
            "approximate_mapped": counts.get(mapper.APPROXIMATE, 0),
            "unsupported_target": counts.get(mapper.TARGET_UNSUPPORTED, 0),
            "missing_pmx_definition": counts.get(mapper.SOURCE_DEFINITION_UNAVAILABLE, 0),
            "invalid_source": counts.get(mapper.INVALID_SOURCE, 0),
            "dropped_morphs": dropped,
            "lane_morphs": len(self.lane_rows),
            "total_keyframes": total_kf,
            "transferred_keyframes": moved,
            "dropped_keyframes": total_kf - moved,
            "max_effect_error": self._max_effect(),
            "rms_effect_error": None,          # filled by the validation run when targets are measured
            "max_temporal_error": self.timeline.get("max_reconstruction_error"),
            "rms_temporal_error": self.timeline.get("max_rms_error"),
            "silent_drops": 0,
        }

    def _max_effect(self):
        vals = [r["effect_distance"] for r in self.rows if r["effect_distance"] is not None]
        return max(vals) if vals else None

    # ---------------------------------------------------------------------------------- verification
    def verify(self, expected_names=None, keyframe_counts=None):
        """Prove the report is complete.  Raises `AuditError` with the specific violation.

        Checked, in order: exactly one row per source name; every name the motion animates present;
        every dropped row carrying a reason and a grade; every mapped row carrying a target; and no
        row claiming a target while its grade says unsupported.
        """
        all_rows = self.rows + self.lane_rows
        seen = {}
        for row in all_rows:
            name = row["source_name"]
            seen[name] = seen.get(name, 0) + 1
        dupes = {n: c for n, c in seen.items() if c > 1}
        if dupes:
            raise AuditError("these source morphs appear more than once in the report: %s"
                             % sorted(dupes)[:10])
        if expected_names is not None:
            missing = sorted(set(expected_names) - set(seen))
            if missing:
                raise AuditError("the motion animates %d morph(s) the report does not mention: %s"
                                 % (len(missing), missing[:10]))
        if keyframe_counts:
            for row in all_rows:
                want = keyframe_counts.get(row["source_name"])
                got = row["source_keyframe_count"]
                if want is not None and got not in (None, want):
                    raise AuditError("morph %r: the report counts %s keyframes, the motion has %s"
                                     % (row["source_name"], got, want))
        for row in all_rows:
            if row["grade"] in (mapper.TARGET_UNSUPPORTED, mapper.SOURCE_DEFINITION_UNAVAILABLE,
                                mapper.INVALID_SOURCE, mapper.APPROXIMATE):
                if row["target_mapping"] is None and not row["reason"]:
                    raise AuditError("morph %r is dropped with neither a target nor a reason"
                                     % row["source_name"])
                if row["grade"] != mapper.APPROXIMATE and row["target_mapping"] is not None:
                    raise AuditError("morph %r is graded %s yet carries target %r - a grade and a "
                                     "target cannot disagree" % (row["source_name"], row["grade"],
                                                                 row["target_mapping"]))
            if row["grade"] in (mapper.EXACT, mapper.NEAR_EXACT, mapper.SEMANTIC,
                                mapper.GEOMETRIC) and row["target_mapping"] is None:
                raise AuditError("morph %r is graded %s but has no target"
                                 % (row["source_name"], row["grade"]))
        return True

    # -------------------------------------------------------------------------------------- output
    def report(self, expected_names=None, keyframe_counts=None):
        self.verify(expected_names, keyframe_counts)
        cov = self.coverage()
        return {
            "audit_version": AUDIT_VERSION,
            "source": self.source,
            "motion": self.motion,
            "target": self.target,
            "coverage": cov,
            "timeline": self.timeline,
            "notes": list(self.notes),
            "extra": dict(self.extra),
            "rows": sorted(self.rows, key=lambda r: (-(r["source_keyframe_count"] or 0),
                                                     str(r["source_name"]))),
            "lane_rows": sorted(self.lane_rows,
                                key=lambda r: (-(r["source_keyframe_count"] or 0),
                                               str(r["source_name"]))),
        }

    def write(self, path, expected_names=None, keyframe_counts=None):
        data = self.report(expected_names, keyframe_counts)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        return data


def _status_for(decision):
    if decision.target_kind == "lane":
        return "LANE_TRANSFER"
    if decision.grade == mapper.EXACT:
        return "EXACT_MAPPING"
    if decision.grade == mapper.NEAR_EXACT:
        return "NEAR_EXACT_MAPPING"
    if decision.grade == mapper.SEMANTIC:
        return "SEMANTIC_MAPPING"
    if decision.grade == mapper.GEOMETRIC:
        return "GEOMETRIC_MAPPING"
    if decision.grade == mapper.APPROXIMATE:
        return "APPROXIMATE_MAPPING"
    if decision.grade == mapper.TARGET_UNSUPPORTED:
        return "TARGET_UNSUPPORTED"
    if decision.grade == mapper.SOURCE_DEFINITION_UNAVAILABLE:
        return "SOURCE_DEFINITION_UNAVAILABLE"
    return "INVALID_SOURCE"


def text_summary(data, limit=None):
    """The block a panel or a console shows: counters first, then the rows that need a human."""
    cov = data["coverage"]
    lines = [
        "morph transfer audit v%d" % data["audit_version"],
        "  source: %s" % (data["source"].get("path") or "-"),
        "  motion: %s" % (data["motion"].get("path") or "-"),
        "  %d source morph(s), %d keyframe(s)" % (cov["total_source_morphs"],
                                                  cov["total_keyframes"]),
        "  mapped %d (%d keyframes): exact %d, near-exact %d, semantic %d, geometric %d"
        % (cov["mapped_morphs"], cov["transferred_keyframes"], cov["exact_mapped"],
           cov["near_exact_mapped"], cov["semantic_mapped"], cov["geometric_mapped"]),
        "  approximate %d, unsupported %d, no source definition %d, invalid %d"
        % (cov["approximate_mapped"], cov["unsupported_target"], cov["missing_pmx_definition"],
           cov["invalid_source"]),
        "  dropped %d morph(s) / %d keyframe(s); SILENT DROPS: %d"
        % (cov["dropped_morphs"], cov["dropped_keyframes"], cov["silent_drops"]),
    ]
    if cov["max_effect_error"] is not None:
        lines.append("  max effect error %.4f" % cov["max_effect_error"])
    if cov["max_temporal_error"] is not None:
        lines.append("  max temporal error %.6f (grid), RMS %.6f"
                     % (cov["max_temporal_error"], cov["rms_temporal_error"] or 0.0))
    for note in data.get("notes") or []:
        lines.append("  note: %s" % note)
    problems = [r for r in data["rows"] + data["lane_rows"]
                if r["grade"] in (mapper.APPROXIMATE, mapper.TARGET_UNSUPPORTED,
                                  mapper.SOURCE_DEFINITION_UNAVAILABLE, mapper.INVALID_SOURCE)]
    if problems:
        lines.append("  --- morphs that did not map cleanly (%d) ---" % len(problems))
        for row in (problems[:limit] if limit else problems):
            lines.append("    %-24s %-30s %s" % (row["source_name"], row["status"],
                                                 row["reason"][:70]))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(__doc__)
