"""Extract frames from a generated clip with headless Chrome.

No ffmpeg on this machine, so the page seeks the video to an exact time and a
headless screenshot of the canvas becomes the PNG. One browser launch per frame:
a 60-frame grid in a single launch stalls on media decode, but a fresh page
seeking once is reliable.

    python capture_frames.py --video assets/frames_raw/drink.mp4 --out assets/frames_staging/drink_frames --fps 10
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CANVAS = (1280, 691)          # what the walk clip was captured at; keeps the same scale
PAGE = """<!doctype html>
<meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#000;overflow:hidden}}canvas{{display:block}}</style>
<canvas id="c" width="{w}" height="{h}"></canvas>
<script>
const t = parseFloat(new URLSearchParams(location.search).get('t') || '0');
const c = document.getElementById('c'), ctx = c.getContext('2d');
const v = document.createElement('video');
v.muted = true; v.playsInline = true; v.preload = 'auto';
v.src = 'file:///{src}';
v.addEventListener('loadedmetadata', () => {{ v.currentTime = Math.min(Math.max(t, 0.001), Math.max(0.001, v.duration - 0.05)); }});
v.addEventListener('seeked', () => {{ ctx.drawImage(v, 0, 0, c.width, c.height); }});
</script>
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--start", type=float, default=0.0)
    args = parser.parse_args()

    video = args.video.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="catframes-"))   # ASCII path: file:// breaks on the Chinese project path
    try:
        shutil.copy2(video, work / "src.mp4")
        page = work / "capture.html"
        page.write_text(PAGE.format(w=CANVAS[0], h=CANVAS[1], src=(work / "src.mp4").as_posix()),
                        encoding="utf-8")
        count = max(1, round((args.duration - args.start) * args.fps))
        last = None
        for index in range(count):
            at = args.start + index / args.fps
            raw_shot = work / f"{index:02d}.png"      # Chrome only ever sees ASCII paths
            shot = args.out / f"{args.video.stem}_{index:02d}.png"
            subprocess.run([
                str(CHROME), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={CANVAS[0]},{CANVAS[1]}",
                f"--screenshot={raw_shot}",
                "--virtual-time-budget=12000",
                f"file:///{page.as_posix()}?t={at:.3f}",
            ], check=True, capture_output=True, timeout=90)
            if not raw_shot.exists():
                print(f"missing screenshot at t={at:.2f}", file=sys.stderr)
                return 1
            shutil.move(raw_shot, shot)
            current = digest(shot)
            if current == last:
                shot.unlink()          # clamp at the end of the clip: drop the repeat
                print(f"duplicate frame at t={at:.2f}, trimmed")
                break
            last = current
        kept = len(list(args.out.glob("*.png")))
        print(f"{args.video.name}: {kept} frames -> {args.out}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())