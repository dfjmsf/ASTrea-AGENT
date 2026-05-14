"""
memory_db — SQLite 永久记忆（Master 对话全量备份）

每步 tool_call / tool_result / user / assistant 消息写入 SQLite，
压缩时原始记录不丢失，Master 通过 recall() 按需检索。

数据库位置：项目目录下 .astrea/memory.db
"""
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("MemoryDB")

# 每个项目一个数据库连接（线程安全）
_db_connections: Dict[str, sqlite3.Connection] = {}
_db_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    step_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    tool_name   TEXT,
    target_file TEXT,
    content     TEXT NOT NULL,
    summary     TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_tool ON memory(tool_name);
CREATE INDEX IF NOT EXISTS idx_memory_file ON memory(target_file);

-- ═══ 自进化系统：轨迹日志 ═══
CREATE TABLE IF NOT EXISTS trajectories (
    traj_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    task_summary TEXT,
    tool_calls  TEXT NOT NULL,
    step_count  INTEGER,
    success     BOOLEAN DEFAULT TRUE,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traj_session ON trajectories(session_id);
CREATE INDEX IF NOT EXISTS idx_traj_created ON trajectories(created_at);

-- ═══ 自进化系统：失败事件日志 ═══
CREATE TABLE IF NOT EXISTS failure_log (
    fail_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    error_type  TEXT,
    error_msg   TEXT,
    context     TEXT,
    step_index  INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fail_tool ON failure_log(tool_name);
CREATE INDEX IF NOT EXISTS idx_fail_type ON failure_log(error_type);
CREATE INDEX IF NOT EXISTS idx_fail_step ON failure_log(session_id, step_index);
"""


def _get_db(project_dir: str) -> sqlite3.Connection:
    """获取或创建项目的 SQLite 连接。"""
    abs_dir = os.path.abspath(project_dir)
    with _db_lock:
        if abs_dir in _db_connections:
            return _db_connections[abs_dir]

        db_dir = os.path.join(abs_dir, ".astrea")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "memory.db")

        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()

        _db_connections[abs_dir] = conn
        logger.info(f"📦 MemoryDB 已连接: {db_path}")
        return conn


def save_step(
    project_dir: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    target_file: Optional[str] = None,
    summary: Optional[str] = None,
) -> int:
    """写入一步记录，返回 step_id。"""
    conn = _get_db(project_dir)
    cursor = conn.execute(
        """INSERT INTO memory (role, tool_name, target_file, content, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (role, tool_name, target_file, content, summary, time.time()),
    )
    conn.commit()
    step_id = cursor.lastrowid
    return step_id


def query(
    project_dir: str,
    step_id: Optional[int] = None,
    tool_name: Optional[str] = None,
    target_file: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """按条件检索原始记录。
    
    参数:
        step_id: 精确匹配步骤 ID
        tool_name: 按工具名过滤（write_code / read_file 等）
        target_file: 按文件名过滤
        keyword: 内容关键词搜索（LIKE）
        limit: 返回条数上限
    返回:
        [{"step_id": 1, "role": "tool", "tool_name": "write_code", ...}]
    """
    conn = _get_db(project_dir)

    conditions = []
    params = []

    if step_id is not None:
        conditions.append("step_id = ?")
        params.append(step_id)
    if tool_name:
        conditions.append("tool_name = ?")
        params.append(tool_name)
    if target_file:
        # 支持 basename 匹配（用户可能只传 "models.py" 而非完整路径）
        conditions.append("(target_file = ? OR target_file LIKE ?)")
        params.extend([target_file, f"%/{target_file}"])
    if keyword:
        conditions.append("content LIKE ?")
        params.append(f"%{keyword}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT * FROM memory WHERE {where} ORDER BY step_id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_all_steps(project_dir: str) -> List[Dict[str, Any]]:
    """获取全部记录（用于压缩时遍历）。"""
    conn = _get_db(project_dir)
    rows = conn.execute("SELECT * FROM memory ORDER BY step_id ASC").fetchall()
    return [dict(row) for row in rows]


def update_summary(project_dir: str, step_id: int, summary: str) -> None:
    """压缩时回填摘要字段。"""
    conn = _get_db(project_dir)
    conn.execute(
        "UPDATE memory SET summary = ? WHERE step_id = ?",
        (summary, step_id),
    )
    conn.commit()


def get_step_count(project_dir: str) -> int:
    """返回当前记录总数。"""
    conn = _get_db(project_dir)
    row = conn.execute("SELECT COUNT(*) FROM memory").fetchone()
    return row[0] if row else 0


def close(project_dir: str) -> None:
    """关闭项目的数据库连接。"""
    abs_dir = os.path.abspath(project_dir)
    with _db_lock:
        conn = _db_connections.pop(abs_dir, None)
        if conn:
            conn.close()
            logger.info(f"📦 MemoryDB 已关闭: {abs_dir}")
