#!/usr/bin/env python3
"""
reels_bot.py - Standalone TikTok to Instagram Reel Telegram Bot & Buffer Scheduler.

100% Free AI Tier (Groq / Gemini / Pollinations zero-key fallback)
Watermark-free video extraction (TikWM API with yt-dlp fallback)
Cloudinary unsigned media upload
Human-in-the-loop Telegram preview with inline approval buttons
Buffer GraphQL automated scheduling across single or multiple channels.
"""

import sys
import os
import re
import json
import html
import uuid
import time
import asyncio
import logging
import sqlite3
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, Tuple, List

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("reels_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("ReelsBot")

# Load environment configuration
load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()

AI_PROVIDER = os.getenv("AI_PROVIDER", "groq").lower().strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_UPLOAD_PRESET = os.getenv("CLOUDINARY_UPLOAD_PRESET", "").strip()

BUFFER_API_KEY = os.getenv("BUFFER_API_KEY", "").strip()
BUFFER_CHANNEL_ID = os.getenv("BUFFER_CHANNEL_ID", "").strip()

TIMEZONE_STR = os.getenv("TIMEZONE", "Asia/Kolkata").strip()
POST_HOUR = int(os.getenv("POST_HOUR", "19"))
POST_MINUTE = int(os.getenv("POST_MINUTE", "0"))
MAX_REEL_DURATION_SEC = int(os.getenv("MAX_REEL_DURATION_SEC", "90"))

BRAND_NAME = os.getenv("BRAND_NAME", "Sigma Mindset").strip()
BRAND_VOICE = os.getenv("BRAND_VOICE", "stoic, commanding, psychologically insightful, concise").strip()
CREDIT_CREATOR = os.getenv("CREDIT_CREATOR", "yes").lower().strip() == "yes"

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)
DB_PATH = Path("reels_bot.db")

TIKTOK_URL_REGEX = re.compile(
    r"https?://(?:[a-zA-Z0-9_-]+\.)?tiktok\.com/[^\s)\]]+",
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Database Management (SQLite Cache)
# ---------------------------------------------------------------------------
def init_db():
    """Initialize SQLite database for persisting pending posts and state."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_posts (
                task_id TEXT PRIMARY KEY,
                chat_id INTEGER,
                message_id INTEGER,
                video_path TEXT,
                cloudinary_url TEXT,
                caption TEXT,
                title TEXT,
                creator TEXT,
                duration INTEGER,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def save_pending_post(post: Dict[str, Any]):
    """Insert or update pending post in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending_posts
            (task_id, chat_id, message_id, video_path, cloudinary_url, caption, title, creator, duration, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post["task_id"],
            post["chat_id"],
            post.get("message_id"),
            post.get("video_path"),
            post.get("cloudinary_url"),
            post.get("caption"),
            post.get("title"),
            post.get("creator"),
            post.get("duration"),
            post.get("status", "pending")
        ))
        conn.commit()

def get_pending_post(task_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve pending post by task_id."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM pending_posts WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def update_pending_status(task_id: str, status: str, caption: Optional[str] = None):
    """Update post status and optionally caption."""
    with sqlite3.connect(DB_PATH) as conn:
        if caption is not None:
            conn.execute("UPDATE pending_posts SET status = ?, caption = ? WHERE task_id = ?", (status, caption, task_id))
        else:
            conn.execute("UPDATE pending_posts SET status = ? WHERE task_id = ?", (status, task_id))
        conn.commit()

# ---------------------------------------------------------------------------
# Video Acquisition: TikWM API + yt-dlp Fallback
# ---------------------------------------------------------------------------
class VideoTooLongError(Exception):
    def __init__(self, duration: int):
        self.duration = duration
        super().__init__(f"Video duration ({duration}s) exceeds maximum allowed {MAX_REEL_DURATION_SEC}s")

async def fetch_video_tikwm(url: str, task_id: str) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch watermark-free video via TikWM API with 3 exponential retries.
    Returns (local_file_path, metadata_dict).
    """
    api_url = "https://www.tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }
    
    last_err = None
    data = None

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for attempt in range(1, 4):
            try:
                logger.info(f"[TikWM] Fetching metadata for {url} (attempt {attempt}/3)...")
                resp = await client.post(api_url, data={"url": url, "hd": "1"}, headers=headers)
                if resp.status_code == 200:
                    res_json = resp.json()
                    if res_json.get("code") == 0 and res_json.get("data"):
                        data = res_json["data"]
                        break
                    else:
                        last_err = res_json.get("msg", "TikWM returned non-zero code")
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as e:
                last_err = str(e)
            
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)

    if not data:
        raise RuntimeError(f"TikWM fetch failed: {last_err}")

    duration = int(data.get("duration") or 0)
    if duration > MAX_REEL_DURATION_SEC:
        raise VideoTooLongError(duration)

    video_url = data.get("hdplay") or data.get("play")
    if not video_url:
        raise RuntimeError("No clean video stream URL found in TikWM response")

    if video_url.startswith("//"):
        video_url = "https:" + video_url
    elif not video_url.startswith("http"):
        video_url = "https://www.tikwm.com" + ("" if video_url.startswith("/") else "/") + video_url

    output_path = DOWNLOADS_DIR / f"{task_id}.mp4"
    logger.info(f"[TikWM] Downloading video stream to {output_path}...")
    
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        resp = await client.get(video_url, headers=headers)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)

    meta = {
        "title": data.get("title", ""),
        "duration": duration,
        "creator": data.get("author", {}).get("unique_id", ""),
        "source": "tikwm"
    }
    return str(output_path), meta

def _ytdlp_sync_download(url: str, task_id: str) -> Tuple[str, Dict[str, Any]]:
    """Synchronous worker for yt-dlp execution with pre-download duration validation."""
    import yt_dlp

    out_tmpl = str(DOWNLOADS_DIR / f"{task_id}.%(ext)s")
    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Pre-download inspection: inspect duration without downloading full video
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError(f"yt-dlp could not extract metadata for {url}")

        duration = int(info.get("duration") or 0)
        if duration > MAX_REEL_DURATION_SEC:
            raise VideoTooLongError(duration)

        # Duration is valid; perform actual video download
        downloaded_info = ydl.extract_info(url, download=True)
        if not downloaded_info:
            raise RuntimeError(f"yt-dlp download failed for {url}")

        file_path = ydl.prepare_filename(downloaded_info)
        if not os.path.exists(file_path):
            # Check if video was merged or remuxed with an alternate container extension
            for candidate in DOWNLOADS_DIR.glob(f"{task_id}.*"):
                if candidate.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]:
                    file_path = str(candidate)
                    break

        final_duration = duration or int(downloaded_info.get("duration") or 0)
        if final_duration > MAX_REEL_DURATION_SEC:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            raise VideoTooLongError(final_duration)

        meta = {
            "title": downloaded_info.get("title", "") or info.get("title", ""),
            "duration": final_duration,
            "creator": (
                downloaded_info.get("uploader_id")
                or downloaded_info.get("uploader")
                or info.get("uploader_id")
                or info.get("uploader")
                or ""
            ),
            "source": "yt-dlp"
        }
        return file_path, meta

async def fetch_video_ytdlp(url: str, task_id: str) -> Tuple[str, Dict[str, Any]]:
    """Fallback video downloader using yt-dlp in async thread."""
    logger.info(f"[yt-dlp] Invoking yt-dlp fallback for {url}...")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ytdlp_sync_download, url, task_id)

async def acquire_clean_video(url: str, task_id: str) -> Tuple[str, Dict[str, Any]]:
    """Acquire video attempting TikWM first, falling back to yt-dlp."""
    try:
        return await fetch_video_tikwm(url, task_id)
    except VideoTooLongError:
        raise
    except Exception as e:
        logger.warning(f"[Acquire] TikWM failed ({e}). Falling back to yt-dlp...")
        return await fetch_video_ytdlp(url, task_id)

# ---------------------------------------------------------------------------
# 100% Free AI Caption Generation Engine
# ---------------------------------------------------------------------------
def build_caption_prompt(title: str, brand_name: str, brand_voice: str) -> str:
    """Build standardized viral Instagram Reels caption prompt matching the actual video topic."""
    raw = (title or "").strip()
    tags = re.findall(r"#\w+", raw)
    clean_text = re.sub(r"#\w+", "", raw).strip()
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    tag_str = " ".join(tags) if tags else "None"

    return (
        f"You are an elite viral Instagram Reels copywriter specializing in high-retention content.\n"
        f"Original TikTok Caption / Description: {clean_text or title or 'Trending video'}\n"
        f"Original TikTok Tags: {tag_str}\n"
        f"Brand Name: {brand_name}\n"
        f"Brand Voice: {brand_voice}\n\n"
        f"CRITICAL INSTRUCTIONS:\n"
        f"- Analyze the TikTok caption, video subject, mood, and original tags to understand the exact characters, scene, and theme.\n"
        f"- DO NOT copy or repeat the original caption/title words verbatim. Write 100% ORIGINAL, FRESH commentary.\n"
        f"- Strictly match the topic (e.g. if anime, write about the anime; if dogs/pets, write about the dog; if comedy/relatable, write humor). NEVER insert dark psychology or stoic quotes unless the video is explicitly about dark psychology.\n"
        f"- HASHTAGS REQUIREMENT: Provide 8 to 12 of the BEST, MOST VIRAL, and HIGHLY-TARGETED hashtags for Instagram Reels directly matching the video subject and category (blend of specific topic tags, character/theme tags, and viral reel discovery tags).\n"
        f"- DO NOT include any copyright disclaimer in your output (it is added automatically by the system).\n\n"
        f"Format Blueprint:\n"
        f"1. HOOK: A punchy 1-line statement that stops the scroll and matches the video topic.\n"
        f"2. THE BREAKDOWN: 1-2 brief, impactful sentences delivering engaging commentary or context matching the video with line breaks.\n"
        f"3. CALL TO ACTION: One clear retention prompt relevant to the video (e.g. 'Drop a ❤️ if you loved this' or 'Tag a friend who needs to see this.').\n"
        f"4. HASHTAGS: A blank line followed by 8-12 best and related niche hashtags that DIRECTLY MATCH the video topic.\n\n"
        f"Rules: Write freshly in your own words. 1-2 taste-level emojis maximum. Keep total output strictly under 750 characters. Output ONLY the caption text."
    )

def build_smart_fallback_caption(title: str, creator: str = "") -> str:
    """Generate an engaging, 100% topic-matched caption directly from the video metadata."""
    raw = (title or "").strip()
    tags = re.findall(r"#\w+", raw)
    clean_text = re.sub(r"#\w+", "", raw).strip()
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    # Detect theme for a fresh hook instead of copying the title verbatim
    lower = raw.lower()
    if any(k in lower for k in ["anime", "naruto", "edit", "amv", "manga", "obito", "gojo", "jujutsu", "sukuna"]):
        hook = "This scene hits different every single time you watch it. 🎬✨"
        fallback_tags = ["#animeedits", "#narutoshippuden", "#animelovers", "#animecore", "#otakulife", "#animeshorts", "#reelsinstagram", "#explorepage"]
    elif any(k in lower for k in ["dog", "pup", "pitbull", "cat", "pet", "animal"]):
        hook = "Pure happiness in one video! Can't stop smiling at this. 🐾❤️"
        fallback_tags = ["#pitbullfun", "#dogsofinstagram", "#puppylove", "#petreels", "#doglife", "#cuteanimals", "#reelsinstagram", "#explorepage"]
    elif any(k in lower for k in ["funny", "laugh", "meme", "joke", "comedy", "relatable"]):
        hook = "Why is this so painfully accurate? 😂 Drop your thoughts below!"
        fallback_tags = ["#relatable", "#comedyreels", "#memesdaily", "#viralvideos", "#funnymoments", "#humor", "#reelsinstagram", "#explorepage"]
    elif any(k in lower for k in ["gym", "workout", "fitness", "muscle"]):
        hook = "Consistency beats talent every single day. Keep grinding! 💪"
        fallback_tags = ["#fitnessmotivation", "#gymreels", "#workoutmotivation", "#fitlife", "#bodybuilding", "#reelsinstagram", "#explorepage"]
    else:
        hook = "Wait till the end... Rate this from 1 to 10 in the comments! ✨"
        fallback_tags = ["#reelsinstagram", "#viralreels", "#trendingnow", "#explorepage", "#instareels", "#fyp"]

    # Filter out TikTok-specific tags like #tiktok #xyzbca
    clean_tags = [t for t in tags if t.lower() not in ["#tiktok", "#foryoupage", "#xyzbca", "#foryou", "#fypシ"]]
    all_tags = []
    for t in clean_tags + fallback_tags:
        if t.lower() not in [x.lower() for x in all_tags]:
            all_tags.append(t)

    tag_str = " ".join(all_tags[:10])
    cta = "Drop a ❤️ if you loved this! Share with someone who needs to see this."
    return f"{hook}\n\n{cta}\n\n{tag_str}"

async def generate_caption_groq(prompt: str, temperature: float = 0.7) -> str:
    """Generate caption using Groq Cloud API (Free tier) with model fallback."""
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY is not configured")
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    models = [GROQ_MODEL, "qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    seen = set()
    models_to_try = [m for m in models if m and not (m in seen or seen.add(m))]

    last_err = None
    for model in models_to_try:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You craft viral, high-converting Instagram Reel captions."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": temperature,
                "max_tokens": 400
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                logger.warning(f"[Groq] Model {model} returned HTTP {resp.status_code}: {resp.text[:120]}")
                last_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[Groq] Model {model} failed: {e}")

    raise RuntimeError(f"All Groq models failed. Last error: {last_err}")

async def generate_caption_gemini(prompt: str, temperature: float = 0.7) -> str:
    """Generate caption using Google Gemini API (Free tier) with model fallback."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")

    models = [GEMINI_MODEL, "gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"]
    seen = set()
    models_to_try = [m for m in models if m and not (m in seen or seen.add(m))]

    last_err = None
    for model in models_to_try:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": 400
                }
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                logger.warning(f"[Gemini] Model {model} returned HTTP {resp.status_code}: {resp.text[:120]}")
                last_err = f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as e:
            last_err = str(e)
            logger.warning(f"[Gemini] Model {model} failed: {e}")

    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")

async def generate_caption_pollinations(prompt: str, temperature: float = 0.7) -> str:
    """Generate caption using Pollinations text API (100% Free, Zero-Key fallback)."""
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://text.pollinations.ai/{encoded_prompt}?model=openai&seed=42&temperature={temperature}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text.strip()

async def generate_viral_caption(
    title: str,
    creator: str = "",
    temperature: float = 0.7
) -> str:
    """Cascading free AI caption generator (Groq -> Gemini -> Pollinations)."""
    prompt = build_caption_prompt(title, BRAND_NAME, BRAND_VOICE)
    caption = None
    errors = []

    # Cascade order based on configured preference
    providers = []
    if AI_PROVIDER == "gemini":
        providers = [("gemini", generate_caption_gemini), ("groq", generate_caption_groq), ("pollinations", generate_caption_pollinations)]
    else:
        providers = [("groq", generate_caption_groq), ("gemini", generate_caption_gemini), ("pollinations", generate_caption_pollinations)]

    for name, func in providers:
        try:
            logger.info(f"[AI Caption] Attempting generation with provider: {name} (temp={temperature})...")
            caption = await func(prompt, temperature)
            if caption:
                logger.info(f"[AI Caption] Successfully generated with {name}")
                break
        except Exception as e:
            logger.warning(f"[AI Caption] Provider {name} failed: {e}")
            errors.append(f"{name}: {e}")

    if not caption:
        logger.info(f"[AI Caption] Generating smart topic-matched caption from metadata for: {title[:40]}")
        caption = build_smart_fallback_caption(title, creator)

    # Creator attribution & copyright disclaimer to prevent takedowns/reports
    if CREDIT_CREATOR and creator:
        credit_line = f"🎥 Credit: @{creator} (TikTok)"
        disclaimer = (
            f"\n\n{credit_line}\n"
            f"⚖️ Disclaimer: No copyright infringement intended. All rights belong to respective owners. "
            f"For credit or immediate removal, please DM us."
        )
        max_body_len = 950 - len(disclaimer)
        if len(caption) > max_body_len:
            caption = caption[:max_body_len - 5].rsplit(" ", 1)[0] + "..."
        caption = caption + disclaimer
    else:
        if len(caption) > 950:
            caption = caption[:940].rsplit(" ", 1)[0] + "..."

    return caption

# ---------------------------------------------------------------------------
# Cloudinary Unsigned Upload
# ---------------------------------------------------------------------------
async def upload_to_cloudinary(video_path: str) -> str:
    """Upload video to Cloudinary via unsigned upload preset with retries."""
    if not CLOUDINARY_CLOUD_NAME or not CLOUDINARY_UPLOAD_PRESET:
        logger.warning("[Cloudinary] Cloud name or preset not set; returning local mock URL")
        return f"https://mock.cdn/video/{Path(video_path).name}"

    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/video/upload"
    
    for attempt in range(1, 4):
        try:
            logger.info(f"[Cloudinary] Uploading {video_path} (attempt {attempt}/3)...")
            async with httpx.AsyncClient(timeout=300.0) as client:
                with open(video_path, "rb") as f:
                    files = {"file": (Path(video_path).name, f, "video/mp4")}
                    data = {"upload_preset": CLOUDINARY_UPLOAD_PRESET}
                    resp = await client.post(url, data=data, files=files)
                    resp.raise_for_status()
                    res_json = resp.json()
                    secure_url = res_json.get("secure_url")
                    if secure_url:
                        logger.info(f"[Cloudinary] Upload succeeded: {secure_url}")
                        return secure_url
        except Exception as e:
            logger.warning(f"[Cloudinary] Attempt {attempt} failed: {e}")
            if attempt == 3:
                raise
            await asyncio.sleep(3)

    raise RuntimeError("Cloudinary upload failed after 3 attempts")

# ---------------------------------------------------------------------------
# Buffer GraphQL Scheduling Engine
# ---------------------------------------------------------------------------
def calculate_post_time() -> Tuple[str, str]:
    """Calculate target schedule time in configured TIMEZONE."""
    try:
        tz = ZoneInfo(TIMEZONE_STR)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    if POST_HOUR < 0:
        # Schedule in ~3 minutes
        post_dt = now + timedelta(minutes=3)
    else:
        post_dt = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
        if post_dt <= now + timedelta(minutes=5):
            post_dt += timedelta(days=1)

    due_at = post_dt.astimezone(timezone.utc).isoformat()
    label = post_dt.strftime("%a, %d %b %Y at %I:%M %p") + f" ({TIMEZONE_STR})"
    return due_at, label

async def schedule_buffer_post(
    video_url: str,
    caption: str,
    due_at_iso: Optional[str] = None,
    channels: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Execute createPost GraphQL mutation against Buffer API across configured channels."""
    if not BUFFER_API_KEY:
        raise ValueError("BUFFER_API_KEY is not configured in .env")

    if channels is not None:
        raw_channels = [c.strip().strip("'\"") for c in channels if c and c.strip().strip("'\"")]
    else:
        raw_channels = [c.strip().strip("'\"") for c in BUFFER_CHANNEL_ID.split(",") if c.strip().strip("'\"")]

    if not raw_channels:
        raise ValueError("BUFFER_CHANNEL_ID is not configured in .env")

    if due_at_iso:
        due_at = due_at_iso
        label = due_at_iso
    else:
        due_at, label = calculate_post_time()

    results = []
    channel_errors = []

    headers = {
        "Authorization": f"Bearer {BUFFER_API_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        for channel_id in raw_channels:
            mutation = f"""mutation {{
  createPost(input: {{
    channelId: {json_str(channel_id)}
    text: {json_str(caption)}
    schedulingType: automatic
    mode: customScheduled
    dueAt: {json_str(due_at)}
    metadata: {{ instagram: {{ type: reel, shouldShareToFeed: true }} }}
    assets: [{{ video: {{ url: {json_str(video_url)} }} }}]
  }}) {{
    ... on PostActionSuccess {{ post {{ id dueAt }} }}
    ... on MutationError {{ message }}
  }}
}}"""
            logger.info(f"[Buffer] Scheduling post for channel {channel_id} at {due_at}...")
            try:
                resp = await client.post("https://api.buffer.com", json={"query": mutation}, headers=headers)
                
                # Check HTTP error status and extract detailed JSON error if available
                if resp.status_code != 200:
                    try:
                        err_payload = resp.json()
                        if isinstance(err_payload, dict) and "errors" in err_payload:
                            err_msgs = [e.get("message", str(e)) for e in err_payload["errors"] if isinstance(e, dict)]
                            msg = "; ".join(err_msgs) if err_msgs else str(err_payload["errors"])
                            raise RuntimeError(f"Buffer API HTTP {resp.status_code}: {msg}")
                    except (ValueError, KeyError):
                        pass
                    resp.raise_for_status()

                res_data = resp.json()

                # Check top-level GraphQL errors (data: null scenario)
                if "errors" in res_data and res_data["errors"]:
                    err_list = res_data["errors"]
                    if isinstance(err_list, list):
                        err_msgs = [e.get("message", str(e)) for e in err_list if isinstance(e, dict)]
                        err_text = "; ".join(err_msgs) if err_msgs else str(err_list)
                    else:
                        err_text = str(err_list)
                    raise RuntimeError(f"Buffer GraphQL error: {err_text}")

                data_obj = res_data.get("data")
                if not isinstance(data_obj, dict):
                    raise RuntimeError(f"Buffer API returned no data object: {res_data}")

                post_data = data_obj.get("createPost")
                if not isinstance(post_data, dict):
                    raise RuntimeError(f"Buffer API missing createPost response: {data_obj}")

                # Check MutationError union
                if "message" in post_data and post_data["message"]:
                    raise RuntimeError(f"Buffer API mutation error: {post_data['message']}")

                if "post" not in post_data or not post_data["post"]:
                    raise RuntimeError(f"Buffer API response missing post object: {post_data}")

                results.append({"channelId": channel_id, **post_data})

            except Exception as e:
                logger.error(f"[Buffer] Error scheduling for channel {channel_id}: {e}")
                channel_errors.append(f"Channel {channel_id}: {str(e)}")

    if channel_errors:
        err_summary = "; ".join(channel_errors)
        if results:
            raise RuntimeError(f"Partial scheduling failure ({len(results)}/{len(raw_channels)} succeeded). Errors: {err_summary}")
        else:
            raise RuntimeError(f"Buffer scheduling failed: {err_summary}")

    return {"status": "success", "dueAt": due_at, "label": label, "results": results}

def json_str(s: str) -> str:
    """Format string safely for GraphQL interpolation."""
    return json.dumps(s)

def make_html_caption(raw_caption: str, suffix_html: str = "", prefix_html: str = "", max_total: int = 1024) -> str:
    """
    Safely escape raw caption for Telegram HTML parse mode and combine with prefix_html and suffix_html,
    ensuring total length strictly does not exceed Telegram's 1024 character caption limit.
    """
    raw_caption = raw_caption or ""
    extra_len = len(prefix_html) + len(suffix_html)
    escaped = html.escape(raw_caption)
    if len(escaped) + extra_len <= max_total:
        return f"{prefix_html}{escaped}{suffix_html}"

    # Truncate raw caption until escaped version fits with prefix, suffix and ellipsis
    target_len = max(0, max_total - extra_len - 3)
    truncated = raw_caption[:target_len]
    escaped = html.escape(truncated)
    while len(escaped) + extra_len + 3 > max_total and len(truncated) > 0:
        truncated = truncated[:-10]
        escaped = html.escape(truncated)
    return f"{prefix_html}{escaped}...{suffix_html}"

# ---------------------------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------------------------
def is_authorized(update: Update) -> bool:
    """Check if update is from the authorized chat ID."""
    clean_allowed = ALLOWED_CHAT_ID.strip("'\"")
    if not clean_allowed:
        return True
    chat = update.effective_chat
    return bool(chat and str(chat.id) == clean_allowed)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command and report chat ID."""
    chat = update.effective_chat
    chat_id = chat.id if chat else "unknown"
    logger.info(f"[Command] /start from chat_id: {chat_id}")

    clean_allowed = ALLOWED_CHAT_ID.strip("'\"")
    if not clean_allowed:
        logger.info(f"[Onboarding] ALLOWED_CHAT_ID is empty. Incoming chat_id: {chat_id}")
        print(f"\n[!] ONBOARDING: Incoming Telegram Chat ID is: {chat_id}")
        print(f"[!] Add ALLOWED_CHAT_ID={chat_id} to your .env file to restrict access.\n")
        text = (
            f"👋 <b>Welcome to ReelsBot!</b>\n\n"
            f"Your Telegram Chat ID is: <code>{chat_id}</code>\n\n"
            f"⚠️ <b>Security Notice:</b> To restrict this bot to you, add this to your <code>.env</code> file:\n"
            f"<code>ALLOWED_CHAT_ID={chat_id}</code>\n\n"
            f"Send me any TikTok link to generate an Instagram Reel!"
        )
    elif str(chat_id) != clean_allowed:
        logger.warning(f"[Security] Unauthorized access attempt from chat_id: {chat_id}")
        await update.message.reply_text("⛔ Unauthorized. This is a private bot instance.")
        return
    else:
        text = (
            f"🎬 <b>ReelsBot is Ready!</b>\n\n"
            f"Send me any TikTok video link (e.g. <code>https://www.tiktok.com/@user/video/...</code>).\n"
            f"I will extract watermark-free HD video, generate a viral caption using free AI, and prepare it for Instagram Reels."
        )
    await update.message.reply_html(text)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process incoming messages, extract TikTok URLs, and initiate pipeline."""
    if not is_authorized(update):
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning(f"[Security] Ignored message from unauthorized chat_id: {chat_id}")
        if update.message:
            await update.message.reply_text("⛔ Unauthorized. This is a private bot instance.")
        return

    clean_allowed = ALLOWED_CHAT_ID.strip("'\"")
    if not clean_allowed:
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.info(f"[Onboarding] Message received while ALLOWED_CHAT_ID is empty. Chat ID: {chat_id}")
        print(f"\n[!] ONBOARDING: Incoming message from chat_id: {chat_id}")

    text = update.message.text or ""
    match = TIKTOK_URL_REGEX.search(text)
    if not match:
        await update.message.reply_text(
            "👋 Send me a TikTok link (e.g., https://www.tiktok.com/@user/video/...) and I'll prepare it as an Instagram Reel."
        )
        return

    raw_url = match.group(0).rstrip(".,?!")
    status_msg = await update.message.reply_text("⏳ Got the link! Fetching HD video and crafting caption… (about 1 minute)")

    task_id = str(uuid.uuid4())[:8]
    try:
        # Step 1: Video Acquisition
        video_path, meta = await acquire_clean_video(raw_url, task_id)

        # Step 2: Viral Caption Generation
        caption = await generate_viral_caption(meta["title"], meta.get("creator", ""))

        # Step 3: Cloudinary Upload
        cloudinary_url = await upload_to_cloudinary(video_path)

        # Step 4: Persist Pending State
        post_data = {
            "task_id": task_id,
            "chat_id": update.effective_chat.id,
            "video_path": video_path,
            "cloudinary_url": cloudinary_url,
            "caption": caption,
            "title": meta["title"],
            "creator": meta.get("creator", ""),
            "duration": meta["duration"],
            "status": "pending"
        }
        save_pending_post(post_data)

        # Step 5: Send Interactive Preview
        _, schedule_label = calculate_post_time()
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve & Schedule", callback_data=f"approve:{task_id}")],
            [
                InlineKeyboardButton("🔄 Regenerate Caption", callback_data=f"regen:{task_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{task_id}")
            ]
        ])

        # Preview header displaying original TikTok caption
        preview_prefix = f"📌 <i>Original: {html.escape(meta['title'][:120])}</i>\n\n" if meta.get("title") else ""
        preview_html = make_html_caption(caption, prefix_html=preview_prefix)

        try:
            if cloudinary_url and cloudinary_url.startswith("http") and "mock.cdn" not in cloudinary_url:
                logger.info(f"[Preview] Sending video preview via Cloudinary URL: {cloudinary_url}")
                preview_msg = await update.effective_chat.send_video(
                    video=cloudinary_url,
                    caption=preview_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                    supports_streaming=True,
                    read_timeout=120.0,
                    write_timeout=120.0
                )
            else:
                with open(video_path, "rb") as video_file:
                    preview_msg = await update.effective_chat.send_video(
                        video=video_file,
                        caption=preview_html,
                        parse_mode="HTML",
                        reply_markup=markup,
                        supports_streaming=True,
                        read_timeout=120.0,
                        write_timeout=120.0
                    )
        except Exception as send_err:
            logger.warning(f"[Preview] Cloudinary URL send_video failed ({send_err}); trying local file upload...")
            with open(video_path, "rb") as video_file:
                preview_msg = await update.effective_chat.send_video(
                    video=video_file,
                    caption=preview_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                    supports_streaming=True,
                    read_timeout=180.0,
                    write_timeout=180.0
                )

        post_data["message_id"] = preview_msg.message_id
        save_pending_post(post_data)
        await status_msg.delete()

    except VideoTooLongError as e:
        await status_msg.edit_text(
            f"⚠️ <b>Video is too long ({e.duration}s).</b>\n"
            f"Instagram Reels only allows videos up to {MAX_REEL_DURATION_SEC} seconds.\n"
            f"Please submit a shorter TikTok clip.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception(f"[Pipeline] Processing failed: {e}")
        safe_err = html.escape(str(e)[:300])
        await status_msg.edit_text(f"⚠️ <b>Error preparing video:</b> {safe_err}", parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks for Approve, Regenerate, and Reject."""
    query = update.callback_query

    if not is_authorized(update):
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning(f"[Security] Unauthorized callback attempt from chat_id: {chat_id}")
        await query.answer("⛔ Unauthorized.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    action, _, task_id = data.partition(":")
    post = get_pending_post(task_id)

    if not post:
        await query.edit_message_caption("⚠️ This request has expired or was removed from cache.")
        return

    if action == "approve":
        try:
            res = await schedule_buffer_post(post["cloudinary_url"], post["caption"])
            update_pending_status(task_id, "scheduled")
            try:
                if post.get("video_path") and os.path.isfile(post["video_path"]):
                    os.remove(post["video_path"])
            except Exception:
                pass
            safe_label = html.escape(str(res.get("label", "")))
            suffix = (
                f"\n\n✅ <b>Approved & scheduled as an Instagram Reel!</b>\n"
                f"📅 Scheduled for: <code>{safe_label}</code>"
            )
            caption_text = make_html_caption(post.get("caption", ""), suffix)
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception(f"[Approval] Buffer schedule failed: {e}")
            safe_err = html.escape(str(e)[:300])
            suffix = f"\n\n⚠️ <b>Scheduling Failed:</b> {safe_err}"
            caption_text = make_html_caption(post.get("caption", ""), suffix)
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML"
            )

    elif action == "regen":
        try:
            new_caption = await generate_viral_caption(
                post["title"],
                post.get("creator", ""),
                temperature=0.9
            )
            update_pending_status(task_id, "pending", new_caption)
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve & Schedule", callback_data=f"approve:{task_id}")],
                [
                    InlineKeyboardButton("🔄 Regenerate Again", callback_data=f"regen:{task_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject:{task_id}")
                ]
            ])
            prefix = f"📌 <i>Original: {html.escape(post.get('title', '')[:120])}</i>\n\n" if post.get("title") else ""
            caption_text = make_html_caption(new_caption, prefix_html=prefix)
            await query.edit_message_caption(
                caption=caption_text,
                parse_mode="HTML",
                reply_markup=markup
            )
        except Exception as e:
            logger.exception(f"[Regenerate] Failed: {e}")

    elif action == "reject":
        update_pending_status(task_id, "rejected")
        # Clean up local video file
        try:
            if post.get("video_path") and os.path.isfile(post["video_path"]):
                os.remove(post["video_path"])
        except Exception:
            pass
        await query.edit_message_caption(
            caption="❌ <b>Discarded.</b> Send another TikTok link whenever you like.",
            parse_mode="HTML"
        )

# ---------------------------------------------------------------------------
# Buffer Discovery CLI Command
# ---------------------------------------------------------------------------
def discover_buffer_cli():
    """Query Buffer GraphQL API to display Organizations and Channel IDs."""
    if not BUFFER_API_KEY:
        print("\n[!] Error: BUFFER_API_KEY is not set in .env. Please set it first.\n")
        return

    headers = {
        "Authorization": f"Bearer {BUFFER_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            # Step 1: Organizations Query
            resp = client.post(
                "https://api.buffer.com",
                json={"query": "query { account { organizations { id name } } }"},
                headers=headers
            )
            if resp.status_code != 200:
                try:
                    err_json = resp.json()
                    if "errors" in err_json:
                        err_msg = "; ".join([e.get("message", str(e)) for e in err_json["errors"] if isinstance(e, dict)])
                        print(f"\n[-] Buffer API Error (HTTP {resp.status_code}): {err_msg}\n")
                        return
                except Exception:
                    pass
                print(f"\n[-] Buffer API Error: HTTP {resp.status_code} - {resp.text}\n")
                return

            data = resp.json()
            if "errors" in data and data["errors"]:
                err_msg = "; ".join([e.get("message", str(e)) for e in data["errors"] if isinstance(e, dict)])
                print(f"\n[-] Buffer GraphQL Error: {err_msg}\n")
                return

            data_obj = data.get("data") or {}
            account_obj = data_obj.get("account") or {}
            orgs = account_obj.get("organizations") or []
            if not orgs:
                print("\n[-] No Buffer organizations found. Ensure your Buffer account has an active Organization.\n")
                return

            org_id = orgs[0]["id"]
            org_name = orgs[0].get("name", "Default")
            print(f"\n[+] Found Buffer Organization: {org_name} (ID: {org_id})")

            # Step 2: Channels Query
            channel_query = f'query {{ channels(input: {{ organizationId: "{org_id}" }}) {{ id name service }} }}'
            resp2 = client.post("https://api.buffer.com", json={"query": channel_query}, headers=headers)
            if resp2.status_code != 200:
                try:
                    err_json2 = resp2.json()
                    if "errors" in err_json2:
                        err_msg = "; ".join([e.get("message", str(e)) for e in err_json2["errors"] if isinstance(e, dict)])
                        print(f"\n[-] Buffer Channels API Error (HTTP {resp2.status_code}): {err_msg}\n")
                        return
                except Exception:
                    pass
                print(f"\n[-] Buffer Channels API Error: HTTP {resp2.status_code} - {resp2.text}\n")
                return

            data2 = resp2.json()
            if "errors" in data2 and data2["errors"]:
                err_msg = "; ".join([e.get("message", str(e)) for e in data2["errors"] if isinstance(e, dict)])
                print(f"\n[-] Buffer Channels GraphQL Error: {err_msg}\n")
                return

            channels_data = data2.get("data") or {}
            channels = channels_data.get("channels") or []

            if not channels:
                print("[-] No channels connected to this organization.\n")
                return

            print("\n" + "=" * 70)
            print(f"{'Service':<15} | {'Channel Name':<25} | {'Channel ID'}")
            print("=" * 70)
            instagram_channels = []
            for ch in channels:
                service = ch.get("service", "unknown")
                name = ch.get("name", "unknown")
                ch_id = ch.get("id", "")
                if service.lower() == "instagram":
                    instagram_channels.append(ch_id)
                print(f"{service:<15} | {name:<25} | {ch_id}")
            print("=" * 70 + "\n")

            if instagram_channels:
                print(f"[i] Recommended Instagram Channel ID(s): {', '.join(instagram_channels)}")
                print(f"[i] Set in your .env: BUFFER_CHANNEL_ID={','.join(instagram_channels)}\n")
            else:
                print("⚠️ Warning: No Instagram channel found. Please connect an Instagram Business/Creator account in Buffer.\n")

    except httpx.RequestError as e:
        print(f"\n[-] Network error communicating with Buffer API: {e}\n")

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main():
    init_db()

    if "--discover-buffer" in sys.argv:
        discover_buffer_cli()
        return

    if not TELEGRAM_BOT_TOKEN:
        print("\n" + "!" * 65)
        print(" [!] ERROR: TELEGRAM_BOT_TOKEN is not set in your .env file!")
        print(" Please create/edit .env and provide your token from @BotFather.")
        print("!" * 65 + "\n")
        sys.exit(1)

    print("=================================================================")
    print("      🚀 ReelsBot: TikTok to Instagram Reel Standalone Daemon    ")
    print("=================================================================")
    print(f" • AI Provider:      {AI_PROVIDER}")
    print(f" • Max Reel Length:  {MAX_REEL_DURATION_SEC}s")
    print(f" • Timezone:         {TIMEZONE_STR}")
    print(f" • Post Target Time: {'Instant (+3 min)' if POST_HOUR < 0 else f'{POST_HOUR:02d}:{POST_MINUTE:02d}'}")
    print(f" • Allowed Chat ID:  {ALLOWED_CHAT_ID or 'UNRESTRICTED (Will prompt on first msg)'}")
    print("=================================================================\n")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(60.0)
        .read_timeout(120.0)
        .write_timeout(120.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot polling initiated...")
    app.run_polling()

if __name__ == "__main__":
    main()
