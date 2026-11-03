#!/usr/bin/env python3
"""cat-widget: a Windows desktop pet driven by actions.json and settings.json.

    python cat_widget.py
"""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# The embeddable interpreter ships a _pth file, so sys.path holds python-embed
# only and never the script directory.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pet.app import PetApp  # noqa: E402
from pet.config import (  # noqa: E402
    ACTIONS_PATH,
    LOG,
    load_json,
    seconds_until,
    set_dpi_aware,
    setup_logging,
)


def main() -> int:
    setup_logging()
    set_dpi_aware()
    actions, _ = load_json(ACTIONS_PATH, {"actions": {}})
    layout = actions.get("layout", {}) if isinstance(actions, dict) else {}
    deadline = str(layout.get("deadline") or "").strip()
    if layout.get("drift"):
        if not deadline:
            LOG.info("no deadline configured; the pet stays visible")
        else:
            remaining = seconds_until(deadline)
            if remaining <= 0:
                LOG.info("already past %s; staying hidden until tomorrow", deadline)
                return 0
            LOG.info("drift plan: %.1f hours until %s", remaining / 3600, deadline)
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
