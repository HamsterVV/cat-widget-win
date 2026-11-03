"""The pet itself: action state machine, render clock, movement, interaction."""
from __future__ import annotations

import ctypes
import json
import math
import os
import random
import sys
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageTk

from .config import (
    ACTION_LOG,
    ACTIONS_PATH,
    COLORKEY,
    DEFAULT_SETTINGS,
    DEFER_SKIP_S,
    EXIT_GRACE_S,
    FADE_S,
    FALLBACK_ACTION,
    FALLBACK_COLORKEY,
    LOG,
    MAX_FRAME_DT_S,
    RENDER_INTERVAL_MS,
    SETTINGS_PATH,
    TICK_MS,
    TIME_RE,
    dpi_scale,
    is_dpi_aware,
    load_json,
    seconds_until,
    ui_scale,
    validate_settings,
    work_area,
)
from .frames import FrameBank
from .scheduler import ScheduleEngine
from .ui import Bubble, SettingsWindow

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
                 is_dpi_aware(), self.scale, ui_scale(), self.display_h)
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
        deadline = str(self.layout.get("deadline") or "").strip()
        self._deadline = deadline or None
        self._exit_ts = time.monotonic() + seconds_until(deadline) if self._deadline else None
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
                from .window import LayeredPetWindow
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
        if self.drift_enabled and self._exit_ts is not None and now >= self._exit_ts + EXIT_GRACE_S:
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
        if self._exit_ts is None:
            # No daily deadline: ease towards the end position and stay on screen.
            return max(0.0, (target - self._pos_x)
                       / float(self.layout.get("drift_time_constant_s", 21600)))
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
            if self._exit_ts is not None and x >= right:   # walked out: day over
                LOG.info("reached the right edge; closing for the day")
                self.quit_app()
                return
            if self._exit_ts is None and x + width > right:
                x = right - width
                self._pos_x = float(x)
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
