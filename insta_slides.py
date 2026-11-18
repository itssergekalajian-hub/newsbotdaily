"""Render and publish a branded 4:5 MidWorld Daily Instagram carousel.

Two entry points, run as separate workflow steps so the slides are committed
(and publicly addressable) before Instagram is asked to fetch them:

    python insta_slides.py render    — write slides/latest/*.jpg + deck.json
    python insta_slides.py publish   — post the carousel via the IG Graph API
    python insta_slides.py check-auth — print the connected Instagram username

Publishing needs two repo secrets — IG_USER_ID (the Instagram Business
account's numeric id) and IG_ACCESS_TOKEN (a long-lived Graph API token with
instagram_content_publish) — and quietly explains what's missing when they
aren't set, so the render keeps working before the account exists.
"""

import base64
import datetime as dt
import functools
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

import daily_news as dn
import video

W, H = 1080, 1350                 # Instagram portrait
OUT_DIR = "slides/latest"
TEMPLATES = "templates/ig"
IG_MARKER = ".last_ig"            # last day actually POSTED to Instagram
MAX_STORIES = 8                   # Instagram Graph API caps a carousel at 10 items
MAX_SLIDES = MAX_STORIES + 2      # cover + stories + closing follow slide
CAPTION_HEADER = "Here's What Happened in the World in the Last 24 Hours:"
CAPTION_FOOTER = "\u26a1 24/7 live breaking wire & market alerts: @midworldnews (Link in bio)"
BRAND = "MIDWORLD DAILY"
IG_HANDLE = os.getenv("IG_HANDLE", "@midworlddaily").strip() or "@midworlddaily"
if not IG_HANDLE.startswith("@"):
    IG_HANDLE = f"@{IG_HANDLE}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ANTON = os.path.join(video.BUNDLED_FONT_DIR, "Anton-Regular.ttf")
OBSIDIAN = "#070A0F"
CYAN = "#00E5FF"
SIGNAL_GOLD = "#FFC400"
YELLOW = SIGNAL_GOLD
TEXT_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-4-hydrogen",
)
DEPRECATED_TEXT_MODELS = frozenset({
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-001",
    "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.5-flash-preview-09-2025",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-3.1-flash-lite-preview",
})
IMAGE_MODELS = (
    "gemini-nano-banana-2.1",
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
)
MODEL_BACKOFFS = (5, 15, 25)

REGION_ART = (
    (("sport", "football", "mma", "tennis", "basket", "olymp", "formula"),
     "sports.png"),
    (("middle east", "gulf", "israel", "iran", "gaza", "lebanon", "syria",
      "saudi", "qatar", "yemen", "iraq", "turkey"), "mideast.png"),
    (("europe", "ukraine", "russia", "britain", "france", "german", "spain",
      "italy", "poland", "balkan"), "europe.png"),
    (("asia", "china", "japan", "korea", "india", "taiwan", "pacific",
      "indonesia", "pakistan", "afghan"), "asia.png"),
    (("america", "united states", "washington", "canada", "brazil", "mexico",
      "argentin", "venezuel", "latin"), "americas.png"),
    (("africa", "nigeria", "kenya", "egypt", "ethiop", "sudan", "sahel",
      "congo", "maghreb"), "africa.png"),
)


def art_for(region: str) -> str:
    low = region.lower()
    for keys, name in REGION_ART:
        if any(k in low for k in keys):
            return os.path.join(TEMPLATES, name)
    return os.path.join(TEMPLATES, "global.png")


# The context-scene library provides branded fallbacks when image generation
# and a story-specific press photo are both unavailable.
SCENES = {
    "missile_strike": "missiles streak across a night sky over a burning city",
    "fighter_jets": "fighter jets bank over desert terrain, smoke below",
    "drones_refinery": "drones circle a burning oil refinery at night",
    "naval_strait": "warships escort an oil tanker through a narrow strait",
    "tank_column": "a column of tanks on a muddy winter road",
    "soldiers_trench": "soldiers in a trench at dusk, flares on the horizon",
    "city_ruins": "a rubble-strewn city street in smoke after a strike",
    "summit_hall": "a grand negotiation hall, long table, rows of flags",
    "handshake": "a handshake between suited figures before flags",
    "assembly_podium": "a podium in a vast international assembly hall",
    "election": "a ballot box in a dark polling hall",
    "protest_night": "a huge night protest crowd with banners and flares",
    "riot_police": "a line of riot police with shields in smoke",
    "courtroom": "a gavel and scales in a dark courtroom",
    "markets_up": "a trading floor glowing green, screens rising",
    "markets_down": "a trading floor glowing red, screens falling",
    "oil_energy": "pumpjacks and a refinery against a sunset",
    "pipeline": "a gas pipeline crossing a snowy landscape",
    "tech_ai": "a glowing data centre corridor, circuit-board light",
    "rocket_launch": "a rocket lifting off at night",
    "city_fire": "firefighters silhouetted against a burning building",
    "flood_rescue": "a rescue boat on a flooded city street",
    "earthquake": "rescue workers on a collapsed building at dawn",
    "aviation": "an airliner on a rain-slick tarmac under storm light",
    "football_night": "a packed football stadium, players celebrating",
    "mma_cage": "an octagon cage under a single spotlight",
}


def scene_art(art: str | None) -> str | None:
    if isinstance(art, str) and art.strip().lower() in SCENES:
        path = os.path.join(TEMPLATES, "scenes",
                            f"{art.strip().lower()}.png")
        if os.path.exists(path):
            return path
    return None


SLIDES_PROMPT = """You are the editor of MidWorld Daily, a world and markets
news carousel covering the last 24 hours ending {date}. Select exactly eight
distinct, consequential stories from the channel posts below, prioritizing
world affairs and markets. Do not repeat the same event. Put the most important
story first. The eight story slides plus a cover and closing slide make a
10-item Instagram carousel.

Return ONLY JSON, no markdown fences:
{{"cover_scene_prompt":"A massive photorealistic Earth on a dark space horizon,
glowing with electric-cyan (#00E5FF) network grid lines and fiery golden
(#FFC400) shockwaves, flanked by visual motifs from today's top three stories
(such as jets, vessels, or market candlesticks) and 2-3 dramatic chest-up
figures representing today's biggest stories in sharp attire in the
upper-middle foreground. The center subject holds a signature prop from
today's standout headline. Vertical 4:5 editorial composition; no text.",
 "slides":[{{"region":"World, Markets, or another concise category",
 "headline":"A punchy, uppercase, one-sentence statement of 10-18
words. Wrap 2-5 key names, entities, numbers or outcomes in **double asterisks**.",
 "caption":"Exactly two concise factual sentences for the Instagram caption.
Keep the paragraph around 180-220 characters total. No emoji, numbering,
bullets, headline, or source list.",
 "scene_prompt":"A specific photorealistic 4:5 editorial scene for this story,
hyper-detailed and tailored to the exact subject, vehicles, landmarks, flags,
or technology and market hardware. No text.",
 "badge":"A concise 2-3 word metric or status, or empty string.",
 "photo":17,
 "source_post":17,
 "source_url":"Exact article URL from the provided post links or wire context,
 otherwise an empty string."}}]}}

Rules:
- Return exactly eight stories and preserve their importance order.
- Headlines MUST be 10-18 words, uppercase, one sentence, and emphasize 2-5
key entities/numbers/outcomes by wrapping each emphasized span in **double
asterisks**. Do not use any other markdown.
- Each caption paragraph must contain exactly two concise sentences and be
around 180-220 characters total, understandable without reading the slide.
Facts and numbers must come only from the posts or agency context; never invent.
- Scene prompts are hyper-detailed photorealistic 4:5 composites tailored to
the exact story subject, vehicles, landmarks, flags, or hardware. Badges are
short 2-3 word metric/status labels or empty. Avoid unrelated decoration.
- "photo" is the one-based number of a post whose attached photo visibly depicts
this exact story; otherwise use null. "source_post" is the one-based post most
directly reporting this story, preferably one with an article link. "source_url"
must exactly match an article URL provided in the context. Never choose a logo,
screenshot, meme or unrelated photo.
- Keep reporting factual, concise and neutral. No emojis in any returned field.

POSTS FROM {date} (the available reporting window):
{items}

AGENCY HEADLINES (context only):
{wire}
"""


class DeckGenerationError(RuntimeError):
    def __init__(self, message: str, retryable_http_failure: bool = False):
        super().__init__(message)
        self.retryable_http_failure = retryable_http_failure


def _text_model_attempts() -> tuple[str, ...]:
    models = list(TEXT_MODELS)
    get_candidates = getattr(dn, "candidate_models", None)
    if not callable(get_candidates):
        return tuple(models)
    try:
        candidates = get_candidates()
    except (requests.exceptions.RequestException, RuntimeError, ValueError,
            KeyError, TypeError) as exc:
        print(f"could not load additional Gemini models: {exc}",
              file=sys.stderr)
        return tuple(models)

    added = 0
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        model = candidate.removeprefix("models/").strip()
        if ("flash" not in model or model in models
                or model in DEPRECATED_TEXT_MODELS):
            continue
        models.append(model)
        added += 1
        if added == 2:
            break
    return tuple(models)


def _validate_generated_deck(deck: dict, posts: list[dict], day: dt.date,
                             wire: str) -> dict:
    slides = [slide for slide in deck.get("slides", [])
              if isinstance(slide, dict) and slide.get("headline")
              and slide.get("caption") and slide.get("scene_prompt")]
    slides = slides[:MAX_STORIES]
    if len(slides) != MAX_STORIES:
        raise RuntimeError(
            f"Gemini returned {len(slides)} valid stories; expected {MAX_STORIES}")
    for slide in slides:
        slide["headline"] = re.sub(
            r"\s+", " ", slide["headline"]).strip().upper()
        emphasis = re.findall(r"\*\*(.+?)\*\*", slide["headline"])
        words = re.findall(r"\b[\w'-]+\b", slide["headline"].replace("**", ""))
        if (not 10 <= len(words) <= 18
                or not 2 <= len(emphasis) <= 5
                or slide["headline"].count("**") != len(emphasis) * 2):
            raise RuntimeError(
                "Gemini returned a headline outside the 10-18 word / "
                "2-5 highlighted-span requirements")
        paragraph = re.sub(
            r"^\s*(?:[-*•]|\d+[.)])\s*", "", slide["caption"]).strip()
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        if len(sentences) != 2 or any(not sentence for sentence in sentences):
            raise RuntimeError(
                "Gemini returned a story caption that is not two sentences")
        slide["caption"] = " ".join(sentences)
        badge = re.sub(r"\s+", " ", str(slide.get("badge", ""))).strip()
        slide["badge"] = " ".join(badge.split()[:3]).upper()
    deck["cover_scene_prompt"] = deck.get("cover_scene_prompt", "").strip()
    if not deck["cover_scene_prompt"]:
        raise RuntimeError("Gemini did not return a cover scene prompt")
    allowed_urls = {
        url.rstrip(".,;:]}")
        for url in re.findall(r"https?://[^\s<>()\"']+", wire)
    }
    allowed_urls.update(
        link for post in posts for link in (post.get("links") or [])
        if isinstance(link, str))
    for slide in slides:
        source_url = slide.get("source_url", "")
        slide["source_url"] = (
            source_url.strip()
            if isinstance(source_url, str) and source_url.strip() in allowed_urls
            else "")
    deck["slides"] = slides
    deck["date"] = day.isoformat()
    deck["caption"] = (
        "Here's What Happened in the World in the Last 24 Hours:\n\n"
        + "\n\n".join(slide["caption"] for slide in slides)
    )
    return deck


def write_deck(posts: list[dict], day: dt.date) -> dict:
    items = "\n\n---\n\n".join(
        f"POST {i} [{p['time']}]{' [has photo]' if p.get('images') else ''}\n"
        f"{'ARTICLE LINKS: ' + ', '.join(p.get('links', [])) + chr(10) if p.get('links') else ''}"
        f"{p['text']}"
        for i, p in enumerate(posts, 1))
    wire = dn.fetch_wire(day)
    prompt = SLIDES_PROMPT.format(date=f"{day:%A, %d %B %Y}",
                                  items=items[:300_000],
                                  wire=wire[:30_000])
    # the writer SEES the photos, so its "photo" picks are judged by what
    # each picture shows — the same grounding the video bulletin uses
    photos = dn._photo_parts(posts)
    print(f"slide deck: {len(photos) // 2} photos attached")
    body = {"contents": [{"parts": [{"text": prompt}] + photos}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 16384,
                                 "responseMimeType": "application/json"}}
    headers = {"x-goog-api-key": dn.GEMINI_KEY,
               "Content-Type": "application/json"}
    last_error = "no Gemini models were available"
    backoff_index = 0
    models = _text_model_attempts()
    for model_index, model in enumerate(models):
        try:
            response = requests.post(
                f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
                headers=headers, json=body, timeout=180)
        except requests.exceptions.RequestException as exc:
            print(f"text model {model} failed ({exc.__class__.__name__}): {exc}",
                  file=sys.stderr)
            last_error = f"{model}: {exc}"
            continue

        if not response.ok:
            last_error = (f"{model}: HTTP {response.status_code}: "
                          f"{response.text[:600]}")
            print(f"text model {model} returned {response.status_code}: "
                  f"{response.text[:400]}", file=sys.stderr)
            if (response.status_code == 429
                    or 500 <= response.status_code <= 599):
                if (model_index < len(models) - 1
                        and backoff_index < len(MODEL_BACKOFFS)):
                    wait = MODEL_BACKOFFS[backoff_index]
                    backoff_index += 1
                    print(f"trying next text model in {wait}s", file=sys.stderr)
                    time.sleep(wait)
            continue

        finish_reason = "unknown"
        try:
            payload = response.json()
            candidates = payload.get("candidates") or []
            candidate = candidates[0] if candidates else {}
            finish_reason = candidate.get("finishReason", "unknown")
            if finish_reason == "MAX_TOKENS":
                raise ValueError("generation finished with MAX_TOKENS")
            parts = (candidate.get("content") or {}).get("parts") or []
            text_parts = [
                part["text"] for part in parts
                if isinstance(part.get("text"), str) and part["text"].strip()
            ]
            raw = "\n".join(
                part["text"] for part in parts
                if not part.get("thought")
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            )
            if not raw:
                raw = "\n".join(text_parts)
            if not raw:
                raise ValueError("model returned no non-empty text parts")
            deck = _validate_generated_deck(
                dn._extract_json(raw), posts, day, wire)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError,
                RuntimeError) as exc:
            last_error = f"{model} (finishReason={finish_reason}): {exc}"
            print(f"text model {model} failed (finishReason={finish_reason}): "
                  f"{exc}", file=sys.stderr)
            continue
        return deck

    raise DeckGenerationError(last_error, retryable_http_failure=True)


def _fit_caption(caption: str, limit: int = 2180) -> str:
    """Keep story paragraphs, the fixed header and funnel footer within limit."""
    limit = max(limit, len(CAPTION_HEADER) + len(CAPTION_FOOTER) + 4)
    text = str(caption or "").strip()
    if text.startswith(CAPTION_HEADER):
        body = text[len(CAPTION_HEADER):].strip()
    else:
        body = text
    if body.endswith(CAPTION_FOOTER):
        body = body[:-len(CAPTION_FOOTER)].rstrip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body)
                  if part.strip()]
    fitted = paragraphs[:]

    def assemble() -> str:
        suffix = "\n\n".join(fitted)
        prefix = CAPTION_HEADER + (f"\n\n{suffix}" if suffix else "")
        return prefix + "\n\n" + CAPTION_FOOTER

    for index in sorted(range(len(fitted)),
                        key=lambda item: len(fitted[item]), reverse=True):
        if len(assemble()) <= limit:
            break
        sentences = re.split(r"(?<=[.!?])\s+", fitted[index], maxsplit=1)
        if len(sentences) > 1:
            fitted[index] = sentences[0].strip()

    result = assemble()
    if len(result) <= limit:
        return result

    available = (limit - len(CAPTION_HEADER) - 2
                 - 2 - len(CAPTION_FOOTER))
    if available <= 0:
        return CAPTION_HEADER + "\n\n" + CAPTION_FOOTER
    body = "\n\n".join(fitted)
    truncated = body[:max(0, available - 3)]
    if len(body) > len(truncated):
        truncated = truncated.rsplit(" ", 1)[0].rstrip()
    return (CAPTION_HEADER + "\n\n" + truncated + "...\n\n"
            + CAPTION_FOOTER)


# Anton: the tall condensed display face the poster format is built on —
# committed to the repo (OFL licence) so every runner renders identically
ANTON = os.path.join(video.BUNDLED_FONT_DIR, "Anton-Regular.ttf")


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = ANTON if os.path.exists(ANTON) else video.BOLD
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        fallback = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        return ImageFont.truetype(fallback, size=size)


def _fit_artwork(path: str | None) -> Image.Image:
    if path and os.path.exists(path):
        with Image.open(path) as source:
            source = ImageOps.exif_transpose(source)
            return ImageOps.fit(source.convert("RGB"), (W, H),
                                method=Image.Resampling.LANCZOS)
    return Image.new("RGB", (W, H), "#101116")


def _lower_scrim_overlay() -> Image.Image:
    start = int(H * 0.58)
    height = H - start
    ramp = Image.new("RGBA", (1, height))
    ramp.putdata([
        (0, 0, 0, round(232 * t * t * (3 - 2 * t)))
        for t in (row / max(height - 1, 1) for row in range(height))
    ])
    gradient = ramp.resize((W, height), Image.Resampling.BILINEAR)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay.alpha_composite(gradient, (0, start))
    return overlay


def _apply_lower_scrim(image: Image.Image) -> Image.Image:
    return Image.alpha_composite(
        image.convert("RGBA"), _lower_scrim_overlay()).convert("RGB")


def _draw_shadow_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int],
                      text: str, font: ImageFont.FreeTypeFont,
                      fill: str, anchor: str = "mm",
                      stroke_width: int = 2) -> None:
    x, y = xy
    draw.text((x + 2, y + 4), text, font=font, fill="#000000",
              anchor=anchor, stroke_width=stroke_width + 1,
              stroke_fill="#000000")
    draw.text(xy, text, font=font, fill=fill, anchor=anchor,
              stroke_width=stroke_width, stroke_fill="#000000")


def _draw_brand(draw: ImageDraw.ImageDraw, y: int) -> None:
    font = _font(32)
    center = W // 2
    left, right = "MIDWORLD", "DAILY"
    left_width = draw.textlength(left, font=font)
    right_width = draw.textlength(right, font=font)
    diamond_size = 12
    gap = 14
    half = int((left_width + right_width + diamond_size + gap * 2) / 2) + 30
    draw.line((70, y + 18, center - half, y + 18),
              fill=SIGNAL_GOLD, width=2)
    draw.line((center + half, y + 18, W - 70, y + 18),
              fill=CYAN, width=2)
    left_x = center - (left_width + right_width + diamond_size + gap * 2) / 2
    draw.text((round(left_x), y + 18), left, font=font, fill="#FFFFFF",
              anchor="lm")
    diamond_x = left_x + left_width + gap + diamond_size / 2
    draw.polygon(
        [(diamond_x, y + 18 - diamond_size / 2),
         (diamond_x + diamond_size / 2, y + 18),
         (diamond_x, y + 18 + diamond_size / 2),
         (diamond_x - diamond_size / 2, y + 18)],
        fill=SIGNAL_GOLD,
    )
    right_x = diamond_x + diamond_size / 2 + gap
    draw.text((round(right_x), y + 18), right, font=font, fill=CYAN,
              anchor="lm")


def _fit_font(text: str, max_width: int, max_size: int,
              min_size: int = 24) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for size in range(max_size, min_size - 1, -2):
        font = _font(size)
        if probe.textlength(text, font=font) <= max_width:
            return font
    return _font(min_size)


def _draw_story_badge(image: Image.Image, badge: str) -> None:
    badge = re.sub(r"\s+", " ", badge).strip().upper()
    if not badge:
        return
    font = _font(31)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    width = min(390, int(measure.textlength(badge, font=font)) + 92)
    height = 68
    x, y = W - width - 38, 42
    accent = SIGNAL_GOLD if any(token in badge for token in ("%", "$", "BREAKING")) else CYAN
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=22,
                           fill=(7, 10, 15, 205),
                           outline=(0, 229, 255, 170), width=2)
    draw.ellipse((x + 20, y + 26, x + 36, y + 42), fill=accent)
    draw.text((x + 50, y + height // 2), badge, font=font,
              fill="#FFFFFF", anchor="lm")
    image.alpha_composite(overlay)


def _save_slide(image: Image.Image, out: str) -> None:
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    image.convert("RGB").save(out, format="JPEG", quality=94, optimize=True)


def _wrap_pixels(words: list[tuple[str, bool]], font: ImageFont.FreeTypeFont,
                 max_width: int) -> list[list[tuple[str, bool]]]:
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    current_width = 0.0
    space = draw.textlength(" ", font=font)
    for word, highlighted in words:
        word_width = draw.textlength(word, font=font)
        next_width = current_width + (space if current else 0) + word_width
        if current and next_width > max_width:
            lines.append(current)
            current, current_width = [], 0.0
        current.append((word, highlighted))
        current_width += (space if len(current) > 1 else 0) + word_width
    if current:
        lines.append(current)
    return lines


def _headline_words(headline: str) -> list[tuple[str, bool]]:
    words = []
    for match in re.finditer(r"\*\*(.+?)\*\*|([^*]+)", headline.upper()):
        highlighted = match.group(1) is not None
        content = match.group(1) if highlighted else match.group(2)
        words.extend((word, highlighted) for word in content.split())
    return words


def _draw_story_headline(draw: ImageDraw.ImageDraw, headline: str) -> None:
    words = _headline_words(headline)
    chosen_font = _font(86)
    lines = []
    for size in range(86, 59, -2):
        font = _font(size)
        lines = _wrap_pixels(words, font, W - 112)
        line_height = int(size * 1.08)
        if len(lines) <= 4 and len(lines) * line_height <= 372:
            chosen_font = font
            break
    line_height = int(chosen_font.size * 1.08)
    top = 814 + max(0, (378 - len(lines) * line_height) // 2)
    metrics = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for line_index, line in enumerate(lines):
        widths = [metrics.textlength(word, font=chosen_font)
                  for word, _ in line]
        space = metrics.textlength(" ", font=chosen_font)
        x = (W - sum(widths) - space * max(len(line) - 1, 0)) / 2
        y = top + line_index * line_height
        for (word, highlighted), word_width in zip(line, widths):
            fill = YELLOW if highlighted else "#FFFFFF"
            draw.text((round(x + 2), y + 4), word, font=chosen_font,
                      fill="#000000", stroke_width=3, stroke_fill="#000000")
            draw.text((round(x), y), word, font=chosen_font,
                      fill=fill, stroke_width=2, stroke_fill="#000000")
            x += word_width + space


# the fixed house look every generated illustration is asked for — ONE style
# across every slide, every day, is what makes the feed read as a brand
SCENE_STYLE = (
    ". MidWorld Daily signature Nano Banana 2.1 art direction: a deep "
    "obsidian-navy cinematic backdrop; dramatic photorealistic subjects occupy "
    "the upper 62% of the vertical 4:5 frame, lit with electric-cyan "
    "(#00E5FF) and warm signal-gold (#FFC400) studio rim lights. Add subtle "
    "glowing cyan/gold intelligence-radar arcs only where they fit the story. "
    "Keep the bottom 36% dark and uncluttered for text overlay. This is "
    "illustrative editorial artwork, not documentary evidence. Strictly ZERO "
    "text, words, letters, numbers, labels, logos, watermarks or typography "
    "inside the image.")


# set once per run when the key proves to have no image-generation quota
_GEN_DEAD = False


@functools.lru_cache(maxsize=1)
def image_models() -> tuple[str, ...]:
    return IMAGE_MODELS


_LOCATION_NAMES = {
    "United States", "United Kingdom", "New York", "South Korea",
    "North Korea", "Middle East", "White House", "European Union",
    "United Nations", "World Bank", "Red Sea", "Arab League",
}
_TITLED_PERSON_RE = re.compile(
    r"\b(?:President|Prime Minister|Foreign Minister|Finance Minister|"
    r"Secretary of State|Chancellor|Governor|CEO|Chairman|Secretary)"
    r"\s+[A-Z][a-z]+(?:[-'][A-Za-z]+)?(?:\s+[A-Z][a-z]+(?:[-'][A-Za-z]+)?){0,2}\b")
_PROPER_NAME_RE = re.compile(
    r"\b[A-Z][a-z]+(?:[-'][A-Za-z]+)?"
    r"(?:\s+[A-Z][a-z]+(?:[-'][A-Za-z]+)?){1,2}\b")


def _deidentify_scene_prompt(prompt: str) -> str:
    role = "a senior public official in a tailored dark suit"
    safe = _TITLED_PERSON_RE.sub(role, prompt)
    safe = _PROPER_NAME_RE.sub(
        lambda match: match.group(0) if match.group(0) in _LOCATION_NAMES else role,
        safe)
    if safe == prompt:
        safe += (" Depict any public figures only as unnamed, vivid descriptive "
                 "roles, never as identifiable people.")
    return safe


def _reference_image_part(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1200, 1500), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88, optimize=True)
    return {"inline_data": {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }}


def _scene_request(model: str, prompt: str, reference: dict | None) -> requests.Response:
    text = prompt
    if reference:
        text += (" Use the attached press photo as visual grounding for the "
                 "story's real-world details; create a cinematic editorial "
                 "composition without copying text or adding logos.")
    parts = [{"text": text + SCENE_STYLE}]
    if reference:
        parts.append(reference)
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "4:5"},
        },
    }
    return requests.post(
        f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
        headers={"x-goog-api-key": dn.GEMINI_KEY,
                 "Content-Type": "application/json"},
        json=body,
        timeout=180,
    )


def _save_generated_scene(response: requests.Response, out: str) -> str:
    """Return 'saved', 'blocked', or 'empty' for an image-generation reply."""
    try:
        payload = response.json()
    except ValueError:
        return "empty"
    candidate = (payload.get("candidates") or [{}])[0]
    if candidate.get("finishReason") == "IMAGE_OTHER":
        return "blocked"
    for part in candidate.get("content", {}).get("parts", []):
        blob = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
        if not blob:
            continue
        try:
            data = base64.b64decode(blob, validate=True)
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                ImageOps.fit(
                    image.convert("RGB"), (W, H),
                    method=Image.Resampling.LANCZOS,
                ).save(out, format="PNG")
            return "saved"
        except (OSError, ValueError) as exc:
            print(f"  generated scene was not a valid image: {exc}",
                  file=sys.stderr)
    return "empty"


def gen_scene(scene_prompt: str, out: str,
              reference_photo: str | None = None) -> bool:
    """Generate signature 4:5 artwork, grounded by an optional press photo."""
    global _GEN_DEAD
    if _GEN_DEAD:
        return False
    reference = _reference_image_part(reference_photo)
    safe_prompt = None
    models = image_models()
    for model in models:
        prompt = safe_prompt or scene_prompt
        for prompt_attempt in range(2):
            try:
                response = _scene_request(model, prompt, reference)
            except requests.exceptions.RequestException as exc:
                print(f"  scene gen {model}: {str(exc)[:120]}", file=sys.stderr)
                break
            blocked = response.status_code == 400
            if response.ok:
                result = _save_generated_scene(response, out)
                if result == "saved":
                    return True
                blocked = result == "blocked"
                if not blocked:
                    print(f"  scene gen {model}: no usable image in reply",
                          file=sys.stderr)
                    break
            else:
                print(f"  scene gen {model}: HTTP {response.status_code} "
                      f"{response.text[:220]}", file=sys.stderr)
                if response.status_code == 429 and (
                        "billing" in response.text.lower()
                        or "check your plan" in response.text.lower()):
                    _GEN_DEAD = True
                    return False
            if blocked and safe_prompt is None:
                safe_prompt = _deidentify_scene_prompt(scene_prompt)
                prompt = safe_prompt
                continue
            break
    return False


def probe() -> None:
    """One honest end-to-end test of the scene generator, for a quick run."""
    print("image models:", list(image_models()))
    out = os.path.join(tempfile.mkdtemp(), "probe.png")
    ok = gen_scene("A damaged oil tanker escorted through a narrow strait "
                   "at dawn, warships silhouetted on the horizon", out)
    print("generated:", ok and f"{os.path.getsize(out)} bytes")


def render_story(work: str, idx: int, total: int, slide: dict,
                 date_text: str, bg: str, out: str,
                 is_photo: bool = False) -> None:
    """Compose the story artwork and highlighted headline with Pillow."""
    image = _apply_lower_scrim(_fit_artwork(bg)).convert("RGBA")
    _draw_story_badge(image, str(slide.get("badge", "")))
    draw = ImageDraw.Draw(image)
    _draw_story_headline(draw, slide["headline"])
    _draw_brand(draw, 1255)
    _save_slide(image, out)


def render_cover(work: str, cover_line: str, date_text: str, total: int,
                 out: str, bg: str | None = None,
                 background_out: str | None = None,
                 overlay_out: str | None = None) -> None:
    background = _fit_artwork(bg or os.path.join(TEMPLATES, "cover.png"))
    overlay = _lower_scrim_overlay()
    draw = ImageDraw.Draw(overlay)
    title_lines = (
        ("WHAT MOVED", "#FFFFFF", 94),
        ("THE WORLD", SIGNAL_GOLD, 98),
        ("IN THE LAST 24 HOURS", SIGNAL_GOLD, 66),
    )
    for (text, color, size), y in zip(title_lines, (846, 944, 1040)):
        _draw_shadow_text(draw, (W // 2, y), text,
                          _fit_font(text, W - 120, size), color)
    _draw_brand(draw, 1114)
    x0, y0, x1, y1 = 288, 1206, 792, 1294
    cut = 22
    draw.polygon(
        [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
         (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
         (x0, y1 - cut), (x0, y0 + cut)],
        fill=SIGNAL_GOLD,
    )
    _draw_shadow_text(draw, (W // 2, (y0 + y1) // 2), "SWIPE LEFT >>",
                      _font(43, bold=True), "#000000", stroke_width=0)
    composite = Image.alpha_composite(background.convert("RGBA"), overlay)
    _save_slide(composite, out)
    if background_out:
        background.save(background_out, format="PNG")
    if overlay_out:
        overlay.save(overlay_out, format="PNG")


def _render_cover_video(background_path: str, overlay_path: str,
                        output_path: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to render the animated cover")
    duration, fps = 7, 30
    music = os.path.join(BASE_DIR, "assets", "ig_music.mp3")
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", background_path,
        "-loop", "1", "-framerate", str(fps), "-i", overlay_path,
    ]
    if os.path.isfile(music):
        command.extend(["-stream_loop", "-1", "-i", music])
        audio_filter = (
            "[2:a]atrim=duration=7,afade=t=out:st=6.2:d=0.8,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
        )
    else:
        command.extend([
            "-f", "lavfi", "-i",
            "sine=frequency=48:sample_rate=48000:duration=7",
            "-f", "lavfi", "-i",
            "sine=frequency=220:sample_rate=48000:duration=7",
        ])
        audio_filter = (
            "[2:a]lowpass=f=85,volume=0.32[bass];"
            "[3:a]volume='0.18*(0.5+0.5*sin(2*PI*0.55*t))':eval=frame[pulse];"
            "[bass][pulse]amix=inputs=2:duration=longest:normalize=0,"
            "afade=t=out:st=6.2:d=0.8,loudnorm=I=-16:TP=-1.5:LRA=11,"
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
        )
    video_filter = (
        "[0:v]scale=2160:2700:flags=lanczos,"
        "zoompan=z='1+0.04*on/209':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        "d=210:s=1080x1350:fps=30,setsar=1[bg];"
        "[bg][1:v]overlay=0:0:shortest=1,format=yuv420p[v]"
    )
    command.extend([
        "-filter_complex", video_filter + ";" + audio_filter,
        "-map", "[v]", "-map", "[a]", "-t", str(duration),
        "-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", output_path,
    ])
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg failed to render animated cover: {exc.stderr[-1200:]}"
        ) from exc


def _rotated_card(image: Image.Image, angle: float) -> Image.Image:
    card = Image.new("RGB", (350, 440), OBSIDIAN)
    card.paste(ImageOps.fit(image.convert("RGB"), (334, 424),
                            method=Image.Resampling.LANCZOS), (8, 8))
    ImageDraw.Draw(card).rectangle((2, 2, 347, 437), outline=CYAN, width=3)
    return card.rotate(angle, resample=Image.Resampling.BICUBIC,
                       expand=True, fillcolor=OBSIDIAN)


def render_outro(work: str, date_text: str, out: str) -> None:
    image = Image.new("RGB", (W, H), OBSIDIAN)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-260, 20, 520, 800), fill=(0, 229, 255, 24))
    glow_draw.ellipse((660, 100, 1330, 850), fill=(255, 196, 0, 15))
    image = Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")
    cover_path = os.path.join(OUT_DIR, "01.jpg")
    cover = _fit_artwork(cover_path if os.path.exists(cover_path) else None)
    left = _rotated_card(cover, 8)

    promo = Image.new("RGB", (350, 440), "#0B1520")
    promo_draw = ImageDraw.Draw(promo)
    promo_draw.rectangle((2, 2, 347, 437), outline=CYAN, width=3)
    promo_draw.text((175, 82), "MIDWORLD", font=_font(46),
                    fill="#FFFFFF", anchor="mm")
    promo_draw.text((175, 136), "LIVE", font=_font(40),
                    fill=CYAN, anchor="mm")
    promo_draw.line((38, 182, 312, 182), fill=SIGNAL_GOLD, width=3)
    for y, line in zip((240, 296, 352),
                       ("BREAKING WIRE", "MARKET ALERTS", "24 / 7")):
        promo_draw.text((175, y), line, font=_fit_font(line, 290, 34),
                        fill=SIGNAL_GOLD if y == 352 else "#FFFFFF",
                        anchor="mm")
    right = promo.rotate(-8, resample=Image.Resampling.BICUBIC,
                         expand=True, fillcolor=OBSIDIAN)
    image.paste(left, (75, 120))
    image.paste(right, (655, 120))
    draw = ImageDraw.Draw(image)

    arrow = []
    for step in range(31):
        t = step / 30
        arrow.append((round(440 + 180 * t),
                      round(282 - 62 * math.sin(math.pi * t))))
    draw.line(arrow, fill=CYAN, width=7, joint="curve")
    end_x, end_y = arrow[-1]
    draw.polygon([(end_x, end_y), (end_x - 20, end_y - 16),
                  (end_x - 16, end_y + 13)], fill=CYAN)

    draw.text((W // 2, 670), "GET BREAKING NEWS",
              font=_fit_font("GET BREAKING NEWS", W - 100, 84),
              fill="#FFFFFF", anchor="mm",
              stroke_width=2, stroke_fill="#000000")
    draw.text((W // 2, 766), "10 MIN FIRST",
              font=_fit_font("10 MIN FIRST", W - 100, 100),
              fill=SIGNAL_GOLD, anchor="mm",
              stroke_width=2, stroke_fill="#000000")
    subline = "24/7 GLOBAL WIRE & MARKET ALERTS ON TELEGRAM"
    draw.text((W // 2, 868), subline,
              font=_fit_font(subline, W - 100, 37),
              fill="#DDE7EE", anchor="mm")

    x0, y0, x1, y1 = 128, 950, 952, 1040
    cut = 22
    draw.polygon(
        [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
         (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
         (x0, y1 - cut), (x0, y0 + cut)],
        fill=SIGNAL_GOLD,
    )
    button_text = "LINK IN BIO \u2022 @MIDWORLDNEWS"
    draw.text((W // 2, (y0 + y1) // 2), button_text,
              font=_fit_font(button_text, x1 - x0 - 50, 42),
              fill="#000000", anchor="mm")
    follow_text = f"Follow {IG_HANDLE} for daily briefings"
    draw.text((W // 2, 1140), follow_text,
              font=_fit_font(follow_text, W - 100, 37),
              fill="#FFFFFF", anchor="mm")
    draw.line((135, 1253, W - 135, 1253), fill="#24424E", width=2)
    draw.text((W // 2, 1294), "MIDWORLD DAILY  \u2022  GLOBAL, EVERY DAY",
              font=_font(23), fill=CYAN, anchor="mm")
    _save_slide(image, out)


def target_day() -> dt.date:
    env = os.getenv("TARGET_DATE", "").strip()
    if env:
        return dt.date.fromisoformat(env)
    # just after local midnight this is the day that just ended; by afternoon
    # it is today-so-far — either way, the most recent full stretch of news
    return (dt.datetime.now(dn.TZ) - dt.timedelta(hours=12)).date()


def _posts_last_24h(end: dt.datetime) -> list[dict]:
    start = end - dt.timedelta(hours=24)
    last_day = (end - dt.timedelta(microseconds=1)).date()
    posts_with_times = []
    current_day = start.date()
    while current_day <= last_day:
        for post in dn.fetch_posts(dn.SOURCE, current_day):
            try:
                post_time = dt.datetime.strptime(post["time"], "%H:%M").time()
            except (KeyError, ValueError) as exc:
                print(f"  skipping post with invalid time: {exc}", file=sys.stderr)
                continue
            created = dt.datetime.combine(current_day, post_time).replace(
                tzinfo=dn.TZ)
            if start <= created < end:
                posts_with_times.append((created, post))
        current_day += dt.timedelta(days=1)
    posts_with_times.sort(key=lambda item: item[0])
    return [post for _, post in posts_with_times]


def _download_story_photo(slide: dict, posts: list[dict], work: str,
                          index: int) -> tuple[str, int, str] | None:
    """Prefer a high-resolution source image, retaining valid Telegram photos."""
    photo_pick = slide.get("photo")
    if not isinstance(photo_pick, int) or not 1 <= photo_pick <= len(posts):
        photo_pick = None
    source_pick = slide.get("source_post")
    if not isinstance(source_pick, int) or not 1 <= source_pick <= len(posts):
        source_pick = photo_pick
    source_url = slide.get("source_url")
    if source_pick is None and not (
            isinstance(source_url, str)
            and source_url.startswith(("http://", "https://"))):
        return None
    source_post = posts[source_pick - 1] if source_pick is not None else {}
    fallback = None
    image_post = (posts[photo_pick - 1] if photo_pick is not None
                  else source_post)
    urls = image_post.get("images") or []
    for candidate, url in enumerate(urls):
        raw = os.path.join(work, f"photo{index}-{candidate}.img")
        try:
            if dn._download(url, raw, dn.UA, min_size=(600, 340)):
                with Image.open(raw) as image:
                    width, height = image.size
                fallback = (raw, photo_pick or source_pick,
                            f"post {photo_pick or source_pick} photo")
                if width >= 1000 and height >= 340:
                    return fallback
        except (requests.exceptions.RequestException, OSError, ValueError) as e:
            print(f"    story photo unavailable ({str(e)[:80]}); "
                  "trying next candidate", file=sys.stderr)

    article_urls = []
    if isinstance(source_url, str) and source_url:
        article_urls.append(source_url)
    for key in ("article_url", "source_url", "url"):
        value = source_post.get(key)
        if isinstance(value, str):
            article_urls.append(value)
    links = source_post.get("links") or []
    if isinstance(links, str):
        links = [links]
    for link in links:
        if isinstance(link, str):
            article_urls.append(link)
        elif isinstance(link, dict):
            article_urls.extend(value for key, value in link.items()
                                if key in ("url", "href")
                                and isinstance(value, str))
    article_urls.extend(
        re.findall(r"https?://[^\s<>()\"']+", source_post.get("text", "")))

    for article_index, article_url in enumerate(
            list(dict.fromkeys(article_urls))[:2]):
        if not article_url.startswith(("http://", "https://")):
            continue
        try:
            response = requests.get(article_url, headers=dn.UA, timeout=8)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            image_urls = []
            for selector in (
                    'meta[property="og:image"]',
                    'meta[name="twitter:image"]',
                    'meta[name="twitter:image:src"]'):
                tag = soup.select_one(selector)
                if tag and tag.get("content"):
                    image_urls.append(urljoin(response.url, tag["content"].strip()))
            for image_index, image_url in enumerate(
                    list(dict.fromkeys(image_urls))[:2]):
                image_response = requests.get(
                    image_url, headers=dn.UA, timeout=8)
                image_response.raise_for_status()
                if (not image_response.headers.get("content-type", "")
                        .startswith("image/")
                        or len(image_response.content) < 8000):
                    continue
                with Image.open(io.BytesIO(image_response.content)) as image:
                    image.load()
                    width, height = image.size
                if width < 600 or height < 340:
                    continue
                raw = os.path.join(
                    work, f"article{index}-{article_index}-{image_index}.img")
                with open(raw, "wb") as image_file:
                    image_file.write(image_response.content)
                article_photo = (raw, source_pick or 0, "article photo")
                if width >= 1000:
                    return article_photo
                if fallback is None:
                    fallback = article_photo
        except (requests.exceptions.RequestException, OSError, ValueError) as e:
            print(f"    article photo unavailable ({str(e)[:80]})",
                  file=sys.stderr)
    return fallback


def _grade_press_photo(source_path: str, output_path: str) -> str:
    image = _fit_artwork(source_path)
    image = ImageEnhance.Contrast(image).enhance(1.14)
    image = ImageEnhance.Color(image).enhance(0.82)
    image = Image.blend(image, Image.new("RGB", (W, H), OBSIDIAN), 0.18)
    ramp = Image.new("RGBA", (W, 1))
    ramp.putdata([
        (0, 229, 255, round(40 * max(0.0, 1.0 - x / (W * 0.72))))
        for x in range(W)
    ])
    tint = ramp.resize((W, H), Image.Resampling.BILINEAR)
    image = Image.alpha_composite(image.convert("RGBA"), tint).convert("RGB")
    image.save(output_path, format="PNG")
    return output_path


def _load_cached_deck(deck_path: str, day: dt.date) -> dict:
    try:
        with open(deck_path, encoding="utf-8") as deck_file:
            deck = json.load(deck_file)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"could not load cached deck {deck_path}: {exc}") from exc
    if not isinstance(deck, dict) or deck.get("date") != day.isoformat():
        raise RuntimeError(f"cached deck {deck_path} is not for {day.isoformat()}")
    slides = deck.get("slides")
    if not isinstance(slides, list) or len(slides) != MAX_STORIES:
        raise RuntimeError(
            f"cached deck {deck_path} must contain exactly {MAX_STORIES} stories")
    for slide in slides:
        if not isinstance(slide, dict) or not slide.get("headline"):
            raise RuntimeError(f"cached deck {deck_path} has an invalid story")
        headline = re.sub(r"\*{2}", "", str(slide["headline"])).strip()
        caption = str(slide.get("caption", "")).strip()
        scene_prompt = slide.get("scene_prompt")
        if not isinstance(scene_prompt, str) or not scene_prompt.strip():
            slide["scene_prompt"] = (
                f"Photorealistic 4:5 editorial scene about {headline}. "
                f"Story context: {caption}. No text or logos.")
        else:
            slide["scene_prompt"] = scene_prompt.strip()
    if not isinstance(deck.get("cover_scene_prompt"), str) or not (
            deck["cover_scene_prompt"].strip()):
        top_stories = "; ".join(
            f"{str(slide['headline']).replace('**', '').strip()}: "
            f"{str(slide.get('caption', '')).strip()}"
            for slide in slides[:3])
        deck["cover_scene_prompt"] = (
            "Dramatic photorealistic 4:5 editorial cover illustrating these "
            f"top world and market stories: {top_stories}. No text or logos.")
    return deck


def render() -> None:
    target = os.getenv("TARGET_DATE", "").strip()
    if target:
        day = dt.date.fromisoformat(target)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).replace(
            tzinfo=dn.TZ)
    else:
        end = dt.datetime.now(dn.TZ)
        day = end.date()
    deck_path = os.path.join(OUT_DIR, "deck.json")
    if not os.getenv("FORCE_SLIDES") and os.path.exists(deck_path):
        try:
            with open(deck_path, encoding="utf-8") as deck_file:
                cached_date = json.load(deck_file).get("date")
            expected_files = [
                "caption.txt", "01.mp4",
                *(f"{index:02d}.jpg" for index in range(1, MAX_SLIDES + 1)),
            ]
            if (cached_date == day.isoformat()
                    and all(os.path.isfile(os.path.join(OUT_DIR, filename))
                            for filename in expected_files)):
                print(f"slides for {day} already rendered — nothing to do")
                return
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            print(f"  ignoring unreadable cached deck: {exc}", file=sys.stderr)
    posts = _posts_last_24h(end)
    if len(posts) < 2:
        print("not enough posts for a carousel")
        return
    try:
        deck = write_deck(posts, day)
    except DeckGenerationError as exc:
        if not exc.retryable_http_failure or not os.path.exists(deck_path):
            raise
        print(f"Gemini text generation failed; reusing cached deck: {exc}",
              file=sys.stderr)
        deck = _load_cached_deck(deck_path, day)
    deck["caption"] = _fit_caption(deck.get("caption", ""))
    total = len(deck["slides"]) + 2
    if total > MAX_SLIDES:
        raise RuntimeError(f"carousel has {total} items; maximum is {MAX_SLIDES}")
    date_text = f"{day:%d %B %Y}"
    os.makedirs(OUT_DIR, exist_ok=True)
    for old in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, old))
    work = tempfile.mkdtemp(prefix="ig-")
    cover_bg = None
    gen = os.path.join(work, "cover_gen.png")
    if gen_scene(deck["cover_scene_prompt"], gen):
        cover_bg = gen
    cover_background = os.path.join(work, "cover_background.png")
    cover_overlay = os.path.join(work, "cover_overlay.png")
    render_cover(work, deck.get("cover", "The day around the globe"),
                 date_text, total, os.path.join(OUT_DIR, "01.jpg"),
                 bg=cover_bg, background_out=cover_background,
                 overlay_out=cover_overlay)
    _render_cover_video(
        cover_background, cover_overlay, os.path.join(OUT_DIR, "01.mp4"))
    print(f"  slide 1: cover ({'generated' if cover_bg else 'backdrop'})")
    for i, slide in enumerate(deck["slides"]):
        bg, label, graded = art_for(slide.get("region", "World")), "backdrop", False
        photo = _download_story_photo(slide, posts, work, i)
        reference_photo = photo[0] if photo else None
        if photo:
            bg, _, label = photo
        generated = False
        if slide.get("scene_prompt"):
            gen = os.path.join(work, f"gen{i}.png")
            if gen_scene(slide["scene_prompt"], gen,
                         reference_photo=reference_photo):
                bg, label, graded = gen, "generated scene", True
                generated = True
        if photo and not generated:
            bg = _grade_press_photo(
                photo[0], os.path.join(work, f"graded{i}.png"))
            label, graded = "cyan-graded press photo", True
        render_story(work, i + 1, total, slide, date_text, bg,
                     os.path.join(OUT_DIR, f"{i + 2:02d}.jpg"),
                     is_photo=graded)
        print(f"  slide {i + 2}: {slide['headline']} ({label})")
    render_outro(work, date_text, os.path.join(OUT_DIR, f"{total:02d}.jpg"))
    caption_path = os.path.join(OUT_DIR, "caption.txt")
    with open(caption_path, "w", encoding="utf-8") as f:
        f.write(deck["caption"])
    with open(deck_path, "w", encoding="utf-8") as f:
        json.dump(deck, f, ensure_ascii=False, indent=1)
    shutil.rmtree(work)
    print(f"rendered {total} slides for {day}")


GRAPH_FACEBOOK = "https://graph.facebook.com/v21.0"
GRAPH_INSTAGRAM = "https://graph.instagram.com/v21.0"


def graph_base(token: str) -> str:
    return GRAPH_INSTAGRAM if token.strip().startswith("IGAA") else GRAPH_FACEBOOK


def _meta_response_json(response: requests.Response, operation: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw_response": response.text}
    if not response.ok:
        print(
            f"Meta API {operation} returned HTTP {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=False)}",
            file=sys.stderr,
            flush=True,
        )
    return payload


def _wait_for_container(container_id: str, token: str, graph: str,
                        attempts: int = 12, sleep_seconds: int = 3) -> None:
    url = f"{graph}/{container_id}"
    params = {"fields": "status_code,status", "access_token": token}
    for attempt in range(attempts):
        response = requests.get(url, params=params, timeout=30)
        payload = _meta_response_json(response, "container status")
        error = payload.get("error")
        if (response.status_code == 400 and isinstance(error, dict)
                and str(error.get("code")) == "100"
                and str(error.get("error_subcode", error.get("subcode"))) == "33"):
            time.sleep(sleep_seconds)
            return
        response.raise_for_status()
        status = payload.get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                f"Instagram container {container_id} reached {status}: "
                f"{json.dumps(payload, ensure_ascii=False)}")
        if attempt < attempts - 1:
            time.sleep(sleep_seconds)
    raise RuntimeError(
        f"Instagram container {container_id} did not finish after {attempts} polls: "
        f"{json.dumps(payload, ensure_ascii=False)}")


def check_auth() -> None:
    token = os.getenv("IG_ACCESS_TOKEN", "").strip()
    user_id = os.getenv("IG_USER_ID", "").strip()
    if not token or not user_id:
        raise RuntimeError("IG_ACCESS_TOKEN and IG_USER_ID must be set")
    graph = graph_base(token)
    response = requests.get(
        f"{graph}/{user_id}",
        params={"fields": "id,username", "access_token": token},
        timeout=15,
    )
    payload = _meta_response_json(response, "auth check")
    response.raise_for_status()
    print(f"Connected Instagram account: @{payload['username']}")


def publish() -> None:
    uid = os.getenv("IG_USER_ID", "").strip()
    tok = os.getenv("IG_ACCESS_TOKEN", "").strip()
    deck_path = os.path.join(OUT_DIR, "deck.json")
    if not os.path.exists(deck_path):
        print("no deck rendered — nothing to publish")
        return
    deck = json.load(open(deck_path))
    day = deck.get("date", "")
    if not uid or not tok:
        print("IG_USER_ID / IG_ACCESS_TOKEN not set — slides rendered and "
              "committed, not posted.\nTo enable auto-posting: make the "
              "Instagram account a Business account, link it to a Facebook "
              "Page, create a long-lived Graph API token with "
              "instagram_basic + instagram_content_publish, and add both "
              "as repo secrets.")
        return
    try:
        force_publish = os.getenv("FORCE_SLIDES", "").strip().lower() not in (
            "", "0", "false")
        if not force_publish and open(IG_MARKER).read().strip() == day:
            print(f"{day} already posted to Instagram — skipping")
            return
    except FileNotFoundError:
        pass

    caption = _fit_caption(deck.get("caption", ""))
    if not caption.startswith(
            "Here's What Happened in the World in the Last 24 Hours:"):
        raise RuntimeError("deck is missing its formatted 24-hour caption")
    if len(caption) > 2200:
        raise RuntimeError(
            "carousel caption exceeds Instagram's 2,200-character limit")
    repo = os.getenv("GITHUB_REPOSITORY", "itssergekalajian-hub/newsbotdaily")
    raw = f"https://raw.githubusercontent.com/{repo}/main/{OUT_DIR}"
    graph = graph_base(tok)
    files = sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".jpg"))
    if len(files) != MAX_SLIDES:
        raise RuntimeError(
            f"carousel has {len(files)} images; expected exactly {MAX_SLIDES}")
    children = []
    video_uploaded = False
    cover_video = os.path.join(OUT_DIR, "01.mp4")
    if os.path.isfile(cover_video) and os.path.getsize(cover_video):
        try:
            response = requests.post(
                f"{graph}/{uid}/media",
                data={"media_type": "VIDEO",
                      "video_url": f"{raw}/01.mp4",
                      "is_carousel_item": "true",
                      "access_token": tok},
                timeout=60,
            )
            payload = _meta_response_json(response, "create animated cover")
            response.raise_for_status()
            video_container = payload["id"]
            _wait_for_container(video_container, tok, graph,
                                attempts=25, sleep_seconds=4)
            children.append(video_container)
            video_uploaded = True
        except (requests.exceptions.RequestException, RuntimeError, KeyError) as exc:
            print(f"::warning::Animated cover rejected; falling back to 01.jpg: "
                  f"{exc}", flush=True)

    image_files = files[1:] if video_uploaded else files
    for f in image_files:
        response = requests.post(f"{graph}/{uid}/media",
                                 data={"image_url": f"{raw}/{f}",
                                       "is_carousel_item": "true",
                                       "access_token": tok}, timeout=60)
        payload = _meta_response_json(response, f"create child {f}")
        response.raise_for_status()
        children.append(payload["id"])
        print(f"  container for {f}: {children[-1]}")

    containers_to_wait = children[1:] if video_uploaded else children
    for child in containers_to_wait:
        _wait_for_container(child, tok, graph)

    response = requests.post(f"{graph}/{uid}/media",
                             data={"media_type": "CAROUSEL",
                                   "children": ",".join(children),
                                   "caption": caption,
                                   "is_ai_generated": "true",
                                   "access_token": tok}, timeout=60)
    payload = _meta_response_json(response, "create carousel")
    response.raise_for_status()
    carousel = payload["id"]
    _wait_for_container(carousel, tok, graph)

    for attempt in range(10):
        response = requests.post(f"{graph}/{uid}/media_publish",
                                 data={"creation_id": carousel,
                                       "access_token": tok}, timeout=60)
        payload = _meta_response_json(response, "publish carousel")
        if payload.get("config_issue"):
            print(f"::warning::Instagram media_publish config_issue: "
                  f"{json.dumps(payload['config_issue'], ensure_ascii=False)}",
                  flush=True)
        if response.ok:
            print(f"published to Instagram: media {payload.get('id')}")
            with open(IG_MARKER, "w") as f:
                f.write(day)
            return
        time.sleep(15)
    raise RuntimeError(
        f"carousel publish failed after 10 attempts: "
        f"{json.dumps(payload, ensure_ascii=False)}")


if __name__ == "__main__":
    {"render": render, "publish": publish, "probe": probe,
     "check-auth": check_auth}[sys.argv[1]]()
