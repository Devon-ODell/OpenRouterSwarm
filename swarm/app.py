"""SwarmApplication: the public face of one repository's swarm.

The bridge and the CLI ask it for paths, records and operations instead of reaching into
the daemon's module state. It holds a handle to the daemon it fronts (passed in, never
imported here) and reads that daemon's current repository paths when asked, so a
repository selected with `use_repo` is always the one answered for.
"""
from __future__ import annotations

from pathlib import Path


class SwarmApplication:
    def __init__(self, daemon):
        self._daemon = daemon

    @classmethod
    def for_repo(cls, repo, *, daemon):
        """Select `repo` on the daemon (its queue, state, logs and config) and front it."""
        daemon.use_repo(daemon.load_cfg(repo))
        return cls(daemon)

    # -- paths ----------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self._daemon.STATE

    @property
    def logs_dir(self) -> Path:
        return self._daemon.LOGS

    @property
    def config_path(self) -> Path:
        return self._daemon.CONFIG

    def log_path(self, day) -> Path:
        """The daemon's human log for one day (a date or an ISO date string)."""
        return self.logs_dir / f"{day}.log"

    # -- records --------------------------------------------------------
    def read_jsonl(self, path):
        """Rows of a JSONL state file, skipping torn lines, as the daemon reads them."""
        return self._daemon._read(path)

    def journal(self):
        return self.read_jsonl(self.state_dir / "journal.jsonl")

    def done(self):
        return self.read_jsonl(self.state_dir / "done.jsonl")

    # -- operations -----------------------------------------------------
    def status(self, workers=(), c=None):
        return self._daemon.run_status(list(workers), c if c is not None else self._daemon.cfg())

    def add_task(self, title, detail="", kind="feature", **fields):
        c = self._daemon.cfg()
        return self._daemon.queue_for(c).add(title, detail, kind, **fields)

    def report(self, hours=24):
        return self._daemon.build_report(self._daemon.cfg(), hours)

    def start(self, hours=None, max_tasks=None, awake=False):
        return self._daemon.start(self._daemon.cfg(), hours, max_tasks, awake)

    def stop(self, drain=False):
        return self._daemon.cmd_stop(type("Args", (), {"repo": None, "drain": drain})())
