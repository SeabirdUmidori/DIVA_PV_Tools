
import os

import bpy
from bpy.props import FloatProperty, StringProperty, BoolProperty, IntProperty
from bpy_extras.io_utils import ExportHelper
from . import exporter, task_blender as tb, task_ops, ui


class DIVA_PV_OT_export_motset(tb.TaskOperator, ExportHelper):
    """Write the Export Rig's pose per frame to a mot set, without holding the UI.

    The work is `task_ops.MotionExportOperation`, driven through the modal task so the export neither
    holds the UI nor loses the ability to cancel.  The profile that decides
    the boundaries is in that operation's docstring; the short version is that 76% of it is
    `scene.frame_set` in a loop over 11,885 frames.

    The output goes to `<name>.part` and is renamed only on success, so a cancel leaves the previous
    file untouched rather than a truncated one the game would try to load.
    """

    bl_idname = "export_scene.diva_pv_motion_set"
    bl_label = "Export DIVA Mot Set (.bin)"
    bl_description = "Write the Export Rig's pose per frame to a Project DIVA mot set (.bin), driving the elbow poles and the spine mapping the game solves the limbs from. The rig is looked up by name, so what is selected does not matter"
    bl_options = {'PRESET', 'BLOCKING'}

    filename_ext = ".bin"

    filter_glob: StringProperty(default="*.bin", options={'HIDDEN'})

    file_chooser = True     # invoke opens the browser; execute runs the export - see TaskOperator

    decimals: IntProperty(
        name="Static Precision",
        description="Override the panel's Static Precision; -1 takes what the panel holds",
        default=-1,
        min=-1,
        max=8
    )
    scale_keys: FloatProperty(
        name="Scale Keys",
        description="Scale factor for keyframes (e.g., 2.0 for doubling frame rate if 30FPS)",
        default=0.0,
        min=0.0,
        max=100.0
    )

    def invoke(self, context, event):
        """Open the save dialog, defaulting the name to the template's own.

        The work is not started from here.  `ExportHelper.invoke` returns `RUNNING_MODAL` while its
        browser is open, and Blender calls **`execute`** on this operator once a path is accepted -
        which `TaskOperator` turns into the same chunked, cancellable run every other long operation
        gets.  That result code cannot tell "still choosing" from "choice made": starting the task
        from `invoke` on it would let the export run with an empty path, write a file, and crash
        Blender in `fileselect_ensure_updated_file_params` because the
        file-select handler had never been installed.
        """
        if not self.filepath:
            self.filepath = "PV0000_DMY_P1_00.bin"
        return ExportHelper.invoke(self, context, event)

    @classmethod
    def poll(cls, context):
        return context.object and context.object.type == 'ARMATURE'

    def draw(self, context):
        # the options drawn below are Scene properties; relabel() keeps their labels and hover text
        # in the panel language even when the dialog is opened without the sidebar having drawn
        ui.relabel()
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        draw_general_panel(layout, self)

    def task_name(self):
        return ui.L("task_export_mot")

    def make_operation(self, context):
        # The rig is looked up by name, and only in a file that has no "Export Rig" does the selection
        # win - so the user does not have to click the right armature first, and the print says which
        # one won.
        armature = bpy.data.objects.get("Export Rig") or context.object
        if armature is not None and armature is not context.object:
            print("Mot set: writing %s rather than the selected %s"
                  % (armature.name, getattr(context.object, "name", "nothing")))
        # Like the camera export and the rest, refuse to replace a file unless the scene says so:
        # without this check the only thing standing between an existing file and being overwritten is
        # the file browser's own "check existing" box - which a script, the F3 menu and a re-run of the
        # same export all bypass.  Same idiom, same message as `diva_pv.camera_export`.
        out = bpy.path.abspath(self.filepath)
        if not out.lower().endswith(".bin"):
            out += ".bin"
        if os.path.exists(out) and not getattr(context.scene, "diva_mot_overwrite", False):
            raise RuntimeError(ui.L("exists") % out)
        # -1 / 0.0 mean the caller did not pass one, which is how the dialog works: it draws the Scene
        # options (translated labels and hover text), so those win unless a script sets the operator
        # property itself.
        decimals = self.decimals if self.decimals >= 0 \
            else int(getattr(context.scene, "diva_mot_decimals", 4))
        scale_keys = self.scale_keys if self.scale_keys > 0 \
            else float(getattr(context.scene, "diva_mot_scale_keys", 1.0))
        return task_ops.MotionExportOperation(
            context, armature=armature,
            options={"filepath": out, "decimals": decimals,
                     "scale_keys": scale_keys})

    def task_finish(self, context, task, operation):
        task_ops.log_benchmark(operation)
        self.report({'INFO'}, ui.L("mot_done") % operation.filepath)
        operation.report = operation.benchmark()

    def task_abort(self, context, task, operation, reason):
        if reason == "cancelled":
            note = (ui.L("mot_cancelled")
                    % (ui.L(task.stage), task.elapsed,
                       os.path.basename(operation.filepath) if operation else "the output"))
            print("[Export] %s" % note)          # the console keeps the technical wording
            self.report({'WARNING'}, note)
        else:
            self.report({'ERROR'}, ui.L("export_failed") % (task.error or reason,))
        if operation is not None and operation.chunks:
            task_ops.log_benchmark(operation, prefix="[Benchmark:aborted]")


def draw_general_panel(layout, operator):
    general_panel, general_body = layout.panel("MOT_Export_General", default_closed=False)
    general_panel.label(text=ui.L("general"))
    if general_body:
        ui.export_options(general_body)


def menu_func_export(self, context):
    # the menu redraws on open, so this text follows the panel language
    self.layout.operator(DIVA_PV_OT_export_motset.bl_idname, text=ui.L("menu_export_mot"))


def refresh_titles():
    """relanger: the F3 name and hover text are baked at registration - re-register to change them."""
    cls = DIVA_PV_OT_export_motset
    if cls.bl_label != ui.L("export_mot_title") or cls.bl_description != ui.L("export_mot_desc"):
        cls.bl_label = ui.L("export_mot_title")
        cls.bl_description = ui.L("export_mot_desc")
        if getattr(cls, "is_registered", False):
            ui.request_retitle(cls)


def register():
    bpy.utils.register_class(DIVA_PV_OT_export_motset)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    ui.register_relanger(refresh_titles)
    refresh_titles()


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(DIVA_PV_OT_export_motset)
