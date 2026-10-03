"""A cooperative task system for Blender, with the Blender parts kept out of it.

The rule of this add-on is that no single synchronous operation may hold the UI, and the rule that
follows from it is that a long loop must not run to completion in one call.  This module provides
the piece
that makes that possible without turning the whole plugin inside out: a task that knows its stages,
its progress and whether it has been cancelled, and that hands work back in **time-bounded chunks**
rather than in fixed-size ones.

Three decisions here are worth stating, because the obvious alternative to each is wrong.

**The chunk size is measured, not configured.**  A fixed `CHUNK_SIZE = 100` runs in 2 ms on one
machine and 300 ms on another, and on the same machine it runs in 2 ms over a cheap loop and 300 ms
over an expensive one.  `Task.chunks` therefore times each chunk and adjusts towards a target budget,
so a cheap loop gets large chunks and an expensive one gets small ones without anybody choosing.

**Stages carry weights from measurement.**  Evenly dividing 100% across twelve stages produces a bar
that sits at 40% for two minutes and then jumps, which is worse than no bar because it actively lies.
A stage declares a weight, and the weights are calibrated against real timings on the real clip; the
default weights below are those measurements.

**Cancellation is checked at safe points, never forced.**  A worker cannot be interrupted inside a
Blender RNA write, so `raise_if_cancelled` is called between chunks and between stages, and the
contract is that the task stops at the next boundary rather than immediately.

Nothing in this module imports bpy.  That is deliberate: a scheduler that can only be tested inside a
running Blender is a scheduler whose bugs are found by users.
"""
import time
import traceback

IDLE = "IDLE"
PREPARING = "PREPARING"
RUNNING = "RUNNING"
CANCELLING = "CANCELLING"
CANCELLED = "CANCELLED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

TERMINAL = (CANCELLED, COMPLETED, FAILED)
# The states a task is *working* in.  `IDLE` is deliberately not one of them: a task that was created
# and never begun is not running, and counting it as active would draw it on the panel as a live card
# with a Cancel button attached to nothing.
ACTIVE = (PREPARING, RUNNING, CANCELLING)

# Seconds of work a single chunk should aim for.  Well under a frame at 60 fps would be wasteful and
# anything approaching 100 ms is visible as a stutter, so the target sits between those.
DEFAULT_BUDGET = 0.020
MIN_CHUNK = 1
MAX_CHUNK = 8192


class TaskCancelled(Exception):
    """Raised at a safe point so the caller's `finally` blocks still run."""


def _now():
    return time.perf_counter()


# Task ids come from a counter, not from the clock.
#
# The id is the key of `TaskManager.tasks`, so two tasks that share one are not two tasks - the second
# *replaces* the first, and the first card disappears.  The clock is the wrong source for it:
# `"task-%d" % (int(_now() * 1000) % 100000000)` is a millisecond timestamp, so any two tasks created
# in the same millisecond collide.  A counter cannot collide at all, and it makes an id stable to
# assert on rather than a number that changes every run.
_TASK_SERIAL = [0]


def _next_task_id():
    _TASK_SERIAL[0] += 1
    return "task-%d" % _TASK_SERIAL[0]


def run_units(steps):
    """Drive a generator of units and return the value it finished with.

    A "unit" is either a zero-argument callable that does one bounded piece of work, or `None` for a
    bare boundary with nothing to do but check the cancel.  The generator's own `return` value is the
    result, which is how every whole-run form in this plugin wraps its stepping counterpart:

        def optimize(...):
            return run_units(optimize_steps(...))

    Calling `next(steps)` in a loop without running the yielded unit silently skips every piece of
    work while still returning a complete-looking verdict - the worst class of bug in this file's
    neighbourhood - so the stepping contract lives in exactly one place: here.
    """
    while True:
        try:
            unit = next(steps)
        except StopIteration as stop:
            return stop.value
        if unit is not None:
            unit()


class Task(object):
    """One unit of work the UI can watch, and stop."""

    __slots__ = ("id", "name", "status", "stage", "stage_index", "stage_count", "message",
                 "current", "total", "started_at", "finished_at", "cancel_requested", "error",
                 "traceback", "result", "notes", "_stage_marks", "_stage_weights", "_samples",
                 "_chunk", "_budget", "on_change")

    def __init__(self, name, task_id=None, on_change=None, budget=DEFAULT_BUDGET):
        self.id = task_id or _next_task_id()
        self.name = name
        self.status = IDLE
        self.stage = "preparing"
        self.stage_index = 0
        self.stage_count = 1
        self.message = ""
        self.current = 0
        self.total = 0
        self.started_at = None
        self.finished_at = None
        self.cancel_requested = False
        self.error = None
        self.traceback = None
        self.result = None
        self.notes = []
        self._stage_marks = []          # [(name, start_fraction, end_fraction)]
        self._stage_weights = []
        self._samples = []              # (timestamp, completed) for the speed and the ETA
        self._chunk = 64
        self._budget = float(budget)
        self.on_change = on_change

    # ------------------------------------------------------------------ lifecycle
    def begin(self, message=""):
        self.status = PREPARING
        self.started_at = _now()
        self.message = message or "preparing"
        self._changed()
        return self

    def start(self, message=""):
        self.status = RUNNING
        self.stage = "running"
        if message:
            self.message = message
        self._changed()
        return self

    def finish(self, result=None, message="done"):
        self.result = result
        self.status = COMPLETED
        self.finished_at = _now()
        self.stage_index = self.stage_count
        self.current = self.total or self.current
        self.message = message
        self._changed()
        return result

    def fail(self, error, message=None):
        self.status = FAILED
        self.finished_at = _now()
        self.error = "%s: %s" % (type(error).__name__, error)
        self.traceback = traceback.format_exc()
        self.message = message or self.error
        self._changed()
        return self

    def cancel(self):
        """Ask the task to stop.  It stops at the next safe point, not inside an RNA write."""
        if self.status in TERMINAL:
            return False
        self.cancel_requested = True
        if self.status == RUNNING:
            self.status = CANCELLING
        self.message = "cancelling"
        self._changed()
        return True

    def is_cancelled(self):
        return self.cancel_requested or self.status in (CANCELLING, CANCELLED)

    def raise_if_cancelled(self):
        if self.is_cancelled():
            raise TaskCancelled(self.name)

    def done(self, status=CANCELLED, message=None):
        self.status = status
        self.finished_at = _now()
        self.message = message or status.lower()
        self._changed()

    # ------------------------------------------------------------------ stages
    def set_stages(self, stages):
        """`stages` is [(name, weight)]; weights come from measurement, not from counting stages."""
        total = float(sum(w for _n, w in stages)) or 1.0
        self._stage_weights = [(n, w / total) for n, w in stages]
        self.stage_count = len(self._stage_weights)
        self._stage_marks = []
        start = 0.0
        for name, weight in self._stage_weights:
            self._stage_marks.append((name, start, start + weight))
            start += weight
        return self

    def set_stage(self, name, message=None):
        """Enter a stage.  The *total* deliberately survives the transition.

        A stage boundary is not a progress boundary: an operation that knows it has 1.7 M keyframes to
        write should keep saying so while it moves from "prepare curves" to "write keys", and resetting
        the total here would make every stage after the one that set it report an indeterminate bar.
        What resets is the *current*
        count, because it is a position within a stage.
        """
        for i, (stage_name, lo, hi) in enumerate(self._stage_marks):
            if stage_name == name:
                self.stage_index = i
                self.stage = name
                self.current = 0
                if message:
                    self.message = message
                self._changed()
                return self
        self.stage = name
        self.current = 0
        if message:
            self.message = message
        self._changed()
        return self

    def enter_stage(self, name, message=None):
        """Context-manager form of `set_stage`, so the stage closes even on an exception."""
        return _Stage(self, name, message)

    # ------------------------------------------------------------------ progress
    def set_progress(self, current, total=None, message=None):
        self.current = int(current)
        if total is not None:
            self.total = int(total)
        if message is not None:
            self.message = message
        if self.total > 0:
            self._samples.append((_now(), self.current))
            if len(self._samples) > 64:
                del self._samples[:32]
        self._changed()
        return self

    def set_total(self, total):
        self.total = int(total)
        self._changed()
        return self

    def advance(self, count=1, message=None):
        return self.set_progress(self.current + count, None, message)

    def set_message(self, message):
        self.message = message
        self._changed()
        return self

    def note(self, message):
        self.notes.append(message)
        return self

    # ------------------------------------------------------------------ derived numbers
    @property
    def elapsed(self):
        if self.started_at is None:
            return 0.0
        return (self.finished_at or _now()) - self.started_at

    @property
    def eta(self):
        """Seconds remaining, from a moving average so one slow chunk cannot say 99999.

        Returns None when there is not yet enough evidence, which the UI renders as an indeterminate
        bar.  Showing a made-up percentage that then sits still is worse than showing none.
        """
        if self.status in TERMINAL or len(self._samples) < 3 or self.total <= 0:
            return None
        first_t, first_c = self._samples[0]
        last_t, last_c = self._samples[-1]
        done = last_c - first_c
        span = last_t - first_t
        if done <= 0 or span <= 1e-6:
            return None
        rate = done / span
        if rate <= 1e-9:
            return None
        remaining = max(0, self.total - self.current)
        return remaining / rate

    @property
    def speed(self):
        if len(self._samples) < 2:
            return None
        first_t, first_c = self._samples[0]
        last_t, last_c = self._samples[-1]
        span = last_t - first_t
        if span <= 1e-6:
            return None
        return (last_c - first_c) / span

    @property
    def fraction(self):
        """Overall 0..1, folding the current item progress into the current stage's own share."""
        if self.status == COMPLETED:
            return 1.0
        if not self._stage_marks:
            return (self.current / float(self.total)) if self.total else 0.0
        _name, lo, hi = self._stage_marks[min(self.stage_index, len(self._stage_marks) - 1)]
        inner = (self.current / float(self.total)) if self.total > 0 else 0.0
        return max(0.0, min(1.0, lo + (hi - lo) * inner))

    @property
    def determinate(self):
        return self.total > 0

    def as_dict(self):
        return {"id": self.id, "name": self.name, "status": self.status, "stage": self.stage,
                "stage_index": self.stage_index, "stage_count": self.stage_count,
                "fraction": round(self.fraction, 5), "message": self.message,
                "current": self.current, "total": self.total,
                "elapsed": round(self.elapsed, 3), "speed": self.speed,
                "eta": self.eta, "cancelled": self.is_cancelled(), "error": self.error}

    # ------------------------------------------------------------------ chunking
    def chunks(self, items, budget=None, minimum=MIN_CHUNK, maximum=MAX_CHUNK):
        """Yield time-bounded slices of `items`, adapting the size after each one.

        The chunk boundary is where the caller gets control back and where cancellation is noticed, so
        the size is chosen to make each slice about `budget` seconds long.  Because a generator resumes
        after the consumer's body has run, the time measured around the `yield` includes the work done
        on the chunk - which is exactly the quantity the size should follow.
        """
        budget = float(budget or self._budget)
        count = len(items) if hasattr(items, "__len__") else None
        if count is not None:
            self.set_total(count)
        size = max(minimum, min(maximum, self._chunk))
        index = 0
        while True:
            if count is not None and index >= count:
                break
            started = _now()
            if count is None:
                chunk = []
                for item in items:
                    chunk.append(item)
                    if len(chunk) >= size:
                        break
                if not chunk:
                    break
            else:
                chunk = items[index:index + size]
                if not chunk:
                    break
            yield chunk
            spent = _now() - started
            index += len(chunk)
            self._chunk = _adapt(size, spent, budget, minimum, maximum)
            size = self._chunk
            if count is not None:
                self.set_progress(index, count)
            else:
                self.advance(len(chunk))
            self.raise_if_cancelled()

    def run_chunks(self, items, work, budget=None):
        """Drive `chunks` and call `work(chunk)` for each, returning the collected results."""
        out = []
        for chunk in self.chunks(items, budget=budget):
            value = work(chunk)
            if value is not None:
                out.append(value)
        return out

    # ------------------------------------------------------------------ logging
    def log_lines(self):
        """The standard task log block, so a console reader sees the same thing as the panel."""
        lines = ["[Task] %s" % self.name, "[Stage] %s" % self.stage]
        if self.determinate:
            lines.append("[Progress] %d%%" % round(100 * self.fraction))
            lines.append("[Current] %d / %d" % (self.current, self.total))
        else:
            lines.append("[Progress] working")
        if self.speed is not None:
            lines.append("[Speed] %.1f items/s" % self.speed)
        if self.eta is not None:
            lines.append("[ETA] %.1fs" % self.eta)
        lines.append("[Elapsed] %.1fs" % self.elapsed)
        if self.status == FAILED:
            lines.append("[Task] FAILED")
            lines.append("[Error] %s" % self.error)
            lines.append("[Traceback]\n%s" % self.traceback)
        return lines

    def log(self, emit=print):
        for line in self.log_lines():
            emit(line)

    def _changed(self):
        if self.on_change is not None:
            self.on_change(self)


def _adapt(size, spent, budget, minimum=MIN_CHUNK, maximum=MAX_CHUNK):
    """The next chunk size, from how long this one took against the budget.

    The step is proportional rather than ±1, so a chunk that took ten times the budget shrinks by
    roughly ten times in one go instead of creeping down over dozens of ticks.  The size never reaches
    zero and never grows without bound.
    """
    if spent <= 1e-9:
        return min(maximum, size * 2)
    ratio = budget / spent
    if 0.75 <= ratio <= 1.35:
        return size
    grown = int(size * max(0.25, min(4.0, ratio)))
    return max(minimum, min(maximum, grown))


class _Stage(object):
    """Context manager so a stage is always closed, including on the way out of an exception."""

    __slots__ = ("task", "name")

    def __init__(self, task, name, message=None):
        self.task = task
        self.name = name
        task.set_stage(name, message)

    def __enter__(self):
        return self.task

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.task.set_progress(self.task.total or 1, self.task.total or 1)
        return False


class TaskManager(object):
    """The tasks the UI can see.  One *running* task at a time, which is what a modal operator implies.

    Finished tasks are **kept**, not dropped.  Removing one the moment it ends is what makes a new
    task erase the previous card: by the time the panel draws again the card it is drawing has been
    deleted, so a full-length export finishes by silently replacing itself
    with "idle" and the result goes unread.  The card is the only place a run's outcome is visible - the
    status line, the elapsed time, the error - so removing it removes the answer to "did that work".

    `current()` still answers "what is being driven right now" and stays `None` between runs; that
    distinction is load-bearing, because the cancel button reads it.  What the
    panel draws is `visible()`.  `FINISHED_KEPT` bounds the history so a long session cannot grow it
    without limit, and only `clear()` - which runs on unregister - takes everything.
    """

    FINISHED_KEPT = 8

    def __init__(self):
        self.tasks = {}
        self.order = []
        self.current_id = None

    def create(self, name, on_change=None, budget=DEFAULT_BUDGET):
        task = Task(name, on_change=on_change, budget=budget)
        self.tasks[task.id] = task
        self.order.append(task.id)
        self.current_id = task.id
        self._prune()
        return task

    def current(self):
        """The task being driven now, or `None`.  `None` is also the answer after one finishes."""
        return self.tasks.get(self.current_id)

    def drop(self, task):
        """Forget a task entirely.  For a task that never really started, not for one that finished."""
        self.tasks.pop(task.id, None)
        if task.id in self.order:
            self.order.remove(task.id)
        if self.current_id == task.id:
            self.current_id = None

    def dismiss(self, task_id):
        """Remove one finished card, or all of them when `task_id` is empty.

        A *running* task is never dismissed by this: its card carries the progress bar and the cancel
        button, and making "dismiss" able to remove it would be a second way to lose a run without
        cancelling it.  Returns the number of cards removed, so the caller can say whether the button
        did anything.
        """
        gone = 0
        for task_id_key in list(self.order):
            task = self.tasks.get(task_id_key)
            if task is None or task.status not in TERMINAL:
                continue
            if task_id and task_id_key != task_id:
                continue
            self.tasks.pop(task_id_key, None)
            self.order.remove(task_id_key)
            gone += 1
        return gone

    def retire(self, task):
        """Finish with a task: it stops being current, and stays on screen as a result card."""
        self.current_id = None
        self._prune()
        return task

    def visible(self):
        """What the panel draws: the running task first, then the finished ones, oldest first."""
        return [self.tasks[i] for i in self.order if i in self.tasks]

    def _prune(self):
        """Drop the oldest *finished* cards until at most `FINISHED_KEPT` of them remain.  Never a live
        one, and never enough of them to take the history over the cap - which is why the count is
        checked after retiring a live task as well as while one is running."""
        finished = [i for i in self.order
                    if i in self.tasks and self.tasks[i].status in TERMINAL]
        for task_id in finished[:max(0, len(finished) - self.FINISHED_KEPT)]:
            self.tasks.pop(task_id, None)
            self.order.remove(task_id)

    def clear(self):
        self.tasks.clear()
        del self.order[:]
        self.current_id = None

    def active(self):
        """The tasks that are actually working.  Not `IDLE` ones, and not finished ones."""
        return [self.tasks[i] for i in self.order
                if self.tasks[i].status in ACTIVE]


MANAGER = TaskManager()


def run_stages(task, stages, budget=None):
    """Run `[(name, weight, callable)]` as one task, with cancellation between them.

    Every stage is a callable that receives the task and does its own chunking.  The weights decide
    how much of the bar each one owns; they are calibrated numbers rather than `1 / len(stages)`.
    """
    task.set_stages([(name, weight) for name, weight, _fn in stages])
    task.start()
    try:
        for name, _weight, fn in stages:
            task.raise_if_cancelled()
            task.set_stage(name)
            fn(task)
    except TaskCancelled:
        task.done(CANCELLED)
    except Exception as error:                      # noqa: BLE001 - reported, not swallowed
        task.fail(error)
    # the task comes back on every path: `status` already distinguishes the outcomes, and returning
    # None on failure would throw away the error, the traceback and the stage it happened in
    return task
