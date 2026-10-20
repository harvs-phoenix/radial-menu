"""Radial menu - settings window.

Customising the wheel should not mean editing JSON by hand, so this is the UI
behind the tray's "Customise menu..." item: pages down the left, the actions
on the selected page on the right, and a small editor per action.

It runs entirely on the Tk main thread (the tray hands control over with
root.after). Every edit happens on a deep copy, so backing out with Cancel
leaves the running menu and the config file untouched.
"""

import copy
import io
import json
import os
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from PIL import Image, ImageTk

from actions import CONFIG_PATH, normalise, save_config
from render import ICONS, THEME_DEFAULT, Wheel, draw_icon, icon_for
from tutorial import (ACCENT, BG, CARD, CARD_HL, DIM, EDGE, FG, FONT,
                      _dark_titlebar)

#: A page can never show more wedges than this; the editor enforces it.
MAX_PER_PAGE = 12

# Display name -> (value stored in action["type"], which target editor to show).
# "scroll" variants carry dx/dy instead of a target string, so they get their
# own entries rather than a direction dropdown nobody would find.
TYPE_SPECS = [
    ("App",               "app",     "app"),
    ("Website",           "url",     "url"),
    ("Keyboard shortcut", "hotkey",  "keys"),
    ("Run a command",     "command", "cmd"),
    ("Type text",         "type",    "text"),
    ("Scroll up",         "scroll",  "up"),
    ("Scroll down",       "scroll",  "down"),
    ("Scroll left",       "scroll",  "left"),
    ("Scroll right",      "scroll",  "right"),
]
NAME_OF_TYPE = {t: n for n, t, _k in TYPE_SPECS}
TYPE_BY_NAME = {n: t for n, t, _k in TYPE_SPECS}
KIND_BY_NAME = {n: k for n, _t, k in TYPE_SPECS}

SCROLL_DELTA = {"up": ("dy", 1), "down": ("dy", -1),
                "left": ("dx", -1), "right": ("dx", 1)}

ICON_NAMES = ["auto"] + sorted(ICONS)

# Keys offered for paging. Escape is deliberately absent: radial._handle_key
# checks it first, so cancelling the menu always wins over a binding.
PAGE_KEY_CHOICES = (
    ("Right arrow", "right"), ("Left arrow", "left"),
    ("Up arrow", "up"), ("Down arrow", "down"),
    ("Tab", "tab"), ("Page Down", "page_down"), ("Page Up", "page_up"),
    ("Space", "space"), ("Home", "home"), ("End", "end"),
    ("Enter", "enter"), ("Insert", "insert"), ("Delete", "delete"),
    ("F5", "f5"), ("F6", "f6"), ("F7", "f7"),
)
KEY_LABEL = {name: label for label, name in PAGE_KEY_CHOICES}
KEY_NAME = {label: name for label, name in PAGE_KEY_CHOICES}

# Trigger buttons accepted by radial.BUTTONS. Kept local so settings.py does
# not have to import the app module (whose entry point is __main__ on a build).
TRIGGER_CHOICES = (
    ("Middle button", "middle"), ("Left button", "left"),
    ("Right button", "right"), ("Back (X1)", "x1"), ("Forward (X2)", "x2"),
)
TRIGGER_LABEL = {name: label for label, name in TRIGGER_CHOICES}
TRIGGER_NAME = {label: name for label, name in TRIGGER_CHOICES}

# Live preview: rendered at half scale so a radius change is visible without
# the preview growing off the tab (radius 80 -> 110px, 300 -> 330px).
PREVIEW_SCALE = 0.5
PREVIEW_BOX = 380

# Colours exposed in the UI: the ones whose effect is obvious at a glance.
# Glow/shadow/borders are left in the JSON for anyone who wants them.
COLOR_CHOICES = (
    ("hover_top", "Hovered slice (top)"),
    ("hover_bottom", "Hovered slice (bottom)"),
    ("slice_top", "Slice (top)"),
    ("slice_bottom", "Slice (bottom)"),
    ("text", "Label text"),
    ("text_dim", "Dim label text"),
    ("accent", "Accent / page dots"),
    ("hub", "Hub background"),
)

# One line per notebook tab, indexed the same way as _on_tab reads them.
TAB_SUBTITLES = (
    "Pages hold up to %d actions each. Scroll, Tab or the arrow keys move "
    "between them while the menu is open." % MAX_PER_PAGE,
    "Size, shape and colours for the ring. The preview redraws as you drag - "
    "nothing applies until you press Save.",
    "Which keys turn pages, and which mouse button opens the menu.",
)


def _type_name(action):
    """Display name for a stored action.

    Scroll is the one type with no 1:1 name: four TYPE_SPECS entries write the
    same "type": "scroll", so a plain type->name lookup always returns the last
    one ("Scroll right"). The direction is recoverable from dx/dy's sign, so
    resolve it from there instead of trusting the map.
    """
    atype = action.get("type", "")
    if atype == "scroll":
        dx, dy = action.get("dx") or 0, action.get("dy") or 0
        if dx:
            return "Scroll right" if dx > 0 else "Scroll left"
        return "Scroll up" if dy >= 0 else "Scroll down"
    return NAME_OF_TYPE.get(atype, atype or "?")


def _summarise(action):
    """One-line list entry: 'Copy   ·   Keyboard shortcut   ·   ctrl+c'."""
    label = action.get("label") or "(no label)"
    atype = action.get("type", "")
    kind = _type_name(action)
    extra = action.get("target", "")
    if atype == "scroll":
        extra = " ".join(
            "%s %s" % (k, action[k]) for k in ("dx", "dy") if action.get(k))
    extra = str(extra)
    if len(extra) > 40:
        extra = extra[:37] + "..."
    return "%s    ·    %s%s" % (label, kind, "    ·    " + extra if extra else "")


def _scrollbar(parent, command):
    """A scrollbar that belongs to the dark chrome.

    Deliberately ttk, not tk: on this Windows build Tk's classic Scrollbar is
    painted from the native visual theme and ignores -bg/-troughcolor
    outright - it reports bg=#1e2330 while a #f0f0f0 track is on screen (the
    neighbouring Listbox and Frame *do* capture correctly, so it is the
    widget, not the capture). ttk.Scrollbar under a configured style renders
    the colour it is given.
    """
    st = ttk.Style(parent)
    st.configure("TScrollbar", background=CARD_HL, troughcolor=BG,
                 borderwidth=0, relief="flat", lightcolor=BG, darkcolor=BG,
                 arrowcolor=DIM)
    # pressed/active is the only time ACCENT appears, same as the sliders
    st.map("TScrollbar",
           background=[("pressed", ACCENT), ("active", ACCENT)],
           arrowcolor=[("active", "#ffffff")])
    return ttk.Scrollbar(parent, command=command, orient="vertical",
                         takefocus=0)


class SettingsWindow:
    """Pages on the left, actions on the right, editor behind a double-click."""

    def __init__(self, master, cfg, on_save=None):
        self.root = tk.Toplevel(master)
        self.root.title("Radial Menu - Settings")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.on_save = on_save

        # work on a copy so Cancel means Cancel
        radial = normalise(copy.deepcopy(cfg)).get("radial", {})
        self.pages = radial.get("pages", [{"name": "Main", "actions": []}]) or \
            [{"name": "Main", "actions": []}]
        self.page_var = tk.IntVar(value=0)

        # Appearance and controls live as Tk vars: every one of them is bound
        # straight to the live preview, so there is no separate model to keep
        # in sync and no apply step before you can see the result.
        theme = dict(THEME_DEFAULT)
        theme.update(radial.get("theme") or {})
        self.color_vars = {k: tk.StringVar(value=theme.get(k, THEME_DEFAULT[k]))
                           for k, _label in COLOR_CHOICES}
        self.radius_var = tk.IntVar(value=int(radial.get("radius", 150)))
        self.inner_var = tk.IntVar(value=int(radial.get("inner_radius", 52)))
        self.corner_var = tk.IntVar(value=int(radial.get("corner_radius", 9)))
        self.gap_var = tk.DoubleVar(value=float(radial.get("gap_degrees", 3.0)))
        self.labels_var = tk.BooleanVar(value=bool(radial.get("show_labels", True)))
        self._swatch_btns = {}

        bindings = radial.get("bindings") or {}
        self.next_key_var = tk.StringVar(value=KEY_LABEL.get(
            (bindings.get("next_page") or ["right"])[0], "Right arrow"))
        self.prev_key_var = tk.StringVar(value=KEY_LABEL.get(
            (bindings.get("prev_page") or ["left"])[0], "Left arrow"))
        self.trigger_var = tk.StringVar(value=TRIGGER_LABEL.get(
            radial.get("trigger_button", "middle"), "Middle button"))

        self._build()
        self._centre()
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)
        # DWM only repaints the non-client area if the frame is already there
        # to invalidate, so the dark title bar has to wait until the window is
        # actually mapped - calling it inline here left a light frame.
        self.root.after(1, lambda: _dark_titlebar(self.root))
        # Deliberately NOT transient(master): the Tk root is withdraw()n (the
        # wheel is drawn by the overlay, not Tk), and a Toplevel tied to a
        # withdrawn master stays unmapped forever. tutorial.py does the same.
        # Focus is taken on a tick so the window exists before we grab it.
        self.root.after(20, self.root.focus_force)

    # -- layout ------------------------------------------------------------
    def _build(self):
        r = self.root

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(head, text="Customise your menu", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(anchor="w")
        # the subtitle tracks whichever tab is showing, so the window always
        # says what you are looking at rather than always describing page one
        self.subtitle = tk.Label(head, text="", bg=BG, fg=DIM, font=(FONT, 9),
                                 wraplength=720, justify="left")
        self.subtitle.pack(anchor="w", pady=(3, 0))

        self.nb = ttk.Notebook(r)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self._style_notebook()

        self._build_menu_tab()
        self._build_appearance_tab()
        self._build_controls_tab()
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab)
        self._on_tab()

        # Footer sits outside the notebook so all three tabs share one
        # Save/Cancel pair - per-tab footers invite the "did that tab save?" doubt.
        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 16))
        self.status = tk.Label(foot, text="", bg=BG, fg=DIM, font=(FONT, 9))
        self.status.pack(side="left")
        self._button(foot, "Cancel", self._cancel).pack(side="right", padx=(8, 0))
        self._button(foot, "Save", self._save, primary=True).pack(side="right")

        self._refresh_pages()
        self._refresh_actions()

    def _build_menu_tab(self):
        body = tk.Frame(self.nb, bg=BG)
        self.nb.add(body, text="  Menu  ")

        # -- pages column -------------------------------------------------
        left = tk.Frame(body, bg=BG, highlightthickness=1,
                        highlightbackground=EDGE)
        left.pack(side="left", fill="y", padx=(0, 12))
        tk.Label(left, text="PAGES", bg=CARD, fg=DIM, font=(FONT, 8, "bold"),
                 anchor="w", padx=10, pady=6).pack(fill="x")

        mid = tk.Frame(left, bg=CARD)
        mid.pack(fill="both", expand=True)
        self.page_list = tk.Listbox(mid, width=20, height=12, bg=CARD, fg=FG,
                                    selectbackground=ACCENT,
                                    selectforeground="#ffffff",
                                    highlightthickness=0, borderwidth=0,
                                    selectmode="browse",
                                    font=(FONT, 10), activestyle="none")
        self.page_list.pack(side="left", fill="both", expand=True, padx=8,
                            pady=8)
        sb = _scrollbar(mid, self.page_list.yview)
        sb.pack(side="right", fill="y")
        self.page_list.config(yscrollcommand=sb.set)
        self.page_list.bind("<<ListboxSelect>>", self._on_page_selected)
        self.page_list.bind("<Double-Button-1>", lambda e: self._rename_page())

        bar = tk.Frame(left, bg=CARD)
        bar.pack(fill="x", pady=(0, 8))
        for text, cmd in (("+", self._add_page), ("Rename", self._rename_page),
                          ("-", self._delete_page)):
            self._button(bar, text, cmd, small=True).pack(side="left",
                                                          padx=(8, 4), expand=True,
                                                          fill="x")

        # -- actions column ----------------------------------------------
        right = tk.Frame(body, bg=BG, highlightthickness=1,
                         highlightbackground=EDGE)
        right.pack(side="left", fill="both", expand=True)
        self.actions_title = tk.Label(right, text="ACTIONS", bg=CARD, fg=DIM,
                                      font=(FONT, 8, "bold"), anchor="w",
                                      padx=10, pady=6)
        self.actions_title.pack(fill="x")

        mid2 = tk.Frame(right, bg=CARD)
        mid2.pack(fill="both", expand=True)
        # browse mode keeps this to exactly one selected row, which is what
        # _edit/_delete/_move rely on when they read curselection()[0]
        self.action_list = tk.Listbox(mid2, width=52, height=12, bg=CARD, fg=FG,
                                      selectbackground=ACCENT,
                                      selectforeground="#ffffff",
                                      highlightthickness=0, borderwidth=0,
                                      selectmode="browse",
                                      font=(FONT, 10), activestyle="none")
        self.action_list.pack(side="left", fill="both", expand=True, padx=8,
                              pady=8)
        sb2 = _scrollbar(mid2, self.action_list.yview)
        sb2.pack(side="right", fill="y")
        self.action_list.config(yscrollcommand=sb2.set)
        self.action_list.bind("<Double-Button-1>", lambda e: self._edit_action())
        self.action_list.bind("<Delete>", lambda e: self._delete_action())

        bar2 = tk.Frame(right, bg=CARD)
        bar2.pack(fill="x", pady=(0, 8))
        for text, cmd in (("+ Add", self._add_action), ("Edit", self._edit_action),
                          ("-", self._delete_action), ("^", self._move_up),
                          ("v", self._move_down)):
            self._button(bar2, text, cmd, small=True).pack(
                side="left", padx=(8, 4), expand=True, fill="x")

    def _build_appearance_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Appearance  ")

        # Preview column first: _refresh_preview keys off its existence, and
        # the sliders below prime their readouts as they are built.
        right = tk.Frame(tab, bg=BG)
        right.pack(side="right", fill="y", padx=(0, 14), pady=14)
        tk.Label(right, text="LIVE PREVIEW", bg=BG, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        self.preview_canvas = tk.Canvas(right, width=PREVIEW_BOX,
                                        height=PREVIEW_BOX, bg="#0a0c12",
                                        highlightthickness=1,
                                        highlightbackground=EDGE)
        self.preview_canvas.pack(pady=(6, 8))
        # drag a slider and the canvas never changes size, but a window manager
        # may; re-centre rather than leaving the ring pinned to a stale middle
        self.preview_canvas.bind("<Configure>", lambda _e: self._refresh_preview())
        self.preview_caption = tk.Label(right, text="", bg=BG, fg=DIM,
                                        font=(FONT, 9))
        self.preview_caption.pack()

        left = tk.Frame(tab, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=14, pady=14)

        tk.Label(left, text="SIZE", bg=BG, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        self._slider(left, "Wheel radius", self.radius_var, 80, 300, "px")
        self._slider(left, "Hub radius", self.inner_var, 24, 120, "px")
        self._slider(left, "Corner roundness", self.corner_var, 0, 30, "")
        self._slider(left, "Slice gap", self.gap_var, 0.0, 15.0, "\u00b0",
                     res=0.5)

        tk.Checkbutton(left, text="Show labels", variable=self.labels_var,
                       command=self._refresh_preview, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG,
                       activeforeground=FG, highlightthickness=0,
                       font=(FONT, 9), anchor="w").pack(fill="x", pady=(8, 0))

        tk.Label(left, text="COLOURS", bg=BG, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w", pady=(16, 0))
        grid = tk.Frame(left, bg=BG)
        grid.pack(fill="x")
        for i, (key, label) in enumerate(COLOR_CHOICES):
            self._swatch(grid, key, label, i // 2, i % 2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

    def _slider(self, parent, label, var, lo, hi, unit="", res=None):
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", pady=(8, 0))
        tk.Label(head, text=label, bg=BG, fg=FG, font=(FONT, 9)).pack(side="left")
        readout = tk.Label(head, text="", bg=BG, fg=ACCENT,
                           font=(FONT, 9, "bold"))
        readout.pack(side="right")

        opts = dict(from_=lo, to=hi, orient="horizontal", variable=var,
                    length=300, sliderlength=14, showvalue=False,
                    # relief flat, not raised: Tk derives a raised handle's
                    # highlight from -bg, so a near-black bg produced a #87888b
                    # bevel that read as a leftover native control. A flat
                    # accent block on the CARD trough is on-palette instead.
                    bg=ACCENT, fg=DIM, troughcolor=CARD, relief="flat",
                    borderwidth=0, bd=0, highlightthickness=0,
                    activebackground="#8fb2ff",
                    command=lambda _v: self._on_slider(var, readout, unit))
        if res is not None:
            opts["resolution"] = res
        tk.Scale(parent, **opts).pack(fill="x", pady=(2, 0))
        self._on_slider(var, readout, unit)      # prime the readout

    def _on_slider(self, var, readout, unit):
        v = var.get()
        if isinstance(v, float) and not float(v).is_integer():
            text = "%.1f%s" % (v, unit)
        else:
            text = "%d%s" % (int(round(v)), unit)
        readout.config(text=text)
        self._refresh_preview()

    def _swatch(self, parent, key, label, row, col):
        cell = tk.Frame(parent, bg=BG)
        cell.grid(row=row, column=col, sticky="w", padx=(0, 12), pady=3)
        var = self.color_vars[key]
        # activebackground must track the colour too, or the swatch flashes the
        # default grey on hover and looks broken.
        btn = tk.Button(cell, width=4, bg=var.get(), activebackground=var.get(),
                        relief="flat", bd=0, cursor="hand2",
                        command=lambda k=key: self._pick_color(k))
        btn.pack(side="left", padx=(0, 8), ipady=6)
        tk.Label(cell, text=label, bg=BG, fg=DIM, font=(FONT, 9)).pack(side="left")
        self._swatch_btns[key] = btn

    def _pick_color(self, key):
        var = self.color_vars[key]
        got = colorchooser.askcolor(color=var.get(), parent=self.root,
                                    title="Choose a colour")
        if got and got[1]:
            var.set(got[1])
            self._swatch_btns[key].config(bg=got[1], activebackground=got[1])
            self._refresh_preview()

    def _build_controls_tab(self):
        tab = tk.Frame(self.nb, bg=BG)
        self.nb.add(tab, text="  Controls  ")

        col = tk.Frame(tab, bg=BG)
        col.pack(anchor="nw", padx=18, pady=18)

        tk.Label(col, text="TURN PAGES", bg=BG, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        names = [label for label, _name in PAGE_KEY_CHOICES]
        self._choice(col, "Next page", self.next_key_var, names)
        self._choice(col, "Previous page", self.prev_key_var, names)
        tk.Label(col, text="Right and left arrows are the defaults.\n"
                           "Tab, PageDown and PageUp always turn pages too, and\n"
                           "scrolling the wheel works while the menu is held.",
                 bg=BG, fg=DIM, font=(FONT, 8), justify="left",
                 anchor="w").pack(anchor="w", pady=(6, 0))

        tk.Label(col, text="OPEN THE MENU", bg=BG, fg=DIM,
                 font=(FONT, 8, "bold")).pack(anchor="w", pady=(18, 0))
        self._choice(col, "Hold this button", self.trigger_var,
                     [label for label, _name in TRIGGER_CHOICES])
        tk.Label(col, text="Side buttons X1 / X2 work too, if your mouse has them.",
                 bg=BG, fg=DIM, font=(FONT, 8), justify="left",
                 anchor="w").pack(anchor="w", pady=(6, 0))

        # An info note, not decoration: fg=CARD_HL on bg=BG was the same
        # colour family as the background and rendered the text invisible.
        tk.Label(col, text="The ring always opens centred on the pointer, so there\n"
                           "is nothing to aim at - just slide into a slice.",
                 bg=CARD, fg=DIM, font=(FONT, 9), justify="left", anchor="w",
                 padx=14, pady=12, highlightthickness=1,
                 highlightbackground=EDGE).pack(anchor="w", pady=(20, 0),
                                                fill="x")

    def _choice(self, parent, label, var, values):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=BG, fg=FG, font=(FONT, 9), width=16,
                 anchor="w").pack(side="left")
        cb = ttk.Combobox(row, textvariable=var, values=values,
                          state="readonly", width=24, font=(FONT, 10))
        cb.pack(side="left", ipady=3)
        return cb

    def _style_notebook(self):
        """Dark tabs, combos and scrollbars.

        Two theme choices, both forced deliberately:

        * **alt**, not clam. clam bakes a 2px near-white ring into its
          Notebook.client element, and every style knob that sounds like it
          should remove it (TNotebook borderwidth, Notebook.client
          borderwidth/relief, client padding, layout border) leaves the ring
          exactly where it is - the page stays inset by 2px either way. alt
          insets by 0, so the page covers the edge and the ring is gone.
        * The native "vista" theme (Tk's default here) draws the tab strip
          itself and ignores every colour below, so the switch has to happen
          before anything is configured.
        """
        st = ttk.Style(self.root)
        for theme in ("alt", "clam"):       # alt is the pick; clam is the floor
            try:
                st.theme_use(theme)
                break
            except tk.TclError:
                continue
        st.configure("TNotebook", background=BG, borderwidth=0, relief="flat",
                     tabmargins=(0, 0, 0, 0))
        # each layer of a tab carries its own edge; zeroing them keeps a
        # theme's default hairline from showing between adjacent tabs. Only
        # geometry here - setting background on these would risk out-ranking
        # the selected-tab colour and flatten the whole strip.
        for el in ("Notebook.tab", "Notebook.padding", "Notebook.focus",
                   "Notebook.label", "TNotebook.client"):
            st.configure(el, borderwidth=0, relief="flat")
        st.configure("TNotebook.Tab", background=CARD, foreground=DIM,
                     padding=(16, 8), font=(FONT, 10, "bold"), borderwidth=0,
                     relief="flat", focuscolor=BG)
        st.map("TNotebook.Tab",
               background=[("selected", CARD_HL), ("!selected", CARD)],
               foreground=[("selected", FG), ("!selected", DIM)],
               expand=[("selected", 1)])
        st.configure("TCombobox", fieldbackground=CARD, background=CARD,
                     foreground=FG, borderwidth=0, lightcolor=BG,
                     arrowcolor=FG, insertcolor=FG)
        st.map("TCombobox",
               fieldbackground=[("readonly", CARD)],
               foreground=[("readonly", FG)],
               selectbackground=[("readonly", CARD)],
               selectforeground=[("readonly", FG)],
               arrowcolor=[("readonly", FG)])

    def _on_tab(self, _event=None):
        try:
            i = self.nb.index(self.nb.select())
        except (tk.TclError, ValueError):
            i = 0
        self.subtitle.config(text=TAB_SUBTITLES[min(i, len(TAB_SUBTITLES) - 1)])
        if i == 1:
            # the canvas had no size (and a half-built widget tree) while the
            # tab was hidden; redraw it now that it is on screen
            self._refresh_preview()

    # -- live appearance preview -----------------------------------------
    def _preview_cfg(self):
        """Geometry + colours only: enough for Wheel to render the ring."""
        return {
            "radius": int(self.radius_var.get()),
            "inner_radius": int(self.inner_var.get()),
            "corner_radius": int(self.corner_var.get()),
            "gap_degrees": float(self.gap_var.get()),
            "show_labels": bool(self.labels_var.get()),
            "theme": {k: v.get() for k, v in self.color_vars.items()},
        }

    def _preview_actions(self):
        """Mirror the page being edited, else the first one with actions."""
        i = self.page_var.get()
        filled = [p for p in self.pages if p.get("actions")]
        if not filled:
            return []
        if 0 <= i < len(self.pages) and self.pages[i].get("actions"):
            page = self.pages[i]
        else:
            page = filled[0]
        return list(page["actions"])[:Wheel.MAX_ACTIONS]

    def _refresh_preview(self):
        if not getattr(self, "preview_canvas", None):
            return                          # menu/controls tab built first
        try:
            actions = self._preview_actions()
            img = Wheel(self._preview_cfg(), actions).image(
                0 if actions else None)     # slice 0 hovered: the gradient
                                            # most of the colours control
            box = max(1, int(img.width * PREVIEW_SCALE))
            img = img.resize((box, box), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            # PhotoImage must stay referenced or Tk drops it and the canvas
            # renders blank on the next redraw
            self._preview_img = ImageTk.PhotoImage(data=buf.getvalue())
            c = self.preview_canvas
            c.delete("all")
            c.create_image(max(1, c.winfo_width() // 2),
                           max(1, c.winfo_height() // 2),
                           image=self._preview_img)
            r = int(self.radius_var.get())
            n = len(actions)
            self.preview_caption.config(
                fg=DIM,
                text="Radius %dpx  \u00b7  wheel \u00d8%dpx  \u00b7  %d slice%s"
                     % (r, (r + 30) * 2, n, "" if n == 1 else "s"))
        except Exception as exc:
            # Not swallowed silently: a broken preview must be *visible*, not
            # a mysteriously blank box (see draw_icon's old except: pass).
            self.preview_caption.config(fg="#ff8f8f",
                                        text="Preview unavailable: %s" % exc)

    def _button(self, parent, text, cmd, small=False, primary=False):
        bg = ACCENT if primary else CARD_HL
        b = tk.Button(parent, text=text, command=cmd, bg=bg,
                      fg="#ffffff" if primary else FG,
                      activebackground=ACCENT if primary else EDGE,
                      activeforeground="#ffffff", relief="flat",
                      bd=0, padx=10 if small else 16, pady=5 if small else 8,
                      font=(FONT, 9 if small else 10),
                      cursor="hand2")
        return b

    # -- page handling ----------------------------------------------------
    def _refresh_pages(self):
        self.page_list.delete(0, "end")
        for p in self.pages:
            self.page_list.insert(
                "end", "%s   (%d)" % (p.get("name", "Page"),
                                      len(p.get("actions", []))))
        if self.pages:
            sel = min(self.page_var.get(), len(self.pages) - 1)
            self.page_var.set(sel)
            self.page_list.selection_clear(0, "end")
            self.page_list.selection_set(sel)
            self.page_list.see(sel)

    def _on_page_selected(self, _event=None):
        sel = self.page_list.curselection()
        if sel:
            self.page_var.set(sel[0])
            self._refresh_actions()

    def _add_page(self):
        name = self._ask_string("New page", "Page name:", "Page %d" % (len(self.pages) + 1))
        if not name:
            return
        self.pages.append({"name": name, "actions": []})
        self.page_var.set(len(self.pages) - 1)
        self._refresh_pages()
        self._refresh_actions()

    def _rename_page(self):
        i = self.page_var.get()
        if not (0 <= i < len(self.pages)):
            return
        name = self._ask_string("Rename page", "Page name:",
                                self.pages[i].get("name", "Page"))
        if not name:
            return
        self.pages[i]["name"] = name
        self._refresh_pages()

    def _delete_page(self):
        i = self.page_var.get()
        if not (0 <= i < len(self.pages)):
            return
        if len(self.pages) == 1:
            messagebox.showinfo("Radial Menu", "You need at least one page.",
                                parent=self.root)
            return
        n = len(self.pages[i].get("actions", []))
        if not messagebox.askyesno(
                "Delete page",
                "Delete '%s' and its %d action(s)?" % (self.pages[i].get("name"), n),
                parent=self.root):
            return
        del self.pages[i]
        self.page_var.set(max(0, i - 1))
        self._refresh_pages()
        self._refresh_actions()

    def _current_page(self):
        i = self.page_var.get()
        if 0 <= i < len(self.pages):
            return self.pages[i]
        return None

    # -- action handling --------------------------------------------------
    def _refresh_actions(self):
        self.action_list.delete(0, "end")
        page = self._current_page()
        if page is None:
            self.actions_title.config(text="ACTIONS")
            return
        self.actions_title.config(text="ACTIONS ON '%s'" % page.get("name", "").upper())
        for a in page.get("actions", []):
            self.action_list.insert("end", _summarise(a))

    def _add_action(self):
        page = self._current_page()
        if page is None:
            return
        if len(page.setdefault("actions", [])) >= MAX_PER_PAGE:
            messagebox.showinfo(
                "Radial Menu",
                "A page holds at most %d actions. Add another page instead."
                % MAX_PER_PAGE, parent=self.root)
            return
        result = ActionDialog(self.root, None).result
        if result is None:
            return
        page["actions"].append(result)
        self._refresh_pages()
        self._refresh_actions()

    def _edit_action(self):
        page = self._current_page()
        sel = self.action_list.curselection()
        if page is None or not sel:
            return
        i = sel[0]
        result = ActionDialog(self.root, page["actions"][i]).result
        if result is None:
            return
        page["actions"][i] = result
        self._refresh_pages()
        self._refresh_actions()
        self.action_list.selection_set(i)

    def _delete_action(self):
        page = self._current_page()
        sel = self.action_list.curselection()
        if page is None or not sel:
            return
        del page["actions"][sel[0]]
        self._refresh_pages()
        self._refresh_actions()

    def _move(self, step):
        page = self._current_page()
        sel = self.action_list.curselection()
        if page is None or not sel:
            return
        acts = page["actions"]
        i = sel[0]
        j = i + step
        if not (0 <= j < len(acts)):
            return
        acts[i], acts[j] = acts[j], acts[i]
        self._refresh_actions()
        self.action_list.selection_set(j)
        self.action_list.see(j)

    def _move_up(self):
        self._move(-1)

    def _move_down(self):
        self._move(1)

    # -- persistence ------------------------------------------------------
    def _save(self):
        if not self.pages:
            messagebox.showwarning("Radial Menu", "Keep at least one page.",
                                   parent=self.root)
            return
        cfg = normalise(self._cfg_out())
        try:
            path = save_config(cfg)
        except OSError as e:
            messagebox.showerror("Radial Menu", "Could not save:\n%s" % e,
                                 parent=self.root)
            return
        if self.on_save:
            try:
                self.on_save(cfg)
            except Exception as e:      # a bad callback must not lose the save
                self.status.config(text="Saved, but reload failed: %s" % e,
                                   fg="#ff8f8f")
                return
        self.status.config(text="Saved to %s" % path, fg=DIM)
        self.root.after(600, self.root.destroy)

    def _cfg_out(self):
        """Build the config dict to write, preserving everything else.

        Re-reads the file rather than reusing the live dict so keys the
        editor does not know about (dock settings, theme, ...) survive.
        """
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        radial = cfg.setdefault("radial", {})
        radial["pages"] = copy.deepcopy(self.pages)
        # keep the legacy flat list pointed at page 0 for older readers
        radial["actions"] = copy.deepcopy(self.pages[0].get("actions", []))

        # -- appearance ----------------------------------------------------
        # Merged, not replaced: glow, shadow and borders stay in the file so
        # the JSON-only knobs keep working alongside the sliders.
        radial["radius"] = int(self.radius_var.get())
        radial["inner_radius"] = int(self.inner_var.get())
        radial["corner_radius"] = int(self.corner_var.get())
        radial["gap_degrees"] = float(self.gap_var.get())
        radial["show_labels"] = bool(self.labels_var.get())
        theme = dict(radial.get("theme") or {})
        for key, var in self.color_vars.items():
            theme[key] = var.get()
        radial["theme"] = theme

        # -- controls ------------------------------------------------------
        radial["bindings"] = {
            "next_page": [KEY_NAME.get(self.next_key_var.get(), "right")],
            "prev_page": [KEY_NAME.get(self.prev_key_var.get(), "left")],
        }
        radial["trigger_button"] = TRIGGER_NAME.get(self.trigger_var.get(),
                                                    "middle")
        return cfg

    def _cancel(self):
        self.root.destroy()

    # -- small helpers ----------------------------------------------------
    def _ask_string(self, title, prompt, initial=""):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=CARD)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text=prompt, bg=CARD, fg=FG,
                 font=(FONT, 10)).pack(padx=18, pady=(14, 6), anchor="w")
        var = tk.StringVar(value=initial)
        ent = tk.Entry(dlg, textvariable=var, bg=BG, fg=FG, insertbackground=FG,
                       relief="flat", bd=0, font=(FONT, 11), width=30)
        ent.pack(padx=18, pady=(0, 12), ipady=6)
        out = []

        def ok(_e=None):
            out.append(var.get().strip())
            dlg.destroy()

        ent.bind("<Return>", ok)
        tk.Button(dlg, text="OK", command=ok, bg=ACCENT, fg="#ffffff",
                  relief="flat", bd=0, padx=14, pady=5, font=(FONT, 10)).pack(
            padx=18, pady=(0, 14), anchor="e")
        self._centre_on(dlg)
        ent.focus_force()
        self.root.wait_window(dlg)
        return out[0] if out else None

    def _centre(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(0, min(sw - w, (sw - w) // 2))
        y = max(0, min(sh - h, (sh - h) // 3))
        self.root.geometry("+%d+%d" % (x, y))

    def _centre_on(self, win):
        win.update_idletasks()
        pw, ph = self.root.winfo_rootx(), self.root.winfo_rooty()
        pw2, ph2 = self.root.winfo_width(), self.root.winfo_height()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry("+%d+%d" % (pw + (pw2 - w) // 2, ph + (ph2 - h) // 2))


class ActionDialog:
    """Modal editor for a single action. `.result` is the dict, or None."""

    def __init__(self, master, action):
        self.result = None
        self.win = tk.Toplevel(master)
        self.win.title("Edit action")
        self.win.configure(bg=CARD)
        self.win.resizable(False, False)
        self.win.transient(master)
        self.win.grab_set()

        a = action or {}
        self.is_new = action is None
        self.label_var = tk.StringVar(value=a.get("label", ""))
        start_name = _type_name(a) if a.get("type") else "App"
        self.kind_name = tk.StringVar(value=start_name)
        self.target_var = tk.StringVar(value=a.get("target", ""))
        self.icon_var = tk.StringVar(value=a.get("icon") or "auto")
        self.steps_var = tk.StringVar(
            value=str(abs(int(a.get("dy", a.get("dx", 1)) or 1))))

        self._build()
        self._sync_kind()
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.bind("<Return>", lambda e: self._ok())
        self._centre()
        self.label_entry.focus_force()
        master.wait_window(self.win)

    def _build(self):
        w = self.win
        pad = dict(padx=16, pady=4, anchor="w")

        tk.Label(w, text="New action" if self.is_new else "Edit action",
                 bg=CARD, fg=FG,
                 font=(FONT, 13, "bold")).pack(padx=16, pady=(14, 8), anchor="w")

        tk.Label(w, text="Label", bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(**pad)
        self.label_entry = tk.Entry(w, textvariable=self.label_var, bg=BG, fg=FG,
                                    insertbackground=FG, relief="flat", bd=0,
                                    font=(FONT, 11), width=34)
        self.label_entry.pack(padx=16, pady=(0, 8), ipady=5, fill="x")

        tk.Label(w, text="What it does", bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(**pad)
        self.kind_box = ttk.Combobox(w, textvariable=self.kind_name,
                                     values=[n for n, _t, _k in TYPE_SPECS],
                                     state="readonly", width=32, font=(FONT, 10))
        self.kind_box.pack(padx=16, pady=(0, 8), ipady=3, fill="x")
        self.kind_box.bind("<<ComboboxSelected>>", lambda e: self._sync_kind())

        tk.Label(w, text="Target", bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(**pad)
        row = tk.Frame(w, bg=CARD)
        row.pack(padx=16, pady=(0, 8), fill="x")
        self.target_entry = tk.Entry(row, textvariable=self.target_var, bg=BG,
                                     fg=FG, insertbackground=FG, relief="flat",
                                     bd=0, font=(FONT, 11))
        self.target_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.browse_btn = tk.Button(row, text="Browse...", command=self._browse,
                                    bg=CARD_HL, fg=FG, relief="flat", bd=0,
                                    padx=10, pady=5, font=(FONT, 9),
                                    cursor="hand2")
        self.browse_btn.pack(side="left", padx=(8, 0))

        self.hint = tk.Label(w, text="", bg=CARD, fg=DIM, font=(FONT, 8),
                             justify="left", wraplength=380)
        self.hint.pack(padx=16, pady=(0, 8), anchor="w")

        # Scroll actions have steps rather than a target, so this row only
        # appears for them - showing an always-disabled target box instead
        # would just look broken.
        self.steps_row = tk.Frame(w, bg=CARD)
        tk.Label(self.steps_row, text="Steps", bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(side="left")
        tk.Entry(self.steps_row, textvariable=self.steps_var, bg=BG, fg=FG,
                 insertbackground=FG, relief="flat", bd=0, font=(FONT, 11),
                 width=6).pack(side="left", padx=(10, 0), ipady=4)

        tk.Label(w, text="Icon", bg=CARD, fg=DIM,
                 font=(FONT, 9)).pack(**pad)
        irow = tk.Frame(w, bg=CARD)
        irow.pack(padx=16, pady=(0, 12), fill="x")
        self.icon_box = ttk.Combobox(irow, textvariable=self.icon_var,
                                     values=ICON_NAMES, width=20, font=(FONT, 10))
        self.icon_box.pack(side="left", ipady=3)
        self.icon_box.bind("<<ComboboxSelected>>", lambda e: self._preview())
        self.preview = tk.Canvas(irow, width=34, height=34, bg=CARD,
                                 highlightthickness=0)
        self.preview.pack(side="left", padx=(12, 0))

        foot = tk.Frame(w, bg=CARD)
        foot.pack(fill="x", padx=16, pady=(0, 14))
        tk.Button(foot, text="Cancel", command=self._cancel, bg=CARD_HL, fg=FG,
                  relief="flat", bd=0, padx=14, pady=7, font=(FONT, 10)).pack(
            side="right", padx=(8, 0))
        tk.Button(foot, text="OK", command=self._ok, bg=ACCENT, fg="#ffffff",
                  relief="flat", bd=0, padx=16, pady=7, font=(FONT, 10)).pack(
            side="right")
        self._preview()

    def _sync_kind(self):
        """Show only the controls this action type actually needs."""
        name = self.kind_name.get()
        kind = KIND_BY_NAME.get(name, "app")
        self._kind = kind
        if kind.startswith("scroll"):
            self.target_entry.config(state="disabled")
            self.target_var.set("")
            self.hint.config(text="Scrolls %s by 'Steps' clicks."
                             % name.split()[-1].lower())
            self.steps_row.pack(padx=16, pady=(0, 8), anchor="w", after=self.hint)
        else:
            self.target_entry.config(state="normal")
            hints = {
                "app":   "Program to launch, e.g. notepad.exe or a full path.",
                "url":   "Web address, e.g. https://github.com",
                "keys":  "Shortcut to press, e.g. ctrl+shift+v",
                "cmd":   "Shell command to run.",
                "text":  "Text typed wherever the cursor is.",
            }
            self.hint.config(text=hints.get(kind, ""))
            self.steps_row.pack_forget()
        # a browse dialog only makes sense when the target is a file path
        self.browse_btn.config(state="normal" if kind == "app" else "disabled")
        self._preview()

    def _browse(self):
        if self._kind != "app":
            return
        path = filedialog.askopenfilename(
            parent=self.win, title="Choose a program",
            filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if path:
            self.target_var.set(path)
            if not self.label_var.get().strip():
                # a sensible default label saves the user typing it twice
                self.label_var.set(os.path.splitext(os.path.basename(path))[0])

    def _preview(self):
        """Redraw the little icon chip next to the picker."""
        self.preview.delete("all")
        tmp = {"label": self.label_var.get() or "Action",
               "type": TYPE_BY_NAME.get(self.kind_name.get(), "app"),
               "target": self.target_var.get()}
        if self.icon_var.get() != "auto":
            tmp["icon"] = self.icon_var.get()
        try:
            ic = draw_icon(icon_for(tmp), 26, FG)
            buf = io.BytesIO()
            ic.save(buf, "PNG")
            # PhotoImage has to be kept alive or Tk drops the image
            self._img = ImageTk.PhotoImage(data=buf.getvalue())
            self.preview.create_image(17, 17, image=self._img)
        except Exception:
            pass

    def _ok(self):
        label = self.label_var.get().strip()
        kind_name = self.kind_name.get()
        atype = TYPE_BY_NAME.get(kind_name, "app")
        kind = KIND_BY_NAME.get(kind_name, "app")
        target = self.target_var.get().strip()

        if not label:
            messagebox.showwarning("Radial Menu", "Give the action a label.",
                                   parent=self.win)
            return
        if kind.startswith("scroll"):
            axis, sign = SCROLL_DELTA[kind]
            try:
                steps = max(1, int(self.steps_var.get()))
            except ValueError:
                steps = 1
            action = {"label": label, "type": "scroll", axis: sign * steps}
        else:
            if not target:
                messagebox.showwarning(
                    "Radial Menu",
                    "This action needs a %s." %
                    {"app": "program", "url": "web address",
                     "keys": "shortcut", "cmd": "command",
                     "text": "text"}.get(kind, "target"), parent=self.win)
                return
            action = {"label": label, "type": atype, "target": target}

        if self.icon_var.get() != "auto":
            action["icon"] = self.icon_var.get()
        self.result = action
        self.win.destroy()

    def _cancel(self):
        self.win.destroy()

    def _centre(self):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry("+%d+%d" % (max(0, (sw - w) // 2),
                                      max(0, (sh - h) // 3)))


def open_settings(master, cfg, on_save=None):
    """Convenience entry point used by radial.main()."""
    return SettingsWindow(master, cfg, on_save)
