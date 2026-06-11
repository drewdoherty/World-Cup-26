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

    # -- file / photo download ---------------------------------------------

    def get_file(self, file_id: str) -> Dict[str, Any]:
        """Call the ``getFile`` Bot API method for *file_id*.

        Returns the ``result`` dict which includes ``file_path`` (a relative
        path used to construct the CDN download URL).
        """
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str) -> bytes:
        """Download a file from the Telegram CDN using its *file_path*.

        *file_path* is the value returned by :meth:`get_file`.  The bot token
        is embedded in the URL; it is **not** logged.

        Raises :class:`TelegramError` on transport failures or non-200
        HTTP responses.
        """
        url = "https://api.telegram.org/file/bot{token}/{file_path}".format(
            token=self._token, file_path=file_path
        )
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise TelegramError("telegram file download failed: %s" % exc) from exc
        if resp.status_code != 200:
            raise TelegramError(
                "telegram file download returned HTTP %d" % resp.status_code
            )
        return resp.content

    def download_photo(self, message: Dict[str, Any]) -> Optional[bytes]:
        """Download the largest photo attached to *message*.

        Returns the raw image bytes, or ``None`` if the message contains no
        ``photo`` field.
        """
        file_id = largest_photo_file_id(message)
        if file_id is None:
            return None
        file_info = self.get_file(file_id)
        return self.download_file(file_info["file_path"])

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


def largest_photo_file_id(message: Dict[str, Any]) -> Optional[str]:
    """Return the ``file_id`` of the highest-resolution photo in *message*.

    Telegram sends photos as a list of :class:`PhotoSize` dicts (one entry per
    resolution).  We pick the entry with the greatest ``width * height``
    product; ties are broken by ``file_size`` (larger wins).

    Returns ``None`` if *message* has no ``photo`` field or the list is empty.
    """
    photos: Optional[List[Dict[str, Any]]] = message.get("photo")
    if not photos:
        return None
    best = max(
        photos,
        key=lambda p: (p.get("width", 0) * p.get("height", 0), p.get("file_size", 0)),
    )
    return best.get("file_id")


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
