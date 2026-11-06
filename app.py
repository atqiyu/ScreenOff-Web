# -*- coding: utf-8 -*-
"""app.py —— ScreenOff-Web 托盘应用入口。

特点：
    * 无控制台窗口、无主界面，启动后常驻 Windows 系统托盘；
    * 左键/默认菜单项打开网页控制面板；
    * 菜单可查看当前屏幕状态、局域网地址、开机自启动开关、关于、退出；
    * 单实例：重复启动只会打开已有实例的网页；
    * 日志写入 %APPDATA%\\ScreenOff-Web\\app.log。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from config import (APP_VERSION, AUTHOR, autostart_command, data_dir,
                    is_autostart_enabled, load_config, save_config, set_autostart)
from icon import make_image
from scheduler import Scheduler
from screen import ScreenController
import server as web

APP_TITLE = "ScreenOff-Web"
MUTEX_NAME = "Local\\ScreenOff-Web-SingleInstance"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000

_state = {"running": True, "port": 8800, "lan_url": "", "icon": None,
          "httpd": None, "controller": None, "scheduler": None}


# ---------------------------------------------------------------- 基础设施
def _setup_logging() -> None:
    """把标准输出重定向到日志文件（打包后无控制台，异常也能追溯）。"""
    path = os.path.join(data_dir(), "app.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.replace(path, path + ".old")
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {APP_TITLE} "
          f"v{APP_VERSION} =====")


def _single_instance() -> bool:
    """创建命名互斥体；已存在同名实例时返回 False。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return False
    _state["mutex"] = handle
    return True


def _open_page() -> None:
    url = f"http://127.0.0.1:{_state['port']}/"
    token = str(load_config().get("token") or "")
    if token:
        url += f"?token={token}"
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


def _message(text: str, title: str = APP_TITLE, flags: int = 0x40) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, flags)


def _copy(text: str) -> None:
    try:
        subprocess.run(["clip"], input=text.encode("gbk", "ignore"),
                       creationflags=CREATE_NO_WINDOW, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _status_text() -> str:
    controller: ScreenController | None = _state["controller"]
    if not controller:
        return "正在启动…"
    return "当前：亮屏中" if controller.screen == "on" else "当前：屏幕已关闭"


def _tooltip() -> str:
    controller: ScreenController | None = _state["controller"]
    state = "亮屏中" if controller and controller.screen == "on" else "已关闭"
    lan = _state["lan_url"] or f"127.0.0.1:{_state['port']}"
    return f"{APP_TITLE} v{APP_VERSION}\n屏幕：{state}\n{lan}"


# ---------------------------------------------------------------- 托盘菜单动作
def _refresh_tray() -> None:
    icon = _state["icon"]
    if not icon:
        return
    try:
        icon.icon = make_image(_state["controller"].screen if _state["controller"] else "on")
        icon.title = _tooltip()
        icon.update_menu()
    except Exception:                                          # noqa: BLE001
        pass


def _on_screen_change(_state_name: str) -> None:
    _refresh_tray()


def _show_details(icon=None, item=None) -> None:
    c = _state["controller"]
    s = _state["scheduler"]
    if not c:
        return
    st = c.status()
    tasks = s.summary()
    next_task = "无"
    if tasks["next"]:
        t = tasks["next"]
        next_task = f"{t['next_run_text']}  {t['action_text']}（{t['mode_text']}）"
    text = (
        f"{APP_TITLE}  v{APP_VERSION}\n\n"
        f"屏幕状态：{st['screen_text']}（持续 {int(st['state_seconds'])} 秒）\n"
        f"本机地址：http://127.0.0.1:{_state['port']}\n"
        f"局域网地址：{_state['lan_url'] or '未检测到'}\n"
        f"开机自启动：{'已开启' if is_autostart_enabled() else '未开启'}\n"
        f"定时任务：{tasks['count']} 个，下一个：{next_task}\n"
        f"电脑已运行：{st['uptime_seconds'] // 3600} 小时\n\n"
        f"配置文件：{os.path.join(data_dir(), 'config.json')}\n"
        f"作者：{AUTHOR}"
    )
    _message(text)


def _toggle_autostart(icon=None, item=None) -> None:
    current = is_autostart_enabled()
    enabled = set_autostart(not current)
    _message(("已开启开机自启动：\n" + autostart_command()) if enabled
             else "已关闭开机自启动。", flags=0x40)
    _refresh_tray()


def _open_config_dir(icon=None, item=None) -> None:
    try:
        os.startfile(data_dir())                               # noqa: S606
    except OSError:
        pass


def _allow_firewall(icon=None, item=None) -> None:
    """以管理员身份为当前端口放行防火墙（弹出 UAC）。"""
    port = _state["port"]
    args = (f'advfirewall firewall add rule name="ScreenOff-Web" dir=in action=allow '
            f'protocol=TCP localport={port}')
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "netsh", args, None, 0)
    except OSError:
        pass


def _about(icon=None, item=None) -> None:
    _message(
        f"{APP_TITLE} v{APP_VERSION}\n"
        "局域网网页控制 Windows 显示器亮灭\n"
        "只关屏幕，电脑照常运行。\n\n"
        f"作者：{AUTHOR}\n"
        "许可：MIT\n"
        "https://github.com/atqiyu/ScreenOff-Web",
        title="关于 ScreenOff-Web")


def _quit(icon, item=None) -> None:
    _state["running"] = False
    httpd = _state.get("httpd")
    if httpd:
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    icon.stop()


# ---------------------------------------------------------------- 主流程
def _start_backend(token: str) -> None:
    cfg = load_config()
    controller, scheduler = web.create_runtime()
    _state["controller"] = controller
    _state["scheduler"] = scheduler

    web.KEEP_AWAKE = controller.keep_awake()
    print(f"[OK] 阻止系统睡眠: {web.KEEP_AWAKE}")
    print(f"[OK] 电源通知: {controller.register_power_notifications()}/2")
    controller.add_listener(_on_screen_change)
    controller.start_poller()
    scheduler.start()

    preferred = int(cfg.get("port", 8800))
    httpd, port = web.start_server(preferred, token, controller, scheduler)
    _state["httpd"] = httpd
    _state["port"] = port
    web.serve_in_thread(httpd)

    ips = web.lan_ips()
    _state["lan_url"] = f"http://{ips[0]}:{port}" if ips else ""
    cfg["last_port"] = port
    try:
        save_config(cfg)
    except OSError:
        pass
    print(f"[OK] HTTP 服务: 0.0.0.0:{port}  LAN: {_state['lan_url']}")


def main() -> int:
    _setup_logging()

    if not _single_instance():
        # 已有实例：打开它的网页后退出
        port = int(load_config().get("last_port", 8800))
        webbrowser.open(f"http://127.0.0.1:{port}/")
        print("已有实例在运行，已打开网页。")
        return 0

    try:
        import pystray
        from pystray import Menu, MenuItem as Item
    except ImportError:
        _message("缺少 pystray / Pillow，无法显示托盘图标。\n"
                 "请安装：pip install pystray pillow", flags=0x10)
        return 1

    cfg = load_config()
    token = str(cfg.get("token", ""))

    try:
        _start_backend(token)
    except Exception as e:                                     # noqa: BLE001
        print(traceback.format_exc())
        _message(f"ScreenOff-Web 启动失败：\n{e}", flags=0x10)
        return 1

    menu = Menu(
        Item(lambda item: _status_text(), None, enabled=False),
        Item(lambda item: _state["lan_url"] or "未检测到局域网地址", None, enabled=False),
        Menu.SEPARATOR,
        Item("打开控制面板", lambda icon, item: _open_page(), default=True),
        Item("复制局域网地址", lambda icon, item: _copy(_state["lan_url"])),
        Item("状态详情", _show_details),
        Menu.SEPARATOR,
        Item("开机自启动", _toggle_autostart,
             checked=lambda item: is_autostart_enabled()),
        Item("允许防火墙访问", _allow_firewall),
        Item("打开数据文件夹", _open_config_dir),
        Menu.SEPARATOR,
        Item("关于", _about),
        Item("退出", _quit),
    )

    icon = pystray.Icon(APP_TITLE, make_image("on"), _tooltip(), menu)
    _state["icon"] = icon

    # 手动启动时打开页面；开机自启动带 --no-open，不打扰用户
    if "--no-open" not in sys.argv and cfg.get("open_browser", True):
        threading.Timer(1.2, _open_page).start()

    icon.run()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())