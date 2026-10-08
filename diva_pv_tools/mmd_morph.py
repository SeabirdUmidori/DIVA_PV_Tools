"""The three layers an MMD face actually has, kept apart on purpose.

    MorphDefinition   what a morph DOES to the model      (from the PMX)
    MorphKeyframe     how much it is applied, and when     (from the VMD)
    MorphEvaluation   the resulting deformation at a weight

Confusing these is the single failure this module exists to prevent.  A VMD record carries a
*name*, a *frame* and a *weight* and nothing else - it does not say what the morph moves, and a
weight of 0.75 is not a distance.  A PMX morph carries the deformation and no animation.  Only
the two together describe a face.

What this module adds over `morph_core`:

  * a keyframe record that keeps the raw Shift-JIS bytes, the decoded string, the normalised key,
    the field width, the file order and the ordinal - nothing is collapsed at parse time, so the
    original animation can always be reconstructed;
  * a `MorphDefinition` for the source model, filled either from the PMX itself or from the
    Blender scene mmd_tools assembled, with an explicit `PROVENANCE` so callers can never mistake
    "a shape key happens to have this name" for "this is what the source morph does";
  * a catalog-driven `classify()` that reads `data/mmd_morph_catalog.json`, and reports the
    PMX panel byte and the measured deformation as *corroborating evidence*, separately from the
    name.  A name match is `SOURCE_NAME`; geometry agreement raises it to `CORROBORATED`; a
    disagreement is reported as `CONFLICT` rather than silently resolved.

No Blender import: the geometry and PMX access lives in `morph_effect`, which is handed data.
"""
import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_JSON = os.path.join(HERE, "data", "mmd_morph_catalog.json")

# PMX panel bytes (PMX 仕様書): 0 system, 1 eyebrow, 2 eye, 3 mouth, 4 other
PANEL_NAMES = {0: "system", 1: "eyebrow", 2: "eye", 3: "mouth", 4: "other"}
# PMX morph kinds, the spec's own numbering
MORPH_KINDS = {0: "group", 1: "vertex", 2: "bone", 3: "uv", 4: "uv1", 5: "uv2", 6: "uv3", 7: "uv4",
               8: "material", 9: "flip", 10: "impulse"}

# Where a MorphDefinition came from.  Ordered by how much it can be trusted about geometry.
PROVENANCE_PMX_BYTES = "pmx_bytes"            # parsed from the model file itself
PROVENANCE_BLENDER_SHAPEKEY = "blender_shapekey"   # mmd_tools' shape key for a vertex morph
PROVENANCE_BLENDER_PROP = "blender_mmd_root"  # mmd_tools' custom property, no geometry
PROVENANCE_NAME_ONLY = "name_only"            # only the VMD name is known
PROVENANCE_NONE = "none"

# How a semantic classification was reached.
SOURCE_NAME = "name"
SOURCE_GEOMETRY = "geometry"
SOURCE_PANEL = "panel"
SOURCE_CORROBORATED = "name+geometry"
SOURCE_CONFLICT = "conflict"
SOURCE_NONE = "unclassified"


def _catalog():
    with open(CATALOG_JSON, encoding="utf-8") as handle:
        blob = json.load(handle)
    families = []
    for fam in blob["families"]:
        names = {normalize(n) for n in fam.get("names", [])}
        regexes = [re.compile(rx) for rx in fam.get("regex", [])]
        families.append({"category": fam["category"], "names": names, "regex": regexes,
                         "usage_pct": fam.get("usage_pct", 0.0)})
    bands = set(blob.get("band_devices", {}).get("names", []))
    return {"families": families, "categories": blob["categories"], "bands": bands,
            "_raw": blob}


_CATALOG = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _catalog()
    return _CATALOG


# --------------------------------------------------------------------------------- normalisation
# Deliberately the same rules `morph_core.normalize` uses, so a name classified here and a name
# matched there cannot disagree about what counts as the same string.
_SEPARATORS = str.maketrans({c: None for c in "_-. /\\\u30fb\uff0e\uff0f\uff3c\uff65\u00b7"})


def normalize(name):
    s = unicodedata.normalize("NFKC", name or "")
    s = s.replace("\u30fb", "\u30fc")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.translate(_SEPARATORS)
    return s.casefold()


_STRIP_ORDINAL = re.compile(r"[\s\u3000]*[0-9\uff10-\uff19\u2460-\u2473\u24ea]+$")


def normalize_stem(name):
    """`normalize()` with a trailing ordinals marker removed: 'あ２' -> 'あ', 'にやり2' -> 'にやり'.

    MMD authors distinguish variants with a trailing digit or circled number ('あ', 'あ2', 'あ3';
    'ウィンク2右').  For *semantics* the ordinal is noise; for *mapping* it is not, because the
    variants are genuinely different drawings.  So the stem is used for classification only, and
    the ordinal stays part of the identity that gets transferred.
    """
    return _STRIP_ORDINAL.sub("", normalize(name))


# ---------------------------------------------------------------------------- VMD keyframes
class MorphKeyframe(object):
    """One VMD morph record, with everything the file said about it.

    `raw_name` keeps the field's bytes so the padding style and the encoding failure marker stay
    auditable; `name` is the decoded string; `norm` is the lookup key; `index` is the record's
    ordinal in the file and `order` its position within its own track.  `weight` is never
    clamped and never rescaled - a caller that needs a bounded value asks for one.
    """

    __slots__ = ("raw_name", "name", "norm", "stem", "frame", "weight", "index", "order",
                 "field_width", "null_terminated", "track")

    def __init__(self, raw_name, name, frame, weight, index, field_width=15,
                 null_terminated=True):
        self.raw_name = raw_name
        self.name = name
        self.norm = normalize(name)
        self.stem = normalize_stem(name)
        self.frame = int(frame)
        self.weight = float(weight)
        self.index = int(index)
        self.order = 0
        self.field_width = field_width
        self.null_terminated = null_terminated
        self.track = None

    @property
    def sjis_failure_marker(self):
        """MMD marks a name it could not encode by overwriting the FIRST byte with 0x00.

        The decoded value then starts with U+0000, which `str.find(b'\\x00')`-style null trimming
        turns into an empty string.  `vmd_reader._name` does exactly that, so such a record
        arrives here with `name == ''` and the whole track is unattributable.  Reporting it is
        the difference between "this motion has no such morph" and "this motion HAS the morph and
        its name was destroyed by the writer".
        """
        return bool(self.raw_name) and self.raw_name[0:1] == b"\x00"

    def describe(self):
        return {"name": self.name, "raw": self.raw_name.hex(), "frame": self.frame,
                "weight": self.weight, "index": self.index, "order": self.order,
                "field_width": self.field_width, "null_terminated": self.null_terminated,
                "sjis_failure_marker": self.sjis_failure_marker}


def keyframes_from_vmd_records(records):
    """Group `vmd_reader` morph records into tracks of `MorphKeyframe`, order preserved.

    Returns ``(tracks, report)`` where `tracks` maps the *decoded* name to its keyframes sorted by
    frame with the file order as the tie-break, and `report` counts the records whose name was
    unusable.  Nothing is dropped: a record whose name decoded empty lands in the "" track and is
    counted, rather than disappearing.
    """
    tracks = {}
    unnamed = []
    for rec in records:
        kf = MorphKeyframe(rec.get("raw_name", b""), rec.get("name", ""), rec["frame"],
                           rec["weight"], rec.get("index", len(unnamed)),
                           rec.get("field_width", 15), rec.get("null_terminated", True))
        if not kf.name:
            unnamed.append(kf)
            continue
        tracks.setdefault(kf.name, []).append(kf)
    for name, keys in tracks.items():
        keys.sort(key=lambda k: (k.frame, k.index))
        for n, k in enumerate(keys):
            k.order = n
            k.track = name
    report = {"unnamed_records": len(unnamed),
              "unnamed_with_failure_marker": sum(1 for k in unnamed if k.sjis_failure_marker),
              "tracks": len(tracks),
              "records": sum(len(v) for v in tracks.values()) + len(unnamed)}
    return tracks, report


# ------------------------------------------------------------------------- morph definitions
class MorphDefinition(object):
    """What one source morph does.  Geometry fields stay empty when it is not known.

    `provenance` says where the record came from and therefore how much of it may be believed:
    only `pmx_bytes` and `blender_shapekey` carry real deformation.  Everything else is a name
    with metadata, and callers that need geometry must check `has_geometry`.
    """

    __slots__ = ("name", "name_en", "panel", "kind", "provenance", "source_model",
                 "source_index", "vertex_offsets", "bone_offsets", "material_offsets",
                 "uv_offsets", "group_children", "group_weights", "flip_children", "vertex_count",
                 "notes")

    def __init__(self, name, name_en="", panel=None, kind="vertex",
                 provenance=PROVENANCE_NAME_ONLY, source_model="", source_index=None):
        self.name = name
        self.name_en = name_en
        self.panel = panel
        self.kind = kind
        self.provenance = provenance
        self.source_model = source_model
        self.source_index = source_index
        self.vertex_offsets = []        # [(vertex_index, dx, dy, dz)]
        self.bone_offsets = []          # [(bone_index, tx, ty, tz, qx, qy, qz, qw)]
        self.material_offsets = []      # raw dicts as the PMX stores them
        self.uv_offsets = []            # [(vertex_index, u, v, z, w)]
        self.group_children = []        # [child morph name]
        self.group_weights = []         # ratio per child, same order
        self.flip_children = []
        self.vertex_count = None
        self.notes = []

    @property
    def has_geometry(self):
        return self.provenance in (PROVENANCE_PMX_BYTES, PROVENANCE_BLENDER_SHAPEKEY)

    @property
    def panel_name(self):
        return PANEL_NAMES.get(self.panel, "unknown" if self.panel is None else str(self.panel))

    def describe(self):
        return {"name": self.name, "name_en": self.name_en, "panel": self.panel,
                "panel_name": self.panel_name, "kind": self.kind,
                "provenance": self.provenance, "source_model": self.source_model,
                "source_index": self.source_index, "vertices": len(self.vertex_offsets),
                "bones": len(self.bone_offsets), "materials": len(self.material_offsets),
                "uv": len(self.uv_offsets), "group_children": len(self.group_children),
                "has_geometry": self.has_geometry, "notes": list(self.notes)}


def definition_from_shape_key(name, coords_delta, vertex_count, source_model="",
                              provenance=PROVENANCE_BLENDER_SHAPEKEY, panel=None):
    """Build a vertex-morph definition from a shape key's *delta* per vertex.

    mmd_tools stores ABSOLUTE coordinates in `ShapeKey.data[i].co` (it copies the basis and adds
    the PMX offset), so the delta is `co - basis_co`.  The caller does that subtraction and hands
    the deltas here; this function only validates shape.
    """
    if len(coords_delta) != vertex_count:
        raise ValueError("%s: %d deltas for %d vertices" % (name, len(coords_delta),
                                                            vertex_count))
    d = MorphDefinition(name, panel=panel, kind="vertex", provenance=provenance,
                        source_model=source_model)
    d.vertex_count = vertex_count
    d.vertex_offsets = [(i, v[0], v[1], v[2]) for i, v in enumerate(coords_delta)]
    return d


# -------------------------------------------------------------------------------- classification
class Classification(object):
    """A semantic decision with its evidence, never a bare label.

    `source` is `name` when only the catalog matched, `name+geometry` when the measured
    deformation agrees, `conflict` when it disagrees, `panel` when only the PMX panel byte knew,
    `unclassified` when nothing did.  `evidence` is a list of human-readable strings, and
    `confidence` a coarse 0..1 - low enough for a name-only guess that no caller can mistake it
    for a measurement.
    """

    __slots__ = ("category", "stem_category", "source", "confidence", "evidence", "panel_category",
                 "name_hit")

    def __init__(self, category="unknown", stem_category=None, source=SOURCE_NONE,
                 confidence=0.0, evidence=None, panel_category=None, name_hit=None):
        self.category = category
        self.stem_category = stem_category or category
        self.source = source
        self.confidence = confidence
        self.evidence = list(evidence or [])
        self.panel_category = panel_category
        self.name_hit = name_hit

    @property
    def is_face(self):
        return self.category not in ("accessory", "plumbing", "body", "unknown")

    @property
    def is_eyelid(self):
        return self.category.startswith("eyelid_")

    @property
    def is_mouth(self):
        return self.category.startswith("mouth_") or self.category == "tongue"

    @property
    def usable_for_geometry_claim(self):
        return self.source in (SOURCE_GEOMETRY, SOURCE_CORROBORATED)

    def describe(self):
        return {"category": self.category, "stem_category": self.stem_category,
                "source": self.source, "confidence": round(self.confidence, 3),
                "evidence": list(self.evidence), "panel_category": self.panel_category}


# Which PMX panel bytes are consistent with which semantic families.  Used as independent
# evidence, never as the decision: a model that puts its mouth morphs on panel 4 is unusual but
# not impossible, and a panel byte cannot say WHICH mouth shape a morph is.
_PANEL_EXPECTS = {
    "eyebrow": ("brow_up", "brow_down", "brow_angry", "brow_sad", "brow_smile", "brow_neutral"),
    "eye": ("eyelid_blink", "eyelid_wink_left", "eyelid_wink_right", "eye_smile", "eye_narrow",
            "eye_wide", "eye_pupil_small", "eye_pupil_large", "eye_pupil_special", "eye_shape",
            "eye_highlight", "gaze"),
    "mouth": ("mouth_phoneme", "mouth_close", "mouth_open", "mouth_smile", "mouth_frown",
              "mouth_smirk", "mouth_wide", "mouth_pucker", "mouth_shape_special", "mouth_teeth",
              "tongue"),
}


def classify(name, panel=None, geometry=None):
    """Semantic category for a morph name, with panel and geometry as separate evidence.

    `geometry` is an optional dict as produced by `morph_effect.MorphEffectSignature.describe()`;
    when present and it names a region (`regions`), agreement with the catalog is recorded as
    `name+geometry` and disagreement as `conflict` - the caller decides what to do about a
    conflict, but it is never hidden.
    """
    cat = catalog()
    key = normalize_stem(name) if name else ""
    evidence = []
    hit = None
    if not key:
        return Classification("unknown", source=SOURCE_NONE, evidence=["the name decoded empty"])

    if key in cat["bands"] or normalize(name) in cat["bands"]:
        return Classification("plumbing", source=SOURCE_NAME, confidence=0.9,
                              evidence=["model-specific band/toggle plumbing"])

    # Exact names across every family first, then the regexes.  The catalog is ordered "most
    # specific first", but that only orders *within* a kind - a general pattern in an early family
    # was beating an exact name in a later one, and it did real damage: `brow_smile`'s /にこり/
    # regex took `にこり口`, which `mouth_smile` lists by name, so a mouth morph was classified as
    # an eyebrow one and the alias audit then flagged the mouth table for containing it.
    for fam in cat["families"]:
        if key in fam["names"]:
            hit = fam
            evidence.append("catalog: exact stem %r -> %s (seen in %.1f%% of 483 motions)"
                            % (key, fam["category"], fam["usage_pct"]))
            break
    if hit is None:
        for fam in cat["families"]:
            for rx in fam["regex"]:
                if rx.search(key):
                    hit = fam
                    evidence.append("catalog: /%s/ matched %r -> %s" % (rx.pattern, key,
                                                                        fam["category"]))
                    break
            if hit:
                break

    panel_cat = None
    if panel is not None:
        for pcat, cats in _PANEL_EXPECTS.items():
            if hit and hit["category"] in cats:
                panel_cat = pcat
                if PANEL_NAMES.get(panel) == pcat:
                    evidence.append("panel byte %d (%s) agrees with the catalog"
                                    % (panel, PANEL_NAMES[panel]))
                else:
                    evidence.append("panel byte %d (%s) does NOT agree with the catalog's %s"
                                    % (panel, PANEL_NAMES.get(panel, panel), pcat))

    if hit is None:
        if panel is not None and PANEL_NAMES.get(panel) in _PANEL_EXPECTS:
            return Classification("unknown", source=SOURCE_PANEL, confidence=0.15,
                                  evidence=["no catalog match; the PMX panel says %s, which "
                                            "narrows the region but not the morph"
                                            % panel_cat or PANEL_NAMES[panel]],
                                  panel_category=panel_cat)
        return Classification("unknown", source=SOURCE_NONE,
                              evidence=["no catalog match for %r" % key])

    category = hit["category"]
    source = SOURCE_NAME
    confidence = 0.6
    if key != normalize(name):
        evidence.append("classification used the stem %r (the trailing ordinal distinguishes a "
                        "variant, not the semantic)" % key)

    if geometry:
        regions = geometry.get("regions") or {}
        expect_region = {
            "eyebrow": "eyebrow", "eye": "eye", "mouth": "mouth",
        }
        want = None
        if category.startswith("brow_") or category == "eyebrow":
            want = "eyebrow"
        elif category.startswith(("eyelid_", "eye_", "gaze")):
            want = "eye"
        elif category.startswith(("mouth_", "tongue")) or category == "tongue":
            want = "mouth"
        if want:
            score = regions.get(want)
            if score is None and geometry is not None:
                # the geometry was measured and the expected region is not in it at all: that is a
                # disagreement, not missing evidence
                score = 0.0
            if score is not None:
                if score >= 0.5:
                    source = SOURCE_CORROBORATED
                    confidence = min(0.95, 0.6 + 0.35 * score)
                    evidence.append("measured deformation concentrates in the %s region "
                                    "(score %.2f), agreeing with the name" % (want, score))
                elif score <= 0.15:
                    source = SOURCE_CONFLICT
                    confidence = 0.25
                    evidence.append("measured deformation does NOT concentrate in the %s region "
                                    "(score %.2f) although the name says it should - the name "
                                    "and the geometry disagree" % (want, score))
                else:
                    evidence.append("measured %s region score %.2f is between the agreeing and "
                                    "conflicting bands; the name decides" % (want, score))

    if panel is not None and panel_cat and not category.startswith(
            ("brow_", "eyelid_", "eye_", "mouth_", "gaze", "tongue")):
        evidence.append("panel byte %d suggests %s, which the catalog's %s does not confirm"
                        % (panel, panel_cat, category))

    return Classification(category, stem_category=hit["category"], source=source,
                          confidence=confidence, evidence=evidence, panel_category=panel_cat,
                          name_hit=key)


def catalog_summary():
    """Human-readable extent of the catalog, for the audit report."""
    cat = catalog()
    return {"families": len(cat["families"]),
            "categories": sorted(cat["categories"]),
            "names": sum(len(f["names"]) for f in cat["families"]),
            "regexes": sum(len(f["regex"]) for f in cat["families"]),
            "band_devices": len(cat["bands"])}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(catalog_summary(), ensure_ascii=False, indent=1))
    for probe in ("まばたき", "ウィンク右", "ｳｨﾝｸ２右", "あ", "あ２", "にやり", "真面目", "瞳小", "じと目",
                  "困る左", "上", "band7", "メガネ", "ぺろっ", "照れ", "涙", "口横広げ", "舌", "完全に未知の名前"):
        c = classify(probe)
        print("%-14r -> %-20s %-16s conf=%.2f  %s"
              % (probe, c.category, c.source, c.confidence,
                 c.evidence[0] if c.evidence else ""))
