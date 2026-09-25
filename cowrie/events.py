"""Extract event values and accumulate shared log observations."""
from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from . import remote
from .models import Check, Config, LastRun, Snapshot, State
from .validation import json_object, parse_json, string


def matching_snapshot(state: State, name: str, signature: str) -> Snapshot | None:
    for snapshot in (state.observed.get(name), state.checks.get(name)):
        if snapshot is not None and snapshot.source == signature:
            return snapshot
    return None


def collect_increment(config: Config, check: Check, state: State) -> State:
    related = [item for item in config.checks
               if item.file == check.file]
    signatures = sorted(fingerprint(config, item) for item in related)
    source: list[str] = [config.ssh.host, check.file, *signatures]
    key = hashlib.sha256(json.dumps(source).encode()).hexdigest()
    previous_cursor = state.streams.get(key)
    # Cursors are shared by extraction conditions, while baselines belong to names.
    # A renamed check must read existing records before establishing its baseline.
    for item in related:
        signature = fingerprint(config, item)
        if matching_snapshot(state, item.name, signature) is None:
            previous_cursor = None
            break
    cursor, data = remote.fetch_increment(config, check.file, previous_cursor)
    candidate = state.copy()
    for item in related:
        field_name = item.json_field
        signature = fingerprint(config, item)
        previous = matching_snapshot(state, item.name, signature)
        old = previous.values if previous is not None else []
        values = sorted(set(old) | set(json_log_values(data, field_name, item.event_ids)))
        candidate.observed[item.name] = Snapshot(signature, values)
    candidate.streams[key] = cursor
    return candidate


def seconds_until_due(check: Check, previous: LastRun | None, source: str, now: float) -> float:
    if previous is None or previous.source != source or now < previous.started_at:
        return 0.0
    return max(0.0, check.interval_seconds - (now - previous.started_at))


def fingerprint(config: Config, check: Check) -> str:
    source = [config.ssh.host, check.file, "json", check.json_field, *sorted(set(check.event_ids))]
    return hashlib.sha256(json.dumps(source, ensure_ascii=True).encode()).hexdigest()


def json_log_values(data: bytes, field_name: str, event_ids: Sequence[str]) -> list[str]:
    """Decode JSONL without conflating escapes, empty values, or field boundaries."""
    values: set[str] = set()
    for number, line in enumerate(data.split(b"\n"), 1):
        if not line.strip():
            continue
        try:
            record = json_object(parse_json(line.decode("utf-8")))
            event_id = string(record.get("eventid"))
            if event_id in event_ids:
                values.add(string(record.get(field_name)))
        except (ValueError, UnicodeError) as exc:
            # Do not put raw log data/credentials in diagnostics; preserve the baseline.
            raise RuntimeError(f"Invalid JSON log record at line {number}") from exc
    return sorted(values)
