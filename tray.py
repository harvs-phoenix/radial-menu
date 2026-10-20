"""System tray presence for the radial menu.

A background app with no notify icon is unfinished: there is nothing to show
it is running, nothing to reopen the tutorial, and no way to quit it other
than Task Manager. This gives it a proper tray icon with a right-click menu.

Everything here is raw Win32 through ctypes rather than an extra package, so
the frozen build needs no new dependency. The callback types are spelled out
explicitly: on 64-bit Windows a default ctypes int is 32 bits, and passing a
truncated HWND or returning a truncated LRESULT through a window procedure is
exactly the class of access violation that killed earlier builds.
"""

import ctypes
import io

from ctypes import wintypes as wt

# Deliberately a private WinDLL handle instead of ctypes.windll: those objects
# are cached per-process, so declaring argtypes here would silently overwrite
# what overlay.py declared for the same exports. RegisterClassW takes a
# different WNDCLASSW layout in each module, and the clash made the wheel
# fail to open with "expected LP_WNDCLASSW instance instead of pointer".
# A separate handle keeps the two modules' declarations independent.
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- Win32 types ----------------------------------------------------------
# WPARAM is unsigned, LPARAM/LRESULT are signed. Naming them out loud keeps
# the LLP64 sizes straight and stops a 64-bit handle being truncated.
LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, WPARAM, LPARAM)

WM_APP = 0x8000
TRAY_MSG = WM_APP + 1        # private message the tray sends us

# Shell_NotifyIcon
NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
NIM_SETVERSION = 0x4
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NOTIFYICON_VERSION = 3

# window messages we care about
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002

# popup menu
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTALIGN = 0x0008

ID_TUTORIAL = 1001
ID_ENABLED = 1002
ID_QUIT = 1003
ID_SETTINGS = 1004


class NOTIFYICONDATAW(ctypes.Structure):
    """Vista-and-later layout, which Windows 10/11 accept via cbSize."""

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


# --- declaring argtypes ---------------------------------------------------
# ctypes defaults every argument to a 32-bit int. On Win64 that silently
# truncates handles, so each of these is spelled out.
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
# GetModuleHandleW is a kernel32 export, not user32 - binding it to user32
# raises AttributeError at import time.
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wt.HWND,
                                  ctypes.POINTER(wt.RECT)]
user32.TrackPopupMenu.restype = ctypes.c_uint
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadCursorW.restype = wt.HANDLE
user32.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, WPARAM, LPARAM]
user32.SendMessageW.restype = LRESULT

# Icon resource helpers: without argtypes the buffer pointer and the DWORD
# length are both truncated, and CreateIconFromResourceEx silently returns None.
_UB = ctypes.POINTER(ctypes.c_ubyte)
user32.LookupIconIdFromDirectoryEx.argtypes = [_UB, wt.BOOL, ctypes.c_int,
                                               ctypes.c_int, wt.UINT]
user32.LookupIconIdFromDirectoryEx.restype = ctypes.c_int
user32.CreateIconFromResourceEx.argtypes = [_UB, wt.DWORD, wt.BOOL, wt.DWORD,
                                            ctypes.c_int, ctypes.c_int, wt.UINT]
user32.CreateIconFromResourceEx.restype = wt.HICON

shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL

IDC_ARROW = 32512


def _hicon_from_pil(img, size):
    """Turn a PIL RGBA image into an HICON, entirely in memory.

    Going through an ICO resource is the reliable route: CreateIconIndirect
    would mean hand-building a DIB and a mask bitmap, and any mistake there is
    an access violation rather than a Python exception.
    """
    try:
        ico = img.convert("RGBA").resize((size, size))
        buf = io.BytesIO()
        ico.save(buf, format="ICO", sizes=[(size, size)])
        data = buf.getvalue()

        arr = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        off = user32.LookupIconIdFromDirectoryEx(
            arr, True, size, size, 0)
        if not off:
            return None
        remaining = len(data) - off
        # from_buffer() needs a writable buffer, and bytes is immutable, so
        # this has to be a copy or it raises TypeError.
        return user32.CreateIconFromResourceEx(
            (ctypes.c_ubyte * remaining).from_buffer_copy(data[off:]),
            remaining, True, 0x00030000, 0, 0, 0)
    except Exception:
        return None


class Tray:
    """Notify icon with a right-click menu.

    Created on the Tk thread so Tk's own message loop dispatches the window
    messages; the control thread deliberately pumps nothing.
    """

    def __init__(self, icon_image, tip="Radial Menu",
                 on_tutorial=None, on_quit=None, on_toggle=None,
                 on_settings=None, is_enabled=None):
        self.on_tutorial = on_tutorial
        self.on_quit = on_quit
        self.on_toggle = on_toggle
        self.on_settings = on_settings
        self.is_enabled = is_enabled or (lambda: True)
        self._hwnd = None
        self._added = False
        self._icon = None
        self._tip = tip
        self._wndproc_ref = None      # keep the callback alive for Win32
        self._class_ref = None
        try:
            self._icon = _hicon_from_pil(icon_image, 32)
            self._create(tip)
        except Exception:
            # A tray failure must never take the menu down with it.
            self._hwnd = None

    # -- construction ------------------------------------------------------
    def _create(self, tip):
        hinst = kernel32.GetModuleHandleW(None)

        def wndproc(hwnd, msg, wp, lp):
            try:
                if msg == TRAY_MSG:
                    self._on_tray(wp, lp)
                    return 0
                if msg == WM_COMMAND:
                    self._on_command(wp & 0xFFFF)
                    return 0
                if msg == WM_DESTROY:
                    return 0
            except Exception:
                pass
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        # The callback has to outlive this method or Windows ends up calling
        # into freed memory.
        self._wndproc_ref = WNDPROC(wndproc)

        cls = WNDCLASSW()
        cls.lpfnWndProc = self._wndproc_ref
        cls.hInstance = hinst
        cls.hCursor = user32.LoadCursorW(None, ctypes.cast(IDC_ARROW, wt.LPCWSTR))
        cls.lpszClassName = "RadialMenuTrayClass"
        self._class_ref = cls
        user32.RegisterClassW(ctypes.byref(cls))

        self._hwnd = user32.CreateWindowExW(
            0, "RadialMenuTrayClass", "Radial Menu Tray", 0,
            0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            self._hwnd = None
            return

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = TRAY_MSG
        nid.hIcon = self._icon
        nid.szTip = tip[:127]

        self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        if self._added:
            nid.uFlags = 0
            nid.uTimeoutOrVersion = NOTIFYICON_VERSION
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    # -- message handling --------------------------------------------------
    def _on_tray(self, wparam, _lparam):
        # double-click opens the tutorial; right-click opens the menu
        if wparam == WM_LBUTTONDBLCLK:
            if self.on_tutorial:
                self.on_tutorial()
        elif wparam == WM_RBUTTONUP:
            self.show_menu()

    def _on_command(self, cmd):
        if cmd == ID_TUTORIAL and self.on_tutorial:
            self.on_tutorial()
        elif cmd == ID_SETTINGS and self.on_settings:
            self.on_settings()
        elif cmd == ID_ENABLED and self.on_toggle:
            self.on_toggle()
        elif cmd == ID_QUIT and self.on_quit:
            self.on_quit()

    def show_menu(self):
        if not self._hwnd:
            return
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            enabled = bool(self.is_enabled())
            user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "Customise menu...")
            user32.AppendMenuW(menu, MF_STRING, ID_TUTORIAL, "Show tutorial")
            user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if enabled else 0),
                               ID_ENABLED, "Menu enabled")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")

            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # The menu needs foreground rights or it will not dismiss itself
            # when the user clicks elsewhere.
            user32.SetForegroundWindow(self._hwnd)
            cmd = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN
                | TPM_RIGHTALIGN, pt.x, pt.y, 0, self._hwnd, None)
            if cmd:
                self._on_command(cmd)
        finally:
            user32.DestroyMenu(menu)

    # -- lifecycle ---------------------------------------------------------
    def set_tip(self, tip):
        if not self._added:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_TIP
        nid.szTip = tip[:127]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove(self):
        if not self._added or not self._hwnd:
            return
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._added = False
        try:
            user32.DestroyWindow(self._hwnd)
        except Exception:
            pass
        self._hwnd = None
