"""A true per-pixel-alpha, click-through, non-activating overlay window.

Tk's `-transparentcolor` trick paints every non-drawn pixel a flat key colour,
which leaves hard aliased edges and black fringes over busy wallpapers. This
module instead creates a WS_EX_LAYERED window and pushes premultiplied BGRA
frames into it with UpdateLayeredWindow - the same API ShareX / overlays use.

Flags used:
  WS_EX_LAYERED     - required for UpdateLayeredWindow
  WS_EX_TRANSPARENT - clicks pass straight through, so the menu can never
                      swallow the mouse-up that selects a slice
  WS_EX_NOACTIVATE  - showing the menu never steals focus from your app
  WS_EX_TOOLWINDOW  - keeps it out of the taskbar and alt-tab
  WS_EX_TOPMOST     - always above normal windows
"""

import ctypes
import ctypes.wintypes as wt

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_POPUP = 0x80000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
DIB_RGB_COLORS = 0

# NOTE: ctypes.wintypes defines LPARAM/WPARAM as c_long, which is 32-bit on
# Windows x64 (LLP64). Window handles and lParams are pointer-sized there, so
# using the wintypes aliases overflows on any 64-bit handle value. Use the
# real pointer-sized types instead.
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, ctypes.c_uint,
                              ctypes.c_size_t, ctypes.c_ssize_t)


class POINT(wt.POINT):
    pass


class SIZE(wt.SIZE):
    pass


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HCURSOR),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


# ctypes assumes c_int (32-bit) for undeclared args, which overflows on the
# 64-bit handles and lParams we pass. Declare the real signatures.
user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wt.LPCWSTR, wt.LPCWSTR,
                                   ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(POINT),
                                       ctypes.POINTER(SIZE), wt.HDC,
                                       ctypes.POINTER(POINT), wt.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), ctypes.c_uint32]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), ctypes.c_uint32,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   ctypes.c_uint32]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]


def _set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


_set_dpi_awareness()


class Overlay:
    """Owns one layered window and blits RGBA frames into it."""

    _class_registered = False
    _wndproc_ref = None  # keep alive, else GC unregisters the WNDPROC

    def __init__(self):
        self.hwnd = None
        self.hdc = None
        self.hbm = None
        self.bits = None
        self.w = self.h = 0
        # hide() keeps hwnd so the DIB can be reused, so hwnd alone cannot
        # mean "on screen". Without this flag move() would blit into a hidden
        # window forever after the first close, and the wheel would never
        # appear again for the rest of the session.
        self._visible = False

    # -- window plumbing ---------------------------------------------------
    @classmethod
    def _register_class(cls):
        if cls._class_registered:
            return
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)

        @WNDPROC
        def wndproc(hwnd, msg, wp, lp):
            if msg == 0x0002:  # WM_DESTROY
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        cls._wndproc_ref = wndproc
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "RadialOverlayWindow"
        # silent failure is fine if the class already exists from a prior run
        user32.RegisterClassW(ctypes.byref(wc))
        cls._class_registered = True

    def _ensure_window(self, w, h):
        if self.hwnd is not None and (w, h) == (self.w, self.h):
            return
        self._register_class()
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        ex = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE |
              WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        style = WS_POPUP
        hwnd = user32.CreateWindowExW(
            ex, "RadialOverlayWindow", None, style, 0, 0, w, h, 0, 0, hinst, None)
        if not hwnd:
            raise ctypes.WinError()
        if self.hwnd is not None:
            user32.DestroyWindow(self.hwnd)
        self.hwnd = hwnd
        self._alloc_dib(w, h)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _alloc_dib(self, w, h):
        # A bitmap must be deselected from its DC before DeleteObject works,
        # and the DC itself has to be freed or every resize leaks a handle.
        if self.hdc:
            gdi32.SelectObject(self.hdc, 0)
            gdi32.DeleteDC(self.hdc)
            self.hdc = None
        if self.hbm:
            gdi32.DeleteObject(self.hbm)
            self.hbm = None
        hdc = gdi32.CreateCompatibleDC(0)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        if not hbm:
            raise ctypes.WinError()
        gdi32.SelectObject(hdc, hbm)
        self.hdc, self.hbm, self.bits, self.w, self.h = hdc, hbm, bits, w, h

    # -- public API --------------------------------------------------------
    def show(self, image, x, y):
        """Place the top-left of `image` at screen (x, y) and display it."""
        w, h = image.size
        self._ensure_window(w, h)
        self._blit(image, x, y)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self._visible = True

    def move(self, image, x, y):
        """Same as show, but skips the z-order push (used for hover updates)."""
        w, h = image.size
        # A hidden window must go through show() again: SWP_SHOWWINDOW is the
        # only thing that brings it back, and move() deliberately omits it.
        if self.hwnd is None or not self._visible:
            return self.show(image, x, y)
        self._blit(image, x, y)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)

    def hide(self):
        if self.hwnd:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW)
        self._visible = False

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.hbm:
            gdi32.DeleteObject(self.hbm)
            self.hbm = None

    # -- internals ---------------------------------------------------------
    def _blit(self, image, x, y):
        """Push an RGBA PIL image into the DIB as premultiplied BGRA."""
        rgba = image.convert("RGBA")
        # The DIB is a fixed-size buffer; writing more bytes than it holds
        # corrupts the heap, so a size mismatch must be corrected here rather
        # than trusted.
        if rgba.size != (self.w, self.h):
            fixed = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            fixed.alpha_composite(rgba, (0, 0))
            rgba = fixed
        a = rgba.getchannel("A")
        r, g, b = rgba.split()[:3]
        # premultiply so the compositor blends correctly
        pr, pg, pb = (_mul(c, a) for c in (r, g, b))
        # reorder RGBA -> BGRA byte order for BI_RGB DIBs
        bgra = Image.merge("RGBA", (pb, pg, pr, a))
        buf = bgra.tobytes()
        ctypes.memmove(self.bits, buf, len(buf))

        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        dst = POINT(x, y)
        size = SIZE(self.w, self.h)
        src = POINT(0, 0)
        ok = user32.UpdateLayeredWindow(
            self.hwnd, None, ctypes.byref(dst), ctypes.byref(size),
            self.hdc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        if not ok:
            raise ctypes.WinError()


def _mul(channel, alpha):
    """channel * alpha / 255, returned as an L-mode image."""
    from PIL import ImageChops
    return ImageChops.multiply(channel, alpha)
