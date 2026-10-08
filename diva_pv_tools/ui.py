"""Sidebar UI: the panels, their operators, the popup option dialogs and the language table.

This is the fork's UI layer.  Nothing here touches the export maths: the motion panel calls the
same `exporter.export_motion` the accepted file was produced with, and the camera/audio/morph panels call
pure-python cores (`camera_core`, `audio_ops`, `morph_core`) that hold no Blender dependency, so each one
can be - and is - tested headlessly without this file.

Three UI decisions are deliberate:

* Every path is typed or browsed **in the panel first**; the button then opens an options popup whose Run
  does the work.  A bare click that opens a file browser would hide the input choice inside the dialog
  and leave nothing to review before something is written.
* The camera writes the `.a3da` the game loads directly; the UI offers no FArC wrapper step.
* All visible strings, including operator tooltips, come from `L()`.  Tooltips are re-applied on each draw
  by `relabel()`, because Blender bakes `bl_description` at registration while the language can change
  afterwards.  Each tooltip states what the step does *and* how to use it: that is the only place long
  instructions fit without filling the panel.
"""
import json
import os
import sys
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper
from . import task_blender as tb
from . import task_core as tc

TAB = "DIVA PV"

TEXTS = {
    "en": {
        'face_solver_tip': "Solve the mouth as one performance (a Viterbi pass over cue statistics measured from SEGA's own scripts) instead of one cue per morph edge",
        'face_approx_tip': 'Also accept near-miss mouth names - the tier below your worklist answers and above giving up',
        'face_format_note': 'Script format note: %s',
        "motion": "Motion",
        "camera": "Camera",
        "music": "Music",
        "motion_vmd": "Source motion (.vmd)",
        "import_vmd": "Import motion (.vmd)",
        "import_vmd_tip": (
            "Load the .vmd above onto the Import Rig. MMD dances are authored at "
            "30 fps while this template samples at 60, so the rig is keyed at "
            "half the scene frame, which the exporter relies on. Requires "
            "mmd_tools enabled. Existing curves are kept, so bones the vmd does "
            "not animate stay unchanged; turn on Clear the rig first only when "
            "switching songs."
        ),
        "export_mot": "Export motion (.bin)",
        "export_mot_tip": (
            "Writes the active armature's per-frame parent-relative transforms "
            "as a DIVA mot set. Select Export Rig first (in the outliner, or use "
            "Auto-Detect below). The elbow poles and spine mapping are driven "
            "during the export, not before, so the viewport may not show exactly "
            "what gets written."
        ),
        "morph": "Export expressions (.dsc)",
        "morph_tip": (
            "Converts the .vmd's mouth morphs into the MOUTH_ANIM cues of a DIVA "
            "PV script. Fill the base script above - usually the .dsc already shipped "
            "with this PV. The output keeps its notes, stage cues and timings "
            "untouched and swaps only the mouth stream. Names are matched against the "
            "character's real mouth table; anything unresolved is written to the "
            "worklist for you to fill, then reused as an alias table next run - never "
            "guessed. MMD expression morphs are NOT transplanted: a DIVA face is a "
            "held state that carries the eyes, so one that is merely wrong is far more "
            "visible than a missing one - they land on the worklist instead. The "
            "eyelids are the exception, and the blink option below chooses how: the "
            "game's own automatic blink, or the dance's まばたき curve. "
            "Uses the motion .vmd above."
        ),
        "worklist": "Worklist to fill",
        "alias": "Alias answers (optional)",
        "rom_root": "DIVA rom root",
        "camera_vmd": "Camera motion (.vmd)",
        "camera_a3da": "Camera model (.a3da)",
        "camera_export": "Export camera (.a3da)",
        "camera_export_tip": (
            "Converts the camera .vmd above into the DIVA camera model: the "
            "eye point is rebuilt from position plus distance along the view "
            "axis, MMD's angle becomes the horizontal field of view, and one "
            "source frame becomes two. The result is written as a text .a3da "
            "and re-read and checked before it replaces anything."
        ),
        "camera_min_y": "Minimum camera height (m)",
        "camera_min_y_on": "Clamp to a minimum height",
        "camera_min_y_on_tip": ("Lift frames that fall below the height on the slider; "
                                "leave off to keep every frame exactly where the camera was edited."),
        "camera_height": "Camera height offset (m)",
        "camera_height_tip": ("Moves the whole shot up or down - viewpoint and target together, "
                              "so the framing keeps its angle. Use it to line the export up with the game floor."),
        "camera_min_y_tip": (
            "Frames below this height are lifted to it. 0 exports the path "
            "unchanged."
        ),
        "audio_src": "Source audio",
        "audio_out": "Output (.ogg)",
        "audio_export": "Export audio (.ogg)",
        "ffmpeg_missing": "No usable ffmpeg was found.",
        "ffmpeg_missing_how": (
            "The song export encodes with ffmpeg's libvorbis. One download "
            "sets it up: the official portable build, verified before use."
        ),
        "fetch_ffmpeg": "Download FFmpeg",
        "fetch_done": "FFmpeg was installed into the add-on's bin folder and is ready to use.",
        "fetch_cancelled": (
            "The ffmpeg download was cancelled. The partial file was deleted "
            "and nothing was installed."
        ),
        "audio_export_tip": (
            "Encodes the file above into the Ogg/Vorbis 44100 Hz format the "
            "game loads. It writes a temp file, re-reads it with its own "
            "bitstream parser, and replaces the output only after all checks "
            "pass, so a failure leaves the existing file untouched. Stereo is "
            "smaller and plays everywhere; quad matches the shipped songs."
        ),
        "audio_channels": "Channels",
        "audio_quality": "Quality (-q)",
        "audio_normalize": "Normalise peak",
        "audio_normalize_tip": (
            "Peak target of the output. 0.95 is safe; 0 leaves the level "
            "alone."
        ),
        "audio_overwrite": "Allow replacing an existing file",
        "face_panel": "Expressions",
        "face_from_motion": "from the imported motion",
        "face_from_motion_tip": (
            "Read the morphs from the .vmd given to Import motion, which "
            "is the dance currently on the rig. Untick this to pick a "
            "different .vmd here (the same one is fine); it only decides "
            "where the cues are read from."
        ),
        "face_vmd": "dance .vmd to read",
        "face_vmd_tip": (
            "The dance whose morph tracks become the MOUTH_ANIM / EXPRESSION cues. "
            "Used only when \"from the imported motion\" is off; it need not be "
            "loaded into the scene and is never modified."
        ),
        "face_out": "PV script to write (.dsc)",
        "face_menu": "Expression export options",
        "face_code": "Slot table of",
        "face_chara": "Character slot in the script",
        "face_replace": "Replace the script's own face stream",
        "face_solver": "Sequence model",
        "face_approx": "Use approximations",
        "face_gap": "Minimum gap between mouth cues (ms)",
        "face_replace_tip": "Drop the script's own lip sync first, so two mouths do not overlap.",
        "limb_clamp": "Clamp unreachable limb targets",
        "limb_clamp_tip": ("For moves that stretch an arm or leg past its joint: pulls the hand or "
                           "foot target back inside the limb length, so the engine never locks an "
                           "elbow or knee straight. Off by default, and turning it on never changes "
                           "an export that already passed its checks."),
        "face_gap_tip": ("Mouth cues closer together than this are thinned, so the engine gets "
                         "readable syllables; 0 keeps every cue."),
        "face_auto_blink": "Automatic blink (the game's own)",
        "face_auto_blink_tip": ("Let the engine blink by itself instead of following the dance. "
                                "Ticked, the script carries AUTO_BLINK(0, 1) and the neutral face "
                                "EXPRESSION(0, 21, 100, 0) - without that face the automatic blink "
                                "does nothing - and the .vmd's まばたき curve is ignored. Unticked "
                                "(the default), no AUTO_BLINK is written and the dance's own "
                                "まばたき drives the eyelids through EXPRESSION(0, 22, 100, 1000) "
                                "while fully closed and EXPRESSION(0, 21, 100, 0) once it leaves "
                                "that value. Only a full closure counts as a blink."),
        "face_done": (
            "%s: %d mouth and %d expression cues, %d records over %d frames, replaced "
            "%s"
        ),
        "face_blink_done": "blink: %s",
        "camera_overwrite": "Allow replacing existing files",
        "mot_overwrite": "Allow replacing an existing file",
        "camera_menu": "Camera export options",
        "audio_menu": "Audio export options",
        "exists": "%s already exists - tick the overwrite option, or choose another name",
        "pick": "Pick a file",
        "pick_tip": (
            "Opens a file browser and writes the choice into the path field at left. "
            "It runs nothing: fill the fields, review them, then press the export "
            "button."
        ),
        "run": "Run",
        "cancel": "Cancel",
        "need_import_rig": "This scene has no 'Import Rig' armature",
        "need_mmd_tools": "mmd_tools is not enabled; importing a .vmd needs it",
        "need_file": "Fill in the input file in the panel first: %s",
        "need_import_first": (
            "Import the motion first: expressions come from the .vmd given to "
            "Import motion, and this file has none recorded yet."
        ),
        "need_out": "Fill in the output file in the panel first: %s",
        "timebase": "Time base: source keys %d..%d, scene %d..%d @ %d fps",
        "timebase_fixed": "frame_end moved to %d so one source frame stays two scene frames",
        "timebase_set": (
            "The scene was not on this template's 1:2 time base, so the import set "
            "it: %s"
        ),
        "timebase_bad": (
            "frame_current_final is not frame_current/2. The export samples scene "
            "frames, so this rig would play at the wrong speed."
        ),
        "vmd_done": "Imported %s (%d keyed frames)",
        "a3da_done": "Wrote %s (from %s, %d bytes, %d frames, file_name=%s)",
        "ogg_done": "Wrote %s (%d bytes, %s Hz, %d ch)",
        "encoder": "encoder: %s",
        "optimize": "Motion Cleanup",
        "optimize_tip": (
            "Turn the imported action into a new one whose joints, timing, and "
            "contacts are inside human range, without flattening the dance. "
            "Analyses first, then optimizes only the windows that need it."
        ),
        "opt_analyse": "Analyse only",
        "opt_apply": "Optimize",
        "opt_pair": "Build A/B rigs",
        "opt_analyse_tip": (
            "Find problems and change nothing: frames, bones, issue, severity, "
            "and the before metrics."
        ),
        "opt_apply_tip": (
            "Write the optimized pose into a new action, so the imported one "
            "survives and the two can be switched."
        ),
        "opt_pair_tip": (
            "Duplicate the rig as Motion Original and Motion Optimized, so both "
            "poses can be scrubbed side by side."
        ),
        "naturalness": "Naturalness (de-mechanicalise)",
        "natural_strength": "Strength",
        "opt_debug": "Diagnostic mode",
        "advanced": "Advanced",
        "opt_advanced_tip": (
            "Show internal parameters. All are computed from the analysis by "
            "default; switch one off to set it by hand."
        ),
        "auto": "Auto",
        "opt_auto_tip": "Let the analysis decide this value. Turn it off to set it yourself.",
        "analysis_report": "Analysis",
        "run_analysis_first": "Run 'Analyse only' to see what was detected.",
        "opt_analysis_tip": (
            "What the last 'Analyse only' run detected: problem, frames, "
            "bones, and suggested fix. Analysis modifies nothing."
        ),
        "opt_mode": "Mode",
        "opt_mode_tip": (
            "auto: detect anomalies, then optimize only those windows. local: "
            "same, limited to the body part below. whole_body: solve the whole "
            "clip at once."
        ),
        "opt_strength": "Strength",
        "opt_strength_tip": (
            "How much the constraints are enforced against keeping the "
            "original pose. 0 changes nothing; 1 is the full solve. This "
            "scales solver weights, not a blend of two poses, so a lower "
            "setting moves less rather than smoothing more."
        ),
        "opt_region": "Body part",
        "opt_region_tip": (
            "Which chain the solver may touch. Head/Neck, arms, legs, and feet "
            "are separate, so a hand can be fixed without redrawing the spine."
        ),
        "opt_smooth": "Temporal smoothness",
        "opt_smooth_tip": (
            "Shared weight on the velocity / acceleration / jerk rows. Higher "
            "removes more roughness but can flatten a fast move; defaults keep a "
            "snap turn snappy."
        ),
        "opt_vel": "Velocity weight",
        "opt_vel_tip": (
            "Weight on how far each frame's step departs from the original's. Keep "
            "it low: a genuine fast move must stay fast. One-frame spikes are "
            "caught by the rows below."
        ),
        "opt_acc": "Acceleration weight",
        "opt_acc_tip": (
            "Weight on the change of the step. Removes a two- or three-frame wobble "
            "without touching the move it sits on."
        ),
        "opt_jerk": "Jerk weight",
        "opt_jerk_tip": (
            "Weight on the third difference, measured against the window's own "
            "roughness. Removes frame-to-frame jitter."
        ),
        "opt_limits": "Enforce joint limits",
        "opt_limits_tip": (
            "Soft range per joint for swing and twist about the bone's own axis: "
            "a free preferred band, a quadratic band, then a hard limit. Never a "
            "clamp - the solver moves the error to whichever chain joint can "
            "take it."
        ),
        "opt_limit_weight": "Joint limit weight",
        "opt_limit_weight_tip": "How strongly a joint outside its preferred band is corrected.",
        "opt_chain": "Chain share weight",
        "opt_chain_tip": (
            "How strongly a turn crammed into one torso segment is spread over "
            "the chain. This catches a 360-degree spin authored on the waist."
        ),
        "opt_contact": "Foot contact",
        "opt_contact_tip": (
            "Detect a planted foot from height and speed - as a confidence, not "
            "a switch - and hold its rotation while it is down, so the "
            "optimizer cannot gain smoothness by sliding a foot."
        ),
        "opt_contact_weight": "Contact weight",
        "opt_contact_weight_tip": "How strongly a planted foot is held still.",
        "opt_events": "Protect motion events",
        "opt_events_tip": (
            "Give onsets, peaks, reversals, and every frame where the joint is "
            "moving extra weight, so a nod stays a nod and a whip turn stays a "
            "whip turn."
        ),
        "opt_pad": "Window padding (frames)",
        "opt_pad_tip": (
            "How far each detected window is grown on both sides. The update fades "
            "in and out over this span, so optimized and untouched motion join "
            "smoothly."
        ),
        "opt_stride": "Sample stride",
        "opt_stride_tip": (
            "Analyse and solve every Nth frame. 1 is exact; on a long clip 2 "
            "halves the whole-body solve cost and writes back only the frames it "
            "saw."
        ),
        "hand_clamp": "Hand target clamp",
        "hand_clamp_tip": (
            "Pull an unreachable hand target back inside the arm's length. Kept "
            "separate from the foot clamp: an over-reaching arm is a "
            "choreography choice, an over-reaching leg is a floating character."
        ),
        "foot_clamp": "Foot target clamp",
        "foot_clamp_tip": "Pull an unreachable foot target inside the leg's own length.",
        "clamp_tolerance": "Tolerance (m)",
        "clamp_tolerance_tip": (
            "How far below the cap the easing starts. The correction is C1 "
            "at that knee and asymptotic to the cap, so a target crossing "
            "the boundary moves a little instead of snapping; the hard "
            "clamp's corner adds a velocity jump."
        ),
        "clamp_softness": "Softness",
        "clamp_softness_tip": (
            "0 reproduces the old hard clamp exactly, so an already-accepted "
            "export can be rebuilt byte for byte; 1 is a full soft "
            "projection."
        ),
        "clamp_blend": "Per-frame pull limit (m)",
        "clamp_blend_tip": (
            "Cap on how much the pull may change frame to frame, applied "
            "forward and backward to stay symmetric in time. 0 disables it."
        ),
        "clamp_angle": "Hinge angle at the cap (deg)",
        "clamp_angle_tip": (
            "The elbow/knee angle the cap leaves. 180 is the singular, fully "
            "straight pose the engine pops out of; 165 keeps a visible bend."
        ),
        "opt_need_action": "This rig has no imported action to clean up.",
        "morph_done": "matched %d, need your answer %d, unmatched %d - worklist lines: %s",
        "morph_ambiguous": "ambiguous: %s -> %s",
        "fail": "Failed: %s",
        "export_mot_title": "Export DIVA Mot Set (.bin)",
        "export_mot_desc": (
            "Write the Export Rig's pose per frame to a Project DIVA mot set "
            "(.bin), driving the elbow poles and the spine mapping the game "
            "uses to solve the limbs. The rig is found by name, so nothing "
            "needs to be selected."
        ),
        "general": "General",
        "menu_export_mot": "Project DIVA Mot Set (.bin)",
        "mot_done": "Export completed: %s",
        "mot_cancelled": "Cancelled at '%s' after %.1f s: %s untouched, no partial file left",
        "export_failed": "Export failed: %s",
        "task": "Task",
        "idle": "idle",
        "card_cancel": "Cancel",
        "card_dismiss": "Dismiss",
        "card_dismiss_all": "Dismiss all finished",
        "card_progress": "%s  %d%%",
        "card_counts": "%d / %d",
        "card_stage_run": "%s ...",
        "card_ended": "%s at '%s'",
        "card_elapsed": "elapsed %.1fs",
        "card_speed": "%.0f/s",
        "card_eta": "eta %.0fs",
        "card_done": "done",
        "end_finished": "finished",
        "end_cancelled": "cancelled",
        "end_failed": "failed",
        "cancel_tip": (
            "Ask the running task to stop. It stops at the next safe point, not "
            "mid-write in Blender, so the file is never left half-changed."
        ),
        "dismiss_tip": (
            "Remove this finished task's card. The work it reported is already done "
            "and committed; this only clears the report."
        ),
        "cancel_none": "nothing is running",
        "cancel_started": "cancelling %s",
        "still_running": "%s is still running",
        "dismiss_none": "no finished task to dismiss",
        "dismissed": "dismissed %d task card(s)",
        "cancelled_fmt": "cancelled at '%s' after %.1f s: %s",
        "tail_cancel_import": "%s was left with no action and the partial curves were removed",
        "tail_cancel_camera": "nothing was published",
        "tail_cancel_audio": "the encoder was stopped and nothing was published",
        "tail_cancel_face": "the base script and the previous output are unchanged",
        "unmapped_warn": "%d track(s) in the file match no bone on %s, e.g. %s",
        "task_import": "Import motion",
        "task_export_mot": "Export mot set",
        "task_camera": "Export camera",
        "task_audio": "Export audio",
        "task_face": "Export expressions",
        "ffmpeg_path": "FFmpeg executable (optional)",
        "ffmpeg_path_tip": ("Leave empty to find ffmpeg automatically - environment variable, PATH, the "
                            "add-on's own bin/ folder or the usual install places; point it at your "
                            "own ffmpeg.exe (built with libvorbis) when automatic finding fails."),
        "fetch_ffmpeg_tip": (
            "Download the pinned official Windows build (about 104 MB) into "
            "the add-on folder. An ffmpeg on PATH or $MMD2DIVA_FFMPEG is still "
            "preferred."
        ),
        "prepare": "prepare",
        "read": "read",
        "parse": "parse",
        "map bones": "map bones",
        "prepare curves": "prepare curves",
        "write keys": "write keys",
        "ik toggles": "ik toggles",
        "finish": "finish",
        "read tables": "read tables",
        "spine mapping": "spine mapping",
        "collect": "collect",
        "build keysets": "build keysets",
        "write file": "write file",
        "convert": "convert",
        "validate": "validate",
        "probe source": "probe source",
        "encode": "encode",
        "verify": "verify",
        "match morphs": "match morphs",
        "build plan": "build plan",
        "splice and validate": "splice and validate",
        "download": "download",
        "verify and install": "verify and install",
    },
    "zh_CN": {
        'face_solver_tip': '把嘴型当作整场演出求解（基于 SEGA 原装脚本指令统计的 Viterbi 路径），而不是每个 morph 边缘各发一条指令',
        'face_approx_tip': '同时接受近似匹配的口型名——优先级低于你在清单里填写的答案',
        'face_format_note': '脚本格式提示：%s',
        "motion": "动作",
        "camera": "镜头",
        "music": "音乐",
        "motion_vmd": "动作文件（.vmd）",
        "import_vmd": "导入动作（.vmd）",
        "import_vmd_tip": (
            "将上方 .vmd 读到 Import Rig。MMD 动作按 30fps 制作、模板按 60fps "
            "取样，所以骨架关键帧落在场景帧号的一半，导出依赖这点。需要启用 mmd_tools。默认保留已有曲线，.vmd "
            "未涉及的骨骼不动；仅换歌时勾选“先清空骨架”。"
        ),
        "export_mot": "导出动作（.bin）",
        "export_mot_tip": (
            "把当前激活骨架的逐帧相对父级变换写成 DIVA 动作数据。请先选中 Export "
            "Rig（在大纲里点，或用下面的自动识别）。肘极向目标与脊柱映射是在导出过程中驱动的，所以视口所见不一定等于写出的内容。"
        ),
        "morph": "导出表情（.dsc）",
        "morph_tip": (
            "把 .vmd 的口型 morph 转成 DIVA PV 脚本的 MOUTH_ANIM 指令。先在上方填入基础脚本，一般就是这个 PV "
            "已安装的原装 "
            ".dsc；输出只替换其中的口型流，音符、舞台与时间轴原样保留。morph 名按角色的真实口型表比对，匹配不上的写入待填清单供填写，"
            "下次作为别名表复用，绝不猜测槽位编号。**MMD 的表情 morph 不参与移植**：DIVA 的表情是引擎保持的状态且包含眼睛，猜错比缺失更明显，"
            "它们会出现在待填清单里。眼睑是唯一的例外，用下面的「自动眨眼」选择由谁驱动：游戏自带的自动眨眼，或动作里的 まばたき 曲线。"
            "输入使用上方填写的动作 .vmd。"
        ),
        "worklist": "待填清单",
        "alias": "alias 答案（可留空）",
        "rom_root": "DIVA rom 根目录",
        "camera_vmd": "镜头文件（.vmd）",
        "camera_a3da": "镜头模型（.a3da）",
        "camera_export": "导出镜头（.a3da）",
        "camera_export_tip": (
            "将上方镜头 .vmd 转为 DIVA 镜头模型：视点由“位置＋沿视线方向距离”重建，MMD 角度当作水平视角，1 帧源变 2 "
            "帧。输出为文本 .a3da，会先读回校验再替换旧文件。"
        ),
        "camera_min_y": "相机最低高度（米）",
        "camera_min_y_on": "限制最低高度",
        "camera_min_y_on_tip": "开启后，低于滑动条高度的帧会被抬到该高度；关闭则完全保留镜头编辑位置。",
        "camera_height": "镜头高度偏移（米）",
        "camera_height_tip": "整体上下平移镜头（视点与目标同步移动，不改变画面角度）；用于把导出对齐到游戏地面。",
        "camera_min_y_tip": "低于该高度的帧会被抬到该高度；填 0 则不抬。",
        "audio_src": "源音频",
        "audio_out": "输出（.ogg）",
        "audio_export": "导出音频（.ogg）",
        "ffmpeg_missing": "未找到可用的 ffmpeg。",
        "ffmpeg_missing_how": "歌曲导出用 ffmpeg 的 libvorbis 编码。下载一次即可配好官方便携构建（使用前会先校验）。",
        "fetch_ffmpeg": "下载 FFmpeg",
        "fetch_done": "FFmpeg 已装入插件的 bin 目录，可以直接使用。",
        "fetch_cancelled": "已取消 ffmpeg 下载并删除临时文件，未安装任何内容。",
        "audio_export_tip": (
            "将上方填入的音频编码为游戏读取的 Ogg/Vorbis 44100 "
            "Hz。先写临时文件，再用自带码流解析读回校验，全部通过后才替换目标文件，因此失败不会动到原文件。2ch 更省体积、到处能播；4ch "
            "与原装歌曲一致。"
        ),
        "audio_channels": "声道",
        "audio_quality": "质量 (-q)",
        "audio_normalize": "峰值归一化",
        "audio_normalize_tip": "输出峰值目标；0.95 安全，填 0 不动音量。",
        "audio_overwrite": "允许覆盖已存在的文件",
        "face_panel": "导出表情",
        "face_from_motion": "以导入的动作 .vmd 为来源",
        "face_from_motion_tip": (
            "从“导入动作”选中的 .vmd 读取表情，也就是当前骨架上的这段舞蹈。取消勾选后可在此另选 "
            ".vmd（选同一个也行），它只决定指令从哪里读取。"
        ),
        "face_vmd": "读取表情的舞蹈 .vmd",
        "face_vmd_tip": (
            "把哪些 morph 轨道变成 MOUTH_ANIM / EXPRESSION 指令，就选那段舞蹈的 .vmd。只在取消勾选“以导入动作 "
            ".vmd 为来源”时使用；它无需载入场景，也不会被改动。"
        ),
        "face_out": "要写出的 PV 脚本（.dsc）",
        "face_menu": "表情导出选项",
        "face_code": "槽表所属角色",
        "face_chara": "脚本里的角色槽位",
        "face_replace": "替换脚本自带的表情流",
        "face_solver": "序列模型",
        "face_approx": "使用近似映射",
        "face_gap": "口型指令最小间隔（毫秒）",
        "face_gap_tip": "间隔小于该值的口型指令会被抽稀，保证引擎能读出清晰音节；填 0 保留全部指令。",
        "face_auto_blink": "自动眨眼（用游戏自带的）",
        "face_auto_blink_tip": "勾选后由引擎自己眨眼，不跟动作走：脚本会写入 AUTO_BLINK(0, 1) 与中性表情 "
                               "EXPRESSION(0, 21, 100, 0)（没有这条表情自动眨眼不会生效），vmd 的 まばたき "
                               "曲线被忽略。不勾选（默认）则不写 AUTO_BLINK，改用动作自带的 まばたき 驱动眼睑："
                               "完全闭合时写 EXPRESSION(0, 22, 100, 1000)，离开该值时写 EXPRESSION(0, 21, 100, 0)。"
                               "只有完全闭合才算眨眼。",
        "face_replace_tip": "先删掉脚本自带的口型再写我们的，避免两张嘴重叠。",
        "limb_clamp": "钳制够不到的手脚目标",
        "limb_clamp_tip": "针对把手臂或腿拉得超过关节长度的动作：沿原方向把手/脚目标收回到肢长以内，避免引擎把肘或膝锁直。默认关闭，开启也不会改变已验收的导出。",
        "face_done": "%s：口型 %d 条、表情 %d 条，共 %d 条记录 / %d 帧，替换掉 %s",
        "face_blink_done": "眨眼：%s",
        "camera_overwrite": "允许覆盖已有文件",
        "mot_overwrite": "允许覆盖已有的动作文件",
        "camera_menu": "镜头导出选项",
        "audio_menu": "音乐导出选项",
        "exists": "%s 已存在——请勾选允许覆盖，或换一个文件名",
        "pick": "选择文件",
        "pick_tip": "打开文件选择器，只把所选结果写回左侧路径框，不执行任何操作。请先填写并检查各路径，再点导出按钮。",
        "run": "执行",
        "cancel": "取消",
        "need_import_rig": "当前场景没有 'Import Rig' 骨架",
        "need_mmd_tools": "mmd_tools 未启用：导入 .vmd 需要它",
        "need_file": "请先在面板里填/选输入文件：%s",
        "need_import_first": "请先执行“导入动作”：表情来自当时选定的 .vmd，当前文件还没有记录。",
        "need_out": "请先在面板里填输出文件：%s",
        "timebase": "时间基：源关键帧 %d..%d，场景 %d..%d @ %d fps",
        "timebase_fixed": "已把 frame_end 调为 %d，保持一源帧＝两场景帧",
        "timebase_set": "当前场景不符合模板要求的 1:2 时间基，导入时已设置：%s",
        "timebase_bad": "frame_current_final 不等于 frame_current/2，导出按场景帧取样，播放速度会出错。",
        "vmd_done": "已导入 %s（%d 个关键帧）",
        "a3da_done": "已写出 %s（来源 %s，%d 字节，%d 帧，file_name=%s）",
        "ogg_done": "已写出 %s（%d 字节，%s Hz，%d 声道）",
        "encoder": "编码器：%s",
        "optimize": "动作整理",
        "optimize_tip": "把导入的动作整理成一条新动作：关节、节奏与接地都回到人体可达范围内，同时不压平舞蹈。先分析，只优化需要优化的区间。",
        "opt_analyse": "仅分析",
        "opt_apply": "执行优化",
        "opt_pair": "生成 A/B 骨架",
        "opt_analyse_tip": "只报告问题（帧范围、骨骼、严重程度、修改前指标），不做任何改动。",
        "opt_apply_tip": "把优化后的姿态写入一条新动作；导入的原动作保留，可随时切换对比。",
        "opt_pair_tip": "把骨架复制为 Motion Original 和 Motion Optimized 两套，可并排拖动时间轴对比。",
        "naturalness": "自然度（去机械感）",
        "natural_strength": "强度",
        "opt_debug": "诊断模式",
        "advanced": "高级设置",
        "opt_advanced_tip": "显示内部参数。默认全部由分析结果自动决定；关闭某项即可手动设置该项。",
        "auto": "自动",
        "opt_auto_tip": "让分析自动决定这个值。关闭后由你手动设置。",
        "analysis_report": "分析结果",
        "run_analysis_first": "先执行「仅分析」即可看到检测结果。",
        "opt_analysis_tip": "最近一次“仅分析”检测到的内容：问题、时间区间、涉及骨骼与建议的修复方式。分析不会修改任何东西。",
        "opt_mode": "模式",
        "opt_mode_tip": "auto：先检测异常，只优化这些区间。local：同上，但限定为下方选中的部位。whole_body：整段一次性求解。",
        "opt_strength": "强度",
        "opt_strength_tip": (
            "约束相对于保持原姿态的强度。0 为完全不变，1 "
            "为完整求解。这里改变的是求解器权重，不是两套姿态的插值，所以调小意味着动得更少，而不是更平滑。"
        ),
        "opt_region": "身体部位",
        "opt_region_tip": "求解器允许改动的骨链。头/颈、手臂、腿、脚彼此独立，可只修手而不重画脊柱。",
        "opt_smooth": "时间平滑权重",
        "opt_smooth_tip": "速度/加速度/jerk 三项的总权重。调高去掉更多粗糙，也可能压平快速动作；默认保留利落感。",
        "opt_vel": "速度权重",
        "opt_vel_tip": "每帧步长偏离原始多少的权重。默认偏低：真正的快速动作必须保持速度。",
        "opt_acc": "加速度权重",
        "opt_acc_tip": "步长变化量的权重，用来去掉两三帧的抖动而不动整体动作。",
        "opt_jerk": "jerk 权重",
        "opt_jerk_tip": "三阶差分权重，以区间自身粗糙度为基准，消除逐帧抖动。",
        "opt_limits": "启用关节活动范围",
        "opt_limits_tip": (
            "每个关节按自身轴的 swing / twist 给出三段范围：preferred 免费、soft 二次惩罚、hard "
            "绝对上限。不是硬截断，求解器会把误差转移给链上接得住的关节。"
        ),
        "opt_limit_weight": "关节范围权重",
        "opt_limit_weight_tip": "超出 preferred 时的回调强度。",
        "opt_chain": "骨链分摊权重",
        "opt_chain_tip": "把集中在躯干单节的旋转分摊到整条骨链的力度。用于发现“整圈转身全写在腰上”。",
        "opt_contact": "脚部接地",
        "opt_contact_tip": "用高度与速度求接地的置信度（不是开关），接地期间保持脚的旋转，防止优化器靠滑步换取平滑。",
        "opt_contact_weight": "接地权重",
        "opt_contact_weight_tip": "接地期间保持脚不动的强度。",
        "opt_events": "保护动作事件",
        "opt_events_tip": "提高起势、峰值、反向以及关节运动帧的权重，让点头仍是点头、甩头仍是甩头。",
        "opt_pad": "区间前后余量（帧）",
        "opt_pad_tip": "每个检出区间向两侧扩展的帧数。修改在此宽度内淡入淡出，优化段与未动段平滑衔接。",
        "opt_stride": "采样步长",
        "opt_stride_tip": "每 N 帧分析并求解一次。1 为精确；长曲目用 2 可将全身求解开销减半，且只回写采样到的帧。",
        "hand_clamp": "手部目标钳制",
        "hand_clamp_tip": "把够不到的手部目标收回到手臂长度以内。与脚部钳制分开设置：手臂够得远常是编舞选择，腿够得远则角色会悬空。",
        "foot_clamp": "脚部目标钳制",
        "foot_clamp_tip": "把够不到的脚部目标收回到腿长以内。",
        "clamp_tolerance": "容差 (m)",
        "clamp_tolerance_tip": "从上限往下多少距离开始生效。拐点处 C1 连续、向上限渐近，所以跨过边界只动一点，不像硬钳制那样突然一拉。",
        "clamp_softness": "柔度",
        "clamp_softness_tip": "0 与旧硬钳制完全一致，可逐字节复现已验收的导出；1 为完整的软投影。",
        "clamp_blend": "每帧拉动上限 (m)",
        "clamp_blend_tip": "拉动量每帧允许变化的上限，前后双向施加以保持时间对称；填 0 关闭。",
        "clamp_angle": "上限处的关节角 (deg)",
        "clamp_angle_tip": "钳制上限给肘/膝留下的角度。180 是会被引擎弹开的完全伸直奇异位；165 保留可见的弯曲。",
        "opt_need_action": "这个骨架没有可整理的动作。",
        "morph_done": "自动匹配 %d，待你填 %d，无对应 %d —— 清单 %s 行",
        "morph_ambiguous": "待定：%s -> %s",
        "fail": "失败：%s",
        "export_mot_title": "导出 DIVA 动作数据（.bin）",
        "export_mot_desc": (
            "把 Export Rig 的逐帧姿态写成 Project DIVA "
            "动作数据（.bin），导出时驱动肘极向目标与解算四肢的脊柱映射。骨架按名称查找，无需选中。"
        ),
        "general": "常规",
        "menu_export_mot": "Project DIVA 动作数据（.bin）",
        "mot_done": "导出完成：%s",
        "mot_cancelled": "已在“%s”处中止（%.1f 秒）：%s 未被改动，也未留下残缺文件",
        "export_failed": "导出失败：%s",
        "task": "任务",
        "idle": "空闲",
        "card_cancel": "取消",
        "card_dismiss": "关闭",
        "card_dismiss_all": "关闭全部已完成",
        "card_progress": "%s  %d%%",
        "card_counts": "%d / %d",
        "card_stage_run": "%s ...",
        "card_ended": "%s：%s",
        "card_elapsed": "耗时 %.1f 秒",
        "card_speed": "%.0f/秒",
        "card_eta": "预计还需 %.0f 秒",
        "card_done": "完成",
        "end_finished": "已完成",
        "end_cancelled": "已取消",
        "end_failed": "失败",
        "cancel_tip": "请求正在运行的任务停止。它会在下一个安全点停下，而不是停在 Blender 写入中途，因此文件不会被写坏。",
        "dismiss_tip": "移除这张已完成任务的卡片。它报告的工作已完成并落盘，这里只是清掉这条报告。",
        "cancel_none": "当前没有运行中的任务",
        "cancel_started": "正在取消 %s",
        "still_running": "%s 仍在运行",
        "dismiss_none": "没有可关闭的已完成任务",
        "dismissed": "已关闭 %d 张任务卡片",
        "cancelled_fmt": "已在“%s”中止（%.1f 秒）：%s",
        "tail_cancel_import": "未给 %s 附加动作，已写一半的曲线已删除",
        "tail_cancel_camera": "未输出任何内容",
        "tail_cancel_audio": "编码器已停止，未输出任何内容",
        "tail_cancel_face": "底本脚本与上一次输出均未改动",
        "unmapped_warn": "文件中 %d 条轨道与 %s 上的骨骼不匹配，例如 %s",
        "task_import": "导入动作",
        "task_export_mot": "导出动作数据",
        "task_camera": "导出镜头",
        "task_audio": "导出音频",
        "task_face": "导出表情",
        "fetch_ffmpeg_tip": "将官方固定版（约 104 MB）下载到插件目录；若 PATH 或 $MMD2DIVA_FFMPEG 已有 ffmpeg，仍优先使用。",
        "ffmpeg_path": "FFmpeg 程序（可选）",
        "ffmpeg_path_tip": "留空则自动查找：环境变量、PATH、插件的 bin 目录及常见安装位置；自动找不到时，在此指向你自己的 ffmpeg.exe（需包含 libvorbis）。",
        "prepare": "准备",
        "read": "读取",
        "parse": "解析",
        "map bones": "骨骼匹配",
        "prepare curves": "曲线准备",
        "write keys": "写入关键帧",
        "ik toggles": "IK 开关",
        "finish": "收尾",
        "read tables": "读取表",
        "spine mapping": "脊柱映射",
        "collect": "收集帧",
        "build keysets": "生成键集",
        "write file": "写文件",
        "convert": "转换",
        "validate": "校验",
        "probe source": "探测源文件",
        "encode": "编码",
        "verify": "确认",
        "match morphs": "表情匹配",
        "build plan": "构建方案",
        "splice and validate": "拼接与校验",
        "download": "下载",
        "verify and install": "校验并安装",
    },
}

# The hover text for every option the dialogs draw.  It lives in hints.json because these strings are
# data, not code: three tables that have to stay in step, which is what the build-time key check
# asserts.  Registered properties bake their description at registration, so
# `refresh_props` reads the language out of here and rebuilds them.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hints.json"),
          encoding="utf-8") as _hints:
    for _language, _table in json.load(_hints).items():
        TEXTS[_language].update(_table)
del _hints, _language, _table


ALIASES = {"zh": "zh_CN", "zh_CN": "zh_CN", "zh_HANS": "zh_CN",
           "zh_HANT": "en", "zh_TW": "en"}     # anything else, ja included, is English
STATE = {"language": None}


def language():
    """Blender's own UI language, mapped onto the tables here.

    There is no language picker of our own: the panel follows Blender (English or Chinese -
    any other Blender locale is English), and the tab stays "DIVA PV" everywhere so it
    remains findable.  `DEFAULT` means "follow the system",
    so it is resolved through Blender's own system-locale reading rather than treated as
    English.  register_ui() runs this before Blender has a usable context - bpy.context is a
    _RestrictContext there and merely *raises* on .preferences - so the lookup is guarded and
    falls back to English.
    """
    try:
        want = bpy.context.preferences.view.language
    except AttributeError:
        want = "en_US"
    if want in ("", "DEFAULT"):
        want = getattr(bpy.app, "system_locale", "") or "en_US"
    return ALIASES.get(want, "en")


def L(key):
    table = TEXTS.get(language(), TEXTS["en"])
    return table.get(key) or TEXTS["en"].get(key, key)


def default_dir():
    return os.path.dirname(bpy.data.filepath) or os.getcwd()


# Is there a usable ffmpeg?  Answering costs a subprocess probe (`-encoders`), so the answer is
# cached for the session and invalidated only by events that can change it: the fetch step, and
# - cheaply - never the panel draw.  An unprobed state draws nothing: silence beats a warning
# line that might appear a frame late.
_FFMPEG = {"path": "", "checked": False, "manual": None}


def ffmpeg_probe(rescan=False):
    manual = ""
    try:
        manual = bpy.path.abspath(bpy.context.scene.diva_ffmpeg_path)
    except (AttributeError, RuntimeError):
        manual = ""                       # no scene yet (startup) - probe without it
    if rescan or not _FFMPEG["checked"] or manual != _FFMPEG["manual"]:
        # a retyped path is a decision made after the last probe: it must re-probe
        from . import audio_ogg
        found = audio_ogg.find_ffmpeg(required=False, extra_paths=[manual] if manual else ())
        _FFMPEG["path"] = found or ""
        _FFMPEG["manual"] = manual
        _FFMPEG["checked"] = True
    return _FFMPEG["path"]


def card_texts():
    """The words `progress_draw` draws but cannot own - this table is the language boundary.

    Stage names double as TEXTS keys (`L` falls back to the key, which is the English stage name
    a untranslated stage keeps), so a new stage needs no plumbing to stay readable - only to gain
    a translation.
    """
    return {"cancel": L("card_cancel"), "dismiss": L("card_dismiss"),
            "dismiss_all": L("card_dismiss_all"),
            "end_words": {tc.COMPLETED: L("end_finished"), tc.CANCELLED: L("end_cancelled"),
                          tc.FAILED: L("end_failed")},
            "messages": {"done": L("card_done")},
            "stage_name": L,
            "progress": L("card_progress"), "counts": L("card_counts"),
            "stage_run": L("card_stage_run"), "ended": L("card_ended"),
            "elapsed": L("card_elapsed"), "speed": L("card_speed"), "eta": L("card_eta")}


def scene_path(scene, name):
    return bpy.path.abspath(getattr(scene, name))


def find_rom_root(base, override=""):
    """Where the game's own slot tables live, worked out from the script that was picked.

    A stock script sits at `<game>/main/rom/script/pv_<id>_<difficulty>.dsc`, so three levels up is a
    rom root.  A mod's script sits at `<game>/mods/<mod>/rom/script/pv_<id>.dsc` and the mod rarely
    carries `rob_mot_tbl.bin` - the tables come from the game itself, the folder above `mods`.  Both
    layouts are tried at every ancestor, and a path typed into `diva_morph_rom_root` still wins
    outright.  Nothing found is not an error: a normal install keeps those tables inside
    `diva_main.cpk`, so "" is returned and `morph_core` reads the slot table that ships with the
    add-on instead - and the report line says which of the two was used.

    `base` may be None - the generated-scaffold mode has no borrowed base - in which case the
    motion file's own folder is climbed instead, because a mod's `.vmd` and its `.dsc` usually
    live in the same tree.
    """
    # exactly the paths the caller rebuilds from the returned root - a directory that merely has a
    # `rom/` of its own is not a rom root, and accepting one hands back a path that then fails to load
    tables = ("main/rom/rob/rob_mot_tbl.bin", "main/rom_switch/rom/rob/rob_mot_tbl.bin")
    if not base:
        return ""
    here = os.path.normpath(os.path.abspath(base)).replace("\\", "/")
    candidates = [override] if override else []
    for _step in range(9):
        here = here.rsplit("/", 1)[0]
        if not here:
            break
        candidates.append(here)
        if "mods" in here.split("/"):       # .../game/mods/<mod>/rom/script/x.dsc -> the game root
            pieces = here.split("/")
            candidates.append("/".join(pieces[:pieces.index("mods")]))
        if "/" not in here:                 # drive or filesystem root, stop climbing
            break
    for candidate in candidates:
        if candidate and any(os.path.isfile(os.path.join(candidate, tail)) for tail in tables):
            return candidate
    # Nothing loose to read - which is the normal state of a Steam install, where the tables live
    # inside diva_main.cpk.  Empty means "use the slot table that ships inside the add-on", and the
    # export report says so, so nobody is left wondering whether a missing rom folder broke it.
    return ""


class FaceError(RuntimeError):
    """Raised by find_rom_root before the export core is even reached, so the dialog reports it."""


def mmd_tools_vmd():
    import importlib
    for name in ("bl_ext.blender_org.mmd_tools.core.vmd.importer", "mmd_tools.core.vmd.importer"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


class DIVA_PV_OT_import_vmd(tb.TaskOperator, ImportHelper):
    """Load a 30 fps MMD motion onto the Import Rig, keeping the template's 1:2 time base.

    The work is `task_ops.VmdImportOperation`, which is a generator of time-bounded stages; this class
    only decides what to do with the result.  mmd_tools' `VMDImporter.assign()` is one long unbroken
    call - no progress, no cancel, and no way to yield, because the freeze is *inside* the call rather
    than around it.  See that operation for what replaced it and which of mmd_tools' conventions are
    still borrowed rather than reimplemented.
    """

    bl_idname = "diva_pv.import_vmd"
    bl_label = "Import motion"
    bl_description = "Load a 30 fps MMD .vmd onto the Import Rig"
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}
    filename_ext = ".vmd"
    filter_glob: StringProperty(default="*.vmd", options={'HIDDEN'})

    filepath: StringProperty(subtype='FILE_PATH')

    file_chooser = True     # invoke opens the browser; execute runs the import - see TaskOperator

    def draw(self, context):
        relabel()   # the option below is a Scene property: its label and hover follow the language
        self.layout.prop(context.scene, "diva_import_clear")

    def task_name(self):
        return L("task_import")

    def make_operation(self, context):
        from . import task_ops
        relabel()
        rig = bpy.data.objects.get("Import Rig")
        if rig is None:
            raise RuntimeError(L("need_import_rig"))
        if mmd_tools_vmd() is None:
            raise RuntimeError(L("need_mmd_tools"))
        # the chooser's own path wins; the panel field is the fallback for a scripted call
        path = bpy.path.abspath(self.filepath) if self.filepath else scene_path(context.scene,
                                                                               "diva_vmd_motion")
        if not os.path.isfile(path):
            raise RuntimeError(L("need_file") % L("motion_vmd"))
        for obj in context.view_layer.objects:
            obj.select_set(obj is rig)
        context.view_layer.objects.active = rig
        if rig.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        return task_ops.VmdImportOperation(context, rig=rig,
                                           options={"path": path, "scale": 0.08,
                                                    "clear_first": context.scene.diva_import_clear})

    def task_finish(self, context, task, operation):
        from . import task_ops
        task_ops.log_benchmark(operation)
        result = operation.result
        scene = context.scene
        # Remember the source: the expression export reads it from here, so the same file is not picked twice.
        scene.diva_vmd_motion = result.path
        self.report({'INFO'}, L("timebase") % (result.frames[0] if result.frames else 0,
                                               result.frames[-1] if result.frames else 0,
                                               scene.frame_start, scene.frame_end,
                                               scene.render.fps))
        if abs(scene.frame_current_final - scene.frame_current / 2.0) >= 1e-4:
            self.report({'WARNING'}, L("timebase_bad"))
        self.report({'INFO'}, L("vmd_done") % (os.path.basename(result.path),
                                               len(result.frames)))
        if result.unmapped:
            self.report({'WARNING'}, L("unmapped_warn")
                        % (len(result.unmapped), operation.rig.name,
                           ", ".join(result.unmapped[:4])))
        operation.report = operation.benchmark()

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            note = (L("cancelled_fmt")
                    % (L(task.stage), task.elapsed,
                       L("tail_cancel_import") % (operation.rig.name if operation else "the rig")))
            print("[Import] cancelled at '%s' after %.1f s" % (task.stage, task.elapsed))
            self.report({'WARNING'}, note)
        else:
            traceback.print_exc()
            self.report({'ERROR'}, "%s  [%s]" % (L("fail") % (task.error or reason),
                                                 _deepest_frame()))
        if operation is not None and operation.chunks:
            from . import task_ops
            task_ops.log_benchmark(operation, prefix="[Benchmark:aborted]")

def export_options(layout):
    """The switches the mot-set export dialog shows in its General panel.

    Every row here is a Scene property, so its label and hover text come from the language table
    (`refresh_props`); the panel itself carries no instruction lines - the hints live on hover rather
    than as text on the panel.
    """
    scene = bpy.context.scene
    layout.prop(scene, "diva_mot_decimals")
    layout.prop(scene, "diva_mot_scale_keys")
    layout.prop(scene, "diva_mot_overwrite")

def suggest(scene_prop, stem_fallback, suffix, extension):
    """Where the export chooser opens: beside the input, named after it.

    The panels carry no output field: the button opens a file chooser and that is where the
    location and name get picked.  Seeding it from the input means one export lands next
    to the file it came from instead of whatever folder the open .blend happens to sit in.
    """
    src = scene_path(bpy.context.scene, scene_prop)
    folder = os.path.dirname(src) or default_dir()
    stem = os.path.splitext(os.path.basename(src))[0] if src else stem_fallback
    return os.path.join(folder, "%s%s.%s" % (stem, suffix, extension))


class ChooserDefaults:
    """Opens the save dialog already pointed beside the input the panel holds.

    `UILayout.operator()` accepts only its own fixed keywords, so a default path cannot be handed over
    from the panel - it has to be set here, before the helper's invoke reads it.  `ExportHelper.invoke`
    seeds a path of its own (the .blend's name) and then calls `fileselect_add` unconditionally, so the
    dialog appears on every press; all this class does is make that first suggestion a better one.

    There is no handover logic here on purpose.  The file-chooser convention is that `invoke` opens the
    dialog and Blender calls **`execute`** once a path is accepted, and `TaskOperator` implements
    exactly that split - so the only thing left for this class to supply is the suggestion.

    Calling the task from `invoke` cannot work: `ExportHelper.invoke` returns `RUNNING_MODAL` whether
    the browser is open or the work is expected to start, so that result cannot tell the two apart.
    Testing `filepath` does not help either - the helper seeds a path before opening the browser, so a
    non-empty `filepath` means "the dialog is about to open", not "the user has chosen".
    """

    source_prop = ""          # scene property whose folder and stem seed the name
    fallback_stem = ""        # used when that property is still empty
    out_suffix = ""           # e.g. ".face" for the spliced PV script

    def invoke(self, context, event):
        if not self.filepath:
            self.filepath = suggest(self.source_prop, self.fallback_stem, self.out_suffix,
                                    self.filename_ext[1:])
        return ExportHelper.invoke(self, context, event)


class DIVA_PV_OT_camera(tb.TaskOperator, ChooserDefaults, ExportHelper):

    """Pick the file to write: the DIVA camera model built from the camera .vmd on the panel.

    The work is `task_ops.CameraExportOperation`.  Small next to the mot set, but it still exceeds the
    chunk budget on its own, which is the criterion for running it as a staged task.
    """

    source_prop = "diva_camera_vmd"
    fallback_stem = "Camera"
    out_suffix = ""

    bl_idname = "diva_pv.camera_export"
    bl_label = "Export camera"
    bl_description = "Choose the .a3da to write, then convert the camera .vmd typed on the panel"
    bl_options = {'PRESET', 'BLOCKING'}
    filename_ext = ".a3da"
    filter_glob: StringProperty(default="*.a3da", options={'HIDDEN'})

    file_chooser = True     # invoke opens the browser; execute runs the export - see TaskOperator

    def draw(self, context):
        relabel()   # open the dialog without the sidebar having drawn and these are still current
        scene = context.scene
        layout = self.layout
        layout.prop(scene, "diva_cam_height", slider=True)
        row = layout.row(align=True)
        row.prop(scene, "diva_camera_min_y_on", text=L("camera_min_y_on"))
        col = row.column()
        col.enabled = scene.diva_camera_min_y_on
        col.prop(scene, "diva_camera_min_y", slider=True)
        layout.prop(scene, "diva_camera_overwrite")

    def task_name(self):
        return L("task_camera")

    def make_operation(self, context):
        from . import task_ops
        relabel()
        scene = context.scene
        src = scene_path(scene, "diva_camera_vmd")
        if not os.path.isfile(src):
            raise RuntimeError(L("need_file") % L("camera_vmd"))
        out = bpy.path.abspath(self.filepath)
        if not out.lower().endswith(".a3da"):
            out += ".a3da"
        if os.path.exists(out) and not scene.diva_camera_overwrite:
            raise RuntimeError(L("exists") % (out,))
        return task_ops.CameraExportOperation(
            context, options={"src": src, "filepath": out,
                              "min_y": scene.diva_camera_min_y if scene.diva_camera_min_y_on
                              else None,
                              "y_offset": scene.diva_cam_height or None})

    def task_finish(self, context, task, operation):
        from . import task_ops
        task_ops.log_benchmark(operation)
        stats = operation.stats
        self.report({'INFO'}, L("a3da_done") % (os.path.basename(stats["out"]),
                                                os.path.basename(operation.src),
                                                stats["bytes"], stats["size"],
                                                stats["file_name"]))

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            self.report({'WARNING'}, L("cancelled_fmt")
                                   % (L(task.stage), task.elapsed, L("tail_cancel_camera")))
        else:
            self.report({'ERROR'}, L("fail") % (task.error or reason,))


class DIVA_PV_OT_audio(tb.TaskOperator, ChooserDefaults, ExportHelper):

    """Pick the .ogg to write: the source audio encoded into the form the game loads.

    The work is `task_ops.AudioExportOperation`.  The encoding time is spent *inside ffmpeg*, so there
    is no loop to chunk; what the operation adds is a cancel that reaches the child process, and a
    determinate bar, and both are explained in its docstring.
    """

    source_prop = "diva_audio_src"
    fallback_stem = "pv_song"
    out_suffix = ""

    bl_idname = "diva_pv.audio_export"
    bl_label = "Export audio"
    bl_description = "Choose the .ogg to write, then encode the audio file typed on the panel"
    bl_options = {'PRESET', 'BLOCKING'}
    filename_ext = ".ogg"

    file_chooser = True     # invoke opens the browser; execute runs the export
    filter_glob: StringProperty(
        default="*.ogg;*.wav;*.flac;*.mp3;*.m4a;*.aac;*.opus;*.wma;*.aiff;*.aif", options={'HIDDEN'})

    def draw(self, context):
        relabel()   # open the dialog without the sidebar having drawn and these are still current
        scene = context.scene
        layout = self.layout
        layout.prop(scene, "diva_audio_channels")
        layout.prop(scene, "diva_audio_quality")
        layout.prop(scene, "diva_audio_normalize")
        layout.prop(scene, "diva_audio_overwrite")

    def task_name(self):
        return L("task_audio")

    def make_operation(self, context):
        from . import task_ops
        relabel()
        scene = context.scene
        src = scene_path(scene, "diva_audio_src")
        if not os.path.isfile(src):
            raise RuntimeError(L("need_file") % L("audio_src"))
        out = bpy.path.abspath(self.filepath)
        if not out.lower().endswith(".ogg"):
            out += ".ogg"
        if os.path.exists(out) and not scene.diva_audio_overwrite:
            raise RuntimeError(L("exists") % out)
        return task_ops.AudioExportOperation(
            context, options={"src": src, "filepath": out,
                              "channels": int(scene.diva_audio_channels),
                              # it is a FloatProperty now, so `or 6` would turn a deliberate 0.0 into 6 -
                              # the knob is ranged, so the value is taken as given
                              "quality": float(scene.diva_audio_quality),
                              "normalize": scene.diva_audio_normalize or None,
                              # the probed binary, manual path included: the encode uses exactly
                              # what the panel's warning logic found, no second opinion
                              "ffmpeg": ffmpeg_probe() or None,
                              "overwrite": scene.diva_audio_overwrite})

    def task_finish(self, context, task, operation):
        from . import task_ops
        task_ops.log_benchmark(operation)
        probe = operation.probe_info or {}
        self.report({'INFO'}, L("ogg_done") % (os.path.basename(operation.filepath),
                                               os.path.getsize(operation.filepath),
                                               probe.get("sample_rate"), probe.get("channels")))
        self.report({'INFO'}, L("encoder") % (os.path.basename(
            str((operation.info or {}).get("ffmpeg") or "")) or "segmenter"))

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            self.report({'WARNING'}, L("cancelled_fmt")
                                   % (L(task.stage), task.elapsed, L("tail_cancel_audio")))
        else:
            self.report({'ERROR'}, L("fail") % (task.error or reason,))


class DIVA_PV_OT_face(tb.TaskOperator, ChooserDefaults, ExportHelper):

    """Pick the PV script to write: the panel's vmd mouth morphs as MOUTH_ANIM cues, plus the blink.

    The work is `task_ops.FaceExportOperation`.  The decision layer is not touched: the operation
    calls the same `morph_core.match` and `face_core.export_face` every other path uses, with the
    same arguments.
    """

    source_prop = "diva_face_vmd"
    fallback_stem = "pv_script"
    out_suffix = ".face"

    bl_idname = "diva_pv.face_export"
    bl_label = "Export expressions (.dsc)"
    bl_description = ("Choose the .dsc to write: the dance .vmd's mouth morphs as MOUTH_ANIM cues on "
                      "the motion's own clock, with the eyelids driven either by the game's own "
                      "automatic blink or by the dance's まばたき curve. The scaffold the splice "
                      "reads from is generated beside the output and removed on publish, so one "
                      ".dsc lands where the dialog points")
    bl_options = {'PRESET', 'BLOCKING'}
    filename_ext = ".dsc"

    file_chooser = True     # invoke opens the browser; execute runs the export
    filter_glob: StringProperty(default="*.dsc", options={'HIDDEN'})

    def draw(self, context):
        relabel()   # open the dialog without the sidebar having drawn and these are still current
        scene = context.scene
        layout = self.layout
        layout.prop(scene, "diva_face_code")
        layout.prop(scene, "diva_face_chara")
        layout.prop(scene, "diva_face_audit")
        layout.prop(scene, "diva_morph_alias")
        layout.prop(scene, "diva_face_overwrite")
        # No base-script row: the exporter generates the scaffold, so the face stream stands on
        # the motion's own clock with no borrowed timeline to contradict it.  No transplant row
        # either: the LOOK_ANIM 12/13 encoding it wrote is not in the shipping data and was the
        # confirmed cause of a stuck gaze, so the exporter refuses it outright.
        # No dance-length row: the cues are placed on the base script's own timeline, whose length the
        # script already states (export_face derives it from its last TIME).  The .vmd is 30 fps and
        # the export is 60 fps for both motion and camera, so a length typed here could not be honest.
        # No rom-root row either: find_rom_root() reads it off the script you picked, and the
        # report line says which root was used.

    def task_name(self):
        return L("task_face")

    def make_operation(self, context):
        from . import morph_core, task_ops
        relabel()
        scene = context.scene
        from_motion = bool(scene.diva_face_from_motion)
        src = scene_path(scene, "diva_vmd_motion" if from_motion else "diva_face_vmd")
        if not os.path.isfile(src):
            raise RuntimeError(L("need_import_first") if from_motion
                               else L("need_file") % L("face_vmd"))
        # No base script: the exporter generates a minimal shipping-shaped scaffold beside the
        # output, so the face stream stands on the motion's own clock with no borrowed camera,
        # stage or lyric timeline to contradict it.  The rom root comes from the panel field or
        # the packaged tables, as the report states.
        out = bpy.path.abspath(self.filepath)
        if not out.lower().endswith(".dsc"):
            out += ".dsc"
        if os.path.exists(out) and not scene.diva_face_overwrite:
            raise RuntimeError(L("exists") % out)
        # The morph audit is published next to the script, always: it is the record of what was
        # measured, what was decided and what could not be transferred, and a report nobody can
        # find is a report nobody reads.  `diva_face_audit` turns it off for a caller who does not
        # want the file; the counters are printed either way.
        audit_path = None
        if getattr(scene, "diva_face_audit", True):
            audit_path = os.path.splitext(out)[0] + ".morph_audit.json"
        return task_ops.FaceExportOperation(
            context, options={
                "src": src, "base": "", "filepath": out,
                "code": (scene.diva_face_code or morph_core.CHARA).upper()[:3],
                "alias": bpy.path.abspath(scene_path(scene, "diva_morph_alias")) or None,
                "rom_root": find_rom_root(None, scene_path(scene, "diva_morph_rom_root")),
                "chara": scene.diva_face_chara,
                "replace": True,
                "overwrite": scene.diva_face_overwrite,
                "audit_path": audit_path})

    def task_finish(self, context, task, operation):
        from . import face_core, morph_core, task_ops
        task_ops.log_benchmark(operation)
        stats, result = operation.stats, operation.result
        self.report({'INFO'}, L("face_root") % (operation.rom_root or morph_core.which_slot_source()))
        if stats.get("script_format_note"):
            # a note, not a refusal (PLUS-era official scripts pair with an older pv_db
            # declaration all the time) - but the user deserves to see it once
            self.report({'WARNING'}, L("face_format_note") % stats["script_format_note"])
        for line in morph_core.report(result).splitlines():
            print("Face export: %s" % line)
        print(face_core.face_report(stats))
        # The morph audit's counters, so the console always states the coverage even when the JSON
        # file is turned off.  `silent drops 0` is a checked claim, not a slogan: `MorphAudit.verify`
        # refuses a report that would omit or double-count a morph the motion animates.
        audit = operation.audit or {}
        if audit.get("error"):
            self.report({'WARNING'}, "morph audit failed: %s" % audit["error"])
        else:
            cov = audit.get("coverage") or {}
            print("Face export: morph audit %d source morph(s) / %d keyframe(s); mapped %d "
                  "(exact %d, near-exact %d, semantic %d, geometric %d), approximate %d, "
                  "unsupported %d, no source definition %d, invalid %d; lane %d; "
                  "SILENT DROPS %d"
                  % (cov.get("total_source_morphs", 0), cov.get("total_keyframes", 0),
                     cov.get("mapped_morphs", 0), cov.get("exact_mapped", 0),
                     cov.get("near_exact_mapped", 0), cov.get("semantic_mapped", 0),
                     cov.get("geometric_mapped", 0), cov.get("approximate_mapped", 0),
                     cov.get("unsupported_target", 0), cov.get("missing_pmx_definition", 0),
                     cov.get("invalid_source", 0), cov.get("lane_morphs", 0),
                     cov.get("silent_drops", 0)))
            if not (audit.get("sources", {}).get("model") or {}).get("path"):
                self.report({'INFO'},
                            "morph audit: no source PMX, so geometry equivalence is NOT VERIFIED - "
                            "pick the dance's own model to enable effect measurement")
        unresolved = len(result["ambiguous"]) + len(result["unmatched"])
        wl = None
        if unresolved:
            # the worklist is a by-product of this export, not a second output to fill in first
            wl = os.path.splitext(operation.filepath)[0] + ".worklist.txt"
            morph_core.write_worklist(result, wl)
        self.report({'INFO'}, L("face_done") % (os.path.basename(stats["out"]), stats["mouth"],
                                               stats["expression"], stats["records"],
                                               stats["frames"],
                                               stats["replaced_base_face"] or "{}"))
        self.report({'INFO'}, L("face_blink_done")
                    % ("%d cue(s)" % stats.get("blink_cues", 0),))
        info = stats.get("expression_info")
        if info:
            self.report({'INFO'}, "expression rules: %s -> %d cue(s), faces %s"
                        % (os.path.basename(info["rules_file"]), info["emitted"],
                           info["faces_used"]))
            if info["missing_morphs"]:
                self.report({'INFO'},
                            "expression rules: this .vmd has none of %s"
                            % ", ".join(info["missing_morphs"]))
        self.report({'INFO'}, L("morph_done") % (len(result["matched"]), len(result["ambiguous"]),
                                                len(result["unmatched"]),
                                                os.path.basename(wl) if wl else "-"))
        named = lambda s: s.get("name") if hasattr(s, "get") else str(s)
        for row in result["ambiguous"][:5]:
            self.report({'WARNING'}, L("morph_ambiguous") % (row["name"],
                                                             ", ".join(map(named,
                                                                           row["suggestions"]))))

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            self.report({'WARNING'}, L("cancelled_fmt")
                                   % (L(task.stage), task.elapsed, L("tail_cancel_face")))
        else:
            traceback.print_exc()
            self.report({'ERROR'}, L("fail") % (task.error or reason,))


def refresh_props():
    """Re-register every Scene option with this language's label and hover text.

    The instructions live on hover, not as lines of text on the panel.  For a `layout.prop()` row
    Blender's hover text is the property's own `description`, which is baked when the property is
    registered - so the definitions have to be rebuilt.  Measured on Blender 4.5: the RNA
    `description` is read-only, but deleting the property and re-adding it built from the same
    definition keeps every value the user typed (String with FILE_PATH/DIR_PATH subtype, Bool, Int,
    Float and Enum all survive; the enum items come back too).  Rebuilding is therefore safe, and
    that was tested rather than assumed: a blanked path would let one song's camera overwrite
    another song's accepted file.  Operator properties cannot be refreshed the same way, which is
    why the import and motion-export options are Scene properties rather than operator ones.
    """
    for name, prop in SCENE_PROPS:
        keywords = dict(prop.keywords)
        keywords.pop("attr", None)          # setattr injects it into the same dict; the factory rejects it
        hint = PROP_HINTS.get(name)
        if hint:
            keywords["name"] = L(hint[0])
            keywords["description"] = L(hint[1])
        if name == "diva_audio_channels":
            keywords["items"] = ((("2", L("ch2"), L("ch2_tip")), ("4", L("ch4"), L("ch4_tip"))))
        delattr(bpy.types.Scene, name)
        setattr(bpy.types.Scene, name, prop.function(**keywords))


def relabel():
    """Re-apply labels and tooltips for the current language; Blender bakes them at registration.

    A `bl_label` assignment alone changes the Python attribute but **not** the string Blender
    copied into the class's registration data when `register_class()` ran - the header a user
    sees is that copied string.  So every class whose title actually changed is also queued for
    re-registration (`request_retitle`), which is what makes the new words appear.  In a session
    with an event loop the re-registration waits for the next timer tick, because a class cannot
    be unregistered while its own panel is mid-draw; headless runs flush inline.
    """
    want = language()
    if STATE["language"] == want:
        return
    STATE["language"] = want
    for cls, label_key, tip_key in ((DIVA_PV_OT_import_vmd, "import_vmd", "import_vmd_tip"),
                                     (DIVA_PV_OT_camera, "camera_export", "camera_export_tip"),
                                     (DIVA_PV_OT_audio, "audio_export", "audio_export_tip"),
                                     (DIVA_PV_OT_face, "morph", "morph_tip"),
                                     (DIVA_PV_OT_fetch_ffmpeg, "fetch_ffmpeg", "fetch_ffmpeg_tip"),
                                     (DIVA_PV_OT_cancel_task, "card_cancel", "cancel_tip"),
                                     (DIVA_PV_OT_dismiss_task, "card_dismiss", "dismiss_tip")):
        if cls.bl_label != L(label_key) or cls.bl_description != L(tip_key):
            cls.bl_label = L(label_key)
            cls.bl_description = L(tip_key)
            request_retitle(cls)
    # Panel headers are baked the same way.  The sidebar tab itself (bl_category) stays "DIVA PV" in
    # every language: it is the one string the user hunts for to find this panel.
    for cls, label_key in ((DIVA_PV_PT_motion, "motion"), (DIVA_PV_PT_face, "face_panel"),
                           (DIVA_PV_PT_camera, "camera"),
                           (DIVA_PV_PT_music, "music"), (DIVA_PV_PT_task, "task")):
        if cls.bl_label != L(label_key):
            cls.bl_label = L(label_key)
            request_retitle(cls)
    refresh_props()
    for relang in _RELANGERS:      # the modules whose classes live outside this file
        relang()
    if RETITLED:
        if bpy.app.background:
            flush_retitles()
        elif not bpy.app.timers.is_registered(_apply_retitles):
            bpy.app.timers.register(_apply_retitles, first_interval=0.05)


# Classes whose baked title changed and which must therefore be re-registered, and the flush
# that does it.  A parent panel is registered before its children and unregistered after them,
# so the two operations sort a fixed order and walk it in opposite directions.
RETITLED = []
FLUSH_ROUNDS = 0          # one per language switch that actually re-registered anything


def request_retitle(cls):
    if cls not in RETITLED:
        RETITLED.append(cls)


def _title_order(classes):
    out, rest = [], list(classes)
    while rest:
        progressed = False
        for cls in list(rest):
            parent = getattr(cls, "bl_parent_id", None)
            if parent and any(c.bl_idname == parent for c in rest if c is not cls):
                continue                       # its parent is queued too: parent first
            out.append(cls)
            rest.remove(cls)
            progressed = True
        if not progressed:                     # a parent outside this round, or a cycle: stop waiting
            out.extend(rest)
            break
    return out


def flush_retitles():
    """Re-register every queued class so the baked headers show the current language."""
    global FLUSH_ROUNDS
    classes = [cls for cls in RETITLED if getattr(cls, "is_registered", False)]
    RETITLED.clear()
    FLUSH_ROUNDS += 1
    try:
        if bpy.app.timers.is_registered(_apply_retitles):
            bpy.app.timers.unregister(_apply_retitles)
    except (ValueError, RuntimeError):
        pass
    order = _title_order(classes)
    for cls in reversed(order):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:                  # noqa: BLE001 - a failed teardown still allows retry
            print("[DIVA] retitle unregister failed for %s: %r" % (cls.__name__, exc))
    for cls in order:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:                  # noqa: BLE001 - the panel keeps its old title
            print("[DIVA] retitle register failed for %s: %r" % (cls.__name__, exc))
    wm = getattr(bpy.context, "window_manager", None)
    for window in (wm.windows if wm else []):
        screen = getattr(window, "screen", None)
        for area in (screen.areas if screen else []):
            area.tag_redraw()
    return bool(order)


def _apply_retitles():
    flush_retitles()
    return None                                   # one-shot: returning None unregisters it


# Late-registered relang: modules that own registered classes but not the language table call this
# at register time so `relabel()` can retitle their classes when the language changes.  It keeps
# the dependency pointing one way (they import ui, ui never imports them back).
_RELANGERS = []


def register_relanger(fn):
    if fn not in _RELANGERS:
        _RELANGERS.append(fn)


class DIVA_PV_PT_motion(bpy.types.Panel):
    bl_label = "Motion"
    bl_idname = "DIVA_PV_PT_motion"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB

    def draw(self, context):
        relabel()
        scene = context.scene
        layout = self.layout
        # no path field: the Import motion button opens the chooser itself and remembers what it was given
        col = layout.column(align=True)
        col.operator("diva_pv.import_vmd", text=L("import_vmd"), icon='IMPORT')
        col.operator("export_scene.diva_pv_motion_set", text=L("export_mot"), icon='EXPORT')


class DIVA_PV_PT_face(bpy.types.Panel):
    """Expressions: its own panel, because its inputs are a different pair of files.

    The .vmd it reads and the PV script it writes have nothing to do with the mot set above, and the
    morphs can come from a dance that is not loaded on the rig - rows shared with Motion would hide
    both facts.
    """

    bl_label = "Expressions"
    bl_idname = "DIVA_PV_PT_face"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB

    def draw(self, context):
        relabel()
        scene = context.scene
        layout = self.layout
        col = layout.column(align=True)
        col.prop(scene, "diva_face_from_motion", text=L("face_from_motion"))
        if not scene.diva_face_from_motion:
            col.prop(scene, "diva_face_vmd", text=L("face_vmd"))
        col.operator("diva_pv.face_export", text=L("morph"), icon='EXPORT')


class DIVA_PV_PT_camera(bpy.types.Panel):
    bl_label = "Camera"
    bl_idname = "DIVA_PV_PT_camera"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB

    def draw(self, context):
        relabel()
        scene = context.scene
        layout = self.layout
        col = layout.column(align=True)
        col.prop(scene, "diva_camera_vmd", text=L("camera_vmd"))
        col.operator("diva_pv.camera_export", text=L("camera_export"), icon='EXPORT')


class DIVA_PV_PT_music(bpy.types.Panel):
    bl_label = "Music"
    bl_idname = "DIVA_PV_PT_music"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB

    def draw(self, context):
        relabel()
        scene = context.scene
        layout = self.layout
        col = layout.column(align=True)
        col.prop(scene, "diva_audio_src", text=L("audio_src"))
        col.prop(scene, "diva_ffmpeg_path", text=L("ffmpeg_path"))
        col.operator("diva_pv.audio_export", text=L("audio_export"), icon='EXPORT')
        # the export below only works with a libvorbis ffmpeg; when none was probed, say so here,
        # where the user is about to click it, and offer the one-click fetch instead of an error.
        # An unknown icon name raises *inside draw* and takes the rest of the panel out -
        # 'DOWNLOAD' is not a Blender icon - so every icon used here exists (smoke-checked).
        if not ffmpeg_probe():
            warn = layout.column(align=True)
            warn.label(text=L("ffmpeg_missing"), icon='ERROR')
            warn.label(text=L("ffmpeg_missing_how"))
            warn.operator("diva_pv.fetch_ffmpeg", text=L("fetch_ffmpeg"), icon='URL')


SCENE_PROPS = (
    ("diva_vmd_motion", StringProperty(name="Motion .vmd", subtype='FILE_PATH')),
    ("diva_camera_vmd", StringProperty(name="Camera .vmd", subtype='FILE_PATH')),
    ("diva_camera_min_y", FloatProperty(name="Min camera Y", default=0.0, min=0.0, soft_max=3.0)),
    ("diva_camera_min_y_on", BoolProperty(name="Clamp to a minimum height", default=False)),
    # lifts or lowers the whole shot (viewpoint and target together) so an export can be
    # aligned with the game floor without touching the framing
    ("diva_cam_height", FloatProperty(name="Camera height offset", default=0.0,
                                      min=-5.0, max=10.0, soft_min=-2.0, soft_max=5.0)),
    ("diva_camera_overwrite", BoolProperty(name="Overwrite existing", default=False)),
    ("diva_audio_src", StringProperty(name="Source audio", subtype='FILE_PATH')),
    ("diva_audio_channels", EnumProperty(name="Channels", default='2',
                                         items=(('2', "2ch stereo", "Smaller, plays everywhere"),
                                                ('4', "4ch quad", "What the shipped songs are")))),
    ("diva_audio_quality", FloatProperty(name="Quality (-q)", default=6.0, min=-0.2, max=10.0,
                                         precision=1, step=10,
                                         description="libvorbis VBR quality, -0.2 to 10")),
    ("diva_audio_normalize", FloatProperty(name="Normalise peak", default=0.95, min=0.0, max=1.0)),
    ("diva_audio_overwrite", BoolProperty(name="Overwrite existing", default=False)),
    # the manual ffmpeg pick: a browse button next to the warning line for users whose
    # ffmpeg is neither on PATH nor fetched - and the escape hatch when a download fails
    ("diva_ffmpeg_path", StringProperty(name="FFmpeg (optional)", subtype='FILE_PATH',
                                        default="")),
    # where the morphs come from is a decision the user makes per export, so it is a Scene property:
    # an operator property's hover text is baked at registration and cannot follow the panel language
    ("diva_face_from_motion", BoolProperty(name="From the imported motion", default=True)),
    ("diva_face_vmd", StringProperty(name="Dance .vmd to read", subtype='FILE_PATH')),
    ("diva_face_code", StringProperty(name="Slot table of", default="MIK")),
    ("diva_face_chara", IntProperty(name="Character slot", default=0, min=0)),
    ("diva_face_overwrite", BoolProperty(name="Overwrite existing", default=False)),
    # the import dialog's "Clear the rig first" and the export dialog's Static Precision / Scale Keys
    # are Scene properties, not operator ones.  An operator property's hover text is baked when the
    # class is registered and cannot be re-translated (measured on Blender 4.5), so they live here,
    # where the language switch can reach both the label and the tooltip.
    ("diva_import_clear", BoolProperty(name="Clear the rig first", default=False)),
    ("diva_mot_decimals", IntProperty(name="Static Precision", default=4, min=0, max=8)),
    ("diva_mot_scale_keys", FloatProperty(name="Scale Keys", default=1.0, min=0.01)),
    ("diva_mot_overwrite", BoolProperty(name="Overwrite existing", default=False)),
    ("diva_morph_alias", StringProperty(name="Alias answers", subtype='FILE_PATH')),
    # The dance's own PMX: the export no longer asks for it.  The geometry-measurement lane it fed
    # is only a reporting nicety; the mapping that ships is name- and table-based either way, and
    # the field was one more input a plain face export did not need.  morph_pipeline's sidecar
    # search still runs for the audit's own report.
    # Export motion writes an exp_PV*.bin beside the mot set: the game's own eye-animation
    # carrier, which every eye-bearing PV ships and no LOOK_ANIM-using PV does.
    ("diva_face_audit", BoolProperty(name="Write the morph audit (.json)", default=True)),
    # not drawn by any panel: leave it empty and the rom root is found from the script you
    # picked (see find_rom_root); the property stays so an unusual install can still be named
    ("diva_morph_rom_root", StringProperty(name="DIVA rom root", subtype='DIR_PATH')),
)

# identifier -> (label key, hover-text key).  Every option a dialog draws gets one, because the
# hover text is where the instructions live now instead of as lines of text on the panel.
PROP_HINTS = {
    "diva_vmd_motion": ("motion_vmd", "motion_vmd_tip"),
    "diva_camera_vmd": ("camera_vmd", "camera_vmd_tip"),
    "diva_camera_min_y": ("camera_min_y", "camera_min_y_tip"),
    "diva_camera_min_y_on": ("camera_min_y_on", "camera_min_y_on_tip"),
    "diva_cam_height": ("camera_height", "camera_height_tip"),
    "diva_camera_overwrite": ("camera_overwrite", "overwrite_tip"),
    "diva_audio_src": ("audio_src", "audio_src_tip"),
    "diva_audio_channels": ("audio_channels", "audio_channels_tip"),
    "diva_audio_quality": ("audio_quality", "audio_quality_tip"),
    "diva_audio_normalize": ("audio_normalize", "audio_normalize_tip"),
    "diva_audio_overwrite": ("audio_overwrite", "overwrite_tip"),
    "diva_ffmpeg_path": ("ffmpeg_path", "ffmpeg_path_tip"),
    "diva_face_from_motion": ("face_from_motion", "face_from_motion_tip"),
    "diva_face_vmd": ("face_vmd", "face_vmd_tip"),
    "diva_face_code": ("face_code", "face_code_tip"),
    "diva_face_chara": ("face_chara", "face_chara_tip"),
    "diva_face_overwrite": ("camera_overwrite", "overwrite_tip"),
    "diva_import_clear": ("clear_first", "clear_first_tip"),
    "diva_mot_decimals": ("decimals", "decimals_tip"),
    "diva_mot_scale_keys": ("scale_keys", "scale_keys_tip"),
    "diva_mot_overwrite": ("mot_overwrite", "overwrite_tip"),
    "diva_morph_alias": ("alias", "alias_tip"),
    "diva_face_audit": ("face_audit", "face_audit_tip"),
}

def probe_dt(scene):
    """Seconds per frame for the rig's own playback rate."""
    fps = scene.render.fps / max(1e-9, scene.render.fps_base)
    return 1.0 / fps


def _deepest_frame():
    """file:line in the deepest frame of the current exception, for the report line."""
    import traceback as _tb
    frames = _tb.extract_tb(sys.exc_info()[2])
    if not frames:
        return "unknown location"
    last = frames[-1]
    return "%s:%d in %s" % (os.path.basename(last.filename), last.lineno, last.name)


class DIVA_PV_OT_cancel_task(bpy.types.Operator):
    """Stop the running task at its next safe point."""

    bl_idname = "diva_pv.cancel_task"
    bl_label = "Cancel"
    bl_description = ("Ask the running task to stop. It stops at the next safe point rather than "
                      "inside a Blender write, so the file is never left half-changed.")

    @classmethod
    def poll(cls, context):
        # `current()`, not the card list: a finished card stays on screen to be read, and cancelling
        # one is meaningless.  The poll is what keeps the button off those cards.
        return tc.MANAGER.current() is not None

    def execute(self, context):
        task = tc.MANAGER.current()
        if task is None:
            self.report({'INFO'}, L("cancel_none"))
            return {'CANCELLED'}
        task.cancel()
        self.report({'INFO'}, L("cancel_started") % task.name)
        tb.redraw(context)
        return {'FINISHED'}


class DIVA_PV_OT_dismiss_task(bpy.types.Operator):
    """Remove a finished task's card, or all of them.

    The card carries its own id, because `UILayout.operator()` cannot pass an argument - a declared
    property is the only channel a panel has.  Without it the button could only mean "remove the
    first", which is wrong the moment there is more than one card.

    A running task is refused rather than removed: its card is where the progress and the cancel
    button live, so dismissing it would hide a run that is still going.  Cancel it first.
    """

    bl_idname = "diva_pv.dismiss_task"
    bl_label = "Dismiss"
    bl_description = ("Remove this finished task's card. The work it reported is already done and "
                      "committed; this only clears the report.")

    task_id: StringProperty(name="Task", default="", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        # offered only when there is a finished card to remove; the running one has no Dismiss
        return any(t.status in tc.TERMINAL for t in tc.MANAGER.visible())

    def execute(self, context):
        if self.task_id:
            task = tc.MANAGER.tasks.get(self.task_id)
            if task is not None and task.status not in tc.TERMINAL:
                self.report({'WARNING'}, L("still_running") % task.name)
                return {'CANCELLED'}
        gone = tc.MANAGER.dismiss(self.task_id)
        if not gone:
            self.report({'INFO'}, L("dismiss_none"))
            return {'CANCELLED'}
        self.report({'INFO'}, L("dismissed") % gone)
        tb.redraw(context)
        return {'FINISHED'}


class DIVA_PV_OT_fetch_ffmpeg(tb.TaskOperator):
    """Download the pinned portable ffmpeg into this add-on's `bin/`.

    The task machinery does the rest, exactly as it does for an export: the transfer runs on a
    worker thread, the card shows MB done of MB total, and a Cancel deletes the partial download
    instead of leaving it in the temp folder.  Nothing is ever executed from the download except
    after its sha256 has been checked against the pin in `ffmpeg_fetch`.
    """

    bl_idname = "diva_pv.fetch_ffmpeg"
    bl_label = "Download FFmpeg"
    bl_description = ("Download the pinned official Windows build (about 104 MB) into the "
                      "add-on folder. An ffmpeg on PATH or $MMD2DIVA_FFMPEG is still preferred")

    def task_name(self):
        return L("fetch_ffmpeg")

    def make_operation(self, context):
        from . import task_ops
        return task_ops.FFmpegFetchOperation(context)

    def task_finish(self, context, task, operation):
        ffmpeg_probe(rescan=True)          # the fetch changed what the probe can find
        self.report({'INFO'}, L("fetch_done"))
        tb.redraw(context)

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            self.report({'WARNING'}, L("fetch_cancelled"))
        else:
            self.report({'ERROR'}, L("fail") % (task.error or reason,))


OPERATORS = (DIVA_PV_OT_cancel_task, DIVA_PV_OT_dismiss_task, DIVA_PV_OT_import_vmd,
             DIVA_PV_OT_camera, DIVA_PV_OT_audio, DIVA_PV_OT_face, DIVA_PV_OT_fetch_ffmpeg)
class DIVA_PV_PT_task(bpy.types.Panel):
    """The task's own panel, so progress is visible in the 3D viewport sidebar."""

    bl_label = "Task"
    bl_idname = "DIVA_PV_PT_task"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = TAB
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        relabel()
        if not tb.progress_draw(self.layout, context, cancel_op="diva_pv.cancel_task",
                                dismiss_op="diva_pv.dismiss_task", texts=card_texts()):
            self.layout.label(text=L("idle"))


def _stop_timers():
    """Called from unregister: a timer must not outlive the module it calls into."""
    try:
        tb.stop_all_timers()
    except Exception:                       # noqa: BLE001 - unregister must not raise
        pass


PANELS = (DIVA_PV_PT_task, DIVA_PV_PT_motion, DIVA_PV_PT_face,
          DIVA_PV_PT_camera, DIVA_PV_PT_music)


def register_ui():
    for name, prop in SCENE_PROPS:
        setattr(bpy.types.Scene, name, prop)
    for cls in OPERATORS + PANELS:
        bpy.utils.register_class(cls)
    STATE["language"] = None
    relabel()


def unregister_ui():
    RETITLED.clear()
    try:
        if bpy.app.timers.is_registered(_apply_retitles):
            bpy.app.timers.unregister(_apply_retitles)
    except (ValueError, RuntimeError):
        pass
    for cls in reversed(OPERATORS + PANELS):
        bpy.utils.unregister_class(cls)
    for name, _prop in SCENE_PROPS:
        delattr(bpy.types.Scene, name)
