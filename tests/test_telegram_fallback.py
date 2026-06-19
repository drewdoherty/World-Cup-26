"""The Telegram client must never drop a reply over a Markdown parse error.

A card line like ``(betfair_ex_uk)`` contributes an odd number of ``_`` which
Telegram's legacy Markdown rejects with "can't parse entities". send_message
must transparently retry the chunk as plain text (with the ``*`` bold markers
stripped) so commands like /card always deliver.
"""
from __future__ import annotations

import pytest

from wca.bot.telegram import TelegramClient, TelegramError


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    """Fails the first send (parse error) and records every payload."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(dict(json))  # copy: send_message mutates the payload
        if json.get("parse_mode") == "Markdown":
            return _FakeResp({
                "ok": False, "error_code": 400,
                "description": "Bad Request: can't parse entities: Can't find end "
                               "of the entity starting at byte offset 379",
            })
        return _FakeResp({"ok": True, "result": {"message_id": 1}})


def _client():
    c = TelegramClient(token="x:y")
    c._session = _FakeSession()
    return c


def test_markdown_parse_error_falls_back_to_plain():
    c = _client()
    text = "*1. United States vs Australia* — Australia @ *5.50* (betfred_uk)"
    res = c.send_message(123, text)  # default parse_mode=Markdown
    assert res == {"message_id": 1}
    calls = c._session.calls
    # First attempt used Markdown (and failed); second was plain text.
    assert calls[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in calls[1]
    # Fallback dropped the bold markers; the underscore venue name survives.
    assert "*" not in calls[1]["text"]
    assert "betfred_uk" in calls[1]["text"]


def test_non_parse_error_still_raises():
    c = TelegramClient(token="x:y")

    class _AuthFail:
        def post(self, url, json=None, timeout=None):
            return _FakeResp({"ok": False, "error_code": 403, "description": "Forbidden"})

    c._session = _AuthFail()
    with pytest.raises(TelegramError):
        c.send_message(123, "hello")
