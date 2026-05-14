"""get_skeleton 工具 — 返回指定文件的 AST 骨架（函数签名、类定义、行号范围）

用于在编辑前快速了解文件结构和定位行号，比 read_file 更轻量。
"""
import os
from typing import Any, Dict

from core.tools import ToolDefinition

import logging
logger = logging.getLogger("Tool.GetSkeleton")


def get_skeleton(
    target_file: str,
    _project_dir: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """返回指定文件的 AST 骨架。

    参数:
        target_file: 目标文件路径（相对于项目根目录）
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "skeleton": "<file_skeleton>...</file_skeleton>"}
    """
    base = _project_dir or os.getcwd()
    abs_path = os.path.join(base, target_file)

    if not os.path.isfile(abs_path):
        return {"status": "fail", "feedback": f"文件不存在: {target_file}"}

    try:
        from tools.observer import Observer
        obs = Observer(base)
        skeleton = obs.get_skeleton(target_file)

        if not skeleton or "Error" in skeleton:
            return {
                "status": "fail",
                "feedback": f"无法提取骨架（可能不是支持的源文件类型）: {target_file}",
            }

        return {
            "status": "ok",
            "skeleton": f"<file_skeleton path='{target_file}'>\n{skeleton}\n</file_skeleton>",
        }
    except Exception as e:
        logger.warning(f"⚠️ get_skeleton 失败: {e}")
        return {"status": "fail", "feedback": f"骨架提取异常: {e}"}


TOOL_DEF = ToolDefinition(
    name="get_skeleton",
    description=(
        "返回指定文件的 AST 骨架（函数签名、类定义、行号范围）。"
        "用于在编辑前快速了解文件结构和定位行号，比 read_file 更轻量。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "目标文件路径（相对于项目根目录）",
            },
        },
        "required": ["target_file"],
    },
    handler=get_skeleton,
)
