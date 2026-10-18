"""MidWorld Today — the daily Instagram carousel.

Still picture slides (never a reel): a branded cover, one slide per region of
the globe with the day's one most important story there explained briefly,
sports last when something genuinely happened, and a follow-up outro. The
writing comes from the same channel scrape as the video bulletin; the art is
a fixed set of Higgsfield-generated backgrounds in templates/ig/, and the
text is drawn razor-sharp by ffmpeg, never baked into generated images.

Two entry points, run as separate workflow steps so the slides are committed
(and publicly addressable) before Instagram is asked to fetch them:

    python insta_slides.py render    — write slides/latest/*.jpg + deck.json
    python insta_slides.py publish   — post the carousel via the IG Graph API

Publishing needs two repo secrets — IG_USER_ID (the Instagram Business
account's numeric id) and IG_ACCESS_TOKEN (a long-lived Graph API token with
instagram_content_publish) — and quietly explains what's missing when they
aren't set, so the render keeps working before the account exists.
"""

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time

import requests

import daily_news as dn
import video

W, H = 1080, 1350                 # Instagram portrait
OUT_DIR = "slides/latest"
TEMPLATES = "templates/ig"
IG_MARKER = ".last_ig"            # last day actually POSTED to Instagram
MAX_SLIDES = 8                    # cover + up to 6 stories + outro fits 10 cap
BRAND = "MIDWORLD DAILY"
IG_HANDLE = os.getenv("IG_HANDLE", "").strip().lstrip("@")

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


SLIDES_PROMPT = """You are the editor of MidWorld Today, a daily Instagram
carousel of still slides. Below is everything a news channel published on
{date}, plus same-day agency headlines for context only.

Pick the day's most important story in EACH region of the globe that had real
news — typically Middle East, Europe, Asia, Americas, Africa — between three
and six regions. Skip a region with nothing that matters. Finish with ONE
sports slide ONLY if something genuinely important happened in sport; omit it
otherwise. Order the regions by how much the story matters.

Return ONLY JSON, no markdown fences:
{{"cover": "one punchy line summing up the day, eight to fourteen words",
  "cover_scene": "one vivid sentence describing an editorial illustration
                  for the day as a whole (see scene rules)",
  "slides": [{{"region": "Middle East",
              "photo": 17,
              "scene": "one vivid sentence describing an editorial
                        illustration for THIS story",
              "headline": "the story in five to nine words, no final stop",
              "body": "25 to 45 words explaining it briefly and clearly:
                       WHO did WHAT, WHY it happened, and the one concrete
                       consequence or thing to watch. Plain complete
                       sentences a stranger understands with no context."}}]}}

Rules:
- "region" is 1 to 3 words as shown above ("Sports" for the sports slide).
- "scene" drives an AI illustration that fills the slide, so make it CONCRETE
  and specific to the story: the flags of the nations involved, a named
  landmark or setting, the objects of the story (a tanker in a strait, a
  fighter jet, a trading floor, a stadium), the weather and mood. NEVER
  include identifiable real people or faces, never text, signs or logos.
  Example: "A damaged oil tanker escorted through a narrow strait at dawn,
  Iranian and Panamanian flags on masts, warships silhouetted on the
  horizon".
- "photo" is the number of ONE post marked [has photo] whose photograph —
  ATTACHED below as "PHOTO attached to POST n", look at it — actually SHOWS
  this slide's story. Choose null unless you can SEE that the photo depicts
  this story's event, place or people. A logo, a screenshot of text, a meme,
  a chart or an unrelated portrait NEVER qualifies.
- Facts come ONLY from the posts and the agency headlines; attribute anything
  that exists only in an agency headline ("per BBC"). Never invent.
- No emojis, no hashtags, no quotes around the texts, no markdown.

POSTS FROM {date}:
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
        r = requests.post(f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
                          headers=headers, json=body, timeout=180)
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
              if s.get("region") and s.get("headline") and s.get("body")]
    if not slides:
        raise RuntimeError("deck came back empty")
    # sports closes the carousel when present, exactly one
    sports = [s for s in slides if "sport" in s["region"].lower()
              or s["region"].lower() in ("football", "mma")]
    rest = [s for s in slides if s not in sports]
    deck["slides"] = (rest + sports[:1])[:MAX_SLIDES - 2]
    deck["date"] = day.isoformat()
    return deck


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur); cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;") + "..."
    return lines


def _base(bg: str) -> str:
    return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1")


# Anton: the tall condensed display face the poster format is built on —
# committed to the repo (OFL licence) so every runner renders identically
ANTON = os.path.join("fonts", "Anton-Regular.ttf")


def _display_font() -> str:
    return ANTON if os.path.exists(ANTON) else video.BOLD


# the fixed house look every generated illustration is asked for — ONE style
# across every slide, every day, is what makes the feed read as a brand
SCENE_STYLE = (
    ". Cinematic photorealistic editorial news illustration, dramatic stormy "
    "sky, deep navy blue and crimson red colour palette, volumetric light, "
    "high detail, moody and simple lower half for a text overlay, vertical "
    "portrait composition. No words, no letters, no numbers, no captions, "
    "no logos, no watermarks, no identifiable real people.")


def image_models() -> tuple[str, ...]:
    r = requests.get(f"{dn.GEMINI_ROOT}/models",
                     headers={"x-goog-api-key": dn.GEMINI_KEY}, timeout=60)
    r.raise_for_status()
    names = [m["name"].removeprefix("models/")
             for m in r.json().get("models", [])
             if "image" in m["name"]
             and "generateContent" in m.get("supportedGenerationMethods", [])]

    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("preview" in n or "exp" in n,
                -(float(m.group(1)) if m else 0.0), len(n))

    return tuple(sorted(names, key=rank))


def gen_scene(scene: str, out: str) -> bool:
    """One on-theme illustration from the slide's scene line, best-effort.

    Uses the same Gemini key as the writer. Every failure path returns False
    so the slide falls back to the story photo or the branded backdrop — an
    image model outage can never cost the deck.
    """
    headers = {"x-goog-api-key": dn.GEMINI_KEY,
               "Content-Type": "application/json"}
    try:
        models = image_models()[:3]
    except Exception as e:
        print(f"  image models unavailable ({str(e)[:60]})", file=sys.stderr)
        return False
    for model in models:
        # image models answer with a text part AND an image part; asking for
        # IMAGE alone is an INVALID_ARGUMENT on the gemini image family
        body = {"contents": [{"parts": [{"text": scene + SCENE_STYLE}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": "4:5"}}}
        for attempt in range(4):
            try:
                r = requests.post(
                    f"{dn.GEMINI_ROOT}/models/{model}:generateContent",
                    headers=headers, json=body, timeout=180)
            except Exception as e:
                print(f"  scene gen {model}: {str(e)[:80]}", file=sys.stderr)
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
                            with open(out, "wb") as f:
                                f.write(data)
                            return True
                print(f"  scene gen {model}: no image in reply",
                      file=sys.stderr)
                break                              # refused: next model
            if r.status_code == 400 and "imageConfig" in str(
                    body["generationConfig"]):
                body["generationConfig"].pop("imageConfig", None)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
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
    """One story slide in the poster format.

    Full-bleed image, a smooth fade into solid navy, and the story told in
    big stacked condensed capitals — lines alternating white and crimson —
    with a short plain-type explainer beneath and a centred brand footer.
    Photographs pass through one fixed brand grade so no slide arrives with
    clashing colour; generated scenes are asked for the palette outright.
    The geometry is identical on every slide, which is what makes the feed
    read as one designed system.
    """
    tag = f"s{idx}"
    disp = _display_font()
    head_lines = [l.upper() for l in _wrap(slide["headline"], 16, 5)]
    body_lines = _wrap(slide["body"], 52, 3)

    head_fs, head_step = 96, 108
    body_fs, body_step = 34, 46
    foot_y = H - 96
    body_h = len(body_lines) * body_step
    y_body = foot_y - 46 - body_h
    head_h = len(head_lines) * head_step
    y_head = y_body - 30 - head_h
    y_chip = y_head - 72
    grad_top = max(y_chip - 300, 140)

    grade = (",eq=saturation=0.72:contrast=1.06,"
             "colorbalance=bs=0.12:rs=-0.05:bm=0.05:gm=0.01"
             if is_photo else "")

    shadow = ":shadowcolor=0x000000@0.6:shadowx=0:shadowy=4"
    c = []
    rf = video._textfile(work, f"{tag}_region.txt", slide["region"].upper())
    c.append(f"drawtext=fontfile={video.BOLD}:textfile={rf}:fontcolor=white"
             f":fontsize=30:box=1:boxcolor={video.RED}@0.96:boxborderw=12"
             f":x=(w-tw)/2:y={y_chip}")
    y = y_head
    for j, line in enumerate(head_lines):
        hf = video._textfile(work, f"{tag}_h{j}.txt", line)
        color = "white" if j % 2 == 0 else "0xE8374F"
        c.append(f"drawtext=fontfile={disp}:textfile={hf}"
                 f":fontcolor={color}:fontsize={head_fs}"
                 f":x=(w-tw)/2:y={y}{shadow}")
        y += head_step
    y = y_body
    for j, line in enumerate(body_lines):
        bf = video._textfile(work, f"{tag}_b{j}.txt", line)
        c.append(f"drawtext=fontfile={video.REGULAR}:textfile={bf}"
                 f":fontcolor=0xD9E2EF:fontsize={body_fs}"
                 f":x=(w-tw)/2:y={y}{shadow}")
        y += body_step
    brf = video._textfile(work, f"{tag}_brand.txt", "—   MIDWORLD  DAILY   —")
    c.append(f"drawtext=fontfile={video.BOLD}:textfile={brf}"
             f":fontcolor=white:fontsize=30:x=(w-tw)/2:y={foot_y}")
    mf = video._textfile(work, f"{tag}_meta.txt",
                         f"{date_text}   ·   {idx + 1}/{total}")
    c.append(f"drawtext=fontfile={video.REGULAR}:textfile={mf}"
             f":fontcolor={video.PALE}:fontsize=24:x=(w-tw)/2:y={foot_y + 42}")

    graph = (
        f"[0:v]{_base('')}{grade}[bg];"
        f"[1:v]format=rgba[nv];[2:v]format=gray[gr];"
        f"[nv][gr]alphamerge[scrim];"
        f"[bg][scrim]overlay=0:0,{','.join(c)}[v]"
    )
    video.run(["ffmpeg", "-y", "-loglevel", "error",
               "-i", bg,
               "-f", "lavfi", "-i", f"color=c={video.NAVY}:s={W}x{H}:d=1",
               "-f", "lavfi", "-i",
               f"gradients=s={W}x{H}:c0=black:c1=white"
               f":x0={W // 2}:y0={grad_top}:x1={W // 2}:y1={H - 170}:d=1",
               "-filter_complex", graph, "-map", "[v]",
               "-frames:v", "1", "-q:v", "2", out])


def render_cover(work: str, cover_line: str, date_text: str, total: int,
                 out: str, bg: str | None = None) -> None:
    disp = _display_font()
    bg = bg or os.path.join(TEMPLATES, "cover.png")
    lines = [l.upper() for l in _wrap(cover_line, 18, 4)]
    step = 102
    y0 = H - 210 - 46 - len(lines) * step
    c = []
    tf = video._textfile(work, "c_brand.txt", "MIDWORLD")
    c.append(f"drawtext=fontfile={disp}:textfile={tf}:fontcolor=white"
             f":fontsize=120:x=(w-tw)/2:y=170"
             f":shadowcolor=0x000000@0.6:shadowx=0:shadowy=4")
    td = video._textfile(work, "c_today.txt", "TODAY")
    c.append(f"drawtext=fontfile={disp}:textfile={td}:fontcolor=white"
             f":fontsize=54:box=1:boxcolor={video.RED}@0.96:boxborderw=14"
             f":x=(w-tw)/2:y=316")
    df = video._textfile(work, "c_date.txt", date_text)
    c.append(f"drawtext=fontfile={video.REGULAR}:textfile={df}"
             f":fontcolor={video.PALE}:fontsize=38:x=(w-tw)/2:y=432")
    y = y0
    for j, line in enumerate(lines):
        lf = video._textfile(work, f"c_l{j}.txt", line)
        color = "white" if j % 2 == 0 else "0xE8374F"
        c.append(f"drawtext=fontfile={disp}:textfile={lf}:fontcolor={color}"
                 f":fontsize=88:x=(w-tw)/2:y={y}"
                 f":shadowcolor=0x000000@0.6:shadowx=0:shadowy=4")
        y += step
    sf = video._textfile(work, "c_swipe.txt",
                         f"{total - 2} STORIES   ·   SWIPE")
    c.append(f"drawtext=fontfile={video.BOLD}:textfile={sf}"
             f":fontcolor=white:fontsize=32:box=1"
             f":boxcolor={video.NAVY}@0.85:boxborderw=14:x=(w-tw)/2:y=h-120")
    graph = (
        f"[0:v]{_base('')}[bg];"
        f"[1:v]format=rgba[nv];[2:v]format=gray[gr];"
        f"[nv][gr]alphamerge[scrim];"
        f"[bg][scrim]overlay=0:0,{','.join(c)}[v]"
    )
    video.run(["ffmpeg", "-y", "-loglevel", "error", "-i", bg,
               "-f", "lavfi", "-i", f"color=c={video.NAVY}:s={W}x{H}:d=1",
               "-f", "lavfi", "-i",
               f"gradients=s={W}x{H}:c0=black:c1=white"
               f":x0={W // 2}:y0={y0 - 280}:x1={W // 2}:y1={H - 160}:d=1",
               "-filter_complex", graph, "-map", "[v]",
               "-frames:v", "1", "-q:v", "2", out])


def render_outro(work: str, date_text: str, out: str) -> None:
    disp = _display_font()
    c = [_base("")]
    tf = video._textfile(work, "o_done.txt", "THAT'S YOUR DAY.")
    c.append(f"drawtext=fontfile={disp}:textfile={tf}:fontcolor=white"
             f":fontsize=96:x=(w-tw)/2:y=520"
             f":shadowcolor=0x000000@0.6:shadowx=0:shadowy=4")
    lines = ["Full video bulletin on Telegram", "@midworldnews"]
    if IG_HANDLE:
        lines.append(f"Follow @{IG_HANDLE} for tomorrow's brief")
    y = 680
    for j, line in enumerate(lines):
        lf = video._textfile(work, f"o_l{j}.txt", line)
        c.append(f"drawtext=fontfile={video.REGULAR}:textfile={lf}"
                 f":fontcolor={video.PALE}:fontsize=42:x=(w-tw)/2:y={y}")
        y += 60
    brf = video._textfile(work, "o_brand.txt", "—   MIDWORLD  DAILY   —")
    c.append(f"drawtext=fontfile={video.BOLD}:textfile={brf}"
             f":fontcolor=white:fontsize=30:x=(w-tw)/2:y=h-96")
    df = video._textfile(work, "o_date.txt", date_text)
    c.append(f"drawtext=fontfile={video.REGULAR}:textfile={df}"
             f":fontcolor={video.PALE}:fontsize=24:x=(w-tw)/2:y=h-54")
    video.run(["ffmpeg", "-y", "-loglevel", "error", "-i",
               os.path.join(TEMPLATES, "outro.png"), "-vf", ",".join(c),
               "-frames:v", "1", "-q:v", "2", out])


def target_day() -> dt.date:
    env = os.getenv("TARGET_DATE", "").strip()
    if env:
        return dt.date.fromisoformat(env)
    # just after local midnight this is the day that just ended; by afternoon
    # it is today-so-far — either way, the most recent full stretch of news
    return (dt.datetime.now(dn.TZ) - dt.timedelta(hours=12)).date()


def render() -> None:
    day = target_day()
    deck_path = os.path.join(OUT_DIR, "deck.json")
    if not os.getenv("FORCE_SLIDES") and os.path.exists(deck_path):
        try:
            if json.load(open(deck_path)).get("date") == day.isoformat():
                print(f"slides for {day} already rendered — nothing to do")
                return
        except Exception:
            pass
    posts = dn.fetch_posts(dn.SOURCE, day)
    if len(posts) < 2:
        print("not enough posts for a carousel")
        return
    deck = write_deck(posts, day)
    total = len(deck["slides"]) + 2
    date_text = f"{day:%d %B %Y}"
    os.makedirs(OUT_DIR, exist_ok=True)
    for old in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, old))
    work = tempfile.mkdtemp(prefix="ig-")
    cover_bg = None
    if deck.get("cover_scene"):
        gen = os.path.join(work, "cover_gen.png")
        if gen_scene(deck["cover_scene"], gen):
            cover_bg = gen
    render_cover(work, deck.get("cover", "The day around the globe"),
                 date_text, total, os.path.join(OUT_DIR, "01.jpg"),
                 bg=cover_bg)
    print(f"  slide 1: cover ({'generated' if cover_bg else 'backdrop'})")
    for i, slide in enumerate(deck["slides"]):
        # each story gets its OWN illustration, generated in the house look
        # from the writer's scene line; the story's photograph is the first
        # fallback and the branded regional backdrop the last
        bg, label, graded = art_for(slide["region"]), "backdrop", False
        gen = os.path.join(work, f"gen{i}.png")
        if slide.get("scene") and gen_scene(slide["scene"], gen):
            bg, label, graded = gen, "generated scene", True
        else:
            pick = slide.get("photo")
            if isinstance(pick, int) and 1 <= pick <= len(posts):
                urls = posts[pick - 1].get("images") or []
                raw = os.path.join(work, f"photo{i}.img")
                if urls and dn._download(urls[0], raw, dn.UA,
                                         min_size=(640, 360)):
                    bg, label, graded = raw, f"post {pick} photo", True
        render_story(work, i + 1, total, slide, date_text, bg,
                     os.path.join(OUT_DIR, f"{i + 2:02d}.jpg"),
                     is_photo=graded)
        print(f"  slide {i + 2}: {slide['region']} — {slide['headline']}"
              f" ({label})")
    render_outro(work, date_text, os.path.join(OUT_DIR, f"{total:02d}.jpg"))
    with open(deck_path, "w", encoding="utf-8") as f:
        json.dump(deck, f, ensure_ascii=False, indent=1)
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

    repo = os.getenv("GITHUB_REPOSITORY", "itssergekalajian-hub/newsbotdaily")
    raw = f"https://raw.githubusercontent.com/{repo}/main/{OUT_DIR}"
    files = sorted(f for f in os.listdir(OUT_DIR) if f.endswith(".jpg"))
    children = []
    for f in files:
        r = requests.post(f"{GRAPH}/{uid}/media",
                          data={"image_url": f"{raw}/{f}",
                                "is_carousel_item": "true",
                                "access_token": tok}, timeout=60)
        r.raise_for_status()
        children.append(r.json()["id"])
        print(f"  container for {f}: {children[-1]}")

    caption = deck.get("cover", "Today, around the globe.")
    caption += "\n\n" + "\n".join(
        f"• {s['region']}: {s['headline']}" for s in deck["slides"])
    caption += "\n\nFull video bulletin on Telegram: t.me/midworldnews"
    caption += "\nImagery: AI-assisted editorial illustration."
    caption += "\n\n#news #worldnews #daily #briefing"
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
