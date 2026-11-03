"""A borderless Win32 window that draws a sprite with true per-pixel alpha.

Tk's colour key can only switch a pixel fully on or off, which turns an
anti-aliased sprite into a hard staircase. `UpdateLayeredWindow` takes real alpha
instead — but `WS_EX_LAYERED` has to be present when the window is created, and a
toolkit-created window cannot offer that. So this module owns its own window and
leaves Tk to the speech bubble and the settings dialog.

Coordinates crossing this boundary are Tk's logical pixels; the display scale is
applied here so the rest of the app can keep working in one unit system.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PIL import Image, ImageChops

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_WS_EX_LAYERED = 0x00080000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_POPUP = 0x80000000
_WS_VISIBLE = 0x10000000
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_PM_REMOVE = 0x0001

_WM_DESTROY = 0x0002
_WM_SETCURSOR = 0x0020
_IDC_HAND = 32649
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_MOUSEMOVE = 0x0200
_WM_RBUTTONUP = 0x0205

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = _LRESULT
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL
_user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC,
                                        ctypes.POINTER(wintypes.POINT),
                                        ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                        ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                                        ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]

_REGISTERED = False


def premultiplied_bgra(image: Image.Image) -> bytes:
    """BGRA with colour scaled by alpha, the layout ULW_ALPHA expects."""
    red, green, blue, alpha = image.convert("RGBA").split()
    red = ImageChops.multiply(red, alpha)
    green = ImageChops.multiply(green, alpha)
    blue = ImageChops.multiply(blue, alpha)
    return Image.merge("RGBA", (blue, green, red, alpha)).tobytes("raw", "RGBA")


class LayeredPetWindow:
    def __init__(self, scale: float, x: int, y: int, width: int, height: int,
                 on_press=None, on_drag=None, on_release=None, on_right=None):
        global _REGISTERED
        self.scale = max(0.5, float(scale))
        self.x = int(x)
        self.y = int(y)
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.on_press = on_press
        self.on_drag = on_drag
        self.on_release = on_release
        self.on_right = on_right
        self.alive = True
        self.dragging = False
        self._cache: dict[tuple, bytes] = {}
        self._bitmap = None
        self._bits = ctypes.c_void_p()
        self._pixel_size = (0, 0)

        self._instance = _kernel32.GetModuleHandleW(None)
        self._class_name = "QoderCatPetWindow"
        self._proc = _WNDPROC(self._wndproc)
        if not _REGISTERED:
            wc = _WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
            wc.lpfnWndProc = self._proc
            wc.hInstance = self._instance
            wc.lpszClassName = self._class_name
            # A NULL class cursor makes Windows fall back to the wait cursor, so
            # hovering the pet showed a spinning ring instead of a pointer.
            _user32.LoadCursorW.restype = wintypes.HANDLE
            _user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            self._hcursor = _user32.LoadCursorW(None, ctypes.c_void_p(_IDC_HAND))
            wc.hCursor = self._hcursor
            self._wc = wc
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassEx failed")
            _REGISTERED = True

        self._screen_dc = _user32.GetDC(None)
        self._mem_dc = _gdi32.CreateCompatibleDC(self._screen_dc)
        # Allocate at the starting size: a move() before the first set_frame would
        # otherwise call UpdateLayeredWindow with a 0x0 bitmap and collapse the
        # window for good.
        self._ensure_bitmap(self._px(width), self._px(height))
        self.hwnd = _user32.CreateWindowExW(
            _WS_EX_LAYERED | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW,
            self._class_name, "pet", _WS_POPUP | _WS_VISIBLE,
            self._px(self.x), self._px(self.y),
            self._px(self.width), self._px(self.height),
            None, None, self._instance, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowEx failed")

    # -- helpers ---------------------------------------------------------- #
    def _px(self, value: int) -> int:
        return int(round(value * self.scale))

    def _ensure_bitmap(self, width: int, height: int) -> None:
        if self._pixel_size == (width, height) and self._bitmap:
            return
        if self._bitmap:
            _gdi32.DeleteObject(self._bitmap)
        info = _BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height          # negative: top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB
        self._bits = ctypes.c_void_p()
        self._bitmap = _gdi32.CreateDIBSection(self._mem_dc, ctypes.byref(info),
                                              _DIB_RGB_COLORS, ctypes.byref(self._bits), None, 0)
        _gdi32.SelectObject(self._mem_dc, self._bitmap)
        self._pixel_size = (width, height)

    def _put(self, pixels: bytes) -> None:
        ctypes.memmove(self._bits, pixels, len(pixels))

    # -- drawing ---------------------------------------------------------- #
    def _compose(self, image: Image.Image, width: int, height: int, bob: int) -> Image.Image:
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sheet.paste(image, ((width - image.width) // 2, -bob), image)
        return sheet

    def set_frame(self, image: Image.Image, canvas: tuple[int, int], bob: int = 0,
                  fade_from: Image.Image | None = None, fade: float = 0.0) -> None:
        """Draw `image` centred inside a `canvas`-sized transparent window.

        `fade_from` (the last frame of the previous action) is dissolved into the
        new frame over the first part of a switch, so changing actions reads as
        the same cat changing pose rather than a cut to a different sprite.
        """
        width, height = canvas
        fade = 0.0 if fade_from is None else max(0.0, min(1.0, fade))
        if fade > 0.0:
            sheet = self._compose(image, width, height, bob)
            start = self._compose(fade_from, width, height, bob)
            sheet = Image.blend(start, sheet, fade)
            if (width, height) != (self._px(width), self._px(height)):
                sheet = sheet.resize((self._px(width), self._px(height)), Image.LANCZOS)
            self.width, self.height = width, height
            self._ensure_bitmap(self._px(width), self._px(height))
            self._put(premultiplied_bgra(sheet))
            self._paint()
            return

        key = (id(image), width, height, bob)
        cached = self._cache.get(key)
        if cached is None or cached[0] is not image:
            sheet = self._compose(image, width, height, bob)
            if (width, height) != (self._px(width), self._px(height)):
                sheet = sheet.resize((self._px(width), self._px(height)), Image.LANCZOS)
            cached = (image, premultiplied_bgra(sheet))
            if len(self._cache) > 96:
                self._cache.clear()
            self._cache[key] = cached
        self.width, self.height = width, height
        pixel_w, pixel_h = self._px(width), self._px(height)
        self._ensure_bitmap(pixel_w, pixel_h)
        self._put(cached[1])
        self._paint()

    def _paint(self) -> None:
        if not self._bitmap or self._pixel_size == (0, 0):
            return
        blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)
        dst = wintypes.POINT(self._px(self.x), self._px(self.y))
        size = wintypes.SIZE(*self._pixel_size)
        src = wintypes.POINT(0, 0)
        _user32.UpdateLayeredWindow(self.hwnd, self._screen_dc, ctypes.byref(dst),
                                    ctypes.byref(size), self._mem_dc, ctypes.byref(src),
                                    0, ctypes.byref(blend), _ULW_ALPHA)

    def move(self, x: int, y: int) -> None:
        self.x, self.y = int(x), int(y)
        self._paint()

    def set_visible(self, visible: bool) -> None:
        _user32.ShowWindow(self.hwnd, 5 if visible else 0)

    # -- messages --------------------------------------------------------- #
    def _cursor(self) -> tuple[int, int]:
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_SETCURSOR:
            _user32.SetCursor(self._hcursor)
            return 1
        if msg == _WM_LBUTTONDOWN:
            self.dragging = True
            _user32.SetCapture(hwnd)
            if self.on_press:
                self.on_press(*self._cursor())
            return 0
        if msg == _WM_MOUSEMOVE and self.dragging:
            if self.on_drag:
                self.on_drag(*self._cursor())
            return 0
        if msg == _WM_LBUTTONUP:
            self.dragging = False
            _user32.ReleaseCapture()
            if self.on_release:
                self.on_release()
            return 0
        if msg == _WM_RBUTTONUP:
            if self.on_right:
                self.on_right()
            return 0
        if msg == _WM_DESTROY:
            self.alive = False
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(self) -> bool:
        """Drain this window's message queue; False once it has been destroyed."""
        msg = wintypes.MSG()
        while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
            if msg.message == 0x0012:      # WM_QUIT
                self.alive = False
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        return self.alive

    def destroy(self) -> None:
        self.alive = False
        try:
            if self.hwnd and _user32.IsWindow(self.hwnd):
                _user32.DestroyWindow(self.hwnd)
            if self._bitmap:
                _gdi32.DeleteObject(self._bitmap)
            _gdi32.DeleteDC(self._mem_dc)
            _user32.ReleaseDC(None, self._screen_dc)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass