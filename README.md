# DIVA PV Tools

**A Blender add-on that turns MMD work into Project DIVA MEGA39's+ mod files** — the dance,
the camera, the song and the face. Import a `.vmd` motion, tune it on a provided rig template,
and export what the game's mod loader reads: a **mot set** (`.bin`), a **camera** (`.a3da`), a
**song** (`.ogg`) and a **PV script** (`.dsc`) whose mouth, eyelids and expressions were solved from
the dance's own morphs — plus a **morph audit** (`.morph_audit.json`) that accounts for every morph
the motion animates.

It is a fork of [BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)
(`DIVA Tools`, by **ThisIsHH** and **FlyingSpirits**, MIT), maintained as **diva_pv_tools** by
**SeabirdUmidori (fork)**; this branch keeps its motion-export core and grows the project into a
converter with a cancellable task system and a bilingual (English/中文) UI. See
[Provenance](#provenance--what-is-inherited-what-is-new) for the exact split.

> **Version 2.0.0** is the first release of this fork as a converter, and the one that grew the face
> into a subsystem of its own: a solved mouth, a rule-driven expression transplant, the eyelid taken
> from the dance's own `まばたき`, and a morph audit that accounts for every morph the motion
> animates.

> **Tested target:** *Hatsune Miku Project DIVA MEGA39's+* (PC) — the only game the exports
> have been accepted in. Other DIVA titles/platforms are untested (see
> [Known limitations](#known-limitations)).

```
MMD world                          Project DIVA world
─────────                          ──────────────────
Motion.vmd  ── import ──► rig ──►  MOT_*.bin          (mot set: 583 keysets, 60 fps)
Camera.vmd  ────────────────────►  <name>.a3da        (the camera model auth_3d loads)
any audio   ────────────────────►  pv_<id>.ogg        (Ogg/Vorbis 44.1 kHz, SEGA header shape)
Motion.vmd morphs ──────────────►  <name>.dsc         (MOUTH_ANIM/EXPRESSION cues in a generated script)
                                  <name>.morph_audit.json  (what every animated morph became, and why)
```

> **Not written by this version:** the eye-motion file `pv_expression/exp_PV<id>.bin`. The export
> still writes the one script record that switches it on, and `exp_writer` (the layout-verified
> writer for it) ships inside the package, but no code path calls it in 2.0.0 — see
> [Known limitations](#known-limitations).

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [The template and the workflow](#the-template-and-the-workflow)
- [Panel reference](#panel-reference)
- [Provenance — what is inherited, what is new](#provenance--what-is-inherited-what-is-new)
- [How it works (architecture & algorithms)](#how-it-works-architecture--algorithms)
- [Output formats at a glance](#output-formats-at-a-glance)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Development notes](#development-notes)
- [Credits & license](#credits--license)

---

## What it does

Five operations — plus the ffmpeg fetch, which runs through the same task machinery — each a task
with a progress card, a working Cancel, and refuse-to-clobber output guards:

| Operation | Input | Output | Notes |
|---|---|---|---|
| **Import motion** | `.vmd` (MMD, 30 fps) | animation on `Import Rig` | restores the template's 60 fps time base; keeps or clears existing curves; routes a master eye/wrist bone (`両目`, `両手首`) onto the per-eye / per-wrist bones the rig actually has |
| **Export motion** | `Export Rig` pose, sampled per frame | mot set `.bin` | byte-for-byte compatible with the upstream exporter's accepted output; per-channel static-vs-linear keyset selection |
| **Export camera** | `Camera.vmd` | the `.a3da` you name | re-implements the DIVA_CameraTool method end-to-end: VMD → DIVA camera model, no JSON intermediate, no FArC packing |
| **Export song** | any audio file ffmpeg decodes | 44.1 kHz Ogg/Vorbis `.ogg` | 2ch/4ch, libvorbis `-q:a` quality, peak normalise; publishes only after re-probing the encode |
| **Export expressions** | `.vmd` morphs, and the dance's own `まばたき` | a `.dsc` written from a generated scaffold, a `.morph_audit.json`, and a `.worklist.txt` if names are unresolved | the scaffold carries the bootstrap a shipping script opens with, so the face stream runs on the motion's own clock with nothing borrowed; the script is re-read and validated against every rule shipping scripts obey before it is published |

The sidebar tab is **DIVA PV** (named so in every language, deliberately — it is what you look
for). The panels are **Motion**, **Expressions**, **Camera**, **Music** and **Task**; the task panel
starts collapsed, and the mot-set export is also on **File → Export → Project DIVA Mot Set (.bin)**.

## Requirements

| Requirement | Needed for | Details |
|---|---|---|
| **Blender 4.0+** (developed on 4.5 LTS) | everything | Blender 4.x Python API only; no pip installs, no bundled binaries |
| **[mmd_tools](https://mmd-tools.github.io/addon.io/)** | motion import | resolved as `bl_ext.blender_org.mmd_tools` (extensions platform) or the legacy `mmd_tools`; the importer deliberately borrows mmd_tools' own `BoneConverter`/`BoneNameMapper` rather than reimplementing its conventions, and refuses the import when the add-on is not enabled |
| **ffmpeg built with `libvorbis`** | song export only | every candidate is probed by parsing `ffmpeg -encoders` for a `libvorbis` row, so a binary that exists but lacks it is skipped, not used. Found in this order: the Music panel's **FFmpeg executable (optional)** field → `$MMD2DIVA_FFMPEG` → `PATH` → the add-on's own `bin/` → `C:/ffmpeg/bin` and `C:/Program Files/ffmpeg/bin`. Or click **Download FFmpeg** — see [Fetching ffmpeg](#fetching-ffmpeg-safe-by-design) |
| **Project DIVA MEGA39's+** | playing the results back | **the exports are tested in-game on *Hatsune Miku Project DIVA MEGA39's+* only** (PC). They are built against that game's shipped data (the packaged slot/name tables and the face knowledge base), and the format lineage is shared across the series — but **no other title or platform has been verified**; treat them as untested |
| **The A-POSE template** | motion import + export | **ships in this repository**: [`template/MMD to DIVA (A-POSE)_fork.blend`](template/MMD%20to%20DIVA%20(A-POSE)_fork.blend) — the rigs (`Import Rig`, `Export Rig`, `ElbowPole_左/右`), the constraint network between them, and the 60 fps time-base contract live in this file; camera/song/expression exports run in *any* file |

No pip packages, no compiled dependencies: everything is pure Python stdlib + `bpy` + `numpy`
(numpy arrives with Blender's own Python for the solver paths that use it).

## Installation

**Option A — release zip (easiest):** download `diva_pv_tools-<version>.zip` from
[Releases](../../releases), then **Blender → Edit → Preferences → Add-ons → Install…** →
pick the zip → enable **Import-Export: DIVA PV Tools (MMD to Project DIVA)**.

**Option B — straight from this repository:** copy the `diva_pv_tools/` folder into Blender's
add-ons directory (`%APPDATA%\Blender Foundation\Blender\4.5\scripts\addons` on Windows,
`~/.config/blender/4.5/scripts/addons` on Linux), then enable it in Preferences.

Then:
1. **Get the template**: open (or keep somewhere safe)
   `template/MMD to DIVA (A-POSE)_fork.blend` from this repository — it is the working file
   for the motion pipeline (see [The template and the workflow](#the-template-and-the-workflow)).
2. If an older `BlenderDivaTools` / `diva_motion_tools` / earlier `diva_pv_tools` is installed,
   **disable and remove it first** — package names differ but the Scene property identifiers are
   deliberately shared with the template, and two copies registering them collide loudly.
3. The add-on's language follows Blender's own UI language:
   **Chinese Blender (简体中文, `zh_CN`/`zh_HANS`) → Chinese UI; everything else → English.**
   The sidebar tab stays `DIVA PV` in both. Restart Blender after changing the language.
4. Optional, for the song export only: put an ffmpeg with libvorbis on `PATH`, set
   `MMD2DIVA_FFMPEG` to its `ffmpeg.exe`, type its path into the Music panel, or simply click
   **Download FFmpeg**.

## The template and the workflow

Open **`template/MMD to DIVA (A-POSE)_fork.blend`** (from this repository) for the motion
pipeline. The name-driven parts it provides:

| Name | Kind | Role |
|---|---|---|
| `Import Rig` | armature | where a `.vmd` lands (MMD bone names: `下半身`, `左腕`, `センター`, `グルーブ`, …) |
| `Export Rig` | armature | the DIVA-named rig (`kl_kosi_xz`, `e_ude_l_cp`, `tl_up_kata`, …) actually sampled by the exporter; driven by constraints from `Import Rig` |
| `ElbowPole_左` / `ElbowPole_右` | empties | elbow pole targets; the exporter re-solves them per sampled frame from `Import Rig`'s own shoulder/elbow/wrist |

The template's rig work originates upstream — the MMD→DIVA rig is **FlyingSpirits'** work in the
BlenderDivaTools project; the `_fork` file is this branch's working copy (time-base contract,
pole wiring and constraint fixes applied per the rules the exporter assumes). Keep the three
names as they are if you edit the file: the add-on resolves them by name and reports exactly
which one a file lacks.

Workflow per song:

```
open the template → Import motion (.vmd)  [the rig is keyed at 60 fps, time base restored]
                  → (optional) edit the dance in Blender / the companion motion_refinery add-on
                  → Export motion (.bin)  [samples Export Rig, drives the poles itself]
open any file     → Export camera  (.vmd → .a3da)
                  → Export song    (any audio → .ogg, name it pv_<id>.ogg)
                  → Export expressions (.vmd morphs → .dsc + morph audit + worklist)
pack the outputs with your mod builder (FArC packing is intentionally not this add-on's job)
```

Why the exporter drives the poles itself: Blender never runs a `frame_change_post` handler
restored from a `.blend`, so a template saved "already activated" would export frozen poles —
frozen arms look almost right and are wrong for every frame the arm turns. The exporter calls
the same math per sampled frame, and **refuses to write at all** when the rig or a pole cannot
be resolved, instead of shipping a silently-broken file.

## Panel reference

The panels hold the **inputs**; everything an operation *writes* is chosen in the save dialog its
button opens, and the options live in that dialog. Every option's hover text carries the full
instructions, in your language.

**Motion** — *Import motion (.vmd)* opens a file chooser (with one option: **Clear the rig first**),
imports onto `Import Rig` (made active + POSE automatically; selection elsewhere does not matter),
and records the source path for the Expressions panel. *Export motion (.bin)* asks for a destination
whose **General** section holds the three export options.

**Expressions** — **from the imported motion** is the one input switch: ticked (default) the morphs
come from the `.vmd` that *Import motion* recorded; unticked, a **dance .vmd to read** field appears
and is read only, never loaded. *Export expressions (.dsc)* then asks for the script to write.

**Camera** — a **Camera motion (.vmd)** path plus *Export camera (.a3da)*, which asks for the
destination `.a3da`.

**Music** — a **Source audio** path, an optional **FFmpeg executable (optional)** field for a
ffmpeg that is neither on `PATH` nor fetched, and *Export audio (.ogg)*. When no usable ffmpeg is
found the panel says so (`No usable ffmpeg was found.`) and offers **Download FFmpeg** in place of
an error.

**Task** — one card per run: stage name, MB/percent progress on the audio and ffmpeg stages,
elapsed/speed/ETA, a Cancel that stops at a safe point (the target file is never half-written), and
kept result cards (`finished / cancelled / failed at '<stage>'`) with per-card Dismiss plus one
**Dismiss all finished** when more than one result is on screen.

The export dialogs, option by option (labels as drawn in English):

| Dialog | Options |
|---|---|
| Import motion | **Clear the rig first** — drop existing curves before import. Turn on when replacing the song, off when layering |
| Export motion **General** | **Static Precision** (4) — the rounding at which a channel counts as "never moves" and is written once as a constant; accepted files use 4. **Scale Keys** (1.0) — sampled-frame → file-frame ratio; the 30→60 factor already lives in the scene time base, so keep 1.0. **Allow replacing an existing file** — the overwrite guard |
| Export expressions | **Slot table of** (`MIK`) — whose real slot table morph names are resolved against (MIK/LEN/LUK/…). **Character slot in the script** (0 = first dancer) — the `chara` written as every cue's first argument. **Write the morph audit (.json)** (on) — write `<output stem>.morph_audit.json` beside the script. **Alias answers (optional)** — your filled-in `worklist` from an earlier run, used as extra name answers. **Allow replacing existing files** |
| Export camera | **Camera height offset (m)** (0.0) — moves the whole shot, viewpoint and target together, so the framing keeps its angle. **Clamp to a minimum height** (off) — when ticked, frames below the next slider are lifted to it. **Minimum camera height (m)** (0.0). **Allow replacing existing files** |
| Export audio | **Channels** (2ch stereo / 4ch quad). **Quality (-q)** (6.0, −0.2…10 — libvorbis VBR quality). **Normalise peak** (0.95; 0 leaves the level alone). **Allow replacing an existing file** |

## Provenance — what is inherited, what is new

This project stands on three published upstreams and borrows conventions from a fourth:

| Upstream | Role here |
|---|---|
| **[BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)** (`DIVA Tools`, MIT © 2024 ThisIsHH; by ThisIsHH & FlyingSpirits) | the fork base: the motion-export pipeline and the rig template — the A-POSE template `.blend` in `template/` derives from this project's rig, **built by FlyingSpirits** (upstream credits: *"their outstanding work on the MMD-DIVA rig"*), carried and fixed forward by this fork |
| **[DIVA_BlenderCameraTool](https://github.com/ThisIsHH/DIVA_BlenderCameraTool)** (MIT © 2023 ThisIsHH) | the camera-export **method**: DIVA's position+interest camera model, its unit/frame/axis conventions, and the `Camera.json` → a3da route — here re-implemented end-to-end as VMD → `.a3da` |
| **[korenkonder/PD_Tool](https://github.com/korenkonder/PD_Tool)** | studied to pin down the binary formats: the mot-set container (`Mot.cs`, credited in `mot_writer.py`), the a3da container, and the `pv_db` command table used to decode/encode `.dsc` |
| **mmd_tools** (external add-on) | VMD bone conventions and the record→curve transform, imported and reused rather than duplicated |
| **diva-camtool** (thtrandomlurker) | cross-checked camera calibration facts (`Frame = ThirtyFrame * 2`, `MMD × 0.08` with Z mirrored, half-angle FOV), cited in `camera_a3da.py` comments |

### Inherited from the original add-on (kept, fixed, or re-housed)

- **The motion export core**: `BoneDataTypes.xml` (bone list, per-bone type, `IKTarget`), the
  mot-set writer's container layout, the per-frame parent-relative sampling
  (`bone_utils`), the keyset machinery (absent/static/linear selection), and
  `utils.is_static`. The export is still byte-compatible with the upstream's accepted output —
  the regression gate is exactly that: re-export a full shipping dance and diff.
- **The elbow-pole placement rule**, unchanged in substance: the locator sits ≈0.3 m beyond the
  elbow, blended toward the upper arm's local ±X as the arm straightens — the original author's
  own logic, which matches SEGA's measured `|tl_up_kata|` envelope (0.36–0.61 m).
- **The rig template and its Scene property names** (`diva_*` plus the template identifiers),
  kept deliberately stable so existing `.blend` files and bindings keep working.
- **The frame-change pole handler** concept — with its registration bugs fixed: the shipped
  version matched handlers by bare name and could delete another add-on's handler with the same
  name (and lose its own on every file load). This branch matches module + object identity and
  re-installs itself via a persistent `load_post`.
- **File → Export → Project DIVA Mot Set (.bin)** menu placement.

### Changed behaviour worth knowing

- **The rig is taken by name**: the exporter uses `Export Rig` if the file has one and only falls
  back to the active object otherwise (and says in the console which one won). The button itself
  needs an active armature — that is the operator's own poll.
- **Undriven poles refuse the export** instead of writing a frozen-pole file (see above).
- **Import is no longer a stub.** The original "Import motion" button was
  `# Not implemented yet!` rendered greyed out. Everything below is this fork's, built so that
  a `.vmd` import + full re-export reproduces the accepted file byte-for-byte:
  - an own SJIS VMD parser (`vmd_reader`) used for import bookkeeping, morph tracks and camera,
    with the dialect (classic Euler vs quaternion records) detected from the headers before any
    record is handed out;
  - a per-bone record→curve pass reusing mmd_tools' converter, stepped by the task system instead
    of one uninterrupted `assign()` that holds the UI with no progress and no Cancel;
  - a master bone the rig has no slot for (`両目`, `両手首`) is routed onto the bones that inherit
    from it — measured on the reference dances, `両目` carries 253…604 real keys while `左目` and
    `右目` carry exactly one placeholder key each;
  - the template's time-base contract restored on import (`fps 60`, `frame_map_old 30`,
    `frame_start 0`), with a warning when the scene disagrees;
  - fixed import arguments (`frame_set(0)`, `frame_margin=0`, …) because mmd_tools adds
    `frame_current` to every keyframe — a cursor parked at the end used to shift whole dances;
  - a motion that *ends* with the eyes turned is reported as such, because the source never
    returns them to centre.
- **Every output path is chosen in a real save dialog**, seeded beside the input it came from; the
  options sit in that dialog, the tooltips carry the instructions, and nothing is written over an
  existing file without the overwrite option.

### Added on top (none of it exists upstream)

1. **Task system** — all four exports and the import run as cancellable, time-budgeted tasks
   (details below); finished cards persist with their outcome.
2. **Camera export** — `Camera.vmd → .a3da`, following the DIVA_CameraTool model end-to-end
   (method provenance above): dialect-aware VMD parsing, degrees-vs-radians detection by median,
   eye reconstruction from position and distance, FOV written as the full horizontal angle in
   radians with `fov_is_horizontal=1` like SEGA's own files, optional floor clamp and height offset.
3. **Song export** — any audio to the container the game reads, with a pure-Python
   Ogg/Vorbis prober (`audio_ogg`) that validates the result instead of trusting the encoder.
4. **Expression export** — VMD morphs → `MOUTH_ANIM` and `EXPRESSION` cue streams, decided by a
   sequence model (`face_retarget`) measured from 1065 shipping scripts, with the eyelids taken
   from the dance's own `まばたき`, plus the **morph audit** beside the script. This is the largest
   single subsystem of the fork.
5. **Bundled game tables** — `expression_slots.json` and the name tables moved out of code into
   package data, so a loose rom folder is optional (an extracted rom can still override).
6. **One-click ffmpeg fetch** — see below.
7. **Bilingual UI (en/zh) that follows Blender's language**, with every panel header/operator
   title re-registered on change, tooltip tables in package data, and a build-time gate that no
   key renders raw.
8. **Motion-editing features moved out**: what grew here as a local animation tool (joint-limit
   cleanup, naturalness, the limb reach clamp) now lives in the separate companion add-on
   **`motion_refinery`**; this package is the converter only, and the two are independent —
   either can be enabled alone.

## How it works (architecture & algorithms)

### Layering

```
UI (ui.py · export_operator.py)  panels, operators, option dialogs, menu entry, language tables
  └─ task_blender.py             Blender boundary: modal timer driver, task cards, progress_draw
      └─ operation.py            Operation = stages = generators of time-bounded *units*
          └─ task_core.py        pure scheduling/state, imports no bpy
          └─ task_ops.py         one operation per product: import/export
              └─ cores (import no bpy, headless-testable):
                   exporter · mot_writer · bone_utils · rig_utils
                   pole_utils · spine_hoist · camera_core · cam_json
                   a3da · camera_a3da · audio_ops · audio_ogg
                   face_core · face_retarget · morph_core · dsc_core
                   vmd_reader · vmd_tracks · farctool · exp_writer
                    morph audit:
                      mmd_morph       the morph model + the name catalog
                      morph_effect    measured effect signatures
                      source_model    PMX definitions (panel byte + vertex deltas)
                      diva_capability what DIVA can express, measured
                      morph_mapper    decisions, graded and explained
                      morph_timeline  frame/weight transfer, measured
                      morph_audit     completeness, provably
                      morph_pipeline  one entry point for all of it
```

Anything that can run without `bpy` does; the task layer can drive it headlessly, so the tested
path and the button path are the same path. Most of the bpy-free modules also run standalone
(`python -m diva_pv_tools.morph_core worklist <file.vmd> <out.txt>`, `… morph_pipeline <motion.vmd>
[report.json]`, `… dsc_core dump <file.dsc>`, and similar), which is how their behaviour is checked
without opening Blender.

### The task system (why the UI never freezes)

An operation is a generator of stages; a stage yields **units** — zero-argument callables doing
one bounded piece of work. `drive()` runs units while the clock says the budget (~20 ms) is not
spent; a modal timer re-enters it every tick, `execute` runs the same object synchronously in
headless sessions. Details that measurement forced:

- **units are sized by the wall clock, checked before starting the next one** — a "bounded" unit
  is still a ceiling, not an average;
- stage progress uses **calibrated weights** (measured per pipeline), not an even split;
- `gc.disable()` during a tick: building a mot set's keysets allocates about two million
  two-element lists, and the collector's walk used to land *between* units (a 95 ms tick);
- **cancellation raises at a safe point**, and cores check cancel *before publishing*, so a
  cancelled export leaves the previous file untouched and no temporary file survives;
- long uninterruptible calls (`ffmpeg`, the face solver, the download, the audit) run on a worker
  thread via `OffThread`, watched in ~8 ms units — allowed precisely because those modules touch no
  `bpy.data`;
- finished cards retire instead of vanishing (a result must be readable after the run), bounded
  history, per-card Dismiss carrying its task id, and one "Dismiss all finished".

### Motion import (VMD → rig)

`vmd_reader` parses the SJIS layout by byte widths and detects the dialect from the headers plus a
strided sample of the records, so a consumer never sees half a file's records: the quaternion
dialect's bone record is 111 B (15-byte name, frame at +15, position at +19, rotation at +31) with
23-byte morph records, the classic one is 64 B / 28 B, and the camera record carries 24 bytes of
interpolation data. VMD bezier handles become F-curve handles through mmd_tools' own
`__setInterpolation` including its `[20, 20, 107, 107]` fallback (the one the reference data
actually needs, since the interpolation bytes are zero on this material); quaternion tracks are
sign-aligned against the previous *converted* rotation, so a dance does not flicker the long way
round at every 180° crossing. The **transform convention is imported from mmd_tools, not
re-derived** — a second implementation of a convention is a second chance to get it subtly wrong —
while the record→curve loop is this add-on's own, per bone, cancellable, and compares
F-curve-for-F-curve with the accepted result. Unmapped tracks are reported, never guessed; the IK
toggle property curves (`mmd_ik_toggle`) are written too, because they are the difference between
a dance whose legs bend and one whose legs are locked straight.

### Motion export (rig → mot set)

Per sampled frame: drive the elbow poles (`pole_utils`, name-resolved), evaluate, and collect
each Export-Rig bone's parent-relative matrix in 3-frame groups (a measured RNA/C-API balance —
3.23 ms per `scene.frame_set`). The measured profile of the reference export (11,885 frames,
212 bones, 1,996,715 keys, 12.0 MB out) is what the stage weights come from:

```
collect (frame loop)   38.33 s   76.1%
process_bones          11.52 s   22.9%
write_mot_bin           0.48 s    1.0%
total                  50.34 s
```

Quaternion → euler conversion evaluates **both branch families** and keeps the branch that
continues the previous key (the shipped version's short-circuit could flip a branch mid-turn and
add a full spin the dance never had). `rig_utils` decides which bone carries the whole-body facing
(the template's own `センター`→`kl_hara_xz` copy plus the equally legitimate `グルーブ`, WORLD-pinned)
and `spine_hoist` re-roots a turn that would otherwise pin 180° onto the hip bone SEGA leaves
near-rest. Each channel is classified absent/static/linear by `is_static` at the chosen decimals —
the writer also implements a tangent keyset kind, but nothing produces one — keys are appended in
bulk (`insert()` binary-searches on every call; the per-key handle loop is 31.6 ms of a 970-key
track), and the file is written through `foreach_set` in keyset-sized units. The writer's layout
mirrors PD_Tool's `Mot.cs`; cancel or failure leaves nothing behind, and success writes
`<name>.bin` from a `.part` rename.

### Camera (`.vmd` → `.a3da`)

DIVA's camera = eye position + interest + roll + fov, not a 6-DOF transform. The MMD camera
record's layout varies by exporter dialect — the reader auto-detects which slot carries the
distance and whether rotations are degrees (median magnitude decides; authors type extra full
turns: reference files hold 183 rad at one key). The eye is reconstructed from the position and
the distance along the view axis (the exact formula follows the detected layout), and FOV becomes
the **full horizontal angle in radians** — MMD's 視野角 is a half-angle, DIVA's `view_point.fov`
is not — written with `fov_is_horizontal=1`, clamped to the measured 8°–55° band. Every animated
channel is written dense, one key per frame, with no tangent fields (the module also carries the
upstream `calculate_hermite_tangents` port, but the a3da writer path does not use it). The a3da is
a text property file; it is written to a temp name, re-read, compared value-by-value against the
channels in memory (tolerance 5e-6), and only then renamed onto the target. Packing into FArC is
out of scope.

### Song (any audio → Ogg/Vorbis, SEGA-shaped)

`audio_ogg` is a pure-stdlib Ogg page + Vorbis header parser: it proves what a file *is* (rate,
channels, blocksize byte `0xB8`, framing bit, CRCs, sequence continuity, EOS, and duration from
the last page's granule — which is exactly what DIVA uses as song length; nothing in `pv_db`
carries a duration). The encode is deliberately two stages — decode to 16-bit PCM at 44.1 kHz, then
encode — through ffmpeg in **`-q:a` quality mode**, because the id-header shape SEGA ships has
`bitrate_maximum = 0` and `bitrate_lower = 0`, which only quality mode produces. Quad mode uses
`pan=4c` (naming the layout would make ffmpeg up-mix and reinterpret the levels); with a stereo
source the default fills the rear pair by copying the front, and a genuinely 4-channel source is
passed through so its rear mix survives. Peak normalise aims 2.0 dB below the requested peak to
absorb vorbis overshoot, then re-measures the encoded file and corrects up to three times, refusing
to publish anything whose measured peak is above full scale or more than 1.5 dB off target — the
promise is "within tolerance, never above full scale", not exactness. Before publishing, the file
is re-probed and checked against the whole DIVA contract: Vorbis I, 44100 Hz, the requested channel
count, no LOOP tags, page CRC and sequence gaps, a positive granule, and a decoded length within one
sample of the source PCM (with a one-frame, 735-sample allowance against the source container).

### Expressions (morphs → `MOUTH_ANIM` / `EXPRESSION`)

The maths lives in bpy-free cores and is driven headlessly, so the tested path and the button path
are the same path. In order:

1. **Name matching** (`morph_core`): normalise (NFKC/casefold), exact against the character's real
   slot table, then the bundled alias tables, then approximation, then the user's **worklist**
   answers — which outrank every automatic tier. Unresolved names are written to a worklist for
   human answers, *never guessed into a shape* (`blink` has no slot at all: reported, not mapped).
   The game-data path reads `rob_mot_tbl.bin`/`mot_db.farc` when an extracted rom is available
   (optional `DIVA_DATA_ROOT`, or the rom root found beside the script), otherwise the packaged
   tables — and the report says which source answered.
2. **Sequence solving** (`face_retarget` + `data/*.json`): the mouth is not an emission per morph —
   it is a Viterbi solve over visemes, in two passes, using priors measured from SEGA's own
   scripts: cue spacing (~0.198 s median), a transition matrix, duration histograms, a
   section-density profile, and per-viseme shape preferences. A curve that never moves is held out
   of the state evidence (it is a rest pose, not articulation) and named in the report. One-frame
   flickers are merged, singleton visemes dropped, and the result is held inside the corpus's own
   cue-rate band (ceiling from the corpus p90, merging the *shortest* events — measured, not
   assumed: a per-event floor once collapsed 72 % of a performance and made the mouth look locked).
   Every emitted mouth shape must exist in a shipping script (`diva_face_targets.json`); nothing
   novel is ever written.
3. **Splice + encode** (`dsc_core`, `face_core`): the script to splice into is **generated**, not
   borrowed — a scaffold carrying the bootstrap block a shipping script opens with (`LYRIC`,
   `MUSIC_PLAY`, `BAR_TIME_SET`, `CHANGE_FIELD`, `DATA_CAMERA_START`, `DATA_CAMERA`, `MIKU_DISP`,
   `SET_PLAYDATA`, `SET_MOTION`, `HAND_SCALE`, `HAND_ANIM`, `TARGET`, `PV_END`, `END`), validated
   before use and removed again once the export publishes, so no borrowed camera, stage or lyric
   timeline can contradict the mouth. The finished file is re-encoded, **re-read and validated
   against every rule shipping scripts obey** (no bare `TIME` blocks, nothing after `PV_END`,
   command widths from the `pv_commands.json` table) before publication; `AUTO_BLINK` is never
   written.
4. **The face** (`face_core` + `data/expression_rules.json`): a DIVA face is a **held state** that
   carries the eyes, so an expression is not a blend to match — it is a **threshold condition** on
   the dance's own morph weights, and the readings in that table were taken off the character in
   game (`MIK_FACE_SAD` is `困る` engaged; `MIK_FACE_CLOSE` is `まばたき` at 1). Rules are ranked by
   tier, rules whose readings are only a guess are never emitted, and a face is always released
   again, because a face left on *is* an eye left moved. This transplant is unconditional: the rule
   table *is* the feature, and there is no switch for it. The **eyelids** follow the dance's own
   `まばたき` — a full closure writes the shut cue, the frame it leaves writes the open one.
5. **Strength and hold.** `MOUTH_ANIM`'s weight and `EXPRESSION`'s intensity are the dance's own
   level mapped onto the bands SEGA's own scripts use (weight top 300 with a rest cue at 100,
   intensity 100..200), read relative to each morph's own peak — the same rule the trigger
   threshold uses, because an MMD morph's absolute scale is the model author's choice. The engine
   holds a record's value until the next one, so the strength is re-stated as it moves (no closer
   than the corpus's own 133 ms re-statement interval), and a shape the dance holds longer than
   the engine's 1000 ms **hold** is re-stated inside it.
6. **The morph audit** (`morph_pipeline`): every morph name the motion animates gets one record in
   the report, with a grade and a reason, and anything DIVA has no dimension for is reported *with
   that reason*. A name the tooling cannot resolve is never guessed into a shape — it is written to
   a worklist for a human answer. The report is written beside the script as
   `<name>.morph_audit.json` (switchable in the dialog), and a `verify()` pass refuses to publish
   one that omits or double-counts a morph the motion animates. When a source `.pmx` sits beside the
   `.vmd` (or exactly one is in the folder) the audit also measures what each morph deforms and
   records that signature; without one, the report says `Geometry equivalence is NOT VERIFIED`.
   Two honest caveats: the measured effect corroborates a classification but does not drive the
   grade in this build, and records whose name does not decode cannot be attributed to a morph at
   all — they are counted in the report rather than mapped.

### Language

Every visible string lives in `TEXTS` (en/`zh_CN`, key sets locked equal by tooling) or
`hints.json` — 240 keys, panel headers, option labels, hover text, enum items, task-card words and
operator tooltips. Because Blender bakes `bl_label` into the class registration, language switches
re-register the affected panels/operators (timer-deferred so a class is never swapped during its own
draw) and rebuild Scene-property definitions — which is safe: stored values survive the
delete/re-add. The panel title and every option tooltip follow Blender's UI language; the tab name
never does.

Three things are checked at **build** time rather than trusted: that every language carries the same
key set, that no key is defined in both tables (where the `TEXTS` copy could never render), and that
every `L()` key exists — a missing one degrades to the English source string with no trace, which is
invisible in a language you read. What deliberately stays English is the diagnostic text the
bpy-free cores raise: a failed task card shows it as the detail line under a translated frame, so a
report can be pasted into a bug thread and read by anyone.

### Fetching ffmpeg (safe by design)

Nothing downloads until the Music panel's button is clicked; the transfer runs on a worker watched
by the scheduler (MB progress, real Cancel, partial files deleted), the zip's sha256 must match the
constant pinned in the module (a mirror can change the URL through `MMD2DIVA_FFMPEG_URL`, never the
verification), and only `ffmpeg.exe`, `ffprobe.exe` and `LICENSE` are unpacked — flat into the
add-on's own `bin/`, which the finder probes after `PATH`. The add-on never ships binaries; the
user's machine fetches them from the build host the module links (the panel quotes the download at
about 104 MB).

## Output formats at a glance

| Output | Container facts |
|---|---|
| `MOT_*.bin` / mot set | keyset count in the header (583 entries for the shipped skeleton: 582 numeric channels plus the terminator), 2-bit kind per keyset (absent/static/linear), dense per-frame keys, float32 values, `bone_info` trailer of 193 bone ids + terminator — per `PD_Tool/KKdMainLib/Mot.cs` |
| the camera `.a3da` | text property file, `#A3DA__________` header, named channels under `camera_root.0` (view_point/interest trans + roll dense type-3 keys, fov either dense or one constant), `view_point.fov` in radians with `fov_is_horizontal=1`, `_.file_name` stamped with the basename you chose, `play_control.size` = frame count |
| `pv_<id>.ogg` | Ogg/Vorbis I, 44100 Hz, blocksize byte `0xB8`, 2 or 4 channels, duration authoritative from the last granule |
| `PV*.dsc` | `TIME` in 1/100000 s units; `MOUTH_ANIM(chara, 0, shapeIdx, weight, hold)` with the *mouth-table index* and `EXPRESSION(chara, slotId, intensity, hold)` with the slot from the character's own table (two numbering systems on purpose); the generated scaffold's magic/version are kept, and the last field is a hold in milliseconds (1000 for a rule face, 0 for an eyelid open, −1 only on the eye-motion switch record) rather than a constant sentinel |
| `<name>.morph_audit.json` | the audit, `indent=1`: `coverage` (counts per grade, silent drops), `rows` and `lane_rows` (one record per morph name, with its classification, target, mapping method, grade, reason and keyframe counts), `timeline`, `target`, `sources`, and — on an export — the `export` stats and the build stamp |
| `<name>.worklist.txt` | written only when names were left unresolved: one line per name, for you to fill in with the character's own slot name; an answered file is reusable as the next run's **Alias answers** |

Game-side mod packaging (FArC containers, `pv_db` rows, model swaps) is out of scope:
this add-on produces the members, mod builders assemble.

## Troubleshooting

| Message | What it means / what to do |
|---|---|
| `mmd_tools is not enabled; importing a .vmd needs it` | enable the mmd_tools add-on (it owns the conventions the importer borrows) |
| `This scene has no 'Import Rig' armature` | you are not in the template file (or it was renamed) — motion import is by design template-bound |
| `elbow poles are not driven, refusing to export: …` | the template is missing `Import Rig` / `ElbowPole_左/右` or the MMD arm bones; the error names the missing pieces; fix them (or restore the template) instead of re-exporting blindly |
| `… already exists - tick the overwrite option, or choose another name` | the overwrite guard; retarget the filename or opt in |
| `No usable ffmpeg was found.` (panel) / `no usable ffmpeg found (needs an ffmpeg built with --enable-libvorbis)` (core) | click **Download FFmpeg** in the Music panel, or point `MMD2DIVA_FFMPEG` / the panel's ffmpeg field at any libvorbis build |
| `checksum mismatch: expected …, the download is … - nothing installed` | refuse-and-report by design: the pinned download was altered in transit (mirror/proxy); retry, or set `MMD2DIVA_FFMPEG` to a trusted binary |
| time-base warning after import | the scene's fps/frame mapping do not match the template contract (60 fps, map 30→60); re-import restores it — check that you did not change the scene afterwards |
| expression export writes a `.worklist.txt` | human decisions, not bugs: fill each name with the character's game slot name, re-run with that file in **Alias answers**; your answers outrank every automatic table |
| `Geometry equivalence is NOT VERIFIED` in the report | no source `.pmx` was found beside the `.vmd`, so nothing about the geometry was measured; every decision was name- and table-based (that is the normal case and does not fail the export) |
| a stage/card says `failed at '<stage>'` | the card keeps the error line and the console gets the traceback; the target file was not touched |

## Known limitations

- **The eye-motion file is not produced.** `exp_writer` (the `pv_expression/exp_PV<id>.bin` writer,
  layout-verified against 46 shipping files) ships in the package but no code path calls it in
  2.0.0: the export writes the one script record that switches such a file on, and writes no file.
  Whatever the source `.vmd` carries for the eyes is imported onto the rig like any other bone
  rotation, and the importer reports a motion that ends with the eyes turned; a `pv_expression` bin
  has to be produced outside this add-on for the game to read gaze channels at all.
- **In-game acceptance exists for one title only**: everything here was validated against
  *Hatsune Miku Project DIVA MEGA39's+* on PC — the mot export byte-checked against an
  accepted shipping conversion, the camera/song/face outputs re-read and validated against
  measured MEGA39's+ game-file rules, and the results loaded in that game. Other entries in
  the series (Future Tone, Arcade, PSP/Switch titles, or console MEGA39's+) share the formats
  but **have not been tested** — treat cross-platform use as unverified, your mileage is your
  own experiment. Whether your mod setup wants loose files or repacked `auth_3d`/`script`
  FArCs depends on the patch tooling you use.
- **FArC packing is a module, not a step.** `farctool` reads *and* writes FArC archives, but the
  only call wired into the add-on is the reader used to pull `mot_db.bin` out of a rom's
  `mot_db.farc`; no panel or task packs or unpacks anything, and `pv_db` is only ever read.
- **The audit's source `.pmx` is found, not chosen.** The export looks for a `.pmx` beside the
  `.vmd` (or the only one in its folder); there is no field to point it somewhere else, and
  without it the audit reports geometry as unverified.
- **No tangent keysets are written.** The mot writer implements the tangent kind, but every
  channel is classified as static or linear, so a tangent-shaped channel exports as dense linear
  keys.
- Face density for mocap-sourced dances can exceed what a stock script carries (the corpus tops
  out around 5.3 cues per second, and the solver is capped there); the engine handles what the
  corpus contains, but check your own.
- The camera module exports **camera** a3da only (not `MOC`/`OBJ` members), and the a3da it writes
  keeps values and structure on a re-read but is not byte-identical to a shipped member (the
  header carries the current time and every line is re-formatted).
- No Japanese UI currently (English + 简体中文; other Blender locales land on English).

## Development notes

Repository layout:

```
DIVA_PV_Tools/
├── README.md · LICENSE · .gitignore
├── build.bat · make_release.py            ← release build + install probe
├── dist/                                  ← the built artifact (not committed)
├── diva_pv_tools/                         ← the add-on package, shipped as source
│   ├── *.py                               (38 modules)
│   ├── data/ + *.json                     (slot tables, name tables, face knowledge base)
│   └── bin/                               ← created by the ffmpeg fetch (never committed)
└── template/
    └── MMD to DIVA (A-POSE)_fork.blend    ← motion-pipeline working file
```

The package data is 17 JSON files plus one XML: `expression_slots.json`, `diva_face_targets.json`,
`diva_face_alias.json`, `pv_commands.json` and `hints.json` at the package root; `BoneDataTypes.xml`,
`expression_rules.json`, `retarget_config.json`, `mmd_morph_catalog.json`, and the corpus knowledge
base (`mouth_prototypes`, `mouth_transitions`, `mouth_durations`, `expression_prototypes`,
`expression_transitions`, `context_model`, `lyric_offsets`, `motion_expression`,
`phoneme_mouth`) under `data/`. An extracted rom can override the slot tables at runtime; nothing
in this repository redistributes SEGA data.

Releases additionally carry the installable `diva_pv_tools-<version>.zip` — the same package tree
plus this README and `LICENSE` copied inside the add-on directory, zipped with exactly one
top-level directory as Blender's installer requires.

- Flat package: format cores (bpy-free, unit-testable), task layer (`task_core` imports no bpy),
  Blender boundary (`ui.py`, `task_blender.py`, `export_operator.py`), template bindings
  (`bone_utils`, `rig_utils`, `pole_utils`, `handlers`, `exporter`). Shared files
  `task_core/operation/task_blender` are byte-identical copies in the companion `motion_refinery`;
  change them in one, sync to the other.
- Style: stdlib + `bpy` only (no pip), ≤ 100 columns, no type-hint churn, every non-obvious
  number in comments carries how it was measured.
- The release zip is built and *install-probed* by `make_release.py`, driven by `build.bat`: it
  refuses a wrong layout, a stray `__pycache__`, a missing required entry, a translation table whose
  languages disagree in either direction and an `L()` key with no entry — then installs the zip into
  a throwaway config and enables it there (a real enable → operator registry → disable → re-enable
  cycle). A failure here is `build.bat` returning non-zero, never a warning.
- The add-on phones home to nobody: the only network code path is the user-triggered ffmpeg
  fetch; the only writes are to the files you name and `bin/` after that click.

## Credits & license

- **[BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)** — ThisIsHH &
  FlyingSpirits — MIT © 2024 ThisIsHH. The motion-export core, the template rig workflow, the
  pole placement rule, and the `calculate_hermite_tangents` port (source-cited in `cam_json`,
  used by its camera-JSON path).
- **[DIVA_BlenderCameraTool](https://github.com/ThisIsHH/DIVA_BlenderCameraTool)** — MIT
  © 2023 ThisIsHH — the camera-export model this fork's `cam_json`/`camera_core`/`a3da`
  implement.
- **[PD_Tool](https://github.com/korenkonder/PD_Tool)** — korenkonder — format reference for
  mot sets (credited at the top of `mot_writer.py`), a3da members and the `pv_db` command table
  era the `.dsc` grammar was recovered from.
- **[mmd_tools](https://mmd-tools.github.io/addon.io/)** — conventions borrowed at runtime, not
  vendored; its authors' work is the reason an MMD dance maps cleanly at all.
- **diva-camtool** (thtrandomlurker) — cross-checked calibration constants, cited in comments.

This fork — **diva_pv_tools**, maintained by **SeabirdUmidori (fork)**, MIT © 2026
SeabirdUmidori — is distributed under the **MIT License**, matching upstream. The bundled game-data
tables (`BoneDataTypes.xml`, slot/alias tables, knowledge statistics) were measured from
**Project DIVA** data shipped by SEGA — mod tools, game files not included, nothing in this repo
redistributes them.
