import json
import os
import shutil
import subprocess
import sys
import webbrowser

from pynput.keyboard import Controller as KeyController, Key
from pynput.mouse import Controller as MouseController

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
# In a built exe, put config.json next to the .exe so it stays editable;
# in dev, config.json just lives beside the scripts.
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else CODE_DIR
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
_keys = KeyController()
_mouse = MouseController()

SPECIAL = {
    "ctrl": Key.ctrl, "shift": Key.shift, "alt": Key.alt, "win": Key.cmd,
    "enter": Key.enter, "esc": Key.esc, "tab": Key.tab, "space": Key.space,
    "backspace": Key.backspace, "delete": Key.delete,
    "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
    "home": Key.home, "end": Key.end, "pageup": Key.page_up, "pagedown": Key.page_down,
}
for _i in range(1, 13):
    SPECIAL[f"f{_i}"] = getattr(Key, f"f{_i}")


def load_config():
    """Find config.json next to the exe (or the script when running from source),
    copying the bundled copy out on first run of a built exe."""
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        bundled = os.path.join(getattr(sys, "_MEIPASS", CODE_DIR), "config.json")
        if os.path.exists(bundled):
            try:
                shutil.copyfile(bundled, path)
            except OSError:
                path = bundled  # exe folder not writable: fall back to the temp copy
        else:
            path = bundled
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    """Persist the whole config back next to the exe/script."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return CONFIG_PATH


def pages_of(radial_cfg):
    """The wheel's pages, as a list of {"name", "actions"} dicts.

    Older configs stored one flat "actions" list; treat that as a single
    page so an existing setup keeps working untouched.
    """
    pages = radial_cfg.get("pages")
    if pages:
        # drop anything malformed rather than crashing the whole menu
        return [p for p in pages if isinstance(p, dict)]
    return [{"name": "Main", "actions": radial_cfg.get("actions", [])}]


def normalise(cfg):
    """Ensure cfg["radial"] carries a "pages" list and a sane "actions".

    Called before saving so the file on disk is always in the new shape,
    but "actions" is kept pointing at page 0 for anything older that reads it.
    """
    radial = cfg.setdefault("radial", {})
    radial["pages"] = pages_of(radial)
    if radial["pages"]:
        radial["actions"] = radial["pages"][0].get("actions", [])
    return cfg


def press_combo(combo):
    """Press keys like 'ctrl+shift+v' together, then release them."""
    keys = [SPECIAL.get(p.strip().lower(), p.strip()) for p in combo.split("+")]
    for k in keys:
        _keys.press(k)
    for k in reversed(keys):
        _keys.release(k)


def run_action(action):
    kind = action.get("type", "")
    target = action.get("target", "").replace("{dir}", BASE_DIR)
    if kind == "app":
        os.startfile(target)
    elif kind == "url":
        webbrowser.open(target)
    elif kind == "command":
        subprocess.Popen(target, shell=True)
    elif kind in ("hotkey", "key"):
        press_combo(target)
    elif kind == "scroll":
        _mouse.scroll(action.get("dx", 0), action.get("dy", 0))
    elif kind == "type":
        _keys.type(target)
