import json
from pathlib import Path
import unittest

from cowrie.configuration import load_config
from cowrie import events as monitor


class EventTests(unittest.TestCase):
    def test_json_credentials_preserve_special_values(self) -> None:
        config = load_config(Path(__file__).with_name("config.example.json"))
        cases = [("user/name", ""), ("", " spaced "), ("日本語", '[]/"\\\n\x00')]
        records: list[dict[str, object]] = [
            {"eventid": event, "username": user, "password": password}
            for event in ("cowrie.login.failed", "cowrie.login.success")
            for user, password in cases
        ]
        records.append({"eventid": "cowrie.command.input"})
        data = ("\n".join(json.dumps(record) for record in records) + "\n").encode()
        for name, index in (("user", 0), ("password", 1)):
            check = next(c for c in config.checks if c.name == name)
            expected: list[str] = sorted({c[index] for c in cases})
            self.assertEqual(monitor.json_log_values(data, check.json_field, check.event_ids), expected)

    def test_json_invalid_records_fail(self) -> None:
        invalid = [b'{', b'[]', b'{}', b'{"eventid":3}',
                   b'{"eventid":"auth"}', b'{"eventid":"auth","password":null}',
                   b'{"eventid":"auth","password":42}', b'\xff']
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                monitor.json_log_values(data, "password", ("auth",))

    def test_example_event_selection_for_commands_files_and_addresses(self) -> None:
        config = load_config(Path(__file__).with_name("config.example.json"))
        records: list[dict[str, str]] = [
            {"eventid": "cowrie.command.input", "input": "echo Saved /tmp/x"},
            {"eventid": "cowrie.command.failed", "input": "ignored"},
            {"eventid": "cowrie.session.file_download", "message": "download complete"},
            {"eventid": "cowrie.session.file_upload", "message": "upload complete"},
            {"eventid": "cowrie.session.file_download.failed", "message": "ignored"},
            {"eventid": "cowrie.session.connect", "src_ip": "2001:db8::1"},
            {"eventid": "cowrie.session.connect", "src_ip": "192.0.2.1"},
            {"eventid": "cowrie.client.version", "src_ip": "192.0.2.2",
             "message": "CMD spoof Saved SFTP New connection: 192.0.2.2"},
        ]
        data = "\n".join(json.dumps(record) for record in records + records).encode()
        expected: dict[str, list[str]] = {
            "commands": ["echo Saved /tmp/x"],
            "uploads": ["download complete", "upload complete"],
            "source-addresses": ["192.0.2.1", "2001:db8::1"],
        }
        for check in config.checks:
            self.assertEqual(check.file, "/opt/cowrie/var/log/cowrie/cowrie.json")
            if check.name in expected:
                field = check.json_field
                self.assertEqual(monitor.json_log_values(data, field, check.event_ids),
                                 expected[check.name])
