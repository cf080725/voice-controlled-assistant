"""Monitor work areas and window placement, including monitors left of the primary."""
import ctypes
import sys
from ctypes import wintypes


def work_areas(fallback=(0, 0, 1920, 1080)):
    areas = []
    if sys.platform == "win32":
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.POINTER(wintypes.RECT),
                                           ctypes.c_ssize_t)

        def visit(handle, dc, rect, data):
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(handle), ctypes.byref(info)):
                r = info.work
                areas.append((r.left, r.top, r.right, r.bottom))
            return True

        try:
            ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
        except OSError:
            pass
    return areas or [fallback]


def place(x, y, width, height, areas, snap=0):
    """Keep the entire window inside the nearest monitor's usable area."""
    cx, cy = x + width / 2, y + height / 2
    def distance(r):
        return max(r[0] - cx, 0, cx - r[2]) ** 2 + max(r[1] - cy, 0, cy - r[3]) ** 2
    area = min(areas, key=distance)
    left, top, right, bottom = area
    max_x, max_y = max(left, right - width), max(top, bottom - height)
    x, y = min(max(x, left), max_x), min(max(y, top), max_y)
    if snap:
        if x - left <= snap:
            x = left
        elif max_x - x <= snap:
            x = max_x
        if y - top <= snap:
            y = top
        elif max_y - y <= snap:
            y = max_y
    return int(x), int(y), area


def geometry(width, height, x, y):
    # A leading + followed by a negative number is an absolute virtual-screen coordinate.
    return f"{width}x{height}+{int(x)}+{int(y)}"
