"""SwarmApplication: the public face of one repository's swarm.

The bridge and the CLI ask it for paths, records and operations instead of reaching into
the daemon's module state. It holds a handle to the daemon it fronts (passed in, never
imported here) and reads that daemon's current repository paths when asked, so a
repository selected with `use_repo` is always the one answered for.
"""
from __future__ import annotations

import json
from pathlib import Path


class SwarmApplication:
    def __init__(self, daemon, c=None):
        self._daemon = daemon
        self._c = c          # the config for_repo() selected; operations act on it, never re-read

    @classmethod
    def for_repo(cls, repo, *, daemon):
        """Select `repo` on the daemon (its queue, state, logs and config) and front it.

        An untuned repository falls back to the default config, which names some other
        repository, so the config is pointed at `repo` before anything is selected — the same
        override the bridge's config_for applies."""
        c = daemon.load_cfg(repo)
        if repo:
            resolved = str(Path(repo).expanduser().resolve())
            if c.get("repo") != resolved:
                c = dict(c, repo=resolved)
        daemon.use_repo(c)
        return cls(daemon, c)

    def _config(self):
        if self._c is None:
            raise RuntimeError("this SwarmApplication fronts paths only; build it with for_repo() "
                               "to run operations on a repository")
        return self._c

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
    # Starting and stopping a daemon are not here on purpose: the lifecycle takes the
    # repository's lock, installs signal handlers and runs for the whole shift, so it must own
    # its process. `swarmd.py run|grind|stop` (and the bridge's grind-cmd/stop) are its entry
    # points.

    def status(self):
        """The selected repository's last run record and now-line, read without writing.

        The daemon's own run_status() also rewrites run.json from this process's run state,
        which from any process but the daemon's would clobber the live record."""
        try:
            run = json.loads((self.state_dir / "run.json").read_text())
        except (OSError, ValueError):
            run = None
        return {"run": run, "now": self._daemon.read_now()}

    def add_task(self, title, detail="", kind="feature", **fields):
        return self._daemon.queue_for(self._config()).add(title, detail, kind, **fields)

    def report(self, hours=24):
        return self._daemon.build_report(self._config(), hours)
