# DIVA PV Tools

**A Blender add-on that turns MMD work into Project DIVA MEGA39's+ mod files** — the dance,
the camera, the song and the face. Import a `.vmd` motion, tune it on a provided rig template,
and export what the game's mod loader reads: a **mot set** (`.bin`), a **camera** (`.a3da`), a
**song** (`.ogg`), **face cues** spliced into a PV script (`.dsc`) and the **eye-motion file**
(`pv_expression/exp_PV<id>.bin`) that script switches on.

It is a fork of [BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)
(`DIVA Tools`, by **ThisIsHH** and **FlyingSpirits**, MIT), maintained as **diva_pv_tools** by
**SeabirdUmidori (fork)**; this branch keeps its motion-export core and grows the project into a
converter with a cancellable task system and a bilingual (English/中文) UI. See
[Provenance](#provenance--what-is-inherited-what-is-new) for the exact split.

> **Version 2.0.0** is the first release of this fork as a converter, and the one that grew the face
> into a subsystem of its own: a solved mouth, a rule-driven expression transplant, the eyelid from
> the dance's own `まばたき`, the gaze in the game's `pv_expression` file, and a **morph audit** that
> proves nothing the motion animates went unreported. See [CHANGELOG.md](CHANGELOG.md).

> **Tested target:** *Hatsune Miku Project DIVA MEGA39's+* (PC) — the only game the exports
> have been accepted in. Other DIVA titles/platforms are untested (see
> [Known limitations](#known-limitations)).

```
MMD world                          Project DIVA world
─────────                          ──────────────────
Motion.vmd  ── import ──► rig ──►  MOT_*.bin          (mot set: 583 keysets, 60 fps)
Camera.vmd  ────────────────────►  CAMPV*_BASE.a3da   (the file auth_3d loads)
any audio   ────────────────────►  pv_<id>.ogg        (Ogg/Vorbis 44.1 kHz, SEGA header shape)
Motion.vmd morphs ──────────────►  PV*.dsc            (MOUTH_ANIM/EXPRESSION spliced into a stock script)
Motion.vmd eyes   ──────────────►  pv_expression/exp_PV<id>.bin   (gaze; the script carries the enable record)
Motion.vmd morphs ──────────────►  <script>.morph_audit.json      (what every animated morph became, and why)
```

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

Five operations, each a task with a progress card, a working Cancel, and refuse-to-clobber
output guards:

| Operation | Input | Output | Notes |
|---|---|---|---|
| **Import motion** | `.vmd` (MMD, 30 fps) | animation on `Import Rig` | restores the template's 60 fps time base; keeps or clears existing curves |
| **Export motion** | `Export Rig` pose, sampled per frame | mot set `.bin` | byte-for-byte compatible with the upstream exporter's accepted output; static/linear/tangent keyset selection |
| **Export camera** | `Camera.vmd` | `CAMPV…_BASE.a3da` | re-implements the DIVA_CameraTool method end-to-end: VMD → DIVA camera model, no JSON intermediate, no FArC packing |
| **Export song** | any audio file ffmpeg decodes | 44.1 kHz Ogg/Vorbis `.ogg` | 2ch/4ch, libvorbis `-q` quality, peak normalise; publishes only after re-probing the encode |
| **Export expressions** | `.vmd` morphs + eyes, and a stock PV script `.dsc` (optional — a scaffold is generated when none is given) | the `.dsc` with its face stream replaced, `pv_expression/exp_PV<id>.bin` beside it, and a `.morph_audit.json` | everything that is not `MOUTH_ANIM`/`EXPRESSION` — notes, stage cues, timings — is preserved record for record; the script is re-read and validated against every rule shipping scripts obey before it is published |

The sidebar tab is **DIVA PV** (named so in every language, deliberately — it is what you look
for). Panels: **Motion**, **Expressions**, **Camera**, **Music**, **Task**.

## Requirements

| Requirement | Needed for | Details |
|---|---|---|
| **Blender 4.0+** (developed on 4.5 LTS) | everything | Blender 4.x Python API only; no `mmap`, no bundled binaries |
| **[mmd_tools](https://mmd-tools.github.io/addon.io/)** | motion import, and the rig template | resolved as `bl_ext.blender_org.mmd_tools` (extensions platform) or the legacy `mmd_tools`; the importer deliberately borrows mmd_tools' own `BoneConverter`/`BoneNameMapper` rather than reimplementing its conventions |
| **ffmpeg built with `libvorbis`** | song export only | found via `$MMD2DIVA_FFMPEG` → `PATH` → the add-on's own `bin/` → common locations; or click **Download FFmpeg** in the Music panel — see [Fetching ffmpeg](#fetching-ffmpeg-safe-by-design) |
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
   `MMD2DIVA_FFMPEG` to its `ffmpeg.exe`, or simply click **Download FFmpeg** in the Music panel.

## The template and the workflow

Open **`template/MMD to DIVA (A-POSE)_fork.blend`** (from this repository) for the motion
pipeline. The name-driven parts it provides:

| Name | Kind | Role |
|---|---|---|
| `Import Rig` | armature | where a `.vmd` lands (MMD bone names: `下半身`, `左腕`, `センター`, `グルーブ`, …) |
| `Export Rig` | armature | the DIVA-named rig (`kl_kosi_xz`, `e_ude_l_cp`, `tl_up_kata`, …) actually sampled by the exporter; driven by constraints from `Import Rig` |
| `ElbowPole_左` / `ElbowPole_右` | empties | elbow pole targets; the game solves `c_kata_*`/`j_*` arms from them |

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
                  → Export expressions (.vmd morphs + eyes → .dsc + exp_PV<id>.bin + morph audit)
pack the outputs with your mod builder (FArC packing is intentionally not this add-on's job)
```

Why the exporter drives the poles itself: Blender never runs a `frame_change_post` handler
restored from a `.blend`, so a template saved "already activated" would export frozen poles —
frozen arms look almost right and are wrong for every frame the arm turns. The exporter calls
the same math per sampled frame, and **refuses to write at all** when the rig or a pole cannot
be resolved, instead of shipping a silently-broken file.

## Panel reference

**Motion** — *Import motion (.vmd)* opens a file chooser, imports onto `Import Rig` (made
active + POSE automatically; selection elsewhere does not matter), and remembers the source path
for the Expressions panel. *Export motion (.bin)* asks for a destination with the export options
in the dialog's right column.

| Option (hover for the current wording in your language) | Effect |
|---|---|
| Clear the rig first | drop existing curves before import — turn on when replacing the song, off when layering |
| Static Precision (4) | rounding at which a channel counts as "never moves" and is written once as a constant; accepted files use 4 |
| Scale Keys (1.0) | sampled-frame → file-frame ratio; the 30→60 factor already lives in the scene time base, so keep 1.0 |
| Allow replacing | all four exports refuse to overwrite an existing file without it |

**Expressions** — the face export's two inputs and the matching controls:

| Control | Meaning |
|---|---|
| From the imported motion | read morphs from the `.vmd` that *Import motion* recorded (the dance now on the rig). Untick to pick a different `.vmd` here — it is only read, never loaded |
| Base PV script (.dsc) | the stock script to splice into: output = that script, face stream replaced |
| Slot table character | which performer's real slot table names are resolved against (MIK/LEN/…) |
| Script character slot | the `chara` parameter written into the cues *of this script* (0 = first dancer) |
| Replace the stock face stream | tick: empty the base's own `MOUTH_ANIM`/`EXPRESSION` first; untick: append |
| Alias answers (optional) | your `worklist` answers file — human decisions outrank every table |

**Camera** — a `Camera.vmd` path plus *Min camera Y* (raise frames below this height to the
floor; 0 = leave alone). The `.a3da` written is the file the game loads from `auth_3d`
(`CAMPV<pv>_BASE.a3da` inside the FArC is the same member format; packing is your mod
builder's step). The `file_name` stamped inside matches the output filename you chose.

**Music** — a source audio path and the encode options: channels (2 = smaller, plays
everywhere; 4 = what shipped songs are, with real rear mixes — do not fake it from stereo),
quality (`libvorbis -q`, −0.2…10; 6 is transparent for this use), peak normalise (0 = leave
level alone), overwrite. When no usable ffmpeg is found the panel says so and offers **Download
FFmpeg**.

**Task** — one card per run: stage name, MB/percent progress on the audio and ffmpeg stages,
elapsed/speed/ETA, a Cancel that stops at a safe point (the target file is never half-written),
and kept result cards (`finished / cancelled / failed at '<stage>'`) with per-card Dismiss.

## Provenance — what is inherited, what is new

This project stands on three published upstreams and borrows conventions from a fourth:

| Upstream | Role here |
|---|---|
| **[BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)** (`DIVA Tools`, MIT © 2024 ThisIsHH; by ThisIsHH & FlyingSpirits) | the fork base: the motion-export pipeline and the rig template — the A-POSE template `.blend` in `template/` derives from this project's rig, **built by FlyingSpirits** (upstream credits: *"their outstanding work on the MMD-DIVA rig"*), carried and fixed forward by this fork |
| **[DIVA_BlenderCameraTool](https://github.com/ThisIsHH/DIVA_BlenderCameraTool)** (MIT © 2023 ThisIsHH) | the camera-export **method**: DIVA's position+interest camera model, its unit/frame/axis conventions, and the `Camera.json` → a3da conversion route — here re-implemented end-to-end as VMD → `.a3da` |
| **[korenkonder/PD_Tool](https://github.com/korenkonder/PD_Tool)** | studied to pin down the binary formats: the mot-set container (`Mot.cs`, credited in `mot_writer.py`), the a3da container, and the `pv_db` command table used to decode/encode `.dsc` |
| **mmd_tools** (external add-on) | VMD bone conventions and the record→curve transform, imported and reused rather than duplicated |
| **diva-camtool** (thtrandomlurker) | cross-checked camera calibration facts (`Frame = ThirtyFrame * 2`, `MMD × 0.08` with Z mirrored, half-angle FOV), cited in `camera_a3da.py` comments |

### Inherited from the original add-on (kept, fixed, or re-housed)

- **The motion export core**: `BoneDataTypes.xml` (bone list, per-bone type, `IKTarget`), the
  mot-set writer's container layout, the per-frame parent-relative sampling
  (`bone_utils`), the keyset machinery (absent/static/linear/tangent selection), and
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
- **File → Export → Project DIVA Mot Set** menu placement.

### Changed behaviour worth knowing

- **No selection dependency**: the exporter takes `Export Rig` **by name** and prints which rig
  won; only a file with no `Export Rig` falls back to the active object.
- **Undriven poles refuse the export** instead of writing a frozen-pole file (see above).
- **Import is no longer a stub.** The original "Import motion" button was
  `# Not implemented yet!` rendered greyed out. Everything below is this fork's, built so that
  a `.vmd` import + full re-export reproduces the accepted file byte-for-byte:
  - an own SJIS VMD parser (`vmd_reader`) used for import bookkeeping, morph tracks and camera;
  - a per-bone record→curve pass reusing mmd_tools' converter, stepped by the task system
    (~2.2 s for a 30 MB dance where one uninterrupted `assign()` held the UI for 13 s);
  - the template's time-base contract restored on import (`fps 60`, `frame_map_old 30`,
    `frame_start 0`), with a warning when the scene disagrees;
  - fixed import arguments (`frame_set(0)`, `frame_margin=0`, …) because mmd_tools adds
    `frame_current` to every keyframe — a cursor parked at the end used to shift whole dances.
- **Every path is chosen in a real save dialog** with the options in its right column; tooltips
  carry the instructions; nothing writes without the overwrite refusal.

### Added on top (none of it exists upstream)

1. **Task system** — all four exports and the import run as cancellable, time-budgeted tasks
   (details below); finished cards persist with their outcome.
2. **Camera export** `.Camera.vmd → CAMPV*_BASE.a3da`, following the DIVA_CameraTool model
   end-to-end (method provenance above): dialect-aware VMD parsing, degrees-vs-radians detection
   by median, eye reconstruction from position+distance, Hermite tangents with their original
   (upstream `utilities/utils.py`) provenance kept, FOV written as radians half-angle +
   `fov_is_horizontal` like SEGA's own files, optional floor clamp.
3. **Song export** — any audio to the container the game reads, with a pure-Python
   Ogg/Vorbis prober (`audio_ogg`) that validates the result instead of trusting the encoder.
4. **Expression export** — VMD morphs → `MOUTH_ANIM`/`EXPRESSION` cue streams spliced into a
   stock `.dsc`, driven by a knowledge base measured from 1065 shipping PV scripts (265
   performances, 133k mouth cues, 9k expressions) and a strict refusal to emit ids no shipping
   script contains. It also writes the eye-motion file `pv_expression/exp_PV<id>.bin` and the
   morph audit beside the script. This is the largest single subsystem of the fork.
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
UI (ui.py)                panels, operators, option dialogs, language tables
  └─ task_blender.py      Blender boundary: modal timer driver, task cards, progress_draw
      └─ operation.py     Operation = stages = generators of time-bounded *units*
          └─ task_core.py pure scheduling/state, imports no bpy  ──┐
          └─ task_ops.py one operation per product: import/export  │
              └─ cores (import no bpy, headless-testable):         │
                   exporter · mot_writer · bone_utils · rig_utils  │
                   pole_utils · camera_core · cam_json · a3da      │
                   audio_ops · audio_ogg · face_core · morph_core  │
                   dsc_core · vmd_reader · vmd_tracks · exp_writer
                    morph audit:
                      mmd_morph       the morph model + the name catalog
                      morph_effect    measured effect signatures
                      source_model    PMX definitions (panel byte + vertex deltas)
                      diva_capability what DIVA can express, measured
                      morph_mapper    effect-based decisions
                      morph_timeline  lossless frame/weight transfer
                      morph_audit     completeness, provably
                      morph_pipeline  one entry point for all of it
```

Anything that can run without `bpy` does; the task layer can drive it headlessly, so the tested
path and the button path are the same path.

### The task system (why the UI never freezes)

An operation is a generator of stages; a stage yields **units** — zero-argument callables doing
one bounded piece of work. `drive()` runs units while the clock says the budget (~20 ms) is not
spent; a modal timer re-enters it every tick, `execute` runs the same object synchronously in
headless sessions. Details that measurement forced:

- **units are sized by the wall clock, checked before starting the next one** — a "bounded"
  unit still ranges 1.7 ms…517 ms, so the budget is a ceiling, not an average;
- stage progress uses **calibrated weights** (measured per pipeline), not an even split;
- `gc.disable()` during a tick: building a mot set's keysets allocates ~2 M two-element lists,
  and the collector's walk used to land *between* units (95 ms worst → 2 ms);
- **cancellation raises at a safe point**, and cores check cancel *before publishing*, so a
  cancelled export leaves the previous file untouched and no `.part` survives;
- long uninterruptible calls (`ffmpeg`, the face solver, the download) run on a worker thread
  via `OffThread`, watched in ~8 ms units — allowed precisely because those modules touch no
  `bpy.data`;
- finished cards retire instead of vanishing (a result must be readable after the run), bounded
  history, per-card Dismiss carrying its task id.

### Motion import (VMD → rig)

`vmd_reader` parses the SJIS layout by byte widths (111 B bone records, 24 B camera interp
bytes, …). VMD bezier handles (0–127 at a fixed stride) become F-curve handles following
mmd_tools' own `__setInterpolation` including its ±20 fallback; quaternion tracks sign-align
consecutive keys like mmd_tools does. The **transform convention is imported from mmd_tools,
not re-derived** — a second implementation of a convention is a second chance to get it subtly
wrong — while the record→curve loop is this add-on's own, per bone, cancellable, and ~6× faster
than the one-shot `assign()` it replaces (and it compares F-curve-for-F-curve with the accepted
result). Unmapped tracks are reported, never guessed.

### Motion export (rig → mot set)

Per sampled frame: drive the elbow poles (`pole_utils`, name-resolved), evaluate, and collect
each Export-Rig bone's parent-relative matrix in 3-frame groups (a measured RNA/C-API balance;
`collect` is ~76 % of the export). Quaternion → euler conversion evaluates **both branch
families** and keeps the branch that continues the previous key (the shipped version's
short-circuit could flip a branch mid-turn and add a full spin the dance never had).
`rig_utils` decides which bone carries the whole-body facing (the template's own
`センター`→`kl_hara_xz` copy plus the equally legitimate `グルーブ`, WORLD-pinned, proven
byte-identical on the accepted dance) and `spine_hoist` re-roots a turn that would otherwise
pin 180° onto the hip bone SEGA leaves near-rest. Each channel's values are classified
absent/static/linear/tangent by `is_static` at the chosen decimals; keys are appended (binary
`insert` cost ~31.6 ms per 970-key track) and written via `foreach_set`. The writer's layout
mirrors PD_Tool's `Mot.cs`. Cancel/failure leave nothing behind; success writes
`<name>.bin` from a `.part` rename.

### Camera (`.vmd` → `.a3da`)

DIVA's camera = eye position + interest + roll + fov, not a 6-DOF transform. The MMD camera
record's layout varies by exporter dialect — the reader auto-detects which slot carries the
distance and whether rotations are degrees (median magnitude decides; authors type extra full
turns: reference files hold 183 rad at one key). Eye is reconstructed as position + forward ×
distance; Hermite tangents reproduce mmd_tools' calculation, sampled dense at 60 fps (SEGA
ships one key per frame), FOV becomes a radians half-angle with `fov_is_horizontal=1` — the
loader reads only `view_point.fov`. The a3da module assembles a `CAMPV*_BASE.a3da` that
round-trips byte-exactly with shipped members; packing into FArC is intentionally out of scope.

### Song (any audio → Ogg/Vorbis, SEGA-shaped)

`audio_ogg` is a pure-stdlib Ogg page + Vorbis header parser: it proves what a file *is* (rate,
channels, blocksize byte, framing bit, CRCs, and duration from the last page's granule — which
is exactly what DIVA uses as song length; nothing in `pv_db` carries a duration).
The encode runs through ffmpeg in **`-q:a` mode because only quality mode reproduces SEGA's
id-header shape** (maximum 0 / lower 0 / nominal by channels), and is verified: sample count
must match the decoded source within a sample or the file is never published. Quad mode uses
`pan=4c` (naming the layout would make ffmpeg up-mix and reinterpret the levels); silence in
the rear pair is explicit, and the docs say so — a 2-channel song is legal, dead-silent fake
quad is not honest. Peak normalise accounts for vorbis overshoot (+0.94…+2.23 dB), promising
only "within tolerance, never above full scale".

### Expressions (morphs → `MOUTH_ANIM` / `EXPRESSION`)

The maths lives in bpy-free cores and is driven headlessly, so the tested path and the button path
are the same path. In order:

1. **Name matching** (`morph_core`): normalise (NFKC/casefold/punctuation-fold), exact against
   the character's real slot table, then the bundled alias tables, then approximation, then the
   user's **worklist** answers — which outrank every automatic tier. Unresolved names are
   written to a worklist for human answers, *never guessed into a shape* (`blink` has no slot
   at all: reported, not mapped). The game-data path reads `rob_mot_tbl.bin`/`mot_db.farc` when
   an extracted rom is available (optional `DIVA_DATA_ROOT`), otherwise the packaged tables —
   and the report says which source answered.
2. **Sequence solving** (`face_core` + `data/*.json`): the mouth is not an emission per morph —
   it is a Viterbi solve over visemes using priors measured from SEGA's own scripts: cue
   spacing (~0.198 s median), transition matrix (82 % of cues change shape), duration
   histograms, lyric-cue offsets (a mouth cue follows a lyric cue by ~0.107 s, never precedes),
   expression co-occurrence, and a section-density profile. One-frame flickers are merged,
   singleton visemes dropped, and the result is held inside the corpus's own cue-rate band
   (ceiling from the corpus p90, merging the *shortest* events — measured, not assumed:
   a per-event floor once collapsed 72 % of a performance and made the mouth look locked).
   Every emitted id must exist in a shipping script (`diva_face_targets.json`); nothing novel
   is ever written.

3. **Splice + encode** (`dsc_core`): the base `.dsc` is decoded, its face records emptied (in
   *replace* mode) and ours inserted, then the whole file re-encoded and **re-read and
   validated against every rule shipping scripts obey** (no bare `TIME` blocks, nothing after
   `PV_END`, command widths from the `pv_commands.json` table) before publication. Non-face
   records come back bit-identical; `hold`/dialect quirks are honoured.

4. **The face** (`face_core` + `data/expression_rules.json`): a DIVA face is a **held state** that
   carries the eyes, so an expression is not a blend to match — it is a **threshold condition** on
   the dance's own morph weights, and the readings in that table were taken off the character in
   game (`MIK_FACE_SAD` is `困る` engaged; `MIK_FACE_CLOSE` is `まばたき` at 1). Rules are ranked by
   tier, and a face is always released again, because a face left on *is* an eye left moved. The
   **eyelids** follow the dance's own `まばたき` — a full closure writes the shut cue, the frame it
   leaves writes the open one, and `AUTO_BLINK` is never written. The **gaze** becomes
   `pv_expression/exp_PV<id>.bin` (`exp_writer`, layout-verified before it is published), plus the
   one record in the script that makes the engine read it.
5. **Strength and hold.** `MOUTH_ANIM`'s weight and `EXPRESSION`'s intensity are the dance's own
   level written on the bands SEGA's own scripts use (weight centred on 100, intensity 100..200),
   read relative to each morph's own peak — the same rule the trigger threshold uses, because an
   MMD morph's absolute scale is the model author's choice. The engine holds a record's value until
   the next one, so the strength is re-stated as it moves, and a shape the dance holds longer than
   the engine's 1000 ms **hold** is re-stated inside it.
6. **The morph audit** (`morph_pipeline`): every morph the motion animates is classified, mapped and
   accounted for, and anything DIVA has no dimension for is reported *with that reason*. A name the
   tooling cannot resolve is never guessed into a shape — it is written to a worklist for a human
   answer, and the audit says which morphs were answered that way.

### Language

Every visible string lives in `TEXTS` (en/`zh_CN`, key sets locked equal by tooling) or
`hints.json` — 240 keys, panel headers, option labels, hover text, enum items, task-card words and
operator tooltips. Because Blender bakes `bl_label` into the class registration, language switches
re-register the affected panels/operators (timer-deferred so a class is never swapped during its own
draw) and rebuild Scene-property definitions — which is safe: stored values survive the
delete/re-add (probed across all property kinds). The panel title and every option tooltip follow
Blender's UI language; the tab name never does.

Two things are checked at **build** time rather than trusted: that every language carries the same
key set, that no key is defined in both tables (where the `TEXTS` copy could never render), and that
every `L()` key exists — a missing one degrades to the English source string with no trace, which is
invisible in a language you read. What deliberately stays English is the diagnostic text the
bpy-free cores raise: a failed task card shows it as the detail line under a translated frame, so a
report can be pasted into a bug thread and read by anyone.

### Fetching ffmpeg (safe by design)

Nothing downloads until the Music panel's button is clicked, the download runs on a worker
watched by the scheduler (MB progress, real Cancel, partial files deleted), the zip's sha256
must match the constant pinned in the module (a mirror can change the URL, never the
verification), and only `ffmpeg.exe`, `ffprobe.exe` and `LICENSE` are unpacked — into the
add-on's own `bin/`, which the finder probes after `PATH`. The add-on never ships binaries;
the user's machine fetches them from the build host ffmpeg.org links.

## Output formats at a glance

| Output | Container facts |
|---|---|
| `MOT_*.bin` / mot set | keyset table (583 slots for the shipped skeleton), 2-bit kind per keyset (absent/static/linear/tangent), dense 60 fps linear keys, float32 values, `bone_info` trailer — per `PD_Tool/KKdMainLib/Mot.cs` |
| `CAMPV<pv>_BASE.a3da` | a3da member format (named channels, Hermite/static records, `PlayControl.Size`), round-trip-checked against a shipped camera member |
| `pv_<id>.ogg` | Ogg/Vorbis I, 44100 Hz, blocksize `0xB8`, 2 or 4 channels, duration authoritative from the last granule |
| `PV*.dsc` | `TIME` in 1/100000 s units, `MOUTH_ANIM(chara, 0, shapeIdx, weight, hold)` with the *mouth-table index*, `EXPRESSION(chara, slotId, intensity, −1)` with the **`rob_mot_tbl` slot** — two numbering systems on purpose, learned from 45 inspected originals |
| `pv_expression/exp_PV<id>.bin` | version 100, 8-byte block descriptors at 32, a NUL-terminated name table, one terminator per region, 16-byte records `(time, tag<<16\|group, value, duration)` on the 60 fps grid; gaze on tags 6/15, 0.5 = straight ahead |
| `<script>.morph_audit.json` | one record per source morph: its PMX definition, the category it was classified into, the DIVA slot it was mapped to (or the reason it has none), the applied score breakdown, and the keyframes transferred vs dropped |

Game-side mod packaging (FArC containers, `pv_db` rows, model swaps) is out of scope:
this add-on produces the members, mod builders assemble.

## Troubleshooting

| Message | What it means / what to do |
|---|---|
| `Need mmd_tools enabled to import a .vmd` | enable the mmd_tools add-on (it owns the conventions the importer borrows) |
| `This file has no Import Rig armature…` | you are not in the template file (or it was renamed) — motion import is by design template-bound |
| `elbow poles are not driven, refusing to export…` | the template is missing `Import Rig` / `ElbowPole_左/右` or the MMD arm bones; the error names the missing pieces; fix them (or restore the template) instead of re-exporting blindly |
| `… already exists — tick "allow replacing"…` | the overwrite guard; retarget the filename or opt in |
| `no usable ffmpeg found …` | click **Download FFmpeg** in the Music panel, or point `MMD2DIVA_FFMPEG` at any libvorbis build |
| `the fetched ffmpeg failed its checksum` | refuse-and-report by design: the pinned download was altered in transit (mirror/proxy); retry, or set `MMD2DIVA_FFMPEG` to a trusted binary |
| time-base warning after import | the scene's fps/frame mapping do not match the template contract (60 fps, map 30→60); re-import restores it — check that you did not change the scene afterwards |
| expression export lists names in the worklist | human decisions, not bugs: fill `[ ]` with the character's game slot name, re-run; your answers outrank every automatic table |
| a stage/card says `failed at '<stage>'` | the card keeps the error line and the console gets the traceback; the target file was not touched |

## Known limitations

- **In-game acceptance exists for one title only**: everything here was validated against
  *Hatsune Miku Project DIVA MEGA39's+* on PC — the mot export byte-checked against an
  accepted shipping conversion, the camera/song/face outputs re-read and validated against
  measured MEGA39's+ game-file rules, and the results loaded in that game. Other entries in
  the series (Future Tone, Arcade, PSP/Switch titles, or console MEGA39's+) share the formats
  but **have not been tested** — treat cross-platform use as unverified, your mileage is your
  own experiment. Whether your mod setup wants loose files or repacked `auth_3d`/`script`
  FArCs depends on the patch tooling you use.
- Face density for mocap-sourced dances can exceed what a stock script carries (up to ~2.4k
  `MOUTH_ANIM` cues); the engine handles it (SEGA ships 2.4k in a dense PV), but check your own.
- The camera module exports **camera** a3da only (not `MOC`/`OBJ` members).
- No Japanese UI currently (English + 简体中文; other Blender locales land on English).
- FArC packing, `pv_db` editing, and stage data are explicitly out of scope.

## Development notes

Repository layout:

```
DIVA_PV_Tools/
├── README.md · CHANGELOG.md · LICENSE · .gitignore
├── build.bat · make_release.py            ← release build + install probe
├── dist/                                  ← the built artifact (not committed)
├── diva_pv_tools/                         ← the add-on package, shipped as source
│   ├── *.py                               (38 modules)
│   └── data/ + *.json                     (slot tables, name tables, face knowledge base)
└── template/
    └── MMD to DIVA (A-POSE)_fork.blend    ← motion-pipeline working file (5.1 MB)
```

Releases additionally carry the installable `diva_pv_tools-<version>.zip` — the same package tree
plus the three root documents inside the add-on directory, zipped with exactly one top-level
directory as Blender's installer requires.

- Flat package: format cores (bpy-free, unit-testable), task layer (`task_core` imports no bpy),
  Blender boundary (`ui.py`, `task_blender.py`), template bindings (`bone_utils`, `rig_utils`,
  `pole_utils`, `exporter`). Shared files `task_core/operation/task_blender` are byte-identical
  copies in the companion `motion_refinery`; change them in one, sync to the other.
- Style: stdlib + `bpy` only (no pip), ≤ 100 columns, no type-hint churn, every non-obvious
  number in comments carries how it was measured.
- The release zip is built and *install-probed* by `make_release.py`, driven by `build.bat`: it
  refuses a wrong layout, a stray `__pycache__`, a missing required entry, a translation table whose
  languages disagree in either direction and an `L()` key with no entry — then installs the zip into
  a throwaway config and enables it there (a real enable → operator registry → disable → re-enable
  cycle). Two failures here are `build.bat` returning non-zero, never a warning.
- The add-on phones home to nobody: the only network code path is the user-triggered ffmpeg
  fetch; the only writes are to the files you name and `bin/` after that click.

## Credits & license

- **[BlenderDivaTools](https://github.com/ThisIsHH/BlenderDivaTools)** — ThisIsHH &
  FlyingSpirits — MIT © 2024 ThisIsHH. The motion-export core, the template rig workflow, the
  pole placement rule, and the `calculate_hermite_tangents` port (source-cited where used).
- **[DIVA_BlenderCameraTool](https://github.com/ThisIsHH/DIVA_BlenderCameraTool)** — MIT
  © 2023 ThisIsHH — the camera-export model this fork's `cam_json`/`camera_core` implement.
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