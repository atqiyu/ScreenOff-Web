# -*- coding: utf-8 -*-
"""config.py —— 配置与数据文件、开机自启动。

所有可写数据都在 %APPDATA%\\ScreenOff-Web\\ 下：
    config.json   端口、访问口令、上次使用的端口
    tasks.json    定时/倒计时任务（重启后继续有效）
    app.log       运行日志
"""

from __future__ import annotations

import json
import os
import sys

APP_NAME = "ScreenOff-Web"
APP_VERSION = "1.1.0"
AUTHOR = "atqiyu"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ScreenOff-Web"

DEFAULTS = {
    "port": 8800,
    "token": "",
    "last_port": 8800,
    "open_browser": True,
}


def data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def resource_dir() -> str:
    """打包成 onefile exe 后，随包资源被解压到 sys._MEIPASS。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(_path("config.json"), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in DEFAULTS if k in saved})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    data = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    tmp = _path("config.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path("config.json"))


# ---------------------------------------------------------------- 开机自启动
def _app_command() -> str:
    """返回开机自启动要执行的命令行（带引号）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --no-open'
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    app_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    return f'"{pythonw}" "{app_py}" --no-open'


def is_autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(value)
    except (ImportError, OSError):
        return False


def set_autostart(enabled: bool) -> bool:
    """写入/删除 HKCU Run 项。返回操作后是否处于开启状态。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _app_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
    except (ImportError, OSError):
        pass
    return is_autostart_enabled()


def autostart_command() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return value
    except (ImportError, OSError):
        return ""