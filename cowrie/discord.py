"""Format safe notifications and deliver them with bounded retries."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from email.message import Message
from http.client import HTTPResponse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.response import addinfourl

from .validation import json_object, parse_json


def messages(name: str, added: Sequence[str]) -> Iterator[str]:
    header = f"{name}\n"
    text = "\n".join("+ " + line for line in added)
    # Render malformed bytes safely and prevent log content from closing code fences.
    text = text.encode("utf-8", errors="backslashreplace").decode("utf-8").replace("`", "ˋ")
    # 750 code points stay under 2000 UTF-16 units even with astral characters.
    for start in range(0, len(text), 750):
        yield header + "```diff\n" + text[start:start + 750] + "\n```"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: object, code: int,
        msg: str, headers: Message, newurl: str,
    ) -> None:
        return None


def post_discord(request: urllib.request.Request) -> None:
    opener = urllib.request.build_opener(NoRedirect())
    raw: object = opener.open(request, timeout=30)
    if not isinstance(raw, (HTTPResponse, addinfourl)):
        raise RuntimeError("Unexpected HTTP response type")
    with raw as response:
        if response.status != 200:
            raise RuntimeError(f"Discord returned HTTP {response.status}")
        response.read()


def webhook_request(url: str, content: str, *, mention_everyone: bool = False) -> urllib.request.Request:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("Webhook URL must be HTTPS")
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    target = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    # Only our explicit prefix may ping. Neutralize mentions in names/log data,
    # including fragments split across message boundaries.
    safe_content = content.replace("@", "@\u200b")
    allowed: list[str] = ["everyone"] if mention_everyone else []
    body: dict[str, object] = {
        "content": ("@everyone\n" if mention_everyone else "") + safe_content,
        "allowed_mentions": {"parse": allowed},
    }
    payload = json.dumps(body).encode()
    return urllib.request.Request(target, data=payload, headers={
        "Content-Type": "application/json", "User-Agent": "CowrieMonitor/1.0"
    }, method="POST")


def send_discord(url: str, content: str, *, mention_everyone: bool = False) -> None:
    request = webhook_request(url, content, mention_everyone=mention_everyone)
    for attempt in range(3):
        try:
            post_discord(request)
            return
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry = (1.0, 2.0, 4.0)[attempt]
            if status == 429:
                try:
                    retry_data = json_object(parse_json(exc.read())).get("retry_after", retry)
                    if isinstance(retry_data, (str, int, float)) and not isinstance(retry_data, bool):
                        retry = float(retry_data)
                except (ValueError, TypeError):
                    pass
            exc.close()
            if attempt == 2 or (status != 429 and status < 500) or not 0 <= retry <= 30:
                raise RuntimeError(f"Discord returned HTTP {status}; snapshot not updated") from None
            time.sleep(retry)
        except (urllib.error.URLError, OSError):
            # Never log exception text: it can contain the secret webhook URL.
            raise RuntimeError("Discord connection failed; snapshot not updated") from None
