"""Convert MMD work into Hatsune Miku Project DIVA MEGA39's+ mod files.

This add-on turns MMD material into what the game's mod loader reads:

  * a VMD body motion becomes a mot set (``.bin``) through ``diva_pv.import_vmd`` and the
    ``File > Export > Project DIVA Mot Set`` operator;
  * a VMD camera becomes a ``CAMPV<pv>_BASE.a3da`` for the DIVA camera model;
  * any audio file becomes the 44.1 kHz Ogg/Vorbis song the game expects, using an ffmpeg
    built with libvorbis (``MMD2DIVA_FFMPEG`` > PATH > the fetched ``bin/`` - the Music
    panel offers a one-click download when none is found);
  * the VMD's mouth morphs become the PV script's ``MOUTH_ANIM`` lip sync, and the eyelids are
    driven either by the game's own automatic blink or by the dance's ``まばたき`` curve
    (``.dsc``), solved against a knowledge base measured from shipping performances;
  * the dance's expressions become ``EXPRESSION`` cues too, when asked for: each DIVA face is a
    blend of MMD morphs read off the character in game, and the blends live in
    ``data/expression_rules.json``.

Every long operation runs as a cancellable, time-sliced task: the sidebar shows a progress
card per run, the UI stays responsive, and Cancel leaves existing files untouched.
Motion *editing* (joint limits, foot contact, naturalness) lives in the companion add-on
``motion_refinery``; the two are independent and either can be enabled alone.

Dependencies: Blender 4.0+, mmd_tools (for VMD import), and the template's
``Import Rig`` / ``Export Rig`` as the motion pipeline's target.  Expression slot names ship
inside this package, so an extracted rom is optional.

Tested target: Hatsune Miku Project DIVA MEGA39's+ (PC) only; other DIVA titles and
platforms share the formats but have not been verified.

Fork of BlenderDivaTools (by ThisIsHH, FlyingSpirits), maintained as **diva_pv_tools** by
SeabirdUmidori (fork).
"""

# ---------------------------------------------------------------------------- build identity
# Which copy is Blender actually running?  A stale install looks exactly like a fresh one from the
# outside.  The id is computed from the sources that ship in the package, so two different builds
# cannot share one, and the path is where the running interpreter really imported from.
#
# The data files are hashed too, and not only the .py files: `data/mmd_morph_catalog.json` decides
# which semantic category a morph name gets and which names the eyelid lane claims, so a build that
# changed only that file would behave differently while reporting the same id - which is exactly the
# confusion the id exists to prevent.
def _build_id():
    import hashlib
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    digest = hashlib.sha256()
    for root, dirs, files in _os.walk(here):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel_root = _os.path.relpath(root, here)
        for name in sorted(files):
            if not name.endswith((".py", ".json")):
                continue
            rel = name if rel_root == "." else _os.path.join(rel_root, name)
            try:
                with open(_os.path.join(root, name), "rb") as fh:
                    digest.update(rel.replace("\\", "/").encode("utf-8"))
                    digest.update(fh.read())
            except OSError:
                pass
    return digest.hexdigest()[:12]


import os as _os_mod

BUILD_ID = _build_id()


def stale_build():
    """`(loaded_build_id, build_id_on_disk, on_disk_path)`.

    Blender imports an add-on's modules once per session and never re-imports them when the files
    change, so a session that was already running when the add-on was updated keeps the *old*
    `face_core`/`task_ops` in `sys.modules` while a lazily-imported sibling can still come from the
    new files.  That mixture is not a code defect in either build - it is a half-old session - and
    it surfaces as an argument error from a function whose signature did change, which reads like a
    bug in the new code.  Comparing the id captured at import against the one the files carry now is
    what tells the two apart.
    """
    return BUILD_ID, _build_id(), _os_mod.path.dirname(_os_mod.path.abspath(__file__))
SOURCE_PATH = _os_mod.path.dirname(_os_mod.path.abspath(__file__))


def build_banner():
    """The three identity lines to ask for when a user reports a problem."""
    return ("%s\n  version = %s\n  build   = %s\n  source  = %s"
            % (bl_info["name"], ".".join(str(v) for v in bl_info["version"]),
               BUILD_ID, SOURCE_PATH))


bl_info = {
    "name": "DIVA PV Tools (MMD to Project DIVA)",
    "author": "ThisIsHH, FlyingSpirits (upstream), SeabirdUmidori (fork)",
    "version": (2, 0, 0),
    "blender": (4, 0, 0),
    "location": "View 3D > Sidebar > DIVA PV, and File > Export > Project DIVA Mot Set (.bin)",
    "description": "Convert MMD dances, cameras, songs and lip sync into Project DIVA "
                   "MEGA39's+ mod files: mot set, a3da, ogg and PV-script cues",
    "category": "Import-Export",
    "doc_url": "",
    "tracker_url": "",
}

from . import export_operator, handlers, ui


def register():
    print(build_banner())
    export_operator.register()
    ui.register_ui()
    # The pole empties have to follow the source arm while the user scrubs the timeline, and the
    # mot-set export drives the same math itself per sampled frame (pole_utils resolves the
    # template's names).  Blender never runs a frame_change_post handler restored from a .blend,
    # and it drops the one registered here as soon as a file is opened - handlers.py keeps a
    # persistent load_post for that.
    handlers.register_pole_handlers()


def unregister():
    # timers and modal jobs first: a timer that fires after the module is gone
    # raises, and a task left in the manager draws a bar that will never move
    try:
        ui._stop_timers()
    except Exception:
        pass
    handlers.unregister_pole_handlers()
    ui.unregister_ui()
    export_operator.unregister()
