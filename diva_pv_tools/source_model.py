"""Resolve a source model's morph definitions, then measure what they deform.

Two paths, and the report always says which one ran:

  * ``from_pmx(path)`` - parse the .pmx itself.  This is the high-quality path: it yields the
    vertex offsets of every vertex morph, the bone offsets of every bone morph, the UV offsets,
    the material deltas and the group composition, for a model that was never opened in Blender.
    It is validated against mmd_tools' own reader by ``tests`` on real models.

  * ``from_blender(context)`` - read the shape keys mmd_tools assembled.  mmd_tools stores
    ABSOLUTE coordinates in a shape key's points, so the delta is ``co - basis_co``.  This path
    only sees vertex morphs; a bone/UV/material/group morph has no shape key of its own (it is a
    vertex group or a shader node or a custom property), so those morphs are reported as
    ``blender_mmd_root`` with no geometry rather than being silently treated as an empty vertex
    morph.

The PMX reader here is written from the published PMX layout and verified byte-for-byte against
mmd_tools on the models on this machine; where it cannot proceed it raises `PmxError` naming the
offset instead of returning a partial model.  Two spots are known-not-settled and are handled
explicitly rather than guessed:

  * the padding inside a **flip** morph's offset table is not fully determined by the published
    layout (one real model carries 8 bytes that ``index_size + 4`` does not explain), so a flip
    morph's offsets are skipped and the record boundary is found by scanning for the next header.
    A flip only redirects another morph's value, so nothing measurable is lost;
  * the same fallback is available for any kind whose table does not land on a valid header.
"""
import math
import os
import struct

# PMX panel bytes / morph kinds, the specification's own numbering.
PANEL_NAMES = {0: "system", 1: "eyebrow", 2: "eye", 3: "mouth", 4: "other"}
MORPH_KINDS = {0: "group", 1: "vertex", 2: "bone", 3: "uv", 4: "uv1", 5: "uv2", 6: "uv3",
               7: "uv4", 8: "material", 9: "flip", 10: "impulse"}
_EXTRA_UV_KINDS = (3, 4, 5, 6, 7)


class PmxError(Exception):
    """A parse failure that names the byte offset, never a partial result."""


class _Cursor(object):
    __slots__ = ("b", "p")

    def __init__(self, blob):
        self.b = blob
        self.p = 0

    def take(self, n):
        if self.p + n > len(self.b):
            raise PmxError("wanted %d bytes at %d of %d" % (n, self.p, len(self.b)))
        out = self.b[self.p:self.p + n]
        self.p += n
        return out

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def i16(self):
        return struct.unpack("<h", self.take(2))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def f32(self):
        return struct.unpack("<f", self.take(4))[0]

    def fs(self, n):
        return struct.unpack("<%df" % n, self.take(4 * n))


def _index(cur, size, signed):
    if size == 1:
        return struct.unpack("<b", cur.take(1))[0] if signed else cur.u8()
    if size == 2:
        return cur.i16() if signed else cur.u16()
    return cur.i32() if signed else cur.u32()


class PmxSourceModel(object):
    """The parts of a PMX this project needs: vertex positions, bone rest positions, morphs."""

    __slots__ = ("path", "version", "encoding", "globals", "name", "name_en", "positions",
                 "uvs", "vertex_count", "bone_names", "bone_positions", "morphs",
                 "morph_by_name", "material_count", "texture_count", "warnings")

    def __init__(self, path):
        self.path = path
        self.warnings = []


def read_pmx(path, want_vertices=True, read_skeleton=False):
    """Parse the blocks of a .pmx that decide what a morph does.

    **Scope, stated plainly.**  This reader covers the header, the four model strings, the vertex
    block (positions and UVs), the face block, the texture list, the material list and the whole
    morph block.  It does **not** walk the bone block: several optional bone fields have a width
    the published layout does not pin down (a real model carries an extra float beside the
    additional-transform pair, and an IK record's rotation-constraint field reads as an integer
    on some files), and a wrong width there silently shifts every following block.  Rather than
    guess, the skeleton is skipped by locating the morph block from its own self-validating
    signature - a ``u32`` count followed by that many records that parse exactly - which is
    checkable and cannot drift.

    The skeleton is therefore only available through ``read_skeleton=True``, which raises
    `PmxError` naming the bone it could not read instead of returning a shifted model.
    `definitions_from_mmd_tools` is the primary path and does provide bone rest positions.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[:4] != b"PMX ":
        raise PmxError("%s: not a PMX (magic %r)" % (path, blob[:4]))
    cur = _Cursor(blob)
    m = PmxSourceModel(path)
    cur.take(4)
    m.version = cur.f32()
    ng = cur.u8()
    g = list(cur.take(ng))
    if len(g) < 8:
        raise PmxError("%s: header declares %d globals, need 8" % (path, len(g)))
    m.encoding = "utf-16-le" if g[0] == 0 else "utf-8"
    m.globals = {"additional_uv": g[1], "vertex": g[2], "texture": g[3], "material": g[4],
                 "bone": g[5], "morph": g[6], "rigid": g[7]}
    add_uv = g[1]
    vsize, tsize, msize, bsize, mosize, rsize = g[2], g[3], g[4], g[5], g[6], g[7]

    def text():
        n = cur.i32()
        if n < 0 or n > 1 << 24 or (m.encoding == "utf-16-le" and n % 2):
            raise PmxError("%s: implausible string length %d at %d" % (path, n, cur.p))
        return cur.take(n).decode(m.encoding)

    m.name = text()
    m.name_en = text()
    text()                                  # comment (local)
    text()                                  # comment (universal)

    m.vertex_count = cur.u32()
    positions = []
    uvs = []
    for i in range(m.vertex_count):
        positions.append(cur.fs(3))
        cur.fs(3)
        uvs.append(cur.fs(2))
        if add_uv:
            cur.fs(4 * add_uv)
        wt = cur.u8()
        if wt == 0:
            _index(cur, bsize, True)
        elif wt == 1:
            _index(cur, bsize, True); _index(cur, bsize, True); cur.f32()
        elif wt in (2, 4):
            for _ in range(4):
                _index(cur, bsize, True)
            cur.fs(4)
        elif wt == 3:
            _index(cur, bsize, True); _index(cur, bsize, True); cur.f32(); cur.fs(9)
        else:
            raise PmxError("%s: vertex %d has weight type %d" % (path, i, wt))
        cur.f32()
    m.positions = positions if want_vertices else []
    m.uvs = uvs if want_vertices else []

    n_faces = cur.u32()
    for _ in range(n_faces):
        _index(cur, vsize, False)

    m.texture_count = cur.u32()
    for _ in range(m.texture_count):
        text()

    m.material_count = cur.u32()
    for _ in range(m.material_count):
        text(); text()
        cur.fs(4); cur.fs(3); cur.f32(); cur.fs(3)
        cur.u8(); cur.fs(4); cur.f32()
        _index(cur, tsize, True); _index(cur, tsize, True)
        cur.u8()
        toon = cur.u8()
        if toon in (0, 1):
            _index(cur, tsize, True)
        else:
            cur.fs(3)
        text()
        cur.u32()

    n_bones = cur.u32()
    names = []
    bone_positions = []
    bone_start = cur.p
    if read_skeleton:
        for bi in range(n_bones):
            try:
                nm = text()
                text()
            except PmxError as exc:
                raise PmxError("%s: bone %d of %d: %s" % (path, bi, n_bones, exc))
            names.append(nm)
            bone_positions.append(cur.fs(3))
            _index(cur, bsize, True)
            cur.i32()
            flags = cur.u16()
            if flags & 0x0001:
                _index(cur, bsize, True)
            else:
                cur.fs(3)
            if flags & 0x0100 or flags & 0x0200:
                _index(cur, bsize, True); cur.f32()
            if flags & 0x0400:
                cur.fs(3)
            if flags & 0x0800:
                cur.fs(9)
            if flags & 0x2000:
                cur.i32()
            if flags & 0x0020:
                _index(cur, bsize, True); cur.i32(); cur.f32()
                n_links = cur.i32()
                if n_links < 0 or n_links > 65536:
                    raise PmxError("%s: bone %d declares %d IK links" % (path, bi, n_links))
                for _ in range(n_links):
                    _index(cur, bsize, True)
                    if cur.u8() == 1:
                        cur.fs(6)
    else:
        # locate the morph block instead of trusting a bone stride
        cur.p = _find_morph_block(blob, bone_start, m.encoding)
        m.warnings.append("the skeleton was not read (read_skeleton=False); the morph block was "
                          "located at %d by signature" % cur.p)
    m.bone_names = names
    m.bone_positions = bone_positions

    n_morphs = cur.u32()
    morphs = []
    for mi in range(n_morphs):
        nm = text()
        nm_en = text()
        panel = cur.u8()
        kind = cur.u8()
        count = cur.i32()
        mdef = {"index": mi, "name": nm, "name_en": nm_en, "panel": panel, "kind": kind,
                "kind_name": MORPH_KINDS.get(kind, "kind%d" % kind), "count": count,
                "vertex_offsets": [], "bone_offsets": [], "uv_offsets": [],
                "group_children": [], "group_weights": [], "material_offsets": [],
                "skipped": None}
        if kind == 1:
            for _ in range(count):
                mdef["vertex_offsets"].append((_index(cur, vsize, False),) + cur.fs(3))
        elif kind == 2:
            for _ in range(count):
                bi = _index(cur, bsize, True)
                mdef["bone_offsets"].append((bi,) + cur.fs(3) + cur.fs(4))
        elif kind in _EXTRA_UV_KINDS:
            for _ in range(count):
                mdef["uv_offsets"].append((_index(cur, vsize, False),) + cur.fs(4))
        elif kind == 0:
            for _ in range(count):
                mdef["group_children"].append(_index(cur, mosize, True))
                mdef["group_weights"].append(cur.f32())
        elif kind == 8:
            for _ in range(count):
                mdef["material_offsets"].append(
                    {"material": _index(cur, msize, True), "calc_mode": cur.u8(),
                     "diffuse": cur.fs(4), "specular": cur.fs(3), "specularity": cur.f32(),
                     "ambient": cur.fs(3), "edge_color": cur.fs(4), "edge_size": cur.f32(),
                     "texture": cur.fs(4), "sphere": cur.fs(4), "toon": cur.fs(4)})
        elif kind == 10:
            for _ in range(count):
                _index(cur, rsize, True); cur.u8(); cur.fs(6)
        else:
            # flip (9) and anything unrecognised: the table's exact padding is not determined by
            # the published layout, and a flip only redirects another morph's value, so skip to
            # the next record header instead of guessing a stride.
            end = _scan_next_header(blob, cur.p, m.encoding, n_morphs, mi)
            mdef["skipped"] = ("offset table of kind %s skipped (%d offset(s)); the next record "
                               "was located by header scan" % (mdef["kind_name"], count))
            m.warnings.append("%s: morph %r: %s" % (os.path.basename(path), nm, mdef["skipped"]))
            cur.p = end
            morphs.append(mdef)
            continue
        morphs.append(mdef)
    m.morphs = morphs
    by_name = {}
    for mdef in morphs:
        by_name.setdefault(mdef["name"], mdef)
        if mdef["name_en"]:
            by_name.setdefault(mdef["name_en"], mdef)
    m.morph_by_name = by_name
    return m


def _find_morph_block(blob, start, enc, limit=1 << 26):
    """Address of the morph block's count word, found by signature rather than by walking bones.

    A candidate is accepted only when the `u32` there is a plausible count AND **every one of the
    declared records** parses: strings decode, panel and kind are in range, and each offset table's
    stride lands exactly on the next record header while staying inside the file.  That is a
    demanding signature - a wrong address cannot satisfy it for a hundred records - and the caller
    re-walks the block anyway, so a mis-found address fails loudly instead of producing garbage.
    """
    lo = start
    hi = min(len(blob) - 8, start + limit)
    q = lo
    while q < hi:
        count = struct.unpack_from("<i", blob, q)[0]
        if 0 < count <= 20000:
            end = _walk_morph_table(blob, q + 4, count, enc)
            if end is not None:
                return q
        q += 1
    raise PmxError("no morph block found between %d and %d; the file either has no morphs or "
                   "uses a layout this reader cannot follow" % (lo, hi))


def _walk_morph_table(blob, p, count, enc):
    """Offset just past `count` morph records at `p`, or None if any record does not parse.

    Needs the header's index sizes, so it re-reads them from the blob rather than taking them as
    arguments - it is called before the main cursor has parsed them.
    """
    ng = blob[8]
    g = list(blob[9:9 + ng])
    if len(g) < 8:
        return None
    vsize, msize, bsize, mosize, rsize = g[2], g[4], g[5], g[6], g[7]
    per_of = {0: mosize + 4, 1: vsize + 12, 2: bsize + 28, 3: vsize + 16, 4: vsize + 16,
              5: vsize + 16, 6: vsize + 16, 7: vsize + 16, 8: msize + 91, 10: rsize + 25}
    for _ in range(count):
        if p + 6 > len(blob):
            return None
        try:
            n1 = struct.unpack_from("<i", blob, p)[0]
            if n1 < 0 or n1 > 400 or (enc == "utf-16-le" and n1 % 2) or p + 4 + n1 > len(blob):
                return None
            blob[p + 4:p + 4 + n1].decode(enc)
            q = p + 4 + n1
            n2 = struct.unpack_from("<i", blob, q)[0]
            if n2 < 0 or n2 > 400 or (enc == "utf-16-le" and n2 % 2) or q + 4 + n2 > len(blob):
                return None
            blob[q + 4:q + 4 + n2].decode(enc)
            q2 = q + 4 + n2
            panel, kind = blob[q2], blob[q2 + 1]
            cnt = struct.unpack_from("<i", blob, q2 + 2)[0]
            if panel > 7 or kind > 10 or cnt < 0 or cnt > 200000:
                return None
            p = q2 + 6
            if kind in per_of:
                p += cnt * per_of[kind]
                if p > len(blob):
                    return None
            else:
                return None                    # flip/impulse padding unsettled: not this candidate
        except (struct.error, UnicodeDecodeError, IndexError):
            return None
    return p


def _scan_next_header(blob, p, enc, total, index, limit=1 << 18):
    """Find the next address that reads as a morph record header, or raise."""
    q = p
    end = min(len(blob) - 14, p + limit)
    while q < end:
        try:
            n1 = struct.unpack_from("<i", blob, q)[0]
            if 0 < n1 <= 400 and (enc != "utf-16-le" or n1 % 2 == 0) and q + 4 + n1 <= len(blob):
                blob[q + 4:q + 4 + n1].decode(enc)
                q1 = q + 4 + n1
                n2 = struct.unpack_from("<i", blob, q1)[0]
                if 0 <= n2 <= 400 and (enc != "utf-16-le" or n2 % 2 == 0) and q1 + 4 + n2 <= len(blob):
                    blob[q1 + 4:q1 + 4 + n2].decode(enc)
                    q2 = q1 + 4 + n2
                    panel, kind = blob[q2], blob[q2 + 1]
                    cnt = struct.unpack_from("<i", blob, q2 + 2)[0]
                    if panel <= 7 and kind <= 10 and 0 <= cnt <= 200000:
                        return q
        except (UnicodeDecodeError, struct.error, IndexError):
            pass
        q += 1
    raise PmxError("morph %d: no following record header within %d bytes of %d"
                   % (index, limit, p))


# ------------------------------------------------------------------- definitions from a PMX
def definitions_from_pmx(model):
    """`{name: MorphDefinition}` from a parsed PMX, with group children resolved to names."""
    from . import mmd_morph

    defs = {}
    for mdef in model.morphs:
        d = mmd_morph.MorphDefinition(
            mdef["name"], mdef["name_en"], panel=mdef["panel"], kind=mdef["kind_name"],
            provenance=mmd_morph.PROVENANCE_PMX_BYTES, source_model=os.path.basename(model.path),
            source_index=mdef["index"])
        d.vertex_count = model.vertex_count
        d.vertex_offsets = list(mdef["vertex_offsets"])
        d.bone_offsets = [(model.bone_names[b] if 0 <= b < len(model.bone_names) else b,)
                          + tuple(rest) for b, *rest in
                          ((o[0],) + tuple(o[1:]) for o in mdef["bone_offsets"])]
        d.uv_offsets = list(mdef["uv_offsets"])
        d.material_offsets = list(mdef["material_offsets"])
        for child_index, ratio in zip(mdef["group_children"], mdef["group_weights"]):
            child = (model.morphs[child_index]["name"]
                     if 0 <= child_index < len(model.morphs) else "morph#%d" % child_index)
            d.group_children.append(child)
            d.group_weights.append(ratio)
        if mdef["skipped"]:
            d.notes.append(mdef["skipped"])
        defs[d.name] = d
        if d.name_en:
            defs.setdefault(d.name_en, d)
    return defs


def signatures_from_pmx(path, names=None, thresholds=None):
    """Measure every vertex morph of a PMX.  Returns (signatures, definitions, frame, report)."""
    from . import mmd_morph
    from . import morph_effect as fx

    model = read_pmx(path)
    frame = None
    frame_error = None
    try:
        frame = fx.frame_from_bones(model.bone_positions, model.bone_names, thresholds=thresholds)
    except fx.MissingFrame as exc:
        frame_error = str(exc)
    defs = definitions_from_pmx(model)
    sigs, notes = fx.analyse_definitions(defs, frame, model.positions, wanted=names)
    report = {"path": os.path.abspath(path), "model": model.name, "model_en": model.name_en,
              "version": model.version, "vertices": model.vertex_count,
              "bones": len(model.bone_names), "morphs": len(model.morphs),
              "kinds": _kind_histogram(model.morphs),
              "frame": frame.describe() if frame else None, "frame_error": frame_error,
              "measured": len(sigs), "not_measured": len(notes),
              "unmeasured": dict(sorted(notes.items())), "warnings": list(model.warnings)}
    return sigs, defs, frame, report


def _kind_histogram(morphs):
    out = {}
    for m in morphs:
        out[m["kind_name"]] = out.get(m["kind_name"], 0) + 1
    return out


# --------------------------------------------------------------- definitions from Blender
def definitions_from_mmd_tools(path, want_vertices=True):
    """Morph definitions from mmd_tools' own PMX reader, without touching the Blender scene.

    `mmd_tools.core.pmx` is a plain data reader - it takes a file path and fills Python objects,
    and it is the same reader this project already depends on for VMD import.  Using it here is
    not a shortcut around measurement: it is the reference implementation, and the tests compare
    every morph of every model on this machine between it and this module's own byte reader, so
    a disagreement is caught rather than shipped.

    mmd_tools does not represent PMX 2.1 flip/impulse morphs at all (its constructor map stops at
    material), so a model containing one cannot be read by it - that case is reported as
    `mmd_tools_stopped_at` together with the morphs it did read, and the caller can fall back to
    `read_pmx`.
    """
    from bl_ext.blender_org.mmd_tools.core import pmx as _pmx

    fs = _pmx.FileReadStream(path)
    try:
        header = _pmx.Header()
        header.load(fs)
        fs.setHeader(header)
        model = _pmx.Model()
        stopped_at = None
        morphs_read = 0
        try:
            model.load(fs)
        except Exception as exc:                      # flip/impulse are absent from mmd_tools
            stopped_at = "%s: %s" % (type(exc).__name__, exc)
            morphs_read = len(getattr(model, "morphs", []) or [])
    finally:
        fs.close()

    from . import mmd_morph

    positions = [tuple(v.co) for v in model.vertices] if want_vertices else []
    bone_names = [b.name for b in model.bones]
    bone_positions = [tuple(b.location) for b in model.bones]
    defs = {}
    kind_of = {"GroupMorph": "group", "VertexMorph": "vertex", "BoneMorph": "bone",
               "MaterialMorph": "material"}
    warnings = []
    for mo in model.morphs:
        cls = type(mo).__name__
        kind = kind_of.get(cls, "uv" if cls == "UVMorph" else cls)
        d = mmd_morph.MorphDefinition(
            mo.name, getattr(mo, "name_e", ""), panel=getattr(mo, "category", None), kind=kind,
            provenance=mmd_morph.PROVENANCE_PMX_BYTES, source_model=os.path.basename(path),
            source_index=len(defs))
        d.vertex_count = len(positions) or None
        if kind == "vertex":
            d.vertex_offsets = [(int(o.index),) + tuple(o.offset) for o in mo.offsets]
        elif kind == "uv":
            d.uv_offsets = [(int(o.index),) + tuple(o.offset) for o in mo.offsets]
            d.notes.append("additional UV channel %s"
                           % getattr(mo, "uv_index", "?"))
        elif kind == "bone":
            for o in mo.offsets:
                bi = int(o.index)
                name = bone_names[bi] if 0 <= bi < len(bone_names) else "bone#%d" % bi
                d.bone_offsets.append((name,) + tuple(o.location_offset)
                                      + tuple(o.rotation_offset))
        elif kind == "group":
            for o in mo.offsets:
                ci = int(o.index)
                child = (model.morphs[ci].name if 0 <= ci < len(model.morphs)
                         else "morph#%d" % ci)
                d.group_children.append(child)
                d.group_weights.append(float(o.factor))
        elif kind == "material":
            d.material_offsets = [{"material": int(getattr(o, "index", -1))} for o in mo.offsets]
        defs[d.name] = d
        if d.name_en:
            defs.setdefault(d.name_en, d)
    report = {"path": os.path.abspath(path), "reader": "mmd_tools",
              "encoding": header.encoding.charset, "version": header.version,
              "vertices": len(positions), "bones": len(bone_names),
              "morphs": len(model.morphs), "mmd_tools_stopped_at": stopped_at,
              "morphs_read_before_stop": morphs_read or len(model.morphs),
              "index_sizes": {"vertex": header.vertex_index_size, "bone": header.bone_index_size,
                              "morph": header.morph_index_size},
              "kinds": _kind_histogram_mmd(model.morphs), "warnings": warnings}
    return defs, positions, bone_names, bone_positions, report


def _kind_histogram_mmd(morphs):
    out = {}
    for mo in morphs:
        cls = type(mo).__name__
        out[cls] = out.get(cls, 0) + 1
    return out


def load_source(path, prefer="mmd_tools"):
    """Best available source: mmd_tools' reader by default, this module's own bytes as fallback.

    Returns ``(definitions, positions, frame, report)``.  A model mmd_tools cannot finish reading
    (PMX 2.1 flip/impulse) is retried with the byte reader, and the report records which reader
    produced the result and why - so an audit can always tell how a definition was obtained.
    """
    from . import morph_effect as fx

    report = {}
    defs = positions = None
    bone_names = bone_positions = None
    if prefer == "mmd_tools":
        try:
            defs, positions, bone_names, bone_positions, report = definitions_from_mmd_tools(path)
        except Exception as exc:
            report = {"path": os.path.abspath(path), "reader": "mmd_tools",
                      "failed": "%s: %s" % (type(exc).__name__, exc)}
    if not defs:
        model = read_pmx(path)
        defs = definitions_from_pmx(model)
        positions = model.positions
        bone_names = model.bone_names
        bone_positions = model.bone_positions
        report = {"path": os.path.abspath(path), "reader": "diva_pv_tools.source_model",
                  "model": model.name, "model_en": model.name_en, "version": model.version,
                  "vertices": model.vertex_count, "bones": len(bone_names),
                  "morphs": len(model.morphs), "kinds": _kind_histogram(model.morphs),
                  "warnings": list(model.warnings),
                  "fallback_because": report.get("failed") or report.get("mmd_tools_stopped_at")}
    frame = None
    frame_error = None
    try:
        frame = fx.frame_from_bones(bone_positions, bone_names)
    except fx.MissingFrame as exc:
        frame_error = str(exc)
    report["frame"] = frame.describe() if frame else None
    report["frame_error"] = frame_error
    return defs, positions, frame, report


def definitions_from_blender(mesh_object, armature_object=None, include_non_vertex=True):
    """Definitions from the scene mmd_tools assembled: shape keys are vertex morphs.

    Returns ``(definitions, positions, frame, report)``.  A shape key's points hold ABSOLUTE
    coordinates (mmd_tools copies the basis and adds the PMX offset), so the delta is
    ``co - basis_co``; getting that subtraction wrong is the classic way to turn every morph into
    a whole-model translation, so it is done here and asserted by the tests.

    Morphs that mmd_tools did not turn into shape keys (bone, UV, material, group) are listed from
    the MMD root object's custom properties when `include_non_vertex` is set, marked with
    provenance ``blender_mmd_root`` and carrying **no** geometry - so nothing downstream can
    mistake a name for a measurement.
    """
    import bpy  # noqa: F401  (only imported on this path, which is Blender-only by definition)

    from . import mmd_morph

    mesh = mesh_object.data
    positions = [tuple(v.co) for v in mesh.vertices]
    defs = {}
    report = {"mesh": mesh_object.name, "vertices": len(positions), "shape_keys": 0,
              "non_vertex_morphs": [], "warnings": []}

    keys = mesh.shape_keys
    if keys is not None and keys.key_blocks:
        basis = keys.key_blocks[0]
        basis_co = [tuple(p.co) for p in basis.data]
        report["shape_keys"] = len(keys.key_blocks) - 1
        report["basis"] = basis.name
        for block in keys.key_blocks[1:]:
            deltas = [(p.co[0] - basis_co[i][0], p.co[1] - basis_co[i][1],
                       p.co[2] - basis_co[i][2]) for i, p in enumerate(block.data)]
            d = mmd_morph.MorphDefinition(
                block.name, provenance=mmd_morph.PROVENANCE_BLENDER_SHAPEKEY,
                source_model=mesh_object.name)
            d.vertex_count = len(positions)
            d.vertex_offsets = [(i, dv[0], dv[1], dv[2]) for i, dv in enumerate(deltas)]
            d.notes.append("delta computed as shape_key.co - basis.co (mmd_tools stores absolute "
                           "coordinates)")
            defs[block.name] = d

    if include_non_vertex:
        root = None
        for obj in getattr(mesh_object, "users_collection", []) or []:
            for cand in obj.objects:
                if getattr(cand, "mmd_type", "") == "ROOT":
                    root = cand
                    break
            if root:
                break
        mmd_root = getattr(root, "mmd_root", None)
        if mmd_root is not None:
            for prop, kind in (("vertex_morphs", "vertex"), ("uv_morphs", "uv"),
                               ("bone_morphs", "bone"), ("material_morphs", "material"),
                               ("group_morphs", "group")):
                for entry in getattr(mmd_root, prop, []) or []:
                    if entry.name in defs:
                        if kind != "vertex":
                            defs[entry.name].notes.append(
                                "mmd_tools also lists this as a %s morph" % kind)
                        continue
                    d = mmd_morph.MorphDefinition(
                        entry.name, panel=getattr(entry, "category", None), kind=kind,
                        provenance=mmd_morph.PROVENANCE_BLENDER_PROP,
                        source_model=mesh_object.name)
                    d.notes.append("mmd_tools models this as a %s morph with no shape key, so its "
                                   "geometry is NOT available on the Blender path" % kind)
                    defs[entry.name] = d
                    report["non_vertex_morphs"].append(entry.name)
        else:
            report["warnings"].append("no MMD root object was found, so bone/UV/material/group "
                                      "morphs cannot be listed at all on this path")

    frame = None
    if armature_object is not None:
        try:
            from . import morph_effect as fx
            names = [b.name for b in armature_object.data.bones]
            pos = [tuple(armature_object.matrix_world @ b.head_local)
                   for b in armature_object.data.bones]
            frame = fx.frame_from_bones(pos, names)
        except Exception as exc:                      # a missing eye bone is not a crash
            report["warnings"].append("could not build the interocular frame: %s" % exc)
    return defs, positions, frame, report


def measure(definitions, positions, frame, wanted=None, kinds=("vertex",)):
    """Signatures + the reasons some morphs could not be measured."""
    from . import morph_effect as fx
    return fx.analyse_definitions(definitions, frame, positions, wanted=wanted, kinds=kinds)


def compare_models(source, target):
    """``compare_models((sigs_a, frame_a, pos_a), (sigs_b, frame_b, pos_b))`` -> ranked pairs.

    Both sides must have a frame; a missing frame means the regional scores are not comparable
    and the comparison is refused rather than approximated.
    """
    from . import morph_effect as fx
    sigs_a, frame_a, _ = source
    sigs_b, frame_b, _ = target
    if frame_a is None or frame_b is None:
        raise fx.MissingFrame("both models need an interocular frame for a comparison")
    rows = []
    for name_a, sig_a in sigs_a.items():
        best = None
        for name_b, sig_b in sigs_b.items():
            cmp = fx.compare(sig_a, sig_b)
            if cmp.get("distance") is None:
                continue
            if best is None or cmp["distance"] < best[1]["distance"]:
                best = (name_b, cmp)
        if best:
            rows.append({"source": name_a, "target": best[0], **best[1]})
    rows.sort(key=lambda r: r["distance"])
    return rows


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg not in sys.path:
        sys.path.insert(0, pkg)
    if len(sys.argv) > 1:
        sigs, defs, frame, report = signatures_from_pmx(sys.argv[1])
        print(json.dumps(report, ensure_ascii=False, indent=1))
        best = sorted(sigs.items(), key=lambda kv: -kv[1].group_score("eye"))[:3]
        for name, sig in best:
            print("%-16r dominant=%-8s eye=%.2f mouth=%.2f brow=%.2f lr=%s"
                  % (name, sig.dominant_group, sig.group_score("eye"), sig.group_score("mouth"),
                     sig.group_score("eyebrow"), sig.lr_symmetry))
