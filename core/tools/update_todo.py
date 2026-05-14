"""
update_todo 工具 — 轻量化任务进度更新

Master 调用此工具标记任务状态变更。
实际数据写入由 master.py 拦截后调用 memory.update_todo() 完成。
"""
from typing import Any, Dict

from core.tools import ToolDefinition


def update_todo(
    task: str,
    status: str,
    summary: str = "",
) -> Dict[str, Any]:
    """更新 Todo 项状态。

    参数:
        task: 任务标识（target_file 或 task_id）
        status: 目标状态（in_progress / completed / failed）
        summary: 完成摘要
    返回:
        参数回传，供 master.py 拦截写入 memory
    """
    return {
        "action": "todo_update",
        "task": task,
        "new_status": status,
        "summary": summary or "",
    }


TOOL_DEF = ToolDefinition(
    name="update_todo",
    description=(
        "更新任务进度。开始写文件前标记 in_progress，"
        "write_code 成功后标记 completed，失败标记 failed。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "任务标识（target_file 名如 'models.py'，或 task_id 如 'task_1'）",
            },
            "status": {
                "type": "string",
                "enum": ["in_progress", "completed", "failed"],
                "description": "任务状态",
            },
            "summary": {
                "type": "string",
                "description": "完成摘要（如 '3 个 CRUD 函数, FastAPI 路由'），completed 时建议填写",
            },
        },
        "required": ["task", "status"],
    },
    handler=update_todo,
)
