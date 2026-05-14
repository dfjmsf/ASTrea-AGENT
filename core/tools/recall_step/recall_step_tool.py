"""
recall_step 工具 — SQLite 步骤记忆精确检索

Master 通过此工具从 memory_db 中按 step_id 精确取回完整的历史操作记录。

设计约束：
- 只接受 step_id 查询（防止 LLM 滥用宽泛检索导致上下文膨胀）
- 上限 3 条（硬约束，不可通过参数突破）
- 返回完整内容（不截断，因为调用频率已被严格约束）

典型用途：
- 压缩后 L4 归档中只保留了 JSON 骨架 + step_id
- Master 需要某步操作的完整细节时，用 step_id 精确取回
"""
import logging
from typing import Any, Dict, List, Union

from core.tools import ToolDefinition

logger = logging.getLogger("Tool.RecallStep")

# 硬约束：单次最多取回 3 条
_MAX_RESULTS = 3


def recall_step(
    step_ids: Union[int, List[int], None] = None,
    _project_dir: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """从 SQLite 步骤记忆中按 step_id 精确取回完整操作记录。

    参数:
        step_ids: 步骤 ID，支持单个整数或整数列表（最多 3 个）
    返回:
        {"status": "ok", "records": [...], "total": N}
    """
    if not _project_dir:
        return {"status": "fail", "feedback": "无项目目录，无法访问步骤记忆"}

    if step_ids is None:
        return {"status": "fail", "feedback": "必须提供 step_ids 参数"}

    # 归一化为列表
    if isinstance(step_ids, int):
        ids = [step_ids]
    elif isinstance(step_ids, list):
        ids = [x for x in step_ids if isinstance(x, int)]
    else:
        return {"status": "fail", "feedback": f"step_ids 类型无效: {type(step_ids).__name__}"}

    if not ids:
        return {"status": "fail", "feedback": "step_ids 为空"}

    # 硬约束：最多 3 条
    if len(ids) > _MAX_RESULTS:
        ids = ids[:_MAX_RESULTS]
        logger.warning(f"⚠️ step_ids 超过上限 {_MAX_RESULTS}，已截断")

    try:
        from core import memory_db

        records = []
        for sid in ids:
            results = memory_db.query(
                project_dir=_project_dir,
                step_id=sid,
                limit=1,
            )
            if results:
                r = results[0]
                records.append({
                    "step_id": r.get("step_id"),
                    "role": r.get("role"),
                    "tool_name": r.get("tool_name"),
                    "target_file": r.get("target_file"),
                    "content": r.get("content", ""),
                    "summary": r.get("summary", ""),
                })

        if not records:
            return {"status": "ok", "records": [], "total": 0, "summary": "未找到匹配记录"}

        logger.info(f"📖 recall_step: 取回 {len(records)} 条记录 (ids={ids})")
        return {
            "status": "ok",
            "records": records,
            "total": len(records),
        }

    except Exception as e:
        logger.error(f"❌ recall_step 异常: {e}")
        return {"status": "fail", "feedback": f"步骤记忆检索异常: {e}"}


TOOL_DEF = ToolDefinition(
    name="recall_step",
    description=(
        "按 step_id 从 SQLite 步骤记忆中精确取回完整操作记录。"
        "仅在需要找回被压缩掉的早期操作细节时使用。"
        "step_id 可从压缩归档的 JSON 骨架中获取。"
        "每次最多取回 3 条。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "step_ids": {
                "description": "要取回的步骤 ID，单个整数或整数列表（最多 3 个）",
                "oneOf": [
                    {"type": "integer"},
                    {"type": "array", "items": {"type": "integer"}, "maxItems": 3},
                ],
            },
        },
        "required": ["step_ids"],
    },
    handler=recall_step,
)
