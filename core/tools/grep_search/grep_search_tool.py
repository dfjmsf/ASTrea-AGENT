"""grep_search 工具 — 在项目中搜索文本"""
import os
import re
from typing import Any, Dict, List, Optional

from core.tools import ToolDefinition


def _match_glob(filename: str, pattern: str) -> bool:
    """简单的 glob 匹配（支持 *.py 格式）"""
    if pattern.startswith("*."):
        return filename.endswith(pattern[1:])
    return pattern in filename


def grep_search(
    pattern: str,
    path: str,
    file_filter: Optional[str] = None,
    _project_dir: str = "",
) -> Dict[str, Any]:
    """在项目中搜索文本，返回匹配结果。
    
    参数:
        pattern: 搜索模式（支持正则表达式）
        path: 搜索路径（目录或文件）
        file_filter: 文件过滤器（如 "*.py"）
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "matches": [{"file": "...", "line": 1, "content": "..."}]}
    """
    try:
        # 相对路径基于项目目录解析
        if _project_dir and not os.path.isabs(path):
            abs_path = os.path.join(_project_dir, path)
        else:
            abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            return {"status": "fail", "feedback": f"路径不存在: {path}"}

        matches: List[Dict[str, Any]] = []
        regex = re.compile(pattern, re.IGNORECASE)

        if os.path.isfile(abs_path):
            files_to_search = [abs_path]
        else:
            files_to_search = []
            for root, dirs, filenames in os.walk(abs_path):
                # 跳过隐藏目录和常见忽略目录
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith(".") and d not in {
                        "node_modules", "__pycache__", ".venv", "venv", ".git",
                    }
                ]
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    if file_filter and not _match_glob(fname, file_filter):
                        continue
                    files_to_search.append(os.path.join(root, fname))

        for fpath in files_to_search:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append({
                                "file": os.path.relpath(fpath, abs_path) if os.path.isdir(abs_path) else os.path.basename(fpath),
                                "line": line_num,
                                "content": line.rstrip()[:200],
                            })
                            if len(matches) >= 50:
                                return {
                                    "status": "ok",
                                    "matches": matches,
                                    "truncated": True,
                                }
            except (UnicodeDecodeError, PermissionError):
                continue

        return {"status": "ok", "matches": matches, "total": len(matches)}

    except Exception as e:
        return {"status": "fail", "feedback": f"搜索失败: {e}"}


TOOL_DEF = ToolDefinition(
    name="grep_search",
    description="在项目中搜索文本。用于定位函数定义、查找引用、排障时搜索报错关键词。",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "搜索模式（支持正则表达式）",
            },
            "path": {
                "type": "string",
                "description": "搜索路径（目录或文件）",
            },
            "file_filter": {
                "type": "string",
                "description": "文件过滤器（如 '*.py'），可选",
            },
        },
        "required": ["pattern", "path"],
    },
    handler=grep_search,
)
