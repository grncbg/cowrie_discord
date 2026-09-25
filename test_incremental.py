import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cowrie_monitor as monitor
from cowrie import remote
from incremental_reader import read_increment


class IncrementalTests(unittest.TestCase):
    def test_incomplete_rotated_line_is_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cowrie.json"
            path.write_bytes(b"complete\npartial")
            identity, offset, anchor, _, _ = read_increment(path, "", 0, "")
            rotated = path.with_name("cowrie.json.1")
            path.rename(rotated)
            path.write_bytes(b"new\n")
            with self.assertRaisesRegex(RuntimeError, "incomplete final line"):
                read_increment(path, identity, offset, anchor)
            with rotated.open("ab") as stream:
                stream.write(b"\n")
            _, _, _, data, _ = read_increment(path, identity, offset, anchor)
            self.assertEqual(data, b"partial\nnew\n")

    def test_renamed_check_rebuilds_baseline(self) -> None:
        for notify_initial in (False, True):
            with self.subTest(notify_initial=notify_initial), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                path = base / "cowrie.json"
                record = b'{"eventid":"command","input":"existing"}\n'
                path.write_bytes(record)
                checks = [monitor.Check(name, str(path), "input", ("command",),
                                        interval_seconds=0) for name in ("unchanged", "before")]
                config = monitor.Config(monitor.SSHConfig("host"), checks)
                reads: list[int] = []
                sent: list[str] = []

                def fetch(config: monitor.Config, file: str,
                          cursor: monitor.Cursor | None) -> tuple[monitor.Cursor, bytes]:
                    identity, offset, anchor, data, _ = read_increment(
                        Path(file), cursor.identity if cursor else "",
                        cursor.offset if cursor else 0, cursor.anchor if cursor else "")
                    reads.append(len(data))
                    return monitor.Cursor(identity, offset, anchor), data

                def send(url: str, content: str, *, mention_everyone: bool = False) -> None:
                    sent.append(content)

                environment: dict[str, str] = {"DISCORD_WEBHOOK_URL": "https://example.invalid"}
                with patch.object(remote, "fetch_increment", new=fetch), \
                        patch.object(monitor, "send_discord", new=send), \
                        patch.dict(os.environ, environment):
                    self.assertEqual(monitor.run(config, base), 0)
                    checks[1].name = "after"
                    config.notify_initial = notify_initial
                    self.assertEqual(monitor.run(config, base), 0)
                    state = monitor.load_state(base / config.state_file)
                    expected_values: list[str] = ["existing"]
                    expected_reads: list[int] = [len(record), len(record)]
                    expected_sent = (list(monitor.messages("after", expected_values))
                                     if notify_initial else list[str]())
                    self.assertEqual(state.checks["after"].values, expected_values)
                    self.assertEqual(reads, expected_reads)
                    self.assertEqual(sent, expected_sent)
                    sent.clear()
                    with path.open("ab") as stream:
                        stream.write(record)
                    self.assertEqual(monitor.run(config, base), 0)
                    self.assertEqual(sent, list[str]())
                    self.assertEqual(reads[-1], len(record))

    def test_multiple_rotations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cowrie.json"
            path.write_bytes(b"baseline\n")
            identity, offset, anchor, _, _ = read_increment(path, "", 0, "")
            with path.open("ab") as stream:
                stream.write(b"tail\n")
            oldest = path.with_name("cowrie.json.2")
            path.rename(oldest)
            os.utime(oldest, (1000, 1000))
            middle = path.with_name("cowrie.json.1")
            middle.write_bytes(b"middle\n")
            os.utime(middle, (2000, 2000))
            path.write_bytes(b"current\n")
            _, _, _, data, _ = read_increment(path, identity, offset, anchor)
            self.assertEqual(data, b"tail\nmiddle\ncurrent\n")

    def test_append_partial_rotation_and_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cowrie.json"
            path.write_bytes(b"one\npar")
            identity, offset, anchor, data, _ = read_increment(path, "", 0, "")
            self.assertEqual(data, b"one\n")
            with path.open("ab") as stream:
                stream.write(b"tial\n")
            identity, offset, anchor, data, _ = read_increment(path, identity, offset, anchor)
            self.assertEqual(data, b"partial\n")
            with path.open("ab") as stream:
                stream.write(b"old tail\n")
            path.rename(path.with_name("cowrie.json.1"))
            path.write_bytes(b"new\n")
            identity, offset, anchor, data, _ = read_increment(path, identity, offset, anchor)
            self.assertEqual(data, b"old tail\nnew\n")
            path.write_bytes(b"replacement longer than previous\n")
            identity, offset, anchor, data, warnings = read_increment(path, identity, offset, anchor)
            self.assertEqual(data, b"replacement longer than previous\n")
            self.assertTrue(warnings)
            path.unlink()
            # Ensure a distinct inode even on filesystems that immediately reuse it.
            replacement = path.with_name("replacement")
            replacement.write_bytes(b"next\n")
            replacement.rename(path)
            if identity != f"{path.stat().st_dev}:{path.stat().st_ino}":
                with self.assertRaises(RuntimeError):
                    read_increment(path, identity, offset, anchor)

    def test_shared_reads_intervals_retry_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "cowrie.json"
            checks = [monitor.Check("fast", str(path), interval_seconds=0,
                                    json_field="input", event_ids=("command",)),
                      monitor.Check("slow", str(path), interval_seconds=600,
                                    json_field="input", event_ids=("command",))]
            config = monitor.Config(monitor.SSHConfig("host"), checks)
            reads: list[int] = []
            sent: list[str] = []
            fail = False

            def fetch(config: monitor.Config, file: str,
                      cursor: monitor.Cursor | None) -> tuple[monitor.Cursor, bytes]:
                identity, offset, anchor, data, _ = read_increment(
                    Path(file), cursor.identity if cursor else "",
                    cursor.offset if cursor else 0, cursor.anchor if cursor else "")
                reads.append(len(data))
                return monitor.Cursor(identity, offset, anchor), data

            def send(url: str, content: str, *, mention_everyone: bool = False) -> None:
                if fail:
                    raise RuntimeError("delivery failed")
                sent.append(content)

            def append(value: str) -> None:
                record: dict[str, str] = {"eventid": "command", "input": value}
                with path.open("ab") as stream:
                    stream.write((json.dumps(record) + "\n").encode())

            environment: dict[str, str] = {"DISCORD_WEBHOOK_URL": "https://example.invalid"}
            with patch.object(remote, "fetch_increment", new=fetch), \
                    patch.object(monitor, "send_discord", new=send), \
                    patch.dict(os.environ, environment), \
                    patch("cowrie_monitor.time.time", return_value=1000):
                append("old")
                self.assertEqual(monitor.run(config, base), 0)
                self.assertEqual(len(reads), 1)
                append("new")
                fail = True
                self.assertEqual(monitor.run(config, base), 1)
                fail = False
                self.assertEqual(monitor.run(config, base), 0)
                self.assertEqual(reads[-1], 0)
                self.assertEqual(len(sent), 1)
                checks[1].interval_seconds = 0
                self.assertEqual(monitor.run(config, base), 0)
                self.assertIn("slow", sent[-1])
                self.assertIn("+ new", sent[-1])
                state = monitor.load_state(base / config.state_file)
                before = (base / config.state_file).read_bytes()
                append("dry")
                self.assertEqual(monitor.run(config, base, dry_run=True), 0)
                self.assertEqual((base / config.state_file).read_bytes(), before)
                self.assertTrue(state.streams)
                self.assertEqual(monitor.run(config, base), 0)
                self.assertIn("+ dry", sent[-1])
                previous = monitor.load_state(base / config.state_file)
                with path.open("ab") as stream:
                    stream.write(b"invalid json\n")
                self.assertEqual(monitor.run(config, base), 1)
                after = monitor.load_state(base / config.state_file)
                self.assertEqual(after.streams, previous.streams)
                self.assertEqual(after.observed, previous.observed)


if __name__ == "__main__":
    unittest.main()
