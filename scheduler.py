# -*- coding: utf-8 -*-
"""scheduler.py —— 屏幕定时 / 倒计时任务。

支持三种任务：
    countdown  倒计时若干秒后执行一次（关屏或开屏）
    once       指定日期时间执行一次
    daily      每天固定时刻执行

任务持久化到 %APPDATA%\\ScreenOff-Web\\tasks.json，应用重启后仍然有效。
若应用关闭期间一次性任务已过期，不会在启动时补执行（避免一开机就关屏），
只会记录一条“已过期”历史。
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
import uuid
from typing import Callable

from config import data_dir

MAX_HISTORY = 10


class Scheduler:
    def __init__(self, do_off: Callable[[], None], do_on: Callable[[], None],
                 log: Callable[[str], None] | None = None):
        self._do_off = do_off
        self._do_on = do_on
        self._log = log or (lambda msg: None)
        self._lock = threading.RLock()
        self._tasks: list[dict] = []
        self._history: list[dict] = []
        self._path = os.path.join(data_dir(), "tasks.json")
        self._running = False
        self._load()
        self._recompute_on_load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            tasks = data.get("tasks", [])
            history = data.get("history", [])
            if isinstance(tasks, list):
                self._tasks = [t for t in tasks if self._valid(t)]
            if isinstance(history, list):
                self._history = history[-MAX_HISTORY:]
        except (OSError, ValueError):
            self._tasks, self._history = [], []

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"tasks": self._tasks, "history": self._history[-MAX_HISTORY:]},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as e:
            self._log(f"[警告] 保存任务失败: {e}")

    @staticmethod
    def _valid(t: dict) -> bool:
        if not isinstance(t, dict):
            return False
        if t.get("mode") not in ("countdown", "once", "daily"):
            return False
        if t.get("action") not in ("on", "off"):
            return False
        if t.get("mode") == "daily":
            return bool(isinstance(t.get("daily_time"), str))
        return isinstance(t.get("next_run"), (int, float))

    def _recompute_on_load(self) -> None:
        """启动时整理任务：过期的一次性任务丢弃；每日任务重新计算下次时间。"""
        now = time.time()
        kept, expired = [], []
        for t in self._tasks:
            if t["mode"] == "daily":
                t["next_run"] = self._next_daily(t["daily_time"])
                kept.append(t)
            elif t.get("next_run", 0) <= now:
                expired.append(t)
            else:
                kept.append(t)
        self._tasks = kept
        if expired:
            self._log(f"{len(expired)} 个一次性任务在应用关闭期间已过期，未补执行。")
            for t in expired:
                self._push_history(t["action"], "已过期未执行")
        self._save()

    @staticmethod
    def _next_daily(hhmm: str) -> float:
        now = datetime.datetime.now()
        h, m = (int(x) for x in hhmm.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return target.timestamp()

    # ------------------------------------------------------------ 增删查
    def add_countdown(self, action: str, seconds: int) -> dict:
        seconds = max(1, int(seconds))
        task = {
            "id": uuid.uuid4().hex[:8],
            "mode": "countdown",
            "action": action,
            "seconds": seconds,
            "next_run": time.time() + seconds,
            "created": time.time(),
        }
        with self._lock:
            self._tasks.append(task)
            self._save()
        self._log(f"新增倒计时：{seconds} 秒后{'关闭' if action == 'off' else '开启'}屏幕。")
        return task

    def add_once(self, action: str, when: float) -> dict:
        task = {
            "id": uuid.uuid4().hex[:8],
            "mode": "once",
            "action": action,
            "next_run": float(when),
            "created": time.time(),
        }
        with self._lock:
            self._tasks.append(task)
            self._save()
        self._log(f"新增定时：{self._fmt(when)}{'关闭' if action == 'off' else '开启'}屏幕。")
        return task

    def add_daily(self, action: str, hhmm: str) -> dict:
        hhmm = self._normalize_hhmm(hhmm)
        task = {
            "id": uuid.uuid4().hex[:8],
            "mode": "daily",
            "action": action,
            "daily_time": hhmm,
            "next_run": self._next_daily(hhmm),
            "created": time.time(),
        }
        with self._lock:
            self._tasks.append(task)
            self._save()
        self._log(f"新增每日任务：每天 {hhmm} {'关闭' if action == 'off' else '开启'}屏幕。")
        return task

    @staticmethod
    def _normalize_hhmm(hhmm: str) -> str:
        parts = str(hhmm).strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("时间需为 HH:MM")
        return f"{h:02d}:{m:02d}"

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t["id"] != task_id]
            changed = len(self._tasks) != before
            if changed:
                self._save()
        return changed

    def clear(self) -> int:
        with self._lock:
            n = len(self._tasks)
            self._tasks.clear()
            self._save()
        return n

    def tasks(self) -> list[dict]:
        now = time.time()
        with self._lock:
            items = []
            for t in self._tasks:
                item = dict(t)
                item["remaining"] = max(0, round(t["next_run"] - now))
                item["next_run_text"] = self._fmt(t["next_run"])
                item["action_text"] = "关闭屏幕" if t["action"] == "off" else "点亮屏幕"
                item["mode_text"] = {"countdown": "倒计时", "once": "定时一次",
                                     "daily": "每天"}[t["mode"]]
                items.append(item)
        items.sort(key=lambda x: x["next_run"])
        return items

    def summary(self) -> dict:
        items = self.tasks()
        return {"count": len(items),
                "next": items[0] if items else None,
                "history": self.history()}

    def history(self) -> list[dict]:
        with self._lock:
            return list(reversed(self._history))

    @staticmethod
    def _fmt(epoch: float) -> str:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------ 执行
    def _push_history(self, action: str, result: str) -> None:
        self._history.append({"action": action,
                              "action_text": "关闭屏幕" if action == "off" else "点亮屏幕",
                              "result": result,
                              "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        self._history = self._history[-MAX_HISTORY:]

    def _fire(self, task: dict) -> None:
        action = task["action"]
        try:
            (self._do_off if action == "off" else self._do_on)()
            result = "已执行"
            self._log(f"任务 {task['id']} 已执行：{'关闭' if action == 'off' else '开启'}屏幕。")
        except Exception as e:                                     # noqa: BLE001
            result = f"执行失败: {e}"
            self._log(result)
        self._push_history(action, result)

    def _loop(self) -> None:
        while True:
            now = time.time()
            due: list[dict] = []
            with self._lock:
                keep = []
                for t in self._tasks:
                    if t["next_run"] <= now:
                        due.append(t)
                        if t["mode"] == "daily":
                            t["next_run"] = self._next_daily(t["daily_time"])
                            keep.append(t)
                    else:
                        keep.append(t)
                if due:
                    self._tasks = keep
                    self._save()
            for t in due:
                self._fire(t)
            time.sleep(0.5)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()