"""Tests for TelegramClient photo-download helpers + conductor image routing.

No real network calls are made; ``requests.Session`` methods are monkeypatched
with lightweight fakes. The conductor-bot tests stub ``ConductorManager`` so no
real agent/git pipeline runs.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from wca.bot.telegram import TelegramClient, TelegramError, largest_photo_file_id


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_client() -> TelegramClient:
    return TelegramClient(token="TESTTOKEN")


def _fake_post_response(body: Dict[str, Any], status_code: int = 200) -> MagicMock:
    """Return a fake requests.Response-like object for POST calls."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _fake_get_response(content: bytes, status_code: int = 200) -> MagicMock:
    """Return a fake requests.Response-like object for GET calls."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    return resp


# ---------------------------------------------------------------------------
# largest_photo_file_id
# ---------------------------------------------------------------------------

class TestLargestPhotoFileId:
    def test_picks_largest_by_area(self) -> None:
        message = {
            "photo": [
                {"file_id": "small", "width": 90, "height": 90, "file_size": 500},
                {"file_id": "medium", "width": 320, "height": 240, "file_size": 12000},
                {"file_id": "large", "width": 800, "height": 600, "file_size": 50000},
            ]
        }
        assert largest_photo_file_id(message) == "large"

    def test_picks_larger_file_size_on_tie(self) -> None:
        # Same area, different file_size
        message = {
            "photo": [
                {"file_id": "a", "width": 100, "height": 100, "file_size": 1000},
                {"file_id": "b", "width": 100, "height": 100, "file_size": 2000},
            ]
        }
        assert largest_photo_file_id(message) == "b"

    def test_returns_none_when_no_photo_key(self) -> None:
        assert largest_photo_file_id({"text": "hello"}) is None

    def test_returns_none_for_empty_photo_list(self) -> None:
        assert largest_photo_file_id({"photo": []}) is None

    def test_single_photo(self) -> None:
        message = {"photo": [{"file_id": "only_one", "width": 320, "height": 240}]}
        assert largest_photo_file_id(message) == "only_one"


# ---------------------------------------------------------------------------
# TelegramClient.get_file
# ---------------------------------------------------------------------------

class TestGetFile:
    def test_returns_file_path(self) -> None:
        client = _make_client()
        result_body = {
            "ok": True,
            "result": {
                "file_id": "abc123",
                "file_unique_id": "u123",
                "file_size": 9999,
                "file_path": "photos/file_0.jpg",
            },
        }
        client._session.post = MagicMock(return_value=_fake_post_response(result_body))
        result = client.get_file("abc123")
        assert result["file_path"] == "photos/file_0.jpg"
        # Confirm the API method was correct
        call_args = client._session.post.call_args
        assert "getFile" in call_args[0][0]

    def test_propagates_api_error(self) -> None:
        client = _make_client()
        error_body = {"ok": False, "error_code": 400, "description": "Bad Request"}
        client._session.post = MagicMock(return_value=_fake_post_response(error_body))
        with pytest.raises(TelegramError, match="400"):
            client.get_file("bad_file_id")


# ---------------------------------------------------------------------------
# TelegramClient.download_file
# ---------------------------------------------------------------------------

class TestDownloadFile:
    def test_returns_bytes_on_success(self) -> None:
        client = _make_client()
        fake_bytes = b"\xff\xd8\xff\xe0fake_jpeg"
        client._session.get = MagicMock(return_value=_fake_get_response(fake_bytes, 200))
        result = client.download_file("photos/file_0.jpg")
        assert result == fake_bytes

    def test_url_contains_token_and_path(self) -> None:
        client = _make_client()
        client._session.get = MagicMock(return_value=_fake_get_response(b"data", 200))
        client.download_file("photos/some_file.jpg")
        call_url = client._session.get.call_args[0][0]
        assert "TESTTOKEN" in call_url
        assert "photos/some_file.jpg" in call_url

    def test_raises_telegram_error_on_404(self) -> None:
        client = _make_client()
        client._session.get = MagicMock(return_value=_fake_get_response(b"not found", 404))
        with pytest.raises(TelegramError, match="404"):
            client.download_file("photos/missing.jpg")

    def test_raises_telegram_error_on_transport_failure(self) -> None:
        import requests as req_lib

        client = _make_client()
        client._session.get = MagicMock(side_effect=req_lib.ConnectionError("timeout"))
        with pytest.raises(TelegramError, match="download failed"):
            client.download_file("photos/file_0.jpg")


# ---------------------------------------------------------------------------
# TelegramClient.download_photo
# ---------------------------------------------------------------------------

class TestDownloadPhoto:
    def _photo_message(self) -> Dict[str, Any]:
        return {
            "message_id": 1,
            "photo": [
                {"file_id": "small_id", "width": 90, "height": 90, "file_size": 500},
                {"file_id": "large_id", "width": 800, "height": 600, "file_size": 50000},
            ],
        }

    def test_returns_bytes_for_photo_message(self) -> None:
        client = _make_client()
        fake_bytes = b"\xff\xd8\xff\xe0image"
        # Fake get_file via _call (which uses POST)
        get_file_response = {
            "ok": True,
            "result": {
                "file_id": "large_id",
                "file_unique_id": "u1",
                "file_size": 50000,
                "file_path": "photos/large.jpg",
            },
        }
        client._session.post = MagicMock(
            return_value=_fake_post_response(get_file_response)
        )
        client._session.get = MagicMock(
            return_value=_fake_get_response(fake_bytes, 200)
        )
        result = client.download_photo(self._photo_message())
        assert result == fake_bytes
        # Confirm get_file was called for the *largest* photo
        post_payload = client._session.post.call_args[1]["json"]
        assert post_payload["file_id"] == "large_id"

    def test_returns_none_for_text_only_message(self) -> None:
        client = _make_client()
        text_message: Dict[str, Any] = {"message_id": 2, "text": "hello"}
        # No network calls should be made
        client._session.post = MagicMock()
        client._session.get = MagicMock()
        result = client.download_photo(text_message)
        assert result is None
        client._session.post.assert_not_called()
        client._session.get.assert_not_called()

    def test_propagates_download_error(self) -> None:
        client = _make_client()
        get_file_response = {
            "ok": True,
            "result": {
                "file_id": "large_id",
                "file_unique_id": "u1",
                "file_size": 50000,
                "file_path": "photos/large.jpg",
            },
        }
        client._session.post = MagicMock(
            return_value=_fake_post_response(get_file_response)
        )
        client._session.get = MagicMock(
            return_value=_fake_get_response(b"", 500)
        )
        with pytest.raises(TelegramError):
            client.download_photo(self._photo_message())


# ---------------------------------------------------------------------------
# TelegramClient.save_image
# ---------------------------------------------------------------------------

class TestSaveImage:
    def _photo_message(self) -> Dict[str, Any]:
        return {
            "photo": [
                {"file_id": "small_id", "width": 90, "height": 90, "file_size": 500},
                {"file_id": "large_id", "width": 800, "height": 600, "file_size": 50000},
            ],
        }

    def test_writes_file_with_source_extension(self, tmp_path) -> None:
        client = _make_client()
        client.get_file = MagicMock(return_value={"file_path": "photos/file_9.JPG"})
        client.download_file = MagicMock(return_value=b"JPEGDATA")
        out = client.save_image(self._photo_message(), tmp_path, "abc")
        assert out is not None
        assert out.endswith("abc.jpg")  # extension lower-cased
        assert Path(out).read_bytes() == b"JPEGDATA"
        client.get_file.assert_called_once_with("large_id")  # highest-res photo

    def test_defaults_extension_when_remote_has_none(self, tmp_path) -> None:
        client = _make_client()
        client.get_file = MagicMock(return_value={"file_path": "photos/file_noext"})
        client.download_file = MagicMock(return_value=b"DATA")
        out = client.save_image(self._photo_message(), tmp_path, "xyz")
        assert out.endswith("xyz.jpg")

    def test_handles_image_document(self, tmp_path) -> None:
        client = _make_client()
        client.get_file = MagicMock(return_value={"file_path": "documents/shot.png"})
        client.download_file = MagicMock(return_value=b"PNG")
        msg = {"document": {"file_id": "doc1", "mime_type": "image/png", "file_name": "shot.png"}}
        out = client.save_image(msg, tmp_path, "d1")
        assert out.endswith("d1.png")
        client.get_file.assert_called_once_with("doc1")

    def test_returns_none_without_image(self, tmp_path) -> None:
        client = _make_client()
        client.get_file = MagicMock()
        client.download_file = MagicMock()
        assert client.save_image({"text": "hi"}, tmp_path, "n") is None
        client.get_file.assert_not_called()


# ---------------------------------------------------------------------------
# ConductorBot — caption + screenshot routing
# ---------------------------------------------------------------------------

_CONDUCTOR_PATH = Path(__file__).resolve().parent.parent / "scripts" / "wca_conductor.py"
_spec = importlib.util.spec_from_file_location("wca_conductor", _CONDUCTOR_PATH)
wca_conductor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wca_conductor)  # type: ignore[union-attr]

from wca.conductor.config import ConductorConfig  # noqa: E402
from wca.conductor.manager import ConductorManager  # noqa: E402
from wca.conductor.models import TaskRecord, TaskStatus  # noqa: E402


class _StubClient:
    """Stands in for TelegramClient: records save_image/send calls, no network."""

    def __init__(self, fail: bool = False) -> None:
        self.saved: list = []
        self.sent: list = []
        self.docs: list = []
        self.photos: list = []
        self.fail = fail

    def save_image(self, message, dest_dir, stem):  # noqa: ANN001
        if self.fail:
            raise TelegramError("simulated download failure")
        self.saved.append(stem)
        return "/tmp/uploads/%s.jpg" % stem

    def send_message(self, chat_id, text, parse_mode="Markdown", reply_markup=None):  # noqa: ANN001
        self.sent.append((str(chat_id), text))
        return {}

    def send_document(self, chat_id, document, filename=None, caption=None):  # noqa: ANN001
        content = bytes(document) if isinstance(document, (bytes, bytearray)) else document
        self.docs.append((str(chat_id), filename, caption, content))
        return {}

    def send_photo(self, chat_id, photo, filename=None, caption=None):  # noqa: ANN001
        content = bytes(photo) if isinstance(photo, (bytes, bytearray)) else photo
        self.photos.append((str(chat_id), filename, caption, content))
        return {}


def _make_bot(tmp_path, admin=None, fail=False):  # noqa: ANN001
    """A ConductorBot whose manager.submit/submit_auto are captured, not run."""
    mgr = ConductorManager(ConductorConfig(repo_root=tmp_path))
    calls: list = []

    def fake_submit(engine, task, chat_id="", images=None):  # noqa: ANN001
        calls.append({"kind": "submit", "engine": engine, "task": task, "images": images})
        return TaskRecord(id=7, engine=engine, task=task, branch="conductor/x",
                          status=TaskStatus.QUEUED.value)

    def fake_submit_auto(task, chat_id="", images=None):  # noqa: ANN001
        calls.append({"kind": "auto", "task": task, "images": images})
        return TaskRecord(id=8, engine="claude", task=task, branch="conductor/y",
                          status=TaskStatus.QUEUED.value, route_reason="auto")

    mgr.submit = fake_submit            # type: ignore[assignment]
    mgr.submit_auto = fake_submit_auto  # type: ignore[assignment]
    client = _StubClient(fail=fail)
    bot = wca_conductor.ConductorBot(client, mgr, allowed=set(), admin=admin)
    return bot, client, calls


def _photo_msg(caption: str | None = None, file_id: str = "big",
               media_group_id: str | None = None, user_id: int = 1) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "photo": [{"file_id": file_id, "width": 1280, "height": 720, "file_size": 9000}],
        "chat": {"id": 1},
        "from": {"id": user_id},
    }
    if caption is not None:
        msg["caption"] = caption
    if media_group_id is not None:
        msg["media_group_id"] = media_group_id
    return msg


class TestConductorImageRouting:
    def test_photo_with_claude_caption_dispatches_with_image(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        reply = bot.handle(_photo_msg("/claude debug this layout"))
        assert "dispatched" in reply and "📎" in reply
        assert len(calls) == 1
        assert calls[0] == {"kind": "submit", "engine": "claude",
                            "task": "debug this layout", "images": ["/tmp/uploads/%s.jpg" % client.saved[0]]}

    def test_photo_with_plain_caption_auto_routes_with_image(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        reply = bot.handle(_photo_msg("fix the broken header"))
        assert "routed" in reply and "📎" in reply
        assert calls[0]["kind"] == "auto"
        assert calls[0]["task"] == "fix the broken header"
        assert calls[0]["images"] == ["/tmp/uploads/%s.jpg" % client.saved[0]]

    def test_photo_without_caption_asks_for_instruction(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        reply = bot.handle(_photo_msg(None))
        assert reply.startswith("📎 Got your screenshot")
        assert calls == []            # nothing dispatched
        assert client.saved == []     # and nothing downloaded

    def test_image_document_with_caption_dispatches(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        msg = {
            "document": {"file_id": "d", "mime_type": "image/png", "file_name": "bug.png"},
            "caption": "/claude what's wrong here",
            "chat": {"id": 1}, "from": {"id": 1},
        }
        reply = bot.handle(msg)
        assert "dispatched" in reply
        assert calls[0]["images"] == ["/tmp/uploads/%s.jpg" % client.saved[0]]

    def test_text_command_does_not_download_images(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        reply = bot.handle({"text": "/status", "chat": {"id": 1}, "from": {"id": 1}})
        assert reply is not None
        assert client.saved == []     # no image work on a plain command
        assert calls == []

    def test_plain_text_without_command_is_ignored(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        assert bot.handle({"text": "hello", "chat": {"id": 1}, "from": {"id": 1}}) is None
        assert calls == []

    # -- hardening: download failure, admin gate, albums --------------------

    def test_download_failure_warns_and_dispatches_without_image(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path, fail=True)
        reply = bot.handle(_photo_msg("/claude debug this"))
        assert "couldn't download" in reply           # user is told
        assert calls[0]["images"] == []               # dispatched without the image
        assert client.saved == []                     # (save_image raised)

    def test_non_admin_is_rejected_before_any_download(self, tmp_path) -> None:
        # admin set to a different user id than the sender (1)
        bot, client, calls = _make_bot(tmp_path, admin="999")
        reply = bot.handle(_photo_msg("/claude debug this", user_id=1))
        assert "Not authorized" in reply
        assert calls == []                            # no dispatch
        assert client.saved == []                     # crucially, no wasted download

    def test_album_groups_members_into_one_task(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        members = [
            _photo_msg("/claude compare these two screens", file_id="a", media_group_id="G"),
            _photo_msg(None, file_id="b", media_group_id="G"),
        ]
        bot._handle_album(members)
        assert len(calls) == 1                        # ONE task, not two
        assert calls[0]["engine"] == "claude"
        assert calls[0]["task"] == "compare these two screens"
        assert len(calls[0]["images"]) == 2           # both screenshots attached
        # exactly one reply, mentioning both screenshots
        assert len(client.sent) == 1
        assert "2 screenshots" in client.sent[0][1]

    def test_album_without_caption_asks_once_and_downloads_nothing(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        members = [
            _photo_msg(None, file_id="a", media_group_id="G"),
            _photo_msg(None, file_id="b", media_group_id="G"),
        ]
        bot._handle_album(members)
        assert calls == []                            # nothing dispatched
        assert client.saved == []                     # no wasted downloads
        assert len(client.sent) == 1                  # a single "add a caption" hint
        assert client.sent[0][1].startswith("📎 Got your screenshot")

    def test_album_override_pluralizes_attach_note(self, tmp_path) -> None:
        bot, client, calls = _make_bot(tmp_path)
        reply = bot.handle(_photo_msg("/claude x"), images_override=["/a.png", "/b.png", "/c.png"])
        assert "3 screenshots attached" in reply
        assert calls[0]["images"] == ["/a.png", "/b.png", "/c.png"]


class TestUploadsPrune:
    def test_prunes_old_files_keeps_recent(self, tmp_path) -> None:
        import os
        bot, _client, _calls = _make_bot(tmp_path)
        d = bot._uploads_dir()
        d.mkdir(parents=True, exist_ok=True)
        old = d / "old.png"
        old.write_bytes(b"x")
        recent = d / "recent.png"
        recent.write_bytes(b"y")
        # age the 'old' file well past the 24h cutoff
        old_ts = __import__("time").time() - 48 * 3600
        os.utime(old, (old_ts, old_ts))
        bot._prune_uploads(max_age_hours=24.0)
        assert not old.exists()
        assert recent.exists()


# ---------------------------------------------------------------------------
# TelegramClient.send_document / send_photo (multipart upload)
# ---------------------------------------------------------------------------

class TestSendFile:
    def test_send_document_from_bytes(self) -> None:
        client = _make_client()
        client._session.post = MagicMock(
            return_value=_fake_post_response({"ok": True, "result": {"message_id": 5}})
        )
        res = client.send_document(1, b"# report\nfindings", filename="r.md", caption="cap")
        assert res["message_id"] == 5
        url, kwargs = client._session.post.call_args[0][0], client._session.post.call_args[1]
        assert "sendDocument" in url
        assert kwargs["data"]["chat_id"] == "1"
        assert kwargs["data"]["caption"] == "cap"
        assert "document" in kwargs["files"]  # multipart file part

    def test_send_photo_from_path(self, tmp_path) -> None:
        p = tmp_path / "chart.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nx")
        client = _make_client()
        client._session.post = MagicMock(
            return_value=_fake_post_response({"ok": True, "result": {"message_id": 7}})
        )
        res = client.send_photo(1, str(p))
        assert res["message_id"] == 7
        url, kwargs = client._session.post.call_args[0][0], client._session.post.call_args[1]
        assert "sendPhoto" in url and "photo" in kwargs["files"]

    def test_send_document_surfaces_api_error(self) -> None:
        client = _make_client()
        client._session.post = MagicMock(
            return_value=_fake_post_response({"ok": False, "error_code": 413, "description": "Too Big"})
        )
        with pytest.raises(TelegramError, match="413"):
            client.send_document(1, b"x", filename="x.bin")


# ---------------------------------------------------------------------------
# ConductorBot — report files + usage chart
# ---------------------------------------------------------------------------

def _git_repo_with_report(tmp_path) -> Path:
    """A repo with a `main` branch and a `conductor/x` task branch that adds a
    .md report + .png chart (kept) plus code + a site/ file (must be excluded)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    g("add", "-A"); g("commit", "-qm", "seed")
    g("checkout", "-qb", "conductor/x")
    (repo / "report.md").write_text("# Report\nfindings here")
    (repo / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"fakepngdata")
    (repo / "code.py").write_text("print(1)")                 # excluded: not a report ext
    (repo / "site").mkdir()
    (repo / "site" / "data.json").write_text("{}")            # excluded: generated feed
    g("add", "-A"); g("commit", "-qm", "task")
    g("checkout", "-q", "main")
    return repo


def _report_bot(repo):
    mgr = ConductorManager(ConductorConfig(repo_root=repo, base_branch="main"))
    client = _StubClient()
    bot = wca_conductor.ConductorBot(client, mgr, allowed=set(), admin=None)
    return bot, client, mgr


class TestTaskReportFiles:
    def test_filters_to_report_files_and_reads_content(self, tmp_path) -> None:
        repo = _git_repo_with_report(tmp_path)
        bot, _client, _mgr = _report_bot(repo)
        rec = TaskRecord(id=3, engine="claude", task="t", branch="conductor/x")
        files = bot._task_report_files(rec)
        by = {name: (content, is_image) for name, content, is_image in files}
        assert set(by) == {"report.md", "chart.png"}     # code.py + site/data.json excluded
        assert by["chart.png"][1] is True                # routed as image
        assert by["report.md"][1] is False
        assert b"findings here" in by["report.md"][0]     # content read from the branch

    def test_send_report_files_routes_images_and_docs(self, tmp_path) -> None:
        repo = _git_repo_with_report(tmp_path)
        bot, client, _mgr = _report_bot(repo)
        rec = TaskRecord(id=3, engine="claude", task="t", branch="conductor/x")
        n = bot._send_report_files("1", rec)
        assert n == 2
        assert [p[1] for p in client.photos] == ["chart.png"]
        assert [d[1] for d in client.docs] == ["report.md"]

    def test_report_command_sends_files(self, tmp_path) -> None:
        repo = _git_repo_with_report(tmp_path)
        bot, client, mgr = _report_bot(repo)
        mgr._records[3] = TaskRecord(id=3, engine="claude", task="t", branch="conductor/x")
        reply = bot.handle({"text": "/report 3", "chat": {"id": 1}, "from": {"id": 1}})
        assert reply is None                              # the files ARE the reply
        assert len(client.photos) + len(client.docs) == 2

    def test_report_command_no_files(self, tmp_path) -> None:
        b, _client, _calls = _make_bot(tmp_path)
        b.manager._records[4] = TaskRecord(id=4, engine="claude", task="t", branch=None)
        reply = b.handle({"text": "/report 4", "chat": {"id": 1}, "from": {"id": 1}})
        assert "No report files" in reply

    def test_no_branch_returns_nothing(self, tmp_path) -> None:
        bot, _client, _calls = _make_bot(tmp_path)
        assert bot._task_report_files(TaskRecord(id=9, engine="claude", task="t", branch=None)) == []


class TestUsageChart:
    def test_chart_command(self, tmp_path) -> None:
        bot, client, _calls = _make_bot(tmp_path)
        bot.manager._records[1] = TaskRecord(
            id=1, engine="claude", task="t", tokens=500, status=TaskStatus.DONE.value
        )
        reply = bot.handle({"text": "/chart", "chat": {"id": 1}, "from": {"id": 1}})
        try:
            import matplotlib  # noqa: F401
            assert reply is None                          # a photo was sent
            assert len(client.photos) == 1
            assert client.photos[0][1] == "conductor_usage.png"
            assert client.photos[0][3][:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes
        except ImportError:
            assert reply is not None and "matplotlib" in reply
