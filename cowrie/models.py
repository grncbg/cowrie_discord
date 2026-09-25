"""Configuration and persisted monitoring state."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SSHConfig:
    host: str
    executable: str = "ssh"
    timeout_seconds: float = 90


@dataclass
class Check:
    name: str
    file: str
    json_field: str
    event_ids: tuple[str, ...]
    interval_seconds: int = 300
    mention_everyone: bool = False


@dataclass
class Config:
    ssh: SSHConfig
    checks: list[Check]
    state_file: str = "state/snapshots.json"
    log_file: str = "logs/monitor.log"
    webhook_env: str = "DISCORD_WEBHOOK_URL"
    notify_initial: bool = False


@dataclass
class Snapshot:
    source: str
    values: list[str]


@dataclass
class LastRun:
    source: str
    started_at: float


@dataclass
class State:
    version: int = 1
    checks: dict[str, Snapshot] = field(default_factory=dict)
    last_runs: dict[str, LastRun] = field(default_factory=dict)
    streams: dict[str, Cursor] = field(default_factory=dict)
    observed: dict[str, Snapshot] = field(default_factory=dict)

    def copy(self) -> State:
        """Copy indexes for a candidate update; stored records are never mutated."""
        return State(
            version=self.version,
            checks=self.checks.copy(),
            last_runs=self.last_runs.copy(),
            streams=self.streams.copy(),
            observed=self.observed.copy(),
        )


@dataclass
class Cursor:
    identity: str
    offset: int
    anchor: str
