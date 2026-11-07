# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Zero-Intrusion Desktop Session Guard
Provides Atomic Execution Window, Ghost Cursor Protection, and Instant Foreground Preservation.
"""
import ctypes
import time
from ctypes import wintypes
from typing import Optional
import pyperclip

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class DesktopSessionGuard:
    """Context Manager enforcing Zero-Intrusion, Atomic Staging, and Instant Context Restoration"""
    
    def __init__(self, target_hwnd: int = 0, preserve_clipboard: bool = True, ghost_cursor: bool = True):
        self.target_hwnd = target_hwnd
        self.preserve_clipboard = preserve_clipboard
        self.ghost_cursor = ghost_cursor
        
        self.orig_fg_hwnd: int = 0
        self.orig_cursor_pos: tuple = (0, 0)
        self.orig_clipboard: Optional[str] = None
        self.start_time: float = 0.0

    def __enter__(self):
        self.start_time = time.time()
        
        # 1. Snapshot active caller window token
        self.orig_fg_hwnd = user32.GetForegroundWindow()
        
        # 2. Snapshot physical cursor position
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        self.orig_cursor_pos = (pt.x, pt.y)
        
        # 3. Snapshot clipboard safely
        if self.preserve_clipboard:
            try:
                self.orig_clipboard = pyperclip.paste()
            except Exception:
                self.orig_clipboard = None
                
        return self

    def native_click(self, x: int, y: int, delay_after: float = 0.1):
        """Microsecond DPI-aware click with hardware event generation"""
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0) # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0) # MOUSEEVENTF_LEFTUP
        if delay_after > 0:
            time.sleep(delay_after)

    def collapse_window(self, hwnd: int, min_button_offset_x: int = -172, min_button_y: int = 20):
        """Collapse target window back to taskbar/tray seamlessly"""
        if not hwnd or not user32.IsWindow(hwnd):
            return
        try:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            win_w = rect.right - rect.left
            
            # Click native minimize anchor button
            min_x = rect.left + win_w + min_button_offset_x
            min_y = rect.top + min_button_y
            user32.SetCursorPos(int(min_x), int(min_y))
            time.sleep(0.02)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            
            # Enforce Win32 minimize
            user32.ShowWindow(hwnd, 6) # SW_MINIMIZE
        except Exception:
            pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 1. Collapse target window if still lingering
        if self.target_hwnd:
            self.collapse_window(self.target_hwnd)

        # 2. Instantly restore original caller's foreground window
        if self.orig_fg_hwnd and self.orig_fg_hwnd != self.target_hwnd:
            try:
                user32.SetForegroundWindow(self.orig_fg_hwnd)
            except Exception:
                pass

        # 3. Restore physical cursor position (Ghost Cursor Return)
        if self.ghost_cursor:
            user32.SetCursorPos(self.orig_cursor_pos[0], self.orig_cursor_pos[1])

        # 4. Restore user clipboard
        if self.preserve_clipboard and self.orig_clipboard is not None:
            try:
                pyperclip.copy(self.orig_clipboard)
            except Exception:
                pass
                
        return False
