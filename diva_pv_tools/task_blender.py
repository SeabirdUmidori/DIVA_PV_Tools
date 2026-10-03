"""The Blender side of the task system: modal operators, timers, state save and restore.

`task_core` is pure Python and knows nothing about Blender.  This module is the boundary, and it
exists to make three rules easy to follow rather than easy to break.

**Blender data is only touched from the main thread.**  A modal operator's `modal` runs on the main
thread, which is what makes it the right vehicle for work that reads or writes `bpy.data`.  There is
no worker thread in this module at all: the chunking in `task_core` already keeps each tick inside a
few milliseconds, and a thread would buy nothing except the ability to corrupt the data.

**A tick returns.**  `modal` processes for at most the task's budget and then returns `RUNNING_MODAL`.
It never waits for anything, and nothing here calls `sleep`, `join` or `result`.

**State is saved before it is changed and restored on every exit path** - completion, cancellation and
failure alike - so a task that dies cannot leave the file in pose mode with a different active object.
"""
import time

import bpy
from bpy.types import Operator

from . import task_core as tc

# Areas that should repaint while a task runs, so the progress is visible wherever the user is looking.
REDRAW_AREAS = {'VIEW_3D', 'PROPERTIES', 'TIMELINE', 'DOPESHEET_EDITOR', 'NLA_EDITOR',
                'INFO', 'CONSOLE'}

TIMER_INTERVAL = 0.01          # 100 Hz: the timer must return before a budget-sized chunk could end


def redraw(context=None):
    """Ask every visible area to repaint.  Cheap, and the only way the bar moves."""
    context = context or bpy.context
    screen = getattr(context, "screen", None)
    if screen is None:
        window = getattr(context, "window", None)
        screen = getattr(window, "screen", None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type in REDRAW_AREAS:
            try:
                area.tag_redraw()
            except (ReferenceError, RuntimeError):
                pass


class StateGuard(object):
    """Record what a task is about to change, and put it back however the task ends.

    A failed or cancelled task must not leave frame, active object, selection, mode or the area
    altered.  Recording them is cheap; forgetting to restore any one of them is
    the kind of defect a user notices immediately and a test suite does not.
    """

    def __init__(self, context=None):
        context = context or bpy.context
        self.frame = None
        self.mode = None
        self.active = None
        self.selected = []
        self.area_type = None
        scene = getattr(context, "scene", None)
        if scene is not None:
            self.frame = scene.frame_current
        view = getattr(context, "view_layer", None)
        if view is not None:
            self.active = view.objects.active
            self.selected = [o for o in view.objects.selected]
            self.mode = getattr(view.objects.active, "mode", None)
        area = getattr(context, "area", None)
        if area is not None:
            self.area_type = area.type

    def restore(self, context=None):
        context = context or bpy.context
        scene = getattr(context, "scene", None)
        if scene is not None and self.frame is not None:
            try:
                scene.frame_set(self.frame)
            except (ReferenceError, RuntimeError, TypeError):
                pass
        view = getattr(context, "view_layer", None)
        if view is None:
            return
        try:
            for obj in view.objects:
                obj.select_set(False)
            for obj in self.selected:
                if obj.name in view.objects:
                    obj.select_set(True)
            if self.active is not None and self.active.name in view.objects:
                view.objects.active = self.active
                if self.mode and getattr(self.active, "mode", None) != self.mode:
                    try:
                        bpy.ops.object.mode_set(mode=self.mode)
                    except (RuntimeError, TypeError):
                        pass
        except (ReferenceError, RuntimeError):
            pass


class TaskOperator(Operator):
    """Modal operator base: `invoke` creates the task and the timer, `modal` ticks it.

    A subclass implements `make_operation(context)` returning a `task_ops.Operation`, and optionally
    `task_finish(context, task, operation)` and `task_abort(context, task, operation, reason)`.
    Everything else - the timer, the cancel key, the state guard, the cleanup on every non-success
    path, the progress property, the failure report - is handled here, because getting any one of them
    wrong in one operator out of five is exactly how a plugin ends up freezing or leaking datablocks.

    **The synchronous `execute` runs the same object.**  It drives the same `Operation` with the same
    units and the same cleanup; it simply cannot return to the event loop between them.  That is what
    makes the tested path and the interactive path the same path, and it is why the headless tests
    measure the real thing rather than a stand-in.
    """

    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    # ------------------------------------------------------------------ to override
    def task_name(self):
        return self.bl_label or self.__class__.__name__

    def make_operation(self, context):
        raise NotImplementedError("%s must return a task_ops.Operation" % self.__class__.__name__)

    def task_finish(self, context, task, operation):
        """Report the result.  Runs once, after the last stage and before the commit is reported."""

    def task_abort(self, context, task, operation, reason):
        """Report a cancellation or a failure.  The operation has already cleaned up after itself."""

    # ------------------------------------------------------------------ modal lifecycle
    #
    # Two of Blender's conventions meet here and they have to be kept apart, because mixing them up
    # crashes Blender rather than raising.
    #
    #   * The **file chooser** convention (`bpy_extras.io_utils.ImportHelper` / `ExportHelper`):
    #     `invoke` calls `window_manager.fileselect_add(self)` and returns `RUNNING_MODAL` *with the
    #     dialog open*.  Blender runs the browser, and when a file is accepted it calls **`execute`** on
    #     the same operator with `filepath` filled in.  The helper's `invoke` seeds a default path and
    #     then opens the browser unconditionally - it does **not** special-case a path that is already
    #     set, so the dialog appears on every press.
    #   * The **modal task** convention: `invoke` registers a timer and returns `RUNNING_MODAL` *with
    #     the work running*, and `modal` drives it in slices.
    #
    # Both answer `RUNNING_MODAL`, so a subclass that calls one and reads the answer as if it came from
    # the other gets a modal task whose file-select handler was never set up.  Blender dies in
    # `fileselect_ensure_updated_file_params` the next time the browser draws - an
    # `EXCEPTION_ACCESS_VIOLATION`, with no Python traceback to say which operator did it.  So the two
    # are separated here by *method* rather
    # than by inspecting a result code: `invoke` chooses, `execute` works.
    file_chooser = False          # True for operators that mix in ImportHelper / ExportHelper

    def invoke(self, context, event):
        if self.file_chooser:
            # `super()`, not `bpy.types.Operator.invoke`: the base class has no Python-level `invoke`
            # at all, so naming it raises `AttributeError: type object 'Operator' has no attribute
            # 'invoke'` the moment the button is pressed.  The real implementation is the helper mixin
            # next in this class's MRO, and cooperative `super()` is what reaches it - which is the
            # whole reason these operators list `ImportHelper` / `ExportHelper` as a base.
            return super(TaskOperator, self).invoke(context, event)
        return self._start_task(context)

    def execute(self, context):
        """Run the work.

        Reached three ways, all of them ordinary: from the file chooser once a path has been accepted,
        from a script calling `bpy.ops...(...)`, and from the F3 menu.  In a real session the work is
        handed to the modal driver, so the file-chosen case is chunked and cancellable like every other
        long operation instead of freezing the UI for the length of the export.

        **`bpy.app.background` is half the test, not just `context.window`.**  A `-b` session still has
        a window object and a window manager, but it has no event loop, so a timer registered there is
        never called: the operator returns `RUNNING_MODAL` and nothing ever happens.  With no event loop
        the
        work runs straight through, which is what a background session wants anyway.
        """
        if getattr(context, "window", None) is not None and not bpy.app.background:
            return self._start_task(context)
        return self._run_sync(context)

    def _start_task(self, context):
        self._task = tc.MANAGER.create(self.task_name(), on_change=lambda _t: redraw(context))
        self._task.begin()
        self._guard = StateGuard(context)
        self._timer = None
        self._operation = None
        self._started = False
        try:
            self._operation = self.make_operation(context)
            self._operation.task = self._task
            # A modal handler is registered before the timer, and the timer is the only thing that
            # ends the modal session.  If scheduling fails after the handler exists, Blender is left
            # holding a handler for an operator that will never tick, which is worse than the original
            # error.  So the two are set up together and torn down together.
            context.window_manager.modal_handler_add(self)
            self._schedule(context)
        except Exception as error:                  # noqa: BLE001 - reported to the user
            self._teardown(context)
            self._task.fail(error)
            self._report_failure(self._task)
            self.report({'ERROR'}, str(error))
            tc.MANAGER.drop(self._task)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _teardown(self, context):
        """Undo whatever part of `invoke` got as far as registering itself.  Idempotent."""
        if self._operation is not None:
            try:
                self._operation.abort("could not start")
            except Exception:                       # noqa: BLE001 - cleanup must not raise
                pass
            self._operation = None
        if self._timer is not None:
            self._remove_timer(context)
        try:
            context.window_manager.modal_handler_remove(self)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
        guard = getattr(self, "_guard", None)
        if guard is not None:
            try:
                guard.restore(context)
            except Exception:                       # noqa: BLE001
                pass

    def _run_sync(self, context):
        """Build the whole thing synchronously, once.

        This is the no-event-loop path: a background session, or `bpy.ops` from a script that has a
        window but asked for the work to be done before returning.  It goes through the identical
        `Operation` object the modal path drives, so a test of this path is a test of the work rather
        than of a copy of it.
        """
        task = tc.MANAGER.create(self.task_name())
        task.begin()
        guard = StateGuard(context)
        operation = None
        try:
            operation = self.make_operation(context)
            operation.task = task
            while not operation.drive():
                pass
            operation.commit()
        except tc.TaskCancelled:
            task.done(tc.CANCELLED)
            if operation is not None:
                operation.abort("cancelled")
            guard.restore(context)
            tc.MANAGER.retire(task)
            self.task_abort(context, task, operation, "cancelled")
            return {'CANCELLED'}
        except Exception as error:                  # noqa: BLE001 - reported to the user
            task.fail(error)
            if operation is not None:
                operation.abort("failed")
            self._report_failure(task)
            guard.restore(context)
            tc.MANAGER.retire(task)
            self.task_abort(context, task, operation, "failed")
            return {'CANCELLED'}
        task.finish()
        guard.restore(context)
        tc.MANAGER.retire(task)
        self.task_finish(context, task, operation)
        return {'FINISHED'}

    def modal(self, context, event):
        if event.type == 'TIMER':
            task = self._task
            operation = self._operation
            try:
                if task.is_cancelled():
                    operation.abort("cancelled")
                    self._close_task(task, tc.CANCELLED)
                    self._release(context, {'CANCELLED'})
                    self.task_abort(context, task, operation, "cancelled")
                    return {'CANCELLED'}
                finished = operation.drive()
            except tc.TaskCancelled:
                operation.abort("cancelled")
                self._close_task(task, tc.CANCELLED)
                self._release(context, {'CANCELLED'})
                self.task_abort(context, task, operation, "cancelled")
                return {'CANCELLED'}
            except Exception as error:              # noqa: BLE001 - reported to the user
                task.fail(error)
                operation.abort("failed")
                self._report_failure(task)
                self._close_task(task, tc.FAILED)
                self._release(context, {'CANCELLED'})
                self.task_abort(context, task, operation, "failed")
                return {'CANCELLED'}
            if finished:
                try:
                    operation.commit()
                except Exception as error:          # noqa: BLE001 - reported to the user
                    task.fail(error)
                    operation.abort("commit refused")
                    self._report_failure(task)
                    self._close_task(task, tc.FAILED)
                    self._release(context, {'CANCELLED'})
                    self.task_abort(context, task, operation, "failed")
                    return {'CANCELLED'}
                self._close_task(task, tc.COMPLETED)
                self._release(context, {'FINISHED'})
                self.task_finish(context, task, operation)
                return {'FINISHED'}
            redraw(context)
            return {'RUNNING_MODAL'}
        if event.type in {'ESC'}:
            # ESC asks; the task stops at the next safe point, which is the only place a Blender write
            # can be interrupted without leaving a half-written datablock behind
            self._task.cancel()
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    # ------------------------------------------------------------------ internals
    def _schedule(self, context):
        """Register the tick timer.

        **The timer lives on the window manager, not on the window.**  `bpy.types.Window` has no
        `event_timer_add`; the method is `WindowManager.event_timer_add(interval, window=...)`, and
        calling it on the window raises `AttributeError: 'Window' object has no attribute
        'event_timer_add'` the moment the button is pressed.

        Because `invoke` is only reached through a real button press, harnesses that drive
        `Operation.drive()` directly never cover this function, so it needs direct testing against a
        context whose `window` has no such method.
        """
        wm = getattr(context, "window_manager", None)
        window = getattr(context, "window", None)
        if wm is None:
            raise RuntimeError("no window manager in this context, so no timer can be scheduled")
        if self._timer is not None:
            # Resolved through the class rather than `self._remove_timer`, so a host object carrying
            # only `_timer` cannot fail halfway through scheduling and leave two timers registered.
            type(self)._remove_timer(self, context)
        self._timer = wm.event_timer_add(TIMER_INTERVAL, window=window)

    def _remove_timer(self, context):
        """Remove the tick timer, wherever it is registered.  Safe to call twice."""
        timer = self._timer
        self._timer = None
        if timer is None:
            return
        wm = getattr(context, "window_manager", None)
        if wm is None:
            return
        try:
            wm.event_timer_remove(timer)
        except (ReferenceError, RuntimeError, TypeError):
            pass

    def _close_task(self, task, status):
        """Retire a finished task: it stops being current and stays on screen as a result.

        Not `drop`.  Dropping removes the card in the same call that gives it its outcome, so a
        full-length run ends by replacing itself with "idle" and the result is never seen.
        """
        task.status = status
        task.finished_at = time.perf_counter()
        tc.MANAGER.retire(task)

    def _release(self, context, result):
        self._remove_timer(context)
        guard = getattr(self, "_guard", None)
        if guard is not None:
            guard.restore(context)
        redraw(context)

    def _report_failure(self, task):
        for line in task.log_lines():
            print(line)
        self.report({'ERROR'}, "%s - %s" % (task.stage, task.error))


def _stage_runner(task, stages):        # pragma: no cover - kept only as a name for old importers
    """Superseded by `task_ops.Operation.drive`.  See that method for the step model."""
    raise NotImplementedError("stages are driven by task_ops.Operation.drive")


class TaskTimer(object):
    """`bpy.app.timers` runner for work that does not need a modal operator.

    Registered with `bpy.app.timers.register`, returns `None` when the task is finished so Blender
    unregisters it, and is tracked in `LIVE_TIMERS` so `unregister()` can stop it - an unregistered
    add-on whose timer still fires into a dead module is a crash inside Blender's timer loop.

    It drives the same `task_ops.Operation` the modal operator drives.  The difference between the two
    is only who calls `drive()` and how often: a modal operator can consume events (ESC, Cancel), and
    a timer cannot, which is why the interactive path uses the former and this exists for work that
    was started from a script or from a panel button with no modal context to attach to.
    """

    def __init__(self, name, make_operation, on_finish=None, on_fail=None):
        self.task = tc.MANAGER.create(name)
        self.task.begin()
        self.make_operation = make_operation
        self.on_finish = on_finish
        self.on_fail = on_fail
        self.operation = None
        self.registered = False

    def start(self):
        bpy.app.timers.register(self._tick, first_interval=0.01, persistent=False)
        self.registered = True
        LIVE_TIMERS.append(self)
        return self

    def _tick(self):
        try:
            if self.operation is None:
                self.operation = self.make_operation()
                self.operation.task = self.task
            if self.task.is_cancelled():
                self.operation.abort("cancelled")
                self._close(tc.CANCELLED)
                return None
            if self.operation.drive():
                self.operation.commit()
                self.task.finish()
                if self.on_finish:
                    self.on_finish(self.task, self.operation)
                self._close(tc.COMPLETED)
                return None
        except tc.TaskCancelled:
            self.operation.abort("cancelled")
            self._close(tc.CANCELLED)
            return None
        except Exception as error:                  # noqa: BLE001 - reported, not swallowed
            self.task.fail(error)
            if self.operation is not None:
                self.operation.abort("failed")
            if self.on_fail:
                self.on_fail(self.task, self.operation)
            self._close(tc.FAILED)
            return None
        redraw()
        return TIMER_INTERVAL

    def cancel(self):
        self.task.cancel()

    def _close(self, status):
        self.task.status = status
        if self in LIVE_TIMERS:
            LIVE_TIMERS.remove(self)
        # retired, not dropped: a run that failed or was cancelled has an outcome worth reading, and
        # the card is the only place it is shown
        tc.MANAGER.retire(self.task)
        self.registered = False
        redraw()


LIVE_TIMERS = []


def stop_all_timers():
    """Called from the add-on's `unregister`, so no timer outlives the module it calls into."""
    for timer in list(LIVE_TIMERS):
        try:
            bpy.app.timers.unregister(timer._tick)
        except (ValueError, RuntimeError):
            pass
        timer.task.cancel()
        timer.task.done(tc.CANCELLED)
        LIVE_TIMERS.remove(timer)
    tc.MANAGER.clear()


def progress_draw(layout, context=None, cancel_op=None, dismiss_op=None, texts=None):
    """Draw the task panel: the running task first, then the finished ones as result cards.

    Every card that has ended keeps its outcome, because that is the only place it is shown - an
    import that failed, an export that was cancelled, the elapsed time of a long run.  Dropping the
    card when the task ended is what made a second task appear to erase the first.

    A finished card is drawn *closed* rather than gone: its last stage, its elapsed time and, when
    there is one, the error that stopped it.  Returns True when anything was drawn, so the caller can
    fall back to "idle" only when there is genuinely nothing to show.

    `cancel_op`/`dismiss_op` are the caller's operator ids.  This module is shared byte for byte with
    `motion_refinery`, so it cannot name `diva_pv.cancel_task` - a scheduler helper that hard-codes
    one add-on's ids is a second add-on drawing buttons into a package it does not depend on.

    `texts` is the same reasoning one level down: the caller's language table owns the words, this
    module owns the drawing.  Keys read, each falling back to the English default this shipped
    with: `cancel`, `dismiss`, `dismiss_all`, `end_words` (status -> word), `stage_name` (a callable
    that translates a stage), and the small templates `progress`, `counts`, `stage_run`, `ended`,
    `elapsed`, `speed`, `eta`.
    """
    tasks = tc.MANAGER.visible()
    if not tasks:
        return False
    texts = texts or {}
    stage_name = texts.get("stage_name") or (lambda s: s)
    end_word = texts.get("end_words") or _END_WORD
    for task in tasks:
        box = layout.box()
        running = task.status not in tc.TERMINAL
        box.label(text=task.name,
                  icon='TIME' if running else _END_ICON.get(task.status, 'INFO'))
        if running:
            if task.determinate:
                box.progress(factor=task.fraction,
                             text=texts.get("progress", "%s  %d%%")
                                  % (stage_name(task.stage), round(100 * task.fraction)))
                box.label(text=texts.get("counts", "%d / %d") % (task.current, task.total))
            else:
                # an indeterminate bar, because a percentage that does not move is a lie
                box.label(text=texts.get("stage_run", "%s ...") % stage_name(task.stage))
        else:
            # what it ended as, and where it got to
            box.label(text=texts.get("ended", "%s at '%s'")
                          % (end_word.get(task.status, task.status.lower()),
                             stage_name(task.stage)))
        if task.message and task.message != task.stage:
            # the scheduler's few canned words (a bare "done") map through the text pack; real
            # messages - paths, MB counts, errors - pass through untranslated, as they should
            box.label(text=(texts.get("messages") or {}).get(task.message, task.message))
        row = box.row(align=True)
        row.label(text=texts.get("elapsed", "elapsed %.1fs") % task.elapsed)
        if running:
            if task.speed:
                row.label(text=texts.get("speed", "%.0f/s") % task.speed)
            if task.eta:
                row.label(text=texts.get("eta", "eta %.0fs") % task.eta)
            if cancel_op:
                row.operator(cancel_op, text=texts.get("cancel", "Cancel"))
        else:
            # The id travels with the button, so it dismisses the card it is drawn on rather than
            # whatever happens to be first.  `task_id` is a declared operator property, which is the
            # only way a panel can hand an argument to a button - `operator()` takes no data.
            if dismiss_op:
                row.operator(dismiss_op,
                             text=texts.get("dismiss", "Dismiss")).task_id = task.id
    if dismiss_op and sum(1 for _t in tasks if _t.status in tc.TERMINAL) > 1:
        # one way to clear the whole history, next to the cards it clears
        layout.operator(dismiss_op,
                        text=texts.get("dismiss_all", "Dismiss all finished")).task_id = ""
    return True


# The two words and the icon a finished card carries.  Kept next to the drawing rather than in the
# language tables because they are statuses, not options - there is no dialog to hover.
_END_WORD = {tc.COMPLETED: "finished", tc.CANCELLED: "cancelled", tc.FAILED: "failed"}
_END_ICON = {tc.COMPLETED: 'CHECKMARK', tc.CANCELLED: 'X', tc.FAILED: 'ERROR'}
