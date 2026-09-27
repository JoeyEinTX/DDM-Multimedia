#!/usr/bin/env python3
"""
Build static/fonts/DDMTote.ttf, the dot-matrix face of the board's tote look.

    cd splash_display
    python tools/make_tote_font.py            # writes static/fonts/DDMTote.ttf
    python tools/make_tote_font.py --check    # exit 1 if the file on disk is out of date
    python tools/make_tote_font.py --list     # the characters the face covers

The glyphs are the dashboard's. Its 5x7 table (dotPatterns in
pi5/static/js/ddm_control.js, the one its ticker and results tote are drawn
from) is read here, never copied: there is no glyph bitmap in this file. A
character the board needs and the dashboard did not have is added there, and
this script is run again. tests/test_quiniela.py fails when the font on disk
is not what the table says.

Why a font. The board is twenty rows of names and counts, a header and a
crawl that never stops. Drawn as DOM dots (35 elements a character, as the
dashboard, the countdown and the roster slide do it) that is some thirty
thousand elements; drawn as text in a face whose glyphs ARE the dots it is
the same DOM as the Impact look, the dots come out of the browser's glyph
cache, and a count ticking or the crawl moving costs what text costs.

Geometry (units; 100 to a dot pitch, 800 to the em):

    a character cell is 6 pitches wide and 8 tall: the 5x7 matrix with half
    a pitch of margin all round, so cells butt against each other like the
    dashboard's tiles;
    the dot in column c (0-4), row r (0-6, top down) is a circle of radius
    DOT_R centred at x = 100 (c + 1), y = 100 (7 - r) - 50 above the
    baseline; ascent 750, descent 50, no line gap, so with line-height 1 the
    cell is exactly the line box;
    font-size = 8 x pitch in px. Whole pitches keep every dot on the pixel
    grid, which is why the board steps the pitch, not the font size.

U+E000 is the socket glyph: all 35 dots, for the unlit bulbs behind a field.

The file is written by hand (the ten TrueType tables Chromium's sanitizer
asks for, simple glyphs, a format 4 cmap): no font library is needed, and
the output is the same bytes on every run.
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent.parent                  # splash_display/
GLYPH_SOURCE = HERE.parent / "pi5" / "static" / "js" / "ddm_control.js"
FONT_PATH = HERE / "static" / "fonts" / "DDMTote.ttf"

FAMILY = "DDM Tote"
PS_NAME = "DDMTote-Regular"
VERSION = "Version 1.0"

COLS, ROWS = 5, 7
UNIT = 100                        # font units per dot pitch
UPEM = 8 * UNIT
ADVANCE = 6 * UNIT
ASCENT = 7 * UNIT + UNIT // 2     # 750
DESCENT = UNIT // 2               # 50
DOT_R = 36                        # the dashboard's bulb: 5 px in a 7 px pitch
SOCKET = 0xE000                   # all 35 dots

# Characters drawn with another character's glyph: a 5x7 matrix has no room
# for an accent, and a name must still be readable. Lower case is upper
# case (the board upper-cases everything anyway).
ALIASES: Dict[str, str] = {
    **{chr(c): chr(c - 32) for c in range(ord("a"), ord("z") + 1)},
    **{a: "A" for a in "ÀÁÂÃÄÅàáâãäå"}, **{a: "C" for a in "Çç"},
    **{a: "E" for a in "ÈÉÊËèéêë"}, **{a: "I" for a in "ÌÍÎÏìíîï"},
    **{a: "N" for a in "Ññ"}, **{a: "O" for a in "ÒÓÔÕÖØòóôõöø"},
    **{a: "U" for a in "ÙÚÛÜùúûü"}, **{a: "Y" for a in "Ýýÿ"},
    "‘": "'", "’": "'", "`": "'", "´": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    "•": "·", "×": "X",
    " ": " ",
}

# 2026-01-01 00:00:00 UTC in seconds since 1904: a fixed date, so the file
# is the same bytes whenever it is built.
FIXED_DATE = 3850070400


# ---------------------------------------------------------------------------
# The dashboard's table
# ---------------------------------------------------------------------------
_ENTRY = re.compile(r"""^\s*(?P<q>['"])(?P<key>(?:\\u[0-9A-Fa-f]{4}|\\.|.)+?)(?P=q)\s*:\s*\[(?P<rows>[^\]]*)\]""")


def read_patterns(path: Path = GLYPH_SOURCE) -> Dict[str, Tuple[int, ...]]:
    """dotPatterns as {character: seven row bitmaps}, bit 4 the left column.
    Raises ValueError when the table is not where it is expected or an
    entry is not seven 5-bit rows."""
    text = path.read_text(encoding="utf-8")
    start = text.find("const dotPatterns = {")
    if start < 0:
        raise ValueError(f"{path}: no dotPatterns table")
    end = text.find("\n};", start)
    if end < 0:
        raise ValueError(f"{path}: dotPatterns is not closed")
    out: Dict[str, Tuple[int, ...]] = {}
    for line in text[start:end].splitlines()[1:]:
        m = _ENTRY.match(line)
        if not m:
            continue
        key = m.group("key")
        if key.startswith("\\u"):
            key = chr(int(key[2:], 16))
        elif key.startswith("\\"):
            key = key[1:]
        if len(key) != 1:
            raise ValueError(f"{path}: dotPatterns key {key!r} is not one character")
        rows = tuple(int(v, 0) for v in m.group("rows").replace(" ", "").split(",") if v)
        if len(rows) != ROWS or any(not 0 <= r < (1 << COLS) for r in rows):
            raise ValueError(f"{path}: dotPatterns[{key!r}] is not {ROWS} rows of {COLS} bits: {rows}")
        out[key] = rows
    if len(out) < 37:
        raise ValueError(f"{path}: only {len(out)} patterns found")
    return out


# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------
def circle(cx: int, cy: int, r: int = DOT_R) -> List[Tuple[int, int, bool]]:
    """One dot: eight quadratic arcs, clockwise (TrueType's outer
    direction), as (x, y, on_curve). On-curve points every 45 degrees, the
    control points between them where the tangents meet."""
    pts: List[Tuple[int, int, bool]] = []
    k = r / math.cos(math.pi / 8)
    for i in range(8):
        a = -i * math.pi / 4
        b = a - math.pi / 8
        pts.append((cx + round(r * math.cos(a)), cy + round(r * math.sin(a)), True))
        pts.append((cx + round(k * math.cos(b)), cy + round(k * math.sin(b)), False))
    return pts


def dots_of(rows: Sequence[int]) -> List[Tuple[int, int]]:
    """The centres of the lit dots, in font units, row by row."""
    out = []
    for r, bits in enumerate(rows):
        for c in range(COLS):
            if bits & (1 << (COLS - 1 - c)):
                out.append((UNIT * (c + 1), UNIT * (ROWS - r) - UNIT // 2))
    return out


def glyph_bytes(rows: Sequence[int]) -> Tuple[bytes, Tuple[int, int, int, int], int, int]:
    """(glyf entry, bbox, points, contours) for one pattern; the entry is
    empty for a pattern with no dot (the space)."""
    contours = [circle(x, y) for x, y in dots_of(rows)]
    if not contours:
        return b"", (0, 0, 0, 0), 0, 0
    points = [p for c in contours for p in c]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    ends = []
    n = -1
    for c in contours:
        n += len(c)
        ends.append(n)
    flags = bytearray()
    xb = bytearray()
    yb = bytearray()
    px = py = 0
    for x, y, on in points:
        f = 0x01 if on else 0x00
        dx, dy = x - px, y - py
        px, py = x, y
        if dx == 0:
            f |= 0x10                                  # x is the same
        elif -255 <= dx <= 255:
            f |= 0x02 | (0x10 if dx > 0 else 0)       # one byte, the flag carries the sign
            xb.append(abs(dx))
        else:
            xb += struct.pack(">h", dx)
        if dy == 0:
            f |= 0x20
        elif -255 <= dy <= 255:
            f |= 0x04 | (0x20 if dy > 0 else 0)
            yb.append(abs(dy))
        else:
            yb += struct.pack(">h", dy)
        flags.append(f)
    data = struct.pack(">hhhhh", len(contours), *bbox)
    data += b"".join(struct.pack(">H", e) for e in ends)
    data += struct.pack(">H", 0)                       # no instructions
    data += bytes(flags) + bytes(xb) + bytes(yb)
    return data, bbox, len(points), len(contours)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _checksum(data: bytes) -> int:
    data = _pad(data)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def cmap_table(mapping: Dict[int, int]) -> bytes:
    """A format 4 subtable (Windows, Unicode BMP): one segment per run of
    code points whose glyph ids run with them."""
    codes = sorted(mapping)
    segs: List[Tuple[int, int, int]] = []              # (start, end, delta)
    for code in codes:
        delta = (mapping[code] - code) & 0xFFFF
        if segs and segs[-1][1] == code - 1 and segs[-1][2] == delta:
            segs[-1] = (segs[-1][0], code, delta)
        else:
            segs.append((code, code, delta))
    segs.append((0xFFFF, 0xFFFF, 1))                   # the closing segment: glyph 0
    n = len(segs)
    search = 2 * (1 << int(math.log2(n)))
    sub = struct.pack(">HHHHHHH", 4, 16 + 8 * n, 0, 2 * n, search, int(math.log2(n)), 2 * n - search)
    sub += b"".join(struct.pack(">H", s[1]) for s in segs)
    sub += struct.pack(">H", 0)
    sub += b"".join(struct.pack(">H", s[0]) for s in segs)
    sub += b"".join(struct.pack(">H", s[2]) for s in segs)
    sub += b"".join(struct.pack(">H", 0) for _ in segs)
    return struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + sub


def name_table() -> bytes:
    records = [(1, FAMILY), (2, "Regular"), (3, f"{FAMILY}:{VERSION}"), (4, FAMILY),
               (5, VERSION), (6, PS_NAME)]
    strings = b""
    entries = b""
    for name_id, text in records:
        raw = text.encode("utf-16-be")
        entries += struct.pack(">HHHHHH", 3, 1, 0x0409, name_id, len(raw), len(strings))
        strings += raw
    return struct.pack(">HHH", 0, len(records), 6 + 12 * len(records)) + entries + strings


def os2_table(first: int, last: int) -> bytes:
    panose = bytes([2, 0, 5, 9, 0, 0, 0, 0, 0, 0])     # bProportion 9: monospaced
    return struct.pack(
        ">HhHHHhhhhhhhhhhh10sIIII4sHHHhhhHHIIhhHHH",
        4,                       # version
        ADVANCE,                 # xAvgCharWidth
        400, 5,                  # weight, width
        0,                       # fsType: installable
        UPEM // 2, UPEM // 2, 0, UNIT,        # subscript size and offset
        UPEM // 2, UPEM // 2, 0, 3 * UNIT,    # superscript size and offset
        UNIT, 4 * UNIT,          # strikeout: one pitch thick, its top on row 3's top (the middle row)
        0,                       # sFamilyClass
        panose,
        0x00000003, 0, 0, 0,     # Basic Latin, Latin-1 Supplement
        b"DDM ",
        0x00C0,                  # fsSelection: REGULAR, USE_TYPO_METRICS
        first, min(last, 0xFFFF),
        ASCENT, -DESCENT, 0,     # typo ascender, descender, line gap
        ASCENT, DESCENT,         # win ascent, descent
        0x00000001, 0,           # code page: Latin 1
        7 * UNIT, 7 * UNIT,      # x height, cap height
        0, 32, 0,                # default char, break char, max context
    )


def build() -> bytes:
    patterns = read_patterns()
    if " " not in patterns:
        raise ValueError("dotPatterns has no space")
    for alias, target in ALIASES.items():
        if target not in patterns:
            raise ValueError(f"alias {alias!r} -> {target!r}: the dashboard has no such pattern")

    # Glyph order: .notdef, the space, then the patterns by code point, then the socket.
    order: List[Tuple[Optional[str], Sequence[int]]] = [(None, (0,) * ROWS), (" ", patterns[" "])]
    order += [(ch, patterns[ch]) for ch in sorted(patterns) if ch != " "]
    order.append((chr(SOCKET), ((1 << COLS) - 1,) * ROWS))
    index = {ch: i for i, (ch, _) in enumerate(order) if ch is not None}

    mapping = {ord(ch): i for ch, i in index.items()}
    for alias, target in ALIASES.items():
        mapping.setdefault(ord(alias), index[target])

    glyf = b""
    loca = [0]
    hmtx = b""
    boxes = []
    max_points = max_contours = 0
    for _, rows in order:
        data, bbox, points, contours = glyph_bytes(rows)
        glyf += _pad(data)
        loca.append(len(glyf))
        hmtx += struct.pack(">Hh", ADVANCE, bbox[0])
        if data:
            boxes.append(bbox)
        max_points = max(max_points, points)
        max_contours = max(max_contours, contours)
    x_min = min(b[0] for b in boxes)
    y_min = min(b[1] for b in boxes)
    x_max = max(b[2] for b in boxes)
    y_max = max(b[3] for b in boxes)
    n = len(order)

    head = struct.pack(
        ">IIIIHHqqhhhhHHhhh",
        0x00010000, 0x00010000,  # version, fontRevision
        0,                       # checkSumAdjustment, filled in below
        0x5F0F3CF5,
        0x0003,                  # baseline at y = 0, left side bearing at x = 0
        UPEM,
        FIXED_DATE, FIXED_DATE,
        x_min, y_min, x_max, y_max,
        0,                       # macStyle
        8,                       # lowestRecPPEM: one pixel a dot
        2,
        1,                       # loca: long offsets
        0,
    )
    hhea = struct.pack(
        ">IhhhHhhhhhhhhhhhH",
        0x00010000, ASCENT, -DESCENT, 0,
        ADVANCE,
        x_min,                                   # minLeftSideBearing
        min(ADVANCE - b[2] for b in boxes),      # minRightSideBearing
        x_max,                                   # xMaxExtent
        1, 0, 0,                                 # caret: upright
        0, 0, 0, 0,
        0,
        n,                                       # every glyph has its own metric
    )
    maxp = struct.pack(">IHHHHHHHHHHHHHH", 0x00010000, n, max_points, max_contours,
                       0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    post = struct.pack(">IIhhIIIII", 0x00030000, 0, -DESCENT, UNIT // 2, 1, 0, 0, 0, 0)
    tables = {
        b"OS/2": os2_table(min(mapping), max(mapping)),
        b"cmap": cmap_table(mapping),
        b"glyf": glyf,
        b"head": head,
        b"hhea": hhea,
        b"hmtx": hmtx,
        b"loca": b"".join(struct.pack(">I", o) for o in loca),
        b"maxp": maxp,
        b"name": name_table(),
        b"post": post,
    }

    tags = sorted(tables)
    count = len(tags)
    power = 1 << int(math.log2(count))
    header = struct.pack(">IHHHH", 0x00010000, count, 16 * power, int(math.log2(count)), 16 * (count - power))
    offset = 12 + 16 * count
    directory = b""
    body = b""
    for tag in tags:
        data = tables[tag]
        directory += struct.pack(">4sIII", tag, _checksum(data), offset, len(data))
        body += _pad(data)
        offset += len(_pad(data))
    font = bytearray(header + directory + body)
    adjustment = (0xB1B0AFBA - _checksum(bytes(font))) & 0xFFFFFFFF
    head_at = 12 + 16 * count + sum(len(_pad(tables[t])) for t in tags if t < b"head")
    font[head_at + 8:head_at + 12] = struct.pack(">I", adjustment)
    return bytes(font)


def covered() -> str:
    """Every character the face draws, the aliases included."""
    patterns = read_patterns()
    return "".join(sorted(set(patterns) | set(ALIASES)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the board's dot-matrix face from the dashboard's 5x7 table.")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if the font on disk is out of date")
    ap.add_argument("--list", action="store_true", help="print the characters the face covers")
    args = ap.parse_args()
    if args.list:
        sys.stdout.buffer.write((covered() + "\n").encode("utf-8"))
        return 0
    font = build()
    if args.check:
        current = FONT_PATH.read_bytes() if FONT_PATH.exists() else b""
        if current != font:
            print(f"{FONT_PATH} is out of date: run python tools/make_tote_font.py")
            return 1
        print(f"{FONT_PATH} is up to date ({len(font)} bytes)")
        return 0
    FONT_PATH.write_bytes(font)
    print(f"wrote {FONT_PATH} ({len(font)} bytes, {len(read_patterns())} patterns from {GLYPH_SOURCE.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
