"""
tracefont.py — trace a TTF's glyph outlines into flattened, simplified polygon
data for the DDM cup's anti-aliased scanline renderer.

Every glyph is scaled so the font's CAP HEIGHT == GRID units. Digits and
capitals therefore share one vertical scale, and a string of mixed text lays
out on one baseline.

Usage: python3 tracefont.py FONT.TTF out.h [eps]
"""
import sys, math
from fontTools.ttLib import TTFont
from fontTools.pens.basePen import BasePen

GRID    = 1000
CHARS   = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ %:-.!?/"
BASE_CH = "H"          # cap-height reference

class FlattenPen(BasePen):
    def __init__(self, glyphSet, tol):
        super().__init__(glyphSet)
        self.contours, self.cur, self.tol = [], [], tol
        self._pt = (0, 0)
    def _moveTo(self, p):
        if self.cur: self.contours.append(self.cur)
        self.cur = [p]; self._pt = p
    def _lineTo(self, p):
        self.cur.append(p); self._pt = p
    def _steps(self, pts):
        d = sum(math.hypot(b[0]-a[0], b[1]-a[1]) for a, b in zip(pts, pts[1:]))
        return max(2, min(32, int(d / self.tol) + 2))
    def _qCurveToOne(self, c, p):
        a = self._pt; n = self._steps([a, c, p])
        for i in range(1, n + 1):
            t = i / n; u = 1 - t
            self.cur.append((u*u*a[0] + 2*u*t*c[0] + t*t*p[0],
                             u*u*a[1] + 2*u*t*c[1] + t*t*p[1]))
        self._pt = p
    def _curveToOne(self, c1, c2, p):
        a = self._pt; n = self._steps([a, c1, c2, p])
        for i in range(1, n + 1):
            t = i / n; u = 1 - t
            self.cur.append((u**3*a[0] + 3*u*u*t*c1[0] + 3*u*t*t*c2[0] + t**3*p[0],
                             u**3*a[1] + 3*u*u*t*c1[1] + 3*u*t*t*c2[1] + t**3*p[1]))
        self._pt = p
    def _closePath(self):
        if self.cur: self.contours.append(self.cur); self.cur = []
    def _endPath(self):
        self._closePath()
    def done(self):
        if self.cur: self.contours.append(self.cur); self.cur = []
        return self.contours

def rdp(pts, eps):
    def _rdp(p):
        if len(p) < 3: return p
        a, b = p[0], p[-1]
        dx, dy = b[0]-a[0], b[1]-a[1]
        L = math.hypot(dx, dy)
        best, bi = -1, 0
        for i in range(1, len(p) - 1):
            d = (math.hypot(p[i][0]-a[0], p[i][1]-a[1]) if L < 1e-9
                 else abs(dy*(p[i][0]-a[0]) - dx*(p[i][1]-a[1])) / L)
            if d > best: best, bi = d, i
        if best <= eps: return [a, b]
        return _rdp(p[:bi+1])[:-1] + _rdp(p[bi:])
    n = len(pts)
    if n < 4: return pts
    h = n // 2
    return _rdp(pts[:h+1])[:-1] + _rdp(pts[h:] + [pts[0]])[:-1]

def trace(path, eps):
    f    = TTFont(path)
    gs   = f.getGlyphSet()
    cmap = f.getBestCmap()
    upem = f['head'].unitsPerEm
    hmtx = f['hmtx']

    # cap height from the reference glyph
    pen = FlattenPen(gs, upem / 300.0)
    gs[cmap[ord(BASE_CH)]].draw(pen)
    ys = [p[1] for c in pen.done() for p in c]
    capH = max(ys)
    s = GRID / capH

    glyphs = {}
    for ch in CHARS:
        gname = cmap[ord(ch)]
        adv, lsb = hmtx[gname]
        pen = FlattenPen(gs, upem / 300.0)
        gs[gname].draw(pen)
        raw = [c for c in pen.done() if len(c) >= 3]

        # y flipped: 0 at cap height, GRID at baseline; may go slightly
        # negative (overshoot) or past GRID (descenders/overshoot)
        cs = []
        for c in raw:
            c = [((p[0]) * s, (capH - p[1]) * s) for p in c]
            c = rdp(c, eps)
            if len(c) >= 3: cs.append(c)

        glyphs[ch] = dict(adv=int(round(adv * s)), contours=cs)
    return glyphs

def emit(glyphs, out, fontname):
    pts, coff, clen = [], [], []
    gfirst, gnc, gadv = [], [], []
    order = list(CHARS)
    for ch in order:
        g = glyphs[ch]
        gfirst.append(len(coff)); gnc.append(len(g['contours'])); gadv.append(g['adv'])
        for c in g['contours']:
            coff.append(len(pts) // 2); clen.append(len(c))
            for p in c:
                pts.append(int(round(p[0]))); pts.append(int(round(p[1])))

    def arr(name, vals, t, per=16):
        s = f"const {t} {name}[] = {{\n"
        for i in range(0, len(vals), per):
            s += "  " + ", ".join(str(v) for v in vals[i:i+per]) + ",\n"
        return s + "};\n\n"

    charmap = "".join(order).replace('\\', '\\\\').replace('"', '\\"')
    h  = f"""// Glyph outlines traced from {fontname}.
// Cap height == FONT_GRID units. y = 0 at cap height, y = FONT_GRID at baseline.
// Each glyph's advance is in the same units. Generated data — do not hand edit.

#ifndef DDM_FONT_H
#define DDM_FONT_H
#include <Arduino.h>

#define FONT_GRID   {GRID}
#define FONT_NGLYPH {len(order)}
static const char FONT_CHARS[] = "{charmap}";

"""
    h += arr("FONT_ADV",   gadv,   "uint16_t", 12)
    h += arr("FONT_FIRST", gfirst, "uint16_t", 12)
    h += arr("FONT_NCONT", gnc,    "uint8_t",  12)
    h += arr("FONT_COFF",  coff,   "uint16_t")
    h += arr("FONT_CLEN",  clen,   "uint16_t")
    h += arr("FONT_PTS",   pts,    "int16_t")
    h += """// Index of a character in the tables above, or -1 if the font lacks it.
static inline int fontIndex(char c) {
  for (int i = 0; i < FONT_NGLYPH; i++) if (FONT_CHARS[i] == c) return i;
  return -1;
}

#endif
"""
    open(out, 'w').write(h)
    return len(pts) // 2

if __name__ == "__main__":
    font = sys.argv[1]; out = sys.argv[2]
    eps  = float(sys.argv[3]) if len(sys.argv) > 3 else 1.4
    g    = trace(font, eps)
    n    = emit(g, out, font.split('/')[-1])
    print(f"{len(g)} glyphs, {n} points, {n*4} bytes of outline data")
    for ch in CHARS:
        gg = g[ch]
        print(f"  '{ch}'  adv={gg['adv']:4d}  contours={len(gg['contours'])}  "
              f"pts={sum(len(c) for c in gg['contours'])}")
