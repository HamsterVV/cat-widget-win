"""Speech bubble, the cute widgets and the settings window."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from .config import COLORKEY, FADE_S, UI_FONT, validate_settings

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
