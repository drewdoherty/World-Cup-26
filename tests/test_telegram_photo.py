"""Tests for TelegramClient photo-download helpers.

No real network calls are made; ``requests.Session`` methods are monkeypatched
with lightweight fakes.
"""
from __future__ import annotations

import json
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
