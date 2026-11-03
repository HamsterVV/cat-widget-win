"""Constants, validation and process-level helpers (DPI, logging, paths)."""
from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
import re
import sys
import tempfile
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import tkinter as tk

APP_DIR = Path(__file__).resolve().parent.parent   # project root, not the package
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


def is_dpi_aware() -> bool:
    return _DPI_AWARE


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
