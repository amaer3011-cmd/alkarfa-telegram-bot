"""
utils/video_edit.py
=====================
Everything needed to put a watermark and/or a title on a video.

* Text is drawn with Pillow into a transparent PNG (Arabic is reshaped and
  reordered with `arabic-reshaper` + `python-bidi`, which works without
  libraqm / ffmpeg's drawtext + fribidi). ffmpeg then just overlays PNGs.
* Watermark: text or a logo image, 5 positions, 3 sizes, 3 opacities.
* Title: always written into the file's metadata ("title" tag). It can also
  be shown on screen for the first few seconds.
* Only a title in the metadata  -> stream copy (fast, no quality loss).
  Anything drawn on the picture -> the video is re-encoded to H.264 (mp4).

Re-encoding is CPU heavy, so at most `MAX_CONCURRENT_ENCODES` ffmpeg jobs
run at the same time.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

import config
from utils.integrity import IntegrityError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Choices offered in the settings menu
# --------------------------------------------------------------------- #
POSITIONS = ["br", "bl", "tr", "tl", "c"]
POSITION_LABELS = {
    "br": "↘️ تحت يمين",
    "bl": "↙️ تحت شمال",
    "tr": "↗️ فوق يمين",
    "tl": "↖️ فوق شمال",
    "c": "⏺ المنتصف",
}
SIZES = [12, 20, 30]  # watermark width as % of the video width
SIZE_LABELS = {12: "صغير", 20: "وسط", 30: "كبير"}
OPACITIES = [100, 70, 40]


def _next(options: list, current) -> object:
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


def next_position(current: str) -> str:
    return _next(POSITIONS, current)


def next_size(current: int) -> int:
    return _next(SIZES, current)


def next_opacity(current: int) -> int:
    return _next(OPACITIES, current)


class FFmpegMissing(RuntimeError):
    pass


# --------------------------------------------------------------------- #
# Specs
# --------------------------------------------------------------------- #
@dataclass
class WatermarkSpec:
    mode: str  # "text" | "image"
    text: Optional[str] = None
    image_path: Optional[str] = None
    position: str = "br"
    size_pct: int = 20
    opacity: int = 70

    def usable(self) -> bool:
        if self.mode == "image":
            return bool(self.image_path and os.path.exists(self.image_path))
        return bool(self.text and self.text.strip())


@dataclass
class VideoEditResult:
    output_path: str
    reencoded: bool
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------- #
# Text rendering (Pillow)
# --------------------------------------------------------------------- #
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabicUI-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_font_path_cache: Optional[str] = None
_font_warned = False


def _find_font() -> Optional[str]:
    global _font_path_cache
    if _font_path_cache is not None:
        return _font_path_cache or None
    for path in ([config.FONT_PATH] if config.FONT_PATH else []) + _FONT_CANDIDATES:
        if path and os.path.exists(path):
            _font_path_cache = path
            return path
    _font_path_cache = ""
    return None


def _font(px: int) -> ImageFont.FreeTypeFont:
    global _font_warned
    path = _find_font()
    if path:
        # BASIC layout on purpose: we reshape + reorder Arabic ourselves
        # (see _shape). With libraqm present Pillow would do it a second
        # time and the text would come out reversed.
        return ImageFont.truetype(path, px, layout_engine=ImageFont.Layout.BASIC)
    if not _font_warned:
        _font_warned = True
        logger.warning("No Arabic-capable font found; set FONT_PATH or install fonts-dejavu-core.")
    try:
        return ImageFont.load_default(px)  # Pillow >= 10.1 (scalable)
    except TypeError:
        return ImageFont.load_default()


# Emoji can't be drawn with the text fonts (they would show as empty boxes).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U0000200D\U000020E3]"
)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def _shape(text: str) -> str:
    """Make Arabic text drawable by Pillow's basic layout engine."""
    text = strip_emoji(text)
    try:
        import arabic_reshaper

        try:
            from bidi import get_display
        except ImportError:  # older python-bidi
            from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _bbox(draw: ImageDraw.ImageDraw, text: str, font, stroke: int) -> Tuple[int, int, int, int]:
    return draw.textbbox((0, 0), text, font=font, stroke_width=stroke)


def _apply_opacity(img: Image.Image, opacity: int) -> Image.Image:
    img = img.convert("RGBA")
    if opacity < 100:
        alpha = img.getchannel("A").point(lambda a: int(a * opacity / 100))
        img.putalpha(alpha)
    return img


def render_text_image(text: str, font_px: int = 128) -> Image.Image:
    """White text with a dark outline on a transparent canvas (multi-line)."""
    lines = [_shape(line) for line in text.split("\n") if line.strip()] or [" "]
    font = _font(font_px)
    stroke = max(2, int(font_px * 0.07))
    pad = stroke * 2
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    boxes = [_bbox(probe, line, font, stroke) for line in lines]
    widths = [b[2] - b[0] for b in boxes]
    heights = [b[3] - b[1] for b in boxes]
    gap = int(font_px * 0.15)
    canvas_w = max(widths) + 2 * pad
    canvas_h = sum(heights) + gap * (len(lines) - 1) + 2 * pad
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    y = pad
    for line, box, w, h in zip(lines, boxes, widths, heights):
        x = pad + (max(widths) - w) // 2
        draw.text(
            (x - box[0], y - box[1]), line, font=font,
            fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
        )
        y += h + gap
    return canvas


def _fit_width(img: Image.Image, target_w: int) -> Image.Image:
    target_w = max(16, target_w)
    ratio = target_w / img.width
    return img.resize((target_w, max(1, int(img.height * ratio))), Image.LANCZOS)


def build_watermark_image(spec: WatermarkSpec, video_w: int) -> Image.Image:
    """The watermark, already scaled for a video `video_w` pixels wide."""
    if spec.mode == "image":
        base = Image.open(spec.image_path).convert("RGBA")
    else:
        base = render_text_image(spec.text or "")
    img = _fit_width(base, int(video_w * spec.size_pct / 100))
    return _apply_opacity(img, spec.opacity)


def build_title_image(title: str, video_w: int) -> Image.Image:
    """White text on a translucent rounded box, wrapped to 80% of the width."""
    font_px = max(18, int(video_w * 0.045))
    font = _font(font_px)
    max_w = int(video_w * 0.8)
    pad = int(font_px * 0.6)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # Wrap the *logical* text first, then shape every line on its own.
    lines: List[str] = []
    for paragraph in title.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            w = _bbox(probe, _shape(candidate), font, 0)[2]
            if current and w > max_w - 2 * pad:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
    lines = [_shape(line) for line in lines[:4]] or [" "]  # at most 4 lines

    boxes = [_bbox(probe, line, font, 0) for line in lines]
    widths = [b[2] - b[0] for b in boxes]
    heights = [b[3] - b[1] for b in boxes]
    gap = int(font_px * 0.3)
    w = min(max(widths) + 2 * pad, video_w)
    h = sum(heights) + gap * (len(lines) - 1) + 2 * pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=pad, fill=(0, 0, 0, 165))
    y = pad
    for line, box, lw, lh in zip(lines, boxes, widths, heights):
        draw.text(((w - lw) // 2 - box[0], y - box[1]), line, font=font, fill=(255, 255, 255, 255))
        y += lh + gap
    return img


def place(position: str, W: int, H: int, w: int, h: int) -> Tuple[int, int]:
    """Top-left corner for an overlay of size (w, h) on a W x H frame."""
    m = max(8, int(W * 0.025))
    if position == "tl":
        return m, m
    if position == "tr":
        return W - w - m, m
    if position == "bl":
        return m, H - h - m
    if position == "c":
        return (W - w) // 2, (H - h) // 2
    return W - w - m, H - h - m  # "br"


def render_preview(spec: WatermarkSpec, out_path: str) -> None:
    """A fake 16:9 frame with the watermark on it (for the settings menu)."""
    W, H = 1280, 720
    frame = Image.new("RGB", (W, H))
    px = frame.load()
    for y in range(H):  # simple vertical gradient so light & dark marks are visible
        shade = 40 + int(110 * y / H)
        for x in range(0, W):
            px[x, y] = (shade, shade + 15, shade + 40)
    wm = build_watermark_image(spec, W)
    x, y = place(spec.position, W, H, wm.width, wm.height)
    frame.paste(wm, (x, y), wm)
    frame.save(out_path, "JPEG", quality=88)


def sanitize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.replace("\x00", "")).strip()[:200]


# --------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------- #
@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float  # seconds, 0 if unknown
    vcodec: str = ""
    acodec: Optional[str] = None  # None => the file has no audio


def _ffmpeg_exe() -> str:
    exe = shutil.which(config.FFMPEG_PATH)
    if not exe:
        raise FFmpegMissing("ffmpeg غير مثبّت على السيرفر")
    return exe


async def probe(path: str) -> Optional[VideoInfo]:
    """Size (after rotation), duration and codecs, parsed from `ffmpeg -i`."""
    proc = await asyncio.create_subprocess_exec(
        _ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    text = err.decode("utf-8", "replace")

    vline = next(
        (ln for ln in text.splitlines()
         if "Video:" in ln and "attached pic" not in ln
         and re.search(r"Video:.*?,\s*\d{2,5}x\d{2,5}[,\s\[]", ln)),
        None,
    )
    if vline is None:
        return None
    size = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})[,\s\[]", vline)
    w, h = int(size.group(1)), int(size.group(2))
    vcodec = (re.search(r"Video:\s*([A-Za-z0-9_]+)", vline) or [None, ""])[1]

    # Phone videos are stored sideways with a rotation flag.
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", text) or re.search(
        r"\brotate\s*:\s*(-?\d+)", text
    )
    if rot and round(abs(float(rot.group(1)))) % 180 == 90:
        w, h = h, w

    aline = next((ln for ln in text.splitlines() if "Audio:" in ln), None)
    acodec = (re.search(r"Audio:\s*([A-Za-z0-9_]+)", aline) or [None, None])[1] if aline else None

    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    seconds = (int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))) if dur else 0.0
    return VideoInfo(w, h, seconds, vcodec, acodec)


async def probe_safe(path: str) -> Optional[VideoInfo]:
    """probe() that never raises (used where the info is only a nice-to-have)."""
    try:
        return await probe(path)
    except Exception:
        return None


async def _decode_ok(path: str, *seek: str) -> bool:
    """Decode ~5 seconds of the file (from the start, or from the end with -sseof)."""
    proc = await asyncio.create_subprocess_exec(
        _ffmpeg_exe(), "-v", "error", "-xerror", "-nostdin", *seek, "-t", "5",
        "-i", path, "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(proc.wait(), timeout=300) == 0
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise


async def verify_video(
    path: str, expected_duration: float = 0.0, expect_audio: bool = False
) -> VideoInfo:
    """
    Make sure a produced video is sound: it has a picture (and sound if the
    source had it), the right length, and both its beginning and its END
    decode without errors (a truncated file fails at the end).
    Raises IntegrityError otherwise.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise IntegrityError("ملف الفيديو الناتج فاضي")
    info = await probe(path)
    if info is None:
        raise IntegrityError("الفيديو الناتج مفيهوش صورة قابلة للقراءة")
    if expect_audio and info.acodec is None:
        raise IntegrityError("الصوت اختفى من الفيديو الناتج")
    if expected_duration > 0 and info.duration > 0:
        if abs(info.duration - expected_duration) > max(2.0, expected_duration * 0.02):
            raise IntegrityError(
                f"مدة الفيديو الناتج ({info.duration:.0f}ث) غير مدة الأصلي ({expected_duration:.0f}ث)"
            )
    if not await _decode_ok(path):
        raise IntegrityError("بداية الفيديو الناتج فيها أخطاء")
    if info.duration > 12 and not await _decode_ok(path, "-sseof", "-6"):
        raise IntegrityError("نهاية الفيديو الناتج فيها أخطاء (ممكن يكون ناقص)")
    return info


async def make_thumbnail(path: str, dest: str, at: float = 1.0) -> bool:
    """A 320px JPEG frame (Telegram's thumbnail limits). Never raises."""
    try:
        for seek in (at, 0.0):
            proc = await asyncio.create_subprocess_exec(
                _ffmpeg_exe(), "-y", "-v", "error", "-nostdin", "-ss", str(seek), "-i", path,
                "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", dest,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return True
    except Exception:
        logger.warning("Could not make a video thumbnail", exc_info=True)
    return False


ProgressCb = Callable[[float, float], Awaitable[None]]

_encode_sem: Optional[asyncio.Semaphore] = None


def encode_semaphore() -> asyncio.Semaphore:
    global _encode_sem
    if _encode_sem is None:
        _encode_sem = asyncio.Semaphore(max(1, config.MAX_CONCURRENT_ENCODES))
    return _encode_sem


async def _run(cmd: List[str], total: float, progress: Optional[ProgressCb]) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    err_tail = bytearray()

    async def read_err() -> None:
        while chunk := await proc.stderr.read(4096):
            err_tail.extend(chunk)
            del err_tail[:-4000]

    async def read_out() -> None:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if progress and total > 0 and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000  # both keys are microseconds
                except ValueError:
                    continue
                await progress(max(0.0, min(done, total)), total)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_out(), read_err(), proc.wait()), timeout=config.VIDEO_TIMEOUT
        )
    except BaseException:  # timeout or task cancelled: never leave ffmpeg running
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode, err_tail.decode("utf-8", "replace")


def _unlink(*paths: Optional[str]) -> None:
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                logger.warning("Could not remove %s", p)


async def edit_video(
    src: str,
    *,
    watermark: Optional[WatermarkSpec] = None,
    title: Optional[str] = None,
    title_on_screen: bool = False,
    progress: Optional[ProgressCb] = None,
) -> VideoEditResult:
    """
    Apply the watermark and/or title. Returns the new file (the source is
    left untouched — the caller deletes it). The result is verified before it
    is returned (see verify_video); raises on any failure so the caller can
    fall back to the untouched original.
    """
    exe = _ffmpeg_exe()
    title = sanitize_title(title) if title else None
    if watermark is not None and not watermark.usable():
        watermark = None
    if watermark is None and not title:
        return VideoEditResult(src, False)

    stem, ext = os.path.splitext(src)
    notes: List[str] = []
    draw_wm = watermark is not None
    draw_title = bool(title) and title_on_screen
    temp_pngs: List[str] = []
    stamp = f"{os.getpid()}_{int(time.time() * 1000)}"

    base = [exe, "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    meta = ["-metadata", f"title={title}"] if title else []

    info = await probe(src)
    if info is None:
        raise RuntimeError("مقدرتش أقرا الفيديو (مفيهوش صورة قابلة للقراءة)")
    has_audio = info.acodec is not None

    # ---------------- metadata-only: remux, no re-encode ----------------
    if not (draw_wm or draw_title):
        out = f"{stem}.titled{ext}"
        flags = ["-movflags", "+faststart"] if ext.lower() in (".mp4", ".m4v", ".mov") else []
        # Keep subtitles when the container accepts them; otherwise retry without.
        code, err = 1, ""
        try:
            for maps in (["-map", "0:v", "-map", "0:a?", "-map", "0:s?"], ["-map", "0:v", "-map", "0:a?"]):
                _unlink(out)
                cmd = base + ["-i", src, *maps, "-c", "copy", *meta, *flags, out]
                code, err = await _run(cmd, 0, None)
                if code == 0:
                    break
            if code != 0:
                raise RuntimeError(f"ffmpeg: {err.strip()[-300:]}")
            await verify_video(out, info.duration, has_audio)
        except BaseException:
            _unlink(out)
            raise
        notes.append("🏷 اتحط العنوان في بيانات الفيديو")
        return VideoEditResult(out, False, notes)

    # ---------------- overlays: re-encode to H.264 / mp4 ----------------
    loop = asyncio.get_running_loop()
    inputs: List[str] = []
    steps: List[str] = []
    current = "[0:v:0]"
    idx = 1
    out = f"{stem}.edited.mp4"
    try:
        if draw_wm:
            img = await loop.run_in_executor(None, build_watermark_image, watermark, info.width)
            path = os.path.join(config.DOWNLOAD_DIR, f"wm_{stamp}.png")
            img.save(path)
            temp_pngs.append(path)
            x, y = place(watermark.position, info.width, info.height, img.width, img.height)
            inputs += ["-i", path]
            steps.append(f"{current}[{idx}:v]overlay=x={x}:y={y}[v{idx}]")
            current, idx = f"[v{idx}]", idx + 1
        if draw_title:
            img = await loop.run_in_executor(None, build_title_image, title, info.width)
            path = os.path.join(config.DOWNLOAD_DIR, f"title_{stamp}.png")
            img.save(path)
            temp_pngs.append(path)
            x = (info.width - img.width) // 2
            y = int(info.height * 0.06)
            secs = config.TITLE_OVERLAY_SECONDS
            inputs += ["-i", path]
            steps.append(
                f"{current}[{idx}:v]overlay=x={x}:y={y}:enable='between(t,0,{secs})'[v{idx}]"
            )
            current, idx = f"[v{idx}]", idx + 1
        steps.append(f"{current}scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[vout]")

        common = (
            base + ["-i", src, *inputs, "-filter_complex", ";".join(steps),
                    "-map", "[vout]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", config.VIDEO_PRESET, "-crf", str(config.VIDEO_CRF),
                    "-max_muxing_queue_size", "4096"]
        )
        tail = [*meta, "-movflags", "+faststart", out]
        # Only AAC is copied untouched. Anything else (ac3, dts, opus, mp3 ...)
        # is either rejected by mp4 or plays without sound in Telegram's
        # players, so it is converted to AAC.
        audio_modes = [["-c:a", "aac", "-b:a", "160k"]]
        if info.acodec in (None, "aac"):
            audio_modes.insert(0, ["-c:a", "copy"])
        code, err = 1, ""
        async with encode_semaphore():
            for audio in audio_modes:
                _unlink(out)
                code, err = await _run(common + audio + tail, info.duration, progress)
                if code == 0:
                    break
        if code != 0:
            raise RuntimeError(f"ffmpeg: {err.strip()[-300:]}")
        await verify_video(out, info.duration, has_audio)
    except BaseException:
        _unlink(out)
        raise
    finally:
        _unlink(*temp_pngs)

    if draw_wm:
        notes.append("💧 اتحطت العلامة المائية")
    if title:
        notes.append("🏷 اتحط العنوان" + (" (على الشاشة وفي بيانات الفيديو)" if draw_title else " في بيانات الفيديو"))
    return VideoEditResult(out, True, notes)
