# -*- coding: utf-8 -*-
"""
ScreenOff-Web —— 局域网显示器开关控制服务器

只关屏幕，电脑照常运行：
    通过 Windows 自带「显示器省电」命令（SC_MONITORPOWER）灭屏/亮屏，
    同时用 SetThreadExecutionState(ES_SYSTEM_REQUIRED) 阻止系统睡眠，
    CPU、内存、网络、下载、后台程序全部不受影响。

用法：
    python server.py                 # 默认端口 8800
    python server.py --port 9000     # 换端口
    python server.py --token 123456  # 简单口令，打开页面需 ?token=123456

屏幕状态判断（三层）：
    1. Windows 电源通知 GUID_MONITOR_POWER_ON / GUID_CONSOLE_DISPLAY_STATE
       —— 系统在显示器亮/灭瞬间回调，最权威；
    2. 灭屏后一旦检测到键鼠输入（GetLastInputInfo 变化）→ 屏幕必然已被唤醒；
    3. 本服务自己发出的开/关命令 → 立即更新状态。
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if os.name != "nt":
    raise SystemExit("本工具只能在 Windows 上运行。")

# 统一控制台输出编码：无论被重定向还是直接显示，中文都不会因代码页不同而报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

# ---------------------------------------------------------------- Win32 接口
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
powrprof = ctypes.WinDLL("powrprof", use_last_error=True)

WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170          # lParam: 2=灭屏, -1=亮屏
HWND_BROADCAST = wintypes.HWND(0xFFFF)
SMTO_ABORTIFHUNG = 0x0002
MOUSEEVENTF_MOVE = 0x0001
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001   # 阻止系统睡眠（不影响显示器）
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
    """uuid.bytes_le 正是 Windows GUID 的二进制布局。"""
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

# ---------------------------------------------------------------- 全局状态
_lock = threading.Lock()
_state = {"screen": "on", "last_change": time.time(), "off_tick": None}
TOKEN = ""
PORT = 8800
KEEP_REF: list = []          # 防止回调结构体被垃圾回收
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "index.html")


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _input_tick():
    """最近一次键鼠输入的系统 tick（毫秒），用于灭屏后的唤醒检测。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if user32.GetLastInputInfo(ctypes.byref(info)):
        return int(info.dwTime)
    return None


def _set_state(new: str, source: str = "") -> None:
    with _lock:
        changed = _state["screen"] != new
        _state["screen"] = new
        if changed:
            _state["last_change"] = time.time()
        _state["off_tick"] = _input_tick() if new == "off" else None
    if changed:
        tag = f"（来源：{source}）" if source else ""
        print(f"[{_now()}] 屏幕状态 -> {'亮屏' if new == 'on' else '灭屏'}{tag}")


# ---------------------------------------------------------------- 显示器控制
def _send_monitor_power(value: int) -> int:
    """向所有顶层窗口逐个 + 广播发送 SC_MONITORPOWER，返回覆盖窗口数。"""
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
                                   value, SMTO_ABORTIFHUNG, 500, ctypes.byref(dummy))
        user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, value)
    except Exception as e:                                   # noqa: BLE001
        print(f"[警告] 发送显示器命令异常: {e}")
    return hit


def monitor_off() -> dict:
    hit = _send_monitor_power(2)
    _set_state("off", "网页命令")
    print(f"[{_now()}] 关屏命令已发送（覆盖 {hit} 个窗口），电脑照常运行。")
    return {"ok": True, "action": "off", "windows": hit}


def monitor_on() -> dict:
    _send_monitor_power(-1)
    # 模拟一次极小的鼠标来回移动（位移 1 像素再移回来），确保显示器被唤醒
    user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, None)
    time.sleep(0.2)
    user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, None)
    _set_state("on", "网页命令")
    print(f"[{_now()}] 开屏命令已发送。")
    return {"ok": True, "action": "on"}


# ---------------------------------------------------------------- 系统电源通知
_POWER_CALLBACK_PROTO = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p,
                                           wintypes.ULONG, ctypes.c_void_p)


def _power_callback(_context, event_type, setting_ptr):
    try:
        if event_type == PBT_POWERSETTINGCHANGE and setting_ptr:
            setting = ctypes.cast(setting_ptr,
                                  ctypes.POINTER(POWERBROADCAST_SETTING)).contents
            if setting.DataLength >= 4:
                val = ctypes.cast(setting.Data,
                                  ctypes.POINTER(wintypes.DWORD)).contents.value
                ps = bytes(setting.PowerSetting)
                if ps == bytes(GUID_CONSOLE_DISPLAY_STATE):
                    new = "on" if val != 0 else "off"        # 0=灭 1=亮 2=变暗
                elif ps == bytes(GUID_MONITOR_POWER_ON):
                    new = "on" if val == 1 else "off"
                else:
                    return 0
                _set_state(new, "系统电源通知")
    except Exception as e:                                   # noqa: BLE001
        print(f"[警告] 电源通知处理异常: {e}")
    return 0


def register_power_notifications() -> int:
    """注册显示器亮/灭的 Windows 系统通知，返回成功注册的 GUID 数。"""
    cb_ref = _POWER_CALLBACK_PROTO(_power_callback)
    params = DEVICE_NOTIFY_SUBSCRIBE_PARAMETERS()
    params.Callback = cb_ref
    params.Context = None
    KEEP_REF.extend((cb_ref, params))                        # 全程保持引用
    ok = 0
    for guid in (GUID_MONITOR_POWER_ON, GUID_CONSOLE_DISPLAY_STATE):
        handle = wintypes.HANDLE(0)
        rc = powrprof.PowerSettingRegisterNotification(
            ctypes.byref(guid), DEVICE_NOTIFY_CALLBACK,
            ctypes.byref(params), ctypes.byref(handle))
        if rc == 0:
            ok += 1
        else:
            print(f"[提示] 电源通知注册失败({rc})，将改用键鼠活动启发式判断。")
    return ok


def start_heuristic_poller() -> None:
    """兜底：灭屏后一旦有键鼠输入，说明屏幕必然被唤醒了。"""
    def _loop():
        while True:
            tick = _input_tick()
            if tick is not None:
                with _lock:
                    if (_state["screen"] == "off"
                            and _state["off_tick"] is not None
                            and tick != _state["off_tick"]):
                        _state["screen"] = "on"
                        _state["last_change"] = time.time()
                        _state["off_tick"] = None
                        print(f"[{_now()}] 检测到键鼠活动，屏幕已被唤醒。")
            time.sleep(1)

    threading.Thread(target=_loop, daemon=True).start()


# ---------------------------------------------------------------- 信息汇总
def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback:
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # UDP connect 不发包
        s.connect(("192.168.255.255", 9))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def _status() -> dict:
    with _lock:
        screen = _state["screen"]
        last_change = _state["last_change"]
    idle = None
    tick = _input_tick()
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
        "hostname": socket.gethostname(),
        "lan_urls": [f"http://{ip}:{PORT}" for ip in _lan_ips()],
        "port": PORT,
        "keep_awake": True,
        "now": _now(),
    }


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "ScreenOffWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/status", "/favicon.ico"):
            return                       # 轮询请求不刷屏
        super().log_request(code, size)

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200) -> None:
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        token = (q.get("token") or [self.headers.get("X-Token", "")])[0]
        return token == TOKEN

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, "rb") as f:
                    self._send_bytes(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send_json({"error": "index.html 缺失"}, 500)
        elif path == "/api/status":
            if not self._authorized():
                self._send_json({"error": "需要口令 (?token=...)"}, 401)
                return
            self._send_json(_status())
        elif path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            self._send_json({"error": "需要口令 (?token=...)"}, 401)
            return
        if path == "/api/screen/off":
            self._send_json(monitor_off())
        elif path == "/api/screen/on":
            self._send_json(monitor_on())
        elif path == "/api/toggle":
            with _lock:
                cur = _state["screen"]
            self._send_json(monitor_on() if cur == "off" else monitor_off())
        else:
            self._send_json({"error": "not found"}, 404)


def main() -> None:
    global TOKEN, PORT
    ap = argparse.ArgumentParser(description="局域网显示器开关控制服务器")
    ap.add_argument("--port", type=int, default=8800, help="监听端口，默认 8800")
    ap.add_argument("--token", default="", help="访问口令（可选）")
    ap.add_argument("--open", action="store_true", help="启动后自动在浏览器打开本机页面")
    args = ap.parse_args()
    TOKEN, PORT = args.token, args.port

    # 关键：阻止系统睡眠（不阻止显示器灭屏）。电脑因此始终保持运行。
    rc = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    print("[OK] 已阻止系统睡眠（ES_SYSTEM_REQUIRED）：屏幕可灭，电脑始终运行。"
          if rc else "[警告] SetThreadExecutionState 失败，系统仍可能按电源计划睡眠。",
          flush=True)

    ok = register_power_notifications()
    print(f"[OK] 系统电源通知已注册 {ok}/2。", flush=True)
    start_heuristic_poller()

    ThreadingHTTPServer.daemon_threads = True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"\n[错误] 无法监听端口 {PORT}：{e}", flush=True)
        print("       可能已有一个服务实例在运行，或该端口被其他程序占用。", flush=True)
        print(f"       换个端口试试：python server.py --port {PORT + 1}", flush=True)
        return

    print("=" * 52, flush=True)
    print("  ScreenOff-Web 局域网显示器开关服务已启动", flush=True)
    print(f"    本机访问   : http://127.0.0.1:{PORT}", flush=True)
    lan = _lan_ips()
    for ip in lan:
        print(f"    局域网访问 : http://{ip}:{PORT}", flush=True)
    if not lan:
        print("    局域网访问 : 未检测到局域网 IP，请检查网络连接", flush=True)
    print("  （手机/其他电脑连同一 Wi-Fi，打开局域网地址即可控制）", flush=True)
    print("=" * 52, flush=True)
    print("  按 Ctrl+C 或直接关闭本窗口即可停止服务。", flush=True)

    if args.open:
        page = f"http://127.0.0.1:{PORT}/" + (f"?token={TOKEN}" if TOKEN else "")
        threading.Timer(0.6, lambda: webbrowser.open(page)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print(f"[{_now()}] 服务已停止。", flush=True)


if __name__ == "__main__":
    main()
