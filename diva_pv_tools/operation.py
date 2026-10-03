"""The shared step model behind every long operation in this add-on.

`task_core` schedules and `task_blender` drives, but neither knows what a "motion cleanup" or a
"VMD import" is.  That knowledge is an `Operation`: a generator of stages, each stage a generator of
bounded units, stepped by one `drive()` so that cancellation, the state guard, the progress
bar, the cleanup contract and the benchmark are written once instead of once per operator.

    stage()      ->  yields a generator
        unit()   ->  yields once per bounded piece of work

**The cleanup contract.**  `abort()` runs on every exit path that is not a successful commit -
cancel, failure, unregister - and must be safe to call twice and safe to call when nothing ran.
Three rules, in order of importance: the user's original data is only read; anything Blender-visible
is built into a *temporary* datablock so a half-built result is unreachable; whatever did become
visible is removed by `abort`.

The package `motion_refinery` carries a byte-identical copy of this file; changes here must be
kept in sync with it.
"""
import gc
import math
import os
import time

import bpy

from . import task_core as tc

# Seconds of work one stage should hand back before the scheduler regains control.  A little under
# the 20 ms budget `task_core` uses, because the caller's own bookkeeping happens after the yield and
# has to fit in the same frame.
UNIT_BUDGET = tc.DEFAULT_BUDGET


class Operation(object):
    """Base class for one long operation: prepare, process, finalize, cancel, rollback, commit.

    Subclasses implement `stages()`, whose entries are `(name, weight, generator-of-generators)`, and
    override only the lifecycle hooks they need.  The base class supplies the parts that are the same
    everywhere and easy to forget in one place: the guard that restores Blender state, the abort that
    runs on every non-success path, and the counters the benchmark report is built from.
    """

    name = "operation"

    def __init__(self, context=None, task=None, options=None):
        self.context = context or bpy.context
        self.task = task
        self.options = dict(options or {})
        self.state = {}
        self.report = {}                    # what the panel shows when it is over
        self.warnings = []
        self.error = None
        # ---- benchmark counters, filled by `drive`
        self.chunks = 0
        self.chunk_seconds = []
        self.unit_seconds = []
        self.stage_seconds = []
        self.t_zero = None
        # ---- cleanup bookkeeping, consumed by `abort`
        self._created_actions = []
        self._created_objects = []
        self._temp_paths = []
        self._committed = False
        self.stages_done = False
        self.transaction = None
        self._gc_was_enabled = None
        # ---- progress, counted in units rather than in whatever a stage happens to own ----
        self.unit_total = 0
        self.units_done = 0

    # ------------------------------------------------------------------ the collector
    def cancel_requested(self):
        """Whether the user has asked this run to stop.  Cheap enough to call from a poll unit."""
        return self.task is not None and self.task.is_cancelled()

    def suspend_gc(self):
        """Turn the cyclic collector off for the duration, and remember to put it back.

        Disabling it removes the walk that lands *between* two units and shows up as a 95 ms tick:
        the collector is a tax on this workload, not an optimisation of it, because building the mot
        set's keysets allocates about two million two-element lists, every one of them a container
        the generational collector tracks.

        Nothing here creates reference cycles - the data structures are trees and the few
        back-references are weak - so reference counting alone reclaims all of it.

        `gc.disable` is refcounted, so nesting is safe, and the previous state is restored whatever the
        operation did: `drive`'s `finally` covers completion, cancellation, failure and the generator
        being closed.  A leak of cycles is the worst case if some future stage does build one, and a
        leak is not a correctness problem in a Blender session that ends with the file.
        """
        if self._gc_was_enabled is None:
            self._gc_was_enabled = gc.isenabled()
            gc.disable()
        return self

    def resume_gc(self):
        if self._gc_was_enabled is None:
            return
        was = self._gc_was_enabled
        self._gc_was_enabled = None
        if was:
            gc.enable()

    # ------------------------------------------------------------------ to override
    def stages(self):
        """`[(stage_name, weight, generator_of_units)]`, weights calibrated against measurement."""
        return []

    def prepare(self):
        """Cheap checks and datablock lookups, before the first heavy stage."""

    def finalize(self):
        """Runs after the last stage, before the commit.  Must be re-entrant-safe."""

    def commit(self):
        """Make the result the user's data.  Only called when every stage completed."""
        self._committed = True

    def abort(self, reason):
        """Undo whatever became visible.  Called on cancel, failure and unregister; idempotent."""
        for action in self._created_actions:
            self._remove_action(action)
        self._created_actions = []
        for obj in self._created_objects:
            self._remove_object(obj)
        self._created_objects = []
        for path in self._temp_paths:
            self._discard_file(path)
        self._temp_paths = []
        if self.transaction is not None:
            self.transaction.rollback()
            self.transaction = None

    def describe(self):
        """The one-line summary the operator reports when it finishes."""
        return "%s: done" % self.name

    # ------------------------------------------------------------------ helpers for subclasses
    def track_action(self, action):
        """Declare a datablock this operation created, so `abort` can remove it again."""
        if action is not None and action not in self._created_actions:
            self._created_actions.append(action)
        return action

    def track_object(self, obj):
        if obj is not None and obj not in self._created_objects:
            self._created_objects.append(obj)
        return obj

    @staticmethod
    def _remove_action(action):
        try:
            if action.users == 0 or action.name.endswith(("_optimized",)):
                bpy.data.actions.remove(action, do_unlink=True)
            elif action.users == 0:
                bpy.data.actions.remove(action, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    @staticmethod
    def _remove_object(obj):
        try:
            data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.armatures.remove(data, do_unlink=True)
        except (ReferenceError, RuntimeError):
            pass

    @staticmethod
    def _discard_file(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def temp_path(self, final_path):
        """A sibling temporary the writer fills, so a cancel cannot leave a plausible-looking file.

        The partial file is written to `<name>.part` and only renamed onto the real name once the
        writer returned successfully.  A cancel therefore leaves either the previous file or nothing -
        never a truncated one that the game would try to load and the user would believe in.
        """
        path = final_path + ".part"
        if path not in self._temp_paths:
            self._temp_paths.append(path)
        return path

    def accept_temp(self, final_path):
        """Rename the temporary onto its final name.  Atomic on both NTFS and POSIX."""
        temp = final_path + ".part"
        os.replace(temp, final_path)
        if temp in self._temp_paths:
            self._temp_paths.remove(temp)
        return final_path

    # ------------------------------------------------------------------ the one stepper
    @staticmethod
    def invoke(work):
        """Wrap one bound of work as a zero-argument callable.

        The unit contract is "a callable that does one bounded piece of work and returns", and it is
        worth being strict about it.  The scheduler measures the wall clock around the call and adapts
        the next chunk to it, so what the callable *does* is the thing the budget applies to.  A plain
        value handed in instead of a callable - a single `FCurve`, say - would be iterated and fail as
        "'FCurve' object is not iterable", surfacing far from the stage that produced it.
        """
        if callable(work):
            return work
        return lambda: work

    @staticmethod
    def unit(fn):
        """Adapt a stage body into a unit factory, so a stage can be written either way.

        A stage body is naturally one of two shapes:

          * a **generator** - `def stage(): ...; yield boundary` - for work that has its own internal
            boundaries.  Each `yield` is one scheduling point: `yield None` when the stage has already
            done its own stepping and only wants the cancel checked, or `yield <callable>` when it has
            a bound of work for the scheduler to measure and size.
          * a **plain function** - `def stage(): ...` - for a stage that is a single indivisible
            action.  `Operation.units` calls `factory()` and expects something steppable, so a plain
            function returns `None` and fails much later with "'NoneType' object is not iterable" and
            no indication of which stage ran.

        This wrapper makes both correct, and `drive` skips a `None` unit rather than iterating it, so
        `yield None` means exactly what it looks like: one scheduling boundary.
        """
        def factory():
            produced = fn()
            if produced is None:
                yield None
                return
            if hasattr(produced, "__next__"):
                for step in produced:
                    yield step
                return
            yield produced
        factory.__name__ = getattr(fn, "__name__", "unit")
        return factory

    def units(self):
        """Every stage, flattened into one stream of unit generators, with the weights registered.

        Flattened rather than nested so that `drive` has a single loop: one place times the work, one
        place reports progress, one place checks the cancel.  The stage name travels with the unit so
        the panel can say which part is running.
        """
        stages = self.stages()
        if self.task is not None:
            self.task.set_stages([(name, weight) for name, weight, _fn in stages])
        for name, _weight, factory in stages:
            if self.task is not None:
                self.task.set_stage(name)
            started = time.perf_counter()
            for step in factory():
                yield name, step
            self.stage_seconds.append((name, time.perf_counter() - started))

    def drive(self, budget=None):
        """Step the whole operation once.  Returns True when it completed.

        This is what both the modal timer and the synchronous `execute` call, which is the point: the
        work does not know which of them is driving it, so the tested path and the interactive path
        are the same path.  The only difference is how often the caller comes back.

        A unit is a zero-argument callable that does one bounded piece of work - see `invoke`.  It runs
        under a `Task.chunks`-style loop: the clock is read around the call, the next unit is skipped
        when the budget is spent, and `raise_if_cancelled` fires at that same boundary.  So both the
        sizing and the cancellation ride on one mechanism rather than being checked separately.
        """
        if self.t_zero is None:
            # set once, not per tick: `drive` is called thousands of times by the modal timer, and
            # resetting the clock each time would make `elapsed` - the number the panel and the
            # report both show - report the duration of the last tick instead of the operation
            self.t_zero = time.perf_counter()
        if not hasattr(self, "_stream"):
            self.prepare()
            self._stream = self.units()
        elif self.stages_done:
            # Nothing left to do, and `next()` would raise StopIteration every time.  Calling an
            # operation a second time after it finished would also re-run `finalize` - which on the
            # import means re-resolving the source path and, if it has gone missing, raising
            # FileNotFoundError from a place unrelated to the caller's mistake.
            return True
        # One unit is one step of the bar, whichever stage it belongs to.  If stages set
        # `task.total` to their own quantities (frames in one, keysets in the next), the bar's
        # meaning would change half way through and the "current / total" line would be
        # meaningless in both halves.  Counting units keeps the stage weights doing the work they
        # were calibrated for, and it needs no cooperation from the stage beyond yielding.
        total_units = self.unit_total
        if self.task is not None and total_units:
            self.task.set_total(total_units)

        def bump():
            self.units_done += 1
            if self.task is not None and total_units:
                self.task.set_progress(self.units_done, total_units)

        self.suspend_gc()
        try:
            return self._step(bump, budget)
        finally:
            # every path out of a tick restores the collector, including the ones that raise: a
            # cancel raises TaskCancelled from inside, and a failure propagates to the caller
            self.resume_gc()

    def _step(self, bump, budget=None):
        """Run units until the budget is spent, then hand the thread back.

        **The budget is a ceiling, not a target.**  A tick keeps taking units while it is still inside
        the budget and stops as soon as it is not - so a stage made of cheap units gets many per tick
        and an expensive one gets one, which is the property a fixed units-per-tick count cannot have.

        That direction matters: units are bounded in *work*, not in time - a single `frame_set` is
        cheap, a bone's whole solve in an IK chain is not, so one unit per tick still misses the
        20 ms the panel promises.  Measuring *before* the next unit rather than after the last
        one is what turns the budget into an upper bound instead of an average: it
        adds the cheap units together and never starts a unit it cannot afford.
        """
        budget = float(budget or (self.task._budget if self.task is not None else tc.DEFAULT_BUDGET))
        deadline = time.perf_counter() + budget
        while True:
            started = time.perf_counter()
            try:
                name, unit = next(self._stream)
            except StopIteration:
                self.finalize()
                self.stages_done = True
                self.chunk_seconds.append(time.perf_counter() - started)
                return True
            if self.task is not None:
                self.task.set_stage(name)
            if unit is not None:
                u0 = time.perf_counter()
                unit()
                # the unit's own cost, recorded so the benchmark can name the worst one instead of
                # pointing at the stage that contains it - a stage can hold two orders of magnitude
                # of unit sizes
                self.unit_seconds.append((time.perf_counter() - u0, name))
                self.chunks += 1
                bump()
            self.chunk_seconds.append(time.perf_counter() - started)
            if self.task is not None:
                # the cancel is checked at the only boundary there is - between two units - so it
                # stops at a safe point rather than inside a Blender write, on every path
                self.task.raise_if_cancelled()
                # decided *before* fetching the next unit, so the budget bounds this tick rather than
                # being discovered to have been exceeded after the next unit has already run
                if time.perf_counter() >= deadline:
                    return False

    @property
    def elapsed(self):
        return (time.perf_counter() - self.t_zero) if self.t_zero else 0.0

    def benchmark(self):
        """The per-operation benchmark report."""
        chunks = self.chunk_seconds or [0.0]
        units = self.unit_seconds or [(0.0, "-")]
        worst_unit = max(units)
        by_stage = {}
        for spent, name in units:
            if spent > by_stage.get(name, (0.0,))[0]:
                by_stage[name] = (spent,)
        return {
            "operation": self.name,
            "total_seconds": round(sum(self.chunk_seconds), 4),
            "wall_seconds": round(self.elapsed, 4),
            "chunks": self.chunks,
            "max_chunk_ms": round(1000 * max(chunks), 2),
            "mean_chunk_ms": round(1000 * sum(chunks) / len(chunks), 2),
            "max_unit_ms": round(1000 * worst_unit[0], 2),
            "max_unit_stage": worst_unit[1],
            "units": len(units),
            "unit_max_by_stage": sorted(((round(1000 * v[0], 2), k) for k, v in by_stage.items()),
                                        reverse=True)[:6],
            "stages": [(n, round(s, 3)) for n, s in self.stage_seconds],
        }


def _unbudgeted(unit):
    """The whole unit in one go, for a caller that has no task (a plain scripted call)."""
    yield unit


def run_to_completion(operation, task=None):
    """Drive `operation` to the end without ever returning to Blender's event loop.

    Only for a caller that has no event loop to return to - a background Blender, a unit test.  The
    interactive path never uses this; that is the whole point of `drive`.
    """
    while not operation.drive():
        pass
    return operation




def log_benchmark(operation, prefix="[Benchmark]"):
    """Print the block the report quotes, so a console reader gets the same numbers as the panel."""
    row = operation.benchmark()
    print("%s %s" % (prefix, row["operation"]))
    print("%s   total %.2f s in %d chunk(s)" % (prefix, row["total_seconds"], row["chunks"]))
    print("%s   chunk  max %.1f ms  mean %.1f ms" % (prefix, row["max_chunk_ms"],
                                                     row["mean_chunk_ms"]))
    print("%s   unit   max %.1f ms in '%s' over %d unit(s)"
          % (prefix, row["max_unit_ms"], row["max_unit_stage"], row["units"]))
    for ms, name in row["unit_max_by_stage"]:
        print("%s   unit   %-28s %8.1f ms" % (prefix, name, ms))
    for name, seconds in row["stages"]:
        print("%s   stage  %-28s %8.2f s" % (prefix, name, seconds))
    return row


class OffThread(object):
    """Run one bpy-free call on a worker thread, and hand the scheduler units to watch it with.

    Work that touches `bpy.data` cannot happen off the main thread at all.  These three calls are
    the other case - `camera_core`, `audio_ops` and `face_core` import no bpy, they
    read input files, run a child process or a solver, and write a temp file - so the only thing
    they can do to a running Blender is hold the thread that calls them.

    Holding that thread is the defect: during a long unit the window does not redraw and
    the Cancel button cannot work, because a button press is an event and no events are being read.

    Each unit here waits at most `poll` seconds, so a tick costs about that much and the event loop
    runs in between.  The cancellation contract is kept: the flag is handed to the writer, which
    checks it **before publishing**, so a cancel leaves the user's existing file untouched rather
    than half-written.  A cancel that arrives after the writer passed that check is reported as a
    cancel and the result is discarded by the caller's `abort`.
    """

    def __init__(self, fn, label="async", poll=0.008, on_cancel=None):
        self.fn = fn
        self.label = label
        self.poll = poll
        self.on_cancel = on_cancel
        self.stop = [False]          # read by the worker's cancel predicate, set by the main thread
        self.thread = None
        self.result = None
        self.error = None
        self.cancelled = False
        self.ticks = 0

    def _worker(self):
        try:
            self.result = self.fn()
        except BaseException as exc:                    # re-raised on the main thread, where it is
            self.error = exc                            # reported through the task card like any other

    def start(self):
        import threading
        self.thread = threading.Thread(target=self._worker, name="diva-%s" % self.label,
                                       daemon=True)
        self.thread.start()

    def step(self):
        """One bounded wait.  The only thing that can happen here is the clock, so it is short."""
        self.ticks += 1
        if not self.cancelled and self.on_cancel is not None and self.on_cancel():
            self.cancelled = True
            self.stop[0] = True
        self.thread.join(self.poll)

    def finish(self):
        if self.cancelled:
            raise tc.TaskCancelled("diva-%s" % self.label)
        if self.error is not None:
            raise self.error

    def units(self):
        """`start`, then one unit per poll, then `finish`.  Usable as `yield from off.units()`."""
        yield self.start
        while self.thread.is_alive():
            yield self.step
        yield self.finish
