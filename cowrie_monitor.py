"""Compare remote grep results and notify Discord. Python 3.10+, stdlib only."""

import argparse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from email.message import Message
import hashlib
from http.client import HTTPResponse
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import TypeGuard
import urllib.error
import urllib.parse
import urllib.request
from urllib.response import addinfourl


LOG = logging.getLogger("cowrie_monitor")


@dataclass
class SSHConfig:
    host: str
    executable: str = "ssh"
    timeout_seconds: float = 90


@dataclass
class Check:
    name: str
    file: str
    regex: str
    notify_removed: bool = True
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


def is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, dict)


def is_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, list)


def json_object(value: object) -> dict[str, object]:
    if not is_mapping(value):
        raise ValueError("Expected a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("Expected string keys")
        result[key] = item
    return result


def json_array(value: object) -> list[object]:
    if not is_sequence(value):
        raise ValueError("Expected a JSON array")
    return list(value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a string")
    return value


def nonempty_string(value: object) -> str:
    result = string(value)
    if not result or "\x00" in result:
        raise ValueError("Expected a nonempty string without NUL")
    return result


def boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Expected a boolean")
    return value


def nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("interval_seconds must be a nonnegative integer")
    return value


def parse_json(data: str | bytes) -> object:
    value: object = json.loads(data)
    return value


def read_json(path: Path) -> object:
    with path.open(encoding="utf-8-sig") as stream:
        return parse_json(stream.read())


def resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


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
        item = json_object(raw_check)
        check = Check(
            name=nonempty_string(item["name"]), file=nonempty_string(item["file"]),
            regex=nonempty_string(item["regex"]),
            notify_removed=boolean(item.get("notify_removed", True)),
            interval_seconds=nonnegative_integer(item.get("interval_seconds", 300)),
            mention_everyone=boolean(item.get("mention_everyone", False)),
        )
        if len(check.name) > 120 or "\n" in check.name or "\r" in check.name:
            raise ValueError("check name must be a single line of at most 120 characters")
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


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    raw = json_object(read_json(path))
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("Invalid state version (not overwritten)")
    checks: dict[str, Snapshot] = {}
    for name, value in json_object(raw.get("checks")).items():
        entry = json_object(value)
        checks[name] = Snapshot(
            source=string(entry["source"]),
            values=[string(item) for item in json_array(entry["values"])],
        )
    last_runs: dict[str, LastRun] = {}
    for name, value in json_object(raw.get("last_runs", {})).items():
        entry = json_object(value)
        started_at = entry["started_at"]
        if (isinstance(started_at, bool) or not isinstance(started_at, (int, float))
                or not math.isfinite(started_at) or started_at < 0):
            raise ValueError("Invalid last run timestamp (not overwritten)")
        last_runs[name] = LastRun(string(entry["source"]), float(started_at))
    return State(version, checks, last_runs)


def seconds_until_due(check: Check, previous: LastRun | None, source: str, now: float) -> float:
    if previous is None or previous.source != source or now < previous.started_at:
        return 0.0
    return max(0.0, check.interval_seconds - (now - previous.started_at))


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


def fingerprint(config: Config, check: Check) -> str:
    source = [config.ssh.host, check.file, check.regex]
    return hashlib.sha256(json.dumps(source, ensure_ascii=True).encode()).hexdigest()


def fetch(config: Config, check: Check) -> list[str]:
    # GNU grep PCRE supports lookbehind. No shell on the local host.
    # Do not pipe grep into sort: a pipeline would hide read/regex failures.
    command = "LC_ALL=C grep -a -oP -e {} -- {}".format(
        shlex.quote(check.regex), shlex.quote(check.file)
    )
    ssh = config.ssh
    result = subprocess.run(
        [ssh.executable, "-T", "-o", "BatchMode=yes", "-o",
         "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", "-o",
         "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", ssh.host, command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=ssh.timeout_seconds, check=False,
    )
    if result.returncode not in (0, 1):
        # stderr may contain terminal escapes or log data; quote and bound it.
        detail = repr(result.stderr.decode("utf-8", errors="replace")[:1000])
        raise RuntimeError(f"SSH/grep failed (exit {result.returncode}): {detail}")
    # surrogateescape preserves distinct non-UTF8 bytes in the saved snapshot.
    return sorted(set(result.stdout.decode("utf-8", errors="surrogateescape").split("\n")) - {""})


def messages(name: str, added: Sequence[str], removed: Sequence[str]) -> Iterator[str]:
    header = f"{name}\n"
    text = "\n".join([*("+ " + line for line in added), *("- " + line for line in removed)])
    # Render malformed bytes safely and prevent log content from closing code fences.
    text = text.encode("utf-8", errors="backslashreplace").decode("utf-8").replace("`", "ˋ")
    # 750 code points stay under 2000 UTF-16 units even with astral characters.
    for start in range(0, len(text), 750):
        yield header + "```diff\n" + text[start:start + 750] + "\n```"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: object, code: int,
        msg: str, headers: Message, newurl: str,
    ) -> None:
        return None


def post_discord(request: urllib.request.Request) -> None:
    opener = urllib.request.build_opener(NoRedirect())
    raw: object = opener.open(request, timeout=30)
    if not isinstance(raw, (HTTPResponse, addinfourl)):
        raise RuntimeError("Unexpected HTTP response type")
    with raw as response:
        if response.status != 200:
            raise RuntimeError(f"Discord returned HTTP {response.status}")
        response.read()


def send_discord(url: str, content: str, *, mention_everyone: bool = False) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("Webhook URL must be HTTPS")
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    target = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    # Only our explicit prefix may ping. Neutralize mentions in names/log data,
    # including fragments split across message boundaries.
    safe_content = content.replace("@", "@\u200b")
    allowed: list[str] = ["everyone"] if mention_everyone else []
    body: dict[str, object] = {
        "content": ("@everyone\n" if mention_everyone else "") + safe_content,
        "allowed_mentions": {"parse": allowed},
    }
    payload = json.dumps(body).encode()
    request = urllib.request.Request(target, data=payload, headers={
        "Content-Type": "application/json", "User-Agent": "CowrieMonitor/1.0"
    }, method="POST")
    for attempt in range(3):
        try:
            post_discord(request)
            return
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry = (1.0, 2.0, 4.0)[attempt]
            if status == 429:
                try:
                    retry_data = json_object(parse_json(exc.read())).get("retry_after", retry)
                    if isinstance(retry_data, (str, int, float)) and not isinstance(retry_data, bool):
                        retry = float(retry_data)
                except (ValueError, TypeError):
                    pass
            exc.close()
            if attempt == 2 or (status != 429 and status < 500) or not 0 <= retry <= 30:
                raise RuntimeError(f"Discord returned HTTP {status}; snapshot not updated") from None
            time.sleep(retry)
        except (urllib.error.URLError, OSError):
            # Never log exception text: it can contain the secret webhook URL.
            raise RuntimeError("Discord connection failed; snapshot not updated") from None


def run(config: Config, base: Path, dry_run: bool = False) -> int:
    path = resolve(base, config.state_file)
    with state_lock(path.with_name(path.name + ".lock")) as acquired:
        if not acquired:
            LOG.info("Another execution holds the state lock; skipped")
            return 0
        state = load_state(path)
        failed = False
        for check in config.checks:
            name = check.name
            try:
                signature = fingerprint(config, check)
                started_at = time.time()
                remaining = seconds_until_due(check, state.last_runs.get(name), signature, started_at)
                if remaining > 0:
                    LOG.info("%s: skipped (next eligible in %.1f seconds)", name, remaining)
                    continue
                if not dry_run:
                    # Record attempts independently of successful notification snapshots.
                    # Persist before I/O so crashes and errors also respect the interval.
                    candidate = State(state.version, state.checks.copy(), state.last_runs.copy())
                    candidate.last_runs[name] = LastRun(signature, started_at)
                    save_state(path, candidate)
                    state = candidate
                current = fetch(config, check)
                previous = state.checks.get(name)
                initial = previous is None or previous.source != signature
                old: set[str] = set()
                if previous is not None and not initial:
                    old = set(previous.values)
                added = sorted(set(current) - old)
                removed = sorted(old - set(current))
                LOG.info("%s: %d unique, +%d -%d%s", name, len(current), len(added),
                         len(removed), " (new baseline)" if initial else "")
                if dry_run:
                    continue
                if not initial or config.notify_initial:
                    notify_removed = removed if check.notify_removed else []
                    if added or notify_removed:
                        url = os.environ.get(config.webhook_env)
                        if not url:
                            raise RuntimeError("Webhook environment variable is not set")
                        for index, content in enumerate(messages(name, added, notify_removed)):
                            send_discord(url, content, mention_everyone=check.mention_everyone and index == 0)
                candidate = State(state.version, state.checks.copy(), state.last_runs.copy())
                candidate.checks[name] = Snapshot(signature, current)
                save_state(path, candidate)
                state = candidate
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
                # TimeoutExpired includes the command but never the webhook secret.
                LOG.error("%s: %s", name, exc)
                failed = True
        return 1 if failed else 0


class Arguments(argparse.Namespace):
    config: Path
    dry_run: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Fetch/compare only; no notification or snapshot changes")
    args = Arguments()
    parser.parse_args(namespace=args)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        path = args.config.resolve()
        config = load_config(path)
        log_path = resolve(path.parent, config.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        return run(config, path.parent, args.dry_run)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        LOG.error("Cannot run monitor: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
