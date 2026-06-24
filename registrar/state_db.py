"""SQLite 状态机：jobs 表 + 断点续跑支持。

状态流转：
    pending → registering → email_verifying → verified → fetching_key → done
    任意状态 → failed（可重试回 pending）

幂等：email 唯一约束，重启不重复注册已完成邮箱。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# 状态枚举
PENDING = "pending"
REGISTERING = "registering"
EMAIL_VERIFYING = "email_verifying"
VERIFIED = "verified"
FETCHING_KEY = "fetching_key"
DONE = "done"
FAILED = "failed"

# 可重试的初始状态
INITIAL_STATUS = PENDING

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    protocol TEXT,
    host TEXT,
    port INTEGER,
    username TEXT,
    auth_code TEXT,
    base_email TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    fireworks_user_id TEXT,
    api_key TEXT,
    password TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    proxy_used TEXT,
    key_disabled INTEGER NOT NULL DEFAULT 0,
    pop_fail_count INTEGER NOT NULL DEFAULT 0,
    email_disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# 已有库的列迁移（ALTER TABLE ADD COLUMN，IF NOT EXISTS 用 PRAGMA 检测）
_MIGRATIONS = [
    ("password", "ALTER TABLE jobs ADD COLUMN password TEXT"),
    ("key_disabled", "ALTER TABLE jobs ADD COLUMN key_disabled INTEGER NOT NULL DEFAULT 0"),
    ("pop_fail_count", "ALTER TABLE jobs ADD COLUMN pop_fail_count INTEGER NOT NULL DEFAULT 0"),
    ("email_disabled", "ALTER TABLE jobs ADD COLUMN email_disabled INTEGER NOT NULL DEFAULT 0"),
]


@dataclass
class Job:
    id: int
    email: str
    protocol: str
    host: str
    port: int
    username: str
    auth_code: str
    base_email: str
    status: str
    fireworks_user_id: str | None
    api_key: str | None
    password: str | None
    error: str | None
    retry_count: int
    proxy_used: str | None
    key_disabled: int
    pop_fail_count: int
    email_disabled: int
    created_at: int
    updated_at: int


class StateDB:
    """状态库封装。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # 列迁移：已有库补 password 列
            cols = {row[1] for row in c.execute("PRAGMA table_info(jobs)").fetchall()}
            for col_name, sql in _MIGRATIONS:
                if col_name not in cols:
                    c.execute(sql)
                    logger.info("DB 迁移：已添加列 %s", col_name)

    @contextmanager
    def _conn(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_email(self, email: str, protocol: str, host: str, port: int,
                     username: str, auth_code: str, base_email: str) -> None:
        """插入新邮箱（若已存在且为 done 则跳过，否则保留）。"""
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                """INSERT INTO jobs (email, protocol, host, port, username, auth_code,
                                      base_email, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(email) DO NOTHING""",
                (email, protocol, host, port, username, auth_code, base_email, now, now),
            )

    def get_job(self, email: str) -> Job | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE email = ?", (email,)).fetchone()
            return self._row_to_job(row) if row else None

    def list_jobs(self, status: str | None = None) -> list[Job]:
        with self._conn() as c:
            if status:
                rows = c.execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [self._row_to_job(r) for r in rows]

    def list_pending(self, limit: int = 100) -> list[Job]:
        """获取待处理任务（pending + 可重试的 failed + 中途崩溃的 registering）。

        跳过 email_disabled=1 的邮箱（判定为假邮箱已禁用，不再重试注册）。
        registering 状态纳入：进程中途崩溃（如断电/kill）会停留在 registering，
        否则永远卡住无法自动重跑；纳入后下次 run 自动从 pending 重新开始注册。
        排序：pending 优先，其次 registering（中断恢复），最后 failed（重试）。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM jobs
                   WHERE status IN ('pending', 'failed', 'registering') AND email_disabled = 0
                   ORDER BY CASE status
                     WHEN 'pending' THEN 0
                     WHEN 'registering' THEN 1
                     ELSE 2 END, id
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def update_status(self, email: str, status: str, *,
                      fireworks_user_id: str | None = None,
                      api_key: str | None = None,
                      password: str | None = None,
                      error: str | None = None,
                      proxy_used: str | None = None,
                      incr_retry: bool = False) -> None:
        """更新任务状态与字段。None 值不覆盖已有值。"""
        now = int(time.time())
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if fireworks_user_id is not None:
            sets.append("fireworks_user_id = ?")
            params.append(fireworks_user_id)
        if api_key is not None:
            sets.append("api_key = ?")
            params.append(api_key)
        if password is not None:
            sets.append("password = ?")
            params.append(password)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if proxy_used is not None:
            sets.append("proxy_used = ?")
            params.append(proxy_used)
        if incr_retry:
            sets.append("retry_count = retry_count + 1")
        params.append(email)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE email = ?", params)

    def reset_failed_to_pending(self) -> int:
        """把 failed 重置为 pending（手动重试入口）。"""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET status = 'pending', error = NULL WHERE status = 'failed'"
            )
            return cur.rowcount

    def export_keys(self, out_path: str | Path) -> int:
        """导出已完成任务的 key 到 JSON，供 sync_channels / sticky_proxy 消费。

        含 auth_code/host/port/protocol：便于后续复用邮箱收信（验证邮件/找回密码）。
        含 disabled：被永久禁用的 key 标记（sticky_proxy 启动时预填充 disabled 集合，
        不再选用；sync_channels 录入时也可据此跳过）。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT email, api_key, fireworks_user_id, base_email,
                          protocol, host, port, username, auth_code, password, key_disabled
                   FROM jobs WHERE status = 'done' AND api_key IS NOT NULL"""
            ).fetchall()
        data = [
            {
                "email": r["email"],
                "api_key": r["api_key"],
                "fireworks_user_id": r["fireworks_user_id"],
                "base_email": r["base_email"],
                "protocol": r["protocol"],
                "host": r["host"],
                "port": r["port"],
                "username": r["username"],
                "auth_code": r["auth_code"],
                "password": r["password"],
                "disabled": bool(r["key_disabled"]),
            }
            for r in rows
        ]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(data)

    def set_key_disabled(self, email: str, disabled: bool,
                         keys_json_path: str | Path | None = None) -> bool:
        """标记/取消标记某 key 为永久禁用。返回是否更新了行。

        数据一致性：DB 的 key_disabled 是 source of truth，keys.json 的 disabled 字段
        由 export_keys 从 DB 生成。若只改 DB 不重新导出 keys.json，两者会不一致
        （sticky_proxy / sync_channels 读 keys.json 仍看到旧 disabled 标记）。
        传入 keys_json_path 时，改完 DB 自动调 export_keys 重新生成 keys.json，
        确保 DB 与 keys.json 实时一致。
        """
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET key_disabled = ?, updated_at = ? WHERE email = ?",
                (1 if disabled else 0, now, email),
            )
            updated = cur.rowcount > 0
        # 改完 DB 后自动同步 keys.json（若调用方提供了路径）
        if updated and keys_json_path:
            n = self.export_keys(keys_json_path)
            logger.info("set_key_disabled(%s, %s) 后已同步 keys.json（%d 个 key）",
                        email, disabled, n)
        return updated

    def incr_pop_fail(self, email: str) -> int:
        """POP 登录失败计数 +1，返回当前累计失败次数（用于假邮箱判定阈值）。"""
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "UPDATE jobs SET pop_fail_count = pop_fail_count + 1, updated_at = ? WHERE email = ?",
                (now, email),
            )
            row = c.execute(
                "SELECT pop_fail_count FROM jobs WHERE email = ?", (email,)
            ).fetchone()
            return int(row["pop_fail_count"]) if row else 0

    def set_email_disabled(self, email: str, disabled: bool) -> bool:
        """标记/取消标记某邮箱为假邮箱禁用（不再用于注册）。返回是否更新了行。"""
        now = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET email_disabled = ?, updated_at = ? WHERE email = ?",
                (1 if disabled else 0, now, email),
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            email=row["email"],
            protocol=row["protocol"],
            host=row["host"],
            port=row["port"],
            username=row["username"],
            auth_code=row["auth_code"],
            base_email=row["base_email"],
            status=row["status"],
            fireworks_user_id=row["fireworks_user_id"],
            api_key=row["api_key"],
            password=row["password"],
            error=row["error"],
            retry_count=row["retry_count"],
            proxy_used=row["proxy_used"],
            key_disabled=row["key_disabled"],
            pop_fail_count=row["pop_fail_count"],
            email_disabled=row["email_disabled"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
