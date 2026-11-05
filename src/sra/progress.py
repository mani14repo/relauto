"""Progress of a long-running assessment job, persisted so the UI (and any API replica) can show it live.

The job record is a plain dict, saved through `sink` on every update:
    state       queued | running | done | failed
    phase       what the job is doing now, e.g. "AI-assisted criteria"
    step/total  position within the phase (AI criteria: criteria done / to do)
    current     the item being worked on now, e.g. "SRE-AT-03-C02 Alert routing"
    started_at, updated_at, finished_at   ISO timestamps (UTC)
    eta_s       estimated seconds left in the phase, from the average so far
    log         recent activity lines: {t, level (info|warn|error), msg}
    warnings    count of warn/error lines; error: failure message; summary: outcome when done
Every line also goes to the Python log, so the backend console shows the same activity.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("sra.job")
MAX_LOG = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Progress:
    def __init__(self, sink: Callable[[dict], None] | None = None, job: dict | None = None, label: str = "",
                 history: list[dict] | None = None) -> None:
        self.sink = sink
        self.label = label
        self._lock = threading.Lock()
        self._step_started = 0.0
        self._durations: list[float] = []
        self.state: dict = {"state": "running", "phase": "Starting", "step": 0, "total": 0, "current": "",
                            "started_at": _now(), "updated_at": _now(), "finished_at": None, "eta_s": None,
                            "log": list(history or [])[-MAX_LOG:], "warnings": 0, "error": None, "summary": None, "job": job or {}}
        self._save()

    # -------------------------------------------------------------- reporting
    def phase(self, name: str, total: int = 0, msg: str = "") -> None:
        with self._lock:
            self.state.update(phase=name, step=0, total=total, current="", eta_s=None)
            self._durations = []
        self._line("info", msg or name)

    def start_item(self, step: int, current: str) -> None:
        """Called before working on item `step` (1-based) of the phase."""
        with self._lock:
            self._step_started = time.monotonic()
            self.state.update(step=step - 1, current=current)
            self.state["eta_s"] = self._eta(step - 1)
            self.state["updated_at"] = _now()
        self._save()

    def finish_item(self, step: int, msg: str, level: str = "info") -> None:
        with self._lock:
            if self._step_started:
                self._durations.append(time.monotonic() - self._step_started)
            self.state.update(step=step, current="")
            self.state["eta_s"] = self._eta(step)
        self._line(level, msg)

    def busy(self, current: str) -> None:
        """Show what is happening now without moving the step count (e.g. loading or indexing)."""
        with self._lock:
            self.state.update(current=current, updated_at=_now())
        self._save()

    def info(self, msg: str) -> None:
        self._line("info", msg)

    def warn(self, msg: str) -> None:
        self._line("warn", msg)

    def done(self, summary: dict, msg: str) -> None:
        with self._lock:
            self.state.update(state="done", phase="Finished", current="", eta_s=None, finished_at=_now(), summary=summary)
        self._line("info", msg)

    def failed(self, error: str) -> None:
        with self._lock:
            self.state.update(state="failed", current="", eta_s=None, finished_at=_now(), error=error)
        self._line("error", f"Run failed: {error}")

    # -------------------------------------------------------------- internals
    def _eta(self, done_steps: int) -> int | None:
        left = self.state["total"] - done_steps
        if not self._durations or left <= 0:
            return None
        return int(sum(self._durations) / len(self._durations) * left)

    def _line(self, level: str, msg: str) -> None:
        getattr(log, {"warn": "warning"}.get(level, level))("%s%s", f"[{self.label}] " if self.label else "", msg)
        with self._lock:
            self.state["log"] = (self.state["log"] + [{"t": _now(), "level": level, "msg": msg}])[-MAX_LOG:]
            if level != "info":
                self.state["warnings"] += 1
            self.state["updated_at"] = _now()
        self._save()

    def _save(self) -> None:
        if self.sink is None:
            return
        try:
            with self._lock:
                snapshot = {**self.state, "log": list(self.state["log"])}
            self.sink(snapshot)
        except Exception:  # progress must never break the run itself
            log.exception("could not save job progress")


class NullProgress(Progress):
    """Used when nobody is watching (CLI, tests): still logs, saves nothing."""

    def __init__(self) -> None:
        super().__init__(sink=None)
