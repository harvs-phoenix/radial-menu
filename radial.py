"""Radial menu - main app.

Runs a background control loop (pynput listener + Win32 overlay) while Tk owns
the main thread for the tutorial/onboarding window. Tk is never used to draw
the wheel itself - that goes through the per-pixel-alpha layered window in
overlay.py, which is why it looks crisp over any wallpaper.
"""

import ctypes
import ctypes.wintypes as wintypes
import math
import os
import queue
import sys
import tempfile
import threading
import time

from pynput import keyboard, mouse
from PIL import Image

from actions import BASE_DIR, load_config, normalise, pages_of, run_action
from overlay import Overlay
from render import Wheel

_user32 = ctypes.windll.user32

# Errors have to go somewhere readable: --noconsole leaves sys.stderr as None
# on Windows, so a plain stderr write would raise inside the very handler
# meant to catch failures.
LOG_PATH = os.path.join(
    BASE_DIR if os.access(BASE_DIR, os.W_OK) else tempfile.gettempdir(),
    "radial_menu.log")


def _log(msg):
    """Append a line to the log file.

    stderr is unusable in a --noconsole build (PyInstaller sets it to None on
    Windows), so a background app needs somewhere its history can be read.
    """
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _crash(tag, exc):
    """Record an unhandled exception with its traceback.

    A --noconsole build gives no stderr, so without this a fault in a worker
    thread simply kills the process and leaves no evidence behind at all.
    """
    try:
        import traceback
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n=== %s %s: unhandled %s ===\n%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), tag,
                type(exc).__name__, "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__))))
    except Exception:
        pass


def _install_crash_hooks():
    """Route every unhandled exception - main thread or worker - to the log."""
    _orig_thread = threading.excepthook
    _orig_main = sys.excepthook

    def thread_hook(args):
        try:
            _crash("thread %s" % getattr(args.thread, "name", "?"), args.exc_value)
        finally:
            _orig_thread(args)

    def main_hook(t, v, tb):
        try:
            _crash("main thread", v)
        finally:
            _orig_main(t, v, tb)

    threading.excepthook = thread_hook
    sys.excepthook = main_hook


def _cursor():
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y

BUTTONS = {
    "middle": mouse.Button.middle,
    "left": mouse.Button.left,
    "right": mouse.Button.right,
    "x1": mouse.Button.x1,
    "x2": mouse.Button.x2,
}

ENTRY_MS = 130        # entry animation length - entry only, exit is instant
TICK = 1 / 120        # control loop rate


def _ease_out(t):
    return 1 - (1 - t) ** 3


#: Keys that always turn pages regardless of configuration. The primary keys
#: are the user's to change in the settings window; these are the discoverable
#: fallbacks that were documented first, so customising one direction does not
#: silently take them away.
ALWAYS_NEXT = ("tab", "page_down")
ALWAYS_PREV = ("page_up",)


def _resolve_keys(names):
    """Map config key names to pynput Key members, dropping unknown ones.

    A typo'd binding has to degrade to "that key does nothing" rather than
    raise: _handle_key runs on the keyboard listener thread, and an exception
    there would kill input for the whole menu.
    """
    out = set()
    for n in names or ():
        k = getattr(keyboard.Key, str(n).strip().lower(), None)
        if k is not None:
            out.add(k)
    return out


class Radial:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pages = pages_of(cfg) or [{"name": "Main", "actions": []}]
        self._page = 0
        self.actions = []
        self.trigger = BUTTONS.get(cfg.get("trigger_button", "middle"), mouse.Button.middle)
        self.wheel = Wheel(cfg)
        self.ov = Overlay()
        self._rebuild_wheel()
        self._apply_bindings()

        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._running = True
        self._open = False
        self.paused = False          # tray toggle: ignores the trigger entirely
        self._center = (0, 0)
        self._hover = None
        self._shown_at = 0.0

        # on_scroll lets a held wheel page through the actions without
        # letting go - which is the whole point of a menu under your finger.
        self._listener = mouse.Listener(on_click=self._on_click,
                                        on_scroll=self._on_scroll)
        self._listener.daemon = True
        self._listener.start()

        self._keys = keyboard.Listener(on_press=self._on_key)
        self._keys.daemon = True
        self._keys.start()

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _rebuild_wheel(self):
        """Point the renderer and the action list at the current page.

        Called from __init__ and again on every page turn / config reload, so
        it must only touch rendering state - the listeners and the control
        thread belong to __init__ alone and are started exactly once.
        """
        page = self.pages[self._page] if self.pages else {"actions": []}
        self.actions = page.get("actions", [])[:Wheel.MAX_ACTIONS]
        self.wheel.set_actions(self.actions, self._page, len(self.pages))

    def _apply_bindings(self):
        """Rebuild the paging key sets from the radial config.

        Defaults are right/left, so paging works out of the box; the always-on
        fallbacks are unioned in afterwards. A key picked for *both* directions
        would otherwise make paging ambiguous - next wins, and the loser is
        removed so the precedence is visible in the data rather than accidental.
        """
        b = self.cfg.get("bindings") or {}
        nxt = _resolve_keys(b.get("next_page", ["right"]))
        prv = _resolve_keys(b.get("prev_page", ["left"]))
        nxt |= _resolve_keys(ALWAYS_NEXT)
        prv |= _resolve_keys(ALWAYS_PREV)
        prv -= nxt
        self.next_keys, self.prev_keys = nxt, prv

    # -- input -------------------------------------------------------------
    def _on_click(self, x, y, button, pressed):
        # An uncaught exception here kills pynput's listener thread, which
        # would silently disable the whole menu - so it is guarded too.
        try:
            if self.paused or button != self.trigger:
                return
            _log("click %s at %d,%d (menu %s)" % (
                "down" if pressed else "up", x, y,
                "open" if self._open else "closed"))
            self._q.put(("press" if pressed else "release", x, y))
        except Exception as e:
            self._report(e, 0.0)

    def _on_scroll(self, x, y, dx, dy):
        """Turn pages while the menu is open, without letting go.

        The two axes deliberately use opposite signs. Vertical dy>0 means the
        wheel was rolled up, which scrolls a document backwards, so it goes to
        the previous page. Horizontal dx>0 is a swipe to the right, which in
        reading order advances, so it goes to the next page. Collapsing both
        into one sign (as an earlier version did) made a trackpad swipe to the
        right walk the pages backwards.
        """
        try:
            if self.paused or not self._open or len(self.pages) < 2:
                return
            if dy:
                delta = -1 if dy > 0 else 1
            elif dx:
                delta = 1 if dx > 0 else -1
            else:
                return                      # both axes zero: nothing happened
            self._q.put(("page", delta, 0))
        except Exception as e:
            self._report(e, 0.0)

    def _on_key(self, key):
        try:
            self._handle_key(key)
        except Exception as e:
            self._report(e, 0.0)

    def _handle_key(self, key):
        if not self._open:
            return
        if key == keyboard.Key.esc:
            self._q.put(("cancel", 0, 0))
            return
        # paging: the primary key is configurable, the fallbacks are fixed.
        # Checked before the digit branch so a binding always takes priority.
        if key in self.next_keys or key in self.prev_keys:
            if len(self.pages) > 1:
                self._q.put(("page", 1 if key in self.next_keys else -1, 0))
            return
        # number row + numpad select a slice directly
        char = getattr(key, "char", None)
        vk = getattr(key, "vk", None)
        idx = None
        if char and char.isdigit() and char != "0":
            idx = int(char) - 1
        elif vk is not None and 96 <= vk <= 105:  # numpad 0-9
            idx = vk - 97
        if idx is not None and 0 <= idx < len(self.actions):
            self._q.put(("jump", idx, 0))

    # -- control loop ------------------------------------------------------
    def _loop(self):
        """Input/render loop.

        This must never die: it is a daemon thread driving the whole menu, and
        if it raises once the wheel would silently stop responding forever
        (with --noconsole there is no traceback either). Each event and each
        frame is therefore guarded individually, so one bad frame costs at most
        that frame.
        """
        last_err = 0.0
        while self._running:
            try:
                while True:
                    kind, x, y = self._q.get_nowait()
                    try:
                        if kind == "press":
                            self._show(x, y)
                        elif kind == "release":
                            self._release(x, y)
                        elif kind == "cancel":
                            self._close(None)
                        elif kind == "jump":
                            self._close(self.actions[x])
                        elif kind == "page":
                            self._page_turn(x)
                    except Exception as e:
                        self._report(e, last_err)
                        last_err = time.monotonic()
            except queue.Empty:
                pass

            try:
                with self._lock:
                    if self._open:
                        self._tick()
            except Exception as e:
                # a failed frame must not leave the overlay half-drawn
                self._report(e, last_err)
                last_err = time.monotonic()
                try:
                    self._open = False
                    self._hover = None
                    self.ov.hide()
                except Exception:
                    pass
            time.sleep(TICK)

    @staticmethod
    def _report(exc, last, throttle=5.0):
        """Log a control-loop error to a file, throttled.

        stderr is not usable here: with --noconsole PyInstaller sets
        sys.stderr to None on Windows, so writing to it would raise. A
        background app needs somewhere it can actually be read.
        """
        now = time.monotonic()
        if now - last < throttle:
            return
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("%s %s: %s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), type(exc).__name__, exc))
        except OSError:
            pass

    def _show(self, x, y):
        if self._open:
            return
        if not any(p.get("actions") for p in self.pages):
            return
        # Landing on an empty page would show a wheel with nothing on it, so
        # jump to the first page that actually has actions.
        if not self.actions:
            for i, page in enumerate(self.pages):
                if page.get("actions"):
                    self._page = i
                    self._rebuild_wheel()
                    break
        self._center = (x, y)
        self._hover = None
        self._shown_at = time.perf_counter()
        self._open = True

    def _page_turn(self, delta):
        """Wrap to the adjacent page while the menu stays open."""
        if len(self.pages) < 2:
            return
        self._page = (self._page + delta) % len(self.pages)
        self._rebuild_wheel()
        _log("page %d/%d (%s)" % (
            self._page + 1, len(self.pages),
            self.pages[self._page].get("name", "")))

    def apply_config(self, cfg):
        """Re-read the radial section after the settings window saved it.

        Called on the Tk thread while the control thread may be mid-frame, so
        everything shared is swapped inside one lock acquisition - _tick reads
        self.wheel twice (once for size, once to render), and a half-applied
        swap would let it pair the old size with the new wheel. Only the
        pure-cpu Wheel construction happens outside the lock.
        """
        new_cfg = cfg.get("radial", {})
        new_wheel = Wheel(new_cfg)
        new_pages = pages_of(new_cfg) or [{"name": "Main", "actions": []}]
        with self._lock:
            self.cfg = new_cfg
            self.trigger = BUTTONS.get(new_cfg.get("trigger_button", "middle"),
                                       mouse.Button.middle)
            self.wheel = new_wheel
            self.pages = new_pages
            self._page = min(self._page, len(new_pages) - 1)
            self._rebuild_wheel()
            self._apply_bindings()
        _log("config reloaded: %d page(s), %d action(s) on page %d" % (
            len(self.pages), len(self.actions), self._page + 1))

    def _tick(self):
        cx, cy = self._center
        px, py = _cursor()
        size = self.wheel.size
        left, top = cx - size // 2, cy - size // 2

        # entry animation: scale + fade in. The scaled wheel is composited
        # onto a full-size canvas so the overlay's DIB never has to be
        # reallocated mid-animation (resizing the frame instead would shrink
        # the buffer and the next full-size blit would overflow it).
        t = (time.perf_counter() - self._shown_at) * 1000 / ENTRY_MS
        if t < 1:
            k = _ease_out(max(0.0, t))
            scale = 0.82 + 0.18 * k
            alpha = int(255 * k)
            frame = self.wheel.image(self._hover)
            if scale < 0.999:
                s = max(8, int(size * scale))
                small = frame.resize((s, s), Image.BILINEAR)
                canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                canvas.alpha_composite(small, ((size - s) // 2, (size - s) // 2))
                frame = canvas
            if alpha < 255:
                frame.putalpha(frame.getchannel("A").point(lambda v: v * alpha // 255))
            self.ov.move(frame, left, top)
            return

        idx = self.wheel.slice_index(px, py, size / 2 + left, size / 2 + top)
        frame = self.wheel.image(idx)
        self._hover = idx
        self.ov.move(frame, left, top)

    def _release(self, x, y):
        if not self._open:
            return
        idx = self.wheel.slice_index(x, y, self._center[0], self._center[1])
        action = self.actions[idx] if idx is not None else None
        self._close(action)

    def _close(self, action):
        if not self._open:
            return
        self._open = False
        self._hover = None
        self.ov.hide()
        if action is not None:
            threading.Thread(target=run_action, args=(action,), daemon=True).start()

    # -- lifecycle ---------------------------------------------------------
    def stop(self):
        self._running = False
        try:
            self._listener.stop()
            self._keys.stop()
        except Exception:
            pass
        self.ov.destroy()

def main():
    import tkinter as tk

    from actions import load_config
    from render import tray_icon
    from settings import open_settings
    from tray import Tray
    from tutorial import Tutorial

    _install_crash_hooks()

    cfg = load_config()
    radial_cfg = cfg.get("radial", {})
    if not radial_cfg.get("enabled", True):
        _log("radial disabled in config, exiting")
        return

    root = tk.Tk()
    root.withdraw()  # Tk only owns the loop; the wheel is drawn by the overlay

    app = Radial(radial_cfg)
    _log("started: trigger=%s pages=%d actions=%d log=%s" % (
        radial_cfg.get("trigger_button", "middle"), len(app.pages),
        len(app.actions), LOG_PATH))

    def show_tutorial():
        Tutorial(root, radial_cfg, on_done=None)

    # first run, or explicitly asked for via config
    if not cfg.get("tutorial", {}).get("seen", False):
        root.after(300, show_tutorial)

    def toggle_enabled():
        app.paused = not app.paused
        _log("menu %s" % ("paused" if app.paused else "resumed"))
        tray.set_tip("Radial Menu (paused)" if app.paused else "Radial Menu")

    # Only ever one settings window: the tray gets clicked repeatedly and a
    # stack of identical editors is more confusing than helpful.
    _settings = {"win": None}

    def show_settings():
        win = _settings.get("win")
        if win is not None and win.root.winfo_exists():
            win.root.lift()
            win.root.focus_force()
            return
        _settings["win"] = open_settings(root, cfg, on_save=app.apply_config)

    # The tray is what makes this a background app rather than a stray
    # process: it shows the menu is running, reopens the tutorial, and gives
    # the user a way out that isn't Task Manager. It is created on the Tk
    # thread so Tk's message loop dispatches its window messages.
    tray = Tray(
        tray_icon(32),
        tip="Radial Menu",
        on_tutorial=lambda: root.after(0, show_tutorial),
        on_quit=lambda: root.after(0, root.destroy),
        on_toggle=lambda: root.after(0, toggle_enabled),
        on_settings=lambda: root.after(0, show_settings),
        is_enabled=lambda: not app.paused)
    _log("tray: hwnd=%s added=%s icon=%s" % (
        tray._hwnd, tray._added, tray._icon))

    # A heartbeat proves the Tk loop is actually pumping: if the process dies
    # these lines simply stop, which separates "loop never started" from
    # "loop ran and something killed it".
    def heartbeat(n=0):
        _log("alive %d" % n)
        root.after(3000, heartbeat, n + 1)

    root.after(3000, heartbeat)

    try:
        root.mainloop()
        # Only reachable if something destroyed the root window. Without this
        # line a normal exit and a killed process look identical in the log.
        _log("mainloop returned - root window was destroyed, exiting")
    finally:
        try:
            tray.remove()
        except Exception:
            pass
        app.stop()

if __name__ == "__main__":
    main()
