#!/usr/bin/env python3
"""
MidWorld Daily — one full calendar-day news brief, as a produced video.

Runs just after local midnight, collects everything @midworldnews posted during
the day that just ended, rewrites it into a topic-by-topic bulletin, narrates it
with an AI voice, and assembles a broadcast-style video: title card, one
animated scene per topic with burned-in captions, and an outro.

All free: Gemini Flash free tier, edge-tts (no key), GitHub Actions.
"""

import asyncio
import datetime as dt
import functools
import json
import os
import re
import shutil
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import edge_tts
import requests
from bs4 import BeautifulSoup

import video

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SOURCE = os.getenv("SOURCE_CHANNEL", "midworldnews")
TARGET = os.environ["TARGET_CHAT_ID"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Beirut"))
MODEL = os.getenv("GEMINI_MODEL", "").strip()      # empty = auto-detect
VOICE = os.getenv("VOICE", "en-US-AndrewMultilingualNeural")
PRESENTER = os.getenv("PRESENTER_IMAGE", "presenter.png")
BRAND = os.getenv("BRAND", "MIDWORLD DAILY")
FORCE_DATE = os.getenv("TARGET_DATE", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MidWorldDaily/1.0)"}
DIGEST_MARK = "📅 MidWorld Daily"
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_SKIP = ("embedding", "aqa", "image", "tts", "live", "vision", "learnlm", "gemma")


# ----------------------------------------------------------------------------
# Which day are we covering?
# ----------------------------------------------------------------------------
def resolve_day() -> dt.date | None:
    """The calendar day to cover, or None if this run should do nothing.

    Two UTC crons are scheduled so that exactly one lands in the first hour of
    the local day whether or not summer time is in effect. Measured from the
    local day boundary rather than "hour == 0", because on the spring-forward
    night 00:00 does not exist. Both sides are converted to UTC first: two
    datetimes sharing a tzinfo subtract as wall-clock, not elapsed time.
    """
    if FORCE_DATE:
        return dt.date.fromisoformat(FORCE_DATE)
    now = dt.datetime.now(TZ)
    boundary = dt.datetime.combine(now.date(), dt.time(0), tzinfo=TZ)
    since = (now.astimezone(dt.timezone.utc)
             - boundary.astimezone(dt.timezone.utc)).total_seconds()
    if not 0 <= since < 3600:
        print(f"local time is {now:%H:%M %Z} — not the midnight run, exiting")
        return None
    return now.date() - dt.timedelta(days=1)


# ----------------------------------------------------------------------------
# 1. Collect one calendar day from the public channel preview
# ----------------------------------------------------------------------------
def fetch_posts(username: str, day: dt.date, max_pages: int = 12) -> list[dict]:
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

            if when.date() != day:
                continue
            body = b.select_one("div.tgme_widget_message_text")
            text = body.get_text("\n").strip() if body else ""
            if len(text) > 20 and DIGEST_MARK not in text:
                posts[mid] = {"time": when.strftime("%H:%M"), "text": text}

        if oldest_day is None or oldest_day < day or oldest_id <= 1:
            break
        before = oldest_id

    return [posts[k] for k in sorted(posts)]


# ----------------------------------------------------------------------------
# 2. Script — Gemini returns topic segments, not one wall of text
# ----------------------------------------------------------------------------
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
    flash = [n for n in usable if "flash" in n and not any(s in n for s in _SKIP)]
    if not flash:
        raise RuntimeError(f"no usable Flash model on this key; saw: {usable}")

    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("lite" in n, "preview" in n or "exp" in n,
                -(float(m.group(1)) if m else 0.0), len(n))

    return tuple(sorted(flash, key=rank))


PROMPT = """You are the writer and anchor of a daily news bulletin called
MidWorld Daily.

Below is everything a news channel published on {date}, midnight to midnight
local time, with the time of each post. This is one complete day.

Write the spoken bulletin as a series of topic segments.

Return ONLY a JSON object, no markdown fences, in exactly this shape:
{{"headline": "one sentence summing up the whole day",
  "segments": [{{"topic": "Middle East", "script": "the spoken words..."}}]}}

Rules for the segments:
- One segment per topic. Use topics that fit the day, for example: Middle East,
  Russia and Ukraine, China and Asia, Europe, United States, Markets, Football,
  MMA. Skip any topic with no news. Between 3 and 8 segments.
- "topic" is a screen label: 1 to 3 words, no punctuation.
- "script" is what the anchor says out loud for that topic: 100 to 220 words.
- Cover the whole day. Where a story developed over several hours, tell it in
  order — what was first reported, how it changed, where it stood by the end.
- Explain what happened and why it matters. Do not just read headlines.
- Use ONLY the information in the posts. Never add facts, numbers or names that
  are not there.
- Merge duplicate reports of one event. If several posts confirm it, you may
  say it was widely reported.
- The source labels single-source or unverified claims. Keep that hedging —
  say "according to a single report" rather than stating it as confirmed.
- Plain spoken language, short sentences. Every character is read aloud, so no
  markdown, no emojis, no asterisks, no hashtags, no bullets, no links.
- The first segment's script should open by greeting the audience and giving
  the headline. The last should end with a short sign-off.

POSTS FROM {date}:
{items}
"""


def _extract_json(raw: str) -> dict:
    """Models wrap JSON in fences or prose more often than they should."""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


def summarize(posts: list[dict], day: dt.date) -> dict:
    items = "\n\n---\n\n".join(f"[{p['time']}] {p['text']}" for p in posts)
    prompt = PROMPT.format(date=f"{day:%A, %d %B %Y}", items=items[:600_000])
    model = MODEL or candidate_models()[0]
    print(f"using model: {model}  |  prompt: {len(prompt):,} chars")

    # No thinkingConfig: the parameter differs between model generations and a
    # rejected field returns a generic INVALID_ARGUMENT naming nothing.
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192,
                             "responseMimeType": "application/json"},
    }
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    alternates = [m for m in candidate_models() if m != model]
    tried = 0

    for attempt in range(8):
        r = requests.post(f"{GEMINI_ROOT}/models/{model}:generateContent",
                          headers=headers, json=body, timeout=300)
        if r.ok:
            break
        if r.status_code == 404 and attempt == 0:
            model = candidate_models()[0]
            print(f"model not found, falling back to: {model}")
            continue
        if r.status_code == 400 and "generationConfig" in body:
            print("400 — retrying with default generation settings")
            body.pop("generationConfig")
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            reason = "rate limited" if r.status_code == 429 else "model busy"
            # Newer generations carry the most traffic; an older Flash is often
            # answering fine while the newest is saturated.
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
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"no candidates returned: {str(data)[:400]}")
    parts = cands[0].get("content", {}).get("parts", [])
    raw = "\n".join(p["text"] for p in parts if "text" in p).strip()
    if not raw:
        raise RuntimeError(
            f"empty response, finishReason={cands[0].get('finishReason')}")

    try:
        brief = _extract_json(raw)
        segments = [s for s in brief.get("segments", [])
                    if s.get("script", "").strip()]
        if not segments:
            raise ValueError("no segments in the reply")
    except Exception as e:
        # A bulletin in one piece still beats no bulletin at all.
        print(f"could not parse segments ({e}) — using the whole reply",
              file=sys.stderr)
        return {"headline": "", "segments": [{"topic": "Today", "script": raw}]}

    for s in segments:
        label = re.sub(r"[^\w &-]", "", s.get("topic", "News")).strip()
        s["topic"] = label[:22] or "News"
    return {"headline": brief.get("headline", ""), "segments": segments}


# ----------------------------------------------------------------------------
# 3. Voice + captions
# ----------------------------------------------------------------------------
def _srt_time(seconds: float) -> str:
    ms = max(int(round(seconds * 1000)), 0)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _lines(text: str, width: int = 62) -> list[str]:
    """Split a script into caption-sized lines, preferring sentence ends."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence:
            continue
        if len(sentence) <= width:
            out.append(sentence)
            continue
        line = ""
        for word in sentence.split():
            if line and len(line) + 1 + len(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            out.append(line)
    return out


def write_srt(text: str, words: list[dict], duration: float, path: str) -> int:
    """Captions for one segment, timed from the voice where possible.

    edge-tts word boundaries have moved between releases and sometimes arrive
    empty, which previously produced a zero-byte .srt and a video with no
    captions at all. When they are missing, lines are spread across the known
    audio duration by character count instead — not perfectly in sync, but
    always present and close.
    """
    lines = _lines(text)
    if not lines:
        open(path, "w").close()
        return 0

    cues: list[tuple[float, float, str]] = []
    if words:
        cursor = 0
        for line in lines:
            chunk = words[cursor:cursor + len(line.split())]
            cursor += len(line.split())
            if not chunk:
                break
            start = chunk[0]["offset"] / 1e7
            end = (chunk[-1]["offset"] + chunk[-1]["duration"]) / 1e7
            cues.append((start, max(end, start + 0.4), line))
    if not cues:
        total = sum(len(l) for l in lines) or 1
        t = 0.0
        for line in lines:
            span = duration * len(line) / total
            cues.append((t, t + span, line))
            t += span

    with open(path, "w", encoding="utf-8") as f:
        for i, (start, end, line) in enumerate(cues, 1):
            f.write(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{line}\n\n")
    return len(cues)


async def _speak(text: str, mp3_path: str) -> list[dict]:
    comm = edge_tts.Communicate(text, VOICE)
    words: list[dict] = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"].endswith("Boundary"):
                if {"offset", "duration", "text"} <= set(chunk.keys()):
                    words.append(chunk)
    return words


def voice_segment(text: str, mp3_path: str, srt_path: str) -> float:
    words = asyncio.run(_speak(text, mp3_path))
    seconds = video.probe_duration(mp3_path)
    cues = write_srt(text, words, seconds, srt_path)
    print(f"    {seconds:5.1f}s audio, {len(words):4d} word timings, "
          f"{cues} captions{'' if words else '  (estimated timing)'}")
    return seconds


# ----------------------------------------------------------------------------
# 4. Publish
# ----------------------------------------------------------------------------
def send(method: str, data: dict, files: dict | None = None) -> None:
    r = requests.post(f"{API}/{method}", data=data, files=files, timeout=900)
    if not r.ok:
        print(f"{method} failed: {r.text}", file=sys.stderr)
        r.raise_for_status()


def publish_video(day: dt.date, headline: str, mp4_path: str) -> None:
    caption = f"{DIGEST_MARK} — {day:%A, %d %B %Y}"
    if headline:
        caption += f"\n\n{headline[:900]}"
    with open(mp4_path, "rb") as f:
        send("sendVideo", {"chat_id": TARGET, "supports_streaming": True,
                           "caption": caption, "width": video.W,
                           "height": video.H}, {"video": f})


def publish_voice(day: dt.date, ogg_path: str) -> None:
    with open(ogg_path, "rb") as f:
        send("sendVoice", {"chat_id": TARGET,
                           "caption": f"🎧 {day:%d %B %Y}"}, {"voice": f})


# ----------------------------------------------------------------------------
def build(brief: dict, day: dt.date, work: str) -> str:
    """Narrate every segment, render a scene for each, join them up."""
    segments = brief["segments"]
    date_text = f"{day:%d %B %Y}"

    print("narrating:")
    audio, srts, spans = [], [], []
    for i, seg in enumerate(segments):
        mp3 = os.path.join(work, f"seg{i}.mp3")
        srt = os.path.join(work, f"seg{i}.srt")
        print(f"  {i + 1}. {seg['topic']}")
        spans.append(voice_segment(seg["script"], mp3, srt))
        audio.append(mp3)
        srts.append(srt)

    total = sum(spans)
    print(f"bulletin: {len(segments)} segments, {total / 60:.1f} minutes")

    intro = os.path.join(work, "intro.mp4")
    video.render_card(work, 4.5,
                      [(BRAND, 54, "white"),
                       (f"{day:%A, %d %B %Y}", 26, video.PALE),
                       ("The full day in review", 22, video.PALE)],
                      intro)
    parts = [intro]

    print("rendering scenes:")
    elapsed = 0.0
    for i, seg in enumerate(segments):
        out = os.path.join(work, f"scene{i}.mp4")
        print(f"  {i + 1}/{len(segments)} {seg['topic']} ({spans[i]:.0f}s)")
        video.render_scene(work, PRESENTER, audio[i], srts[i], seg["topic"],
                           BRAND, date_text, i, elapsed, total, out)
        elapsed += spans[i]
        parts.append(out)

    outro = os.path.join(work, "outro.mp4")
    video.render_card(work, 3.5,
                      [(BRAND, 44, "white"),
                       (f"@{TARGET.lstrip('@')}", 24, video.PALE)],
                      outro, wipe=False)
    parts.append(outro)

    final = os.path.join(work, "brief.mp4")
    video.concat(parts, final)
    size = os.path.getsize(final)
    print(f"final video: {size / 1e6:.1f} MB, {video.probe_duration(final):.0f}s")
    if size > 49 * 1024 * 1024:
        raise RuntimeError(f"{size / 1e6:.0f} MB is over Telegram's bot limit")
    return final


def main() -> None:
    day = resolve_day()
    if day is None:
        return

    posts = fetch_posts(SOURCE, day)
    print(f"{day}: collected {len(posts)} posts from @{SOURCE}")
    if len(posts) < 2:
        print("not enough for a brief, nothing published")
        return

    brief = summarize(posts, day)
    words = sum(len(s["script"].split()) for s in brief["segments"])
    print(f"script: {words} words across {len(brief['segments'])} segments")

    if not os.path.exists(PRESENTER):
        raise RuntimeError(f"{PRESENTER} is missing — the video needs it")

    work = tempfile.mkdtemp(prefix="midworld-")
    try:
        final = build(brief, day, work)
        publish_video(day, brief.get("headline", ""), final)
        print("published video")
    except Exception as e:
        # A finished bulletin should never be lost to a rendering problem.
        print(f"video failed ({e}) — falling back to voice", file=sys.stderr)
        joined = os.path.join(work, "all.mp3")
        with open(joined, "wb") as out:
            for i in range(len(brief["segments"])):
                p = os.path.join(work, f"seg{i}.mp3")
                if os.path.exists(p):
                    with open(p, "rb") as chunk:
                        out.write(chunk.read())
        if os.path.exists(joined) and os.path.getsize(joined) > 1000:
            ogg = os.path.join(work, "brief.ogg")
            video.run(["ffmpeg", "-y", "-loglevel", "error", "-i", joined,
                       "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
                       "-ar", "48000", "-ac", "1", ogg])
            publish_voice(day, ogg)
            print("published voice as fallback")
        else:
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
