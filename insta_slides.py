"""Render and publish a branded 4:5 MidWorld Daily Instagram carousel.

Two entry points, run as separate workflow steps so the slides are committed
(and publicly addressable) before Instagram is asked to fetch them:

    python insta_slides.py render    — write slides/latest/*.jpg + deck.json
    python insta_slides.py publish   — post the carousel via the IG Graph API

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
import sys
import tempfile
import time

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

import daily_news as dn
import video

W, H = 1080, 1350                 # Instagram portrait
OUT_DIR = "slides/latest"
TEMPLATES = "templates/ig"
IG_MARKER = ".last_ig"            # last day actually POSTED to Instagram
MAX_STORIES = 8                   # Instagram Graph API caps a carousel at 10 items
MAX_SLIDES = MAX_STORIES + 2      # cover + stories + closing follow slide
BRAND = "MIDWORLD DAILY"
IG_HANDLE = os.getenv("IG_HANDLE", "@midworlddaily").strip() or "@midworlddaily"
if not IG_HANDLE.startswith("@"):
    IG_HANDLE = f"@{IG_HANDLE}"
ANTON = os.path.join(video.BUNDLED_FONT_DIR, "Anton-Regular.ttf")
YELLOW = "#FFD400"

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


# The context-scene library: Higgsfield-generated editorial illustrations in
# the fixed house look, one per news archetype, committed under
# templates/scenes/. The writer picks the one that matches each story, so
# every slide carries imagery that REPRESENTS its story even with no image
# model available at render time.
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
{{"cover_scene_prompt":"One dramatic photorealistic 4:5 editorial composition
combining the day's top three visual elements, leaders or events. No text.",
 "slides":[{{"region":"World, Markets, or another concise category",
 "headline":"A punchy, uppercase, one-sentence statement of 10-18
words. Wrap 2-5 key names, entities, numbers or outcomes in **double asterisks**.",
 "caption":"Exactly two concise factual sentences for the Instagram caption.
No emoji, numbering, bullets, headline, or source list.",
 "scene_prompt":"A specific photorealistic 4:5 editorial scene for this story,
describing the setting, visible details and mood. No text.",
 "photo":17}}]}}

Rules:
- Return exactly eight stories and preserve their importance order.
- Headlines MUST be 10-18 words, uppercase, one sentence, and emphasize 2-5
key entities/numbers/outcomes by wrapping each emphasized span in **double
asterisks**. Do not use any other markdown.
- Each caption paragraph must contain exactly two sentences and be understandable
without reading the slide. Facts and numbers must come only from the posts or
agency context; never invent.
- Scene prompts are photorealistic 4:5 editorial illustrations with no text,
letters, numbers, logos or watermarks. Avoid implying that an illustrative
image is real documentary footage; do not depict identifiable private people.
- "photo" is the one-based number of a post whose attached photo visibly depicts
this exact story; otherwise use null. Never choose a logo, screenshot, meme or
unrelated photo.
- Keep reporting factual, concise and neutral. No emojis in any returned field.

POSTS FROM {date} (the available reporting window):
{items}

AGENCY HEADLINES (context only):
{wire}
"""


def write_deck(posts: list[dict], day: dt.date) -> dict:
    items = "\n\n---\n\n".join(
        f"POST {i} [{p['time']}]{' [has photo]' if p.get('images') else ''}\n"
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
    model = dn.candidate_models()[0]
    alternates = [m for m in dn.candidate_models() if m != model]
    tried = 0
    body = {"contents": [{"parts": [{"text": prompt}] + photos}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096,
                                 "responseMimeType": "application/json",
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    headers = {"x-goog-api-key": dn.GEMINI_KEY,
               "Content-Type": "application/json"}
    for attempt in range(14):
        try:
            r = requests.post(
                f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
                headers=headers, json=body, timeout=180)
        except requests.exceptions.RequestException as exc:
            wait = min(15 * 2 ** (attempt // 2), 240)
            print(f"connection dropped ({exc.__class__.__name__}) on {model} "
                  f"— waiting {wait}s")
            if alternates and tried < len(alternates) and attempt % 2 == 1:
                model = alternates[tried]
                tried += 1
                print(f"switching to alternate model: {model}")
            time.sleep(wait)
            continue
        if r.ok:
            break
        if r.status_code == 404 and tried < len(alternates):
            model = alternates[tried]; tried += 1
            continue
        if r.status_code == 400 and "thinkingConfig" in str(
                body.get("generationConfig", {})):
            body["generationConfig"].pop("thinkingConfig", None)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            if alternates and tried < len(alternates) and attempt % 2 == 1:
                model = alternates[tried]; tried += 1
                print(f"{r.status_code} — switching to {model}")
                continue
            wait = min(15 * 2 ** (attempt // 2), 240)
            print(f"{r.status_code} — waiting {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
    else:
        raise RuntimeError("gemini unavailable for the slide deck")

    raw = "\n".join(p["text"] for p in
                    r.json()["candidates"][0]["content"]["parts"]
                    if "text" in p)
    deck = dn._extract_json(raw)
    slides = [s for s in deck.get("slides", [])
              if isinstance(s, dict) and s.get("headline")
              and s.get("caption") and s.get("scene_prompt")]
    slides = slides[:MAX_STORIES]
    if len(slides) != MAX_STORIES:
        raise RuntimeError(
            f"Gemini returned {len(slides)} valid stories; expected {MAX_STORIES}")
    for slide in slides:
        slide["headline"] = re.sub(r"\s+", " ", slide["headline"]).strip().upper()
        emphasis = re.findall(r"\*\*(.+?)\*\*", slide["headline"])
        words = re.findall(r"\b[\w'-]+\b", slide["headline"].replace("**", ""))
        if (not 10 <= len(words) <= 18
                or not 2 <= len(emphasis) <= 5
                or slide["headline"].count("**") != len(emphasis) * 2):
            raise RuntimeError(
                "Gemini returned a headline outside the 10-18 word / "
                "2-5 highlighted-span requirements")
        paragraph = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", slide["caption"]).strip()
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        if len(sentences) != 2 or any(not sentence for sentence in sentences):
            raise RuntimeError("Gemini returned a story caption that is not two sentences")
        slide["caption"] = " ".join(sentences)
    deck["cover_scene_prompt"] = deck.get("cover_scene_prompt", "").strip()
    if not deck["cover_scene_prompt"]:
        raise RuntimeError("Gemini did not return a cover scene prompt")
    deck["slides"] = slides
    deck["date"] = day.isoformat()
    deck["caption"] = (
        "Here's What Happened in the World in the Last 24 Hours:\n\n"
        + "\n\n".join(slide["caption"] for slide in slides)
    )
    return deck


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


def _apply_lower_scrim(image: Image.Image) -> Image.Image:
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
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


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


def _draw_brand(draw: ImageDraw.ImageDraw, y: int, color: str,
                line_color: str) -> None:
    font = _font(32)
    center = W // 2
    left, right = "MIDWORLD", "DAILY"
    left_width = draw.textlength(left, font=font)
    right_width = draw.textlength(right, font=font)
    diamond_size = 12
    gap = 14
    half = int((left_width + right_width + diamond_size + gap * 2) / 2) + 30
    draw.line((70, y + 18, center - half, y + 18), fill=line_color, width=2)
    draw.line((center + half, y + 18, W - 70, y + 18), fill=line_color, width=2)
    left_x = center - (left_width + right_width + diamond_size + gap * 2) / 2
    draw.text((round(left_x), y + 18), left, font=font, fill=color, anchor="lm")
    diamond_x = left_x + left_width + gap + diamond_size / 2
    draw.polygon(
        [(diamond_x, y + 18 - diamond_size / 2),
         (diamond_x + diamond_size / 2, y + 18),
         (diamond_x, y + 18 + diamond_size / 2),
         (diamond_x - diamond_size / 2, y + 18)],
        fill=color,
    )
    right_x = diamond_x + diamond_size / 2 + gap
    draw.text((round(right_x), y + 18), right, font=font, fill=color, anchor="lm")


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
    ". Create a photorealistic editorial news illustration in a vertical 4:5 "
    "composition, cinematic natural light and realistic detail. Compose for "
    "full-bleed cropping with a clear, uncluttered upper area and a darker "
    "lower area for later graphic overlay. This is illustrative artwork, not "
    "documentary evidence. Do not render text, letters, numbers, labels, "
    "logos, watermarks or typography; do not depict identifiable real people.")


# set once per run when the key proves to have no image-generation quota
_GEN_DEAD = False


@functools.lru_cache(maxsize=1)
def image_models() -> tuple[str, ...]:
    preferred = ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]
    try:
        r = requests.get(f"{dn.GEMINI_ROOT}/models",
                         headers={"x-goog-api-key": dn.GEMINI_KEY}, timeout=30)
        r.raise_for_status()
        names = [m["name"].removeprefix("models/")
                 for m in r.json().get("models", [])
                 if "image" in m["name"]
                 and "generateContent" in m.get("supportedGenerationMethods", [])]
    except requests.exceptions.RequestException as e:
        print(f"  image model discovery unavailable ({str(e)[:60]}); "
              "trying preferred image models", file=sys.stderr)
        names = []

    def rank(n: str) -> tuple:
        if n in preferred:
            return (preferred.index(n),)
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (len(preferred) + ("preview" in n or "exp" in n),
                -(float(m.group(1)) if m else 0.0), len(n))

    return tuple(sorted(set(preferred + names), key=rank))


def gen_scene(scene_prompt: str, out: str) -> bool:
    """Generate text-free 4:5 artwork from a story-specific scene prompt.

    Tries preferred Gemini image models with bounded retries. Failure returns
    False so verified story photos or the regional template can take over.
    """
    global _GEN_DEAD
    if _GEN_DEAD:
        return False
    headers = {"x-goog-api-key": dn.GEMINI_KEY,
               "Content-Type": "application/json"}
    models = image_models()
    for model in models[:4]:
        # image models answer with a text part AND an image part; asking for
        # IMAGE alone is an INVALID_ARGUMENT on the gemini image family
        body = {"contents": [{"parts": [{"text": scene_prompt + SCENE_STYLE}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": "4:5"}}}
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
                    headers=headers, json=body, timeout=180)
            except requests.exceptions.RequestException as e:
                print(f"  scene gen {model}: {str(e)[:80]}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            if r.ok:
                parts = (r.json().get("candidates") or [{}])[0] \
                    .get("content", {}).get("parts", [])
                for p in parts:
                    blob = (p.get("inlineData") or p.get("inline_data")
                            or {}).get("data")
                    if blob:
                        data = base64.b64decode(blob)
                        if len(data) > 20_000:
                            try:
                                with Image.open(io.BytesIO(data)) as image:
                                    image.load()
                                    ImageOps.fit(
                                        image.convert("RGB"), (W, H),
                                        method=Image.Resampling.LANCZOS,
                                    ).save(out, format="PNG")
                                return True
                            except (OSError, ValueError) as e:
                                print(f"  scene gen {model}: invalid image ({e})",
                                      file=sys.stderr)
                print(f"  scene gen {model}: no image in reply",
                      file=sys.stderr)
                break                              # refused: next model
            if r.status_code == 400 and "imageConfig" in str(
                    body["generationConfig"]):
                body["generationConfig"].pop("imageConfig", None)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                if r.status_code == 429 and ("billing" in r.text
                                             or "check your plan" in r.text):
                    print(f"  scene gen: no image quota on this key "
                          f"({model}) — catalog art takes over",
                          file=sys.stderr, flush=True)
                    _GEN_DEAD = True
                    return False
                if attempt == 0:
                    print(f"  scene gen {model}: {r.status_code} "
                          f"{r.text[:220]}", file=sys.stderr, flush=True)
                time.sleep(8 * (attempt + 1))
                continue
            print(f"  scene gen {model}: {r.status_code} {r.text[:160]}",
                  file=sys.stderr)
            break                                  # 400/404 etc: next model
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
    image = _apply_lower_scrim(_fit_artwork(bg))
    draw = ImageDraw.Draw(image)
    _draw_story_headline(draw, slide["headline"])
    _draw_brand(draw, 1255, YELLOW, YELLOW)
    _save_slide(image, out)


def render_cover(work: str, cover_line: str, date_text: str, total: int,
                 out: str, bg: str | None = None) -> None:
    image = _apply_lower_scrim(
        _fit_artwork(bg or os.path.join(TEMPLATES, "cover.png")))
    draw = ImageDraw.Draw(image)
    _draw_shadow_text(draw, (W // 2, 852), "WHAT HAPPENED",
                      _font(92), "#FFFFFF")
    _draw_shadow_text(draw, (W // 2, 956), "IN THE WORLD",
                      _font(96), YELLOW)
    _draw_shadow_text(draw, (W // 2, 1060), "IN THE LAST 24 HOURS",
                      _font(64), YELLOW)
    _draw_brand(draw, 1138, "#FFFFFF", "#FFFFFF")
    x0, y0, x1, y1 = 288, 1206, 792, 1294
    cut = 22
    draw.polygon(
        [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut),
         (x1, y1 - cut), (x1 - cut, y1), (x0 + cut, y1),
         (x0, y1 - cut), (x0, y0 + cut)],
        fill=YELLOW,
    )
    _draw_shadow_text(draw, (W // 2, (y0 + y1) // 2), "SWIPE LEFT >>",
                      _font(43, bold=True), "#000000", stroke_width=0)
    _save_slide(image, out)


def _rotated_card(image: Image.Image, angle: float) -> Image.Image:
    card = Image.new("RGB", (350, 440), "#F3F3F3")
    card.paste(ImageOps.fit(image.convert("RGB"), (334, 424),
                            method=Image.Resampling.LANCZOS), (8, 8))
    return card.rotate(angle, resample=Image.Resampling.BICUBIC,
                       expand=True, fillcolor="#0A0A0C")


def render_outro(work: str, date_text: str, out: str) -> None:
    image = Image.new("RGB", (W, H), "#0A0A0C")
    cover_path = os.path.join(OUT_DIR, "01.jpg")
    cover = _fit_artwork(cover_path if os.path.exists(cover_path) else None)
    left = _rotated_card(cover, 8)

    promo = Image.new("RGB", (350, 440), "#141519")
    promo_draw = ImageDraw.Draw(promo)
    promo_draw.rectangle((2, 2, 347, 437), outline="#515159", width=3)
    promo_lines = ["THE STORIES", "THAT SHAPED", "OUR WORLD", "IN THE", "LAST DAY"]
    y = 75
    for index, line in enumerate(promo_lines):
        color = YELLOW if index in (1, 4) else "#FFFFFF"
        promo_draw.text((175, y), line, font=_font(39), fill=color,
                        anchor="mm", stroke_width=1, stroke_fill="#000000")
        y += 63
    right = promo.rotate(-8, resample=Image.Resampling.BICUBIC,
                         expand=True, fillcolor="#0A0A0C")
    image.paste(left, (65, 230))
    image.paste(right, (665, 230))
    draw = ImageDraw.Draw(image)

    arrow = []
    for step in range(31):
        t = step / 30
        arrow.append((round(440 + 180 * t),
                      round(345 - 68 * math.sin(math.pi * t))))
    draw.line(arrow, fill="#FFFFFF", width=7, joint="curve")
    end_x, end_y = arrow[-1]
    draw.polygon([(end_x, end_y), (end_x - 20, end_y - 16),
                  (end_x - 16, end_y + 13)], fill="#FFFFFF")

    draw.text((W // 2, 795), "The world's biggest stories, every day.",
              font=_font(34), fill="#FFFFFF", anchor="mm",
              stroke_width=1, stroke_fill="#000000")
    follow_font = _font(45)
    follow = "Follow "
    prefix_width = draw.textlength(follow, font=follow_font)
    handle_width = draw.textlength(IG_HANDLE, font=follow_font)
    x = (W - prefix_width - handle_width) / 2
    draw.text((round(x), 900), follow, font=follow_font, fill="#FFFFFF",
              anchor="lm")
    draw.text((round(x + prefix_width), 900), IG_HANDLE, font=follow_font,
              fill="#D4FF00", anchor="lm")

    badge_y = 1174
    center = W // 2
    draw.line((118, badge_y, center - 70, badge_y), fill="#6A6A70", width=2)
    draw.line((center + 70, badge_y, W - 118, badge_y), fill="#6A6A70", width=2)
    draw.ellipse((center - 58, badge_y - 58, center + 58, badge_y + 58),
                 outline="#D4FF00", width=3)
    draw.text((center, badge_y), "MW", font=_font(42), fill="#FFFFFF", anchor="mm")
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
                          index: int) -> tuple[str, int] | None:
    """Download the photo selected as relevant, requiring at least 900px width."""
    pick = slide.get("photo")
    if not isinstance(pick, int) or not 1 <= pick <= len(posts):
        return None
    urls = posts[pick - 1].get("images") or []
    if not urls:
        return None
    for candidate, url in enumerate(urls):
        raw = os.path.join(work, f"photo{index}-{candidate}.img")
        try:
            if dn._download(url, raw, dn.UA, min_size=(900, 1)):
                return raw, pick
        except requests.exceptions.RequestException as e:
            print(f"    story photo unavailable ({str(e)[:80]}); "
                  "trying next candidate", file=sys.stderr)
    return None


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
            if json.load(open(deck_path)).get("date") == day.isoformat():
                print(f"slides for {day} already rendered — nothing to do")
                return
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            print(f"  ignoring unreadable cached deck: {exc}", file=sys.stderr)
    posts = _posts_last_24h(end)
    if len(posts) < 2:
        print("not enough posts for a carousel")
        return
    deck = write_deck(posts, day)
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
    render_cover(work, deck.get("cover", "The day around the globe"),
                 date_text, total, os.path.join(OUT_DIR, "01.jpg"),
                 bg=cover_bg)
    print(f"  slide 1: cover ({'generated' if cover_bg else 'backdrop'})")
    for i, slide in enumerate(deck["slides"]):
        # Use the visually selected source photo when it has enough resolution;
        # otherwise retain the regional branded background.
        bg, label, graded = art_for(slide.get("region", "World")), "backdrop", False
        photo = _download_story_photo(slide, posts, work, i)
        if photo:
            bg, pick = photo
            label, graded = f"post {pick} photo", True
        if not graded and slide.get("scene_prompt"):
            gen = os.path.join(work, f"gen{i}.png")
            if gen_scene(slide["scene_prompt"], gen):
                bg, label, graded = gen, "generated scene", True
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


GRAPH = "https://graph.facebook.com/v21.0"


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
        if open(IG_MARKER).read().strip() == day:
            print(f"{day} already posted to Instagram — skipping")
            return
    except FileNotFoundError:
        pass

    caption = deck.get("caption", "")
    if not caption.startswith(
            "Here's What Happened in the World in the Last 24 Hours:"):
        raise RuntimeError("deck is missing its formatted 24-hour caption")
    if len(caption) > 2200:
        raise RuntimeError(
            "carousel caption exceeds Instagram's 2,200-character limit")
    repo = os.getenv("GITHUB_REPOSITORY", "itssergekalajian-hub/newsbotdaily")
    raw = f"https://raw.githubusercontent.com/{repo}/main/{OUT_DIR}"
    files = sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".jpg"))
    if len(files) != MAX_SLIDES:
        raise RuntimeError(
            f"carousel has {len(files)} images; expected exactly {MAX_SLIDES}")
    children = []
    for f in files:
        r = requests.post(f"{GRAPH}/{uid}/media",
                          data={"image_url": f"{raw}/{f}",
                                "is_carousel_item": "true",
                                "access_token": tok}, timeout=60)
        r.raise_for_status()
        children.append(r.json()["id"])
        print(f"  container for {f}: {children[-1]}")

    r = requests.post(f"{GRAPH}/{uid}/media",
                      data={"media_type": "CAROUSEL",
                            "children": ",".join(children),
                            "caption": caption,
                            "access_token": tok}, timeout=60)
    r.raise_for_status()
    carousel = r.json()["id"]

    # containers process asynchronously; publish retries while they finish
    for attempt in range(10):
        r = requests.post(f"{GRAPH}/{uid}/media_publish",
                          data={"creation_id": carousel,
                                "access_token": tok}, timeout=60)
        if r.ok:
            print(f"published to Instagram: media {r.json().get('id')}")
            with open(IG_MARKER, "w") as f:
                f.write(day)
            return
        time.sleep(15)
    raise RuntimeError(f"carousel publish failed: {r.text[:300]}")


if __name__ == "__main__":
    {"render": render, "publish": publish, "probe": probe}[sys.argv[1]]()
