"""Frame loading, torso-centroid alignment and the sprite cache."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageTk

from .config import APP_DIR, LOG

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
