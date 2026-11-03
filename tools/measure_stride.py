"""Measure the stride a sprite cycle actually implies.

    python measure_stride.py <frames_dir>

Foot slide is the real reason a walk reads as "marching on the spot": the legs
complete a cycle while the window barely moves. The fix is to set
`move_px_per_loop` to the distance the artwork itself depicts, and that distance
is measurable. A paw that is planted does not move through the world, so between
two consecutive frames it slides backwards across the body by exactly however far
the body advanced. Summing that slide over the cycle gives the true stride.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PIL import Image

PAW_BAND = 0.16      # bottom fraction of the frame that can be a grounded paw
PAW_GAP = 25         # px of empty columns that separate one paw from the next
MATCH_RADIUS = 60    # px: how far a paw may move between frames and still be "same paw"


def _anchor_and_paws(image: Image.Image) -> tuple[float, list[float]]:
    """Torso centroid x (the body anchor `_align_frames` uses) plus grounded paw centres."""
    align = importlib.util.spec_from_file_location("cw", Path(__file__).with_name("cat_widget.py"))
    module = importlib.util.module_from_spec(align)
    align.loader.exec_module(module)
    mask = module._alpha_mask(image)
    box = mask.getbbox()
    anchor = module._torso_centroid(mask, box)
    if anchor is None:
        raise SystemExit(f"{image.filename}: no torso pixels to anchor on")

    pixels = mask.load()
    top = box[3] - int((box[3] - box[1]) * PAW_BAND)
    live = [
        x for x in range(box[0], box[2])
        if any(pixels[x, y] for y in range(top, box[3]))
    ]
    paws: list[float] = []
    run: list[int] = []
    for x in live + [None]:                      # sentinel flushes the last run
        if run and (x is None or x - run[-1] > PAW_GAP):
            paws.append(sum(run) / len(run))
            run = []
        if x is not None:
            run.append(x)
    return anchor - box[0], [p - box[0] for p in paws]


def _slide(previous: list[float], current: list[float]) -> float | None:
    """Backward travel of a paw that stayed on the ground, relative to the body.

    Pairs each paw with its nearest counterpart one-to-one, then reports the
    median slide. Taking the smallest delta instead would bias the estimate toward
    zero: with four paws on screen there is always some pair that barely moved,
    even when every real stance paw travelled a long way.
    """
    pairs = sorted(
        (
            (old - new, old, new)
            for old in previous for new in current
            if 0 <= old - new <= MATCH_RADIUS
        ),
    )
    used_old: set[float] = set()
    used_new: set[float] = set()
    matched: list[float] = []
    for delta, old, new in pairs:
        if old in used_old or new in used_new:
            continue
        used_old.add(old)
        used_new.add(new)
        matched.append(delta)
    if not matched:
        return None
    matched.sort()
    return matched[len(matched) // 2]


def main(directory: Path) -> int:
    frames = sorted(directory.glob("*.png"))
    if len(frames) < 2:
        print(f"{directory}: needs at least 2 frames")
        return 1
    readings = [_anchor_and_paws(Image.open(path).convert("RGBA")) for path in frames]

    print(f"{'frame':<16}{'paws (body-relative x)':<30}")
    for path, (_, paws) in zip(frames, readings):
        print(f"{path.name:<16}{', '.join(f'{p:.0f}' for p in paws):<30}")

    slides = []
    for index in range(len(readings)):
        _, previous = readings[index]
        _, current = readings[(index + 1) % len(readings)]
        value = _slide(previous, current)
        slides.append(value)
        print(f"  {frames[index].name} -> {frames[(index + 1) % len(frames)].name}: "
              f"{'no planted paw tracked' if value is None else f'{value:.1f} px'}")

    tracked = [s for s in slides if s is not None]
    if not tracked:
        print("stride: indeterminate (no paw stayed grounded between frames)")
        return 1
    # Untracked steps are gaps in the artwork, not zero travel: average over what
    # was measurable and scale up to a full cycle.
    per_step = sum(tracked) / len(tracked)
    print(f"\nmeasured stride per cycle ~= {per_step * len(frames):.0f} px "
          f"({len(frames)} frames, {len(tracked)} tracked steps, mean {per_step:.1f} px/step)")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
