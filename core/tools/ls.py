"""ls 工具 — 返回当前项目的文件树"""
import os
from typing import Any, Dict

from core.tools import ToolDefinition


# 跳过的目录
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
             '.astrea', '.sandbox', 'dist', '.idea', '.vscode', 'env'}


def _build_tree(base_dir: str, rel_path: str = "", max_depth: int = 2, current_depth: int = 0) -> str:
    """递归构建目录树文本。"""
    if current_depth > max_depth:
        return ""

    full_path = os.path.join(base_dir, rel_path) if rel_path else base_dir
    if not os.path.isdir(full_path):
        return ""

    lines = []
    try:
        entries = sorted(os.listdir(full_path))
    except PermissionError:
        return ""

    # 分离目录和文件
    dirs = [e for e in entries if os.path.isdir(os.path.join(full_path, e)) and e not in SKIP_DIRS]
    files = [e for e in entries if os.path.isfile(os.path.join(full_path, e))]

    indent = "  " * current_depth

    for d in dirs:
        lines.append(f"{indent}{d}/")
        sub = _build_tree(base_dir, os.path.join(rel_path, d) if rel_path else d,
                         max_depth, current_depth + 1)
        if sub:
            lines.append(sub)

    for f in files:
        lines.append(f"{indent}{f}")

    return "\n".join(lines)


def ls(
    path: str = "",
    max_depth: int = 2,
    _project_dir: str = "",
    **kwargs,
) -> Dict[str, Any]:
    """返回项目目录结构。

    参数:
        path: 要查看的子目录路径（相对于项目根），默认为项目根目录
        max_depth: 目录树最大深度，默认 2
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "tree": "<current_file_tree>...</current_file_tree>"}
    """
    base = _project_dir or os.getcwd()
    if path:
        base = os.path.join(base, path)

    if not os.path.isdir(base):
        return {"status": "fail", "feedback": f"目录不存在: {base}"}

    tree_text = _build_tree(base, max_depth=max_depth)
    return {
        "status": "ok",
        "tree": f"<current_file_tree>\n{tree_text}\n</current_file_tree>",
    }


TOOL_DEF = ToolDefinition(
    name="ls",
    description="返回项目当前的目录结构。在需要确认项目文件布局或查找文件时调用。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要查看的子目录路径（相对于项目根），默认为项目根目录",
            },
            "max_depth": {
                "type": "integer",
                "description": "目录树最大深度，默认 2",
            },
        },
        "required": [],
    },
    handler=ls,
)
