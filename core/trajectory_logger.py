"""
trajectory_logger — 自进化系统数据采集层

负责记录 ReAct 轨迹和工具失败事件到 SQLite。
为 PatternMiner（模式挖掘）和 ConstraintStore（约束生成）提供数据源。

设计要点：
- 轨迹在 task_done 时一次性写入（不是逐步写入）
- 失败事件在工具执行失败时立即写入
- 间隔复现判断：同一 error_signature 间隔 ≥ min_step_gap 步才算系统性问题
"""
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("TrajectoryLogger")


class TrajectoryLogger:
    """轨迹采集与失败事件记录。

    依赖 memory_db 的 SQLite 连接，操作 trajectories / failure_log 两张表。
    """

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        # 延迟获取 DB 连接（memory_db 模块级连接池管理）
        self._db = None

    def _get_db(self):
        """延迟获取 SQLite 连接。"""
        if self._db is None:
            from core.memory_db import _get_db
            self._db = _get_db(self.project_dir)
        return self._db

    # ═══════════════════════════════════════════════════
    # 轨迹日志（trajectories 表）
    # ═══════════════════════════════════════════════════

    def log_trajectory(
        self,
        session_id: str,
        tool_calls: List[Dict[str, Any]],
        task_summary: str = "",
        success: bool = True,
    ) -> str:
        """写入一条完整的任务轨迹。

        参数:
            session_id: 会话 ID
            tool_calls: 工具调用序列，每项含 {name, args_keys, args_pattern, status}
            task_summary: task_done 的 summary
            success: 任务是否成功

        返回:
            生成的 traj_id
        """
        traj_id = str(uuid.uuid4())
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False, default=str)
        step_count = len(tool_calls)

        conn = self._get_db()
        conn.execute(
            """INSERT INTO trajectories
               (traj_id, session_id, task_summary, tool_calls, step_count, success, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (traj_id, session_id, task_summary, tool_calls_json, step_count, success, time.time()),
        )
        conn.commit()
        logger.debug(f"轨迹已记录: {traj_id} ({step_count} 步)")
        return traj_id

    def count_since(self, since_traj_id: Optional[str] = None) -> int:
        """统计 since_traj_id 之后的新轨迹数量。

        如果 since_traj_id 为 None，返回全部轨迹数。
        """
        conn = self._get_db()
        if since_traj_id is None:
            row = conn.execute("SELECT COUNT(*) FROM trajectories").fetchone()
        else:
            # 通过 created_at 比较（traj_id 是 UUID，不能直接排序）
            row_ref = conn.execute(
                "SELECT created_at FROM trajectories WHERE traj_id = ?",
                (since_traj_id,),
            ).fetchone()
            if row_ref is None:
                row = conn.execute("SELECT COUNT(*) FROM trajectories").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trajectories WHERE created_at > ?",
                    (row_ref["created_at"],),
                ).fetchone()
        return row[0] if row else 0

    def get_latest_traj_id(self) -> Optional[str]:
        """返回最新一条轨迹的 traj_id。"""
        conn = self._get_db()
        row = conn.execute(
            "SELECT traj_id FROM trajectories ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["traj_id"] if row else None

    def get_trajectories(
        self,
        since_traj_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """获取轨迹列表（增量 + 窗口）。

        参数:
            since_traj_id: 增量游标，只返回此 ID 之后的轨迹
            limit: 最多返回条数
        """
        conn = self._get_db()
        if since_traj_id is None:
            rows = conn.execute(
                "SELECT * FROM trajectories ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            row_ref = conn.execute(
                "SELECT created_at FROM trajectories WHERE traj_id = ?",
                (since_traj_id,),
            ).fetchone()
            if row_ref is None:
                rows = conn.execute(
                    "SELECT * FROM trajectories ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trajectories WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
                    (row_ref["created_at"], limit),
                ).fetchall()
        return [dict(r) for r in rows]

    # ═══════════════════════════════════════════════════
    # 失败事件日志（failure_log 表）
    # ═══════════════════════════════════════════════════

    def log_failure(
        self,
        session_id: str,
        tool_name: str,
        error_msg: str,
        step_index: int,
        error_type: str = "unknown",
        context: str = "",
    ) -> None:
        """记录一次工具执行失败事件。

        参数:
            session_id: 会话 ID
            tool_name: 失败的工具名
            error_msg: 错误信息（截取前 200 字符）
            step_index: 全局 tool_call 步计数器
            error_type: 错误分类（默认 unknown，EvoAgent 后续再精分类）
            context: 上下文摘要
        """
        conn = self._get_db()
        conn.execute(
            """INSERT INTO failure_log
               (session_id, tool_name, error_type, error_msg, context, step_index, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, tool_name, error_type, error_msg[:200], context[:500], step_index, time.time()),
        )
        conn.commit()
        logger.debug(f"失败事件已记录: {tool_name} @ step {step_index}")

    def get_session_failures(self, session_id: str) -> List[Dict[str, Any]]:
        """获取指定会话的所有失败事件。"""
        conn = self._get_db()
        rows = conn.execute(
            "SELECT * FROM failure_log WHERE session_id = ? ORDER BY step_index",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recurrent_failures(
        self,
        session_id: str,
        min_step_gap: int = 3,
    ) -> List[Dict[str, Any]]:
        """查找本会话中满足「间隔复现」条件的失败事件。

        间隔复现定义：同一 error_signature（tool_name::error_type）
        在间隔 ≥ min_step_gap 步 tool_call 后再次出现。
        过滤 LLM 对同一步连续重试的噪声。

        返回:
            [{"error_signature": str, "occurrences": list[dict], "max_gap": int}]
        """
        rows = self.get_session_failures(session_id)

        # 按 error_signature 分组
        groups: Dict[str, list] = defaultdict(list)
        for row in rows:
            sig = f"{row['tool_name']}::{row.get('error_type', 'unknown')}"
            groups[sig].append(row)

        recurrent = []
        for sig, events in groups.items():
            if len(events) < 2:
                continue
            # 检查相邻出现的间隔
            max_gap = 0
            for i in range(1, len(events)):
                gap = events[i]["step_index"] - events[i - 1]["step_index"]
                max_gap = max(max_gap, gap)
            if max_gap >= min_step_gap:
                recurrent.append({
                    "error_signature": sig,
                    "occurrences": events,
                    "max_gap": max_gap,
                })
        return recurrent
