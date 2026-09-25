"""Load monitor settings and resolve configuration-relative paths."""
from __future__ import annotations

from pathlib import Path

from .models import Check, Config, SSHConfig
from .validation import boolean, json_array, json_object, nonempty_string, nonnegative_integer, read_json


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def parse_check(value: object) -> Check:
    item = json_object(value)
    check = Check(
        name=nonempty_string(item["name"]),
        file=nonempty_string(item["file"]),
        json_field=nonempty_string(item["json_field"]),
        event_ids=tuple(nonempty_string(value) for value in json_array(item["event_ids"])),
        interval_seconds=nonnegative_integer(item.get("interval_seconds", 300)),
        mention_everyone=boolean(item.get("mention_everyone", False)),
    )
    if not check.event_ids:
        raise ValueError("event_ids must not be empty")
    if len(check.name) > 120 or "\n" in check.name or "\r" in check.name:
        raise ValueError("check name must be a single line of at most 120 characters")
    return check


def load_config(path: Path) -> Config:
    config = json_object(read_json(path))
    ssh = json_object(config["ssh"])
    host = nonempty_string(ssh["host"])
    if host.startswith("-"):
        raise ValueError("ssh.host must be a nonempty SSH host/alias")
    timeout = ssh.get("timeout_seconds", 90)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("ssh.timeout_seconds must be positive")
    raw_checks = json_array(config["checks"])
    if not raw_checks:
        raise ValueError("checks must be a nonempty array")
    names: set[str] = set()
    checks: list[Check] = []
    for raw_check in raw_checks:
        check = parse_check(raw_check)
        if check.name in names:
            raise ValueError("Duplicate check name")
        names.add(check.name)
        checks.append(check)
    return Config(
        ssh=SSHConfig(host, nonempty_string(ssh.get("executable", "ssh")), float(timeout)),
        checks=checks,
        state_file=nonempty_string(config.get("state_file", "state/snapshots.json")),
        log_file=nonempty_string(config.get("log_file", "logs/monitor.log")),
        webhook_env=nonempty_string(config.get("webhook_env", "DISCORD_WEBHOOK_URL")),
        notify_initial=boolean(config.get("notify_initial", False)),
    )
