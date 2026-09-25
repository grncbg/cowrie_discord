from collections.abc import Mapping
import json
from pathlib import Path
import tempfile
import unittest

from cowrie import configuration as monitor


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
    def test_config_validation_and_defaults(self) -> None:
        path = self.base / "config.json"
        check: dict[str, object] = {"name": "test", "file": "/tmp/cowrie.json", "json_field": "input", "event_ids": ["command"]}
        ssh: dict[str, object] = {"host": "cowrie-host"}
        config: dict[str, object] = {"ssh": ssh, "checks": [check]}
        path.write_text(json.dumps(config), encoding="utf-8")
        loaded = monitor.load_config(path)
        self.assertEqual(loaded.ssh.timeout_seconds, 90)
        self.assertFalse(loaded.notify_initial)
        self.assertFalse(loaded.checks[0].mention_everyone)
        self.assertEqual(loaded.checks[0].interval_seconds, 300)
        json_check: dict[str, object] = {
            "name": "password", "file": "/tmp/cowrie.json",
            "json_field": "password", "event_ids": ["cowrie.login.failed"],
        }
        invalid: list[Mapping[str, object]] = [
            *({"checks": [dict(json_check, event_ids=value)]}
              for value in (None, [], "auth", [1], [""])),
            *({"checks": [dict(json_check, json_field=value)]}
              for value in (None, "", 1)),
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
