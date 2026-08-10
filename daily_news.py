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
import base64
import datetime as dt
import functools
import json
import os
import re
import subprocess
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
# a touch quicker than default reads as a broadcaster rather than a reader
RATE = os.getenv("VOICE_RATE", "+8%")
# "gemini" sounds markedly more human but is capped near ten requests a day on
# the free tier; "edge" is unlimited. Gemini failures fall back to edge.
ENGINE = os.getenv("VOICE_ENGINE", "edge").strip().lower()
TTS_MODEL = os.getenv("TTS_MODEL", "").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "Charon")
PRESENTER = os.getenv("PRESENTER_IMAGE", "presenter.png")
BRAND = os.getenv("BRAND", "MIDWORLD DAILY")
FORCE_DATE = os.getenv("TARGET_DATE", "").strip()
# preview a full render without posting to Telegram (the workflow uploads the
# resulting preview.mp4 as an artifact instead)
DRY_RUN = os.getenv("DRY_RUN", "").strip() not in ("", "0", "false", "False", "no")

# github.event.schedule, e.g. "0 21 * * *" — empty for manual runs
CRON = os.getenv("CRON_SCHEDULE", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MidWorldDaily/1.0)"}
DIGEST_MARK = "📅 MidWorld Daily"
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"
_SKIP = ("embedding", "aqa", "image", "tts", "live", "vision", "learnlm", "gemma")


# ----------------------------------------------------------------------------
# Which day are we covering?
# ----------------------------------------------------------------------------
CRON_HOURS = (21, 22)          # must match the schedule in the workflow


def _midnight_cron(now: dt.datetime) -> int:
    """Which scheduled hour is local midnight today.

    Each candidate is converted to local time and scored by how long after the
    local midnight it lands; the smallest wins. Deriving it from the UTC offset
    instead leaves a hole on the spring-forward night: the clock changes between
    the two crons, so the earlier one sees offset +2 and the later one +3, and
    each concludes the other is the right one. Nothing publishes that day.
    """
    best, best_score = CRON_HOURS[0], 1e9
    for hour in CRON_HOURS:
        fired = dt.datetime.combine(now.astimezone(dt.timezone.utc).date(),
                                    dt.time(hour), tzinfo=dt.timezone.utc)
        local = fired.astimezone(TZ)
        midnight = dt.datetime.combine(local.date(), dt.time(0), tzinfo=TZ)
        score = ((local.astimezone(dt.timezone.utc)
                  - midnight.astimezone(dt.timezone.utc)).total_seconds() / 3600)
        if score < 0:
            score += 24
        if score < best_score:
            best, best_score = hour, score
    return best


def resolve_day() -> dt.date | None:
    """The calendar day to cover, or None if this run should do nothing.

    Two UTC crons are scheduled so that one of them is local midnight whether or
    not summer time is in effect, and GitHub tells us which fired via
    github.event.schedule.

    This deliberately does NOT check how close to midnight the job actually
    started. Scheduled runs on GitHub are regularly delayed — often tens of
    minutes, sometimes over an hour — and the previous version required the job
    to begin within 60 minutes of midnight, so a delayed run started, decided it
    was not the midnight run, and quietly published nothing. Matching on the
    cron instead means a late run still delivers the right day, just late.
    """
    if FORCE_DATE:
        return dt.date.fromisoformat(FORCE_DATE)

    now = dt.datetime.now(TZ)
    if CRON:                                   # a scheduled run
        wanted = _midnight_cron(now)
        try:
            fired = int(CRON.split()[1])
        except (IndexError, ValueError):
            fired = wanted                     # unparseable: assume it's ours
        if fired != wanted:
            print(f"cron {fired:02d}:00 UTC is the off-season one "
                  f"({wanted:02d}:00 UTC is local midnight today) — exiting")
            return None
        print(f"scheduled run for local midnight, started {now:%H:%M %Z}")
    else:
        print(f"manual run at {now:%H:%M %Z}")

    # The day that has just ended. A midnight run always sits in the early
    # hours of the NEXT local day, so this is simply yesterday — and it stays
    # correct however late GitHub actually started the job. (Subtracting an
    # hour first, as an earlier version did, silently moved the target forward
    # a day once the delay passed 60 minutes.)
    return now.date() - dt.timedelta(days=1)


# ----------------------------------------------------------------------------
# 1. Collect one calendar day from the public channel preview
# ----------------------------------------------------------------------------
def _post_media(block) -> dict:
    """Pull both photos and playable video clips out of one rendered post.

    Photos and video thumbnails live in a CSS background-image. Actual playable
    clips are in a <video src> — those are the gold: a few seconds of the real
    footage the newsroom posted beats any still. Returns both, kept apart so the
    builder can prefer a clip and fall back to a photo.
    """
    photos, videos = [], []
    for node in block.select(
            ".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb,"
            " .tgme_widget_message_roundvideo_thumb, i.link_preview_image,"
            " i.link_preview_right_image"):
        m = re.search(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)",
                      node.get("style", ""))
        if m and m.group(1).startswith("http"):
            photos.append(m.group(1))
    for vid in block.select("video.tgme_widget_message_video"):
        src = vid.get("src", "")
        if src.startswith("http"):
            videos.append(src)
    return {"photos": photos, "videos": videos}


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
                media = _post_media(b)
                posts[mid] = {"time": when.strftime("%H:%M"), "text": text,
                              "images": media["photos"], "videos": media["videos"]}

        if oldest_day is None or oldest_day < day or oldest_id <= 1:
            break
        before = oldest_id

    ordered = [posts[k] for k in sorted(posts)]
    pics = sum(1 for p in ordered if p["images"])
    clips = sum(1 for p in ordered if p["videos"])
    print(f"{day}: collected {len(ordered)} posts from @{username} "
          f"({pics} with photos, {clips} with video)")
    return ordered


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


@functools.lru_cache(maxsize=1)
def tts_models() -> tuple[str, ...]:
    """Text-to-speech models this key can use, newest first.

    candidate_models() deliberately filters TTS models out (they cannot write
    the script), so the human-voice path needs its own lookup. Without this the
    "gemini" engine could never find a voice model and silently fell back to the
    robotic edge voice every time.
    """
    try:
        r = requests.get(f"{GEMINI_ROOT}/models",
                         headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"    could not list TTS models: {str(e)[:100]}")
        return ()
    names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
             if "tts" in m["name"].lower()
             and "generateContent" in m.get("supportedGenerationMethods", [])]

    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("preview" in n or "exp" in n,
                -(float(m.group(1)) if m else 0.0), len(n))

    return tuple(sorted(names, key=rank))


PROMPT = """You are the writer and anchor of a daily news bulletin called
MidWorld Daily.

Below is everything a news channel published on {date}, midnight to midnight
local time, with the time of each post. This is one complete day.

Write the spoken bulletin as a series of topic segments.

Return ONLY a JSON object, no markdown fences, in exactly this shape:
{{"headline": "one sentence summing up the whole day",
  "segments": [{{"topic": "Middle East",
                "headline": "four to eight words naming this topic's main story",
                "sources": [3, 17, 42],
                "photo": "2 to 4 words naming a real place to photograph",
                "script": "the spoken words..."}}]}}

Rules for the segments:
- One segment per topic. Use topics that fit the day, for example: Middle East,
  Russia and Ukraine, China and Asia, Europe, United States, Markets, Football,
  MMA. Skip a topic only if it genuinely had no news. Cover EVERY distinct
  region or subject that saw real reporting today — the viewer should feel they
  got the whole channel, not a hand-picked few. Between 4 and 9 segments.
- "topic" is a screen label: 1 to 3 words, no punctuation.
- each segment's "headline" is an on-screen caption for that topic: 4 to 8
  words, no final full stop. It is read by the viewer, not spoken.
- "sources" lists the numbers of the posts this segment is built from, most
  important first. Every post is numbered below. This is how the channel's own
  photograph of the event gets attached to the right topic, so put the post
  that best represents the story first. Between 1 and 6 numbers.
- each segment's "photo" is only a FALLBACK, used when none of the source posts
  carried a picture. It names a REAL, PHOTOGRAPHABLE PLACE connected to the
  story, which will be looked up in a photo archive. Use a city, country,
  landmark, building or institution: "Beirut", "Kyiv Ukraine", "Tokyo Stock
  Exchange", "Santiago Bernabeu stadium". Never a person's name, never an event,
  never a description of a scene, never anything violent or distressing.
- "script" is what the anchor says out loud for that topic: 90 to 170 words.
- Cover the whole day. Where a story developed over several hours, tell it in
  order — what was first reported, how it changed, where it stood by the end.
- Explain what happened and why it matters. Do not just read headlines.
- Use ONLY the information in the posts. Never add facts, numbers or names that
  are not there.
- Merge duplicate reports of one event. If several posts confirm it, you may
  say it was widely reported.
- The source labels single-source or unverified claims. Keep that hedging —
  say "according to a single report" rather than stating it as confirmed.
- Write for the EAR, not the page. This is the difference between a bulletin
  people finish and one they close:
  * Vary sentence length. Follow a long sentence with a very short one. A
    run of same-length sentences is what makes a read sound robotic.
  * Use contractions — "isn't", "they've", "that's". Written-out forms sound
    stiff when spoken.
  * Lead each item with the thing that happened, not with scene-setting.
    "Israel struck three villages overnight" beats "In a development that
    came late in the day, it was reported that...".
  * Link items with spoken connectives — "meanwhile", "elsewhere", "that
    matters because", "the other side of this" — so topics flow into each
    other instead of stopping dead.
  * Cut throat-clearing: "it is worth noting", "in terms of", "as far as X is
    concerned", "there were reports suggesting that". Say the thing.
  * Prefer active voice and concrete subjects. Name who did what.
  * Never begin consecutive sentences with the same word.
- Make it engaging, not just correct. A flat, even read is what makes news
  forgettable:
  * Bring warmth and energy, like a real anchor who finds this genuinely
    interesting. The personality is in the phrasing and rhythm — never in
    exaggeration, hype or opinion. Report it straight, but make it alive.
  * Give each story a shape: what happened, why it matters, what to watch next.
    Land the final line of each topic so it resolves instead of trailing off.
  * Use a vivid, exact verb over a vague one; a concrete detail over a generic
    phrase — but only details that are actually in the posts.
- Plain spoken language. Every character is read aloud, so no markdown, no
  emojis, no asterisks, no hashtags, no bullets, no links, and no numbers
  written as digits where a reader would say them differently — write "twenty
  thousand", not "20,000".
- Open the whole bulletin with a genuine hook — one crisp line on the single
  most striking thing that happened today — then a brief warm greeting and the
  day's headline. Do not open with a bare "welcome to the news". End the last
  segment with a short, warm sign-off that invites the viewer back tomorrow.

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


def _unescape(s: str) -> str:
    """Undo the JSON string escapes we care about when salvaging by regex."""
    return (s.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")
             .replace("\\r", " ").replace("\\/", "/").replace("\\\\", "\\")).strip()


def _salvage_segments(raw: str) -> list[dict]:
    """Recover whole segment objects from a reply whose JSON is truncated.

    When the top-level parse fails — a reply cut off mid-object — walk the
    "segments" array and keep every {...} object that IS complete and json-loads
    cleanly, matching braces while respecting strings. Far better than narrating
    the raw reply: the viewer hears finished topics, not half a JSON document.
    """
    out: list[dict] = []
    i = raw.find('"segments"')
    if i < 0:
        return out
    i = raw.find("[", i)
    if i < 0:
        return out

    depth, start, in_str, esc = 0, None, False, False
    for j in range(i, len(raw)):
        c = raw[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = j
            depth += 1
        elif c == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(raw[start:j + 1])
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict) and str(obj.get("script", "")).strip():
                        out.append(obj)
                    start = None
        elif c == "]" and depth == 0:
            break
    return out


def _prose_from_json(raw: str) -> str:
    """Lift only the spoken text out of a reply we could not parse.

    The last line of defence, so a broken reply never gets read out as JSON.
    The "script" fields carry the actual bulletin; pull them with a regex —
    including a final one that was cut off mid-sentence, which has no closing
    quote — and speak only those. Everything else (keys, brackets, the source
    numbers) is left on the floor.
    """
    parts = re.findall(r'"script"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
    return " ".join(_unescape(p) for p in parts if _unescape(p)).strip()


def _plain_text(raw: str) -> str:
    """Strip anything that reads as code, for a reply that was not our JSON."""
    text = re.sub(r'"(?:headline|topic|sources|photo|script|segments)"\s*:',
                  " ", raw)
    text = re.sub(r"[\{\}\[\]\"]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def summarize(posts: list[dict], day: dt.date) -> dict:
    items = "\n\n---\n\n".join(
        f"POST {i} [{p['time']}]{' [has photo]' if p.get('images') else ''}\n"
        f"{p['text']}"
        for i, p in enumerate(posts, 1))
    prompt = PROMPT.format(date=f"{day:%A, %d %B %Y}", items=items[:600_000])
    model = MODEL or candidate_models()[0]
    print(f"using model: {model}  |  prompt: {len(prompt):,} chars")

    # thinkingBudget=0 is the important line here. Gemini 2.5 Flash turns
    # "thinking" on by default, and those tokens count against maxOutputTokens.
    # Left on, the model can spend almost the whole budget reasoning and return
    # JSON that is cut off mid-segment; that fails to parse and the bulletin
    # falls back to reading the raw reply — keys, braces and source numbers —
    # out loud. Disabling it keeps the budget for the actual script. Older
    # generations (2.0 Flash) reject the field with a generic INVALID_ARGUMENT;
    # the retry loop below drops it, then the cap, on a 400.
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 16384,
                             "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 0}},
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
        if r.status_code == 400:
            # Degrade one field at a time rather than dropping the whole config
            # (which would also lose responseMimeType JSON). Thinking config and
            # an over-large token cap are the two fields older models reject.
            gc = body.get("generationConfig")
            if gc and "thinkingConfig" in gc:
                gc.pop("thinkingConfig")
                print("400 — model rejects thinkingConfig, dropping it")
                continue
            if gc and gc.get("maxOutputTokens", 0) > 8192:
                gc["maxOutputTokens"] = 8192
                print("400 — capping maxOutputTokens at 8192")
                continue
            if "generationConfig" in body:
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

    headline, segments = "", []
    try:
        brief = _extract_json(raw)
        headline = brief.get("headline", "") or ""
        segments = [s for s in brief.get("segments", [])
                    if isinstance(s, dict) and str(s.get("script", "")).strip()]
    except Exception as e:
        print(f"reply was not clean JSON ({e})", file=sys.stderr)

    # A truncated reply (a busy day, or thinking tokens eating the budget) leaves
    # the top-level JSON unparseable. Recover whatever whole segments did arrive
    # rather than narrating the raw reply — braces, keys and source numbers and
    # all, which is exactly the "reading out the JSON" failure this guards.
    if not segments:
        segments = _salvage_segments(raw)
        if segments:
            print(f"recovered {len(segments)} segment(s) from a truncated reply",
                  file=sys.stderr)
        if not headline:
            m = re.search(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
            if m:
                headline = _unescape(m.group(1))

    # Absolute last resort: speak only the script text, never the JSON scaffolding.
    if not segments:
        prose = _prose_from_json(raw) or _plain_text(raw)
        print("no segments recoverable — narrating cleaned text only",
              file=sys.stderr)
        return {"headline": headline,
                "segments": [{"topic": "Today", "script": prose}]}

    for s in segments:
        label = re.sub(r"[^\w &-]", "", s.get("topic", "News")).strip()
        s["topic"] = label[:22] or "News"
        src = s.get("sources") or []
        s["sources"] = [int(n) for n in src
                        if isinstance(n, (int, float, str))
                        and str(n).strip().lstrip("-").isdigit()][:6]
        head = re.sub(r"\s+", " ", str(s.get("headline", ""))).strip(" .")
        s["headline"] = head[:58]
        s["photo"] = re.sub(r"[^\w\s-]", " ", str(s.get("photo", ""))).strip()[:60]
    return {"headline": headline, "segments": segments}


# ----------------------------------------------------------------------------
# 3. Voice + captions
# ----------------------------------------------------------------------------
def _ass_time(seconds: float) -> str:
    cs = max(int(round(seconds * 100)), 0)
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6_000)
    sec, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


# Captions are written as ASS rather than SRT on purpose. ffmpeg's SRT decoder
# lays subtitles out in a fixed 384x288 space and scales the result to the
# frame, so a Fontsize of 18 arrives at ~45px and MarginV=46 lifts the text
# ~115px — which parked the captions on top of the lower third instead of below
# it. An ASS file declares its own PlayResX/PlayResY, so every number here is
# real pixels at 1280x720. (The subtitles filter's original_size option does not
# fix this; it only corrects aspect ratio.)
ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,DejaVu Sans,22,&H00FFFFFF,&H000000FF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,4,1,0,2,80,80,46,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


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


def _sentences(text: str) -> list[str]:
    """Split narration into whole sentences — never mid-sentence.

    An earlier version also broke long sentences at commas so captions would fit.
    Each fragment was synthesised separately, and every speech clip carries its
    own leading and trailing silence, so each comma became an audible stop where
    a speaker would have run straight through. Measured on a real bulletin: 21
    pauses in 40 seconds, one every 1.9 seconds. Long sentences now stay whole
    and it is the caption that gets divided, in _cues_for.
    """
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        sent = sent.strip()
        if not sent:
            continue
        # a fragment on its own becomes a caption that flashes past, and a
        # separate speech request; fold it into the sentence before it
        if out and len(sent) < 30:
            out[-1] = f"{out[-1]} {sent}"
        else:
            out.append(sent)
    return out


def _cues_for(sentence: str, start: float, span: float) -> list[tuple]:
    """Caption cues for one spoken sentence, divided if it is long.

    The audio is never cut, so the split is a proportional estimate — but inside
    a single sentence that is accurate to a fraction of a second, because the
    speaking rate barely varies across a few seconds of continuous delivery.
    """
    limit = 84                        # two caption lines of ~42 characters
    if len(sentence) <= limit:
        return [(start, start + span, sentence)]

    words, chunks, cur = sentence.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > limit:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        if len(cur) < 25 and chunks:   # avoid a stray tail cue
            chunks[-1] = f"{chunks[-1]} {cur}"
        else:
            chunks.append(cur)

    total = sum(len(c) for c in chunks) or 1
    cues, clock = [], start
    for c in chunks:
        share = span * len(c) / total
        cues.append((clock, clock + share, c))
        clock += share
    return cues


async def _speak(text: str, mp3_path: str) -> None:
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(mp3_path)


# Silence trimmed from the head and tail of every clip, then one deliberate gap
# inserted between sentences. Left alone, each clip's own padding stacks with
# the next one's and the delivery turns stop-start.
GAP = float(os.getenv("SENTENCE_GAP", "0.20"))
TRIM = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0"
        ":detection=peak,areverse,"
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0"
        ":detection=peak,areverse")


def _silence_splits(wav: str, want: int) -> list[float] | None:
    """Find `want` sentence boundaries in a clip by locating its longest pauses.

    Used when a whole segment is synthesised in one request, so there are no
    per-sentence durations to add up. A newsreader pauses between sentences, so
    the longest silences are the boundaries; taking the `want` longest and
    putting them back in time order recovers the timings. Returns None when the
    audio does not contain enough distinct pauses to be confident.
    """
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", wav, "-af",
         "silencedetect=noise=-38dB:d=0.16", "-f", "null", "-"],
        capture_output=True, text=True).stderr

    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", out)]
    spans = [(b - a, (a + b) / 2) for a, b in zip(starts, ends) if b > a]
    if len(spans) < want:
        return None
    chosen = sorted(spans, reverse=True)[:want]
    return sorted(mid for _, mid in chosen)


def gemini_voice(text: str, wav_path: str) -> bool:
    """Speak a whole segment with Gemini TTS. False if it is not usable.

    Noticeably more natural than edge-tts, and it takes a direction for tone,
    which is the part that makes it sound like a person reading the news rather
    than a machine reading a page. The catch is the free tier: about ten
    requests a day, so this runs once per topic rather than once per sentence,
    and any failure falls straight back to edge-tts.
    """
    model = TTS_MODEL or next(iter(tts_models()), "")
    if not model:
        return False

    direction = ("Read this as a professional television news anchor: warm, "
                 "measured, engaged. Vary your pace and emphasis naturally, "
                 "lift slightly at the start of each new story, and let the "
                 "important words land. Do not sound flat or rushed.\n\n")
    body = {
        "contents": [{"parts": [{"text": direction + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig":
                                             {"voiceName": TTS_VOICE}}},
        },
    }
    try:
        r = requests.post(f"{GEMINI_ROOT}/models/{model}:generateContent",
                          headers={"x-goog-api-key": GEMINI_KEY,
                                   "Content-Type": "application/json"},
                          json=body, timeout=300)
        if not r.ok:
            print(f"    gemini tts {r.status_code}: {r.text[:160]}")
            return False
        parts = r.json()["candidates"][0]["content"]["parts"]
        blob = next((p["inlineData"]["data"] for p in parts if "inlineData" in p), None)
        if not blob:
            return False
        pcm = base64.b64decode(blob)
        raw = wav_path + ".pcm"
        with open(raw, "wb") as f:
            f.write(pcm)
        # the API returns headerless signed 16-bit little-endian PCM at 24 kHz
        video.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le",
                   "-ar", "24000", "-ac", "1", "-i", raw,
                   "-ar", "48000", wav_path])
        return os.path.getsize(wav_path) > 4000
    except Exception as e:
        print(f"    gemini tts failed: {str(e)[:120]}")
        return False


def voice_segment(text: str, mp3_path: str, srt_path: str, work: str,
                  tag: str, engine: str | None = None) -> tuple[float, str]:
    """Narrate one segment; return (duration, voice actually used).

    The caller passes an explicit engine so the whole bulletin can be held to a
    single voice — the returned voice name lets it notice a mid-bulletin
    fallback and re-read everything consistently.

    Two things are going on here.

    Sync: edge-tts word-boundary events are unreliable, and estimating caption
    times by character count drifts badly. Each sentence is synthesised on its
    own and measured, so every caption start is the exact sum of the audio
    before it.

    Delivery: synthesising separately means each clip arrives padded with
    silence at both ends, and those pads stack at every join — about 0.63s per
    sentence, which is what makes the read sound stop-start. So each clip is
    trimmed hard and a deliberate gap is inserted instead: a short breath after
    a clause, a slightly longer one after a full stop. The result runs
    continuously at a news pace rather than pausing everywhere.
    """
    engine = engine or ENGINE
    sentences = _sentences(text)
    if not sentences:
        raise RuntimeError("nothing to narrate")

    # One Gemini request for the whole segment, when enabled. Captions are then
    # recovered from the pauses in the audio rather than from per-sentence
    # durations, because synthesising sentence by sentence would exhaust the
    # daily quota in a single bulletin.
    if engine == "gemini":
        whole = os.path.join(work, f"{tag}_whole.wav")
        if gemini_voice(" ".join(sentences), whole):
            span = video.probe_duration(whole)
            splits = _silence_splits(whole, len(sentences) - 1)
            if splits is None:
                # no clear pauses: fall back to splitting by text length, which
                # is rough but stays inside one segment so it cannot drift far
                total_chars = sum(len(x) for x in sentences) or 1
                acc, splits = 0, []
                for sent in sentences[:-1]:
                    acc += len(sent)
                    splits.append(span * acc / total_chars)
                print("    (no clear pauses — captions spaced by length)")
            video.run(["ffmpeg", "-y", "-loglevel", "error", "-i", whole,
                       "-c:a", "libmp3lame", "-q:a", "3", mp3_path])
            bounds = [0.0] + splits + [span]
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(ASS_HEAD)
                for i, sent in enumerate(sentences):
                    # long sentences become several short captions rather than
                    # one that overflows into four lines and swamps the screen
                    for start, end, chunk in _cues_for(sent, bounds[i],
                                                        bounds[i + 1] - bounds[i]):
                        line = _wrap(chunk).replace("\n", "\\N")
                        f.write(f"Dialogue: 0,{_ass_time(start)},"
                                f"{_ass_time(max(end - 0.04, start + 0.3))},"
                                f"Main,,0,0,0,,{line}\n")
            print(f"    {span:5.1f}s audio, {len(sentences)} captions (gemini)")
            return span, "gemini"
        print("    gemini tts unavailable, using edge-tts")

    pieces, cues, clock = [], [], 0.0
    for i, sent in enumerate(sentences):
        raw = os.path.join(work, f"{tag}_{i:03d}_raw.mp3")
        asyncio.run(_speak(sent, raw))
        if not os.path.exists(raw) or os.path.getsize(raw) < 200:
            continue                          # skip anything the voice refused
        clip = os.path.join(work, f"{tag}_{i:03d}.wav")
        span = video.trim_silence(raw, clip)
        if span < 0.15:                       # trimmed to nothing
            continue

        pieces.append(clip)
        # split a long sentence into short caption cues over its own span, so
        # no single caption balloons past two lines
        cues.extend(_cues_for(sent, clock, span))
        clock += span

        if i < len(sentences) - 1:
            # a clause continues the thought, a sentence closes one
            gap = 0.17 if sent.rstrip().endswith(",") else 0.30
            pieces.append(video.silence(work, gap, f"{tag}_{i:03d}_gap.wav"))
            clock += gap

    if not pieces:
        raise RuntimeError("voice synthesis produced no audio")

    video.join_audio(work, pieces, mp3_path)

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(ASS_HEAD)
        for start, end, line in cues:
            stop = max(end - 0.04, start + 0.3)
            text_line = _wrap(line).replace("\n", "\\N")
            f.write(f"Dialogue: 0,{_ass_time(start)},{_ass_time(stop)},"
                    f"Main,,0,0,0,,{text_line}\n")

    total = video.probe_duration(mp3_path)
    print(f"    {total:5.1f}s audio, {len(cues)} captions (measured, trimmed)")
    return total, "edge"


def _wrap(line: str, width: int = 42) -> str:
    """Two short lines read better on a phone than one long one."""
    if len(line) <= width:
        return line
    words, out, cur = line.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return "\n".join(out[:2]) if len(out) <= 2 else "\n".join(
        [" ".join(out[:len(out)//2]), " ".join(out[len(out)//2:])])


# ----------------------------------------------------------------------------
# Topic imagery
# ----------------------------------------------------------------------------
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
IMAGE_BASE = os.getenv("IMAGE_BASE", "https://image.pollinations.ai/prompt")
WANT_IMAGES = os.getenv("TOPIC_IMAGES", "1") == "1"
# real photographs only by default; set to 1 to allow a generated image when
# the archive has nothing usable
ALLOW_GENERATED = os.getenv("ALLOW_GENERATED_IMAGES", "0") == "1"
CLIPS = os.getenv("SOURCE_CLIPS", "1") == "1"
MUSIC = os.getenv("MUSIC_BED", "1") == "1"
# a held beat after each topic and a dissolve into the next, so the bulletin
# moves between stories the way a broadcast does rather than snapping across
TOPIC_PAUSE = float(os.getenv("TOPIC_PAUSE", "0.8"))
SCENE_XFADE = float(os.getenv("SCENE_XFADE", "0.5"))

UA_HEADERS = {"User-Agent": "MidWorldDaily/1.0 (Telegram news digest bot)"}
_BAD_FILE = ("logo", "icon", "map of", "flag of", "coat of arms", "diagram",
             "chart", "seal of", "emblem", ".svg")


def _commons_search(query: str) -> tuple[str, str] | None:
    """Find a real photograph on Wikimedia Commons. Returns (url, credit).

    Commons is used rather than an image generator because generated pictures of
    news events are invented — they show a plausible-looking place that does not
    correspond to anything that happened. A real photograph of the actual city
    or building is both honest and better looking. The licences require credit,
    so the photographer and licence are carried through and printed on screen.
    """
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": "6",
        "gsrlimit": "20", "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": "1024",
    }
    r = requests.get(COMMONS_API, params=params, headers=UA_HEADERS, timeout=45)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}

    best = None
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        title = page.get("title", "").lower()
        url = info.get("thumburl") or info.get("url")
        if not url or any(bad in title for bad in _BAD_FILE):
            continue
        width, height = info.get("width", 0), info.get("height", 0)
        if width < 800 or height < 500:
            continue
        if width / max(height, 1) < 1.15:        # portraits crop badly to a panel
            continue
        meta = info.get("extmetadata") or {}
        artist = re.sub(r"<[^>]+>", "", str(meta.get("Artist", {}).get("value", "")))
        licence = str(meta.get("LicenseShortName", {}).get("value", "")).strip()
        credit = " / ".join(x for x in
                            (re.sub(r"\s+", " ", artist).strip()[:40], licence) if x)
        score = width * height
        if best is None or score > best[0]:
            best = (score, url, f"{credit} — Wikimedia Commons" if credit
                    else "Wikimedia Commons")
    return (best[1], best[2]) if best else None


def _download(url: str, path: str, headers: dict) -> bool:
    r = requests.get(url, timeout=60, headers=headers)
    if not r.ok or len(r.content) < 8000:
        print(f"    image unavailable ({r.status_code}, {len(r.content)} bytes)")
        return False
    if not r.headers.get("content-type", "").startswith("image/"):
        print("    response was not an image")
        return False
    with open(path, "wb") as f:
        f.write(r.content)
    # ffprobe exits 0 on a non-image and reports 0x0, so check the dimensions
    w, h = video.probe_size(path)
    if w < 200 or h < 200:
        print(f"    image did not decode ({w}x{h})")
        return False
    return True


def segment_media(seg: dict, posts: list[dict], work: str,
                  tag: str, seconds: float) -> tuple[str, str, bool] | None:
    """The panel for one topic. Returns (path, credit, is_video) or None.

    Order of preference:
      1. a VIDEO clip from one of the posts this segment came from — the actual
         footage the newsroom published, which is far more alive than a still
      2. a photograph attached to one of those posts
      3. a real photograph of the named place from Wikimedia Commons
      4. a generated illustration, only if explicitly enabled

    Everything degrades quietly: a scene with no panel is fine; a build that
    dies over one missing clip is not.
    """
    if not WANT_IMAGES:
        return None

    # 1. the channel's own video footage, in Gemini's ranked post order
    if CLIPS:
        for n in seg.get("sources", []):
            if not 1 <= n <= len(posts):
                continue
            for url in posts[n - 1].get("videos", []):
                raw = os.path.join(work, f"{tag}_clip.mp4")
                try:
                    if _download(url, raw, UA) and video.probe_duration(raw) >= 1.0:
                        panel = video.make_video_panel(
                            work, raw, f"{tag}_vpanel.mp4", seconds)
                        return panel, f"@{SOURCE}", True
                except Exception as e:
                    print(f"    post {n} clip failed: {str(e)[:70]}")

    # 2-4. fall back to a still image
    raw = os.path.join(work, f"{tag}.jpg")
    hit = segment_image(seg, posts, raw)
    if hit:
        try:
            return video.make_panel(work, hit[0], f"{tag}_panel.png"), hit[1], False
        except Exception as e:
            print(f"    still panel failed: {str(e)[:70]}")
    return None


def segment_image(seg: dict, posts: list[dict], path: str) -> tuple[str, str] | None:
    """The picture for one topic, preferring the source channel's own photo.

    Order of preference:
      1. a photograph attached to one of the posts this segment was written
         from — the actual picture the newsroom published with that story
      2. a real photograph of the named place from Wikimedia Commons
      3. a generated illustration, only if explicitly enabled

    The first is what makes the video feel like it belongs to the channel
    instead of being decorated with stock imagery. Everything degrades quietly:
    a scene with no picture is fine, a build that dies over a missing picture is
    not.
    """
    if not WANT_IMAGES:
        return None

    # 1. the channel's own photographs, in the order Gemini ranked the posts
    for n in seg.get("sources", []):
        if not 1 <= n <= len(posts):
            continue
        for url in posts[n - 1].get("images", []):
            try:
                if _download(url, path, UA):
                    return path, f"@{SOURCE}"
            except Exception as e:
                print(f"    post {n} image failed: {str(e)[:70]}")

    # 2. a real photograph of the place
    query = seg.get("photo", "")
    if query:
        try:
            hit = _commons_search(query)
            if hit and _download(hit[0], path, UA_HEADERS):
                return path, hit[1]
        except Exception as e:
            print(f"    commons lookup failed: {str(e)[:80]}")

    # 3. generated, off by default and always labelled as an illustration
    if ALLOW_GENERATED and query:
        styled = f"{query}, editorial news photograph, muted colours, no text"
        url = (f"{IMAGE_BASE}/{requests.utils.quote(styled)}"
               f"?width=768&height=432&nologo=true&seed={abs(hash(query)) % 9999}")
        try:
            if _download(url, path, UA_HEADERS):
                return path, "illustration"
        except Exception as e:
            print(f"    generated image failed: {str(e)[:80]}")

    return None


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


# ----------------------------------------------------------------------------
def build(brief: dict, day: dt.date, work: str,
          posts: list[dict]) -> str:
    """Narrate every segment, render a scene for each, join them up."""
    segments = brief["segments"]
    date_text = f"{day:%d %B %Y}"

    def narrate(engine: str):
        audio, srts, spans, used = [], [], [], []
        for i, seg in enumerate(segments):
            raw_mp3 = os.path.join(work, f"seg{i}.mp3")
            srt = os.path.join(work, f"seg{i}.ass")
            print(f"  {i + 1}. {seg['topic']}")
            span, eng = voice_segment(seg["script"], raw_mp3, srt, work, f"v{i}",
                                      engine=engine)
            # hold the anchor for a beat after each story; captions end before
            # the pad, so their timing is untouched
            mp3 = raw_mp3
            if TOPIC_PAUSE > 0:
                mp3 = os.path.join(work, f"seg{i}_held.mp3")
                video.pad_audio_tail(raw_mp3, mp3, TOPIC_PAUSE)
                span += TOPIC_PAUSE
            spans.append(span); audio.append(mp3); srts.append(srt); used.append(eng)
        return audio, srts, spans, used

    print("narrating:")
    audio, srts, spans, used = narrate(ENGINE)
    # One voice for the whole bulletin. The Gemini free tier often runs out of
    # TTS quota partway through, which would leave the first topics in the human
    # voice and the rest in the robotic one — more jarring than either alone. If
    # that happens, re-read everything in the fallback voice so it stays uniform.
    if ENGINE == "gemini" and "gemini" in used and "edge" in used:
        print("gemini quota ran out mid-bulletin — re-narrating in one voice (edge)",
              file=sys.stderr)
        audio, srts, spans, used = narrate("edge")

    total = sum(spans)
    print(f"bulletin: {len(segments)} segments, {total / 60:.1f} minutes")

    intro = os.path.join(work, "intro.mp4")
    video.render_card(work, 4.5,
                      [(BRAND, 54, "white"),
                       (f"{day:%A, %d %B %Y}", 26, video.PALE),
                       ("The full day in review", 22, video.PALE)],
                      intro)
    parts = [intro]

    # the ticker carries the day's topics so the strip always has content
    ticker = "     •     ".join(
        f"{s['topic'].upper()}: {s.get('headline') or 'latest'}"
        for s in segments)
    ticker = f"{ticker}     •     "

    print("finding footage:")
    panels: list[str | None] = []
    credits: list[str] = []
    is_video: list[bool] = []
    for i, seg in enumerate(segments):
        got = segment_media(seg, posts, work, f"m{i}", spans[i])
        if got:
            panels.append(got[0])
            credits.append(got[1])
            is_video.append(got[2])
            kind = "video" if got[2] else "photo"
            print(f"  {i + 1}. {seg['topic']} <- {got[1]} ({kind})")
        else:
            panels.append(None)
            credits.append("")
            is_video.append(False)

    print("rendering scenes:")
    elapsed = 0.0
    for i, seg in enumerate(segments):
        out = os.path.join(work, f"scene{i}.mp4")
        print(f"  {i + 1}/{len(segments)} {seg['topic']} ({spans[i]:.0f}s)")
        video.render_scene(work, PRESENTER, audio[i], srts[i], seg["topic"],
                           seg.get("headline", ""), BRAND, date_text, ticker,
                           i, elapsed, total, out, panel=panels[i],
                           credit=credits[i], panel_is_video=is_video[i])
        elapsed += spans[i]
        parts.append(out)

    outro = os.path.join(work, "outro.mp4")
    video.render_card(work, 3.5,
                      [(BRAND, 44, "white"),
                       (f"@{TARGET.lstrip('@')}", 24, video.PALE)],
                      outro, wipe=False)
    parts.append(outro)

    joined = os.path.join(work, "joined.mp4")
    video.crossfade_concat(parts, joined, SCENE_XFADE)

    final = joined
    if MUSIC:
        try:
            bed = video.build_bed(work, video.probe_duration(joined))
            scored = os.path.join(work, "brief.mp4")
            video.add_music(joined, bed, scored)
            final = scored
            print("music bed mixed under the narration")
        except Exception as e:
            # the bulletin is finished either way; music is the optional layer
            print(f"music step skipped: {str(e)[:150]}", file=sys.stderr)
    size = os.path.getsize(final)
    dur = video.probe_duration(final)
    print(f"final video: {size / 1e6:.1f} MB, {dur:.0f}s")

    # Telegram bots may send up to 50 MB per file. A bulletin is always one
    # video, never split across messages — if it is too big it is re-encoded to
    # a target bitrate that fits, which for a talking-head-style still is
    # visually lossless. This is why the day comes as a single post.
    limit = 49 * 1024 * 1024
    if size > limit:
        target_kbps = int((limit * 8 / dur) / 1000 * 0.92) - 128   # leave room for audio
        target_kbps = max(target_kbps, 300)
        shrunk = os.path.join(work, "brief_fit.mp4")
        print(f"over 50 MB — re-encoding to ~{target_kbps} kbps video")
        video.run(["ffmpeg", "-y", "-loglevel", "error", "-i", final,
                   "-c:v", "libx264", "-preset", "medium",
                   "-b:v", f"{target_kbps}k", "-maxrate", f"{int(target_kbps * 1.3)}k",
                   "-bufsize", f"{target_kbps * 2}k", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "112k", "-movflags", "+faststart", shrunk])
        final = shrunk
        print(f"re-encoded to {os.path.getsize(final) / 1e6:.1f} MB")
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
        # Video only, by request. If the render fails the run fails loudly (and
        # can be re-run) rather than quietly sending a separate voice note —
        # the channel gets the video bulletin or nothing.
        final = build(brief, day, work, posts)
        if DRY_RUN:
            # preview mode: keep the finished file for inspection (the workflow
            # uploads it as an artifact) and do NOT post anything to Telegram
            preview = os.path.join(os.getcwd(), "preview.mp4")
            shutil.copyfile(final, preview)
            print(f"dry run — wrote {preview} ({os.path.getsize(preview)/1e6:.1f} MB), "
                  f"nothing published")
            return
        publish_video(day, brief.get("headline", ""), final)
        print("published video")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
