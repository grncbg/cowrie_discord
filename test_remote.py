import base64
import json
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch

from cowrie.models import Config, Cursor, SSHConfig
from cowrie.remote import fetch_increment


class RemoteTests(unittest.TestCase):
    def test_reader_location_arguments_and_response(self) -> None:
        data = b'{"eventid":"command","input":"hello"}\n'
        response: dict[str, object] = {
            "identity": "new", "offset": 42, "anchor": "digest",
            "warnings": [], "data": base64.b64encode(data).decode("ascii"),
        }
        commands: list[list[str]] = []

        def execute(
            args: list[str], *, stdin: int, stdout: int, stderr: int,
            timeout: float, check: bool,
        ) -> subprocess.CompletedProcess[bytes]:
            commands.append(args)
            self.assertEqual(timeout, 15)
            self.assertEqual(stdin, subprocess.DEVNULL)
            self.assertFalse(check)
            return subprocess.CompletedProcess(args, 0, json.dumps(response).encode(), b"")

        config = Config(SSHConfig("host", timeout_seconds=15), [])
        filename = "/tmp/log with 'quotes'.json"
        with patch.object(subprocess, "run", new=execute):
            cursor, received = fetch_increment(config, filename, Cursor("old", 10, "anchor"))
        self.assertEqual(cursor, Cursor("new", 42, "digest"))
        self.assertEqual(received, data)
        self.assertEqual(commands[0][-2], "host")
        command = shlex.split(commands[0][-1])
        self.assertEqual(command[:2], list[str](["python3", "-c"]))
        self.assertEqual(command[2], Path(__file__).with_name("incremental_reader.py").read_text(encoding="utf-8"))
        self.assertEqual(command[3:], list[str]([filename, "old", "10", "anchor"]))
        compile(command[2], "<remote reader>", "exec")
