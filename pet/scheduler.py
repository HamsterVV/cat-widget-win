"""Wall-clock scheduling: reminders inside a time window plus one-shot events."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import DEFER_SKIP_S, in_window, parse_hhmm

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
