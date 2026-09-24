from collections.abc import Mapping, Sequence
import copy
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import cowrie_monitor as monitor


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
            checks=[monitor.Check("commands", "/tmp/audit.log", "CMD.+$")],
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
        with patch.object(monitor, "fetch", new=fetch), \
                patch.object(monitor, "send_discord", new=send), \
                patch.object(os, "environ", new=environment):
            result = monitor.run(self.config, self.base, dry_run)
        return result, sent

    def test_baseline_unchanged_added_removed(self) -> None:
        self.assertEqual(self.execute(["CMD a"])[1], list[str]())
        self.assertEqual(self.execute(["CMD a"])[1], list[str]())
        result, sent = self.execute(["CMD b"])
        self.assertEqual(result, 0)
        self.assertIn("+ CMD b", sent[0])
        self.assertIn("- CMD a", sent[0])

    def test_failed_delivery_preserves_snapshot_and_retries(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.assertEqual(self.execute(["CMD b"], RuntimeError("HTTP 500"))[0], 1)
        self.assertEqual(self.snapshot().checks, previous.checks)
        self.assertEqual(len(self.execute(["CMD b"])[1]), 1)

    def test_fetch_failure_is_not_empty_snapshot(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.now += 300

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            raise RuntimeError("grep failed")

        with patch.object(monitor, "fetch", new=fetch):
            self.assertEqual(monitor.run(self.config, self.base), 1)
        self.assertEqual(self.snapshot().checks, previous.checks)

    def test_initial_notification_and_removed_option(self) -> None:
        self.config.notify_initial = True
        self.assertEqual(len(self.execute(["CMD a"])[1]), 1)
        self.config.checks[0].notify_removed = False
        self.assertEqual(self.execute([])[1], list[str]())
        self.assertEqual(self.snapshot().checks["commands"].values, list[str]())

    def test_source_change_rebaselines_and_dry_run_does_not_update(self) -> None:
        self.execute(["CMD a"])
        previous = self.snapshot()
        self.execute(["CMD b"], dry_run=True)
        self.assertEqual(self.snapshot(), previous)
        self.config.checks[0].regex = "Saved.+$"
        self.assertEqual(self.execute(["Saved file"])[1], list[str]())

    def test_independent_checks_continue_after_failure(self) -> None:
        second = copy.deepcopy(self.config.checks[0])
        second.name = "second"
        self.config.checks.append(second)

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            if check.name == "commands":
                raise RuntimeError("fail")
            return ["CMD b"]

        with patch.object(monitor, "fetch", new=fetch):
            self.assertEqual(monitor.run(self.config, self.base), 1)
        self.assertNotIn("commands", self.snapshot().checks)
        self.assertIn("second", self.snapshot().checks)

    def test_grep_exit_codes_and_unique_values(self) -> None:
        result = subprocess.CompletedProcess[bytes](list[str](), 0, b"CMD b\nCMD a\nCMD a\n", b"")

        def run(
            args: Sequence[str], *, stdin: int, stdout: int, stderr: int,
            timeout: float, check: bool,
        ) -> subprocess.CompletedProcess[bytes]:
            self.assertIn("BatchMode=yes", args)
            return result

        with patch.object(subprocess, "run", new=run):
            expected: list[str] = ["CMD a", "CMD b"]
            self.assertEqual(monitor.fetch(self.config, self.config.checks[0]), expected)
            result = subprocess.CompletedProcess[bytes](list[str](), 1, b"", b"")
            self.assertEqual(monitor.fetch(self.config, self.config.checks[0]), list[str]())
            result = subprocess.CompletedProcess[bytes](list[str](), 2, b"", b"Permission denied")
            with self.assertRaises(RuntimeError):
                monitor.fetch(self.config, self.config.checks[0])

    def test_long_messages_and_mentions(self) -> None:
        chunks = list(monitor.messages("😀" * 120, ["😀" * 3000 + "``` @everyone"], []))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 2000)
            self.assertEqual(chunk.count("```"), 2)
        requests: list[urllib.request.Request] = []

        def post(request: urllib.request.Request) -> None:
            requests.append(request)

        with patch.object(monitor, "post_discord", new=post):
            monitor.send_discord("https://discord.com/api/webhooks/test?thread_id=123", "@everyone")
        request = requests[0]
        self.assertIsNotNone(request.data)
        if request.data is None:
            self.fail("Missing payload")
        if not isinstance(request.data, bytes):
            self.fail("Payload must be bytes")
        body = monitor.json_object(monitor.parse_json(request.data))
        mentions = monitor.json_object(body["allowed_mentions"])
        self.assertEqual(mentions["parse"], list[str]())
        self.assertIn("wait=true", request.full_url)
        self.assertIn("thread_id=123", request.full_url)

    def test_rate_limit_retry(self) -> None:
        requests: list[urllib.request.Request] = []
        waits: list[float] = []

        def post(request: urllib.request.Request) -> None:
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "limit", Message(),
                    io.BytesIO(b'{"retry_after": 0.1}'),
                )

        def sleep(seconds: float) -> None:
            waits.append(seconds)

        with patch.object(monitor, "post_discord", new=post), \
                patch.object(time, "sleep", new=sleep):
            monitor.send_discord("https://example.invalid/secret", "hello")
        self.assertEqual(waits, list[float]([0.1]))
        self.assertEqual(len(requests), 2)

    def test_explicit_everyone_prefix_and_untrusted_mentions(self) -> None:
        bodies: list[dict[str, object]] = []

        def post(request: urllib.request.Request) -> None:
            if not isinstance(request.data, bytes):
                self.fail("Expected byte payload")
            bodies.append(monitor.json_object(monitor.parse_json(request.data)))

        hostile = "@everyone @here <@123> <@&456> " + "😀" * 700
        with patch.object(monitor, "post_discord", new=post):
            monitor.send_discord("https://example.invalid/webhook", hostile, mention_everyone=True)
            monitor.send_discord("https://example.invalid/webhook", hostile)
        content = monitor.string(bodies[0]["content"])
        self.assertTrue(content.startswith("@everyone\n"))
        self.assertEqual(content.count("@everyone"), 1)
        self.assertNotIn("@here", content)
        self.assertNotIn("<@123>", content)
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 2000)
        self.assertEqual(monitor.json_object(bodies[0]["allowed_mentions"])["parse"], list[str](["everyone"]))
        self.assertEqual(monitor.json_object(bodies[1]["allowed_mentions"])["parse"], list[str]())
        self.assertNotIn("@everyone", monitor.string(bodies[1]["content"]))

    def test_only_selected_check_mentions_and_only_first_chunk(self) -> None:
        self.config.checks[0].mention_everyone = True
        self.config.checks.append(monitor.Check("quiet", "/tmp/audit.log", "CMD.+$"))
        self.execute(["CMD a"])
        sent: list[tuple[str, bool]] = []

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            return ["CMD " + "x" * 1800]

        def send(url: str, content: str, *, mention_everyone: bool = False) -> None:
            sent.append((content, mention_everyone))

        self.now += 300
        environment = dict(os.environ, DISCORD_WEBHOOK_URL="https://example.invalid/webhook")
        with patch.object(monitor, "fetch", new=fetch), \
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
            with patch.object(monitor, "fetch", new=fetch):
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

    def test_config_validation_and_defaults(self) -> None:
        path = self.base / "config.json"
        check: dict[str, object] = {"name": "test", "file": "/tmp/audit.log", "regex": "CMD.+$"}
        ssh: dict[str, object] = {"host": "cowrie-host"}
        config: dict[str, object] = {"ssh": ssh, "checks": [check]}
        path.write_text(json.dumps(config), encoding="utf-8")
        loaded = monitor.load_config(path)
        self.assertEqual(loaded.ssh.timeout_seconds, 90)
        self.assertTrue(loaded.checks[0].notify_removed)
        self.assertFalse(loaded.notify_initial)
        self.assertFalse(loaded.checks[0].mention_everyone)
        self.assertEqual(loaded.checks[0].interval_seconds, 300)
        invalid: list[Mapping[str, object]] = [
            {"checks": [dict(check, notify_removed="true")]},
            {"checks": [dict(check, mention_everyone="true")]},
            {"checks": [dict(check, mention_everyone=1)]},
            {"ssh": dict(ssh, timeout_seconds=True)},
            {"ssh": dict(ssh, host=123)},
            {"state_file": []}, {"notify_initial": 1}, {"checks": [check, check]},
            *({"checks": [dict(check, interval_seconds=value)]}
              for value in (-1, True, "300", 1.5, None)),
        ]
        for change in invalid:
            with self.subTest(change=change):
                updated = config.copy()
                updated.update(change)
                path.write_text(json.dumps(updated), encoding="utf-8")
                with self.assertRaises(ValueError):
                    monitor.load_config(path)

    def test_public_example_loads_without_mentions(self) -> None:
        config = monitor.load_config(Path(__file__).with_name("config.example.json"))
        self.assertEqual(config.ssh.host, "cowrie-host")
        self.assertFalse(config.notify_initial)
        self.assertGreater(len(config.checks), 0)
        for check in config.checks:
            self.assertFalse(check.mention_everyone)
            self.assertEqual(check.interval_seconds, 0)


    def test_zero_interval_runs_on_every_invocation(self) -> None:
        path = self.base / "config.json"
        path.write_text(
            '{"ssh":{"host":"cowrie-host"},"state_file":"state.json",'
            '"checks":[{"name":"commands","file":"/tmp/audit.log",'
            '"regex":"CMD.+$","interval_seconds":0}]}', encoding="utf-8",
        )
        self.config = monitor.load_config(path)
        self.assertEqual(self.config.checks[0].interval_seconds, 0)
        self.execute(["CMD a"], advance_seconds=0)
        self.assertIn("+ CMD b", self.execute(["CMD b"], advance_seconds=0)[1][0])
        self.assertIn("+ CMD c", self.execute(["CMD c"], advance_seconds=299)[1][0])
        self.assertEqual(self.execute(["CMD d"], RuntimeError("failed"), advance_seconds=0)[0], 1)
        self.assertIn("+ CMD d", self.execute(["CMD d"], advance_seconds=0)[1][0])

    def test_interval_boundary_and_independent_checks(self) -> None:
        self.config.checks.append(monitor.Check("slow", "/tmp/audit.log", "CMD.+$", interval_seconds=900))
        self.execute(["CMD a"], advance_seconds=0)
        previous = self.snapshot()
        seen: list[str] = []

        def fetch(config: monitor.Config, check: monitor.Check) -> list[str]:
            seen.append(check.name)
            return ["CMD a"]

        with patch.object(monitor, "fetch", new=fetch):
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

        with patch.object(monitor, "fetch", new=fetch):
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
        self.assertIn("- CMD a", sent[0])

    def test_interval_source_changes_and_clock_rollback(self) -> None:
        self.config.checks[0].interval_seconds = 900
        self.execute(["CMD a"], advance_seconds=0)
        self.config.checks[0].interval_seconds = 300
        self.assertEqual(len(self.execute(["CMD b"], advance_seconds=300)[1]), 1)
        self.config.checks[0].regex = "Saved.+$"
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
