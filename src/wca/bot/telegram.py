"""Minimal Telegram Bot API client built on ``requests``.

Deliberately dependency-free beyond ``requests`` (already a project dep) and
synchronous: the management bot is low-traffic and long-polling in a simple
loop keeps compute near zero between messages. See
https://core.telegram.org/bots/api for the endpoints used.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
        return self._result(resp)

    @staticmethod
    def _result(resp: "requests.Response") -> Any:
        """Parse a Bot API response, raising :class:`TelegramError` on failure."""
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

    def _upload(self, method: str, data: Dict[str, Any], files: Dict[str, Any]) -> Any:
        """POST a multipart form (file upload). File transfers can be slow, so
        this uses a longer floor on the timeout than JSON calls."""
        url = API_BASE.format(token=self._token, method=method)
        try:
            resp = self._session.post(
                url, data=data, files=files, timeout=max(self._timeout, 120.0)
            )
        except requests.RequestException as exc:
            raise TelegramError("telegram upload failed: %s" % exc) from exc
        return self._result(resp)

    # -- sending -----------------------------------------------------------

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: Optional[str] = "Markdown",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a text message, transparently splitting if over the length cap.

        If a chunk fails to parse under ``parse_mode`` (e.g. an odd ``_`` from a
        venue name like ``betfair_ex_uk`` makes Telegram's Markdown reject the
        whole message with "can't parse entities"), the chunk is retried as
        plain text so the bot NEVER drops a reply over a formatting glitch.
        """
        chunks = _split_message(text)
        last: Dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            # Only attach the keyboard to the final chunk.
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            try:
                last = self._call("sendMessage", payload)
            except TelegramError as exc:
                if parse_mode and "parse entities" in str(exc).lower():
                    # Markdown parse failure -> resend as plain text, dropping the
                    # bold ``*`` markers so the fallback reads cleanly (underscores
                    # in venue names render fine literally without a parse mode).
                    payload.pop("parse_mode", None)
                    payload["text"] = chunk.replace("*", "")
                    last = self._call("sendMessage", payload)
                else:
                    raise
        return last

    def send_document(
        self,
        chat_id: int | str,
        document: Union[str, "os.PathLike[str]", bytes, bytearray],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a file as a Telegram *document* (any type, up to ~50 MB).

        ``document`` may be a filesystem path or raw ``bytes`` (then ``filename``
        is used for the displayed name). ``caption`` is sent as plain text (no
        parse mode) so report content never trips Markdown parsing.
        """
        name, fileobj, opened = _as_fileobj(document, filename)
        try:
            data: Dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1024]
            return self._upload("sendDocument", data, {"document": (name, fileobj)})
        finally:
            if opened:
                fileobj.close()

    def send_photo(
        self,
        chat_id: int | str,
        photo: Union[str, "os.PathLike[str]", bytes, bytearray],
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an image as a Telegram *photo* (rendered inline, up to ~10 MB).

        For larger images or non-displayable types use :meth:`send_document`.
        """
        name, fileobj, opened = _as_fileobj(photo, filename)
        try:
            data: Dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption[:1024]
            return self._upload("sendPhoto", data, {"photo": (name, fileobj)})
        finally:
            if opened:
                fileobj.close()

    # -- interactive menus (inline keyboards) ------------------------------

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> Any:
        """Acknowledge a tapped inline-keyboard button (dismisses the spinner)."""
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._call("answerCallbackQuery", payload)

    def set_my_commands(self, commands: List[Dict[str, str]]) -> Any:
        """Register the slash-command menu (the '/' button in Telegram clients)."""
        return self._call("setMyCommands", {"commands": commands})

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
        """Download the image attached to *message*.

        Handles both compressed photos (``photo`` field) and images sent as
        files (``document`` field with an ``image/*`` mime type — Telegram
        uses this when the sender picks "send as file", which macOS drag-drop
        does by default for screenshots).

        Returns the raw image bytes, or ``None`` if the message contains no
        image.
        """
        file_id = largest_photo_file_id(message)
        if file_id is None:
            file_id = image_document_file_id(message)
        if file_id is None:
            return None
        file_info = self.get_file(file_id)
        return self.download_file(file_info["file_path"])

    def save_image(
        self, message: Dict[str, Any], dest_dir: "str | os.PathLike[str]", stem: str
    ) -> Optional[str]:
        """Download the image attached to *message* and write it under *dest_dir*.

        Picks the highest-resolution ``photo`` (or an ``image/*`` ``document``),
        preserves the source file extension (defaulting to ``.jpg``), and writes
        it to ``<dest_dir>/<stem><ext>``. Creates *dest_dir* if needed.

        Returns the absolute path written, or ``None`` when *message* has no
        image. Raises :class:`TelegramError` on a transport/API failure.
        """
        file_id = largest_photo_file_id(message) or image_document_file_id(message)
        if file_id is None:
            return None
        info = self.get_file(file_id)
        remote_path = str(info.get("file_path") or "")
        ext = os.path.splitext(remote_path)[1].lower() or ".jpg"
        data = self.download_file(remote_path)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / ("%s%s" % (stem, ext))
        out.write_bytes(data)
        return str(out)

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


def image_document_file_id(message: Dict[str, Any]) -> Optional[str]:
    """Return the ``file_id`` of an image sent as a *file* (document).

    Telegram delivers uncompressed images as a ``document`` with an
    ``image/*`` mime type instead of a ``photo``.  Returns ``None`` if the
    message has no document or the document is not an image.
    """
    doc: Optional[Dict[str, Any]] = message.get("document")
    if not doc:
        return None
    mime = (doc.get("mime_type") or "").lower()
    name = (doc.get("file_name") or "").lower()
    if not (
        mime.startswith("image/")
        or name.endswith((".png", ".jpg", ".jpeg", ".webp"))
    ):
        return None
    return doc.get("file_id")


def _as_fileobj(
    src: Union[str, "os.PathLike[str]", bytes, bytearray],
    filename: Optional[str],
) -> Tuple[str, Any, bool]:
    """Normalise *src* (a path or raw bytes) to ``(name, fileobj, opened)``.

    ``opened`` is True only when a real file was opened (so the caller closes
    it); in-memory ``bytes`` become a ``BytesIO`` the caller may leave to GC.
    """
    if isinstance(src, (bytes, bytearray)):
        return (filename or "file"), io.BytesIO(bytes(src)), False
    p = Path(src)
    return (filename or p.name), p.open("rb"), True


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
