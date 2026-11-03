"""Turn flat-background ImageGen sprites into cut-out animation frames.

    python ingest_frames.py <out_dir> <image> [<image> ...] [--strip N] [--height 384]

Pass `--strip N` when one image holds N poses side by side: the sheet is keyed
once and split at the gaps between poses.

The generator is asked for a solid magenta backdrop so this script can recover a
clean alpha matte: it keys the backdrop, drops detached blobs (the corner
watermark), neutralises magenta colour spill on the rim, then resamples every
result to one height. Runtime frame alignment handles registration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

CELL = 8  # px per connected-component cell; watermark glyphs are far from the body


def _column_projection(distance: Image.Image, cut: int) -> list[int]:
    """Mean coverage per column of `distance` thresholded at `cut`, cropped to content."""
    mask = distance.point(lambda v, c=cut: 255 if v > c else 0)
    box = mask.getbbox()
    if box is None:
        return []
    projection = mask.crop(box).resize((box[2] - box[0], 1), Image.BOX)
    return list(projection.get_flattened_data() if hasattr(projection, "get_flattened_data")
                else projection.getdata())


def _matte(distance: Image.Image, cut: int) -> tuple[Image.Image, Image.Image]:
    """(hard mask, ramped alpha) for a chosen threshold."""
    hard = distance.point(lambda v: 255 if v > cut else 0)
    alpha = distance.point(
        lambda v: 255 if v > cut else (v * 255) // cut if v > cut // 3 else 0
    )
    return hard, alpha


def _background_level(distance: Image.Image) -> int:
    """Backdrop noise floor, read from the border.

    The median rather than a high percentile: a zoomed clip can put the subject
    over a large share of the border, and the 95th percentile then reports the
    cat as "backdrop noise" and keys the whole frame away.
    """
    w, h = distance.size
    band = max(4, min(w, h) // 30)
    edges = (
        list(distance.crop((0, 0, w, band)).get_flattened_data())
        + list(distance.crop((0, h - band, w, h)).get_flattened_data())
        + list(distance.crop((0, 0, band, h)).get_flattened_data())
        + list(distance.crop((w - band, 0, w, h)).get_flattened_data())
    )
    edges.sort()
    return edges[len(edges) // 2] if edges else 10


def _valley_boundaries(profile: list[float], expect: int) -> list[int]:
    """Split a strip's column profile into `expect` poses at its deepest valleys.

    Splitting on *empty* columns needs a keying threshold high enough to break the
    fur bridges between neighbours, and on a low-contrast backdrop that threshold
    also eats the cat's pale head, paws and tail. Valleys work at the low threshold
    that keeps the whole animal, so segmentation and keying stop fighting.
    """
    width = len(profile)
    if expect < 2 or width < expect * 8:
        return []
    radius = max(2, width // (expect * 12))
    smoothed = [
        sum(profile[max(0, i - radius):i + radius + 1]) / len(profile[max(0, i - radius):i + radius + 1])
        for i in range(width)
    ]
    minima = [
        (i, smoothed[i]) for i in range(1, width - 1)
        if smoothed[i] <= smoothed[i - 1] and smoothed[i] <= smoothed[i + 1]
    ]
    minima.sort(key=lambda pair: pair[1])
    spacing = width // (expect * 2)
    chosen: list[int] = []
    for index, _ in minima:
        if all(abs(index - taken) >= spacing for taken in chosen):
            chosen.append(index)
        if len(chosen) == expect - 1:
            return sorted(chosen)
    return []  # not enough separable poses: the strip is unusable


def _distance(rgb: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    """How far each pixel sits from the backdrop in chroma rather than brightness.

    A plain RGB distance fails on a washed-out backdrop: ginger fur and a rose
    backdrop can be 80 apart in total colour yet 100 apart in blue-minus-green,
    which is the axis that actually distinguishes them. Comparing each channel
    against green therefore keys far more reliably, and it makes a pale or dark
    watermark collapse into the backdrop instead of surviving as a speck.
    """
    red, green, blue = rgb.split()

    def offset(channel: Image.Image, amount: int) -> Image.Image:
        return channel.point(lambda v: min(255, max(0, v + amount)))

    drift_r = ImageChops.difference(red, offset(green, key[0] - key[1]))
    drift_b = ImageChops.difference(blue, offset(green, key[2] - key[1]))
    return ImageChops.lighter(drift_r, drift_b)


def _dominant_corner_color(image: Image.Image) -> tuple[int, int, int]:
    """The backdrop colour, as the most common colour on the frame border.

    Six fixed corner samples break as soon as the subject reaches into a corner
    (a zoomed-in clip puts an ear on the top-left pixel), so count a border band
    and take its mode instead.
    """
    rgb = image.convert("RGB")
    w, h = rgb.size
    band = max(2, min(w, h) // 100)
    bands = (rgb.crop((0, 0, w, band)), rgb.crop((0, h - band, w, h)),
             rgb.crop((0, 0, band, h)), rgb.crop((w - band, 0, w, h)))
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for strip in bands:
        for colour in strip.get_flattened_data():
            key = (colour[0] >> 3, colour[1] >> 3, colour[2] >> 3)
            slot = buckets.get(key)
            if slot is None:
                buckets[key] = [1, colour[0], colour[1], colour[2]]
            else:
                slot[0] += 1
                slot[1] += colour[0]
                slot[2] += colour[1]
                slot[3] += colour[2]
    if not buckets:
        raise SystemExit("empty frame")
    count, r, g, b = max(buckets.values(), key=lambda value: value[0])
    return (r // count, g // count, b // count)


def _largest_blob_mask(mask: Image.Image) -> Image.Image:
    """Keep only the connected component a downscaled grid says is biggest."""
    w, h = mask.size
    cols, rows = max(1, w // CELL), max(1, h // CELL)
    grid = mask.resize((cols, rows), Image.BOX).point(lambda v: 1 if v > 24 else 0)
    cells = list(grid.get_flattened_data()) if hasattr(grid, "get_flattened_data") else list(grid.getdata())
    active = [i for i in range(cols * rows) if cells[i]]
    parent = {i: i for i in active}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for y in range(rows):
        for x in range(cols):
            i = y * cols + x
            if not cells[i]:
                continue
            if x + 1 < cols and cells[i + 1]:
                union(i, i + 1)
            if y + 1 < rows and cells[i + cols]:
                union(i, i + cols)

    sizes: dict[int, int] = {}
    for i in active:
        root = find(i)
        sizes[root] = sizes.get(root, 0) + 1
    if not sizes:
        return mask
    winner = max(sizes, key=sizes.get)

    keep = Image.new("L", (cols, rows), 0)
    keep.putdata([255 if cells[i] and find(i) == winner else 0 for i in range(cols * rows)])
    # Grow back to full size and pad a cell so thin limbs are not clipped.
    keep = keep.resize((w, h), Image.NEAREST).filter(ImageFilter.MaxFilter(CELL * 3 + 1))
    return ImageChops.multiply(mask, keep)


def _despill(rgb: Image.Image, alpha: Image.Image, key: tuple[int, int, int]) -> Image.Image:
    """Pull the backdrop's own chroma out of every pixel, rim included.

    The rim of a keyed sprite is a blend of fur and backdrop, so it keeps the
    backdrop's hue. Over a dark desktop that is invisible; over a light one it
    shows up as coloured noise along the silhouette. Projecting each pixel onto
    the backdrop's chroma direction and clipping that projection to whatever the
    solid interior already shows removes the tint without touching fur colour.
    """
    key_mean = sum(key) / 3.0
    direction = [channel - key_mean for channel in key]
    length = sum(component * component for component in direction) ** 0.5
    if length < 1e-6:
        return rgb
    direction = [component / length for component in direction]

    inner = alpha.filter(ImageFilter.MinFilter(5))
    pixels, inner_pixels = rgb.load(), inner.load()
    width, height = rgb.size

    samples: list[float] = []
    for y in range(0, height, 2):
        for x in range(0, width, 2):
            if inner_pixels[x, y] > 200:
                r, g, b = pixels[x, y]
                mean = (r + g + b) / 3.0
                samples.append((r - mean) * direction[0] + (g - mean) * direction[1]
                               + (b - mean) * direction[2])
    if not samples:
        return rgb
    samples.sort()
    cap = samples[int(len(samples) * 0.95)]

    out = rgb.copy()
    target = out.load()
    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            mean = (r + g + b) / 3.0
            projection = (r - mean) * direction[0] + (g - mean) * direction[1] \
                + (b - mean) * direction[2]
            if projection <= cap:
                continue
            excess = projection - cap
            target[x, y] = tuple(
                max(0, min(255, round(channel - excess * component)))
                for channel, component in zip((r, g, b), direction)
            )
    return out


def _defringe(rgb: Image.Image, alpha: Image.Image, radius: int = 3) -> Image.Image:
    """Replace contaminated rim colours with the median of the solid interior."""
    solid = alpha.point(lambda v: 255 if v >= 250 else 0)
    if solid.getbbox() is None:
        return rgb
    window = 2 * radius + 1
    # Paint the outside with the interior's own median first: a plain median
    # filter would otherwise average in the backdrop colour it is meant to remove.
    interior = _solid_mean(rgb, alpha)
    backdrop = tuple(round(v) for v in interior) if interior else (128, 128, 128)
    padded = Image.composite(rgb, Image.new("RGB", rgb.size, backdrop),
                             solid)
    repaired = padded.filter(ImageFilter.MedianFilter(window))
    rim = ImageChops.subtract(solid, solid.filter(ImageFilter.MinFilter(window)))
    return Image.composite(repaired, rgb, rim)


def _solid_mean(rgb: Image.Image, alpha: Image.Image) -> list[float] | None:
    pixels, alpha_pixels = rgb.load(), alpha.load()
    total = [0.0, 0.0, 0.0]
    count = 0
    for y in range(0, rgb.height, 2):
        for x in range(0, rgb.width, 2):
            if alpha_pixels[x, y] > 200:
                r, g, b = pixels[x, y]
                total[0] += r
                total[1] += g
                total[2] += b
                count += 1
    return [v / count for v in total] if count else None


def match_color(frame: Image.Image, reference: Image.Image) -> Image.Image:
    """Scale each channel of `frame` so its fur averages the reference's average."""
    source = _solid_mean(frame.convert("RGB"), frame.split()[3])
    target = _solid_mean(reference.convert("RGB"), reference.split()[3])
    if not source or not target:
        return frame
    red, green, blue, alpha = frame.split()
    scaled = [
        channel.point(lambda v, ratio=target[i] / source[i]: min(255, round(v * ratio)))
        for i, channel in enumerate((red, green, blue))
    ]
    return Image.merge("RGBA", (*scaled, alpha))


def _keyed(source: Path, cut: int) -> tuple[Image.Image, Image.Image, Image.Image]:
    """The source's RGB plus its hard mask and ramped alpha, all cropped to the subject."""
    rgb = Image.open(source).convert("RGB")
    hard, alpha = _matte(_distance(rgb, _dominant_corner_color(rgb)), cut)
    box = hard.getbbox()
    if box is None:
        raise SystemExit(f"{source}: nothing left after keying the backdrop")
    return rgb.crop(box), hard.crop(box), alpha.crop(box)


def _fill_small_holes(alpha: Image.Image, max_area: int) -> Image.Image:
    """Close enclosed background specks without touching real gaps like legs.

    A transparentcolor window composites alpha literally, so a single stray
    transparent pixel inside the fur shows the desktop through the cat.
    """
    w, h = alpha.size
    data = bytearray(alpha.tobytes())
    seen = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            if data[i] or seen[i]:
                continue
            # Gather this enclosed background region, noting whether it escapes
            # to the border (then it is real background, not a hole).
            stack = [i]
            seen[i] = 1
            region = []
            escapes = False
            while stack:
                j = stack.pop()
                region.append(j)
                cx, cy = j % w, j // w
                for n in (j - 1, j + 1, j - w, j + w):
                    if cx == 0 and n == j - 1 or cx == w - 1 and n == j + 1:
                        continue
                    if n < 0 or n >= w * h or cy == 0 and n == j - w or cy == h - 1 and n == j + w:
                        escapes = True
                        continue
                    if data[n] or seen[n]:
                        continue
                    seen[n] = 1
                    stack.append(n)
            if not escapes and len(region) <= max_area:
                for j in region:
                    data[j] = 255
    return Image.frombytes("L", (w, h), bytes(data))


def _opaque(alpha: Image.Image) -> Image.Image:
    """Hard silhouette: the window keys on colour, so partial alpha is a defect."""
    solid = alpha.point(lambda v: 255 if v >= 128 else 0)
    if solid.getbbox() is None:
        return solid
    body = sum(1 for v in solid.getdata() if v)
    return _fill_small_holes(solid, max(400, body // 500))


def _drop_backdrop_hue(rgb: Image.Image, alpha: Image.Image, key: tuple[int, int, int],
                       tolerance: int = 40, min_saturation: int = 110) -> Image.Image:
    """Blank pixels that are unmistakably the backdrop's own hue.

    Chroma distance is brightness-normalised, so a shadowed patch of backdrop —
    the gap between two tucked legs, say — can drift *further* from the key than
    the fur does and survive as an opaque coloured blob inside the body. Hue is
    immune to that: the backdrop keeps its hue under shadow, the fur keeps its
    own, and a saturated pixel of the backdrop's hue is the backdrop.
    """
    hsv = rgb.convert("HSV")
    hue, saturation, _ = hsv.split()
    key_hue = Image.new("RGB", (1, 1), key).convert("HSV").getpixel((0, 0))[0]
    hue_delta = ImageChops.difference(hue, Image.new("L", rgb.size, key_hue)).point(
        lambda d: min(d, 256 - d)  # hue is circular
    )
    matches = ImageChops.multiply(
        hue_delta.point(lambda d: 255 if d <= tolerance else 0),
        saturation.point(lambda s: 255 if s >= min_saturation else 0),
    )
    return ImageChops.subtract(alpha, matches)


def _bleed_alpha(rgb: Image.Image, alpha: Image.Image, passes: int = 6) -> Image.Image:
    """Copy fur colour outwards into the transparent region, a pixel at a time.

    A colour-key window ignores alpha, so whatever RGB sits under the transparent
    pixels is never drawn — until something resizes the sprite. Every downscale
    blends across the silhouette, and if the outside is dark grey that blend turns
    into a dark rim that only shows up on a light desktop. Pushing the interior's
    own colour outwards makes those blends land on fur colour instead.
    """
    width, height = rgb.size
    pixels = [tuple(px) for px in rgb.getdata()]
    cover = [1 if value > 127 else 0 for value in alpha.getdata()]
    for _ in range(passes):
        filled = list(pixels)
        grown = cover[:]
        for y in range(height):
            row = y * width
            for x in range(width):
                i = row + x
                if cover[i]:
                    continue
                total = [0, 0, 0]
                count = 0
                for j, inside in ((i - 1, x > 0), (i + 1, x < width - 1),
                                  (i - width, y > 0), (i + width, y < height - 1)):
                    if inside and cover[j]:
                        colour = pixels[j]
                        total[0] += colour[0]
                        total[1] += colour[1]
                        total[2] += colour[2]
                        count += 1
                if count:
                    filled[i] = tuple(round(v / count) for v in total)
                    grown[i] = 1
        pixels, cover = filled, grown
    out = rgb.copy()
    out.putdata(pixels)
    return out


def _finish(rgb: Image.Image, hard: Image.Image, alpha: Image.Image,
            box: tuple[int, int, int, int], height: int,
            key: tuple[int, int, int] | None = None,
            factor: float | None = None) -> Image.Image:
    """Cut one pose out of an already-keyed sheet and normalise it to `height`.

    `factor` overrides the per-pose normalisation with one scale for the whole
    clip, which is what keeps a sitting or lying cat from being blown up to the
    height of a standing one.
    """
    keep = _largest_blob_mask(hard.crop(box))
    masked = ImageChops.multiply(alpha.crop(box), keep)
    trimmed = masked.getbbox()
    if trimmed is None:
        return None
    window = (box[0] + trimmed[0], box[1] + trimmed[1], box[0] + trimmed[2], box[1] + trimmed[3])
    # Crop the masked alpha, not the raw one: a neighbouring pose's tail tip can
    # fall inside this pose's bounding window and would otherwise survive as a
    # floating chunk on the desktop.
    alpha = _opaque(masked.crop(trimmed))
    body_rgb = rgb.crop(window)
    if key is not None:
        alpha = _drop_backdrop_hue(body_rgb, alpha, key)
    body = body_rgb if key is None else _despill(body_rgb, alpha, key)
    body = _defringe(body, alpha)
    # Repair colour before resampling: LANCZOS would otherwise drag backdrop colour inward.
    factor = factor if factor else height / alpha.height
    target_h = max(1, round(alpha.height * factor))
    resized = lambda image: image.resize(
        (max(1, round(image.width * factor)), target_h), Image.LANCZOS)
    final_alpha = resized(alpha).point(lambda v: 255 if v >= 128 else 0)
    # Bleed last: every later downscale then blends fur with fur, not with leftover
    # backdrop colour, which is what produced the dark rim on a light desktop.
    final_rgb = _bleed_alpha(resized(body), final_alpha)
    return Image.merge("RGBA", (*final_rgb.split(), final_alpha))


def _subject_height(source: Path) -> int:
    """Height of the keyed subject in a raw frame, for calibrating a global scale."""
    rgb = Image.open(source).convert("RGB")
    distance = _distance(rgb, _dominant_corner_color(rgb))
    cut = max(4 * _background_level(distance), 30)
    box = _matte(distance, cut)[0].getbbox()
    if box is None:
        raise SystemExit(f"{source}: nothing left after keying the backdrop")
    return box[3] - box[1]


def _pad_canvas(frame: Image.Image, canvas_height: int) -> Image.Image:
    """Put the frame on a taller canvas, paws on the bottom edge.

    Every action then shares one ground line and one display height, so a cat
    lying down keeps its true size instead of being scaled up to fill the window.
    """
    if frame.height >= canvas_height:
        return frame
    sheet = Image.new("RGBA", (frame.width, canvas_height), (0, 0, 0, 0))
    sheet.paste(frame, (0, canvas_height - frame.height))
    return sheet


def convert(source: Path, height: int, factor: float | None = None) -> Image.Image:
    rgb = Image.open(source).convert("RGB")
    key = _dominant_corner_color(rgb)
    distance = _distance(rgb, key)
    cut = max(4 * _background_level(distance), 30)
    keyed = _keyed(source, cut)
    frame = _finish(*keyed, (0, 0, keyed[1].width, keyed[1].height), height, key, factor)
    if frame is None:
        raise SystemExit(f"{source}: the subject was one disconnected speck")
    return frame


def convert_strip(source: Path, height: int, expect: int) -> list[Image.Image]:
    """Slice a horizontal sprite strip at the gaps between poses."""
    rgb = Image.open(source).convert("RGB")
    key = _dominant_corner_color(rgb)
    distance = _distance(rgb, key)
    key_cut = max(4 * _background_level(distance), 30)
    hard, alpha = _matte(distance, key_cut)
    box = hard.getbbox()
    if box is None:
        print(f"{source.name}: nothing left after keying the backdrop", file=sys.stderr)
        return []
    keyed = (rgb.crop(box), hard.crop(box), alpha.crop(box))
    profile = _column_projection(distance, key_cut)
    valleys = _valley_boundaries(profile, expect)
    if not valleys:
        print(f"{source.name}: cannot find {expect} separable poses at keying cut {key_cut}; "
              f"regenerate with a flatter, higher-contrast backdrop", file=sys.stderr)
        return []
    edges = [0] + valleys + [keyed[1].width]
    frames = []
    for left, right in zip(edges, edges[1:]):
        pose = keyed[1].crop((left, 0, right, keyed[1].height)).getbbox()
        if pose is None or pose[3] - pose[1] < keyed[1].height // 4:
            continue  # a watermark glyph, not a pose
        frame = _finish(*keyed, (left, pose[1], right, pose[3]), height, key)
        if frame is not None:
            frames.append(frame)
    if len(frames) != expect:
        print(f"{source.name}: produced {len(frames)} poses, expected {expect}", file=sys.stderr)
    return frames


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("images", type=Path, nargs="+")
    parser.add_argument("--height", type=int, default=384)
    parser.add_argument("--prefix", default="frame")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--mirror-to", type=Path, default=None)
    parser.add_argument("--strip", type=int, default=0,
                        help="treat each input as one horizontal strip and slice it into N poses")
    parser.add_argument("--match-color-to", type=Path, default=None,
                        help="an existing frame whose fur colour every output should match")
    parser.add_argument("--scale-ref", type=Path, default=None,
                        help="raw frame whose subject height defines one scale for every input")
    parser.add_argument("--canvas-height", type=int, default=0,
                        help="pad every output to this height, bottom-aligned")
    args = parser.parse_args(argv)

    factor = args.height / _subject_height(args.scale_ref) if args.scale_ref else None

    reference = Image.open(args.match_color_to).convert("RGBA") if args.match_color_to else None
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.mirror_to:
        args.mirror_to.mkdir(parents=True, exist_ok=True)

    number = args.start
    for source in args.images:
        frames = (convert_strip(source, args.height, args.strip) if args.strip
                  else [convert(source, args.height, factor)])
        for frame in frames:
            if reference is not None:
                frame = match_color(frame, reference)
            if args.canvas_height:
                frame = _pad_canvas(frame, args.canvas_height)
            name = f"{args.prefix}_{number:02d}.png"
            number += 1
            frame.save(args.out_dir / name)
            if args.mirror_to:
                frame.transpose(Image.FLIP_LEFT_RIGHT).save(args.mirror_to / name)
            print(f"{source.name} -> {args.out_dir / name} {frame.size}")


if __name__ == "__main__":
    main(sys.argv[1:])
