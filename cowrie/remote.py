"""Retrieve incremental log bytes through SSH."""
from __future__ import annotations

import base64
import logging
from pathlib import Path
import shlex
import subprocess

from .models import Config, Cursor
from .validation import json_array, json_object, nonempty_string, nonnegative_integer, parse_json, string

LOG = logging.getLogger("cowrie_monitor")


def fetch_increment(config: Config, file: str, cursor: Cursor | None) -> tuple[Cursor, bytes]:
    reader = (Path(__file__).resolve().parent.parent / "incremental_reader.py").read_text(encoding="utf-8")
    command = "python3 -c " + shlex.quote(reader) + " " + " ".join(shlex.quote(arg) for arg in (
        file, cursor.identity if cursor else "", str(cursor.offset if cursor else 0),
        cursor.anchor if cursor else "",
    ))
    result = subprocess.run(
        [config.ssh.executable, "-T", "-o", "BatchMode=yes", "-o",
         "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15", "-o",
         "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", config.ssh.host, command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=config.ssh.timeout_seconds, check=False,
    )
    if result.returncode:
        detail = repr(result.stderr.decode("utf-8", errors="replace")[-1000:])
        raise RuntimeError(f"Incremental SSH read failed: {detail}")
    response = json_object(parse_json(result.stdout))
    updated = Cursor(nonempty_string(response["identity"]),
                     nonnegative_integer(response["offset"]), string(response["anchor"]))
    for warning in json_array(response["warnings"]):
        LOG.warning("%s", string(warning))
    return updated, base64.b64decode(string(response["data"]), validate=True)
