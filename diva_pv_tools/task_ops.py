"""The four DIVA-side long operations: import a VMD, write a mot set, a camera, a song, a face.

Motion Cleanup lives in the companion add-on `motion_refinery`; what stays here are the
operations that produce the files the game loads.  All of them are `Operation` subclasses - see
`operation.py` for the step model and the cancel/cleanup contract - and `task_ops.Operation` keeps
resolving for code that imports it by that name.

The mot-set export applies no reach clamp: measuring a leg from `kg_hara_y` puts the foot outside
SEGA's own span on most frames of an ordinary dance, so a global clamp there raises its own
assertion instead of pulling anything in.  Reach editing lives in `motion_refinery` as an edit of
the *action*, sized from the rig's own bones.
"""
import gc
import math
import os
import time

import bpy

from . import task_core as tc
from .operation import (UNIT_BUDGET, OffThread, Operation, _unbudgeted,  # noqa: F401
                        log_benchmark, run_to_completion)

# ---------------------------------------------------------------------------- VMD import
class VmdImportResult(object):
    """What an import produced, in the one shape the operator and the tests both read."""

    __slots__ = ("action", "frames", "bones", "records", "morphs", "ik_curves", "unmapped",
                 "dialect", "path")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))


class VmdImportOperation(Operation):
    """Load a 30 fps MMD motion onto the Import Rig, in time-bounded pieces.

    **Why this is not a call to `mmd_tools`.**  `VMDImporter.assign()` runs to completion inside one
    Python frame - no progress, no cancel and no way to yield.  Wrapping it in a modal operator would
    change nothing: the freeze is inside the call, not around it.

    So the record-to-curve half is done here.  The parts that carry mmd_tools' own conventions are
    still borrowed from it rather than guessed at, because getting them wrong is how a dance imports
    looking almost right - the bone transform convention (`BoneConverter`: the armature-space matrix
    with Y and Z swapped, transposed, quaternion-sandwiched) is imported from the installed extension.
    What is reimplemented is only the loop structure around it, which is exactly the part that had to
    become interruptible.

    The four things it has to get right, all of them checked against mmd_tools' own output:

      * **the time base.**  `VMDImporter.__frame_start` is `scene.frame_current` ADDED to every key,
        and `frame_margin` applies when that is 0 or 1.  The add-on parks the scene on frame 0 and
        passes `frame_margin=0`, so both are zero and a key at source frame 5942 lands on scene frame
        5942.  The template's 1:2 time base is a *scene* setting, not something the curves carry.
      * **the quaternion sign.**  Consecutive keys are made sign-compatible against the previous
        converted rotation, not against the raw one; without it a dance flickers through the long way
        round at every 180-degree crossing.
      * **the interpolation.**  `bezier` in a VMD is four control values per curve index, and
        mmd_tools' own `interp` is all zeros on this data because it does not parse them, so the
        handles come from the `[20, 20, 107, 107]` fallback - which is also what it uses whenever a
        value barely moves.  Both branches are reproduced; `\u81ea\u52d5` handles are not, because the
        source explicitly sets FREE.
      * **the IK toggle curves.**  `mmd_ik_toggle` is a boolean custom property, and those keys are
        the difference between a dance whose legs bend and one whose legs are locked straight.
    """

    name = "Import motion"

    # Measured on the reference dance (30.5 MB): parsing and key writing dominate by a wide margin.  The weights
    # follow that ratio rather than an even split, because a bar that
    # sits at 50% through 90% of the work is a bar that lies.
    WEIGHTS = [("prepare", 0.002), ("read", 0.002), ("parse", 0.077), ("map bones", 0.006),
               ("prepare curves", 0.004), ("write keys", 0.894), ("ik toggles", 0.008),
               ("finish", 0.009)]

    def __init__(self, context=None, task=None, rig=None, options=None):
        Operation.__init__(self, context, task, options)
        self.rig = rig
        self.path = (options or {}).get("path", "")
        self.scale = float((options or {}).get("scale", 0.08))
        self.clear_first = bool((options or {}).get("clear_first", False))
        self.result = None
        self._previous_action = None
        self._created_action = None

    # ------------------------------------------------------------------ stages
    def prepare(self):
        from . import vmd_reader
        self.start_frame = 0
        self.blob = None
        self.info = None
        self.tracks = None
        self.order = None
        self.written = None
        self.state["vmd_reader"] = vmd_reader

    def prepare_stage(self):
        """Everything that must be true before a byte is read, so a bad input costs nothing."""
        from . import vmd_reader
        scene = self.context.scene
        if self.rig is None:
            raise ValueError("no Import Rig")
        path = self.path
        if not path:
            raise ValueError("no motion file was chosen")
        if not os.path.isfile(path):
            raise ValueError("no such file: %s" % path)
        if getattr(scene, "diva_import_clear", False):
            self._previous_action = self.rig.animation_data.action if self.rig.animation_data \
                else None
            # Clearing matters for the datablock name: `bpy.data.actions.new("%s_bone")` comes out
            # "Motion_bone.001" if the previous import is still held, and then every later stage in the
            # project - the optimizer's action name, the A/B rigs, the export report - carries the
            # suffix.  The action is removed after the new one is attached, not here, so a cancel in
            # between leaves the rig exactly as it was.
            self.rig.animation_data_clear()
        # The scene's time base is set before the curves exist, because it is what the rest of the
        # pipeline reads to decide whether the dance is running at the right speed.  A file left at
        # 30 fps with no stretch evaluates the same curves at double speed and then holds the last
        # pose, so this is not cosmetic.
        scene.frame_start = 0
        scene.render.fps = 60
        scene.render.frame_map_old = 30
        scene.render.frame_map_new = 60
        scene.frame_set(0)
        self.state["scene_ready"] = True
        yield None

    def _read(self, path):
        """Read the whole file.  One unit, because the read itself is 27 ms and chunking it is theatre."""
        with open(path, "rb") as fh:
            return fh.read()

    def read_stage(self):
        from . import vmd_reader
        path = self.path
        size = os.path.getsize(path)

        def _read():
            self.blob = self._read(path)

        yield _read
        if len(self.blob) != size:
            raise ValueError("%s: read %d of %d bytes" % (path, len(self.blob), size))
        self.state["bytes"] = size
        if self.task is not None:
            self.task.note("%s, %.1f MB" % (os.path.basename(path), size / 1e6))

    def parse_stage(self):
        """Settle the dialect, then walk the records in groups.

        The dialect has to be settled before any record is handed out: `vmd_reader.read` decides it by
        trying the classic layout, discovering half way through that the quaternions are not unit
        length, and restarting - which is impossible once a consumer has seen records.  `detect` asks
        the same question from the headers and a fixed-size sample of the records, so `records` can
        commit to an answer.
        """
        from . import vmd_reader
        self.info = vmd_reader.detect(self.blob)
        if self.info is None:
            magic = self.blob[:30].rstrip(b"\x00").decode("ascii", "replace")
            raise ValueError("%s: no VMD dialect chain reaches EOF (magic=%r)"
                             % (self.path, magic))
        if self.task is not None:
            self.task.note("%s: %d bone record(s), %d morph record(s)"
                           % (self.info["dialect"], self.info["bones"], self.info["morphs"]))
        bones = []
        morphs = []
        properties = []

        def _group(kind, batch):
            if kind == "bones":
                bones.extend(batch)
            elif kind == "morphs":
                morphs.extend(batch)
            else:
                properties.extend(batch)

        for kind, batch in vmd_reader.records(self.blob, self.info):
            # one group per unit: the reader already slices into groups of 4096, which on this file
            # is 67 units for 272k records - coarse enough that the per-unit overhead disappears and
            # fine enough that a cancel lands inside a fraction of a second
            yield self.invoke(lambda _k=kind, _b=batch: _group(_k, _b))
        if len(bones) != self.info["bones"] or len(morphs) != self.info["morphs"]:
            raise ValueError("parsed %d/%d bone and %d/%d morph record(s)"
                             % (len(bones), self.info["bones"], len(morphs), self.info["morphs"]))
        self.state["bone_records"] = bones
        self.state["morph_records"] = morphs
        self.state["properties"] = properties
        # Group into per-bone tracks.  This reads the file directly rather than the per-record dicts
        # above, and it is stepped: the whole-file form is 98 ms on the reference dance (272,681 records), far too
        # long for a single scheduler unit.
        tracker = group_tracks_units(self.blob, self.info)
        while True:
            try:
                unit = next(tracker)
            except StopIteration as stop:
                self.tracks, self.order = stop.value
                break
            yield unit
        self.state["track_count"] = len(self.order)

    def map_stage(self):
        """Resolve each VMD bone name to a pose bone, and count the work that is coming.

        The count is what makes the progress bar determinate from here on: keys times curves is known
        exactly once the bone set is known, so there is no reason to show an indeterminate bar for the
        ninety percent of the operation that is the writing.
        """
        self.mapper = _bone_mapper(self.rig)
        self.matched = []
        self.unmapped = []
        for name in self.order:
            pb = self.mapper.get(name)
            if pb is None:
                self.unmapped.append(name)
                continue
            self.matched.append((name, pb))
        total = 0
        for name, _pb in self.matched:
            total += 7 * self.tracks[name]["count"]
        self.state["key_total"] = total
        if self.task is not None:
            self.task.set_total(total)
            self.task.note("%d of %d track(s) matched, %d key(s) to write"
                           % (len(self.matched), len(self.order), total))
        if not self.matched:
            raise ValueError("none of the %d track(s) in %s match a bone on %s"
                             % (len(self.order), os.path.basename(self.path), self.rig.name))
        yield None

    def curves_stage(self):
        """Create the action and every F-curve, before a single key is written.

        Curves first, keys second, and they are separate stages because they fail differently: a curve
        that cannot be created is a rig/bone-name problem and is worth reporting before the long
        write stage, while a key that cannot be written is a data problem.  Creating them all
        up front also means the write loop does no `fcurves.find` at all, which at 7 curves per bone
        is 546 lookups this file does not have to do.

        `fcurves.new(..., action_group=)` is not used, for the reason `motion_blend.carry_channels`
        documents: it raises TypeError for the boolean custom property `mmd_ik_toggle`.  Groups are
        attached the way mmd_tools attaches them, by finding or creating the group and assigning it.
        """
        action = bpy.data.actions.new(name="%s_bone" % self.rig.name)
        self._created_action = action
        self.track_action(action)
        curves = {}
        for name, pb in self.matched:
            pb.rotation_mode = 'QUATERNION'
            path = 'pose.bones["%s"]' % pb.name
            curves[name] = {
                "loc": [_get_or_create(action, path + ".location", i, pb.name) for i in range(3)],
                "rot": [_get_or_create(action, path + ".rotation_quaternion", i, pb.name)
                        for i in range(4)],
            }
        self.state["action"] = action
        self.state["curves"] = curves
        # The interpolation controls.  `bezier_for` needs a converter, and a converter needs a pose
        # bone; the source file's own interpolation bytes are not parsed into these records (neither
        # does mmd_tools), so what every curve gets is its `[20, 20, 107, 107]` fallback - computed
        # through the same helper rather than hard-coded, so a future reader that does parse them only
        # has to fill `track["interp"]`.
        from . import vmd_tracks as vt
        self.state["bezier"] = vt.bezier_for(
            vt.converter_for(self.matched[0][1], self.scale),
            self.tracks[self.matched[0][0]].get("interp"), 0)
        # the enum integers `foreach_set` needs, read once from the RNA rather than once per curve
        self.state["codes"] = vt.keyframe_enums(curves[self.matched[0][0]]["rot"][0])
        yield None

    def write_stage(self):
        """Convert and write one bone's whole track per unit.

        One bone, not one frame, and the reason is the converter: `BoneConverter` is built per pose
        bone from its rest matrix, and the sign-compatibility of consecutive quaternions is a running
        value across the track.  Both belong to the bone, so the bone is the smallest unit that does
        not either rebuild the converter or lose the running sign.  Measured at ~50 ms for the largest
        track on the reference dance (5943 keys x 7 curves), which is two to three scheduler chunks - the bar stays
        live and the engine comes back between bones.
        """
        from . import vmd_tracks as vt
        curves = self.state["curves"]
        written = [0]

        def write_one(name, pb):
            track = self.tracks[name]
            converter = vt.converter_for(pb, self.scale)
            frames = track["frames"]
            count = len(frames)
            if count == 0:
                return
            pos = vt.convert_locations(converter, track["loc"])
            quats = vt.convert_rotations(converter, track["rot"])
            vt.make_compatible(quats)
            series = [pos[:, axis] for axis in range(3)] + [quats[:, axis] for axis in range(4)]
            # Mass insert rather than `keyframe_points.insert` per key.  `add()` grows the array and
            # `fill_curve` writes it with one `foreach_set` per array, because `insert()`
            # binary-searches the existing keys on every call, and the per-key
            # handle loop is 31.6 ms of a 970-key track on its own.
            for axis, fc in enumerate(curves[name]["loc"] + curves[name]["rot"]):
                fc.keyframe_points.add(count)
                vt.fill_curve(fc, frames, series[axis], self.state["bezier"], self.state["codes"])
            written[0] += count * 7

        for name, pb in self.matched:
            yield self.invoke(lambda _n=name, _p=pb: write_one(_n, _p))
        self.state["keys_written"] = written[0]

    def ik_stage(self):
        """The `mmd_ik_toggle` curves, which decide whether the legs bend at all.

        A boolean custom property, so each one is a single-key curve: `keyframe_points.insert` is
        never used and neither is a group, because `fcurves.new(..., action_group=)` raises TypeError
        for a boolean path.  The first frame's states are also written onto the bones themselves,
        which is what mmd_tools does and what makes the rig evaluate the same pose the dance starts in
        before the timeline is scrubbed.
        """
        properties = self.state.get("properties") or []
        action = self.state["action"]
        if not properties:
            yield None
            return
        first_frame = float("inf")
        first_states = {}
        for record in properties:
            if record["frame"] <= first_frame:
                first_frame = record["frame"]
                for name, enable in record["ik"]:
                    first_states[name] = enable

        def _write():
            for record in properties:
                for ik_name, enable in record["ik"]:
                    pb = self.mapper.get(ik_name)
                    if pb is None:
                        continue
                    path = 'pose.bones["%s"].mmd_ik_toggle' % pb.name
                    fc = action.fcurves.find(path, index=0)
                    if fc is None:
                        fc = action.fcurves.new(path, index=0)
                    fc.keyframe_points.insert(float(record["frame"]), 1.0 if enable else 0.0,
                                              options={'FAST'})
            for fc in action.fcurves:
                if fc.data_path.endswith("mmd_ik_toggle"):
                    fc.update()

        yield self.invoke(_write)
        for name, enable in first_states.items():
            pb = self.mapper.get(name)
            if pb is not None and getattr(pb, "mmd_ik_toggle", None) != enable:
                pb.mmd_ik_toggle = enable
        self.state["ik_frames"] = len(properties)

    def finish_stage(self):
        """Attach the action and report the frames."""
        scene = self.context.scene
        action = self.state["action"]
        if self.rig.animation_data is None:
            self.rig.animation_data_create()
        if self._previous_action is not None and self._previous_action.users == 0:
            # Drop the datablock as well, or `bpy.data.actions.new()` comes out "Motion_bone.001" and
            # the orphan of every previous import stays in the file.
            try:
                bpy.data.actions.remove(self._previous_action, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass
        self.rig.animation_data.action = action
        # Walked per bone rather than with one set comprehension over every matched track: on the reference dance
        # that comprehension reads 272,681 frame numbers in one 47 ms gap - the "stages are chunked
        # but a stage is not" defect this design guards against.
        seen = set()

        def gather(name):
            seen.update(int(f) for f in self.tracks[name]["frames"])

        for name, _pb in self.matched:
            yield self.invoke(lambda _n=name: gather(_n))
        frames = sorted(seen)
        last = frames[-1] if frames else 0
        # frame_end comes from the SONG length, not the last motion key: shrinking it would cut the
        # export short and silently change an accepted file, so the range only ever grows.
        if last * 2 > scene.frame_end:
            scene.frame_end = last * 2
        scene.frame_set(scene.frame_end)
        self.context.view_layer.update()
        self.result = VmdImportResult(
            action=action, frames=frames, bones=len(self.matched), records=len(self.state
                                                                              ["bone_records"]),
            morphs=len(self.state.get("morph_records") or []),
            ik_curves=0, unmapped=self.unmapped, dialect=self.info["dialect"], path=self.path)
        yield None

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.prepare_stage, self.read_stage, self.parse_stage, self.map_stage,
                           self.curves_stage, self.write_stage, self.ik_stage, self.finish_stage))]

    # ------------------------------------------------------------------ lifecycle
    def commit(self):
        """Nothing to promote: the action was made active in `finish_stage`.

        An import has no "original" to keep - the whole point is to replace what the rig was playing -
        so the transaction here is only about cleanliness: this marks the operation as one whose
        result may stay, which is what stops `abort` from removing the action on the way out.
        """
        self._committed = True

    def describe(self):
        if self.result is None:
            return "nothing was imported"
        return ("imported %s: %d bone(s), %d record(s), %d frame(s), %d unmapped track(s)"
                % (os.path.basename(self.result.path), self.result.bones, self.result.records,
                   len(self.result.frames), len(self.result.unmapped)))


_CHANNEL_COUNTS = {"Position": 3, "Rotation": 3, "PositionRotation": 6, "GlobalPosition": 3,
                   "GlobalRotation": 3, "HeadIKTargetRotation": 6, "ArmIKTargetRotation": 6,
                   "LegIKTargetRotation": 6}


def _channel_count(bones_data):
    """How many numeric channels the bones will produce, for a determinate progress total.

    A bone type the table does not know contributes nothing, which is what `channel_units` does with
    it too - the count and the walk have to agree or the bar ends somewhere other than 100%.
    """
    return sum(_CHANNEL_COUNTS.get(bone.get("Type"), 0) for bone in bones_data)


def _get_or_create(action, data_path, index, group_name):
    """One F-curve, created in the right group.  `action_group=` is avoided on purpose."""
    fc = action.fcurves.find(data_path, index=index)
    if fc is None:
        fc = action.fcurves.new(data_path, index=index)
    if group_name and (fc.group is None or fc.group.name != group_name):
        group = None
        for g in action.groups:
            if g.name == group_name:
                group = g
                break
        if group is None:
            group = action.groups.new(group_name)
        fc.group = group
    return fc


def _bone_mapper(armature):
    """MMD bone name -> pose bone, using mmd_tools' own table so the names agree with its importer.

    `BoneNameMapper` also handles the `_l` / `_r` suffix convention and the Japanese-to-English
    translation table, and reimplementing that would be a second source of truth for which bone
    "左ひじ" is.  Falls back to a plain name lookup when mmd_tools is not installed, because the rest
    of the import still works for a rig whose bones are named the MMD way.
    """
    try:
        from bl_ext.blender_org.mmd_tools.core.vmd.importer import BoneNameMapper
    except ImportError:
        try:
            from mmd_tools.core.vmd.importer import BoneNameMapper
        except ImportError:
            BoneNameMapper = None
    if BoneNameMapper is not None:
        return BoneNameMapper(armature)
    return armature.pose.bones


def group_tracks_units(blob, info, group=8192):
    """Per-bone tracks, built in time-bounded pieces, with the finished mapping as the return value.

    The whole-file form of this is 98 ms on the reference dance (272,681 records), far too long for one unit, and
    it is two separable things: a per-record name lookup and one vectorised
    column read.  The lookup is the expensive half and is done here, in groups; the columns are one
    pass and are charged to the group that happens to be current.

    The per-record name lookup is a registry hit keyed on the raw 15 bytes, not a decode: a VMD names
    the same bone on every one of its keys, so the reference dance's 272,681 records contain 207 distinct names.
    Decoding each one was 60 ms of the 98.

    The result - `({name: {"frames","loc","rot","count"}}, [name in first-seen order])` - is returned,
    so a caller cannot observe a half-built mapping: a suspension between two groups leaves arrays that
    no later stage reads.
    """
    import numpy as np
    from . import vmd_reader as vr
    bs, bfr, bpos, brot, brotn = (info["bs"], info["bfr"], info["bpos"], info["brot"],
                                  info["brotn"])
    base = vr.BONE_BASE
    n = info["bones"]
    width = info["bn"]
    registry = {}
    frames = np.empty(n, dtype=np.int64)
    buckets = {}
    order = []
    out = {}

    def resolve(_start, _end):
        """Name the records in one group and file their indices under that bone.

        The bucketing is charged to the group rather than to a pass over the whole file at the end:
        a single loop over 272,681 names is 40 ms on its own, which is the same defect one level down.
        """
        for i in range(_start, _end):
            off = base + i * bs
            raw = blob[off:off + width]
            name = registry.get(raw)
            if name is None:
                name = registry[raw] = vr._name(raw)
                buckets[name] = []
                order.append(name)
            buckets[name].append(i)
            frames[i] = int.from_bytes(blob[off + bfr:off + bfr + 4], "little")

    def assemble(name):
        """One bone's track: gather its columns, order them by frame, and slice them out."""
        idx = np.asarray(buckets[name], dtype=np.int64)
        # a stable sort keeps the file's own order for two keys on the same frame, which is what
        # mmd_tools' `keyFrames.sort(key=frame_number)` does - Python's sort is stable too
        idx = idx[np.argsort(frames[idx], kind="stable")]
        out[name] = {"frames": frames[idx].tolist(), "loc": pos[idx].copy(),
                     "rot": rot[idx].copy(), "count": len(idx)}

    for start in range(0, n, group):
        yield lambda _s=start: resolve(_s, min(_s + group, n))
    # the columns are gathered here, where every name is already known, rather than inside the loop:
    # `np.frombuffer` has no `step` and a strided `view` is rejected, so the bytes are copied into the
    # shape the view needs.  One copy of one column beats one attribute access per record by a wide
    # margin at 272k records, and the reshape below is a view.
    rows = np.frombuffer(blob, dtype=np.uint8, count=n * bs, offset=base).reshape(n, bs)
    pos = np.ascontiguousarray(rows[:, bpos:bpos + 12]).view("<f4").reshape(n, 3).astype(np.float64)
    rot = (np.ascontiguousarray(rows[:, brot:brot + 4 * brotn]).view("<f4")
           .reshape(n, brotn).astype(np.float64))
    for name in order:
        yield lambda _n=name: assemble(_n)
    return out, order


# ---------------------------------------------------------------------------- mot set export
class MotionExportOperation(Operation):
    """Write the Export Rig's pose per frame to a Project DIVA mot set, in time-bounded pieces.

    Measured on the reference dance (11,885 frames, 212 bones, 1,996,715 keys written, 12.0 MB out):

        collect (frame loop)   38.33 s   76.1%      <- scene.frame_set, 3.23 ms/frame
        process_bones          11.52 s   22.9%
        write_mot_bin           0.48 s    1.0%
        read_data               0.00 s
        ------------------------------------------
        total                  50.34 s

    The profile decides the boundaries.  The frame loop is 76% of the run and cannot be made cheaper -
    94% of a frame is Blender evaluating the depsgraph, not this plugin - so it is chunked by frame
    group and nothing else about it is changed.  `process_bones` is a per-bone transform of a whole
    track, so the bone is its unit.  The write is 0.5 s of `struct.pack` over two million values, which
    is one unit with the intermediate file reported while it runs, as allowed for an
    atomic operation that is not the dominant cost.

    **The output is written to `<name>.part` and renamed on success.**  A cancel or a failure therefore
    leaves either the previous file or nothing - never a truncated mot set that the game would try to
    load and the user would believe in.  The rename is the last thing that happens, after the file has
    been closed and its size checked.
    """

    name = "Export mot set"

    # (stage, weight) from the measurement above.
    WEIGHTS = [("read tables", 0.0004), ("spine mapping", 0.0001), ("collect", 0.7613),
               ("build keysets", 0.2288), ("write file", 0.0092)]

    def __init__(self, context=None, task=None, armature=None, options=None):
        Operation.__init__(self, context, task, options)
        self.armature = armature
        self.filepath = (options or {}).get("filepath", "")
        self.decimals = int((options or {}).get("decimals", 4))
        self.scale_keys = float((options or {}).get("scale_keys", 1.0))
        self.report_data = {}

    def prepare(self):
        from . import exporter
        scene = self.context.scene
        if self.armature is None or self.armature.type != 'ARMATURE':
            raise ValueError('No "Export Rig" armature in this file, and the selected object is not '
                             "an armature either.")
        if not self.filepath:
            raise ValueError("no output path was chosen")
        self.frame_start = int(scene.frame_start)
        self.frame_end = int(scene.frame_end)
        self.frames = self.frame_end - self.frame_start + 1
        self.temp = self.temp_path(self.filepath)
        self.state["exporter"] = exporter
        self.writer = None
        # The number of units is known exactly before the walk starts, and it is what the bar counts:
        # the frames (in groups of `COLLECT_GROUP`), one per skeleton bone for the table, one per
        # channel for the keysets, and the keyset groups of the write.  A bar that counts steps of the
        # work rather than one stage's private quantity stays meaningful from start to finish.
        from . import bone_utils
        self.unit_total = ((self.frames + bone_utils.COLLECT_GROUP - 1) // bone_utils.COLLECT_GROUP
                           + 2 + 3 + 1)

    def tables_stage(self):
        from . import exporter
        self.bones_data, self.bone_info = exporter.read_data()
        yield None

    def spine_stage(self):
        from . import rig_utils
        self.spine_notes = list(rig_utils.apply_spine_copies(self.armature))
        for note in self.spine_notes:
            print("Spine mapping: %s" % note)
        yield None

    def collect_stage(self):
        """The frame loop.  One unit is a group of frames; see `bone_utils.collect_units`."""
        from . import bone_utils, pole_utils
        scene = self.context.scene

        def update_poles(_frame):
            problems = pole_utils.drive_scene_poles(scene)
            if problems:
                raise RuntimeError("elbow poles are not driven, refusing to export: %s"
                                   % "; ".join(problems))

        units = bone_utils.collect_units(self.armature, self.frame_start, self.frame_end,
                                         pre_sample=update_poles)
        collected = [None]

        # `collect_units` returns the finished mapping, which a `for` loop cannot observe, so it is
        # driven by hand: the last `next` raises StopIteration carrying the mapping.
        while True:
            try:
                unit = next(units)
            except StopIteration as stop:
                collected[0] = stop.value
                break
            yield unit
        self.transforms = collected[0]


    def keysets_stage(self):
        """One numeric channel per unit: 187 bones became 583 channels at ~20 ms each.

        The bone was the obvious unit and it is the wrong one: a `Rotation` bone is three channels of
        11,885 frames each and the Euler continuity pass is per frame, so one bone is 76 ms on average
        and 263 ms for the worst.  `channel_units` yields one channel at a time, in the order the file's
        keyset types are written in.
        """
        from . import bone_utils
        export_data = []

        def emit(unit):
            make, box = unit
            make()
            export_data.extend(box)

        for bone in self.bones_data:
            for unit in bone_utils.channel_units(bone, self.transforms, self.armature.name,
                                                 self.decimals, self.scale_keys,
                                                 self.frame_start):
                yield self.invoke(lambda _u=unit: emit(_u))
        export_data.append(None)
        self.export_data = export_data

    def write_stage(self):
        """The file write, in units of eight keysets: 2.2 ms each, so ~18 ms per unit.

        An atomic write is allowed if it is not the dominant cost and the UI says "writing"; this
        one is measured at 457 ms in one call on the real mot set, which is 0.9% of the export - but
        457 ms is also the longest single freeze in the whole operation, so it is chunked as well.  See
        `mot_writer.write_mot_bin_units` for why a keyset boundary is safe.
        """
        from . import mot_writer
        path = self.temp
        # created here, not in `stages()`: `stages()` runs before any unit is stepped, and the writer
        # would then open the file and read `export_data` before the keyset stage has filled it
        self.writer = mot_writer.write_mot_bin_units(path, self.export_data, self.bone_info,
                                                     self.frames, group=8)
        while True:
            try:
                unit = next(self.writer)
            except StopIteration:
                break
            yield unit
        size = os.path.getsize(path)
        if size <= 0:
            raise ValueError("the mot writer produced an empty file")
        self.state["bytes"] = size

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.tables_stage, self.spine_stage, self.collect_stage,
                           self.keysets_stage, self.write_stage))]

    def abort(self, reason):
        """Discard the temp file.  The real output was never opened, so there is nothing else to undo."""
        writer = self.writer
        self.writer = None
        if writer is not None:
            # the writer is a generator holding the file open between units; closing it here releases
            # the handle before the temp path is removed, which on Windows is the difference between a
            # cleanup that works and one that reports "the file is in use by another process"
            try:
                writer.close()
            except (RuntimeError, ValueError, OSError):
                pass
        Operation.abort(self, reason)

    def commit(self):
        """Publish the file.  The rename is atomic, so readers see the old file or the new one."""
        self.accept_temp(self.filepath)
        self._committed = True
        self.report_data = {"path": self.filepath, "bytes": self.state.get("bytes", 0),
                            "keysets": len(self.export_data), "frames": self.frames}

    def describe(self):
        if not self._committed:
            return "nothing was written"
        keys = sum(len(k.values) for k in self.export_data if k is not None)
        return ("wrote %s: %.1f MB, %d keyset(s), %d key(s), %d frame(s)"
                % (os.path.basename(self.filepath), self.report_data.get("bytes", 0) / 1e6,
                   self.report_data.get("keysets", 0), keys, self.frames))


# ---------------------------------------------------------------------------- the other exports
class CameraExportOperation(Operation):
    """The camera .vmd -> .a3da conversion, measured at 0.83 s on the real file.

    Small next to the mot set, and still over the chunk budget on its own: the work is eight channels
    of 11,883 keys plus a full re-read and channel-by-channel comparison of what was written.  The
    stage boundaries are the ones the pipeline already has, and the validation stage is deliberately
    one unit, because its whole job is to prove the file is whole before it is published.

    `camera_core.export_camera_a3da` already writes to a temp name and only `os.replace`s it after
    validation passes, so a cancel cannot leave a partial a3da - there is no temp path for this
    operation to register.
    """

    name = "Export camera"
    WEIGHTS = [("convert", 0.90), ("validate", 0.10)]

    def __init__(self, context=None, task=None, options=None):
        Operation.__init__(self, context, task, options)
        self.src = (options or {}).get("src", "")
        self.filepath = (options or {}).get("filepath", "")
        self.min_y = (options or {}).get("min_y")
        self.stats = None

    def prepare(self):
        if not self.src or not os.path.isfile(self.src):
            raise ValueError("no camera .vmd was chosen")
        if not self.filepath:
            raise ValueError("no output path was chosen")
        self.unit_total = 2

    def convert_stage(self):
        from . import camera_core
        # The conversion is one call that cannot be stepped from the inside, so it goes to a worker
        # thread and these units only watch it: about 8 ms a tick instead of holding the main thread
        # for the 0.8-1.1 s of the call.  `cancel` is checked by the writer before it publishes, so a
        # cancel still leaves an existing .a3da exactly as it was.
        off = OffThread(lambda: camera_core.export_camera_a3da(
            self.src, self.filepath, min_y=self.min_y, file_name=os.path.basename(self.filepath),
            cancel=lambda: off.stop[0]), "camera-export", on_cancel=self.cancel_requested)
        yield from off.units()
        self.stats = off.result

    def check_stage(self):
        """The published file, read back: `export_camera_a3da` validated it before the rename, and a
        zero-length file would mean the rename published nothing."""
        size = os.path.getsize(self.filepath)
        if size <= 0:
            raise ValueError("the camera writer produced an empty file")
        yield None

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.convert_stage, self.check_stage))]

    def commit(self):
        self._committed = True

    def describe(self):
        if self.stats is None:
            return "nothing was written"
        return ("wrote %s: %d bytes, %d key(s), layout %s"
                % (os.path.basename(self.stats["out"]), self.stats["bytes"], self.stats["keys"],
                   self.stats["layout"]))


class AudioExportOperation(Operation):
    """The .ogg encode, measured at 6.9 s on a four-minute song.

    **This one is not chunked by the scheduler at all, and that is not a shortcut.**  The work happens
    in ffmpeg, in another process; there is no loop here to slice.  What the operation provides instead
    is the two things a user actually needs from it: a determinate progress bar (the finished encode's
    duration divided into the elapsed wall clock, which is what `cancel` is polled on anyway) and a
    cancel that reaches the child - `audio_ops._run` polls every 50 ms and terminates it.

    `audio_ops.convert` already writes to a scratch file and publishes only after probing it, so
    stopping it leaves the previous output untouched.
    """

    name = "Export audio"
    WEIGHTS = [("probe source", 0.02), ("encode", 0.95), ("verify", 0.03)]

    def __init__(self, context=None, task=None, options=None):
        Operation.__init__(self, context, task, options)
        self.src = (options or {}).get("src", "")
        self.filepath = (options or {}).get("filepath", "")
        self.channels = int((options or {}).get("channels", 2))
        self.quality = (options or {}).get("quality")
        self.normalize = (options or {}).get("normalize")
        self.overwrite = bool((options or {}).get("overwrite", False))
        self.info = None
        self.probe_info = None

    def prepare(self):
        from . import audio_ops
        if not self.src or not os.path.isfile(self.src):
            raise ValueError("no audio source was chosen")
        if not self.filepath:
            raise ValueError("no output path was chosen")
        if os.path.exists(self.filepath) and not self.overwrite:
            raise ValueError("%s already exists" % self.filepath)
        self.unit_total = 3
        self.state["audio_ops"] = audio_ops
        self.source_info = {}

    def source_stage(self):
        """What the source is, so the bar can be determinate and the estimate honest.

        An ffprobe call, measured at 191 ms - long enough for the window to skip a frame, so it runs
        off-thread too.
        """
        from . import audio_ops

        def probe():
            try:
                self.source_info = audio_ops.source_stream_info(self.src)
            except Exception:                                   # noqa: BLE001 - reported as unknown
                self.source_info = {}

        off = OffThread(probe, "audio-source-probe")
        yield from off.units()

    def encode_stage(self):
        """The encode runs on a worker thread; these units only watch it, ~8 ms at a time.

        `audio_ops` already polls its own child every 50 ms and terminates it when `cancel` says so -
        but a poll living inside a single scheduler unit can never see a cancel, because the Cancel
        button needs the event loop that the unit is holding.  Moving the call to a thread is what
        makes that existing polling reachable, so a multi-second encode is watched in hundreds of
        ticks the window can draw between.  An empty scratch file from a killed ffmpeg never reaches the
        user: `convert` publishes only after it has probed and measured the scratch file it wrote.
        """
        from . import audio_ops
        if self.cancel_requested():
            raise tc.TaskCancelled(self.name)
        off = OffThread(lambda: audio_ops.convert(self.src, self.filepath, channels=self.channels,
                                                 quality=self.quality,
                                                 normalize_peak=self.normalize,
                                                 overwrite=self.overwrite,
                                                 cancel=lambda: off.stop[0]),
                        "audio-encode", on_cancel=self.cancel_requested)
        yield from off.units()
        self.info = off.result          # a cancel is raised by the unit stream itself: `finish`
                                        # sees the flag and stops here, so the probe never runs on a
                                        # file that was never published

    def verify_stage(self):
        from . import audio_ops
        size = os.path.getsize(self.filepath)
        if size <= 0:
            raise ValueError("the encoder produced an empty file")
        # the probe and the per-channel measurement are two more child processes, measured at 0.62 s
        # in one unit - long enough to skip frames, so they run off-thread too
        off = OffThread(lambda: audio_ops.probe(self.filepath), "audio-verify")
        yield from off.units()
        self.probe_info = off.result

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.source_stage, self.encode_stage, self.verify_stage))]

    def commit(self):
        self._committed = True

    def describe(self):
        if self.info is None:
            return "nothing was written"
        return ("wrote %s: %.1f MB, %s Hz, %s channel(s)"
                % (os.path.basename(self.filepath), os.path.getsize(self.filepath) / 1e6,
                   (self.probe_info or {}).get("sample_rate"),
                   (self.probe_info or {}).get("channels")))


class FaceExportOperation(Operation):
    """The expression/mouth splice, measured at 5.7 s on the real pair (1.2 match + 4.5 export).

    Three stages, in the order the pipeline already had them, and each is a unit the scheduler can
    measure: reading the dance's morph track and matching it against the slot tables; building the
    plan; and the export itself, which decodes the base script, decides every event, splices, encodes,
    re-reads the result and validates it against every rule a shipping script obeys.  That last stage
    is one unit on purpose - it publishes only after the validation passes, and a half-validated
    publish is the thing it exists to prevent.

    Expression and mouth behaviour is not touched by this class.  It calls the same `morph_core` and
    `face_core` entry points the operator called before, with the same arguments; the only difference
    is that the caller can now watch it and stop it.
    """

    name = "Export expressions"
    # Measured on a full-length reference dance against a stock base script: match 1.18 s, plan
    # 0.00 s, export 4.52 s - the splice dominates, hence the weights below.
    WEIGHTS = [("match morphs", 0.207), ("build plan", 0.001), ("splice and validate", 0.792)]
    def __init__(self, context=None, task=None, options=None):
        Operation.__init__(self, context, task, options)
        self.src = (options or {}).get("src", "")
        self.base = (options or {}).get("base", "")
        self.filepath = (options or {}).get("filepath", "")
        self.alias = (options or {}).get("alias")
        self.chara = int((options or {}).get("chara", 0))
        self.replace = bool((options or {}).get("replace", True))
        self.overwrite = bool((options or {}).get("overwrite", False))
        self.min_gap_ms = (options or {}).get("min_gap_ms", 0)
        self.solver = bool((options or {}).get("solver", True))
        self.approx = bool((options or {}).get("approx", False))
        self.rom_root = (options or {}).get("rom_root")
        self.result = None
        self.plan = None
        self.stats = None
        self.explain = []

    def prepare(self):
        from . import morph_core
        if not self.src or not os.path.isfile(self.src):
            raise ValueError("no dance .vmd was chosen")
        if not self.base or not os.path.isfile(self.base):
            raise ValueError("no base PV script was chosen")
        if not self.filepath:
            raise ValueError("no output path was chosen")
        if os.path.exists(self.filepath) and not self.overwrite:
            raise ValueError("%s already exists" % self.filepath)
        code = (self.options.get("code") or morph_core.CHARA).upper()[:3]
        if code != morph_core.CHARA:
            morph_core.CHARA = code
        self.unit_total = 3

    def rom_stage(self):
        """Point the slot tables at the rom root the caller found, if any.

        Not folded into `prepare` because it mutates module-level state that the match below depends
        on, and a stage boundary is where a failure can be reported against the thing that failed.
        """
        from . import morph_core
        if self.rom_root:
            morph_core._ROB_TBL = (os.path.join(self.rom_root, "main/rom/rob/rob_mot_tbl.bin"),
                                   os.path.join(self.rom_root,
                                                "main/rom_switch/rom/rob/rob_mot_tbl.bin"))
            morph_core._MOT_DB = (os.path.join(self.rom_root, "main/rom/rob/mot_db.farc"),
                                  os.path.join(self.rom_root,
                                               "main/rom_switch/rom/rob/mot_db.farc"))
            morph_core._CACHE.clear()
        yield None

    def match_stage(self):
        from . import morph_core
        # reading the dance's morph track and resolving every name against the slot tables is 0.89 s
        # of pure file work - `morph_core` imports no bpy, so it goes off-thread like the other two
        off = OffThread(lambda: morph_core.match(self.src, alias_file=self.alias,
                                                approx=self.approx), "morph-match",
                        on_cancel=self.cancel_requested)
        yield from off.units()
        self.result = off.result

    def plan_stage(self):
        from . import face_core, morph_core
        self.plan = face_core.plan_from_match(self.result["matched"], morph_core.game_names())
        yield None

    def export_stage(self):
        from . import face_core, morph_core
        self.explain = []
        out, base, src = self.filepath, self.base, self.src
        plan, game = self.plan, morph_core.game_names()
        chara, replace, overwrite = self.chara, self.replace, self.overwrite
        gap, solver = self.min_gap_ms, self.solver
        explain = self.explain

        # Same shape as the camera and audio stages: the decision layer is one uninterrupted solve
        # over the whole dance, so it runs off-thread and is watched in ~8 ms units instead of one
        # 5.1-7.9 s block.  `export_face` checks the cancel flag before its publish step, so the
        # script the user pointed at is never left half-replaced.
        off = OffThread(lambda: face_core.export_face(
            base, src, out, plan, game, chara=chara, replace=replace, overwrite=overwrite,
            min_gap_ms=gap, solver=solver, explain=explain, cancel=lambda: off.stop[0]),
            "face-export", on_cancel=self.cancel_requested)
        yield from off.units()
        self.stats = off.result

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.match_stage, self.plan_stage, self.export_stage))]

    def commit(self):
        self._committed = True

    def describe(self):
        if self.stats is None:
            return "nothing was written"
        return ("wrote %s: %d record(s), %d mouth + %d expression cue(s), validated against every "
                "shipping-script rule" % (os.path.basename(self.stats["out"]), self.stats["records"],
                                          self.stats["mouth"], self.stats["expression"]))


class FFmpegFetchOperation(Operation):
    """Fetch the portable ffmpeg this add-on's `bin/` is built for, off the main thread.

    Two stages, one call each, both too long for a single main-thread unit: streaming the
    pinned zip and then verifying its digest and unpacking it.  Both run through `OffThread`,
    and the download reports bytes: the worker writes progress into `self.state` while the
    watching units hand it to the task, so the bar reads as MB done of MB total rather than
    as scheduler ticks.
    """

    name = "Fetch FFmpeg"
    WEIGHTS = [("download", 0.85), ("verify and install", 0.15)]

    def __init__(self, context=None, task=None, options=None):
        Operation.__init__(self, context, task, options)
        self.state = {"done": 0, "total": 0}
        self.temp = ""
        self.path = ""

    def prepare(self):
        from . import ffmpeg_fetch
        if not ffmpeg_fetch.supported_platform():
            raise ValueError("the fetch step is for Windows; on other systems install ffmpeg "
                             "with the package manager and it will be found on PATH")
        self.unit_total = 2

    # The download stage advances the bar with bytes, not with ticks: the inherited counter
    # would race a hundred-thousand-unit watch stream against a two-unit plan.  `_report_mb`
    # is the only thing that sets progress while `download` runs.
    def bump(self):
        pass

    def download_stage(self):
        from . import ffmpeg_fetch

        def grab():
            return ffmpeg_fetch.download(
                cancel=lambda: off.stop[0],
                progress=lambda done, total: self.state.update(done=done, total=total))

        off = OffThread(grab, "ffmpeg-download", on_cancel=self.cancel_requested)
        for step in off.units():
            self._report_mb()
            yield step
        self.temp, _bytes = off.result

    def _report_mb(self):
        if self.task is None:
            return
        done, total = self.state.get("done", 0), self.state.get("total", 0)
        if total:
            self.task.set_progress(done, total)      # byte-space; the stage weights scale it
        self.task.set_message("%.0f / %.0f MB" % (done / 2 ** 20, total / 2 ** 20))

    def install_stage(self):
        from . import ffmpeg_fetch
        off = OffThread(lambda: ffmpeg_fetch.install(self.temp), "ffmpeg-install",
                        on_cancel=self.cancel_requested)
        yield from off.units()
        self.path = off.result

    def stages(self):
        return [(name, weight, self.unit(body)) for (name, weight), body in zip(
            self.WEIGHTS, (self.download_stage, self.install_stage))]

    def abort(self, reason):
        # the temp zip belongs to the caller until `install` consumes it; a cancel between the
        # stages must not leave the download sitting in the system temp directory
        if self.temp:
            from . import ffmpeg_fetch
            ffmpeg_fetch._drop(self.temp)
            self.temp = ""

    def commit(self):
        self._committed = True

    def describe(self):
        return "installed %s" % self.path if self.path else "nothing was installed"


