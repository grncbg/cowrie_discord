from email.message import Message
import io
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from cowrie import discord as monitor
from cowrie.validation import json_object, parse_json, string


class DiscordTests(unittest.TestCase):
    def test_long_messages_and_mentions(self) -> None:
        chunks = list(monitor.messages("😀" * 120, ["😀" * 3000 + "``` @everyone"]))
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
        body = json_object(parse_json(request.data))
        mentions = json_object(body["allowed_mentions"])
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
            bodies.append(json_object(parse_json(request.data)))

        hostile = "@everyone @here <@123> <@&456> " + "😀" * 700
        with patch.object(monitor, "post_discord", new=post):
            monitor.send_discord("https://example.invalid/webhook", hostile, mention_everyone=True)
            monitor.send_discord("https://example.invalid/webhook", hostile)
        content = string(bodies[0]["content"])
        self.assertTrue(content.startswith("@everyone\n"))
        self.assertEqual(content.count("@everyone"), 1)
        self.assertNotIn("@here", content)
        self.assertNotIn("<@123>", content)
        self.assertLessEqual(len(content.encode("utf-16-le")) // 2, 2000)
        self.assertEqual(json_object(bodies[0]["allowed_mentions"])["parse"], list[str](["everyone"]))
        self.assertEqual(json_object(bodies[1]["allowed_mentions"])["parse"], list[str]())
        self.assertNotIn("@everyone", string(bodies[1]["content"]))
