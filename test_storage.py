import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cowrie.models import Cursor, LastRun, Snapshot, State
from cowrie.storage import load_state, save_state


class StorageTests(unittest.TestCase):
    def test_round_trip_all_state_components(self) -> None:
        state = State(
            checks={"commands": Snapshot("source", ["", "日本語\n"])},
            last_runs={"commands": LastRun("source", 123.5)},
            streams={"stream": Cursor("device:inode", 42, "anchor")},
            observed={"commands": Snapshot("source", ["", "日本語\n", "new"])},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, state)
            self.assertEqual(load_state(path), state)

    def test_failed_replace_preserves_state_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_state(path, State())
            before = path.read_bytes()

            def fail(source: str, destination: Path) -> None:
                raise OSError("disk unavailable")

            with patch.object(os, "replace", new=fail), self.assertRaises(OSError):
                save_state(path, State(checks={"commands": Snapshot("source", ["new"])}))
            self.assertEqual(path.read_bytes(), before)
            remaining: list[Path] = list(path.parent.iterdir())
            self.assertEqual(remaining, list[Path]([path]))
