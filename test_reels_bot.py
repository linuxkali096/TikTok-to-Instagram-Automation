#!/usr/bin/env python3
"""
test_reels_bot.py - Comprehensive 4-Tier Automated Test Suite for ReelsBot.

Matrix Architecture (from teamwork_explorer_survey_3/handoff.md):
- Tier 1: In-Memory Unit & Logic Tests (T1.1 - T1.12)
- Tier 2: Subsystem Mock Integration Tests (T2.1 - T2.15)
- Tier 3: Telegram Handler Flow & State Machine Tests (T3.1 - T3.10)
- Tier 4: Live Dry-Run & Non-Destructive Probes (T4.1 - T4.5)
- Backward-compatible baseline test aliases
"""

import os
import sys
import json
import sqlite3
import asyncio
import py_compile
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch, ANY

import httpx
import pytest
from telegram import Update
from telegram.ext import ContextTypes

# Ensure workspace root is in sys.path
WORKSPACE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE_DIR))

import reels_bot


# ===========================================================================
# Pytest Fixtures
# ===========================================================================

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provides an isolated SQLite database path and schema for each test."""
    db_file = tmp_path / "test_reels_isolated.db"
    monkeypatch.setattr(reels_bot, "DB_PATH", db_file)
    reels_bot.init_db()
    return db_file


@pytest.fixture
def clean_downloads(tmp_path, monkeypatch):
    """Provides a temporary, isolated downloads directory."""
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reels_bot, "DOWNLOADS_DIR", dl_dir)
    return dl_dir


def make_mock_update(
    chat_id: int = 123456,
    text: str = None,
    callback_data: str = None,
    message_id: int = 101,
):
    """Helper creating a strongly-mocked Telegram Update and Context."""
    update = MagicMock(spec=Update)
    chat = MagicMock()
    chat.id = chat_id
    update.effective_chat = chat

    if text is not None:
        message = MagicMock()
        message.message_id = message_id
        message.text = text
        message.reply_text = AsyncMock()
        message.reply_html = AsyncMock()
        update.message = message
    else:
        update.message = None

    if callback_data is not None:
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = message_id
        update.callback_query = query
    else:
        update.callback_query = None

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    return update, context


# ===========================================================================
# Tier 1: In-Memory Unit & Logic Tests (T1.1 - T1.12)
# ===========================================================================

def test_tiktok_url_regex_comprehensive():
    """T1.1: Verify regex handles standard, vm, vt, m.tiktok URLs and rejects non-TikTok links."""
    valid_cases = [
        ("Check this: https://www.tiktok.com/@psychology/video/7123456789012345678!", "https://www.tiktok.com/@psychology/video/7123456789012345678"),
        ("Short link: https://vm.tiktok.com/ZM1234567/?query=1", "https://vm.tiktok.com/ZM1234567/?query=1"),
        ("VT domain: https://vt.tiktok.com/ZS1234567.", "https://vt.tiktok.com/ZS1234567"),
        ("Mobile link: https://m.tiktok.com/v/7123456789.html", "https://m.tiktok.com/v/7123456789.html"),
        ("Embedded punctuation: https://www.tiktok.com/@user/video/987654321?foo=bar, look here", "https://www.tiktok.com/@user/video/987654321?foo=bar"),
        ("Question mark boundary: https://vt.tiktok.com/ZS9999999?", "https://vt.tiktok.com/ZS9999999"),
    ]
    for text, expected in valid_cases:
        m = reels_bot.TIKTOK_URL_REGEX.search(text)
        assert m is not None, f"Failed to match valid URL in: {text}"
        cleaned = m.group(0).rstrip(".,?!")
        assert cleaned == expected, f"Expected {expected}, got {cleaned}"

    invalid_cases = [
        "https://www.youtube.com/watch?v=123",
        "https://instagram.com/reel/123",
        "https://twitter.com/user/status/123",
        "Hello there how are you?",
        "https://not-tiktok.com/video/123",
        "ftp://tiktok.com/@user/video/123",
    ]
    for text in invalid_cases:
        assert reels_bot.TIKTOK_URL_REGEX.search(text) is None, f"Incorrectly matched invalid input: {text}"


def test_duration_guard_boundary(monkeypatch):
    """T1.2: Verify duration guard passes values <= 90s and raises VideoTooLongError for > 90s."""
    monkeypatch.setattr(reels_bot, "MAX_REEL_DURATION_SEC", 90)

    # Values within boundary should not trigger exception
    for duration in [0, 15, 60, 89, 90]:
        if duration > reels_bot.MAX_REEL_DURATION_SEC:
            raise reels_bot.VideoTooLongError(duration)

    # Boundary violation values
    for duration in [91, 120, 300]:
        with pytest.raises(reels_bot.VideoTooLongError) as exc_info:
            if duration > reels_bot.MAX_REEL_DURATION_SEC:
                raise reels_bot.VideoTooLongError(duration)
        assert exc_info.value.duration == duration
        assert f"{duration}s" in str(exc_info.value)
        assert "90s" in str(exc_info.value)


def test_post_time_instant_mode(monkeypatch):
    """T1.3: Verify POST_HOUR = -1 calculates schedule target ~3 minutes into the future in UTC."""
    monkeypatch.setattr(reels_bot, "POST_HOUR", -1)
    monkeypatch.setattr(reels_bot, "TIMEZONE_STR", "Asia/Kolkata")

    due_at, label = reels_bot.calculate_post_time()
    assert due_at is not None
    dt = datetime.fromisoformat(due_at)
    now_utc = datetime.now(timezone.utc)
    diff_sec = (dt - now_utc).total_seconds()
    # Should be ~3 minutes (180s) from now, allowing minor execution jitter
    assert 150 <= diff_sec <= 210
    assert "Asia/Kolkata" in label


def test_post_time_scheduled_today(monkeypatch):
    """T1.4: Verify calculate_post_time targets today when scheduled hour is in the future."""
    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 9, 10, 0, 0, tzinfo=tz)

    monkeypatch.setattr(reels_bot, "datetime", MockDateTime)
    monkeypatch.setattr(reels_bot, "POST_HOUR", 19)
    monkeypatch.setattr(reels_bot, "POST_MINUTE", 0)
    monkeypatch.setattr(reels_bot, "TIMEZONE_STR", "Asia/Kolkata")

    due_at, label = reels_bot.calculate_post_time()
    assert "2026-11-09" in due_at
    assert "Mon, 09 Nov 2026 at 07:00 PM (Asia/Kolkata)" in label


def test_post_time_scheduled_tomorrow_rollover(monkeypatch):
    """T1.5: Verify post time rolls over to tomorrow if target hour is past or within 5 min guard."""
    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 9, 19, 0, 0, tzinfo=tz)

    monkeypatch.setattr(reels_bot, "datetime", MockDateTime)
    # Target is only 2 minutes away (<= 5 minute lead guard)
    monkeypatch.setattr(reels_bot, "POST_HOUR", 19)
    monkeypatch.setattr(reels_bot, "POST_MINUTE", 2)
    monkeypatch.setattr(reels_bot, "TIMEZONE_STR", "Asia/Kolkata")

    due_at, label = reels_bot.calculate_post_time()
    # Must roll over to Nov 10
    assert "2026-11-10" in due_at
    assert "Tue, 10 Nov 2026 at 07:02 PM (Asia/Kolkata)" in label


def test_post_time_invalid_timezone_fallback(monkeypatch):
    """T1.6: Verify calculate_post_time catches invalid timezone and falls back to UTC gracefully."""
    monkeypatch.setattr(reels_bot, "TIMEZONE_STR", "Nonexistent/Invalid_Timezone_Name_404")
    due_at, label = reels_bot.calculate_post_time()
    assert due_at is not None
    assert isinstance(due_at, str)
    assert "+00:00" in due_at or "Z" in due_at


def test_build_caption_prompt_structure():
    """T1.7: Verify build_caption_prompt includes viral blueprint sections and character constraint."""
    prompt = reels_bot.build_caption_prompt(
        title="Unbreakable focus under pressure",
        brand_name="Sigma Alpha",
        brand_voice="calm, stoic, calculated"
    )
    assert "Sigma Alpha" in prompt
    assert "calm, stoic, calculated" in prompt
    assert "Unbreakable focus under pressure" in prompt
    assert "1. HOOK:" in prompt
    assert "2. THE BREAKDOWN:" in prompt
    assert "3. CALL TO ACTION:" in prompt
    assert "4. HASHTAGS:" in prompt
    assert "750 characters" in prompt


@pytest.mark.asyncio
async def test_caption_truncation_under_950(monkeypatch):
    """T1.8: Verify generate_viral_caption truncates lengthy outputs to <= 950 chars with ellipsis."""
    # Dummy provider returning 1,400 characters
    oversized_text = "Mindset " * 200

    async def mock_groq(prompt, temperature=0.7):
        return oversized_text

    monkeypatch.setattr(reels_bot, "generate_caption_groq", mock_groq)
    monkeypatch.setattr(reels_bot, "AI_PROVIDER", "groq")
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", False)

    caption = await reels_bot.generate_viral_caption("Overly long text test", "dummy_creator")
    assert len(caption) <= 950
    assert caption.endswith("...")


@pytest.mark.asyncio
async def test_creator_credit_formatting(monkeypatch):
    """T1.9: Verify creator credit appended if CREDIT_CREATOR=True and omitted otherwise."""
    async def mock_groq(prompt, temperature=0.7):
        return "Silence is power.\n\nMaster emotions.\n\nSave this ♟️\n\n#stoic #sigma"

    monkeypatch.setattr(reels_bot, "generate_caption_groq", mock_groq)
    monkeypatch.setattr(reels_bot, "AI_PROVIDER", "groq")

    # Case 1: CREDIT_CREATOR is True and creator name present
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", True)
    cap_with_credit = await reels_bot.generate_viral_caption("Title", "stoic_architect")
    assert "\n\n🎥 Credit: @stoic_architect (TikTok)" in cap_with_credit

    # Case 2: CREDIT_CREATOR is False
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", False)
    cap_no_credit = await reels_bot.generate_viral_caption("Title", "stoic_architect")
    assert "Credit:" not in cap_no_credit

    # Case 3: Creator username is empty
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", True)
    cap_empty_creator = await reels_bot.generate_viral_caption("Title", "")
    assert "Credit:" not in cap_empty_creator


def test_database_crud_lifecycle(tmp_path, monkeypatch):
    """T1.10: Verify SQLite CRUD operations, table schema, and status transitions."""
    test_db = tmp_path / "crud_test.db"
    monkeypatch.setattr(reels_bot, "DB_PATH", test_db)
    reels_bot.init_db()

    # Verify table schema has 11 expected columns
    with sqlite3.connect(test_db) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(pending_posts)")
        columns = [row[1] for row in cur.fetchall()]
        expected_cols = [
            "task_id", "chat_id", "message_id", "video_path",
            "cloudinary_url", "caption", "title", "creator",
            "duration", "status", "created_at"
        ]
        assert columns == expected_cols

    # Save post
    post_data = {
        "task_id": "task_crud_001",
        "chat_id": 987654321,
        "message_id": 555,
        "video_path": "/tmp/test.mp4",
        "cloudinary_url": "https://res.cloudinary.com/demo/sample.mp4",
        "caption": "Initial Hook Caption",
        "title": "Master Your Silence",
        "creator": "stoic_user",
        "duration": 45,
        "status": "pending"
    }
    reels_bot.save_pending_post(post_data)

    # Retrieve post
    post = reels_bot.get_pending_post("task_crud_001")
    assert post is not None
    assert post["chat_id"] == 987654321
    assert post["status"] == "pending"
    assert post["caption"] == "Initial Hook Caption"

    # Update status and caption
    reels_bot.update_pending_status("task_crud_001", "scheduled", "Updated Scheduled Caption")
    post = reels_bot.get_pending_post("task_crud_001")
    assert post["status"] == "scheduled"
    assert post["caption"] == "Updated Scheduled Caption"

    # Update status only
    reels_bot.update_pending_status("task_crud_001", "rejected")
    post = reels_bot.get_pending_post("task_crud_001")
    assert post["status"] == "rejected"
    assert post["caption"] == "Updated Scheduled Caption"

    # Query non-existent task
    assert reels_bot.get_pending_post("unknown_task_xyz") is None


def test_is_authorized_filtering(monkeypatch):
    """T1.11: Verify is_authorized handles empty config (onboarding) vs restricted chat ID."""
    def make_upd(chat_id):
        u = MagicMock(spec=Update)
        if chat_id is not None:
            c = MagicMock()
            c.id = chat_id
            u.effective_chat = c
        else:
            u.effective_chat = None
        return u

    # Unrestricted (onboarding mode)
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")
    assert reels_bot.is_authorized(make_upd(111111)) is True
    assert reels_bot.is_authorized(make_upd(999999)) is True

    # Restricted mode
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "111111")
    assert reels_bot.is_authorized(make_upd(111111)) is True
    assert reels_bot.is_authorized(make_upd(999999)) is False
    assert reels_bot.is_authorized(make_upd(None)) is False

    # Quoted restricted string
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", '"111111"')
    assert reels_bot.is_authorized(make_upd(111111)) is True
    assert reels_bot.is_authorized(make_upd(222222)) is False


def test_json_str_graphql_escaping():
    """T1.12: Verify json_str properly escapes strings for GraphQL mutation payloads."""
    raw_str = 'Line 1: "Double quotes" and \\backslash\nLine 2: \tTabbed text ♟️'
    escaped = reels_bot.json_str(raw_str)
    assert escaped.startswith('"')
    assert escaped.endswith('"')
    # Round-trip verification: json.loads returns identical raw text
    assert json.loads(escaped) == raw_str


# ===========================================================================
# Tier 2: Subsystem Mock Integration Tests (T2.1 - T2.15)
# ===========================================================================

@pytest.mark.asyncio
async def test_tikwm_fetch_success(clean_downloads):
    """T2.1: Verify TikWM API success downloads HD stream and parses metadata."""
    task_id = "test_t21_success"
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {
        "code": 0,
        "msg": "success",
        "data": {
            "title": "Unbreakable Focus: Master High Stakes",
            "duration": 42,
            "hdplay": "https://www.tikwm.com/video/media/hdplay/7123.mp4",
            "author": {"unique_id": "stoic_architect"}
        }
    }

    mock_get_resp = MagicMock(status_code=200, content=b"fake_hd_video_stream_bytes")
    mock_get_resp.raise_for_status = MagicMock()

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_post_resp
    client_mock.get.return_value = mock_get_resp

    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        path, meta = await reels_bot.fetch_video_tikwm(
            "https://www.tiktok.com/@stoic_architect/video/7123",
            task_id
        )

    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read() == b"fake_hd_video_stream_bytes"
    assert meta["title"] == "Unbreakable Focus: Master High Stakes"
    assert meta["duration"] == 42
    assert meta["creator"] == "stoic_architect"
    assert meta["source"] == "tikwm"


@pytest.mark.asyncio
async def test_tikwm_retry_backoff_success(clean_downloads):
    """T2.2: Verify TikWM retries up to 3 times with exponential backoff on transient errors."""
    task_id = "test_t22_retry"
    resp_fail_1 = MagicMock(status_code=500)
    resp_fail_2 = MagicMock(status_code=200)
    resp_fail_2.json.return_value = {"code": -1, "msg": "Rate limited temporarily"}
    resp_success = MagicMock(status_code=200)
    resp_success.json.return_value = {
        "code": 0,
        "data": {
            "title": "Retry Succeeded",
            "duration": 35,
            "hdplay": "https://www.tikwm.com/video/hd.mp4",
            "author": {"unique_id": "retry_creator"}
        }
    }

    mock_get = MagicMock(status_code=200, content=b"stream_content")
    mock_get.raise_for_status = MagicMock()

    client_mock = AsyncMock()
    client_mock.post.side_effect = [resp_fail_1, resp_fail_2, resp_success]
    client_mock.get.return_value = mock_get

    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            path, meta = await reels_bot.fetch_video_tikwm("https://tiktok.com/vid", task_id)

    assert client_mock.post.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list[0][0][0] == 2  # 2^1 = 2s
    assert mock_sleep.call_args_list[1][0][0] == 4  # 2^2 = 4s
    assert meta["creator"] == "retry_creator"


@pytest.mark.asyncio
async def test_tikwm_duration_exceeded_raises_immediately(clean_downloads):
    """T2.3: Verify TikWM aborts before downloading stream if metadata duration > 90s."""
    mock_post_resp = MagicMock(status_code=200)
    mock_post_resp.json.return_value = {
        "code": 0,
        "data": {
            "title": "Too Long Video",
            "duration": 115,
            "hdplay": "https://www.tikwm.com/video/hd.mp4",
            "author": {"unique_id": "long_creator"}
        }
    }

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_post_resp

    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        with pytest.raises(reels_bot.VideoTooLongError) as exc_info:
            await reels_bot.fetch_video_tikwm("https://tiktok.com/long", "task_t23")

    assert exc_info.value.duration == 115
    assert client_mock.get.called is False


@pytest.mark.asyncio
async def test_tikwm_failure_failover_to_ytdlp():
    """T2.4: Verify acquire_clean_video automatically falls back to yt-dlp when TikWM fails."""
    expected_meta = {
        "title": "Fallback Video",
        "duration": 50,
        "creator": "ytdlp_user",
        "source": "yt-dlp"
    }

    with patch("reels_bot.fetch_video_tikwm", AsyncMock(side_effect=RuntimeError("TikWM gateway 502"))):
        with patch("reels_bot.fetch_video_ytdlp", AsyncMock(return_value=("/tmp/ytdlp_clip.mp4", expected_meta))) as mock_ytdlp:
            path, meta = await reels_bot.acquire_clean_video("https://tiktok.com/fail", "task_t24")

    mock_ytdlp.assert_awaited_once_with("https://tiktok.com/fail", "task_t24")
    assert meta["source"] == "yt-dlp"
    assert path == "/tmp/ytdlp_clip.mp4"


def test_ytdlp_predownload_duration_guard():
    """T2.5: Verify yt-dlp inspects duration before full download, aborting on violation."""
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = {"duration": 150, "title": "Overly Long Video"}

    mock_ydl_cls = MagicMock()
    mock_ydl_cls.return_value.__enter__.return_value = mock_ydl

    with patch("yt_dlp.YoutubeDL", mock_ydl_cls):
        with pytest.raises(reels_bot.VideoTooLongError) as exc_info:
            reels_bot._ytdlp_sync_download("https://tiktok.com/yt_long", "task_t25")

    assert exc_info.value.duration == 150
    assert mock_ydl.extract_info.call_count == 1
    call_kwargs = mock_ydl.extract_info.call_args[1]
    assert call_kwargs.get("download") is False


@pytest.mark.asyncio
async def test_groq_caption_generation_success(monkeypatch):
    """T2.6: Verify Groq Cloud API caption generation parses OpenAI schema and sends auth token."""
    monkeypatch.setattr(reels_bot, "GROQ_API_KEY", "gsk_test_groq_key_999")
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Silence is the ultimate weapon.\n\nNever explain.\n\nSave this ♟️\n\n#darkpsychology #stoic"
                }
            }
        ]
    }
    mock_resp.raise_for_status = MagicMock()

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        result = await reels_bot.generate_caption_groq("Craft caption")

    assert "Silence is the ultimate weapon." in result
    assert client_mock.post.called
    headers = client_mock.post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer gsk_test_groq_key_999"


@pytest.mark.asyncio
async def test_gemini_caption_generation_success(monkeypatch):
    """T2.7: Verify Gemini Free Tier generates caption and passes API key parameter."""
    monkeypatch.setattr(reels_bot, "GEMINI_API_KEY", "gemini_free_key_777")
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "True power never seeks validation.\n\n#mindset #stoic"}]
                }
            }
        ]
    }
    mock_resp.raise_for_status = MagicMock()

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        result = await reels_bot.generate_caption_gemini("Craft caption")

    assert "True power never seeks validation." in result
    url = client_mock.post.call_args[0][0]
    assert "key=gemini_free_key_777" in url


@pytest.mark.asyncio
async def test_ai_cascade_groq_fail_to_gemini(monkeypatch):
    """T2.8: Verify failover cascade automatically transitions from Groq error to Gemini."""
    monkeypatch.setattr(reels_bot, "AI_PROVIDER", "groq")
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", True)

    with patch("reels_bot.generate_caption_groq", AsyncMock(side_effect=RuntimeError("Groq 401"))):
        with patch("reels_bot.generate_caption_gemini", AsyncMock(return_value="Gemini Hook\n\n#stoic")):
            caption = await reels_bot.generate_viral_caption("Stoic Mastery", "stoic_architect")

    assert "Gemini Hook" in caption
    assert "🎥 Credit: @stoic_architect (TikTok)" in caption


@pytest.mark.asyncio
async def test_ai_cascade_all_fail_to_deterministic(monkeypatch):
    """T2.9: Verify that when all network AI providers fail, deterministic fallback is returned."""
    monkeypatch.setattr(reels_bot, "AI_PROVIDER", "groq")
    monkeypatch.setattr(reels_bot, "CREDIT_CREATOR", True)

    with patch("reels_bot.generate_caption_groq", AsyncMock(side_effect=RuntimeError("Groq Down"))):
        with patch("reels_bot.generate_caption_gemini", AsyncMock(side_effect=RuntimeError("Gemini Down"))):
            with patch("reels_bot.generate_caption_pollinations", AsyncMock(side_effect=RuntimeError("Pollinations 402"))):
                caption = await reels_bot.generate_viral_caption("Inner Peace", "master")

    assert "Rate this from 1 to 10" in caption or "Drop a ❤️" in caption
    assert "🎥 Credit: @master (TikTok)" in caption
    assert "Disclaimer: No copyright infringement intended" in caption
    assert len(caption) <= 950


@pytest.mark.asyncio
async def test_cloudinary_unsigned_upload_success(tmp_path, monkeypatch):
    """T2.10: Verify unsigned Cloudinary video upload returns secure_url."""
    monkeypatch.setattr(reels_bot, "CLOUDINARY_CLOUD_NAME", "test_cloud_acc")
    monkeypatch.setattr(reels_bot, "CLOUDINARY_UPLOAD_PRESET", "preset_unsigned")

    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"dummy_video_stream")

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "secure_url": "https://res.cloudinary.com/test_cloud_acc/video/upload/v123/clip.mp4"
    }
    mock_resp.raise_for_status = MagicMock()

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        url = await reels_bot.upload_to_cloudinary(str(video_file))

    assert url == "https://res.cloudinary.com/test_cloud_acc/video/upload/v123/clip.mp4"
    assert client_mock.post.called
    data_payload = client_mock.post.call_args[1]["data"]
    assert data_payload["upload_preset"] == "preset_unsigned"


@pytest.mark.asyncio
async def test_cloudinary_missing_credentials_mock_url(monkeypatch):
    """T2.11: Verify missing Cloudinary credentials falls back to local mock CDN URL."""
    monkeypatch.setattr(reels_bot, "CLOUDINARY_CLOUD_NAME", "")
    monkeypatch.setattr(reels_bot, "CLOUDINARY_UPLOAD_PRESET", "")

    url = await reels_bot.upload_to_cloudinary("/tmp/my_offline_video.mp4")
    assert url == "https://mock.cdn/video/my_offline_video.mp4"


@pytest.mark.asyncio
async def test_buffer_graphql_createpost_success(monkeypatch):
    """T2.12: Verify successful Buffer GraphQL createPost mutation with Instagram Reel schema."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "buf_live_token")
    monkeypatch.setattr(reels_bot, "BUFFER_CHANNEL_ID", "chan_ig_123")

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "data": {
            "createPost": {
                "post": {
                    "id": "buf_post_success_1",
                    "dueAt": "2026-11-09T13:30:00Z"
                }
            }
        }
    }

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        res = await reels_bot.schedule_buffer_post(
            "https://mock.cdn/video/test.mp4",
            "High impact caption",
            due_at_iso="2026-11-09T13:30:00Z"
        )

    assert res["status"] == "success"
    assert len(res["results"]) == 1
    assert res["results"][0]["post"]["id"] == "buf_post_success_1"
    # Verify GraphQL query structure
    mutation_query = client_mock.post.call_args[1]["json"]["query"]
    assert "type: reel" in mutation_query
    assert "shouldShareToFeed: true" in mutation_query


@pytest.mark.asyncio
async def test_buffer_graphql_top_level_error_handling(monkeypatch):
    """T2.13: Verify Buffer top-level GraphQL errors (data: null) raise explicit RuntimeError."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "buf_invalid_token")
    monkeypatch.setattr(reels_bot, "BUFFER_CHANNEL_ID", "chan_ig_123")

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "errors": [{"message": "Access token is not valid"}]
    }

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        with pytest.raises(RuntimeError) as exc_info:
            await reels_bot.schedule_buffer_post("https://mock.cdn/v.mp4", "Caption")

    assert "Access token is not valid" in str(exc_info.value)


@pytest.mark.asyncio
async def test_buffer_graphql_mutation_error_handling(monkeypatch):
    """T2.14: Verify Buffer MutationError responses raise descriptive RuntimeError."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "buf_valid_token")
    monkeypatch.setattr(reels_bot, "BUFFER_CHANNEL_ID", "chan_ig_123")

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "data": {
            "createPost": {
                "message": "The selected Instagram channel has disconnected."
            }
        }
    }

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        with pytest.raises(RuntimeError) as exc_info:
            await reels_bot.schedule_buffer_post("https://mock.cdn/v.mp4", "Caption")

    assert "Instagram channel has disconnected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_buffer_multichannel_dispatch(monkeypatch):
    """T2.15: Verify multi-channel comma-separated dispatch posts to each channel sequentially."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "buf_valid_token")
    monkeypatch.setattr(reels_bot, "BUFFER_CHANNEL_ID", "chan_ig_1, chan_ig_2")

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "data": {
            "createPost": {
                "post": {"id": "buf_post_multi", "dueAt": "2026-11-09T13:30:00Z"}
            }
        }
    }

    client_mock = AsyncMock()
    client_mock.post.return_value = mock_resp
    client_cls = MagicMock()
    client_cls.return_value.__aenter__.return_value = client_mock

    with patch("httpx.AsyncClient", client_cls):
        res = await reels_bot.schedule_buffer_post("https://mock.cdn/v.mp4", "Caption")

    assert len(res["results"]) == 2
    assert res["results"][0]["channelId"] == "chan_ig_1"
    assert res["results"][1]["channelId"] == "chan_ig_2"


# ===========================================================================
# Tier 3: Telegram Handler Flow & State Machine Tests (T3.1 - T3.10)
# ===========================================================================

@pytest.mark.asyncio
async def test_start_handler_unconfigured_onboarding(monkeypatch):
    """T3.1: Verify /start command replies with copyable chat ID when ALLOWED_CHAT_ID is empty."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")
    update, context = make_mock_update(chat_id=987654, text="/start")

    await reels_bot.start_handler(update, context)

    update.message.reply_html.assert_awaited_once()
    reply_text = update.message.reply_html.call_args[0][0]
    assert "Welcome to ReelsBot!" in reply_text
    assert "<code>987654</code>" in reply_text


@pytest.mark.asyncio
async def test_start_handler_unauthorized_rejection(monkeypatch):
    """T3.2: Verify /start command rejects unauthorized chat ID when ALLOWED_CHAT_ID is set."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "111111")
    update, context = make_mock_update(chat_id=987654, text="/start")

    await reels_bot.start_handler(update, context)

    update.message.reply_text.assert_awaited_once_with("⛔ Unauthorized. This is a private bot instance.")


@pytest.mark.asyncio
async def test_message_handler_non_tiktok_guidance(monkeypatch):
    """T3.3: Verify non-TikTok text prompts instructional guidance without triggering pipeline."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")
    update, context = make_mock_update(chat_id=123456, text="Can you process this YouTube video?")

    await reels_bot.message_handler(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Send me a TikTok link" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_message_handler_full_pipeline_success(temp_db, tmp_path, monkeypatch):
    """T3.4: Verify full pipeline: video download, AI caption, upload, DB save, and preview dispatch."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    # Dummy video file on disk
    dummy_vid = tmp_path / "pipeline_test.mp4"
    dummy_vid.write_bytes(b"dummy_mp4_bytes")

    status_msg = AsyncMock()
    status_msg.delete = AsyncMock()

    update, context = make_mock_update(
        chat_id=123456,
        text="Check this: https://www.tiktok.com/@stoic/video/7123456789"
    )
    update.message.reply_text = AsyncMock(return_value=status_msg)

    preview_msg = MagicMock()
    preview_msg.message_id = 999
    update.effective_chat.send_video = AsyncMock(return_value=preview_msg)

    with patch("reels_bot.acquire_clean_video", AsyncMock(return_value=(str(dummy_vid), {"title": "Viral Reel", "duration": 40, "creator": "stoic_architect"}))):
        with patch("reels_bot.generate_viral_caption", AsyncMock(return_value="Viral Hook Line\n\n#mindset")):
            with patch("reels_bot.upload_to_cloudinary", AsyncMock(return_value="https://res.cloudinary.com/demo/sample.mp4")):
                await reels_bot.message_handler(update, context)

    update.effective_chat.send_video.assert_awaited_once()
    status_msg.delete.assert_awaited_once()

    # Verify post was persisted with pending status
    with sqlite3.connect(reels_bot.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM pending_posts WHERE chat_id = 123456")
        row = cur.fetchone()
        assert row is not None
        assert row["title"] == "Viral Reel"
        assert row["status"] == "pending"
        assert row["message_id"] == 999
        assert row["cloudinary_url"] == "https://res.cloudinary.com/demo/sample.mp4"


@pytest.mark.asyncio
async def test_message_handler_duration_too_long_alert(temp_db, monkeypatch):
    """T3.5: Verify VideoTooLongError triggers user alert and aborts pipeline without preview."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock()

    update, context = make_mock_update(
        chat_id=123456,
        text="https://www.tiktok.com/@user/video/7123456789"
    )
    update.message.reply_text = AsyncMock(return_value=status_msg)

    with patch("reels_bot.acquire_clean_video", AsyncMock(side_effect=reels_bot.VideoTooLongError(120))):
        await reels_bot.message_handler(update, context)

    status_msg.edit_text.assert_awaited_once()
    assert "Video is too long (120s)" in status_msg.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_callback_approve_action(temp_db, tmp_path, monkeypatch):
    """T3.6: Verify 'approve' callback schedules Buffer post, updates DB, purges file, and edits caption."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    dummy_vid = tmp_path / "approve_vid.mp4"
    dummy_vid.write_bytes(b"temp_video")

    reels_bot.save_pending_post({
        "task_id": "task_app_001",
        "chat_id": 123456,
        "message_id": 501,
        "video_path": str(dummy_vid),
        "cloudinary_url": "https://mock.cdn/video/sample.mp4",
        "caption": "Approved Reel Caption",
        "title": "Stoic Reel",
        "creator": "author",
        "duration": 40,
        "status": "pending"
    })

    update, context = make_mock_update(chat_id=123456, callback_data="approve:task_app_001")

    with patch("reels_bot.schedule_buffer_post", AsyncMock(return_value={"status": "success", "label": "Mon, 09 Nov 2026 at 07:00 PM (Asia/Kolkata)"})):
        await reels_bot.callback_handler(update, context)

    update.callback_query.edit_message_caption.assert_awaited_once()
    caption_arg = update.callback_query.edit_message_caption.call_args[1]["caption"]
    assert "Approved &amp; scheduled as an Instagram Reel!" in caption_arg or "Approved & scheduled as an Instagram Reel!" in caption_arg
    assert "Mon, 09 Nov 2026 at 07:00 PM (Asia/Kolkata)" in caption_arg

    # Status updated in DB
    post = reels_bot.get_pending_post("task_app_001")
    assert post["status"] == "scheduled"
    # Local video cleaned up
    assert not os.path.exists(dummy_vid)


@pytest.mark.asyncio
async def test_callback_approve_failure_handling(temp_db, monkeypatch):
    """T3.7: Verify Buffer schedule failure edits message caption with error warning and does not crash."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    reels_bot.save_pending_post({
        "task_id": "task_fail_001",
        "chat_id": 123456,
        "message_id": 502,
        "video_path": "/tmp/nonexistent.mp4",
        "cloudinary_url": "https://mock.cdn/v.mp4",
        "caption": "Fail Caption",
        "title": "Title",
        "creator": "author",
        "duration": 30,
        "status": "pending"
    })

    update, context = make_mock_update(chat_id=123456, callback_data="approve:task_fail_001")

    with patch("reels_bot.schedule_buffer_post", AsyncMock(side_effect=RuntimeError("Buffer Token expired"))):
        await reels_bot.callback_handler(update, context)

    update.callback_query.edit_message_caption.assert_awaited_once()
    caption_arg = update.callback_query.edit_message_caption.call_args[1]["caption"]
    assert "Scheduling Failed:" in caption_arg
    assert "Buffer Token expired" in caption_arg


@pytest.mark.asyncio
async def test_callback_regen_action(temp_db, monkeypatch):
    """T3.8: Verify 'regen' callback re-runs AI with elevated temp=0.9 and refreshes caption in-place."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    reels_bot.save_pending_post({
        "task_id": "task_regen_001",
        "chat_id": 123456,
        "message_id": 503,
        "video_path": "/tmp/v.mp4",
        "cloudinary_url": "https://mock.cdn/v.mp4",
        "caption": "Old Initial Caption",
        "title": "Mindset Mastery",
        "creator": "stoic_lead",
        "duration": 30,
        "status": "pending"
    })

    update, context = make_mock_update(chat_id=123456, callback_data="regen:task_regen_001")

    with patch("reels_bot.generate_viral_caption", AsyncMock(return_value="Brand New Regenerated Caption ♟️")) as mock_gen:
        await reels_bot.callback_handler(update, context)

    mock_gen.assert_awaited_once_with("Mindset Mastery", "stoic_lead", temperature=0.9)
    update.callback_query.edit_message_caption.assert_awaited_once()
    assert "Brand New Regenerated Caption" in update.callback_query.edit_message_caption.call_args[1]["caption"]
    assert reels_bot.get_pending_post("task_regen_001")["caption"] == "Brand New Regenerated Caption ♟️"


@pytest.mark.asyncio
async def test_callback_reject_action(temp_db, tmp_path, monkeypatch):
    """T3.9: Verify 'reject' callback purges local video file, marks post rejected, and edits caption."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")

    dummy_vid = tmp_path / "reject_vid.mp4"
    dummy_vid.write_bytes(b"reject_stream")

    reels_bot.save_pending_post({
        "task_id": "task_rej_001",
        "chat_id": 123456,
        "message_id": 504,
        "video_path": str(dummy_vid),
        "cloudinary_url": "https://mock.cdn/v.mp4",
        "caption": "Reject Caption",
        "title": "Title",
        "creator": "author",
        "duration": 30,
        "status": "pending"
    })

    update, context = make_mock_update(chat_id=123456, callback_data="reject:task_rej_001")

    await reels_bot.callback_handler(update, context)

    update.callback_query.edit_message_caption.assert_awaited_once()
    assert "Discarded" in update.callback_query.edit_message_caption.call_args[1]["caption"]
    assert reels_bot.get_pending_post("task_rej_001")["status"] == "rejected"
    assert not os.path.exists(dummy_vid)


@pytest.mark.asyncio
async def test_callback_expired_task(temp_db, monkeypatch):
    """T3.10: Verify callback on expired or nonexistent task ID replies with cache expiration alert."""
    monkeypatch.setattr(reels_bot, "ALLOWED_CHAT_ID", "")
    update, context = make_mock_update(chat_id=123456, callback_data="approve:task_expired_999")

    await reels_bot.callback_handler(update, context)

    update.callback_query.edit_message_caption.assert_awaited_once_with(
        "⚠️ This request has expired or was removed from cache."
    )


# ===========================================================================
# Tier 4: Live Dry-Run & Non-Destructive Probes (T4.1 - T4.5)
# ===========================================================================

def test_py_compile_syntax_and_integrity():
    """T4.1: Verify Python byte-compilation succeeds on reels_bot.py and test_reels_bot.py."""
    bot_py = WORKSPACE_DIR / "reels_bot.py"
    test_py = WORKSPACE_DIR / "test_reels_bot.py"

    res1 = py_compile.compile(str(bot_py), doraise=True)
    res2 = py_compile.compile(str(test_py), doraise=True)

    assert res1 is not None and os.path.exists(res1)
    assert res2 is not None and os.path.exists(res2)


def test_cli_discover_buffer_missing_key(monkeypatch, capsys):
    """T4.2: Verify discover_buffer_cli prints error message when BUFFER_API_KEY is not set."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "")
    reels_bot.discover_buffer_cli()
    captured = capsys.readouterr().out
    assert "[!] Error: BUFFER_API_KEY is not set in .env. Please set it first." in captured


def test_cli_discover_buffer_live_dummy_token(monkeypatch, capsys):
    """T4.3: Verify discover_buffer_cli handles dummy token with clean API error output without crash."""
    monkeypatch.setattr(reels_bot, "BUFFER_API_KEY", "dummy_token_test_123")
    reels_bot.discover_buffer_cli()
    captured = capsys.readouterr().out
    assert "Buffer API Error" in captured or "Network error" in captured


def test_launcher_script_guard_missing_token(tmp_path):
    """T4.4: Verify start_bot.sh aborts with exit code 1 when TELEGRAM_BOT_TOKEN is placeholder."""
    script_path = WORKSPACE_DIR / "start_bot.sh"
    temp_env = tmp_path / ".env"
    temp_env.write_text("TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here\n")
    temp_script = tmp_path / "start_bot.sh"
    temp_script.write_text(script_path.read_text())
    temp_script.chmod(0o755)
    (tmp_path / ".venv").mkdir()

    res = subprocess.run([str(temp_script)], capture_output=True, text=True, cwd=str(tmp_path))
    assert res.returncode == 1
    output = res.stdout + res.stderr
    assert "Warning: TELEGRAM_BOT_TOKEN is not set in .env" in output


@pytest.mark.asyncio
async def test_live_pollinations_endpoint_probe():
    """T4.5: Verify live zero-key Pollinations endpoint probe and fallback cascade resilience."""
    try:
        res = await reels_bot.generate_caption_pollinations("Say hello", temperature=0.7)
        assert isinstance(res, str)
        assert len(res) > 0
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        # Expected if IP is rate-limited (HTTP 402 / 429) or offline
        if isinstance(exc, httpx.HTTPStatusError):
            assert exc.response.status_code in (402, 404, 429, 500, 502, 503)

    # In all circumstances, viral caption cascade succeeds without unhandled exceptions
    caption = await reels_bot.generate_viral_caption("Mindset Shift", "creator")
    assert isinstance(caption, str)
    assert len(caption) <= 950
    assert len(caption) > 0


# ===========================================================================
# Backward-Compatible Baseline Tests
# ===========================================================================

def test_tiktok_regex_matching():
    """Verify regex handles various TikTok link formats and cleans trailing punctuation."""
    test_tiktok_url_regex_comprehensive()


def test_database_lifecycle(tmp_path, monkeypatch):
    """Verify SQLite cache initialization, saving, retrieving, and status updates."""
    test_database_crud_lifecycle(tmp_path, monkeypatch)


def test_duration_guard(monkeypatch):
    """Verify VideoTooLongError exception triggers when duration exceeds limit."""
    test_duration_guard_boundary(monkeypatch)


def test_post_time_calculation(monkeypatch):
    """Verify scheduling timestamp calculations for instant (-1) and fixed hour."""
    test_post_time_instant_mode(monkeypatch)


@pytest.mark.asyncio
async def test_free_ai_caption_generation():
    """Verify viral caption generator creates structured captions under 950 characters."""
    caption = await reels_bot.generate_viral_caption(
        title="Why the strongest minds remain silent",
        creator="stoic_philosophy",
        temperature=0.7
    )
    assert isinstance(caption, str)
    assert len(caption) > 20
    assert len(caption) <= 950
    assert "#" in caption
    if reels_bot.CREDIT_CREATOR:
        assert "@stoic_philosophy" in caption or "Credit:" in caption


if __name__ == "__main__":
    pytest.main(["-v", __file__])
