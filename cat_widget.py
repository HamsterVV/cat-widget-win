#!/usr/bin/env python3
"""cat-widget: a Windows desktop pet driven by SPEC v2.4.

Layout: cat body bbox window, overrideredirect + transparentcolor + topmost.
Behaviour: the cat walks continuously; a wall-clock scheduler described by
settings.json interrupts it with reminders and one-shot events.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import faulthandler
import json
import logging
import math
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageChops, ImageDraw, ImageTk

APP_DIR = Path(__file__).resolve().parent
# The embeddable interpreter ships a _pth file, so sys.path holds python-embed
# only and never the script directory: without this, `import pet_window` picks up
# a stale copy that happens to sit next to the interpreter.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
ACTIONS_PATH = APP_DIR / "actions.json"
SETTINGS_PATH = APP_DIR / "settings.json"
COLORKEY = "#010203"
FALLBACK_COLORKEY = "#FF00FF"
DEFER_SKIP_S = 120          # SPEC §5: defer up to 2 min, then drop
TICK_MS = 1000              # SPEC §5: global 1s wall-clock tick
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
RENDER_INTERVAL_MS = 33     # motion/animation clock, decoupled from sprite fps
MAX_FRAME_DT_S = 0.25       # a laptop wake must not teleport the cat across the screen
EXIT_GRACE_S = 120          # let the exit run finish before force-closing the app

LOG = logging.getLogger("cat-widget")
ACTION_LOG = logging.getLogger("cat-widget.actions")

FALLBACK_ACTION = "walk"    # the only continuous state; also the no-frames refuge
REMINDER_ACTIONS = ("drink",)
UI_FONT = "Microsoft YaHei UI"
FADE_S = 0.15               # short crossfade: softens the cut without a visible double image
DEFAULT_SETTINGS = {
    "height_px": 120,
    "schedules": [
        {
            "start": "13:40",
            "end": "12:00",
            "reminders": {"drink": 15},
            "bubble_text": {"drink": "该喝水啦"},
        },
    ],
    "events": [],
    "sleep_window": {"start": "22:00", "end": "07:00"},
    "transition_s": 0.15,
}


# --------------------------------------------------------------------------- #
# environment helpers
# --------------------------------------------------------------------------- #
def setup_logging() -> Path:
    log_dir = APP_DIR / "logs"
    try:
        log_dir.mkdir(exist_ok=True)
        probe = log_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        log_dir = Path(tempfile.gettempdir()) / "cat-widget-logs"
        log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_dir / "cat.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    if sys.stderr is not None:  # pythonw.exe has no stderr
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        LOG.addHandler(console)
    else:
        # pythonw discards stderr, which hides every exception raised inside the
        # window-procedure callback. Keep it in a file instead.
        sys.stderr = open(log_dir / "error.log", "a", buffering=1, encoding="utf-8")
    LOG.setLevel(logging.INFO)
    LOG.addHandler(file_handler)
    ACTION_LOG.setLevel(logging.INFO)
    action_handler = RotatingFileHandler(log_dir / "actions.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    action_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    ACTION_LOG.addHandler(action_handler)
    ACTION_LOG.propagate = False

    crash = open(log_dir / "crash.log", "a", encoding="utf-8")
    faulthandler.enable(file=crash)
    return log_dir


_DPI_AWARE = False


def set_dpi_aware() -> None:
    """Opt into per-monitor DPI awareness before any window exists.

    This is not cosmetic: a DPI-virtualised process has its layered-window
    coordinates scaled a second time by the compositor, which pushes the pet off
    the screen entirely. shcore's SetProcessDpiAwareness fails silently once an
    awareness context is already set, so the newer call is tried first and its
    result is checked.
    """
    global _DPI_AWARE
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARE = True
            return
    except Exception:  # noqa: BLE001 - only exists on Windows 10 1703+
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            _DPI_AWARE = True
            return
    except Exception:  # noqa: BLE001 - fall back to the legacy call
        pass
    try:
        _DPI_AWARE = bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:  # noqa: BLE001
        LOG.warning("DPI awareness could not be set")


def dpi_scale() -> float:
    if sys.platform != "win32":
        return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:  # noqa: BLE001
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return dpi / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def seconds_until(label: str) -> float:
    """Seconds from now until today's HH:MM wall-clock time; negative once passed."""
    try:
        hour, minute = (int(part) for part in label.split(":"))
    except ValueError:
        LOG.warning("malformed deadline %r; ignoring it", label)
        return -1.0
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return (target - now).total_seconds()


def work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the primary work area in pixels."""
    if sys.platform == "win32":
        rect = _RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


# --------------------------------------------------------------------------- #
# time + settings helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(text: str) -> int:
    match = TIME_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"invalid time: {text!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def in_window(minute: int, start: int, end: int) -> bool:
    """Half-open [start, end); end < start means the window crosses midnight."""
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def _minutes_of(seg: dict) -> set[int]:
    start = parse_hhmm(seg["start"])
    end = parse_hhmm(seg["end"])
    if start == end:
        return set()
    if start < end:
        return set(range(start, end))
    return set(range(start, 1440)) | set(range(0, end))


def validate_settings(data: dict, action_names: set[str]) -> list[str]:
    errors: list[str] = []
    height = data.get("height_px")
    if not isinstance(height, int) or not 60 <= height <= 600:
        errors.append("height_px 必须是 60-600 的整数")

    transition = data.get("transition_s", FADE_S)
    if isinstance(transition, bool) or not isinstance(transition, (int, float)) \
            or not 0 <= transition <= 1:
        errors.append("transition_s 必须是 0-1 的秒数")

    window = data.get("sleep_window")
    if window is not None:
        if not isinstance(window, dict):
            errors.append("sleep_window 必须是对象")
        else:
            try:
                sleep_start = parse_hhmm(window.get("start", ""))
                sleep_end = parse_hhmm(window.get("end", ""))
            except ValueError:
                errors.append("sleep_window 的起止时间必须是 HH:MM")
            else:
                if sleep_start == sleep_end:
                    errors.append("sleep_window 起止时间不能相同")

    timing = data.get("timing", {})
    if timing and not isinstance(timing, dict):
        errors.append("timing 必须是对象")
    elif isinstance(timing, dict):
        for name, values in timing.items():
            if name not in action_names:
                errors.append(f"timing 里有未知动作: {name}")
                continue
            if not isinstance(values, dict):
                errors.append(f"timing.{name} 必须是对象")
                continue
            durations = values.get("duration_s")
            if durations is not None:
                valid = (isinstance(durations, list) and len(durations) == 2
                         and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                 and 0.2 <= x <= 600 for x in durations)
                         and durations[0] <= durations[1])
                if not valid:
                    errors.append(f"timing.{name}.duration_s 必须是 [最短, 最长] 秒")
            weight = values.get("weight")
            if weight is not None and (isinstance(weight, bool) or not isinstance(weight, int)
                                       or not 0 <= weight <= 1000):
                errors.append(f"timing.{name}.weight 必须是 0-1000 的整数")

    schedules = data.get("schedules")
    if not isinstance(schedules, list):
        errors.append("schedules 必须是数组")
        schedules = []

    occupied: dict[int, int] = {}
    for index, seg in enumerate(schedules):
        where = f"时段 #{index + 1}"
        if not isinstance(seg, dict):
            errors.append(f"{where} 不是对象")
            continue
        try:
            start = parse_hhmm(seg.get("start", ""))
            end = parse_hhmm(seg.get("end", ""))
        except ValueError:
            errors.append(f"{where} 时间格式必须是 HH:MM")
            continue
        if start == end:
            errors.append(f"{where} 起止时间不能相同")
            continue
        for minute in _minutes_of(seg):
            if minute in occupied:
                errors.append(f"{where} 与时段 #{occupied[minute] + 1} 重叠")
                break
            occupied[minute] = index
        reminders = seg.get("reminders", {})
        if not isinstance(reminders, dict):
            errors.append(f"{where} reminders 必须是对象")
        else:
            for name, interval in reminders.items():
                if name not in REMINDER_ACTIONS:
                    errors.append(f"{where} 不支持的提醒动作: {name}")
                if not isinstance(interval, int) or interval < 1:
                    errors.append(f"{where} {name} 间隔必须是 >=1 的整数分钟")

    events = data.get("events", [])
    if not isinstance(events, list):
        errors.append("events 必须是数组")
        events = []
    for index, event in enumerate(events):
        where = f"事件 #{index + 1}"
        if not isinstance(event, dict):
            errors.append(f"{where} 不是对象")
            continue
        try:
            parse_hhmm(event.get("at", ""))
        except ValueError:
            errors.append(f"{where} at 时间格式必须是 HH:MM")
        action = event.get("action")
        if action not in action_names:
            errors.append(f"{where} 未知动作: {action}")
        duration = event.get("duration")
        if duration is not None and (not isinstance(duration, int) or duration <= 0):
            errors.append(f"{where} duration 必须是正整数秒")
    return errors


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return default, f"{path.name} 不存在，使用内置默认值"
    except (OSError, json.JSONDecodeError) as exc:
        return default, f"{path.name} 读取失败：{exc}"


# --------------------------------------------------------------------------- #
# scheduler (pure logic, no tkinter)
# --------------------------------------------------------------------------- #
class ScheduleEngine:
    """Wall-clock scheduler: reminders per segment + one-shot daily events."""

    def __init__(self, settings: dict, now_fn=datetime.now):
        self.now_fn = now_fn
        self.settings = settings
        self._segment_key = None
        self._due: dict[tuple[int, str], datetime] = {}
        self._fired: set[tuple[str, int]] = set()
        self._sleep_active = False

    def set_settings(self, settings: dict) -> None:
        self.settings = settings
        self._segment_key = None
        self._due = {}
        self._fired = set()
        self._sleep_active = False

    def current_segment(self, now: datetime) -> tuple[int | None, dict | None]:
        minute = now.hour * 60 + now.minute
        for index, seg in enumerate(self.settings.get("schedules", [])):
            try:
                if in_window(minute, parse_hhmm(seg["start"]), parse_hhmm(seg["end"])):
                    return index, seg
            except (KeyError, ValueError):
                continue
        return None, None

    def _segment_start(self, now: datetime, seg: dict) -> datetime:
        start = parse_hhmm(seg["start"])
        end = parse_hhmm(seg["end"])
        start_today = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if start <= end:
            return start_today
        if now.hour * 60 + now.minute >= start:
            return start_today
        return start_today - timedelta(days=1)

    def tick(self, now: datetime | None = None) -> list[dict]:
        now = now or self.now_fn()
        triggers: list[dict] = []
        index, seg = self.current_segment(now)

        if index != self._segment_key:
            self._segment_key = index
            self._due = {}
            if seg:
                seg_start = self._segment_start(now, seg)
                for action, interval in seg.get("reminders", {}).items():
                    due = seg_start
                    while due <= now:
                        due += timedelta(minutes=interval)
                    self._due[(index, action)] = due

        if seg:
            reminders = seg.get("reminders", {})
            for key in list(self._due):
                due = self._due[key]
                if now < due:
                    continue
                interval = reminders.get(key[1])
                if interval:
                    if now - due <= timedelta(seconds=DEFER_SKIP_S):
                        triggers.append({
                            "type": "reminder",
                            "action": key[1],
                            "bubble_text": seg.get("bubble_text", {}).get(key[1]),
                            "deadline": due + timedelta(seconds=DEFER_SKIP_S),
                        })
                    while self._due[key] <= now:
                        self._due[key] += timedelta(minutes=interval)

        window = self.settings.get("sleep_window") or {}
        try:
            sleep_start = parse_hhmm(window.get("start", ""))
            sleep_end = parse_hhmm(window.get("end", ""))
        except (AttributeError, ValueError):
            sleep_start = sleep_end = None
        if sleep_start is not None and sleep_start != sleep_end:
            inside = in_window(now.hour * 60 + now.minute, sleep_start, sleep_end)
            if not inside:
                self._sleep_active = False
            elif not self._sleep_active:
                # Entering the window (or starting inside it): sleep until it ends.
                self._sleep_active = True
                end_at = now.replace(hour=sleep_end // 60, minute=sleep_end % 60,
                                     second=0, microsecond=0)
                if end_at <= now:
                    end_at += timedelta(days=1)
                triggers.append({
                    "type": "event",
                    "action": "sleep",
                    "duration": max(60.0, (end_at - now).total_seconds()),
                    "bubble_text": "该休息啦",
                })

        today = now.date().isoformat()
        for event_index, event in enumerate(self.settings.get("events", [])):
            key = (today, event_index)
            if key in self._fired:
                continue
            try:
                at = parse_hhmm(event["at"])
            except (KeyError, ValueError):
                self._fired.add(key)
                continue
            event_at = now.replace(hour=at // 60, minute=at % 60, second=0, microsecond=0)
            if now < event_at:
                continue
            self._fired.add(key)
            if now - event_at <= timedelta(seconds=DEFER_SKIP_S):
                triggers.append({
                    "type": "event",
                    "action": event.get("action"),
                    "duration": event.get("duration"),
                    "bubble_text": event.get("bubble_text"),
                })
        return triggers


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #
# The sheets are cropped tight to the sprite, so every frame has a different
# width and the cat's body sits at a different x; drawing them centred makes the
# animal lurch sideways once per cycle.  Re-pad each frame onto one common
# canvas using the torso centroid as the horizontal anchor and the ground line
# as the vertical one -- the legs and tail are meant to move, the body is not.
ALIGN_BAND = (0.20, 0.55)   # content-height fraction holding head/back/flank


def _alpha_mask(image: Image.Image) -> Image.Image:
    return image.split()[3].point(lambda v: 255 if v > 200 else 0)


def _torso_centroid(mask: Image.Image, box) -> float | None:
    x0, y0, x1, y1 = box
    band = mask.crop((x0, y0 + int((y1 - y0) * ALIGN_BAND[0]), x1, y0 + int((y1 - y0) * ALIGN_BAND[1])))
    pixels = band.load()
    total = weight = 0
    for y in range(0, band.height, 2):
        for x in range(0, band.width, 2):
            if pixels[x, y]:
                total += 1
                weight += x
    return x0 + weight / total if total else None


def _align_frames(images: list[Image.Image]) -> list[Image.Image]:
    if len(images) < 2:
        return images
    masks = [_alpha_mask(image) for image in images]
    boxes = [mask.getbbox() for mask in masks]
    if any(box is None for box in boxes):
        return images
    anchors = [_torso_centroid(mask, box) for mask, box in zip(masks, boxes)]
    if any(anchor is None for anchor in anchors):
        return images
    placed_x = [-anchor for anchor in anchors]
    left = min(placed_x)
    right = max(px + image.width for px, image in zip(placed_x, images))
    height = max(image.height for image in images)
    padded = []
    for image, box, px in zip(images, boxes, placed_x):
        sheet = Image.new("RGBA", (int(round(right - left)), height))
        # Anchor on the paws, not the content box: frames that keep the same
        # canvas height (a cat lying down under a taller standing pose) then
        # share one ground line instead of being re-normalised per pose.
        sheet.paste(image, (int(round(px - left)), height - box[3]))
        padded.append(sheet)
    return padded


def ui_scale() -> float:
    """Physical pixels per Tk logical pixel (Windows display scaling).

    GetScaleFactorForDevice returns the percentage directly (100/125/150); this
    process is DPI-virtualised, so Tk's coordinates are smaller than the pixels
    UpdateLayeredWindow wants and every blit has to be scaled up.
    """
    if _DPI_AWARE:
        return 1.0          # coordinates are already physical pixels
    try:
        value = ctypes.windll.shcore.GetScaleFactorForDevice(0)
        if value and value > 0:
            return max(1.0, value / 100.0)
    except Exception:  # noqa: BLE001 - shcore is missing before Windows 8.1
        pass
    return 1.0


class FrameBank:
    def __init__(self, actions: dict, scale: float):
        self.actions = actions.get("actions", {})
        self.scale = scale
        self._cache: dict[tuple[str, int, str], list[ImageTk.PhotoImage]] = {}
        # The layered renderer needs the PIL pixels, not the Tk photo handles.
        self._raw: dict[tuple[str, int, str], list[Image.Image]] = {}

    def dir_for(self, action: str, direction: int) -> str:
        spec = self.actions.get(action, {})
        if direction > 0 and spec.get("mirror_dir"):
            return spec["mirror_dir"]
        return spec.get("frames_dir", f"assets/frames/{action}")

    def _load(self, directory: Path, display_h: int,
              flip: bool = False) -> tuple[list[ImageTk.PhotoImage], list[Image.Image]]:
        paths = sorted(directory.glob("*.png"))
        if not paths:
            return [], []
        images = []
        for path in paths:
            with Image.open(path) as image:
                frame = image.convert("RGBA")
            # Only walk ships a mirrored directory; every other action faces right
            # by flipping the same frames at load time.
            images.append(frame.transpose(Image.FLIP_LEFT_RIGHT) if flip else frame)
        try:
            images = _align_frames(images)
        except Exception:  # noqa: BLE001 - a bad sheet must not stop the pet
            LOG.exception("frame alignment failed for %s, using raw frames", directory.name)
        photos, raws = [], []
        for image in images:
            if image.height != display_h:
                width = max(1, round(image.width * display_h / image.height))
                image = image.resize((width, display_h), Image.LANCZOS)
            raws.append(image)
            photos.append(ImageTk.PhotoImage(image))
        return photos, raws

    def frames(self, action: str, direction: int, display_h: int) -> list[ImageTk.PhotoImage]:
        directory = self.dir_for(action, direction)
        key = (action, direction, directory)
        if key in self._cache:
            return self._cache[key]
        path = APP_DIR / directory
        flip = direction > 0 and not self.actions.get(action, {}).get("mirror_dir")
        photos, raws = self._load(path, display_h, flip) if path.is_dir() else ([], [])
        self._cache[key] = photos
        self._raw[key] = raws
        return photos

    def raw_frames(self, action: str, direction: int, display_h: int) -> list[Image.Image]:
        key = (action, direction, self.dir_for(action, direction))
        if key not in self._raw:
            self.frames(action, direction, display_h)
        return self._raw.get(key, [])


# --------------------------------------------------------------------------- #
# speech bubble
# --------------------------------------------------------------------------- #
class Bubble:
    def __init__(self, app: "PetApp"):
        self.app = app
        self.settings = app.actions.get("bubble", {})
        self.window: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self.text_id = None
        self._after_id = None
        self._font = tkfont.Font(
            family=self.settings.get("font_family", "Microsoft YaHei"),
            size=max(8, round(self.settings.get("font_size", 12) * app.scale)),
        )
        self.max_chars = int(self.settings.get("max_chars", 20))
        self.duration = float(self.settings.get("duration_s", 4))

    def show(self, text: str) -> None:
        if not text:
            return
        text = text[: self.max_chars]
        width = self._font.measure(text) + 24
        height = self._font.metrics("linespace") + 14
        if self.window is None or not self.window.winfo_exists():
            self.window = tk.Toplevel(self.app.root)
            self.window.overrideredirect(True)
            self.window.attributes("-topmost", True)
            try:
                self.window.attributes("-transparentcolor", COLORKEY)
            except tk.TclError:
                pass
            self.canvas = tk.Canvas(self.window, width=width, height=height, bg=COLORKEY, highlightthickness=0)
            self.canvas.pack()
        else:
            self.window.geometry(f"{width}x{height}")
            self.canvas.configure(width=width, height=height)
        self.canvas.delete("all")
        self._rounded_rect(1, 1, width - 1, height - 1, 8, fill="#FBF7EE", outline="#D9CDB8")
        self.text_id = self.canvas.create_text(width / 2, height / 2, text=text, font=self._font, fill="#3A3A3A")
        self.reposition()
        if self._after_id:
            self.app.root.after_cancel(self._after_id)
        self._after_id = self.app.root.after(int(self.duration * 1000), self.hide)

    def _rounded_rect(self, x1, y1, x2, y2, radius, **kwargs) -> None:
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, **kwargs)

    def reposition(self) -> None:
        if not self.window or not self.window.winfo_exists():
            return
        self.window.update_idletasks()
        bw = self.window.winfo_width()
        bh = self.window.winfo_height()
        cat_x, cat_y = self.app.window_xy()
        cat_w = self.app.window_width()
        left, top, right, bottom = self.app.work_area
        x = cat_x + (cat_w - bw) // 2
        x = max(left, min(right - bw, x))
        y = cat_y - bh - 6
        if y < top:
            y = cat_y + self.app.window_height() + 6
        self.window.geometry(f"+{x}+{y}")

    def hide(self) -> None:
        self._after_id = None
        if self.window and self.window.winfo_exists():
            self.window.destroy()
        self.window = None


# --------------------------------------------------------------------------- #
# settings window
# --------------------------------------------------------------------------- #
_ROUND_CACHE: dict[tuple, ImageTk.PhotoImage] = {}


def rounded_image(width: int, height: int, radius: int, fill: str,
                  border: str = "", border_width: int = 1) -> ImageTk.PhotoImage:
    """Anti-aliased rounded rectangle, drawn at 2x and downsampled.

    Tk has no rounded corners and a smoothed polygon bulges on the long edges,
    so the card and button backgrounds are rasterised once per size.
    """
    key = (width, height, radius, fill, border, border_width)
    cached = _ROUND_CACHE.get(key)
    if cached is not None:
        return cached
    scale = 2
    image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (border_width * scale, border_width * scale,
         width * scale - 1 - border_width * scale, height * scale - 1 - border_width * scale),
        radius=radius * scale, fill=fill,
        outline=border or None, width=border_width * scale if border else 0)
    image = image.resize((width, height), Image.LANCZOS)
    photo = ImageTk.PhotoImage(image)
    if len(_ROUND_CACHE) > 120:
        _ROUND_CACHE.clear()
    _ROUND_CACHE[key] = photo
    return photo


class _CuteCard(tk.Canvas):
    """A rounded white card: draws its own background and hosts a frame inside."""

    def __init__(self, parent, width: int, fill: str, border: str, radius: int = 18, pad: int = 14):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0,
                         width=width, height=80)
        self.fill, self.border, self.radius, self.pad = fill, border, radius, pad
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self._size = (0, 0)
        self.bind("<Configure>", self._layout)

    def _layout(self, _event=None) -> None:
        width = self.winfo_width()
        if width <= 1:
            return
        inner = max(1, width - 2 * self.pad)
        self.itemconfigure(self._win, width=inner)
        need = self.body.winfo_reqheight() + 2 * self.pad
        if abs(self.winfo_height() - need) > 1:
            self.configure(height=need)
        if self._size == (width, need):
            return
        self._size = (width, need)
        self.delete("bg")
        self._bg = rounded_image(width - 1, need - 1, self.radius, self.fill, self.border)
        self.create_image(0, 0, image=self._bg, anchor="nw", tags="bg")
        self.tag_lower("bg")


class _CuteSlider(tk.Canvas):
    """Round-thumb slider; ttk's own scale cannot be recoloured on Windows."""

    def __init__(self, parent, variable: tk.DoubleVar, low: float, high: float,
                 step: float = 1.0, bg: str = "#FFFFFF", accent: str = "#FF8A5B",
                 track: str = "#F7E6D8", height: int = 28):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, height=height,
                         cursor="hand2")
        self.variable, self.low, self.high, self.step = variable, low, high, step
        self.accent, self.track = accent, track
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._move)
        self.bind("<B1-Motion>", self._move)
        variable.trace_add("write", lambda *_: self._draw())

    def _move(self, event) -> str:
        pad = 12
        span = max(1, self.winfo_width() - 2 * pad)
        ratio = min(1.0, max(0.0, (event.x - pad) / span))
        value = self.low + ratio * (self.high - self.low)
        if self.step:
            value = round(value / self.step) * self.step
        self.variable.set(value)
        return "break"

    def _pill(self, x1: float, y1: float, x2: float, y2: float, fill: str) -> None:
        r = (y2 - y1) / 2
        points = [x1 + r, y1, x2 - r, y1, x2, y1 + r, x2, y2 - r,
                  x2 - r, y2, x1 + r, y2, x1, y2 - r, x1, y1 + r]
        self.create_polygon(points, smooth=True, fill=fill, outline="")

    def _draw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        pad = 12
        cy = self.winfo_height() / 2
        span = (self.high - self.low) or 1
        ratio = min(1.0, max(0.0, (float(self.variable.get()) - self.low) / span))
        x0, x1 = pad, width - pad
        thumb = x0 + ratio * (x1 - x0)
        self._pill(x0, cy - 5, x1, cy + 5, self.track)
        if thumb > x0 + 6:
            self._pill(x0, cy - 5, max(x0 + 12, thumb), cy + 5, self.accent)
        self.create_oval(thumb - 10, cy - 10, thumb + 10, cy + 10,
                         fill="#FFFFFF", outline=self.accent, width=2)
        self.create_oval(thumb - 3.5, cy - 3.5, thumb + 3.5, cy + 3.5,
                         fill=self.accent, outline="")


class _CuteButton(tk.Canvas):
    def __init__(self, parent, text: str, command, width: int, height: int,
                 fill: str, fg: str, font, border: str = "", hover: str = "",
                 bg: str = "#FFF7F0"):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, width=width,
                         height=height, cursor="hand2")
        self.text, self.command, self.fill, self.fg, self.font = text, command, fill, fg, font
        self.border, self.hover = border, hover or fill
        self._draw(self.fill)
        self.bind("<Button-1>", lambda _e: self.command())
        self.bind("<Enter>", lambda _e: self._draw(self.hover))
        self.bind("<Leave>", lambda _e: self._draw(self.fill))

    def _draw(self, fill: str) -> None:
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        self.delete("all")
        image = rounded_image(width - 1, height - 1, height // 2, fill,
                              self.border or fill, 1)
        self.create_image(0, 0, image=image, anchor="nw")
        self._bg = image
        self.create_text(width / 2, height / 2, text=self.text, fill=self.fg,
                         font=self.font)


class SettingsWindow:
    BG = "#FFF7F0"
    CARD = "#FFFFFF"
    LINE = "#F6E5D8"
    INK = "#5B4636"
    MUTED = "#A8907F"
    ACCENT = "#FF8A5B"
    ACCENT_DARK = "#F0743F"
    DANGER = "#E0533D"

    def __init__(self, app: "PetApp"):
        self.app = app
        self.window: tk.Toplevel | None = None
        self._s = 1.0

    def _font(self, size: int, bold: bool = False):
        return (UI_FONT, -round(size * self._s), "bold") if bold else (UI_FONT, -round(size * self._s))

    def _px(self, value: int) -> int:
        return round(value * self._s)

    def _row(self, parent, label: str) -> tk.Frame:
        row = tk.Frame(parent, bg=self.CARD)
        row.pack(fill="x", pady=self._px(5))
        tk.Label(row, text=label, bg=self.CARD, fg=self.INK, width=8, anchor="w",
                 font=self._font(12)).pack(side="left")
        return row

    def _spin(self, row, var, low: float, high: float, step: float = 1.0):
        ttk.Spinbox(row, from_=low, to=high, increment=step, textvariable=var,
                    width=5, justify="center", font=self._font(12)).pack(side="left")

    def _unit(self, row, text: str) -> None:
        tk.Label(row, text=text, bg=self.CARD, fg=self.MUTED, font=self._font(10)).pack(
            side="left", padx=self._px(6))

    def _scale_row(self, parent, label: str, var, low: float, high: float, fmt):
        row = self._row(parent, label)
        value = tk.Label(row, text=fmt(var.get()), bg=self.CARD, fg=self.INK, width=7,
                         anchor="e", font=self._font(12))
        _CuteSlider(row, var, low, high, step=1, bg=self.CARD, accent=self.ACCENT,
                    track="#F7E6D8", height=self._px(32)).pack(
            side="left", fill="x", expand=True, padx=(0, self._px(8)))
        value.pack(side="left")
        var.trace_add("write", lambda *_: value.config(text=fmt(float(var.get()))))

    # -- window ------------------------------------------------------------ #
    def open(self) -> None:
        if self.window and self.window.winfo_exists():
            self.window.lift()
            self.window.focus_force()
            return
        self._s = float(getattr(self.app, "scale", 1.0) or 1.0)
        settings = self.app.settings
        specs = self.app.actions.get("actions", {})

        def span(name: str, fallback) -> tuple[float, float]:
            value = specs.get(name, {}).get("duration_s", fallback)
            return float(value[0]), float(value[1])

        fmt = lambda value: ("%.1f" % value).rstrip("0").rstrip(".")
        sessions = settings.get("schedules") or [{}]
        reminders = sessions[0].get("reminders") or {}
        sleep_window = settings.get("sleep_window") or {}

        self.window = tk.Toplevel(self.app.root)
        self.window.title("🐾 橘猫设置")
        self.window.configure(bg=self.BG)
        self.window.attributes("-topmost", True)
        try:
            self.window.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        page = tk.Frame(self.window, bg=self.BG, padx=self._px(14), pady=self._px(12))
        page.pack(fill="both", expand=True)
        tk.Label(page, text="🐾 橘猫设置", bg=self.BG, fg=self.INK,
                 font=self._font(22, True)).pack(anchor="w")
        tk.Label(page, text="平时一直走动 · 点保存就关窗", bg=self.BG, fg=self.MUTED,
                 font=self._font(10)).pack(anchor="w", pady=(self._px(2), self._px(10)))

        card_w = self._px(470)

        habits = _CuteCard(page, card_w, self.CARD, self.LINE, self._px(18), self._px(14))
        habits.pack(fill="x", pady=(0, self._px(10)))
        head = tk.Frame(habits.body, bg=self.CARD)
        head.pack(fill="x", pady=(0, self._px(6)))
        tk.Label(head, text="日常", bg=self.CARD, fg=self.INK, font=self._font(13, True)).pack(side="left")
        tk.Label(head, text="喝水 · 舔爪 · 睡觉", bg=self.CARD, fg=self.MUTED,
                 font=self._font(10)).pack(side="left", padx=self._px(8))

        self.drink_every = tk.StringVar(value=str(reminders.get("drink", 15)))
        self.drink_sec = tk.StringVar(value=fmt(span("drink", [4.7, 4.7])[0]))
        row = self._row(habits.body, "☀️ 喝水")
        self._spin(row, self.drink_every, 1, 720, 1)
        self._unit(row, "分钟一次，每次")
        self._spin(row, self.drink_sec, 1, 60, 0.5)
        self._unit(row, "秒")

        self.lick_sec = tk.StringVar(value=fmt(span("lickpaw", [4.7, 4.7])[0]))
        row = self._row(habits.body, "🧡 舔爪")
        self._unit(row, "每次")
        self._spin(row, self.lick_sec, 1, 60, 0.5)
        self._unit(row, "秒")
        weight = int(specs.get("lickpaw", {}).get("weight", 20))
        self.weight_var = tk.DoubleVar(value=weight)
        self._scale_row(habits.body, "触发概率", self.weight_var, 0, 200,
                        lambda v: "%d%%" % round(v / (100 + v) * 100) if v > 0 else "关闭")

        self.sleep_from = tk.StringVar(value=str(sleep_window.get("start", "22:00")))
        self.sleep_to = tk.StringVar(value=str(sleep_window.get("end", "07:00")))
        row = self._row(habits.body, "🌙 睡觉")
        self._unit(row, "从")
        ttk.Entry(row, textvariable=self.sleep_from, width=6, justify="center",
                  font=self._font(12)).pack(side="left")
        self._unit(row, "到")
        ttk.Entry(row, textvariable=self.sleep_to, width=6, justify="center",
                  font=self._font(12)).pack(side="left")
        tk.Label(habits.body, text="这段时间里一直趴着睡，到点自己起来继续走", bg=self.CARD,
                 fg=self.MUTED, font=self._font(10)).pack(anchor="w", pady=(self._px(2), 0))

        look = _CuteCard(page, card_w, self.CARD, self.LINE, self._px(18), self._px(14))
        look.pack(fill="x", pady=(0, self._px(12)))
        head = tk.Frame(look.body, bg=self.CARD)
        head.pack(fill="x", pady=(0, self._px(6)))
        tk.Label(head, text="外观", bg=self.CARD, fg=self.INK, font=self._font(13, True)).pack(side="left")
        self.height_var = tk.DoubleVar(value=int(settings.get("height_px", 120)))
        self._scale_row(look.body, "大小", self.height_var, 80, 240, lambda v: "%d px" % round(v))
        self.fade_var = tk.DoubleVar(value=int(round(float(settings.get("transition_s", FADE_S)) * 1000)))
        self._scale_row(look.body, "切换淡入", self.fade_var, 0, 500, lambda v: "%d ms" % round(v))

        footer = tk.Frame(page, bg=self.BG)
        footer.pack(fill="x")
        tk.Button(footer, text="退出宠物", command=self.app.quit_app, bg=self.BG, fg=self.DANGER,
                  relief="flat", font=self._font(10), cursor="hand2", bd=0).pack(side="left")
        _CuteButton(footer, "保存并应用", self.save, self._px(132), self._px(40),
                    self.ACCENT, "#FFFFFF", self._font(12, True), hover=self.ACCENT_DARK,
                    bg=self.BG).pack(side="right")
        _CuteButton(footer, "取消", self.close, self._px(88), self._px(40),
                    "#FFFFFF", self.INK, self._font(12), border=self.LINE,
                    hover="#FFF1E7", bg=self.BG).pack(side="right", padx=self._px(8))

        self.status_var = tk.StringVar()
        self.status_label = tk.Label(page, textvariable=self.status_var, bg=self.BG, fg=self.DANGER,
                                     font=self._font(10), justify="left", wraplength=card_w)
        if self.app.settings_error:
            self._show_error(self.app.settings_error)

        self.window.update_idletasks()
        self.window.update()
        self._center()

    def _center(self) -> None:
        left, top, right, bottom = self.app.work_area
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = left + (right - left - width) // 2
        y = top + max(0, (bottom - top - height) // 3)
        self.window.geometry(f"+{x}+{y}")

    # -- actions ----------------------------------------------------------- #
    def _number(self, var, name: str, low: float, high: float) -> float:
        try:
            value = float(var.get().strip().replace("，", "."))
        except ValueError:
            raise ValueError(f"{name}需要填数字") from None
        if not low <= value <= high:
            raise ValueError(f"{name}应在 {low:g}–{high:g} 之间")
        return value

    def _collect(self) -> dict:
        every = int(round(self._number(self.drink_every, "喝水间隔", 1, 720)))
        drink = self._number(self.drink_sec, "喝水时长", 1, 60)
        lick = self._number(self.lick_sec, "舔爪时长", 1, 60)
        sleep_from = self.sleep_from.get().strip()
        sleep_to = self.sleep_to.get().strip()

        data = json.loads(json.dumps(self.app.settings))
        data["height_px"] = int(round(float(self.height_var.get())))
        data["transition_s"] = round(float(self.fade_var.get()) / 1000.0, 2)

        timing = data.get("timing") or {}
        timing.setdefault("drink", {})["duration_s"] = [drink, drink]
        timing.setdefault("lickpaw", {})["duration_s"] = [lick, lick]
        timing["lickpaw"]["weight"] = int(round(self.weight_var.get()))
        data["timing"] = timing

        sessions = data.get("schedules") or [{"start": "00:00", "end": "23:59"}]
        for segment in sessions:
            segment.setdefault("reminders", {})["drink"] = every
            segment["reminders"].pop("sleep", None)
        data["schedules"] = sessions

        data["sleep_window"] = {"start": sleep_from, "end": sleep_to}
        data["events"] = [event for event in data.get("events", [])
                          if event.get("action") != "sleep"]
        return data

    def _show_error(self, text: str) -> None:
        if not self.status_label.winfo_ismapped():
            self.status_label.pack(anchor="w", pady=(self._px(6), 0))
        self.status_var.set(text)

    def save(self) -> None:
        try:
            data = self._collect()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        errors = validate_settings(data, set(self.app.actions.get("actions", {})))
        if errors:
            self._show_error("；".join(errors))
            return
        self.app.save_settings(data)
        self.close()

    def close(self) -> None:
        if self.window and self.window.winfo_exists():
            self.window.destroy()
        self.window = None

# --------------------------------------------------------------------------- #
# the pet
# --------------------------------------------------------------------------- #
class PetApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.scale = dpi_scale()
        actions, actions_error = load_json(ACTIONS_PATH, {"actions": {}})
        if actions_error:
            LOG.error(actions_error)
        self.actions = actions
        self._action_defaults = json.loads(json.dumps(actions.get("actions", {})))
        render_spec = actions.get("render", {}) if isinstance(actions, dict) else {}
        self.renderer = str(render_spec.get("renderer", "colorkey"))
        self.petwin = None
        self._ui_scale = 1.0
        self._last_raw: Image.Image | None = None
        self._last_bob = 0
        self._fade_from: Image.Image | None = None
        self._fade_start = 0.0
        self._click_pending = False
        self.settings, self.settings_error = load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
        errors = validate_settings(self.settings, set(self.actions.get("actions", {})))
        if errors:
            self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
            self.settings_error = "；".join(errors)
            LOG.error("settings invalid, using defaults: %s", self.settings_error)
        self._apply_timing()

        self.engine = ScheduleEngine(self.settings)
        self.frames = FrameBank(self.actions, self.scale)
        self.bubble = Bubble(self)
        self.settings_window = SettingsWindow(self)

        self.display_h = max(24, round(int(self.settings.get("height_px", 180)) * self.scale))
        LOG.info("dpi aware=%s dpi_scale=%.2f ui_scale=%.2f display_h=%d",
                 _DPI_AWARE, self.scale, ui_scale(), self.display_h)
        self.work_area = work_area(root)
        self.colorkey = COLORKEY
        self._setup_window()

        self.action: str | None = None
        self.direction = -1                     # -1 faces left (original frames)
        self.frame_index = 0
        self.action_started = 0.0
        self.action_until = 0.0
        self._anim_t = 0.0                      # seconds into the current action
        self._last_render = time.monotonic()
        self._pos_x = 0.0                       # float window x; geometry only on change
        self._applied_x = 0
        self._win_w = 1
        self._win_y = 0
        self.dragging = False
        self.pending: list[dict] = []
        self.startup = bool(self.actions.get("startup", {}).get("walk_in", True))
        self.startup_target = None
        self.layout = self.actions.get("layout", {}) if isinstance(self.actions, dict) else {}
        self.drift_enabled = bool(self.layout.get("drift", False))
        self._exit_ts = time.monotonic() + seconds_until(self.layout.get("deadline", "17:00"))
        self._exit_margin = float(self.layout.get("exit_margin_s", 60))
        self._settings_mtime = SETTINGS_PATH.stat().st_mtime if SETTINGS_PATH.exists() else 0.0
        self._saving = False
        self._tick_count = 0
        self._fps_cap: int | None = None          # SPEC §6: CPU over budget -> 6fps
        self._cpu_prev: tuple[float, int] | None = None
        self._drawn_key = None                    # dirty-region: skip identical frames
        self._frames_missing = False
        self._closed = False

        self._bind_events()
        self._place_initial()
        if self.renderer == "petwin":
            try:
                from pet_window import LayeredPetWindow
                self._ui_scale = ui_scale()
                self.petwin = LayeredPetWindow(
                    self._ui_scale, self._applied_x, self._win_y,
                    self._win_w, self.display_h,
                    on_press=self._press_at, on_drag=self._drag_at,
                    on_release=self._release_at, on_right=self._right_clicked)
                LOG.info("layered pet window at %.2fx scaling", self._ui_scale)
            except Exception:  # noqa: BLE001 - never leave the user with nothing on screen
                LOG.exception("layered pet window unavailable; falling back to colour key")
                self.renderer = "colorkey"
                self.petwin = None
        self._log_action("startup", "init")
        self.root.after(50, self._render)
        self.root.after(TICK_MS, self._tick)

    # -- window ----------------------------------------------------------- #
    def _setup_window(self) -> None:
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if self.renderer == "petwin":
            # The sprite lives in its own layered window; this Tk root only hosts
            # the speech bubble and the settings dialog, so it stays hidden.
            self.colorkey = COLORKEY
            self.root.configure(bg=self.colorkey)
            self.root.withdraw()
        else:
            try:
                self.root.attributes("-transparentcolor", COLORKEY)
            except tk.TclError:
                self.colorkey = FALLBACK_COLORKEY
                try:
                    self.root.attributes("-transparentcolor", FALLBACK_COLORKEY)
                except tk.TclError:
                    LOG.warning("transparentcolor unavailable; using solid background")
                    self.colorkey = "#202020"
            self.root.configure(bg=self.colorkey)
        self.canvas = tk.Canvas(self.root, bg=self.colorkey, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._pet_item = self.canvas.create_image(0, 0, anchor="center")
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)

    def _place_initial(self) -> None:
        left, top, right, bottom = self.work_area
        self._win_w = max(1, self._max_frame_width(FALLBACK_ACTION))
        y = bottom - self.display_h
        if self.drift_enabled:
            # Start just inside the left edge and walk only rightwards until the
            # deadline, so the pet never shuttles back and forth across the screen.
            x = left + int((right - left) * float(self.layout.get("start_ratio", 0.08)))
            self.direction = 1
            self.startup = False
            self.startup_target = None
        else:
            x = left
            if self.startup:
                x = left - self._win_w
                self.direction = 1
                self.startup_target = left + int(
                    (right - left) * float(self.actions.get("startup", {}).get("end_x_ratio", 0.33)))
        self._place_window(self._win_w, self.display_h, x, y)
        self._win_y = y
        self._sync_position(x)

    def _max_frame_width(self, action: str) -> int:
        frames = self.frames.frames(action, -1, self.display_h)
        return max((frame.width() for frame in frames), default=120)

    def window_xy(self) -> tuple[int, int]:
        if self.petwin is not None:
            return self._applied_x, self._win_y
        return self.root.winfo_x(), self.root.winfo_y()

    def window_width(self) -> int:
        if self.petwin is not None:
            return self._win_w
        return max(1, self.root.winfo_width())

    def window_height(self) -> int:
        return max(1, self.root.winfo_height())

    # -- input ------------------------------------------------------------ #
    def _bind_events(self) -> None:
        self._press: tuple[int, int, int, int] | None = None

    def _press_at(self, x_root: int, y_root: int) -> None:
        origin_x, origin_y = self.window_xy()
        self._press = (x_root, y_root, origin_x, origin_y)

    def _drag_at(self, x_root: int, y_root: int) -> None:
        if not self._press:
            return
        dx = x_root - self._press[0]
        dy = y_root - self._press[1]
        threshold = int(self.actions.get("interaction", {}).get("drag_threshold_px", 8))
        if not self.dragging and abs(dx) + abs(dy) < threshold:
            return
        self.dragging = True
        self._move_to(self._press[2] + dx, self._press[3] + dy)

    def _release_at(self) -> None:
        if self.dragging:
            self.dragging = False
            self._clamp_visible()
        elif self._press is not None:
            # Only raise a flag here: doing the resize and the 55-frame load inside
            # the window-procedure callback kills the process.
            self._click_pending = True
        self._press = None

    def _look_at_user(self) -> None:
        """A plain left click: stop, turn the head to the screen, then walk on."""
        spec = self.actions.get("actions", {}).get("look")
        if not spec or self.action == "look":
            return
        self._play("look", reason="click")

    def _right_clicked(self) -> None:
        if self.dragging:  # SPEC #15 P1: ignore right-click while dragging
            return
        self.settings_window.open()

    def _on_press(self, event) -> None:
        self._press_at(event.x_root, event.y_root)

    def _on_motion(self, event) -> None:
        self._drag_at(event.x_root, event.y_root)

    def _on_release(self, _event) -> None:
        self._release_at()

    def _on_right_click(self, _event) -> None:
        self._right_clicked()

    def _move_to(self, x: int, y: int) -> None:
        left, top, right, bottom = self.work_area
        width, height = self._win_w, self.display_h
        min_visible = int(self.actions.get("interaction", {}).get("min_visible_px", 40))
        x = max(left - width + min_visible, min(right - min_visible, x))
        y = max(top - height + min_visible, min(bottom - min_visible, y))
        self._place_window(width, height, x, y)
        self._win_y = y
        self._sync_position(x)

    def _place_window(self, width: int, height: int, x: int, y: int) -> None:
        """Move (and, for Tk, size) the sprite whichever window owns it."""
        if self.petwin is not None:
            self.petwin.move(x, y)
        elif self.renderer != "petwin":
            self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _sync_position(self, x: int) -> None:
        """Re-anchor the float walk position after a drag or a resize."""
        self._pos_x = float(x)
        self._applied_x = x

    def _clamp_visible(self) -> None:
        origin_x, origin_y = self.window_xy()
        self._move_to(origin_x, origin_y)

    # -- scheduling ------------------------------------------------------- #
    def _tick(self) -> None:
        try:
            self._reload_if_changed()
            now = datetime.now()
            for trigger in self.engine.tick(now):
                self._handle_trigger(trigger, now)
            self._check_pending(now)
            self._tick_count += 1
            if self._tick_count % 30 == 0:
                percent = self._sample_cpu()
                if percent is not None:
                    if percent > 3.0 and self._fps_cap != 6:
                        LOG.warning("cpu %.1f%% > 3%%, capping fps at 6", percent)
                        self._fps_cap = 6
                    elif percent < 2.0 and self._fps_cap is not None:
                        LOG.info("cpu %.1f%%, restoring normal fps", percent)
                        self._fps_cap = None
        except Exception:  # noqa: BLE001 - never kill the pet
            LOG.exception("tick failed")
        if not self._closed:
            self.root.after(TICK_MS, self._tick)

    def _handle_trigger(self, trigger: dict, now: datetime) -> None:
        action = trigger.get("action")
        if not action or action not in self.actions.get("actions", {}):
            return
        duration = trigger.get("duration")
        text = trigger.get("bubble_text") or self.actions["actions"].get(action, {}).get("bubble_text")
        item = {"action": action, "duration": duration, "bubble_text": text,
                "deadline": trigger.get("deadline") or (now + timedelta(seconds=DEFER_SKIP_S))}
        if self._busy():
            self.pending.append(item)
            LOG.info("deferred %s until idle", action)
        else:
            self._play(action, duration=duration, bubble=text, reason="scheduled")

    def _check_pending(self, now: datetime) -> None:
        if not self.pending:
            return
        # SPEC §5: deferrals older than 2 minutes are dropped, never queued up
        self.pending = [item for item in self.pending
                        if item.get("deadline") is None or now <= item["deadline"]]
        if self._busy() or not self.pending:
            return
        item = self.pending.pop(0)
        self._play(item["action"], duration=item.get("duration"),
                   bubble=item.get("bubble_text"), reason="deferred")

    def _busy(self) -> bool:
        if self.dragging:
            return True
        if self.action is None:
            return False
        # walk is the permanent default, so it must never block a reminder
        spec = self.actions.get("actions", {}).get(self.action, {})
        return spec.get("mode") in ("reminder", "event", "interaction")

    # -- actions ---------------------------------------------------------- #
    def _play(self, action: str, duration: float | None = None,
              bubble: str | None = None, reason: str = "random") -> None:
        spec = self.actions.get("actions", {}).get(action, {})
        frames = self.frames.frames(action, self.direction, self.display_h)
        if not frames:
            if action != FALLBACK_ACTION:
                LOG.warning("no frames for %s, falling back to %s", action, FALLBACK_ACTION)
                self._play(FALLBACK_ACTION, duration=duration, bubble=None, reason="fallback")
            elif not self._frames_missing:
                LOG.error("no frames for %s either; nothing to show", FALLBACK_ACTION)
                self._frames_missing = True
            return
        if duration is None:
            low, high = spec.get("duration_s", [5, 10])
            duration = random.uniform(float(low), float(high))
        phase = self._cycle_phase(action)
        previous = self.action
        self.action = action
        now = time.monotonic()
        if previous is not None and previous != action and self._last_raw is not None:
            self._fade_from = self._last_raw
            self._fade_start = now
        else:
            self._fade_from = None
        self.action_started = now
        self.action_until = now + float(duration)
        self._anim_t = phase * len(frames) / self._sprite_fps()
        self.frame_index = int(self._anim_t * self._sprite_fps()) % len(frames) if len(frames) > 1 else 0
        self._resize_for(action)
        self._log_action(action, reason)
        if bubble:
            self.bubble.show(bubble)

    def _cycle_phase(self, next_action: str) -> float:
        """Where we are in the current cycle, so walk->run does not snap to frame 0."""
        if not self.action or self.frame_index <= 0:
            return 0.0
        if self.frames.dir_for(next_action, self.direction) != self.frames.dir_for(self.action, self.direction):
            return 0.0
        prev = self.frames.frames(self.action, self.direction, self.display_h)
        return (self.frame_index % len(prev)) / len(prev) if len(prev) > 1 else 0.0

    def _sprite_fps(self) -> float:
        spec = self.actions.get("actions", {}).get(self.action, {})
        fps = float(spec.get("fps", 8) or 8)
        if self._fps_cap:
            fps = min(fps, float(self._fps_cap))
        return max(1.0, fps)

    def _resize_for(self, action: str) -> None:
        frames = self.frames.frames(action, self.direction, self.display_h)
        width = max(1, max((frame.width() for frame in frames), default=self._win_w))
        # keep the body centred when the window changes width
        x = int(round(self._pos_x)) + (self._win_w - width) // 2
        self._win_w = width
        self._place_window(width, self.display_h, x, self._win_y)
        self._sync_position(x)

    def _random_action(self) -> None:
        pool = [(name, spec.get("weight", 0)) for name, spec in self.actions.get("actions", {}).items()
                if spec.get("mode") == "random" and spec.get("weight", 0) > 0]
        if not pool:
            self._play(FALLBACK_ACTION, reason="random-empty")
            return
        total = sum(weight for _, weight in pool)
        pick = random.uniform(0, total)
        upto = 0.0
        for name, weight in pool:
            upto += weight
            if pick <= upto:
                self._play(name, reason="random")
                return
        self._play(pool[-1][0], reason="random")

    # -- render loop ------------------------------------------------------ #
    def _render(self) -> None:
        if self._closed:
            return
        if self.petwin is not None and not self.petwin.pump():
            LOG.info("pet window closed")
            self.quit_app()
            return
        if self._click_pending:
            self._click_pending = False
            self._look_at_user()
        now = time.monotonic()
        dt = min(MAX_FRAME_DT_S, max(0.0, now - self._last_render))
        self._last_render = now
        try:
            if not self.dragging:
                self._advance(dt)
        except Exception:  # noqa: BLE001
            LOG.exception("render failed")
        if not self._closed:
            self.root.after(RENDER_INTERVAL_MS, self._render)

    def _advance(self, dt: float) -> None:
        now = time.monotonic()
        # The exit run starts at the deadline and takes a couple of seconds to
        # carry the pet off the right edge, so only force-close well after it.
        if self.drift_enabled and now >= self._exit_ts + EXIT_GRACE_S:
            LOG.info("past the deadline by %.0fs; closing", EXIT_GRACE_S)
            self.quit_app()
            return
        if self.action is None:
            if self.startup and self.startup_target is not None:
                self._play("walk", duration=3600, reason="startup-walk-in")
            else:
                self._random_action()
            return
        if now >= self.action_until:
            if self.action == "look":
                self._play(FALLBACK_ACTION, reason="after-look")
                return
            if not (self.startup and self.startup_target is not None):
                self._random_action()
                return
        frames = self._current_frames()
        if not frames:
            self._play(FALLBACK_ACTION, reason="no-frames")
            return
        self._anim_t += dt
        spec = self.actions.get("actions", {}).get(self.action, {})
        index = int(self._anim_t * self._sprite_fps())
        count = len(frames)
        tail = int(spec.get("tail_loop", 0) or 0)
        if spec.get("loop", True) and count > 1:
            self.frame_index = index % count
        elif index < count:
            self.frame_index = index
        elif 0 < tail < count:
            # A one-shot clip that ends in a resting pose keeps cycling its last
            # `tail` frames, so "lie down" ends in breathing instead of a freeze.
            self.frame_index = count - tail + (index - count) % tail
        else:
            # A one-shot clip (a cat stepping into a litter box) must not be replayed:
            # wrap only when the action is authored as a loop, otherwise hold the last pose.
            self.frame_index = count - 1
        image = frames[min(self.frame_index, len(frames) - 1)]
        raw = None
        if self.petwin is not None:
            raws = self.frames.raw_frames(self.action, self.direction, self.display_h)
            if self.frame_index < len(raws):
                raw = raws[self.frame_index]
        bob = self._body_bob(spec, len(frames))
        fade = 0.0
        if self._fade_from is not None and raw is not None:
            span = float(self.settings.get("transition_s", FADE_S))
            elapsed = now - self._fade_start
            if span <= 0 or elapsed >= span:
                self._fade_from = None
            else:
                fade = elapsed / span
        key = (self.action, self.direction, self.frame_index, self.display_h, self._win_w, bob,
               round(fade, 2))
        if key != self._drawn_key:  # dirty region: static frames are not redrawn
            self._draw(image, raw, bob, fade)
            self._drawn_key = key
        self._move_if_needed(dt)

    def _body_bob(self, spec: dict, frame_count: int) -> int:
        """Upward lift in pixels, peaking twice per stride.

        A walk cycle rises at each passing pose, so the body should lift and settle
        twice per loop. Generated frames usually keep the torso dead level, which is
        what makes them read as a shuffle rather than a walk; this supplies the
        missing weight shift without touching the artwork. The lift is never
        negative: dropping the sprite would push its paws outside the window.
        """
        amplitude = float(spec.get("bob_px", 0) or 0)
        if not amplitude or frame_count < 2:
            return 0
        phase = self.frame_index / frame_count
        # Cosine, not sine: a 4-frame cycle samples a sine exactly at its zero
        # crossings, which would make the lift a constant. This peaks on the
        # passing poses and bottoms out on the contacts.
        return round(amplitude * (0.5 - 0.5 * math.cos(4.0 * math.pi * phase)))

    def _draw(self, image: ImageTk.PhotoImage, raw: Image.Image | None = None,
              bob: int = 0, fade: float = 0.0) -> None:
        if self.petwin is not None and raw is not None:
            self.petwin.set_frame(raw, (self._win_w, self.display_h), bob,
                                  fade_from=self._fade_from, fade=fade)
            self._last_raw = raw
            self._last_bob = bob
            return
        # itemconfig instead of delete+create: swapping a canvas item does not
        # invalidate the whole colour-keyed window
        self.canvas.itemconfig(self._pet_item, image=image)
        self.canvas.coords(self._pet_item, self._win_w // 2, self.display_h // 2 - bob)
        self._keep_ref = image

    def _current_frames(self):
        return self.frames.frames(self.action, self.direction, self.display_h)

    def _walk_speed(self) -> float:
        spec = self.actions.get("actions", {}).get(self.action, {})
        per_loop = spec.get("move_px_per_loop", 0)
        if not per_loop:
            return 0.0
        frames = self.frames.frames(self.action, self.direction, self.display_h)
        # authored fps, not the CPU-capped one: throttling the legs must not
        # make the cat travel slower
        fps = float(spec.get("fps", 8) or 8)
        return float(per_loop) * fps / max(1, len(frames))

    def _drift_speed(self) -> float:
        """Pixels per second for a drift-mode walk.

        Recomputed every tick from the distance still to cover and the time still
        available, so a reminder that pauses the walk is absorbed instead of making
        the pet arrive late. It hands over to the exit run during the last stretch.
        """
        if not self.drift_enabled:
            return 0.0
        now = time.monotonic()
        left, _top, right, _bottom = self.work_area
        target = left + (right - left) * float(self.layout.get("end_ratio", 0.72))
        remaining = self._exit_ts - now - self._exit_margin
        if remaining <= 0:
            # Last stretch: aim past the right edge, so the cat walks out of the
            # screen and is gone by 17:00 instead of stopping at 72%.
            target = right + self._win_w
            remaining = max(0.5, self._exit_ts - now)
        return max(0.0, (target - self._pos_x) / remaining)

    def _move_if_needed(self, dt: float) -> None:
        # In drift mode the daily walk creeps rightwards; the exit run and the
        # reminders keep their own authored speeds.
        if self.drift_enabled and self.action == FALLBACK_ACTION:
            px_per_s = self._drift_speed()
        else:
            px_per_s = self._walk_speed()
        if not px_per_s:
            return
        left, top, right, bottom = self.work_area
        width = self._win_w
        # direction -1: faces/moves left (original frames); +1: faces/moves right (mirrored frames)
        self._pos_x += px_per_s * dt * self.direction
        x = int(round(self._pos_x))
        if self.drift_enabled:
            if x >= right:                      # walked off the right edge: day over
                LOG.info("reached the right edge; closing for the day")
                self.quit_app()
                return
        elif x <= left and self.direction < 0:
            self.direction = 1
            x = left
            self._pos_x = float(x)
        elif x + width >= right and self.direction > 0:
            self.direction = -1
            x = right - width
            self._pos_x = float(x)
        if x != self._applied_x:  # SetWindowPos is the expensive call, skip no-ops
            self._applied_x = x
            self._win_y = bottom - self.display_h
            self._place_window(self._win_w, self.display_h, x, self._win_y)
            if self.bubble.window is not None:
                self.bubble.reposition()
        if self.startup and self.startup_target is not None and x >= self.startup_target:
            self.startup = False
            self.startup_target = None
            self._random_action()

    def _log_action(self, action: str, reason: str) -> None:
        ACTION_LOG.info("action=%s reason=%s", action, reason)

    def _sample_cpu(self) -> float | None:
        """Process CPU percent normalised like Task Manager; None on non-Windows."""
        if sys.platform != "win32":
            return None
        try:
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            created = ctypes.c_ulonglong()
            exited = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            ok = ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            total = kernel.value + user.value  # 100ns units
            now = time.monotonic()
            if self._cpu_prev is None:
                self._cpu_prev = (now, total)
                return None
            prev_now, prev_total = self._cpu_prev
            self._cpu_prev = (now, total)
            elapsed = max(1e-6, now - prev_now)
            cores = max(1, os.cpu_count() or 1)
            return (total - prev_total) / 1e7 / elapsed * 100.0 / cores
        except Exception:  # noqa: BLE001
            return None

    # -- settings --------------------------------------------------------- #
    def _reload_if_changed(self) -> None:
        try:
            mtime = SETTINGS_PATH.stat().st_mtime
        except OSError:
            return
        if mtime <= self._settings_mtime or self._saving:
            return
        self._settings_mtime = mtime
        data, error = load_json(SETTINGS_PATH, None)
        if error or data is None:
            LOG.warning("settings reload failed: %s", error)
            return
        errors = validate_settings(data, set(self.actions.get("actions", {})))
        if errors:
            LOG.warning("settings reload rejected: %s", "；".join(errors))
            return
        self.apply_settings(data)

    def save_settings(self, data: dict) -> None:
        self._saving = True
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2)
            temp = SETTINGS_PATH.with_suffix(".json.tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, SETTINGS_PATH)
            self._settings_mtime = SETTINGS_PATH.stat().st_mtime
            self.apply_settings(data)
        finally:
            self._saving = False

    def _apply_timing(self) -> None:
        """Merge settings.timing over the shipped action defaults, in place.

        FrameBank keeps a reference to the actions dict, so the specs have to be
        mutated rather than replaced for a live change to reach the renderer.
        """
        current = self.actions.setdefault("actions", {})
        for name, base in self._action_defaults.items():
            spec = current.get(name)
            if not isinstance(spec, dict):
                spec = {}
                current[name] = spec
            spec.clear()
            spec.update(json.loads(json.dumps(base)))
        overrides = self.settings.get("timing") or {}
        for name, values in overrides.items():
            spec = current.get(name)
            if not isinstance(spec, dict) or not isinstance(values, dict):
                continue
            for key in ("duration_s", "weight"):
                if key in values:
                    spec[key] = values[key]

    def apply_settings(self, data: dict) -> None:
        self.settings = data
        self.settings_error = None
        self._apply_timing()
        self.engine.set_settings(data)
        new_height = max(24, round(int(data.get("height_px", 180)) * self.scale))
        if new_height != self.display_h:
            self.display_h = new_height
            self.frames._cache.clear()
            self._resize_for(self.action or FALLBACK_ACTION)
        LOG.info("settings applied")

    def quit_app(self) -> None:
        if self._closed:
            return
        self._closed = True
        LOG.info("exit requested")
        if self.petwin is not None:
            self.petwin.destroy()
            self.petwin = None
        self.root.destroy()


# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging()
    set_dpi_aware()
    actions, _ = load_json(ACTIONS_PATH, {"actions": {}})
    layout = actions.get("layout", {}) if isinstance(actions, dict) else {}
    if layout.get("drift"):
        remaining = seconds_until(layout.get("deadline", "17:00"))
        if remaining <= 0:
            LOG.info("already past %s; staying hidden until tomorrow", layout.get("deadline"))
            return 0
        LOG.info("drift plan: %.1f hours until %s", remaining / 3600, layout.get("deadline"))
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        LOG.error("tkinter could not start: %s", exc)
        return 1
    PetApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())