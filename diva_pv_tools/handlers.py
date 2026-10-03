
import bpy

from . import pole_utils


# Kept so a rig that cannot drive its poles says so once instead of once per frame.
_last_problems = []


def frame_change_handler(scene, depsgraph=None):
    """Blender calls frame_change_post with (scene, depsgraph).

    The handler signature must accept both arguments, and Blender does not run handlers restored from
    a .blend at all, so the pole empties silently keep the pose the template was saved in.  The
    exporter drives the same math itself, so playback is a preview, not a dependency.
    """
    problems = pole_utils.drive_scene_poles(scene)
    if problems != _last_problems:
        _last_problems[:] = problems
        for problem in problems:
            print(problem)


def is_handler_registered(handler_name, handlers):
    # Match the module too: a .blend, or another add-on sharing the list, can hold an inert handler of
    # the same name, and "already registered" would then block ours.  The load_post list really does
    # hold another add-on's `load_post_handler`, so names alone collide.
    return any(h.__name__ == handler_name and getattr(h, "__module__", None) == __name__
               for h in handlers)


def _is_ours(handler, name):
    """Whether this list entry claims to be one of this module's handlers."""
    return handler.__name__ == name and getattr(handler, "__module__", None) == __name__


def register_frame_handler():
    # A .blend comes back holding an inert function that has *our* name and *our* module string, and it
    # cannot run; left in the list it would make the check below think we are already registered, which
    # is how a frozen pole survives.  So drop the stale copies that claim our module, keeping
    # this import's own function object - and leave other add-ons' handlers alone, whatever they are
    # called (the load_post list really does hold another add-on's `load_post_handler`).
    bpy.app.handlers.frame_change_post[:] = [
        h for h in bpy.app.handlers.frame_change_post
        if not (_is_ours(h, frame_change_handler.__name__) and h is not frame_change_handler)
    ]
    if not is_handler_registered(frame_change_handler.__name__, bpy.app.handlers.frame_change_post):
        bpy.app.handlers.frame_change_post.append(frame_change_handler)
        print("Frame change handler has been registered and is now active.")
    else:
        print("Frame change handler is already registered and active.")


def unregister_frame_handler():
    bpy.app.handlers.frame_change_post[:] = [
        h for h in bpy.app.handlers.frame_change_post
        if not _is_ours(h, frame_change_handler.__name__)
    ]
    print("Frame change handler has been unregistered.")


@bpy.app.handlers.persistent
def load_post_handler(scene, _depsgraph=None):
    """Put the pole handler back after a file is opened.

    Registering from the add-on's register() is not enough on its own: opening a .blend replaces
    frame_change_post with the file's own list, so the handler is lost on the first file load and the
    poles go back to the frozen pose the template was saved with.  Blender does not re-register a
    frame handler on its own after a file load; this load_post hook is what re-registers it.
    """
    register_frame_handler()


def register_pole_handlers():
    name = load_post_handler.__name__
    bpy.app.handlers.load_post[:] = [
        h for h in bpy.app.handlers.load_post if not (_is_ours(h, name) and h is not load_post_handler)
    ]
    register_frame_handler()
    if not is_handler_registered(name, bpy.app.handlers.load_post):
        bpy.app.handlers.load_post.append(load_post_handler)


def unregister_pole_handlers():
    unregister_frame_handler()
    bpy.app.handlers.load_post[:] = [
        h for h in bpy.app.handlers.load_post if not _is_ours(h, load_post_handler.__name__)
    ]
