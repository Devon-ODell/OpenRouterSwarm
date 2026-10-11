"""Per-repository runtime context (P3).

The daemon's repository state lived in mutable module globals — ``STATE``, ``LOGS``,
``SLUG``, ``_run``, ``_stop``, ``_processes`` and friends — so two repositories could
never run in one process, and every call site's dependencies were invisible. This module
owns those lives as explicit objects:

- ``RepoPaths``     — where a repository's queue, logs, worktrees, config and attempts live
- ``ConfigSnapshot``— one immutable configuration view (taken when a task is claimed)
- ``RunState``      — the daemon's run dict (``_run``) and its lock
- ``EventSink``     — ``log()`` and ``journal()``, per-context paths and locks
- ``ProcessRegistry`` — ``process()`` / ``kill_group()`` / ``shutdown()``, per-context
- ``RuntimeContext``  — the bundle, handed explicitly to new modules

Rule (target architecture, "the one design rule"): no extracted module may read
``STATE``, ``LOGS``, ``SLUG``, ``CONFIG``, ``_run``, ``_stop`` or any other global
from ``swarmd``. State arrives as an argument or it does not arrive. This module never
imports ``swarmd`` in any form (and ``swarmd`` imports this module, so it cannot).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, MutableMapping, MutableSequence, MutableSet
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class RepoPaths:
    """Where one repository's queue, logs, worktrees, config and artifacts live.

    Mirrors what ``use_repo()`` computed from the ``STATE``/``LOGS``/``SLUG`` globals,
    plus the worktree root that used to be derived as ``HERE / "wt" / SLUG`` and the
    per-repository tuned config file.
    """

    repo: Path
    state: Path          # was the STATE global
    logs: Path           # was the LOGS global
    worktrees: Path
    config: Path
    slug: str            # was the SLUG global


def _freeze(value: Any) -> Any:
    """Recursively freeze a config value so no ordinary mutation can reach the snapshot.

    Mappings become read-only mappings with recursively frozen values; lists and tuples
    become immutable sequences; sets become frozensets. Everything else (str, int, float,
    bool, None, Path…) is already immutable and returned as-is. Mapping and set contents
    retain their value semantics; sequences deliberately become tuples, so callers must
    not rely on equality preserving a mutable input's concrete container type.
    """
    if isinstance(value, MutableMapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, MutableSequence):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, MutableSet):
        return frozenset(_freeze(v) for v in value)
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable. Taken once when a task is claimed; an in-flight attempt never sees a
    reload.

    The underlying config is a plain ``dict``; callers can hold the snapshot across a
    reload without their values changing. ``__post_init__`` deep-freezes a defensive copy
    (a frozen dataclass containing a mutable dict is *not* enough): mappings become
    read-only mappings with recursively frozen values, lists/tuples become immutable
    sequences, sets become frozensets. Mutating the original nested input after
    construction can never change the snapshot, and storing the snapshot in another
    container cannot leak it a writable alias.
    """

    values: Mapping[str, Any]
    mtime: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _freeze(self.values))

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        return key in self.values

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


@dataclass
class RunState:
    """The daemon's run dict and its guard lock (``_run`` / ``_run_lock``).

    Owns its own lock and dict; nothing here aliases swarmd module globals, so two
    contexts in one process each observe their own run.
    """

    data: dict = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def __iter__(self):
        return iter(self.data)

    def update(self, *args, **kwargs) -> None:
        self.data.update(*args, **kwargs)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self.data.setdefault(key, default)

    def pop(self, key: str, default: Any = None) -> Any:
        return self.data.pop(key, default)

    def clear(self) -> None:
        self.data.clear()


class EventSink:
    """Per-context ``log()`` and ``journal()`` (swarmd lines 174-190).

    Reproduces the existing output byte-for-byte:

    - ``log(msg, worker="swarm")`` prints ``HH:MM:SS [worker] msg`` and appends the same
      line to ``logs/<today>.log`` under the context's ``_print_lock``.
    - ``journal(event, **kw)`` appends one JSON object per line to ``state/journal.jsonl``
      under the context's ``_journal_lock``, with a ``run_id`` read live from the
      context's RunState — exactly how the daemon's ``_run`` global fed it.

    Both files are opened in append mode per call, as the daemon did, so several contexts
    writing distinct paths need no shared lock.

    Lock scopes match the daemon's globals exactly: ``_print_lock`` guards stdout printing
    only (each log-file append is one atomic ``write`` under no lock), and ``_journal_lock``
    guards each journal append.
    """

    def __init__(self, paths: RepoPaths, run_state: RunState) -> None:
        self._paths = paths
        self._run_state = run_state
        self._print_lock = threading.Lock()
        self._journal_lock = threading.Lock()

    def log(self, msg: str, worker: str = "swarm") -> None:
        line = f"{dt.datetime.now():%H:%M:%S} [{worker}] {msg}"
        with self._print_lock:
            print(line, flush=True)
        with open(self._paths.logs / f"{dt.date.today()}.log", "a") as f:
            f.write(line + "\n")

    def journal(self, event: str, **kw) -> None:
        rec = {"t": time.time(), "iso": dt.datetime.now().isoformat(timespec="seconds"),
               "event": event, "run_id": self._run_state.get("id"), **kw}
        with self._journal_lock:
            with open(self._paths.state / "journal.jsonl", "a") as f:
                f.write(json.dumps(rec) + "\n")


class ProcessRegistry:
    """Per-context ``process()`` / ``kill_group()`` / ``shutdown()`` (swarmd lines
    586-618).

    ``process()`` is a generator-based context manager, exactly like swarmd's: the child
    starts under the registry lock, is yielded, and ``__exit__`` kills the group if still
    running, reaps, and unregisters. ``shutdown()`` sets the same injected stop event that
    ``process()`` checks and kills every registered child — an exact match for the legacy
    module-global ``_stop`` shared by both functions.

    A registry created standalone (tests, later packets) makes its own stop event; a
    context's registry shares the context's ``stop`` event, so a daemon told to stop can
    never leave another daemon's children running in the same process.
    """

    def __init__(self, stop: threading.Event | None = None) -> None:
        self._processes = set()
        self._lock = threading.Lock()
        self._stopping = stop if stop is not None else threading.Event()

    @property
    def processes(self) -> frozenset:
        """Snapshot of live children, for tests and for shutdown bookkeeping."""
        with self._lock:
            return frozenset(self._processes)

    @staticmethod
    def kill_group(p) -> None:
        """Stop a child and everything it started. Never raises: a process that is already
        gone, or that the OS will not let us signal, must not abort a shutdown that is
        killing others."""
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:              # gone (ProcessLookupError) or refused (PermissionError)
            pass

    @contextmanager
    def process(self, cmd, **kwargs):
        """Start ``cmd`` in its own process group, registered with this registry, and yield
        the child — the same generator-based context manager contract as ``swarmd.process``.
        The ``finally`` mirrors the daemon's: kill the group if still running, reap,
        unregister."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("swarm is stopping")
            p = subprocess.Popen(cmd, start_new_session=True, **kwargs)
            self._processes.add(p)
        try:
            yield p
        finally:
            if p.poll() is None:
                self.kill_group(p)
            p.wait()
            with self._lock:
                self._processes.discard(p)

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            for p in self._processes:
                self.kill_group(p)

    def is_stopping(self) -> bool:
        return self._stopping.is_set()


@dataclass
class RuntimeContext:
    """Everything one repository's daemon instance needs, handed explicitly to new
    modules. Replaces module-global ``STATE``/``LOGS``/``SLUG``/``_run``/``_stop``/
    ``_processes`` and the lock globals for new code."""

    paths: RepoPaths
    config: ConfigSnapshot
    events: EventSink          # replaces log() / journal()
    run_state: RunState        # replaces _run / _run_lock
    processes: ProcessRegistry  # replaces _processes / _process_lock
    stop: threading.Event       # replaces _stop
