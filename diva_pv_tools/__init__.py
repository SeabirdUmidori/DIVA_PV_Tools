"""Convert MMD work into Hatsune Miku Project DIVA MEGA39's+ mod files.

This add-on turns MMD material into what the game's mod loader reads:

  * a VMD body motion becomes a mot set (``.bin``) through ``diva_pv.import_vmd`` and the
    ``File > Export > Project DIVA Mot Set`` operator;
  * a VMD camera becomes a ``CAMPV<pv>_BASE.a3da`` for the DIVA camera model;
  * any audio file becomes the 44.1 kHz Ogg/Vorbis song the game expects, using an ffmpeg
    built with libvorbis (``MMD2DIVA_FFMPEG`` > PATH > the fetched ``bin/`` - the Music
    panel offers a one-click download when none is found);
  * the VMD's mouth and expression morphs become PV-script face cues (``.dsc``), solved
    against a knowledge base measured from shipping performances.

Every long operation runs as a cancellable, time-sliced task: the sidebar shows a progress
card per run, the UI stays responsive, and Cancel leaves existing files untouched.
Motion *editing* (joint limits, foot contact, naturalness) lives in the companion add-on
``motion_refinery``; the two are independent and either can be enabled alone.

Dependencies: Blender 4.0+, mmd_tools (for VMD import), and the template's
``Import Rig`` / ``Export Rig`` as the motion pipeline's target.  Expression slot names ship
inside this package, so an extracted rom is optional.

Tested target: Hatsune Miku Project DIVA MEGA39's+ (PC) only; other DIVA titles and
platforms share the formats but have not been verified.

Fork of BlenderDivaTools (by ThisIsHH, FlyingSpirits), maintained by SeabirdUmidori.
"""

# ---------------------------------------------------------------------------- build identity
# Which copy is Blender actually running?  A stale install looks exactly like a fresh one from the
# outside.  The id is computed from the sources that ship in the package, so two different builds
# cannot share one, and the path is where the running interpreter really imported from.
def _build_id():
    import hashlib
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    digest = hashlib.sha256()
    for name in sorted(f for f in _os.listdir(here) if f.endswith(".py")):
        try:
            with open(_os.path.join(here, name), "rb") as fh:
                digest.update(name.encode("utf-8"))
                digest.update(fh.read())
        except OSError:
            pass
    return digest.hexdigest()[:12]


import os as _os_mod

BUILD_ID = _build_id()
SOURCE_PATH = _os_mod.path.dirname(_os_mod.path.abspath(__file__))


def build_banner():
    """The three identity lines to ask for when a user reports a problem."""
    return ("%s\n  version = %s\n  build   = %s\n  source  = %s"
            % (bl_info["name"], ".".join(str(v) for v in bl_info["version"]),
               BUILD_ID, SOURCE_PATH))


bl_info = {
    "name": "DIVA PV Tools (MMD to Project DIVA)",
    "author": "ThisIsHH, FlyingSpirits; fork maintained by SeabirdUmidori",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View 3D > Sidebar > DIVA PV, and File > Export > Project DIVA Mot Set (.bin)",
    "description": "Convert MMD dances, cameras, songs and facial animation into Project DIVA "
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
