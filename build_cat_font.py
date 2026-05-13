from __future__ import annotations

import statistics
import sys
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from PIL import Image, ImageDraw, ImageFont
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from shapely.affinity import scale as scale_geom
from shapely.affinity import translate as move_geom
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

REF_IMAGE = BASE_DIR / "frames" / "frame-final-clean.png"
DIST_DIR = BASE_DIR / "dist"

THRESHOLD = 92
UPM = 1000
ASCENDER = 860
DESCENDER = -140
CAP_HEIGHT = 760
SIDE_BEARING = 28
UPSCALE = 4
LOWERCASE_SCALE = 2 / 3

# These are the user-confirmed boxes around the handwritten cat glyphs.
GLYPH_BOXES = {
    "A": (137, 72, 178, 132),
    "B": (185, 72, 221, 132),
    "C": (227, 72, 267, 132),
    "D": (274, 72, 314, 132),
    "E": (320, 72, 354, 132),
    "F": (368, 72, 399, 132),
    "G": (402, 72, 447, 132),
    "H": (452, 72, 485, 132),
    "I": (490, 72, 516, 132),
    "J": (509, 72, 544, 132),
    "K": (549, 72, 583, 132),
    "L": (142, 184, 177, 246),
    "M": (179, 184, 225, 246),
    "N": (231, 184, 263, 246),
    "O": (269, 184, 317, 246),
    "P": (324, 184, 357, 246),
    "Q": (365, 184, 416, 246),
    "R": (423, 184, 460, 246),
    "S": (464, 184, 498, 246),
    "T": (500, 184, 543, 246),
    "U": (549, 184, 592, 246),
    "V": (138, 295, 180, 360),
    "W": (181, 295, 239, 360),
    "X": (243, 295, 277, 360),
    "Y": (279, 295, 316, 360),
    "Z": (318, 295, 357, 360),
}

ROW_STRINGS = ["ABCDEFGHIJK", "LMNOPQRSTU", "VWXYZ", "abcdefghijk", "lmnopqrstu", "vwxyz"]


def connected_components(mask: list[list[bool]]) -> list[list[tuple[int, int]]]:
    height = len(mask)
    width = len(mask[0]) if height else 0
    seen = [[False] * width for _ in range(height)]
    out = []

    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue

            queue = deque([(x, y)])
            seen[y][x] = True
            points = []

            while queue:
                cx, cy = queue.popleft()
                points.append((cx, cy))
                for ny in range(max(0, cy - 1), min(height, cy + 2)):
                    for nx in range(max(0, cx - 1), min(width, cx + 2)):
                        if mask[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            queue.append((nx, ny))

            out.append(points)

    return out


def polygons_from_geom(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out = []
        for part in geom.geoms:
            out.extend(polygons_from_geom(part))
        return out
    raise TypeError(f"Unsupported geometry type: {type(geom)!r}")


def draw_polygon(pen: TTGlyphPen, poly: Polygon):
    poly = orient(poly, sign=-1.0)

    def add_ring(coords):
        pts = [(round(x), round(y)) for x, y in list(coords)[:-1]]
        if len(pts) < 3:
            return
        pen.moveTo(pts[0])
        for pt in pts[1:]:
            pen.lineTo(pt)
        pen.closePath()

    add_ring(poly.exterior.coords)
    for interior in poly.interiors:
        add_ring(interior.coords)


def geom_to_glyph(geom):
    pen = TTGlyphPen(None)
    for poly in polygons_from_geom(geom):
        draw_polygon(pen, poly)
    return pen.glyph()


def trace_crop(image: Image.Image, crop_box: tuple[int, int, int, int]):
    crop = image.crop(crop_box).convert("L")
    crop = crop.resize((crop.width * UPSCALE, crop.height * UPSCALE), Image.Resampling.BICUBIC)
    width, height = crop.size

    mask = [[crop.getpixel((x, y)) < THRESHOLD for x in range(width)] for y in range(height)]

    # The boxes are already user-approved, so keep every non-trivial component inside them.
    keep_mask = [[False] * width for _ in range(height)]
    for component in connected_components(mask):
        if len(component) < 4:
            continue
        for x, y in component:
            keep_mask[y][x] = True

    rects = []
    minx = width
    miny = height
    maxx = 0
    maxy = 0

    for y in range(height):
        x = 0
        while x < width:
            if not keep_mask[y][x]:
                x += 1
                continue

            start = x
            while x < width and keep_mask[y][x]:
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
                x += 1

            rects.append(box(start, height - y - 1, x, height - y))

    if not rects:
        return GeometryCollection(), 1.0, width

    geom = unary_union(rects)
    geom = geom.buffer(0.7, join_style=1).buffer(-0.7, join_style=1)
    geom = geom.simplify(0.22, preserve_topology=True)

    minx_g, miny_g, _, maxy_g = geom.bounds
    geom = move_geom(geom, xoff=0, yoff=-miny_g)

    black_height = max(1.0, maxy_g - miny_g)
    return geom, black_height, width


def build_font():
    DIST_DIR.mkdir(exist_ok=True)

    image = Image.open(REF_IMAGE)
    traced = {}
    heights = []

    for char, crop_box in GLYPH_BOXES.items():
        geom, black_height, crop_width = trace_crop(image, crop_box)
        traced[char] = {"geom": geom, "crop_width": crop_width}
        heights.append(black_height)

    scale_factor = CAP_HEIGHT / statistics.median(heights)

    uppercase_chars = list(GLYPH_BOXES.keys())
    lowercase_chars = [ch.lower() for ch in uppercase_chars]
    glyph_order = [".notdef"] + uppercase_chars + lowercase_chars
    cmap = {ord(ch): ch for ch in uppercase_chars + lowercase_chars}
    glyphs = {}
    metrics = {}

    notdef = box(0, 0, 520, 760).difference(box(60, 60, 460, 700))
    glyphs[".notdef"] = geom_to_glyph(notdef)
    metrics[".notdef"] = (600, 0)

    for char, item in traced.items():
        scaled = scale_geom(item["geom"], xfact=scale_factor, yfact=scale_factor, origin=(0, 0))
        scaled = move_geom(scaled, xoff=SIDE_BEARING, yoff=0)
        advance = int(round(item["crop_width"] * scale_factor + SIDE_BEARING * 2))
        glyphs[char] = geom_to_glyph(scaled)
        metrics[char] = (max(advance, 180), 0)

        lowercase_char = char.lower()
        lower_factor = scale_factor * LOWERCASE_SCALE
        lowercase = scale_geom(item["geom"], xfact=lower_factor, yfact=lower_factor, origin=(0, 0))
        lowercase = move_geom(lowercase, xoff=SIDE_BEARING, yoff=0)
        lowercase_advance = int(round(item["crop_width"] * lower_factor + SIDE_BEARING * 2))
        glyphs[lowercase_char] = geom_to_glyph(lowercase)
        metrics[lowercase_char] = (max(lowercase_advance, 150), 0)

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=ASCENDER, descent=DESCENDER)
    fb.setupOS2(
        sTypoAscender=ASCENDER,
        sTypoDescender=DESCENDER,
        sTypoLineGap=120,
        usWinAscent=ASCENDER,
        usWinDescent=abs(DESCENDER),
        sxHeight=0,
        sCapHeight=CAP_HEIGHT,
    )
    fb.setupNameTable(
        {
            "familyName": "Cat Font",
            "styleName": "Regular",
            "fullName": "Cat Font Regular",
            "psName": "CatFont-Regular",
            "version": "Version 5.0",
            "uniqueFontIdentifier": "CatFont-Regular-5.0",
        }
    )
    fb.setupPost()
    fb.setupMaxp()
    fb.setupDummyDSIG()

    font_path = DIST_DIR / "CatFont-Regular.ttf"
    fb.save(font_path)
    return font_path


def render_preview(font_path: Path):
    preview_path = font_path.with_name("CatFont-preview.png")
    diagnostic_path = font_path.with_name("CatFont-glyph-map.png")

    reference = Image.open(REF_IMAGE).convert("RGB")
    reference = reference.resize((reference.width * 14 // 10, reference.height * 14 // 10))

    canvas = Image.new("RGB", (1120, 1320), "white")
    draw = ImageDraw.Draw(canvas)
    ui_font = ImageFont.load_default()
    cat_font = ImageFont.truetype(str(font_path), 78)

    canvas.paste(reference, (30, 40))
    draw.text((30, 12), "REFERENCE", fill="black", font=ui_font)
    draw.line((30, 640, 1090, 640), fill=(210, 210, 210), width=1)
    draw.text((30, 670), "GENERATED FONT", fill="black", font=ui_font)

    y = 730
    for row in ROW_STRINGS:
        draw.text((90, y), row, fill="black", font=cat_font)
        y += 95

    canvas.save(preview_path)

    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    cell_w, cell_h = 180, 200
    cols = 4
    rows = (len(chars) + cols - 1) // cols
    diag = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
    dd = ImageDraw.Draw(diag)
    for i, ch in enumerate(chars):
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        dd.rectangle((x, y, x + cell_w - 1, y + cell_h - 1), outline="lightgray", width=1)
        dd.text((x + 8, y + 8), ch, fill="black", font=ui_font)
        bbox = dd.textbbox((0, 0), ch, font=cat_font)
        tw = bbox[2] - bbox[0]
        dd.text((x + (cell_w - tw) // 2 - bbox[0], y + 55 - bbox[1]), ch, fill="black", font=cat_font)
    diag.save(diagnostic_path)

    return preview_path


def main():
    font_path = build_font()
    preview_path = render_preview(font_path)
    print(font_path)
    print(preview_path)


if __name__ == "__main__":
    main()
