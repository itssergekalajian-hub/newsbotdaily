"""Broadcast-style video assembly for MidWorld Daily.

Builds a title card, one animated scene per topic, and an outro, then joins
them. Everything is ffmpeg — no GPU, no paid service.

One hard-won constraint shapes this module: `drawbox` evaluates its x/y/w/h
expressions ONCE, at filter setup, so anything drawn with it is frozen for the
whole clip. `drawtext` and `overlay` (with eval=frame) do re-evaluate per frame.
So every moving element here is either a drawtext — using its own box= backing
plate — or a colour source overlaid at an animated position. Static furniture
still uses drawbox, which is cheaper.
"""

from __future__ import annotations

import os
import subprocess
import sys

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
BOLD = f"{FONT_DIR}/DejaVuSans-Bold.ttf"
REGULAR = f"{FONT_DIR}/DejaVuSans.ttf"

W, H, FPS = 1280, 720, 25
NAVY = "0x0A1428"
RED = "0xC8102E"
PALE = "0x9FB6D4"

SUB_STYLE = ("Fontname=DejaVu Sans,Fontsize=17,PrimaryColour=&H00FFFFFF,"
             "BackColour=&HC0000000,BorderStyle=4,Outline=0,Shadow=0,MarginV=52")

ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
       "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]


def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg failed ({p.returncode}):\n{tail}")


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _textfile(work: str, name: str, text: str) -> str:
    """drawtext reads its text from a file, sidestepping every escaping rule.

    Headlines contain colons, commas, apostrophes and percent signs — each of
    which means something inside a filtergraph.
    """
    path = os.path.join(work, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path.replace("\\", "/")


def _motion(frames: int, variant: int) -> str:
    """Ken Burns move sized to the clip, so it never freezes part-way through.

    Both zoom and pan are fractions of the clip's own length, and the pan is a
    fraction of the available slack, so the window stays inside the source at
    any duration without any bounds arithmetic.
    """
    f = max(frames, 2)
    zin, zout = f"min(1+0.12*on/{f},1.12)", f"max(1.12-0.12*on/{f},1.0)"
    presets = [
        (zin,  f"(iw-iw/zoom)*(0.5+0.30*on/{f})", f"(ih-ih/zoom)*(0.5-0.25*on/{f})"),
        (zout, f"(iw-iw/zoom)*(0.5-0.30*on/{f})", f"(ih-ih/zoom)*(0.5+0.25*on/{f})"),
        (zin,  f"(iw-iw/zoom)*(0.5-0.28*on/{f})", f"(ih-ih/zoom)*(0.5+0.20*on/{f})"),
        (zout, f"(iw-iw/zoom)*(0.5+0.28*on/{f})", f"(ih-ih/zoom)*(0.5-0.20*on/{f})"),
    ]
    z, x, y = presets[variant % len(presets)]
    return (f"scale=1600:900,zoompan=z='{z}':x='{x}':y='{y}'"
            f":d=1:s={W}x{H}:fps={FPS}")


def render_scene(work: str, presenter: str, audio: str, srt: str, topic: str,
                 brand: str, date_text: str, variant: int,
                 elapsed: float, total: float, out: str) -> None:
    """One topic: moving anchor plate, chrome, lower third, captions."""
    seconds = probe_duration(audio)
    frames = max(int(seconds * FPS), 2)

    bf = _textfile(work, "brand.txt", brand)
    df = _textfile(work, "date.txt", date_text)
    tf = _textfile(work, f"topic{variant}.txt", topic.upper())

    # progress bar: a full-width strip slid in from the left, so the visible
    # portion equals how far through the whole bulletin we are
    bar = (f"[bg][2:v]overlay=x='W*(({elapsed}+t)/{max(total, 1)}-1)'"
           f":y=H-6:eval=frame[p]")

    chrome = ",".join([
        f"drawbox=x=0:y=0:w=iw:h=54:color={NAVY}@0.92:t=fill",
        f"drawbox=x=0:y=54:w=iw:h=3:color={RED}:t=fill",
        f"drawtext=fontfile={BOLD}:textfile={bf}:fontcolor=white:fontsize=24:x=32:y=15",
        f"drawtext=fontfile={REGULAR}:textfile={df}:fontcolor={PALE}:fontsize=19:x=w-tw-32:y=18",
        # lower third slides in over 0.6s; box= gives it its own backing plate
        f"drawtext=fontfile={BOLD}:textfile={tf}:fontcolor=white:fontsize=28"
        f":box=1:boxcolor={RED}@0.95:boxborderw=18"
        f":x='-600+min(t/0.6\\,1)*640':y=566",
    ])

    subs = ""
    if srt and os.path.exists(srt) and os.path.getsize(srt) > 20:
        subs = f",subtitles='{srt}':force_style='{SUB_STYLE}'"

    still = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    for label, bg in (("motion", _motion(frames, variant)), ("still", still)):
        graph = f"[0:v]{bg}[bg];{bar};[p]{chrome}{subs}[v]"
        try:
            run(["ffmpeg", "-y", "-loglevel", "error",
                 "-loop", "1", "-framerate", str(FPS), "-i", presenter,
                 "-i", audio,
                 "-f", "lavfi", "-i",
                 f"color=c={RED}@0.9:s={W}x6:r={FPS}:d={seconds + 1}",
                 "-filter_complex", graph,
                 "-map", "[v]", "-map", "1:a", *ENC, "-shortest", out])
            return
        except RuntimeError as e:
            print(f"  scene '{topic}' {label} render failed: {e}", file=sys.stderr)
    raise RuntimeError(f"could not render scene: {topic}")


def render_card(work: str, seconds: float, lines: list[tuple[str, int, str]],
                out: str, wipe: bool = True) -> None:
    """Title or outro card: flat navy, a rule that slides in, stacked text."""
    y_title, y_rule = 240, 330
    parts = []
    for i, (text, size, color) in enumerate(lines):
        tf = _textfile(work, f"card_{os.path.basename(out)}_{i}.txt", text)
        y = y_title if i == 0 else y_rule + 40 + (i - 1) * 46
        fade = f":alpha='min(max((t-{0.3 + i * 0.25})/0.5\\,0)\\,1)'"
        parts.append(
            f"drawtext=fontfile={BOLD if size > 30 else REGULAR}:textfile={tf}"
            f":fontcolor={color}:fontsize={size}:x=(w-tw)/2:y={y}{fade}")
    parts.append(f"fade=t=out:st={max(seconds - 0.5, 0.1):.2f}:d=0.5")
    chain = ",".join(parts)

    inputs = ["-f", "lavfi", "-i", f"color=c={NAVY}:s={W}x{H}:r={FPS}:d={seconds}",
              "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}"]
    if wipe:
        inputs += ["-f", "lavfi", "-i",
                   f"color=c={RED}:s=520x4:r={FPS}:d={seconds}"]
        graph = (f"[0:v][2:v]overlay=x='min(t/0.5\\,1)*((W-520)/2+520)-520'"
                 f":y={y_rule}:eval=frame[b];[b]{chain}[v]")
    else:
        graph = f"[0:v]{chain}[v]"

    run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
         "-filter_complex", graph,
         "-map", "[v]", "-map", "1:a", *ENC, "-t", str(seconds), out])


def concat(parts: list[str], out: str) -> None:
    listing = out + ".txt"
    with open(listing, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", listing, "-c", "copy", "-movflags", "+faststart", out])
    except RuntimeError:
        # stream copy is fussy about parameter drift between parts
        print("  concat copy failed, re-encoding", file=sys.stderr)
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", listing, *ENC, "-movflags", "+faststart", out])
