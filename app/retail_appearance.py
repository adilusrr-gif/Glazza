"""Coarse garment colors from Frigate person crops, for route-candidate review."""
from io import BytesIO

from PIL import Image


MAX_IMAGE_BYTES = 2 * 1024 * 1024
# Each garment has eight circular hue bins and three achromatic brightness bins.
_BINS = 11
_BAND_ROWS = ((0.32, 0.49), (0.57, 0.75))


def _person_bounds(box, width, height):
    # Frigate's completed-event data.box is [left, top, width, height] on its clean snapshot.
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1.000001 for v in box):
        return None
    x, y, w, h = box
    if not (0 <= x < x + w <= 1.000001 and 0 < y < y + h < 1):
        return None
    left, top = round(x * width), round(y * height)
    right, bottom = min(width, round((x + w) * width)), round((y + h) * height)
    if top <= 0 or bottom >= height or right-left < 20 or bottom-top < 48:
        return None
    if not 0.16 <= (right-left) / (bottom-top) <= 1.05:
        return None
    return left, top, right, bottom


def live_crop_box(box, frame_width, frame_height):
    """Map a normalized Frigate 0.18.2 best-frame box into its live crop=1 JPEG.

    get_snapshot_bytes uses a square with a 300-pixel floor and clamps its origin
    to the frame. Numpy slicing also clips its far edge on narrow/short sources.
    Frigate preserves that cropped aspect ratio when resizing the HTTP response.
    """
    if (type(frame_width) is not int or type(frame_height) is not int
            or not 0 < frame_width <= 4096 or not 0 < frame_height <= 4096
            or frame_width * frame_height > 3840 * 2160):
        return None
    bounds = _person_bounds(box, frame_width, frame_height)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    side = max(300, int(max(width, height) * 1.1 // 4) * 4)
    x = max(0, min(int((left + right - side) / 2), frame_width - side))
    y = max(0, min(int((top + bottom - side) / 2), frame_height - side))
    crop_width = min(side, frame_width - x)
    crop_height = min(side, frame_height - y)
    return [(left - x) / crop_width, (top - y) / crop_height, width / crop_width, height / crop_height]


def _color_band(image, columns, rows):
    width, height = image.size
    region = (round(width * columns[0]), round(height * rows[0]),
              round(width * columns[1]), round(height * rows[1]))
    patch = image.crop(region).resize((16, 16), Image.Resampling.BOX).convert('HSV')
    histogram = [0.] * _BINS
    for hue, saturation, value in patch.get_flattened_data():
        if saturation >= 52 and value >= 40:
            position = hue / 32
            first = int(position)
            second = (first + 1) % 8
        else:
            # Interpolate between dark, middle and light. A small exposure change
            # should not change every pixel's bin on a neutral shirt.
            position = value * 2 / 255
            first = 8 + int(position)
            second = min(first + 1, _BINS - 1)
        fraction = position % 1
        histogram[first] += 1 - fraction
        histogram[second] += fraction
    # Percentages sum to exactly 100 after rounding with largest remainders.
    percentages = [n * 100 / 256 for n in histogram]
    counts = [int(n) for n in percentages]
    remainder = 100 - sum(counts)
    for i in sorted(range(_BINS), key=lambda i: percentages[i] - counts[i], reverse=True)[:remainder]:
        counts[i] += 1
    return counts


def _overlap(left, right):
    return sum(min(a, b) for a, b in zip(left, right))


def _informative(upper, lower):
    # Monochrome/IR video, weak color and flat single-color surfaces cannot support a match.
    return max(sum(upper[:8]), sum(lower[:8])) >= 40 and _overlap(upper, lower) <= 85


def clothing_signature(image_bytes, box=None):
    """Return two 11-bin integer histograms or None. No image or facial features are retained.

    For a clean full frame, pass its normalized best-snapshot person box. Without a
    box the caller must supply an *already person-cropped* Frigate snapshot.
    """
    if not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
        return None
    try:
        with Image.open(BytesIO(image_bytes), formats=('JPEG', 'WEBP')) as raw:
            width, height = raw.size
            if (max(width, height) > 4096 or width * height > 3840 * 2160 or getattr(raw, 'n_frames', 1) != 1
                    or width < 20 or height < 48):
                return None
            if box is not None:
                bounds = _person_bounds(box, width, height)
                if not bounds:
                    return None
                person = raw.crop(bounds)
            elif 0.16 <= width / height <= 1.12:
                person = raw
            else:
                return None

            upper, lower = [_color_band(person, (.40, .60), rows) for rows in _BAND_ROWS]
            if not _informative(upper, lower):
                return None
            if box is None and width / height > .8:
                # A padded square often includes shelving or a tiny/off-centre person.
                # Abstain when both garment regions resemble the same side background.
                foreground = [upper, lower]
                for columns in ((.05, .20), (.80, .95)):
                    background = [_color_band(person, columns, rows) for rows in _BAND_ROWS]
                    if all(_overlap(a, b) >= 75 for a, b in zip(foreground, background)):
                        return None
            return upper + lower
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _bands(signature):
    if not isinstance(signature, (list, tuple)) or len(signature) != 2 * _BINS:
        return None
    if any(type(v) is not int or not 0 <= v <= 100 for v in signature):
        return None
    upper, lower = signature[:_BINS], signature[_BINS:]
    if sum(upper) != 100 or sum(lower) != 100 or not _informative(upper, lower):
        return None
    return upper, lower


def compare_clothing(left, right):
    """Three-way appearance consistency; the thresholds are initial pilot settings."""
    a, b = _bands(left), _bands(right)
    if a is None or b is None:
        return 'unknown'
    upper, lower = [_overlap(x, y) for x, y in zip(a, b)]
    if min(upper, lower) >= 70 and (upper + lower) / 2 >= 80:
        return 'compatible'
    if min(upper, lower) <= 30 or (upper + lower) / 2 <= 45:
        return 'incompatible'
    return 'unknown'
