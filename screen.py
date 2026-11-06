# -*- coding: utf-8 -*-
"""screen.py —— Win32 显示器控制与状态检测。

只关屏幕，电脑照常运行：
    SC_MONITORPOWER 只让显示器进入省电状态，CPU、内存、网络、下载全部不受影响；
    SetThreadExecutionState(ES_SYSTEM_REQUIRED) 阻止系统进入睡眠。

屏幕状态判断（三层）：
    1. 系统电源通知 GUID_MONITOR_POWER_ON / GUID_CONSOLE_DISPLAY_STATE —— 最权威；
    2. 灭屏后检测到键鼠输入（GetLastInputInfo 变化）→ 屏幕必然已被唤醒；
    3. 自身发出的开/关命令 —— 立即更新。
"""

from __future__ import annotations

import ctypes
import datetime
import os
import threading
import time
import uuid
from ctypes import wintypes
from typing import Callable

if os.name != "nt":
    raise RuntimeError("ScreenOff-Web 只能在 Windows 上运行。")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
powrprof = ctypes.WinDLL("powrprof", use_last_error=True)

WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
HWND_BROADCAST = wintypes.HWND(0xFFFF)
SMTO_ABORTIFHUNG = 0x0002
MOUSEEVENTF_MOVE = 0x0001
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
PBT_POWERSETTINGCHANGE = 0x8013
DEVICE_NOTIFY_CALLBACK = 2

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                                       ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(wintypes.DWORD)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                                ctypes.c_ssize_t]
user32.PostMessageW.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.LONG, wintypes.LONG,
                               wintypes.DWORD, ctypes.c_void_p]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _make_guid(s: str) -> GUID:
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, ctypes.sizeof(g))
    return g


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID),
                ("DataLength", wintypes.DWORD),
                ("Data", ctypes.c_byte * 8)]


class DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS(ctypes.Structure):
    _fields_ = [("Callback", ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p,
                                                wintypes.ULONG, ctypes.c_void_p)),
                ("Context", ctypes.c_void_p)]


GUID_MONITOR_POWER_ON = _make_guid("{02722323-A3A5-43DA-9FE1-A87915C23B96}")
GUID_CONSOLE_DISPLAY_STATE = _make_guid("{6FE69556-704A-47A0-8F24-C28D936FDA97}")

powrprof.PowerSettingRegisterNotification.argtypes = [
    ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(wintypes.HANDLE)]
powrprof.PowerSettingRegisterNotification.restype = wintypes.DWORD


class ScreenController:
    """显示器开关 + 状态维护。线程安全，可注册状态变化回调。"""

    def __init__(self, log: Callable[[str], None] | None = None):
        self._log = log or (lambda msg: None)
        self._lock = threading.RLock()
        self._screen = "on"
        self._last_change = time.time()
        self._off_tick: int | None = None
        self._listeners: list[Callable[[str], None]] = []
        self._keep_ref: list = []
        self._polling = False

    # ------------------------------------------------------------ 状态
    @property
    def screen(self) -> str:
        with self._lock:
            return self._screen

    def add_listener(self, cb: Callable[[str], None]) -> None:
        self._listeners.append(cb)

    def _set_state(self, new: str, source: str = "") -> None:
        with self._lock:
            changed = self._screen != new
            self._screen = new
            if changed:
                self._last_change = time.time()
            self._off_tick = self._input_tick() if new == "off" else None
        if changed:
            tag = f"（来源：{source}）" if source else ""
            self._log(f"屏幕状态 -> {'亮屏' if new == 'on' else '灭屏'}{tag}")
            for cb in list(self._listeners):
                try:
                    cb(new)
                except Exception:                                  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ Win32
    @staticmethod
    def _input_tick() -> int | None:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
        return None

    def _send_monitor_power(self, value: int) -> int:
        hit = 0
        dummy = wintypes.DWORD(0)

        def _cb(hwnd, _lp):
            nonlocal hit
            if user32.IsWindowVisible(hwnd):
                user32.SendMessageTimeoutW(hwnd, WM_SYSCOMMAND, SC_MONITORPOWER, value,
                                           SMTO_ABORTIFHUNG, 500, ctypes.byref(dummy))
                hit += 1
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_cb), 0)
            user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER,
                                       value, SMTO_ABORTIFHUNG, 500,
                                       ctypes.byref(dummy))
            user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, value)
        except Exception as e:                                     # noqa: BLE001
            self._log(f"[警告] 发送显示器命令异常: {e}")
        return hit

    def monitor_off(self, source: str = "命令") -> dict:
        hit = self._send_monitor_power(2)
        self._set_state("off", source)
        self._log(f"关屏命令已发送（覆盖 {hit} 个窗口），电脑照常运行。")
        return {"ok": True, "action": "off", "windows": hit}

    def monitor_on(self, source: str = "命令") -> dict:
        self._send_monitor_power(-1)
        # 轻微晃动一下鼠标（1 像素往返），确保显示器被唤醒
        user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, None)
        time.sleep(0.2)
        user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, None)
        self._set_state("on", source)
        self._log("开屏命令已发送。")
        return {"ok": True, "action": "on"}

    # ------------------------------------------------------------ 保活 / 通知
    def keep_awake(self) -> bool:
        """阻止系统睡眠（不影响显示器熄灭）。"""
        rc = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return bool(rc)

    def _power_callback(self, _context, event_type, setting_ptr):
        try:
            if event_type == PBT_POWERSETTINGCHANGE and setting_ptr:
                setting = ctypes.cast(setting_ptr,
                                      ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                if setting.DataLength >= 4:
                    val = ctypes.cast(setting.Data,
                                      ctypes.POINTER(wintypes.DWORD)).contents.value
                    ps = bytes(setting.PowerSetting)
                    if ps == bytes(GUID_CONSOLE_DISPLAY_STATE):
                        new = "on" if val != 0 else "off"       # 0=灭 1=亮 2=变暗
                    elif ps == bytes(GUID_MONITOR_POWER_ON):
                        new = "on" if val == 1 else "off"
                    else:
                        return 0
                    self._set_state(new, "系统电源通知")
        except Exception as e:                                     # noqa: BLE001
            self._log(f"[警告] 电源通知处理异常: {e}")
        return 0

    _POWER_CB = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p,
                                   wintypes.ULONG, ctypes.c_void_p)

    def register_power_notifications(self) -> int:
        cb_ref = self._POWER_CB(self._power_callback)
        params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS()
        params.Callback = cb_ref
        params.Context = None
        self._keep_ref.extend((cb_ref, params))
        ok = 0
        for guid in (GUID_MONITOR_POWER_ON, GUID_CONSOLE_DISPLAY_STATE):
            handle = wintypes.HANDLE(0)
            rc = powrprof.PowerSettingRegisterNotification(
                ctypes.byref(guid), DEVICE_NOTIFY_CALLBACK,
                ctypes.byref(params), ctypes.byref(handle))
            if rc == 0:
                ok += 1
            else:
                self._log(f"[提示] 电源通知注册失败({rc})，改用键鼠活动判断。")
        return ok

    def start_poller(self, interval: float = 1.0) -> None:
        """兜底：灭屏后一有键鼠输入，即认定屏幕已亮。"""
        if self._polling:
            return
        self._polling = True

        def _loop():
            while True:
                tick = self._input_tick()
                if tick is not None:
                    with self._lock:
                        if (self._screen == "off"
                                and self._off_tick is not None
                                and tick != self._off_tick):
                            self._screen = "on"
                            self._last_change = time.time()
                            self._off_tick = None
                            self._log("检测到键鼠活动，屏幕已被唤醒。")
                            for cb in list(self._listeners):
                                try:
                                    cb("on")
                                except Exception:                  # noqa: BLE001
                                    pass
                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True, name="screen-poller").start()

    # ------------------------------------------------------------ 状态快照
    def status(self) -> dict:
        with self._lock:
            screen = self._screen
            last_change = self._last_change
        tick = self._input_tick()
        idle = None
        if tick is not None:
            idle = max(0.0, (int(kernel32.GetTickCount()) - tick) / 1000.0)
        return {
            "screen": screen,
            "screen_text": "亮屏中" if screen == "on" else "已关闭",
            "last_change": datetime.datetime.fromtimestamp(last_change).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "state_seconds": round(time.time() - last_change, 1),
            "input_idle_seconds": round(idle, 1) if idle is not None else None,
            "uptime_seconds": int(kernel32.GetTickCount64() // 1000),
        }