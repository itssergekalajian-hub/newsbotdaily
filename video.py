"""Broadcast-style video assembly for MidWorld Daily.

Builds a title card, one scene per topic, and an outro, then joins them.
Everything is ffmpeg — no GPU, no paid service.

Two constraints shape this module.

First: NO Ken Burns. `zoompan` rounds its window position to whole pixels, so a
slow drift holds the frame still for ten-odd frames and then snaps a pixel
sideways. Measured on a real bulletin, most consecutive frames differed by 0.00
and then one by 1.07 — that reads as a shake, not a drift, and slowing it down
only lengthens the freeze between jumps. Real bulletins use locked-off cameras
anyway, so each scene holds a still framing and the movement comes from cutting
between framings and from graphics that travel fast enough for whole-pixel
steps to be invisible.

Second: `drawbox` evaluates its geometry once, at filter setup, so anything
drawn with it is frozen for the whole clip. Moving elements are therefore
either `drawtext` (which re-evaluates per frame and carries its own box=
backing plate) or a colour source overlaid with eval=frame. Static furniture
stays on drawbox, which is cheaper.
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

# Caption styling lives in the .ass file itself (see ASS_HEAD in daily_news.py),
# which declares PlayResX/PlayResY so its numbers are real pixels. Overriding it
# here with force_style would drag back the 384x288 scaling problem.

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


def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split("x")[:2]
    return int(w), int(h)


def framing(presenter: str, variant: int) -> str:
    """A locked-off camera angle. Cutting between these supplies the movement.

    Every crop is computed once and never animated, so there is nothing to
    judder. The subject sits right of centre in the source, so tighter framings
    bias that way rather than cropping the middle.
    """
    sw, sh = probe_size(presenter)
    ar = W / H
    fw = min(sw, int(sh * ar))          # widest 16:9 crop the source allows
    fh = int(fw / ar)

    # (fraction of the full frame, horizontal bias 0..1): wide, medium, close
    presets = [(1.00, 0.50), (0.82, 0.62), (0.68, 0.66), (0.88, 0.40)]
    scale, bias = presets[variant % len(presets)]

    cw = max(int(fw * scale) // 2 * 2, 320)
    ch = max(int(cw / ar) // 2 * 2, 180)
    x = max(0, min(int((sw - cw) * bias), sw - cw))
    y = max(0, min(int((sh - ch) * 0.42), sh - ch))   # keeps heads high
    return f"crop={cw}:{ch}:{x}:{y},scale={W}:{H},setsar=1"


# Trimming both ends of every synthesised sentence. edge-tts pads each clip with
# roughly a quarter-second of silence at the front and a third at the back, and
# because the bulletin is built one sentence at a time those pads stack at every
# join — measured at ~0.63s per sentence, close to a minute of dead air across a
# six-minute read. Trimming them and inserting a deliberate, much shorter gap is
# what makes the delivery sound continuous rather than stop-start.
TRIM = ("silenceremove=start_periods=1:start_silence=0:start_threshold=-50dB"
        ":detection=peak,areverse,"
        "silenceremove=start_periods=1:start_silence=0:start_threshold=-50dB"
        ":detection=peak,areverse")


def trim_silence(src: str, dst: str) -> float:
    """Strip leading/trailing silence, return the trimmed duration."""
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-af", TRIM, "-ar", "48000", "-ac", "1", dst])
    return probe_duration(dst)


def silence(work: str, seconds: float, name: str) -> str:
    path = os.path.join(work, name)
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=mono", "-t", f"{seconds:.3f}", path])
    return path


def join_audio(work: str, pieces: list[str], out: str) -> None:
    listing = os.path.join(work, os.path.basename(out) + ".txt")
    with open(listing, "w") as f:
        for p in pieces:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listing, "-c:a", "libmp3lame", "-q:a", "3", out])


PANEL_W, PANEL_H = 452, 254


def make_panel(work: str, image: str, name: str) -> str:
    """Crop a topic image to the panel size and give it a border.

    Done as a separate pass so the scene filtergraph stays simple, and so a
    corrupt download fails here rather than halfway through a long render.
    """
    out = os.path.join(work, name)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", image,
         "-vf", (f"scale={PANEL_W}:{PANEL_H}:force_original_aspect_ratio=increase,"
                 f"crop={PANEL_W}:{PANEL_H},"
                 f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.85:t=3"),
         "-frames:v", "1", out])
    return out


def build_bed(work: str, seconds: float) -> str:
    """A soft four-chord pad, synthesised — no licensing, no download.

    Roots move every 26 seconds so it evolves instead of droning. Everything is
    low-passed hard and kept far below the voice; it is meant to remove the dead
    silence between sentences, not to be listened to.
    """
    roots = [110.0, 87.31, 130.81, 98.0]          # A2, F2, C3, G2
    parts = []
    for i, r in enumerate(roots):
        piece = os.path.join(work, f"chord{i}.wav")
        graph = (f"sine=r=48000:frequency={r}:duration=26,volume=0.5[a];"
                 f"sine=r=48000:frequency={r * 1.5:.2f}:duration=26,volume=0.30[b];"
                 f"sine=r=48000:frequency={r * 2:.2f}:duration=26,volume=0.18[c];"
                 f"[a][b][c]amix=inputs=3:normalize=0,"
                 f"tremolo=f=0.1:d=0.3,lowpass=f=480,"
                 f"afade=t=in:st=0:d=3,afade=t=out:st=23:d=3,"
                 f"aformat=channel_layouts=stereo")
        run(["ffmpeg", "-y", "-loglevel", "error",
             "-filter_complex", graph, "-t", "26", piece])
        parts.append(piece)

    listing = os.path.join(work, "bed_list.txt")
    with open(listing, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    bed = os.path.join(work, "bed.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listing, "-c", "copy", bed])
    return bed


def add_music(video_in: str, bed: str, out: str, level: float = 0.30) -> None:
    """Mix the bed under the finished bulletin, ducking it beneath the voice.

    sidechaincompress keys the music off the narration, so it lifts in the gaps
    and pulls back the moment anyone speaks. Measured on a real bulletin: the
    pauses go from near-silent to +16 dB while the speech level does not move at
    all. The video is stream-copied — only the audio is re-encoded.
    """
    graph = (f"[1:a]volume={level}[m];"
             f"[m][0:a]sidechaincompress=threshold=0.015:ratio=8"
             f":attack=15:release=500[duck];"
             f"[duck][0:a]amix=inputs=2:normalize=0:duration=first[out]")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_in,
         "-stream_loop", "-1", "-i", bed,
         "-filter_complex", graph, "-map", "0:v", "-map", "[out]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", "-shortest", out])


def render_scene(work: str, presenter: str, audio: str, srt: str, topic: str,
                 headline: str, brand: str, date_text: str, ticker: str,
                 variant: int, elapsed: float, total: float, out: str,
                 panel: str | None = None, credit: str = "") -> None:
    """One topic: locked framing, chrome, lower third, ticker, captions."""
    seconds = probe_duration(audio)

    bf = _textfile(work, "brand.txt", brand)
    df = _textfile(work, "date.txt", date_text)
    tf = _textfile(work, f"topic{variant}.txt", topic.upper())
    hf = _textfile(work, f"head{variant}.txt", headline) if headline else ""
    kf = _textfile(work, "ticker.txt", ticker) if ticker else ""

    # progress strip: a full-width bar slid in from the left, so the visible
    # part equals how far through the whole bulletin we are
    bar = (f"[bg][2:v]overlay=x='W*(({elapsed}+t)/{max(total, 1)}-1)'"
           f":y=H-5:eval=frame[p]")

    chrome = [
        f"drawbox=x=0:y=0:w=iw:h=52:color={NAVY}@0.94:t=fill",
        f"drawbox=x=0:y=52:w=iw:h=3:color={RED}:t=fill",
        f"drawtext=fontfile={BOLD}:textfile={bf}:fontcolor=white:fontsize=23:x=30:y=15",
        f"drawtext=fontfile={REGULAR}:textfile={df}:fontcolor={PALE}:fontsize=18:x=w-tw-30:y=17",
        # scrim so captions stay readable over a bright frame
        f"drawbox=x=0:y=ih-196:w=iw:h=196:color=black@0.42:t=fill",
        # topic plate slides in over the first half second, then holds
        f"drawtext=fontfile={BOLD}:textfile={tf}:fontcolor=white:fontsize=25"
        f":box=1:boxcolor={RED}@0.96:boxborderw=15"
        f":x='-560+min(t/0.5\\,1)*588':y=524",
    ]
    if hf:
        chrome.append(
            f"drawtext=fontfile={BOLD}:textfile={hf}:fontcolor=white:fontsize=26"
            f":x=32:y=572:alpha='min(max((t-0.55)/0.4\\,0)\\,1)'")
    if panel and credit:
        # Commons licences require attribution, so it is printed under the photo
        cf = _textfile(work, f"credit{variant}.txt", credit)
        chrome.append(
            f"drawtext=fontfile={REGULAR}:textfile={cf}:fontcolor=white@0.72"
            f":fontsize=13:box=1:boxcolor=black@0.45:boxborderw=5:x=64:y=364"
            f":alpha='min(max((t-0.8)/0.5\\,0)\\,1)'")
    if kf:
        # ticker scrolls at ~95 px/s — fast enough that whole-pixel steps are
        # invisible, which is exactly what a slow Ken Burns drift could not do
        chrome.append(f"drawbox=x=0:y=ih-32:w=iw:h=27:color={NAVY}@0.96:t=fill")
        chrome.append(
            f"drawtext=fontfile={REGULAR}:textfile={kf}:fontcolor={PALE}"
            f":fontsize=16:y=H-27:x='w-mod(t*95\\,w+tw)'")
    chrome.append("fade=t=in:st=0:d=0.35")        # soft cut into every scene

    chain = ",".join(chrome)
    subs = ""
    if srt and os.path.exists(srt) and os.path.getsize(srt) > 20:
        subs = f",subtitles='{srt}'"

    # the topic image rides in as an over-the-shoulder panel on the left,
    # which is the empty half of the anchor plate
    extra, panel_step = [], ""
    if panel and os.path.exists(panel):
        extra = ["-loop", "1", "-framerate", str(FPS), "-i", panel]
        panel_step = (f";[p][3:v]overlay=eval=frame"
                      f":x='-{PANEL_W + 20}+min(t/0.55\\,1)*({PANEL_W + 20}+62)'"
                      f":y=104:enable='gte(t,0.25)'[p2]")

    plain = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    for label, base in (("framed", framing(presenter, variant)), ("plain", plain)):
        stage = "[p2]" if panel_step else "[p]"
        graph = f"[0:v]{base}[bg];{bar}{panel_step};{stage}{chain}{subs}[v]"
        try:
            run(["ffmpeg", "-y", "-loglevel", "error",
                 "-loop", "1", "-framerate", str(FPS), "-i", presenter,
                 "-i", audio,
                 "-f", "lavfi", "-i",
                 f"color=c={RED}@0.9:s={W}x5:r={FPS}:d={seconds + 1}",
                 *extra,
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
