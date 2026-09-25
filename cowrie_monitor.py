"""Read incremental Cowrie JSON events and notify Discord. Python 3.10+, stdlib only."""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

# Preserve existing imports; new components should import their owning module.
from cowrie.models import (
    SSHConfig as SSHConfig,
    Check as Check,
    Config as Config,
    Snapshot as Snapshot,
    LastRun as LastRun,
    State as State,
    Cursor as Cursor,
)
from cowrie.configuration import resolve as resolve, load_config as load_config
from cowrie.storage import (
    load_state as load_state,
    save_state as save_state,
    state_lock as state_lock,
)
from cowrie.events import (
    collect_increment as collect_increment,
    seconds_until_due as seconds_until_due,
    fingerprint as fingerprint,
    json_log_values as json_log_values,
)
from cowrie.discord import (
    messages as messages,
    NoRedirect as NoRedirect,
    post_discord as post_discord,
    send_discord as send_discord,
)
from cowrie.remote import fetch_increment as fetch_increment
from cowrie.validation import (
    is_mapping as is_mapping,
    is_sequence as is_sequence,
    json_object as json_object,
    json_array as json_array,
    string as string,
    nonempty_string as nonempty_string,
    boolean as boolean,
    nonnegative_integer as nonnegative_integer,
    parse_json as parse_json,
    read_json as read_json,
)

LOG = logging.getLogger("cowrie_monitor")


def notify_added(config: Config, check: Check, added: list[str]) -> None:
    if not added:
        return
    url = os.environ.get(config.webhook_env)
    if not url:
        raise RuntimeError("Webhook environment variable is not set")
    for index, content in enumerate(messages(check.name, added)):
        send_discord(url, content, mention_everyone=check.mention_everyone and index == 0)


def run(config: Config, base: Path, dry_run: bool = False) -> int:
    path = resolve(base, config.state_file)
    with state_lock(path.with_name(path.name + ".lock")) as acquired:
        if not acquired:
            LOG.info("Another execution holds the state lock; skipped")
            return 0
        state = load_state(path)
        failed = False
        collected: dict[str, Exception | None] = {}
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
                    candidate = state.copy()
                    candidate.last_runs[name] = LastRun(signature, started_at)
                    save_state(path, candidate)
                    state = candidate
                if check.file not in collected:
                    try:
                        candidate = collect_increment(config, check, state)
                        if not dry_run:
                            save_state(path, candidate)
                        state = candidate
                        collected[check.file] = None
                    except (OSError, ValueError, KeyError, TypeError, RuntimeError,
                            subprocess.SubprocessError) as exc:
                        collected[check.file] = exc
                error = collected[check.file]
                if error is not None:
                    raise error
                current = state.observed[name].values
                previous = state.checks.get(name)
                initial = previous is None or previous.source != signature
                old: set[str] = set()
                if previous is not None and not initial:
                    old = set(previous.values)
                added = sorted(set(current) - old)
                LOG.info("%s: %d unique, +%d%s", name, len(current), len(added), " (new baseline)" if initial else "")
                if dry_run:
                    continue
                if not initial or config.notify_initial:
                    notify_added(config, check, added)
                candidate = state.copy()
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
