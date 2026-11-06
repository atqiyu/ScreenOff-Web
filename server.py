# -*- coding: utf-8 -*-
"""server.py —— ScreenOff-Web 的 HTTP 服务与命令行入口。

作为模块使用：
    controller, scheduler = create_runtime(log)
    httpd, port = start_server(8800, "", controller, scheduler, log)

命令行使用（会显示控制台窗口，适合调试）：
    python server.py [--port 8800] [--token 口令] [--open]
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from config import (APP_VERSION, AUTHOR, load_config, resource_dir, save_config,
                    is_autostart_enabled, set_autostart)
from scheduler import Scheduler
from screen import ScreenController

APP_START = time.time()
KEEP_AWAKE = False
LOG: Callable[[str], None] = lambda msg: print(msg, flush=True)


# ---------------------------------------------------------------- 运行时
def create_runtime(log: Callable[[str], None] | None = None):
    """创建屏幕控制器与任务调度器（不启动 HTTP 服务）。"""
    global LOG
    if log:
        LOG = log
    controller = ScreenController(log=LOG)
    scheduler = Scheduler(
        do_off=lambda: controller.monitor_off(source="定时任务"),
        do_on=lambda: controller.monitor_on(source="定时任务"),
        log=LOG,
    )
    return controller, scheduler


def lan_ips() -> list[str]:
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
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 9))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def build_status(controller: ScreenController, scheduler: Scheduler,
                 port: int, token: str) -> dict:
    data = controller.status()
    data.update({
        "hostname": socket.gethostname(),
        "lan_urls": [f"http://{ip}:{port}" for ip in lan_ips()],
        "local_url": f"http://127.0.0.1:{port}",
        "port": port,
        "token_required": bool(token),
        "autostart": is_autostart_enabled(),
        "keep_awake": KEEP_AWAKE,
        "version": APP_VERSION,
        "author": AUTHOR,
        "app_uptime_seconds": int(time.time() - APP_START),
        "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": scheduler.summary(),
    })
    return data


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"ScreenOffWeb/{APP_VERSION}"
    protocol_version = "HTTP/1.1"

    controller: ScreenController
    scheduler: Scheduler
    token: str = ""
    port: int = 8800

    # ------------------------------------------------------------ 工具
    def log_message(self, fmt, *args):        # noqa: D401
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/status", "/api/tasks", "/favicon.ico"):
            return
        LOG("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authorized(self) -> bool:
        if not self.token:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = (q.get("token") or [self.headers.get("X-Token", "")])[0]
        return supplied == self.token

    def _deny(self) -> None:
        self._json({"error": "需要访问口令（?token=...）"}, 401)

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(resource_dir(), "index.html"), "rb") as f:
                    self._send_bytes(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send_bytes(500, b"index.html missing", "text/plain; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
            return
        if not self._authorized():
            self._deny()
            return
        if path == "/api/status":
            self._json(build_status(self.controller, self.scheduler,
                                    self.port, self.token))
        elif path == "/api/tasks":
            self._json({"tasks": self.scheduler.tasks(),
                        "history": self.scheduler.history()})
        else:
            self._json({"error": "not found"}, 404)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._authorized():
            self._deny()
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/screen/off":
                self._json(self.controller.monitor_off(source="网页"))
            elif path == "/api/screen/on":
                self._json(self.controller.monitor_on(source="网页"))
            elif path == "/api/toggle":
                if self.controller.screen == "off":
                    self._json(self.controller.monitor_on(source="网页"))
                else:
                    self._json(self.controller.monitor_off(source="网页"))
            elif path == "/api/tasks":
                self._add_task()
            elif path == "/api/tasks/clear":
                self._json({"ok": True, "removed": self.scheduler.clear()})
            elif path == "/api/autostart":
                enabled = bool(self._body().get("enabled"))
                state = set_autostart(enabled)
                LOG(f"开机自启动 -> {'开启' if state else '关闭'}")
                self._json({"ok": True, "autostart": state})
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, KeyError) as e:
            self._json({"error": f"参数错误：{e}"}, 400)

    def _add_task(self) -> None:
        data = self._body()
        mode = data.get("mode")
        action = data.get("action")
        if action not in ("on", "off"):
            raise ValueError("action 需为 on 或 off")
        if mode == "countdown":
            seconds = int(data.get("seconds", 0))
            if seconds <= 0:
                raise ValueError("seconds 需大于 0")
            task = self.scheduler.add_countdown(action, seconds)
        elif mode == "once":
            raw = str(data.get("at", "")).strip()
            try:
                when = datetime.datetime.fromisoformat(raw)
            except ValueError:
                raise ValueError("at 需为 ISO 时间，如 2026-11-06T22:30") from None
            if when.timestamp() <= time.time():
                raise ValueError("时间必须晚于当前时间")
            task = self.scheduler.add_once(action, when.timestamp())
        elif mode == "daily":
            task = self.scheduler.add_daily(action, str(data.get("time", "")))
        else:
            raise ValueError("mode 需为 countdown / once / daily")
        self._json({"ok": True, "task": task,
                    "tasks": self.scheduler.tasks()})

    # ------------------------------------------------------------ DELETE
    def do_DELETE(self):
        if not self._authorized():
            self._deny()
            return
        path = urllib.parse.urlparse(self.path).path
        task_id = ""
        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
        else:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            task_id = (q.get("id") or [""])[0]
        if path.startswith("/api/tasks") and task_id:
            ok = self.scheduler.remove(task_id)
            self._json({"ok": ok, "tasks": self.scheduler.tasks()})
        else:
            self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------- 启动
def start_server(port: int, token: str, controller: ScreenController,
                 scheduler: Scheduler, log: Callable[[str], None] | None = None,
                 max_tries: int = 10) -> tuple[ThreadingHTTPServer, int]:
    """在 0.0.0.0 上启动 HTTP 服务，端口被占用时自动向后寻找。"""
    if log:
        global LOG
        LOG = log

    class BoundHandler(Handler):
        pass

    BoundHandler.controller = controller
    BoundHandler.scheduler = scheduler
    BoundHandler.token = token
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True

    last_error = None
    for offset in range(max_tries):
        candidate = port + offset
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", candidate), BoundHandler)
        except OSError as e:
            last_error = e
            continue
        BoundHandler.port = candidate
        return httpd, candidate
    raise OSError(f"无法监听端口 {port}+{max_tries}：{last_error}")


def serve_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    thread.start()
    return thread


# ---------------------------------------------------------------- 命令行
def main() -> int:
    ap = argparse.ArgumentParser(description="局域网显示器开关控制服务")
    ap.add_argument("--port", type=int, default=None, help="监听端口，默认读取配置")
    ap.add_argument("--token", default=None, help="访问口令（可选）")
    ap.add_argument("--open", action="store_true", help="启动后自动打开本机页面")
    ap.add_argument("--tray", action="store_true", help="以托盘模式启动（同 app.py）")
    args = ap.parse_args()

    if args.tray:
        try:
            import app
        except ImportError:
            print("托盘模式需要 pystray 与 Pillow，请先执行：pip install pystray pillow")
            return 1
        return app.main()

    cfg = load_config()
    port = args.port if args.port is not None else int(cfg.get("port", 8800))
    token = args.token if args.token is not None else str(cfg.get("token", ""))

    controller, scheduler = create_runtime()
    global KEEP_AWAKE
    KEEP_AWAKE = controller.keep_awake()
    print("[OK] 已阻止系统睡眠（ES_SYSTEM_REQUIRED）：屏幕可灭，电脑始终运行。"
          if KEEP_AWAKE else "[警告] SetThreadExecutionState 失败，系统仍可能睡眠。",
          flush=True)
    print(f"[OK] 系统电源通知已注册 {controller.register_power_notifications()}/2。",
          flush=True)
    controller.start_poller()
    scheduler.start()

    try:
        httpd, actual_port = start_server(port, token, controller, scheduler)
    except OSError as e:
        print(f"\n[错误] {e}", flush=True)
        return 1

    cfg["last_port"] = actual_port
    try:
        save_config(cfg)
    except OSError:
        pass

    print("=" * 56, flush=True)
    print(f"  ScreenOff-Web v{APP_VERSION} 服务已启动", flush=True)
    print(f"    本机访问   : http://127.0.0.1:{actual_port}", flush=True)
    ips = lan_ips()
    for ip in ips:
        print(f"    局域网访问 : http://{ip}:{actual_port}", flush=True)
    if not ips:
        print("    局域网访问 : 未检测到局域网 IP，请检查网络连接", flush=True)
    print(f"    开机自启动 : {'已开启' if is_autostart_enabled() else '未开启'}", flush=True)
    print("  （手机/其他电脑连同一 Wi-Fi，打开局域网地址即可控制）", flush=True)
    print("=" * 56, flush=True)

    if args.open:
        page = f"http://127.0.0.1:{actual_port}/" + (f"?token={token}" if token else "")
        threading.Timer(0.6, lambda: webbrowser.open(page)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("服务已停止。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())