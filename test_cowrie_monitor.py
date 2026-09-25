from collections.abc import Callable
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cowrie_monitor as monitor
from cowrie import remote


def incremental_stub(fetch: Callable[[monitor.Config, monitor.Check], list[str]]) -> Callable[
    [monitor.Config, str, monitor.Cursor | None], tuple[monitor.Cursor, bytes]
]:
    def read(config: monitor.Config, file: str, cursor: monitor.Cursor | None) -> tuple[monitor.Cursor, bytes]:
        check = next(item for item in config.checks if item.file == file)
        records = [{"eventid": check.event_ids[0], check.json_field: value}
                   for value in fetch(config, check)]
        data = "\n".join(json.dumps(record) for record in records).encode()
        return monitor.Cursor("test", len(data), ""), data
    return read


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.now = 1_000_000.0

        def clock() -> float:
            return self.now

        clock_patch = patch.object(time, "time", new=clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.config = monitor.Config(
            ssh=monitor.SSHConfig("cowrie-host"), state_file="state.json",
            checks=[monitor.Check("commands", "/tmp/cowrie.json", "input", ("command",))],
        )

    def snapshot(self) -> monitor.State:
        return monitor.load_state(self.base / "state.json")

    def execute(
        self, values: list[str], send_error: Exception | None = None, dry_run: bool = False,
        advance_seconds: float = 300,
    ) -> tuple[int, list[str]]:
        sent: list[str] = []
        self.now += advance_seconds

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            return values

        def send(url: str, content: str, *, mention_everyone: bool = False) -> None:
            sent.append(content)
            if send_error is not None:
                raise send_error

        environment = dict(os.environ, DISCORD_WEBHOOK_URL="https://example.invalid/secret")
        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)), \
                patch.object(monitor, "send_discord", new=send), \
                patch.object(os, "environ", new=environment):
            result = monitor.run(self.config, self.base, dry_run)
        return result, sent

    def test_baseline_unchanged_and_added(self) -> None:
        self.assertEqual(self.execute(["CMD a"])[1], list[str]())
        self.assertEqual(self.execute(["CMD a"])[1], list[str]())
        result, sent = self.execute(["CMD b"])
        self.assertEqual(result, 0)
        self.assertIn("+ CMD b", sent[0])
        self.assertNotIn("- CMD a", sent[0])

    def test_failed_delivery_preserves_snapshot_and_retries(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.assertEqual(self.execute(["CMD b"], RuntimeError("HTTP 500"))[0], 1)
        self.assertEqual(self.snapshot().checks, previous.checks)
        self.assertEqual(len(self.execute(["CMD b"])[1]), 1)

    def test_failed_observation_save_does_not_notify_or_advance_cursor(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        original_save = monitor.save_state
        saves = 0

        def save(path: Path, state: monitor.State) -> None:
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("observation save failed")
            original_save(path, state)

        with patch.object(monitor, "save_state", new=save):
            result, sent = self.execute(["CMD b"])
        self.assertEqual(result, 1)
        self.assertEqual(sent, list[str]())
        after = self.snapshot()
        self.assertEqual(after.checks, previous.checks)
        self.assertEqual(after.observed, previous.observed)
        self.assertEqual(after.streams, previous.streams)
        self.assertNotEqual(after.last_runs, previous.last_runs)
        self.assertIn("+ CMD b", self.execute(["CMD b"])[1][0])

    def test_fetch_failure_is_not_empty_snapshot(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.now += 300

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            raise RuntimeError("read failed")

        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)):
            self.assertEqual(monitor.run(self.config, self.base), 1)
        self.assertEqual(self.snapshot().checks, previous.checks)

    def test_initial_notification_and_cumulative_values(self) -> None:
        self.config.notify_initial = True
        self.assertEqual(len(self.execute(["CMD a"])[1]), 1)
        self.assertEqual(self.execute([])[1], list[str]())
        self.assertEqual(self.snapshot().checks["commands"].values, list[str](["CMD a"]))

    def test_source_change_rebaselines_and_dry_run_does_not_update(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.execute(["CMD b"], dry_run=True)
        self.assertEqual(self.snapshot(), previous)
        self.config.checks[0].json_field = "message"
        self.assertEqual(self.execute(["Saved file"])[1], list[str]())

    def test_independent_checks_continue_after_failure(self) -> None:
        second = copy.deepcopy(self.config.checks[0])
        second.name = "second"
        second.file = "/tmp/second.json"
        self.config.checks.append(second)

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            if check.name == "commands":
                raise RuntimeError("fail")
            return ["CMD b"]

        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)):
            self.assertEqual(monitor.run(self.config, self.base), 1)
        self.assertNotIn("commands", self.snapshot().checks)
        self.assertIn("second", self.snapshot().checks)


    def test_json_fingerprint_rebaselines(self) -> None:
        self.execute(["old"])
        check = self.config.checks[0]
        original = monitor.fingerprint(self.config, check)
        check.json_field = "username"
        check.event_ids = ("failed", "success")
        signature = monitor.fingerprint(self.config, check)
        self.assertNotEqual(original, signature)
        self.assertEqual(self.execute(["new"])[1], list[str]())
        check.event_ids = ("success", "failed", "success")
        self.assertEqual(signature, monitor.fingerprint(self.config, check))
        check.json_field = "password"
        self.assertNotEqual(signature, monitor.fingerprint(self.config, check))
        check.json_field = "username"
        check.event_ids = ("success",)
        self.assertNotEqual(signature, monitor.fingerprint(self.config, check))


    def test_only_selected_check_mentions_and_only_first_chunk(self) -> None:
        self.config.checks[0].mention_everyone = True
        self.config.checks.append(monitor.Check("quiet", "/tmp/cowrie.json", "input", ("command",)))
        self.execute(["CMD a"])
        sent: list[tuple[str, bool]] = []

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            return ["CMD " + "x" * 1800]

        def send(url: str, content: str, *, mention_everyone: bool = False) -> None:
            sent.append((content, mention_everyone))

        self.now += 300
        environment = dict(os.environ, DISCORD_WEBHOOK_URL="https://example.invalid/webhook")
        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)), \
                patch.object(monitor, "send_discord", new=send), \
                patch.object(os, "environ", new=environment):
            self.assertEqual(monitor.run(self.config, self.base), 0)
        self.assertGreater(len(sent), 2)
        self.assertTrue(sent[0][1])
        mention_count: int = sum(1 for _, mention in sent if mention)
        self.assertEqual(mention_count, 1)
        self.assertTrue(any(content.startswith("quiet\n") for content, _ in sent))

    def test_lock_prevents_overlapping_execution(self) -> None:
        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            self.fail("Overlapping execution must not fetch")

        with monitor.state_lock(self.base / "state.json.lock") as acquired:
            self.assertTrue(acquired)
            with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)):
                self.assertEqual(monitor.run(self.config, self.base), 0)

    def test_corrupt_state_is_not_overwritten(self) -> None:
        path = self.base / "state.json"
        for content in ('broken', '[]', '{"version": true, "checks": {}}',
                        '{"version": 1, "checks": {"x": {"source": "s", "values": [7]}}}'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.execute(["CMD a"])
                self.assertEqual(path.read_text(), content)


    def test_zero_interval_runs_on_every_invocation(self) -> None:
        path = self.base / "config.json"
        path.write_text(
            '{"ssh":{"host":"cowrie-host"},"state_file":"state.json",'
            '"checks":[{"name":"commands","file":"/tmp/audit.log",'
            '"json_field":"input","event_ids":["command"],"interval_seconds":0}]}', encoding="utf-8",
        )
        self.config = monitor.load_config(path)
        self.assertEqual(self.config.checks[0].interval_seconds, 0)
        self.execute(["CMD a"], advance_seconds=0)
        self.assertIn("+ CMD b", self.execute(["CMD b"], advance_seconds=0)[1][0])
        self.assertIn("+ CMD c", self.execute(["CMD c"], advance_seconds=299)[1][0])
        self.assertEqual(self.execute(["CMD d"], RuntimeError("failed"), advance_seconds=0)[0], 1)
        self.assertIn("+ CMD d", self.execute(["CMD d"], advance_seconds=0)[1][0])

    def test_interval_boundary_and_independent_checks(self) -> None:
        self.config.checks.append(monitor.Check("slow", "/tmp/slow.json", "input", ("command",), interval_seconds=900))
        self.execute(["CMD a"], advance_seconds=0)
        previous = self.snapshot()
        seen: list[str] = []

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            seen.append(check.name)
            return ["CMD a"]

        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)):
            self.now += 299
            self.assertEqual(monitor.run(self.config, self.base), 0)
            self.assertEqual(len(seen), 0)
            self.assertEqual(self.snapshot(), previous)
            self.now += 1
            self.assertEqual(monitor.run(self.config, self.base), 0)
            self.assertEqual(seen, list[str](["commands"]))
            seen.clear()
            self.now += 600
            self.assertEqual(monitor.run(self.config, self.base), 0)
            self.assertEqual(seen, list[str](["commands", "slow"]))

    def test_failed_notification_waits_without_losing_difference(self) -> None:
        self.execute(["CMD a"], advance_seconds=0)
        self.assertEqual(self.execute(["CMD b"], RuntimeError("failed"))[0], 1)
        previous = self.snapshot()
        self.assertEqual(len(self.execute(["CMD b"], advance_seconds=299)[1]), 0)
        self.assertEqual(self.snapshot(), previous)
        self.assertIn("+ CMD b", self.execute(["CMD b"], advance_seconds=1)[1][0])

    def test_first_fetch_failure_is_scheduled(self) -> None:
        calls: list[str] = []

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            calls.append(check.name)
            raise RuntimeError("offline")

        with patch.object(remote, "fetch_increment", new=incremental_stub(fetch)):
            self.assertEqual(monitor.run(self.config, self.base), 1)
            self.assertEqual(monitor.run(self.config, self.base), 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("commands", self.snapshot().checks)

    def test_old_state_migrates_without_resetting_baseline(self) -> None:
        self.execute(["CMD a"], advance_seconds=0)
        path = self.base / "state.json"
        raw = monitor.json_object(monitor.read_json(path))
        del raw["last_runs"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        sent = self.execute(["CMD b"], advance_seconds=0)[1]
        self.assertIn("+ CMD b", sent[0])
        self.assertNotIn("- CMD a", sent[0])

    def test_interval_source_changes_and_clock_rollback(self) -> None:
        self.config.checks[0].interval_seconds = 900
        self.execute(["CMD a"], advance_seconds=0)
        self.config.checks[0].interval_seconds = 300
        self.assertEqual(len(self.execute(["CMD b"], advance_seconds=300)[1]), 1)
        self.config.checks[0].json_field = "message"
        self.execute(["Saved a"], advance_seconds=0)
        self.assertEqual(self.snapshot().checks["commands"].values, list[str](["Saved a"]))
        self.assertEqual(len(self.execute(["Saved b"], advance_seconds=-100)[1]), 1)

    def test_delayed_and_dry_run_do_not_catch_up_or_postpone(self) -> None:
        self.execute(["CMD a"], advance_seconds=0)
        previous = self.snapshot()
        self.execute(["CMD b"], dry_run=True, advance_seconds=9000)
        self.assertEqual(self.snapshot(), previous)
        self.assertEqual(len(self.execute(["CMD b"], advance_seconds=0)[1]), 1)
        self.assertEqual(len(self.execute(["CMD c"], advance_seconds=0)[1]), 0)

    def test_invalid_timestamp_is_not_overwritten(self) -> None:
        path = self.base / "state.json"
        for stamp in (True, -1, "yesterday", float("nan"), float("inf")):
            data: dict[str, object] = {
                "version": 1, "checks": {},
                "last_runs": {"commands": {"source": "s", "started_at": stamp}},
            }
            content = json.dumps(data)
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                monitor.load_state(path)
            self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
