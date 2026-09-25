"""Validate, atomically save, and lock persistent state."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from .models import Cursor, LastRun, Snapshot, State
from .validation import json_array, json_object, nonempty_string, nonnegative_integer, read_json, string


def load_snapshots(value: object) -> dict[str, Snapshot]:
    snapshots: dict[str, Snapshot] = {}
    for name, raw in json_object(value).items():
        entry = json_object(raw)
        snapshots[name] = Snapshot(
            source=string(entry["source"]),
            values=[string(item) for item in json_array(entry["values"])],
        )
    return snapshots


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json_object(read_json(path))
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("Invalid state version (not overwritten)")
    checks = load_snapshots(raw.get("checks"))
    last_runs: dict[str, LastRun] = {}
    for name, value in json_object(raw.get("last_runs", {})).items():
        entry = json_object(value)
        started_at = entry["started_at"]
        if (isinstance(started_at, bool) or not isinstance(started_at, (int, float))
                or not math.isfinite(started_at) or started_at < 0):
            raise ValueError("Invalid last run timestamp (not overwritten)")
        last_runs[name] = LastRun(string(entry["source"]), float(started_at))
    streams: dict[str, Cursor] = {}
    for key, value in json_object(raw.get("streams", {})).items():
        entry = json_object(value)
        streams[key] = Cursor(nonempty_string(entry["identity"]),
                              nonnegative_integer(entry["offset"]), string(entry["anchor"]))
    observed = load_snapshots(raw.get("observed", {}))
    return State(version, checks, last_runs, streams, observed)


@contextmanager
def state_lock(path: Path) -> Iterator[bool]:
    """OS lock released even after process termination; keep the lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_state(path: Path, state: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            serialized: object = asdict(state)
            json.dump(serialized, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
