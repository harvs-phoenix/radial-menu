"""High quality renderer for the radial menu.

Draws the wheel with Pillow at 2x supersampling so every edge is antialiased,
then downscales. Slices are true rounded wedges (Minkowski-dilated polygons),
filled with a vertical gradient, with a soft drop shadow and a glow that
appears under the hovered slice.

Everything is returned as RGBA images ready to be blitted into a Win32
layered window (see overlay.py) - no Tk colour-key hacks, no black fringes.
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SS = 2  # supersampling factor

FONT_REG = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
FONT_BOLD = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "seguisb.ttf")

THEME_DEFAULT = {
    "hub": "#12141c",
    "hub_border": "#2c3245",
    "slice_top": "#232838",
    "slice_bottom": "#171a26",
    "slice_border": "#333a52",
    "hover_top": "#5b8cff",
    "hover_bottom": "#8b5cf6",
    "glow": "#6d8dff",
    "text": "#e9edf7",
    "text_dim": "#8d95ab",
    "accent": "#5b8cff",
    "shadow": "#000000",
}

_ICON_STROKE = 3.2  # in final pixels, scaled up internally


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _font(size, bold=False):
    path = FONT_BOLD if bold else FONT_REG
    try:
        return ImageFont.truetype(path, int(size))
    except OSError:
        return ImageFont.load_default()


# ---------------------------------------------------------------- geometry --

def _polar(cx, cy, r, ang_deg):
    """Screen point for radius r and angle measured clockwise from north."""
    a = math.radians(ang_deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def rounded_wedge(draw, cx, cy, r_in, r_out, a0, a1, corner, fill):
    """Fill a wedge from a0..a1 (deg, clockwise from north) with rounded corners.

    Built as a densely sampled polygon dilated by `corner` using thick lines
    and discs, which is exactly the Minkowski sum with a disc - giving true
    rounded corners on both the arc ends and the hub end.
    """
    step = 2.5
    span = (a1 - a0) % 360
    if span < 0.01:
        return
    # shrink the sampled polygon by `corner` along each edge so the dilation
    # lands exactly on the intended boundary
    d_out = math.degrees(corner / max(r_out, 1)) + 0.0
    d_in = math.degrees(corner / max(r_in, 1)) + 0.0
    lo, hi = a0 + d_out, a1 - d_out
    pts = []
    n = max(2, int(span / step))
    if hi > lo:
        for i in range(n + 1):
            pts.append(_polar(cx, cy, r_out - corner, lo + (hi - lo) * i / n))
        lo_i, hi_i = a0 + d_in, a1 - d_in
        for i in range(n + 1):
            pts.append(_polar(cx, cy, r_in + corner, hi_i - (hi_i - lo_i) * i / n))
    else:
        # wedge thinner than the corner allowance: fall back to a plain wedge
        pts = [_polar(cx, cy, r_out, a0), _polar(cx, cy, r_out, a1),
               _polar(cx, cy, r_in, a1), _polar(cx, cy, r_in, a0)]

    if len(pts) >= 3:
        draw.polygon(pts, fill=fill)
    w = max(1, int(round(corner * 2)))
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        draw.line([x1, y1, x2, y2], fill=fill, width=w)
        draw.ellipse([x1 - corner, y1 - corner, x1 + corner, y1 + corner], fill=fill)


def _gradient_mask(mask, top, bottom, radius, cy):
    """Paint a vertical gradient through an L-mode `mask`."""
    w, h = mask.size
    grad = Image.new("RGB", (1, h))
    gp = grad.load()
    for y in range(h):
        t = min(1.0, max(0.0, abs(y - cy) / max(radius, 1)))
        gp[0, y] = _lerp(top, bottom, t ** 0.85)
    grad = grad.resize((w, h), Image.NEAREST)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(grad.convert("RGBA"), (0, 0), mask)
    return out


# ------------------------------------------------------------------ icons --
# Stroke icons drawn in a 24x24 box, tinted to any colour.

def _poly(d, pts, col, w, close=False):
    seq = list(pts) + ([pts[0]] if close else [])
    d.line(seq, fill=col, width=w, joint="curve")
    r = w / 2.0
    for x, y in seq:
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)


def _circle(d, cx, cy, r, col, w):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=w)


def _roundrect(d, box, r, col, w):
    d.rounded_rectangle(box, radius=r, outline=col, width=w)


def _arc(d, cx, cy, r, a0, a1, col, w):
    d.arc([cx - r, cy - r, cx + r, cy + r], a0, a1, fill=col, width=w)


ICONS = {
    "scroll_up": lambda d, c, w: (
        _roundrect(d, [7, 2, 17, 22], 5, c, w), d.line([12, 6, 12, 11], fill=c, width=w),
        _poly(d, [(9, 10), (12, 6.5), (15, 10)], c, w)),
    "scroll_down": lambda d, c, w: (
        _roundrect(d, [7, 2, 17, 22], 5, c, w), d.line([12, 13, 12, 18], fill=c, width=w),
        _poly(d, [(9, 14), (12, 17.5), (15, 14)], c, w)),
    "arrow_up": lambda d, c, w: (_poly(d, [(12, 19), (12, 5)], c, w),
                                 _poly(d, [(7, 10), (12, 5), (17, 10)], c, w)),
    "arrow_down": lambda d, c, w: (_poly(d, [(12, 5), (12, 19)], c, w),
                                   _poly(d, [(7, 14), (12, 19), (17, 14)], c, w)),
    "arrow_left": lambda d, c, w: (_poly(d, [(19, 12), (5, 12)], c, w),
                                   _poly(d, [(10, 7), (5, 12), (10, 17)], c, w)),
    "arrow_right": lambda d, c, w: (_poly(d, [(5, 12), (19, 12)], c, w),
                                    _poly(d, [(14, 7), (19, 12), (14, 17)], c, w)),
    "copy": lambda d, c, w: (_roundrect(d, [3, 7, 13, 20], 2, c, w),
                             _roundrect(d, [9, 3, 19, 16], 2, c, w)),
    "paste": lambda d, c, w: (_roundrect(d, [4, 4, 20, 21], 2, c, w),
                              _roundrect(d, [9, 2, 15, 6], 1.5, c, w),
                              d.line([8, 12, 16, 12], fill=c, width=w),
                              d.line([8, 16, 14, 16], fill=c, width=w)),
    "undo": lambda d, c, w: (_arc(d, 13, 13, 7, 120, 300, c, w),
                             _poly(d, [(4, 13), (10, 9), (10, 17)], c, w, close=True)),
    "redo": lambda d, c, w: (_arc(d, 11, 13, 7, 240, 60, c, w),
                             _poly(d, [(20, 13), (14, 9), (14, 17)], c, w, close=True)),
    "clipboard": lambda d, c, w: (_roundrect(d, [5, 4, 19, 21], 2, c, w),
                                  _roundrect(d, [9, 2, 15, 6], 1.5, c, w)),
    "lock": lambda d, c, w: (_roundrect(d, [5, 11, 19, 21], 2, c, w),
                             _arc(d, 12, 11, 4, 180, 360, c, w),
                             d.ellipse([11, 14, 13, 17], fill=c)),
    "unlock": lambda d, c, w: (_roundrect(d, [5, 11, 19, 21], 2, c, w),
                               _arc(d, 12, 11, 4, 200, 380, c, w),
                               d.ellipse([11, 14, 13, 17], fill=c)),
    "star": lambda d, c, w: _poly(d, [(
        *[(12 + 8 * math.sin(math.radians(a)), 12 - 8 * math.cos(math.radians(a)))
          for a in (0, 144, 288)][0],)], c, w),
    "settings": lambda d, c, w: (
        d.line([4, 8, 20, 8], fill=c, width=w), d.line([4, 16, 20, 16], fill=c, width=w),
        d.ellipse([8, 5, 13, 11], fill="#00000000", outline=c, width=w),
        d.ellipse([12, 13, 17, 19], outline=c, width=w)),
    "power": lambda d, c, w: (_arc(d, 12, 13, 7, -55, 235, c, w),
                              d.line([12, 3, 12, 12], fill=c, width=w)),
    "camera": lambda d, c, w: (_roundrect(d, [2, 7, 22, 20], 3, c, w),
                               _circle(d, 12, 13.5, 4.2, c, w),
                               _poly(d, [(8, 7), (9.5, 4), (14.5, 4), (16, 7)], c, w)),
    "folder": lambda d, c, w: _poly(d, [(3, 19), (3, 7), (9.5, 7), (11.5, 9.5),
                                        (21, 9.5), (21, 19)], c, w, close=True),
    "globe": lambda d, c, w: (_circle(d, 12, 12, 9, c, w),
                              d.ellipse([5, 3, 19, 21], outline=c, width=w),
                              d.line([3, 12, 21, 12], fill=c, width=w)),
    "terminal": lambda d, c, w: (_roundrect(d, [2, 4, 22, 20], 3, c, w),
                                 _poly(d, [(6, 9), (10, 12), (6, 15)], c, w),
                                 d.line([12, 16, 17, 16], fill=c, width=w)),
    "trash": lambda d, c, w: (d.line([3, 6, 21, 6], fill=c, width=w),
                              _poly(d, [(6, 6), (7.5, 21), (16.5, 21), (18, 6)], c, w),
                              d.line([9.5, 10, 9.5, 18], fill=c, width=w),
                              d.line([14.5, 10, 14.5, 18], fill=c, width=w),
                              _poly(d, [(9, 4), (15, 4)], c, w)),
    "play": lambda d, c, w: _poly(d, [(7, 4), (20, 12), (7, 20)], c, w, close=True),
    "music": lambda d, c, w: (d.ellipse([4, 16, 9, 21], outline=c, width=w),
                              d.ellipse([14, 14, 19, 19], outline=c, width=w),
                              _poly(d, [(9, 18.5), (19, 16.5), (19, 5), (14, 7)], c, w)),
    "mic": lambda d, c, w: (_roundrect(d, [9, 2, 15, 13], 3, c, w),
                            _arc(d, 12, 12, 6, 0, 180, c, w),
                            d.line([12, 18, 12, 21], fill=c, width=w),
                            d.line([8, 21, 16, 21], fill=c, width=w)),
    "chat": lambda d, c, w: (_roundrect(d, [3, 4, 21, 17], 3, c, w),
                             _poly(d, [(8, 17), (8, 21), (13, 17)], c, w)),
    "mail": lambda d, c, w: (_roundrect(d, [3, 6, 21, 18], 2, c, w),
                             _poly(d, [(4, 8), (12, 14), (20, 8)], c, w)),
    "home": lambda d, c, w: (_poly(d, [(3, 12), (12, 4), (21, 12)], c, w),
                             _poly(d, [(5.5, 11), (5.5, 20), (18.5, 20), (18.5, 11)], c, w)),
    "search": lambda d, c, w: (_circle(d, 10.5, 10.5, 6.5, c, w),
                               d.line([15.5, 15.5, 20, 20], fill=c, width=w)),
    "plus": lambda d, c, w: (d.line([12, 5, 12, 19], fill=c, width=w),
                             d.line([5, 12, 19, 12], fill=c, width=w)),
    "check": lambda d, c, w: _poly(d, [(5, 13), (10, 18), (19, 6)], c, w),
    "close": lambda d, c, w: (_poly(d, [(6, 6), (18, 18)], c, w),
                              _poly(d, [(18, 6), (6, 18)], c, w)),
    "dots": lambda d, c, w: [d.ellipse([x - 2, 10, x + 2, 14], fill=c) for x in (5, 12, 19)],
    "mouse": lambda d, c, w: (_roundrect(d, [7, 2, 17, 22], 5, c, w),
                              d.line([12, 4, 12, 10], fill=c, width=w)),
}


def _star_points(cx, cy, r, spikes=5):
    pts = []
    for i in range(spikes * 2):
        rr = r if i % 2 == 0 else r * 0.44
        a = math.radians(i * 180 / spikes - 90)
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


ICONS["star"] = lambda d, c, w: _poly(d, _star_points(12, 12.5, 9.5), c, w, close=True)


class _Scaled:
    """Draw proxy mapping a 24x24 design box onto a canvas of side `s`.

    Lets the icon definitions below stay written in simple 0-24 design units
    while everything is actually drawn supersampled at full resolution.
    """

    def __init__(self, draw, s, design=24.0):
        self._d = draw
        self._k = s / design

    def _p(self, xy):
        """Accept either [(x, y), ...] or a flat [x0, y0, x1, y1, ...] list."""
        k = self._k
        out = []
        it = iter(xy)
        first = next(it, None)
        if first is None:
            return out
        try:
            iter(first)
        except TypeError:
            # flat coordinate list
            vals = [first] + list(it)
            for i in range(0, len(vals) - 1, 2):
                out.extend((vals[i] * k, vals[i + 1] * k))
            return out
        out.extend((first[0] * k, first[1] * k))
        for v in it:
            out.extend((v[0] * k, v[1] * k))
        return out

    def _w(self, w):
        return max(1, int(round(w * self._k)))

    def line(self, xy, fill=None, width=1, joint=None):
        self._d.line(self._p(xy), fill=fill, width=self._w(width), joint=joint)

    def ellipse(self, xy, fill=None, outline=None, width=1):
        self._d.ellipse(self._p(xy), fill=fill, outline=outline, width=self._w(width))

    def arc(self, xy, a0, a1, fill=None, width=1):
        self._d.arc(self._p(xy), a0, a1, fill=fill, width=self._w(width))

    def rounded_rectangle(self, xy, radius=0, fill=None, outline=None, width=1):
        self._d.rounded_rectangle(self._p(xy), radius=self._w(radius), fill=fill,
                                  outline=outline, width=self._w(width))

    def rectangle(self, xy, fill=None, outline=None, width=1):
        self._d.rectangle(self._p(xy), fill=fill, outline=outline, width=self._w(width))

    def polygon(self, xy, fill=None):
        self._d.polygon(self._p(xy), fill=fill)

    def pieslice(self, xy, a0, a1, fill=None):
        self._d.pieslice(self._p(xy), a0, a1, fill=fill)


_HOTKEY_ICONS = {
    "ctrl+c": "copy", "ctrl+x": "copy", "ctrl+v": "paste", "ctrl+shift+v": "clipboard",
    "ctrl+z": "undo", "ctrl+y": "redo", "ctrl+a": "check", "ctrl+s": "folder",
    "ctrl+p": "camera", "ctrl+f": "search", "ctrl+t": "globe", "ctrl+n": "plus",
    "ctrl+w": "close", "alt+tab": "dots", "alt+f4": "power",
}


def icon_for(action):
    """Pick an icon for an action, preferring an explicit "icon" config key."""
    explicit = (action.get("icon") or "").strip()
    if explicit:
        return explicit
    kind = action.get("type", "")
    label = (action.get("label") or "").lower()
    target = (action.get("target") or "").lower().strip()

    if kind == "scroll":
        if action.get("dy", 0) > 0:
            return "scroll_up"
        if action.get("dy", 0) < 0:
            return "scroll_down"
        return "arrow_left" if action.get("dx", 0) < 0 else "arrow_right"
    if kind in ("hotkey", "key"):
        hit = _HOTKEY_ICONS.get(target)
        if hit:
            return hit
        if "+" in target:
            return "plus"
        return "terminal"
    if kind == "url":
        return "globe"
    if kind == "app":
        low = target.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if "calc" in low:
            return "settings"
        if any(s in low for s in ("note", "code", "word", "write")):
            return "clipboard"
        if any(s in low for s in ("browser", "chrome", "edge", "firefox")):
            return "globe"
        if "screen" in low or "clip" in low:
            return "camera"
        return "folder"
    if kind == "command":
        return "terminal"
    if kind == "type":
        return "chat"
    if kind == "quit":
        return "power"

    # last resort: match the label
    for key, name in (("scroll up", "scroll_up"), ("scroll down", "scroll_down"),
                      ("clipboard", "clipboard"), ("copy", "copy"), ("paste", "paste"),
                      ("undo", "undo"), ("redo", "redo"), ("left", "arrow_left"),
                      ("right", "arrow_right"), ("quit", "power"), ("help", "check")):
        if key in label:
            return name
    return "dots"


def draw_icon(name, size, color, stroke=_ICON_STROKE):
    """Render an icon to RGBA. `size` is the FINAL pixel size (SS applied here).

    `color` may be an RGB tuple or a "#rrggbb" string. Both forms occur: the
    wheel passes tuples, but a hex literal is what callers naturally reach for,
    and `tuple(color)` on a string quietly builds a 7-tuple of characters that
    PIL then rejects - which, inside a preview's try/except, meant a silently
    blank icon. Converting here is cheaper than making every caller remember.

    Drawn straight at supersampled resolution through _Scaled, so the strokes
    are antialiased and match the quality of the rest of the wheel.
    """
    if isinstance(color, str):
        color = _hex(color)
    fn = ICONS.get(name)
    s = max(1, int(size * SS))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    if fn is None:
        return img
    d = _Scaled(ImageDraw.Draw(img), s)
    col = tuple(color) + (255,)
    # `stroke` is in FINAL px; the design box is 24 units across `size` px
    pen = max(0.4, stroke * 24.0 / max(size, 1))
    fn(d, col, pen)
    return img


def tray_icon(size=32, accent=(91, 140, 255)):
    """A filled pie-wheel glyph for the system tray.

    The line-art action icons turn to mush at tray size, so this uses solid
    wedges with one highlighted slice and a punched-out hub. That keeps it
    readable at 16px and visually identical to the wheel it belongs to.
    """
    s = max(8, int(size * 2))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s / 2.0
    r = s * 0.47
    hub = r * 0.32
    n, step, gap = 8, 45.0, 6.0
    box = [cx - r, cy - r, cx + r, cy + r]
    for i in range(n):
        a0 = -90 + i * step + gap / 2.0
        a1 = -90 + (i + 1) * step - gap / 2.0
        col = tuple(accent) + (255,) if i == 0 else (82, 92, 122, 255)
        d.pieslice(box, a0, a1, fill=col)
    # clear the hub so it reads as a wheel rather than a pie chart
    d.ellipse([cx - hub, cy - hub, cx + hub, cy + hub], fill=(0, 0, 0, 0))
    return img


# ------------------------------------------------------------------ wheel --

class Wheel:
    #: Hard cap per page - beyond this the wedge labels stop fitting legibly.
    MAX_ACTIONS = 12

    def __init__(self, cfg, actions=None):
        self.theme = dict(THEME_DEFAULT)
        self.theme.update(cfg.get("theme", {}))
        self.actions = list(
            actions if actions is not None else cfg.get("actions", [])
        )[:self.MAX_ACTIONS]
        self.page_index = 0
        self.page_count = 1
        self.R = int(cfg.get("radius", 150))
        self.inner = int(cfg.get("inner_radius", 52))
        self.corner = int(cfg.get("corner_radius", 9))
        self.gap = float(cfg.get("gap_degrees", 3.0))
        self.pad = 30  # room for shadow + glow
        self.size = (self.R + self.pad) * 2
        self.labels_on = cfg.get("show_labels", True)
        self._fonts = {}
        self._base = None
        self._hover = {}

    def set_actions(self, actions, page_index=0, page_count=1):
        """Swap in another page's actions and drop the cached frames.

        The wedge geometry is derived from the slice count, so frames cached
        for the previous page are not merely stale - they are wrong.
        """
        page = list(actions)[:self.MAX_ACTIONS]
        if (page == self.actions and page_index == self.page_index
                and page_count == self.page_count):
            return
        self.actions = page
        self.page_index = page_index
        self.page_count = page_count
        self._base = None
        self._hover = {}

    def _font_for(self, px, bold=False):
        key = (int(px), bold)
        if key not in self._fonts:
            self._fonts[key] = _font(key[0], bold)
        return self._fonts[key]

    # -- public ------------------------------------------------------------
    def image(self, hover=None):
        """Full RGBA frame. `hover` is a slice index or None."""
        if self._base is None:
            self._prerender()
        if hover is None or not self.actions:
            return self._base.copy()
        img = self._base.copy()
        img.alpha_composite(self._hover.get(hover, self._idle_glow))
        return img

    def slice_index(self, px, py, cx, cy):
        dx, dy = px - cx, py - cy
        if math.hypot(dx, dy) < self.inner or not self.actions:
            return None
        step = 360 / len(self.actions)
        theta = math.degrees(math.atan2(dx, -dy)) % 360
        return int(((theta + step / 2) % 360) // step)

    # -- internals ---------------------------------------------------------
    def _prerender(self):
        t = self.theme
        S = self.size * SS
        c = S / 2.0
        R, ri = self.R * SS, self.inner * SS
        corner = self.corner * SS
        n = len(self.actions)
        step = 360.0 / n if n else 360.0

        # 1. drop shadow of the whole silhouette
        shadow_src = Image.new("L", (S, S), 0)
        ds = ImageDraw.Draw(shadow_src)
        ds.ellipse([c - R, c - R, c + R, c + R], fill=255)
        ds.ellipse([c - ri, c - ri, c + ri, c + ri], fill=0)
        shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        shadow.putalpha(shadow_src.filter(ImageFilter.GaussianBlur(11 * SS / 2)))
        base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sh_col = _hex(t["shadow"])
        base.paste(Image.new("RGBA", (S, S), sh_col + (120,)), (0, 0),
                   shadow.getchannel("A"))

        # 2. slice shapes -> mask, then gradient-filled
        for i in range(n):
            a0 = i * step - step / 2 + self.gap / 2
            a1 = i * step + step / 2 - self.gap / 2
            m = Image.new("L", (S, S), 0)
            rounded_wedge(ImageDraw.Draw(m), c, c, ri, R, a0, a1, corner, 255)
            col = _gradient_mask(m, _hex(t["slice_top"]), _hex(t["slice_bottom"]), R, c)
            # thin outline
            out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            od = ImageDraw.Draw(out)
            rounded_wedge(od, c, c, ri, R, a0, a1, corner, _hex(t["slice_border"]) + (150,))
            col.alpha_composite(out)
            base.alpha_composite(col)

        # 3. hub
        hd = ImageDraw.Draw(base)
        hub_col = _hex(t["hub"])
        hd.ellipse([c - ri, c - ri, c + ri, c + ri], fill=hub_col + (255,),
                   outline=_hex(t["hub_border"]) + (255,), width=int(1.6 * SS))

        # 4. per-slice glow + brightened layer for hover states
        self._hover = {}
        idle = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        self._idle_glow = idle
        for i in range(n):
            a0 = i * step - step / 2 + self.gap / 2
            a1 = i * step + step / 2 - self.gap / 2
            layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            # glow behind
            gm = Image.new("L", (S, S), 0)
            rounded_wedge(ImageDraw.Draw(gm), c, c, ri, R + 3 * SS, a0, a1, corner, 255)
            glow = Image.new("RGBA", (S, S), _hex(t["glow"]) + (255,))
            glow.putalpha(gm.filter(ImageFilter.GaussianBlur(7 * SS)).point(lambda v: int(v * 0.75)))
            layer.alpha_composite(glow)
            # bright wedge
            m = Image.new("L", (S, S), 0)
            rounded_wedge(ImageDraw.Draw(m), c, c, ri, R, a0, a1, corner, 255)
            col = _gradient_mask(m, _hex(t["hover_top"]), _hex(t["hover_bottom"]), R, c)
            layer.alpha_composite(col)
            layer.alpha_composite(self._labels(i, c=c, R=R, ri=ri, step=step))
            self._hover[i] = layer

        # 5. idle labels + icons + hub content
        base.alpha_composite(self._labels(None, c=c, R=R, ri=ri, step=step))

        self._base = base.resize((self.size, self.size), Image.LANCZOS)
        self._hover = {k: v.resize((self.size, self.size), Image.LANCZOS)
                       for k, v in self._hover.items()}

    def _labels(self, hovered, c, R, ri, step):
        t = self.theme
        img = Image.new("RGBA", (self.size * SS, self.size * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # icon sits just inside the middle of the band, label just outside it,
        # both pushed along the slice's own radius so diagonal slices read right
        r_icon = ri + (R - ri) * 0.40
        r_label = ri + (R - ri) * 0.76
        # widest chord available at the label radius, for auto-shrinking text.
        # 0.78 not 1.0: the label is a horizontal box, so its corners would
        # otherwise poke past a diagonally-oriented wedge.
        chord = 2 * r_label * math.sin(math.radians(min(step, 90) / 2))
        max_w = chord * 0.78
        for i, a in enumerate(self.actions):
            on = (i == hovered)
            col = _hex(t["text"]) if on else _hex(t["text_dim"])
            ang = i * step

            icon_px = 27 if on else 24
            icon_size = icon_px * SS
            ix, iy = _polar(c, c, r_icon, ang)
            ic = draw_icon(icon_for(a), icon_px, col)
            img.alpha_composite(ic, (int(ix - icon_size / 2), int(iy - icon_size / 2)))

            if self.labels_on:
                label = a.get("label", "?")
                size = (15 if on else 13.5) * SS
                f = self._font_for(size, bold=on)
                # shrink until the label fits inside its wedge
                while size > 8.5 * SS:
                    bb = d.textbbox((0, 0), label, font=f)
                    if bb[2] - bb[0] <= max_w:
                        break
                    size -= SS
                    f = self._font_for(size, bold=on)
                lx, ly = _polar(c, c, r_label, ang)
                d.text((lx, ly), label, font=f, fill=col + (255,), anchor="mm")

        # hub content: hovered action's icon+label, else a hint.
        # The hub disc is repainted opaque so the idle hint never bleeds
        # through when this layer is composited over the base frame.
        ri_hub = self.inner * SS
        d.ellipse([c - ri_hub, c - ri_hub, c + ri_hub, c + ri_hub],
                  fill=_hex(t["hub"]) + (255,))
        if hovered is not None and hovered < len(self.actions):
            a = self.actions[hovered]
            col = _hex(t["text"])
            ic = draw_icon(icon_for(a), 30, col)
            img.alpha_composite(ic, (int(c - 15 * SS), int(c - 20 * SS)))
            d.text((c, c + 14 * SS), a.get("label", ""), font=self._font_for(14 * SS, True),
                   fill=col + (255,), anchor="mm")
            d.text((c, c + 28 * SS), "release to run", font=self._font_for(9.5 * SS),
                   fill=_hex(t["text_dim"]) + (255,), anchor="mm")
        else:
            ic = draw_icon("mouse", 20, _hex(t["text_dim"]))
            img.alpha_composite(ic, (int(c - 10 * SS), int(c - 21 * SS)))
            d.text((c, c + 6 * SS), "hold middle", font=self._font_for(9.5 * SS),
                   fill=_hex(t["text_dim"]) + (255,), anchor="mm")
            if self.page_count > 1:
                self._page_dots(d, c, ri_hub)
        return img

    def _page_dots(self, d, c, ri_hub):
        """Pagination indicator hugging the bottom of the hub.

        Filled dot is the current page, hollow rings are the rest. Shown only
        when there is more than one page, so a simple setup stays uncluttered.
        """
        n = self.page_count
        gap, r = 8 * SS, 2.7 * SS
        on = _hex(self.theme["accent"])
        off = _hex(self.theme["text_dim"])
        y = c + ri_hub - 10 * SS
        x0 = c - (n - 1) * gap / 2
        for i in range(n):
            x = x0 + i * gap
            box = [x - r, y - r, x + r, y + r]
            if i == self.page_index:
                d.ellipse(box, fill=on + (255,))
            else:
                d.ellipse(box, outline=off + (210,), width=max(1, int(1.2 * SS)))


def save_preview(path, cfg, hover=4):
    """Utility used by the README/tutorial: render a sample frame to disk."""
    Wheel(cfg).image(hover).save(path)
    return path
