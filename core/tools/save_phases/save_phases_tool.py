"""
save_phases 工具 — 纯磁盘持久化（零 LLM，零 memory 操作）

将 Master 自主规划的 Phase 列表写入项目目录，供 CLI 重启恢复和 L2b 压缩后恢复使用。
Phases 数据已在 L2b 对话链中，不注入 memory。
"""
import json
import logging
import os
from typing import Any, Dict

from core.tools import ToolDefinition

logger = logging.getLogger("Tool.SavePhases")


def save_phases(
    phases: str,
    _project_dir: str = "",
) -> Dict[str, Any]:
    """将 Phase 列表持久化到项目目录。

    参数:
        phases: Phase 列表 JSON 字符串
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "phases_count": N, "summary": "..."}
    """
    # ═══ 解析 JSON ═══
    try:
        phases_data = json.loads(phases) if isinstance(phases, str) else phases
    except json.JSONDecodeError as e:
        return {"status": "fail", "feedback": f"phases JSON 解析失败: {e}"}

    # 兼容两种格式：直接列表 或 {"phases": [...]}
    if isinstance(phases_data, dict):
        phases_list = phases_data.get("phases", [])
    elif isinstance(phases_data, list):
        phases_list = phases_data
    else:
        return {"status": "fail", "feedback": f"phases 格式错误: 预期 list 或 dict，得到 {type(phases_data).__name__}"}

    if not phases_list:
        return {"status": "fail", "feedback": "phases 列表为空"}

    # ═══ 写入磁盘 ═══
    if _project_dir:
        astrea_dir = os.path.join(_project_dir, ".astrea")
        os.makedirs(astrea_dir, exist_ok=True)
        phases_path = os.path.join(astrea_dir, "phases.json")
        try:
            with open(phases_path, "w", encoding="utf-8") as f:
                json.dump(phases_list, f, ensure_ascii=False, indent=2)
            logger.info(f"📋 Phases 已写入: {phases_path} ({len(phases_list)} 个阶段)")
        except Exception as e:
            logger.warning(f"⚠️ Phases 写入磁盘失败: {e}")

    # ═══ 构建摘要 ═══
    summary_parts = []
    for i, phase in enumerate(phases_list):
        name = phase.get("name", f"Phase {i + 1}")
        summary_parts.append(name)

    return {
        "status": "ok",
        "phases_count": len(phases_list),
        "summary": f"{len(phases_list)} 个阶段: {', '.join(summary_parts)}",
    }


TOOL_DEF = ToolDefinition(
    name="save_phases",
    description=(
        "将分阶段构建计划持久化到项目目录。"
        "复杂项目（5+文件）时先规划 phases，再逐 Phase 调用 save_plan。"
        "简单项目不需要调用此工具，直接调 save_plan。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "phases": {
                "type": "string",
                "description": (
                    "Phase 列表的 JSON 字符串。每个 Phase 包含: "
                    "name(阶段名), scope(功能范围描述), "
                    "demo(用户验证点), files_estimate(预估文件数)"
                ),
            },
        },
        "required": ["phases"],
    },
    handler=save_phases,
)
