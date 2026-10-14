"""SQLite 持久化：每天的目标时刻、生成结果、历史记录、取走标记。

关键设计：
* 每天的目标时刻（target_at）在当天第一次被访问时就随机确定并写库，
  这样程序重启、多进程读同一个库时都不会把时间改掉。
* 文案一旦生成就不再重新生成，避免同一天内容变来变去。
* delivered_at + once_grace_minutes 实现「一次取走 + 短时间可重试」，
  这样 iPhone 那边即使发送失败也能在宽限期内重新取到同一句话。
"""

from __future__ import annotations

import random
import sqlite3
import threading
from datetime import date as ddate
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day            TEXT PRIMARY KEY,
    target_at      TEXT,
    text           TEXT,
    source         TEXT,
    model          TEXT,
    generated_at   TEXT,
    delivered_at   TEXT,
    delivery_count INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    context_files  TEXT,
    context_chars  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_daily_delivered ON daily(delivered_at);
"""

# 后加的列：老库需要补上，否则程序会报 "no such column"。
MIGRATIONS = (
    ("context_files", "ALTER TABLE daily ADD COLUMN context_files TEXT"),
    ("context_chars", "ALTER TABLE daily ADD COLUMN context_chars INTEGER NOT NULL DEFAULT 0"),
)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给已存在的老数据库补上后加的列。"""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(daily)")}
        for column, sql in MIGRATIONS:
            if column not in existing:
                self._conn.execute(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- 基础
    def _row(self, day: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM daily WHERE day = ?", (day,))
        return cur.fetchone()

    def get(self, day: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._row(day)
        return dict(row) if row else None

    # ------------------------------------------------------------ 目标时刻
    def ensure_plan(
        self,
        day: str,
        window_start: Any,
        window_end: Any,
        rng: Optional[random.Random] = None,
    ) -> Dict[str, Any]:
        """确保当天的记录存在，并随机确定今天的目标时刻（精确到分钟）。"""
        with self._lock:
            row = self._row(day)
            if row is None:
                rng = rng or random
                start = datetime.combine(ddate.fromisoformat(day), window_start)
                end = datetime.combine(ddate.fromisoformat(day), window_end)
                span_minutes = max(0, int((end - start).total_seconds() // 60))
                offset = rng.randint(0, span_minutes) if span_minutes > 0 else 0
                target = (start + timedelta(minutes=offset)).replace(second=0, microsecond=0)
                self._conn.execute(
                    "INSERT INTO daily (day, target_at) VALUES (?, ?)",
                    (day, target.strftime("%Y-%m-%d %H:%M:%S")),
                )
                self._conn.commit()
                row = self._row(day)
        return dict(row)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- 写入
    def save_text(
        self,
        day: str,
        text: str,
        source: str,
        model: str,
        error: Optional[str] = None,
        context_files: Optional[List[str]] = None,
        context_chars: int = 0,
    ) -> None:
        """写入当天文案。

        注意：这里会把 delivered_at 清空、delivery_count 归零。
        否则用 `generate --force` 换掉今天的内容后，手机那边会因为
        「今天已经取走过」而永远拿不到新内容。
        """
        files = ", ".join(context_files) if context_files else None
        with self._lock:
            self._conn.execute(
                "UPDATE daily SET text = ?, source = ?, model = ?, generated_at = ?, "
                "attempts = attempts + 1, last_error = ?, context_files = ?, context_chars = ?, "
                "delivered_at = NULL, delivery_count = 0 WHERE day = ?",
                (text, source, model, _now_str(), error, files, context_chars, day),
            )
            self._conn.commit()

    def bump_attempt(self, day: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE daily SET attempts = attempts + 1, last_error = ? WHERE day = ?",
                (error[:500], day),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 取走逻辑
    def claim(self, day: str, grace_minutes: int) -> Optional[str]:
        """认领今天的内容：第一次取走会打标记，宽限期内允许重试，之后不再返回。"""
        with self._lock:
            row = self._row(day)
            if row is None or not row["text"]:
                return None
            delivered_at = row["delivered_at"]
            if delivered_at:
                try:
                    delivered = datetime.strptime(delivered_at, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    delivered = None
                if delivered and datetime.now() - delivered > timedelta(minutes=grace_minutes):
                    return None
                self._conn.execute(
                    "UPDATE daily SET delivery_count = delivery_count + 1 WHERE day = ?",
                    (day,),
                )
                self._conn.commit()
                return str(row["text"])
            self._conn.execute(
                "UPDATE daily SET delivered_at = ?, delivery_count = delivery_count + 1 "
                "WHERE day = ?",
                (_now_str(), day),
            )
            self._conn.commit()
            return str(row["text"])

    # ---------------------------------------------------------------- 历史
    def recent_texts(self, limit: int = 30) -> List[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT text FROM daily WHERE text IS NOT NULL AND text <> '' "
                "ORDER BY day DESC LIMIT ?",
                (limit,),
            )
            return [str(r["text"]) for r in cur.fetchall()]

    def history(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM daily ORDER BY day DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN text IS NOT NULL AND text <> '' THEN 1 ELSE 0 END) AS generated, "
                "SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END) AS delivered "
                "FROM daily"
            )
            row = cur.fetchone()
        return {k: (row[k] or 0) for k in ("total", "generated", "delivered")}
