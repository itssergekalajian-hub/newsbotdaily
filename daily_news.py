#!/usr/bin/env python3
"""
MidWorld Daily — one full calendar-day news brief.

Runs just after local midnight, collects everything @midworldnews posted during
the day that just ended, rewrites it into one explained bulletin, narrates it
with an AI voice, and posts text + voice note + presenter video.

All free: Gemini Flash free tier, edge-tts (no key), GitHub Actions.
"""

import asyncio
import datetime as dt
import functools
import os
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import edge_tts
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SOURCE = os.getenv("SOURCE_CHANNEL", "midworldnews")   # public channel, no @
TARGET = os.environ["TARGET_CHAT_ID"]                  # "@midworlddaily"
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

# EET / EEST — the +2/+3 switch is handled by the zone, never hardcode an offset
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Beirut"))

MODEL = os.getenv("GEMINI_MODEL", "").strip()   # empty = auto-detect a Flash model
VOICE = os.getenv("VOICE", "en-US-AndrewMultilingualNeural")
MAKE_VIDEO = os.getenv("MAKE_VIDEO", "1") == "1"
PRESENTER = os.getenv("PRESENTER_IMAGE", "presenter.png")

# Set to e.g. "2030-02-01" to rebuild one specific day by hand. Overrides the
# midnight guard, so it also works for catching up after a missed run.
FORCE_DATE = os.getenv("TARGET_DATE", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MidWorldDaily/1.0)"}
DIGEST_MARK = "📅 MidWorld Daily"


# ----------------------------------------------------------------------------
# Which day are we covering?
# ----------------------------------------------------------------------------
def resolve_day() -> dt.date | None:
    """The calendar day to cover, or None if this run should do nothing.

    Two UTC crons are scheduled (21:00 and 22:00) so that exactly one of them
    lands in the first hour of the local day whether or not summer time is in
    effect. The other one exits here.

    The window is measured from the local day boundary rather than from
    "hour == 0", because on the spring-forward night 00:00 does not exist —
    the day starts at 01:00. Note that subtracting two datetimes that share a
    tzinfo object is wall-clock arithmetic in Python, so both sides are
    converted to UTC first or that night comes out an hour off.
    """
    if FORCE_DATE:
        return dt.date.fromisoformat(FORCE_DATE)
    now = dt.datetime.now(TZ)
    boundary = dt.datetime.combine(now.date(), dt.time(0), tzinfo=TZ)
    since_midnight = (now.astimezone(dt.timezone.utc)
                      - boundary.astimezone(dt.timezone.utc)).total_seconds()
    if not 0 <= since_midnight < 3600:
        print(f"local time is {now:%H:%M %Z} — not the midnight run, exiting")
        return None
    return now.date() - dt.timedelta(days=1)


# ----------------------------------------------------------------------------
# 1. Collect one calendar day from the public channel preview
# ----------------------------------------------------------------------------
def fetch_posts(username: str, day: dt.date, max_pages: int = 10) -> list[dict]:
    posts: dict[int, dict] = {}
    before = None

    for _ in range(max_pages):
        url = f"https://t.me/s/{username}"
        if before:
            url += f"?before={before}"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()

        blocks = BeautifulSoup(r.text, "html.parser").select("div.tgme_widget_message")
        if not blocks:
            break

        # t.me/s renders oldest first, so blocks[0] is the top of this page
        oldest_id = oldest_day = None
        for b in blocks:
            stamp = b.select_one("time[datetime]")
            attr = b.get("data-post", "")
            if not stamp or "/" not in attr:
                continue
            when = dt.datetime.fromisoformat(stamp["datetime"]).astimezone(TZ)
            mid = int(attr.rsplit("/", 1)[1])
            if oldest_id is None:
                oldest_id, oldest_day = mid, when.date()

            if when.date() != day:          # skips both later and earlier days
                continue
            body = b.select_one("div.tgme_widget_message_text")
            text = body.get_text("\n").strip() if body else ""
            if len(text) > 20 and DIGEST_MARK not in text:
                posts[mid] = {"time": when.strftime("%H:%M"), "text": text}

        # walked back past the target day, or hit the start of the channel
        if oldest_day is None or oldest_day < day or oldest_id <= 1:
            break
        before = oldest_id

    return [posts[k] for k in sorted(posts)]


# ----------------------------------------------------------------------------
# 2. Summarize — Gemini Flash free tier, one request per day
# ----------------------------------------------------------------------------
PROMPT = """You are the writer and anchor of a daily news bulletin called
MidWorld Daily.

Below is everything a news channel published on {date}, from midnight to
midnight, local time, with the time of each post. This is one complete day.
Turn it into ONE spoken bulletin script that a voice will read aloud.

Rules:
- Use ONLY the information in the posts. Never add facts, numbers, names or
  background that are not there.
- Cover the whole day. Where a story developed across several hours, tell it in
  order: what was first reported, how it changed, where it stood by the end of
  the day.
- Group by topic and cover each in turn, for example: Middle East (Lebanon,
  Israel, Iran) / Russia and Ukraine / China and Asia / Europe and the US /
  Markets and economy / Sports (football, MMA) / Other. Skip empty groups.
- Don't just list headlines. For each topic explain what happened and why it
  matters.
- Merge duplicate reports of one event. If several posts confirm it, you may
  say it was widely reported.
- The source labels single-source or unverified claims. Keep that hedging in
  the script — say "according to a single report" rather than stating it as
  confirmed fact.
- Tell it as a flowing story, plain spoken language, short sentences.
- Open with: "This is MidWorld Daily for {date}." then one sentence that sums
  up the whole day. Close with a one-line sign-off.
- Plain text only. No markdown, no emojis, no asterisks, no hashtags, no
  bullet symbols, no links — every character you write will be spoken aloud.
- Target length: 700 to 1100 words.

POSTS FROM {date}:
{items}
"""


GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_SKIP = ("embedding", "aqa", "image", "tts", "live", "vision", "learnlm", "gemma")


@functools.lru_cache(maxsize=1)
def candidate_models() -> tuple[str, ...]:
    """Every Flash model this key can use, best first.

    Model names get retired on their own schedule — hardcoding one means the
    bot breaks silently months later with a 404. This asks the API instead.
    """
    r = requests.get(f"{GEMINI_ROOT}/models",
                     headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
    r.raise_for_status()
    usable = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
              if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in usable
             if "flash" in n and not any(s in n for s in _SKIP)]
    if not flash:
        raise RuntimeError(f"no usable Flash model on this key; saw: {usable}")

    # plain > lite, stable > preview, newest generation, then short alias
    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        version = float(m.group(1)) if m else 0.0
        return ("lite" in n, "preview" in n or "exp" in n, -version, len(n))

    return tuple(sorted(flash, key=rank))


def discover_model() -> str:
    return candidate_models()[0]


def summarize(posts: list[dict], day: dt.date) -> str:
    items = "\n\n---\n\n".join(f"[{p['time']}] {p['text']}" for p in posts)
    prompt = PROMPT.format(date=f"{day:%A, %d %B %Y}", items=items[:600_000])
    model = MODEL or discover_model()
    print(f"using model: {model}  |  prompt: {len(prompt):,} chars")

    # No thinkingConfig here on purpose: the parameter differs between model
    # generations and a rejected field returns a generic INVALID_ARGUMENT that
    # says nothing about which field it disliked.
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}

    # Alternates to fall back to when the first choice is overloaded. The
    # newest generation is the busiest, so an older Flash is often free when
    # 3.x is throwing 503s — a slightly older model beats no brief at all.
    alternates = [m for m in candidate_models() if m != model]
    tried = 0

    for attempt in range(8):
        r = requests.post(f"{GEMINI_ROOT}/models/{model}:generateContent",
                          headers=headers, json=body, timeout=300)
        if r.ok:
            break
        if r.status_code == 404 and attempt == 0:
            model = discover_model()          # pinned name is gone, find another
            print(f"model not found, falling back to: {model}")
            continue
        if r.status_code == 400 and "generationConfig" in body:
            # something in the tuning knobs is unsupported; let the model default
            print("400 — retrying with default generation settings")
            body.pop("generationConfig")
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            reason = "rate limited" if r.status_code == 429 else "model busy"
            # Google's spikes are usually short; alternate between waiting it
            # out and trying a less popular model.
            if alternates and tried < len(alternates) and attempt % 2 == 1:
                model = alternates[tried]
                tried += 1
                print(f"{reason} ({r.status_code}) — switching to {model}")
                continue
            wait = min(20 * 2 ** (attempt // 2), 120)
            print(f"{reason} ({r.status_code}) — waiting {wait}s")
            time.sleep(wait)
            continue
        print(f"gemini error {r.status_code}: {r.text[:1000]}", file=sys.stderr)
        r.raise_for_status()
    else:
        raise RuntimeError(f"gemini unavailable after 8 attempts (last: {model})")

    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"no candidates returned: {str(data)[:400]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if "text" in p).strip()
    if not text:
        raise RuntimeError(
            f"empty response, finishReason={candidates[0].get('finishReason')} — "
            "usually means the token budget was spent before any text was written"
        )
    return text


# ----------------------------------------------------------------------------
# 3. Voice + subtitles — edge-tts, free, no API key
# ----------------------------------------------------------------------------
def _srt_time(ticks: int) -> str:
    """edge-tts reports offsets in 100-nanosecond units."""
    ms = ticks // 10_000
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(words: list[dict], path: str,
              max_chars: int = 68, max_secs: float = 4.0) -> int:
    """Group word timings into readable caption lines and write an SRT.

    Written by hand rather than via edge_tts.SubMaker: that helper's API has
    moved between versions and a silently empty subtitle file makes ffmpeg
    fail with a bare INVALID_DATA, which is miserable to diagnose.
    """
    cues, current = [], []
    for w in words:
        current.append(w)
        line = " ".join(x["text"] for x in current)
        span = (current[-1]["offset"] + current[-1]["duration"]
                - current[0]["offset"]) / 1e7
        if (len(line) >= max_chars or span >= max_secs
                or w["text"].endswith((".", "?", "!", "—"))):
            cues.append(current)
            current = []
    if current:
        cues.append(current)

    with open(path, "w", encoding="utf-8") as f:
        for i, cue in enumerate(cues, 1):
            start = _srt_time(cue[0]["offset"])
            end = _srt_time(cue[-1]["offset"] + cue[-1]["duration"])
            text = " ".join(x["text"] for x in cue)
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
    return len(cues)


async def synthesize(text: str, mp3_path: str, srt_path: str) -> None:
    comm = edge_tts.Communicate(text, VOICE)
    words: list[dict] = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary" and chunk.get("text"):
                words.append({"text": chunk["text"],
                              "offset": int(chunk.get("offset", 0)),
                              "duration": int(chunk.get("duration", 0))})
    cues = write_srt(words, srt_path)
    print(f"audio: {os.path.getsize(mp3_path):,} bytes  |  "
          f"{len(words)} word timings -> {cues} caption lines")


# ----------------------------------------------------------------------------
# 4. Package — ogg voice note, and a still-presenter video with captions
# ----------------------------------------------------------------------------
def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-25:])
        raise RuntimeError(f"ffmpeg failed ({p.returncode}):\n{tail}")


def to_voice_note(mp3_path: str, ogg_path: str) -> None:
    run(["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus",
         "-b:a", "48k", "-vbr", "on", "-ar", "48000", "-ac", "1", ogg_path])


def audio_seconds(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def to_video(mp3_path: str, srt_path: str, mp4_path: str) -> None:
    """Render the presenter video: slow camera drift plus timed captions.

    The motion is a Ken Burns move — the frame is upscaled, then a slowly
    zooming, drifting window is cropped out of it. It is not a talking face:
    lip-sync models need a GPU and these runners are CPU-only.

    Both zoom and pan are expressed as a fraction of the bulletin's own length,
    so the move takes exactly as long as the audio. A fixed per-frame rate
    finishes its travel in about ninety seconds and then sits clamped against
    the edge of the source for the rest of a five-minute brief. Writing the pan
    as a fraction of the available slack also keeps it inside the frame at any
    duration, with no bounds arithmetic to get wrong.
    """
    fps = 25
    frames = max(int(audio_seconds(mp3_path) * fps), 2)
    zoom_to = 1.12

    motion = (
        "scale=1600:900,"
        f"zoompan=z='min(1+{zoom_to - 1:.4f}*on/{frames},{zoom_to})'"
        f":x='(iw-iw/zoom)*(0.5+0.3*on/{frames})'"
        f":y='(ih-ih/zoom)*(0.5-0.3*on/{frames})'"
        f":d=1:s=1280x720:fps={fps}"
    )
    style = ("Fontsize=17,PrimaryColour=&H00FFFFFF,BackColour=&HC0000000,"
             "BorderStyle=4,Outline=0,Shadow=0,MarginV=45")
    still = "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"

    has_subs = os.path.exists(srt_path) and os.path.getsize(srt_path) > 50
    subs = f",subtitles={srt_path}:force_style='{style}'" if has_subs else ""
    if not has_subs:
        print("no usable subtitles — video will have no captions")
    print(f"rendering {frames / fps:.0f}s of video ({frames} frames)")

    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-shortest", mp4_path]

    # each fallback drops one feature rather than losing the video entirely
    attempts = [
        ("motion + captions", motion + subs),
        ("captions only", still + subs),
        ("plain still", still),
    ]
    for label, chain in attempts:
        try:
            run(["ffmpeg", "-y", "-loop", "1", "-framerate", "25",
                 "-i", PRESENTER, "-i", mp3_path,
                 "-filter_complex", f"[0:v]{chain}[v]",
                 "-map", "[v]", "-map", "1:a", *tail])
            size = os.path.getsize(mp4_path)
            print(f"video: {label}, {size / 1e6:.1f} MB")
            if size > 49 * 1024 * 1024:
                raise RuntimeError(f"video is {size/1e6:.0f} MB, over Telegram's limit")
            return
        except RuntimeError as e:
            print(f"render '{label}' failed: {e}", file=sys.stderr)
    raise RuntimeError("every video render attempt failed")


# ----------------------------------------------------------------------------
# 5. Publish
# ----------------------------------------------------------------------------
def send(method: str, data: dict, files: dict | None = None) -> None:
    r = requests.post(f"{API}/{method}", data=data, files=files, timeout=600)
    if not r.ok:
        print(f"{method} failed: {r.text}", file=sys.stderr)
        r.raise_for_status()


def publish_text(script: str, day: dt.date) -> None:
    body = f"{DIGEST_MARK} — {day:%A, %d %B %Y}\n\n{script}"
    for i in range(0, len(body), 4000):
        send("sendMessage", {"chat_id": TARGET, "text": body[i:i + 4000],
                             "disable_web_page_preview": True})


def publish_voice(day: dt.date, ogg_path: str) -> None:
    with open(ogg_path, "rb") as f:
        send("sendVoice", {"chat_id": TARGET,
                           "caption": f"🎧 {day:%d %B %Y} in full"}, {"voice": f})


def publish_video(day: dt.date, mp4_path: str) -> None:
    with open(mp4_path, "rb") as f:
        send("sendVideo", {"chat_id": TARGET, "supports_streaming": True,
                           "caption": f"📺 {day:%d %B %Y}"}, {"video": f})


def main() -> None:
    day = resolve_day()
    if day is None:
        return

    posts = fetch_posts(SOURCE, day)
    print(f"{day}: collected {len(posts)} posts from @{SOURCE}")
    if len(posts) < 2:
        print("not enough for a brief, nothing published")
        return

    script = summarize(posts, day)
    print(f"script: {len(script.split())} words")

    asyncio.run(synthesize(script, "brief.mp3", "brief.srt"))

    if not os.path.exists(PRESENTER):
        raise RuntimeError(
            f"{PRESENTER} is missing — the video needs a presenter image")

    try:
        to_video("brief.mp3", "brief.srt", "brief.mp4")
        publish_video(day, "brief.mp4")
        print("published video")
    except Exception as e:
        # A finished brief should never be lost to a rendering problem, so fall
        # back to the audio rather than publishing nothing at all.
        print(f"video failed ({e}) — falling back to voice", file=sys.stderr)
        to_voice_note("brief.mp3", "brief.ogg")
        publish_voice(day, "brief.ogg")
        print("published voice as fallback")


if __name__ == "__main__":
    main()

