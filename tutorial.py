"""First-run onboarding for the radial menu.

A dark, styled Tk window that walks through the gesture in three steps and
offers a live "try it" so you can practise the hold-slide-release before you
rely on it. Rendered icons come from render.py so the tutorial looks exactly
like the menu it is teaching.
"""

import base64
import io
import json
import tkinter as tk

from actions import CONFIG_PATH
from render import draw_icon, icon_for

BG = "#0e1016"
CARD = "#171a24"
CARD_HL = "#1e2330"
FG = "#e9edf7"
DIM = "#8d95ab"
ACCENT = "#5b8cff"
ACCENT2 = "#8b5cf6"
EDGE = "#262c3c"

FONT = "Segoe UI"


def _dark_titlebar(widget):
    """Force the window's title bar dark so it matches the themed body.

    Tk only exposes this from 8.6.12 and only via a private flag, so we talk
    to DWM directly: attribute 20 is the Windows 10 20H1+/11 spelling and
    attribute 19 the older 1809 one.

    Setting the attribute alone is not enough - DWM keeps painting the old
    non-client area until the frame is invalidated, so the window has to be
    kicked with SWP_FRAMECHANGED afterwards or the bar stays light.
    """
    try:
        import ctypes
        from ctypes import wintypes as wt

        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi

        # winfo_id() is a child wrapper; the frame DWM cares about is the root.
        hwnd = user32.GetAncestor(user32.GetParent(widget.winfo_id()), 2)  # GA_ROOT
        if not hwnd:
            hwnd = widget.winfo_id()

        applied = False
        for attr in (20, 19):
            value = ctypes.c_int(1)
            if dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                applied = True
                break
        if not applied:
            try:
                widget.attributes("-darktitlebar", True)
            except Exception:
                pass
            return False

        # Invalidate the non-client area so the dark bar actually appears.
        SWP_NOMOVE, SWP_NOSIZE, SWP_FRAMECHANGED = 0x0002, 0x0001, 0x0020
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def _photo(name, size, color=(140, 170, 255)):
    """Icon -> Tk PhotoImage (PNG through base64, works on Tk 8.6)."""
    img = draw_icon(name, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


class Tutorial(tk.Toplevel):
    """Three-step onboarding. Calls `on_done()` when finished or skipped."""

    def __init__(self, master, wheel_cfg, on_done=None, on_practice=None):
        super().__init__(master)
        self.wheel_cfg = wheel_cfg
        self.on_done = on_done
        self.on_practice = on_practice
        self.step = 0
        self._imgs = []  # keep references so Tk doesn't garbage collect them

        self.title("Radial Menu - Quick Start")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._finish)
        # DWM only honours the dark-mode hint once the window is mapped and has
        # its non-client area built. after_idle() is not enough here: _centre()
        # calls update_idletasks(), which flushes that callback while the
        # window is still unmapped, so the hint lands on a frame that is then
        # rebuilt light. Retry on timers until the bar has actually changed.
        for delay in (80, 600, 1200):
            self.after(delay, lambda: _dark_titlebar(self))

        self._build()
        self._show_step(0)
        self._centre()
        # safety net: a resolution/monitor change mid-tutorial otherwise
        # leaves the window somewhere the user cannot reach
        self.after(1500, self._keep_on_screen)

        self.bind("<Escape>", lambda e: self._finish())
        self.bind("<Right>", lambda e: self._next())
        self.bind("<Left>", lambda e: self._prev())
        self.after(20, self.focus_force)

    # -- layout ------------------------------------------------------------
    def _build(self):
        wrap = tk.Frame(self, bg=BG, padx=28, pady=24)
        wrap.pack(fill="both", expand=True)

        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x")
        self._imgs.append(_photo("mouse", 30, (139, 160, 255)))
        tk.Label(head, image=self._imgs[-1], bg=BG).pack(side="left")
        tk.Label(head, text="  Radial Menu", bg=BG, fg=FG,
                 font=(FONT, 17, "bold")).pack(side="left")
        tk.Label(wrap, text="A pie menu under your middle mouse button.",
                 bg=BG, fg=DIM, font=(FONT, 10)).pack(anchor="w", pady=(2, 0))

        self.body = tk.Frame(wrap, bg=BG)
        self.body.pack(fill="both", expand=True, pady=(16, 10))

        nav = tk.Frame(wrap, bg=BG)
        nav.pack(fill="x")
        self.dots = tk.Frame(nav, bg=BG)
        self.dots.pack(side="left")
        self._dot_vars = []
        for i in range(3):
            v = tk.Label(self.dots, text="●", bg=BG, fg=EDGE, font=(FONT, 9))
            v.pack(side="left", padx=3)
            self._dot_vars.append(v)

        self.back_btn = self._btn(nav, "Back", self._prev, primary=False)
        self.next_btn = self._btn(nav, "Next", self._next, primary=True)
        self.skip_btn = self._btn(nav, "Skip", self._finish, primary=False)

    def _btn(self, parent, text, cmd, primary=True):
        b = tk.Label(parent, text=text, bg=ACCENT if primary else CARD,
                     fg=FG if primary else DIM, font=(FONT, 10, "bold"),
                     padx=16, pady=7, cursor="hand2",
                     relief="flat", bd=0)
        b.pack(side="right", padx=(8, 0))
        b.bind("<Button-1>", lambda e: cmd())
        for ev in ("<Enter>", "<Leave>"):
            b.bind(ev, lambda e, b=b, p=primary: self._hover_btn(b, p, e))
        return b

    @staticmethod
    def _hover_btn(b, primary, e):
        if e.type == "2":
            b.configure(bg=ACCENT2 if primary else CARD_HL,
                        fg=FG if primary else FG)
        else:
            b.configure(bg=ACCENT if primary else CARD,
                        fg=FG if primary else DIM)

    def _card(self, icon, title, body, extra=None):
        c = tk.Frame(self.body, bg=CARD, highlightbackground=EDGE,
                     highlightthickness=1, padx=20, pady=18)
        c.pack(fill="both", expand=True)
        self._imgs.append(_photo(icon, 46, (120, 155, 255)))
        tk.Label(c, image=self._imgs[-1], bg=CARD).pack(anchor="w")
        tk.Label(c, text=title, bg=CARD, fg=FG, font=(FONT, 14, "bold")
                 ).pack(anchor="w", pady=(12, 4))
        tk.Label(c, text=body, bg=CARD, fg=DIM, font=(FONT, 10),
                 justify="left", wraplength=380).pack(anchor="w")
        if extra:
            extra(c)
        return c

    # -- steps -------------------------------------------------------------
    def _show_step(self, i):
        for w in self.body.winfo_children():
            w.destroy()
        self.step = i
        for j, d in enumerate(self._dot_vars):
            d.configure(fg=ACCENT if j == i else EDGE)

        if i == 0:
            def chips(c):
                actions = self.wheel_cfg.get("actions", [])[:12]
                row = tk.Frame(c, bg=CARD)
                row.pack(anchor="w", pady=(14, 0))
                for a in actions[:6]:
                    self._imgs.append(_photo(icon_for(a), 18, (175, 195, 255)))
                    tk.Label(row, image=self._imgs[-1], bg=CARD).pack(side="left", padx=(0, 10))
            self._card("mouse",
                       "Hold, slide, release",
                       "Hold the middle mouse button anywhere and a menu of your "
                       "actions appears around the pointer. Slide towards the one "
                       "you want and let go - that's it.",
                       chips)
        elif i == 1:
            def rows(c):
                for label, detail in (
                    ("Scroll Up / Down", "mouse wheel, no clicking needed"),
                    ("Copy / Paste / Undo", "one gesture instead of three chords"),
                    ("Left / Right", "horizontal scroll for wide sheets"),
                ):
                    r = tk.Frame(c, bg=CARD_HL, padx=10, pady=7)
                    r.pack(fill="x", pady=(0, 6))
                    tk.Label(r, text=label, bg=CARD_HL, fg=FG,
                             font=(FONT, 10, "bold")).pack(anchor="w")
                    tk.Label(r, text=detail, bg=CARD_HL, fg=DIM,
                             font=(FONT, 9)).pack(anchor="w")
            self._card("clipboard",
                       "Slices are yours to change",
                       "Every slice comes from config.json. Add, remove or reorder "
                       "them, and each one takes an icon automatically from its "
                       "action type.",
                       rows)
        else:
            def keys(c):
                for label, detail in (
                    ("1 - 9", "pick a slice by number, hands off the mouse"),
                    ("Esc", "close the menu without running anything"),
                    ("Middle-click in the centre", "cancel, just like Esc"),
                ):
                    r = tk.Frame(c, bg=CARD_HL, padx=10, pady=7)
                    r.pack(fill="x", pady=(0, 6))
                    tk.Label(r, text=label, bg=CARD_HL, fg=ACCENT,
                             font=(FONT, 10, "bold")).pack(side="left")
                    tk.Label(r, text=detail, bg=CARD_HL, fg=DIM,
                             font=(FONT, 9)).pack(side="left", padx=(12, 0))
            self._card("check",
                       "You're ready",
                       "Try the gesture now if you like, or just start using it. "
                       "You can reopen this guide any time by adding a "
                       "\"tutorial\" action to a slice.",
                       keys)

        self.back_btn.configure(state="normal" if i > 0 else "disabled")
        self.next_btn.configure(text="Done" if i == 2 else "Next")

    # -- nav ---------------------------------------------------------------
    def _next(self):
        if self.step >= 2:
            self._finish()
        else:
            self._show_step(self.step + 1)

    def _prev(self):
        if self.step > 0:
            self._show_step(self.step - 1)

    def _finish(self):
        self._mark_seen()
        self.destroy()
        if self.on_done:
            self.on_done()

    def _mark_seen(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            tut = cfg.setdefault("tutorial", {})
            tut["seen"] = True
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except (OSError, ValueError):
            pass

    def _centre(self):
        """Place the window, clamped so it can never land off-screen.

        The display mode can change while this window is up (monitor swap,
        resolution or DPI change), and an unclamped offset then strands it
        somewhere the user cannot see or click. Clamping keeps it reachable
        whatever the screen reports.
        """
        self.update_idletasks()
        w = self.winfo_width() or self.winfo_reqwidth()
        h = self.winfo_height() or self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, min(sw - w, (sw - w) // 2))
        y = max(0, min(sh - h, (sh - h) // 3))
        self.geometry(f"+{x}+{y}")

    def _keep_on_screen(self, n=0):
        """Re-place the window if a display change pushed it out of view.

        Runs a bounded number of times: this is a safety net for mode
        switches, not a hook that fights the user while they drag it about.
        """
        if not self.winfo_exists():
            return
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w, h = self.winfo_width(), self.winfo_height()
            x, y = self.winfo_x(), self.winfo_y()
            if x + w <= 0 or y + h <= 0 or x >= sw or y >= sh:
                self._centre()
        except tk.TclError:
            return
        if n < 20:
            self.after(1000, self._keep_on_screen, n + 1)


def practice(root, overlay, wheel_cfg, done=None):
    """Show the real wheel centred on the screen so the gesture can be practised.

    The wheel is opened through the live Radial controller, so every slice runs
    its real action - which is the whole point of a practice run.
    """
    import time
    from render import Wheel

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    x, y = sw // 2, sh // 2

    class _Practice:
        """Minimal stand-in exposing the bits _tick() needs."""

        def __init__(self):
            self.wheel = Wheel(wheel_cfg)
            self.actions = wheel_cfg.get("actions", [])[:12]
            self.ov = overlay
            self._open = True
            self._hover = None
            self._center = (x, y)
            self._shown_at = time.perf_counter()

        def close(self):
            self._open = False
            self.ov.hide()

    return _Practice()
