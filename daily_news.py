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
# a touch quicker than default reads as a broadcaster rather than a reader
RATE = os.getenv("VOICE_RATE", "+8%")
PRESENTER = os.getenv("PRESENTER_IMAGE", "presenter.png")
BRAND = os.getenv("BRAND", "MIDWORLD DAILY")
FORCE_DATE = os.getenv("TARGET_DATE", "").strip()

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
  "segments": [{{"topic": "Middle East",
                "headline": "four to eight words naming this topic's main story",
                "photo": "2 to 4 words naming a real place to photograph",
                "script": "the spoken words..."}}]}}

Rules for the segments:
- One segment per topic. Use topics that fit the day, for example: Middle East,
  Russia and Ukraine, China and Asia, Europe, United States, Markets, Football,
  MMA. Skip any topic with no news. Between 3 and 8 segments.
- "topic" is a screen label: 1 to 3 words, no punctuation.
- each segment's "headline" is an on-screen caption for that topic: 4 to 8
  words, no final full stop. It is read by the viewer, not spoken.
- each segment's "photo" names a REAL, PHOTOGRAPHABLE PLACE connected to the
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
- Plain spoken language. Every character is read aloud, so no markdown, no
  emojis, no asterisks, no hashtags, no bullets, no links, and no numbers
  written as digits where a reader would say them differently — write "twenty
  thousand", not "20,000".
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
        head = re.sub(r"\s+", " ", str(s.get("headline", ""))).strip(" .")
        s["headline"] = head[:58]
        s["photo"] = re.sub(r"[^\w\s-]", " ", str(s.get("photo", ""))).strip()[:60]
    return {"headline": brief.get("headline", ""), "segments": segments}


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
Style: Main,DejaVu Sans,27,&H00FFFFFF,&H000000FF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,4,0,0,2,70,70,34,1

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
    limit = 104                       # two caption lines of ~52 characters
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


def voice_segment(text: str, mp3_path: str, srt_path: str, work: str,
                  tag: str) -> float:
    """Narrate one segment and caption it from measured audio, not estimates.

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
    sentences = _sentences(text)
    if not sentences:
        raise RuntimeError("nothing to narrate")

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
        cues.append((clock, clock + span, sent))
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
    return total


def _wrap(line: str, width: int = 52) -> str:
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
MUSIC = os.getenv("MUSIC_BED", "1") == "1"

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


def fetch_topic_image(subject: str, query: str,
                      path: str) -> tuple[str, str] | None:
    """Get one real photograph for a topic. Returns (path, credit) or None.

    Commons first, because a real photograph of the actual place is both honest
    and better looking than an invented one. A generated image is only used if
    ALLOW_GENERATED_IMAGES is switched on, and it is credited as an illustration
    so it is never mistaken for documentary footage.

    Every failure path returns None and the scene simply renders without a
    panel. This is the only step that depends on an outside service, and a
    bulletin missing a side image beats a bulletin that failed to build.
    """
    if not (WANT_IMAGES and (subject or query)):
        return None

    try:
        hit = _commons_search(query or subject)
        if hit and _download(hit[0], path, UA_HEADERS):
            return path, hit[1]
    except Exception as e:
        print(f"    commons lookup failed: {str(e)[:100]}")

    if ALLOW_GENERATED and subject:
        styled = f"{subject}, editorial news photograph, muted colours, no text"
        url = (f"{IMAGE_BASE}/{requests.utils.quote(styled)}"
               f"?width=768&height=432&nologo=true"
               f"&seed={abs(hash(subject)) % 9999}")
        try:
            if _download(url, path, UA_HEADERS):
                return path, "illustration"
        except Exception as e:
            print(f"    generated image failed: {str(e)[:100]}")

    print("    no usable photograph")
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
        srt = os.path.join(work, f"seg{i}.ass")
        print(f"  {i + 1}. {seg['topic']}")
        spans.append(voice_segment(seg["script"], mp3, srt, work, f"v{i}"))
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

    # the ticker carries the day's topics so the strip always has content
    ticker = "     •     ".join(
        f"{s['topic'].upper()}: {s.get('headline') or 'latest'}"
        for s in segments)
    ticker = f"{ticker}     •     "

    print("finding photographs:")
    panels: list[str | None] = []
    credits: list[str] = []
    for i, seg in enumerate(segments):
        raw = os.path.join(work, f"img{i}.jpg")
        got = fetch_topic_image(seg.get("photo", ""), seg.get("photo", ""), raw)
        if got:
            try:
                panels.append(video.make_panel(work, got[0], f"panel{i}.png"))
                credits.append(got[1])
                print(f"  {i + 1}. {seg['topic']} <- {seg.get('photo', '')}")
                continue
            except Exception as e:
                print(f"  {i + 1}. {seg['topic']}: unusable ({str(e)[:60]})")
        panels.append(None)
        credits.append("")

    print("rendering scenes:")
    elapsed = 0.0
    for i, seg in enumerate(segments):
        out = os.path.join(work, f"scene{i}.mp4")
        print(f"  {i + 1}/{len(segments)} {seg['topic']} ({spans[i]:.0f}s)")
        video.render_scene(work, PRESENTER, audio[i], srts[i], seg["topic"],
                           seg.get("headline", ""), BRAND, date_text, ticker,
                           i, elapsed, total, out, panel=panels[i],
                           credit=credits[i])
        elapsed += spans[i]
        parts.append(out)

    outro = os.path.join(work, "outro.mp4")
    video.render_card(work, 3.5,
                      [(BRAND, 44, "white"),
                       (f"@{TARGET.lstrip('@')}", 24, video.PALE)],
                      outro, wipe=False)
    parts.append(outro)

    joined = os.path.join(work, "joined.mp4")
    video.concat(parts, joined)

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
