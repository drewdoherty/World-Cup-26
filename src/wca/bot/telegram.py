"""Minimal Telegram Bot API client built on ``requests``.

Deliberately dependency-free beyond ``requests`` (already a project dep) and
synchronous: the management bot is low-traffic and long-polling in a simple
loop keeps compute near zero between messages. See
https://core.telegram.org/bots/api for the endpoints used.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Telegram message bodies are capped at 4096 UTF-8 chars.
MAX_MESSAGE_LEN = 4096


class TelegramError(RuntimeError):
    """Raised when the Telegram API returns ``ok: false`` or transport fails."""


class TelegramClient:
    """Thin wrapper over the Telegram Bot HTTP API.

    Parameters
    ----------
    token:
        Bot token from BotFather. Falls back to ``TELEGRAM_BOT_TOKEN`` env var.
    timeout:
        Per-request HTTP timeout in seconds. Long-poll calls add the poll
        duration on top of this.
    """

    def __init__(self, token: Optional[str] = None, timeout: float = 30.0) -> None:
        tok = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not tok:
            raise TelegramError(
                "no bot token: pass token= or set TELEGRAM_BOT_TOKEN in .env"
            )
        self._token = tok
        self._timeout = float(timeout)
        self._session = requests.Session()

    # -- low-level ---------------------------------------------------------

    def _call(self, method: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        url = API_BASE.format(token=self._token, method=method)
        try:
            resp = self._session.post(url, json=payload, timeout=timeout or self._timeout)
        except requests.RequestException as exc:  # network/transport failure
            raise TelegramError("telegram request failed: %s" % exc) from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramError("telegram returned non-JSON: %s" % resp.text[:200]) from exc
        if not body.get("ok", False):
            raise TelegramError(
                "telegram API error (%s): %s"
                % (body.get("error_code", "?"), body.get("description", "unknown"))
            )
        return body["result"]

    # -- sending -----------------------------------------------------------

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: Optional[str] = "Markdown",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a text message, transparently splitting if over the length cap."""
        chunks = _split_message(text)
        last: Dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            # Only attach the keyboard to the final chunk.
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            last = self._call("sendMessage", payload)
        return last

    # -- receiving ---------------------------------------------------------

    def get_updates(
        self,
        offset: Optional[int] = None,
        poll_timeout: int = 25,
        allowed_updates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Long-poll for updates. ``offset`` should be ``last_update_id + 1``."""
        payload: Dict[str, Any] = {"timeout": int(poll_timeout)}
        if offset is not None:
            payload["offset"] = int(offset)
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates
        # HTTP timeout must exceed the server-side long-poll window.
        return self._call("getUpdates", payload, timeout=poll_timeout + 10)


def _split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> List[str]:
    """Split ``text`` into <= ``limit`` char chunks, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            # A single line longer than the limit must be hard-split.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks
