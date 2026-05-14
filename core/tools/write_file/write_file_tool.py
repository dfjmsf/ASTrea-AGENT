"""write_file 工具 — 直接写入文件内容（配置文件等非代码文件）"""
import os
from typing import Any, Dict

from core.tools import ToolDefinition


def write_file(file_path: str, content: str, _project_dir: str = "") -> Dict[str, Any]:
    """直接写入文件内容（用于 .env / .gitignore / README.md 等非代码文件）。
    
    参数:
        file_path: 要写入的文件路径
        content: 文件内容
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok"} 或 {"status": "fail", "feedback": "..."}
    """
    try:
        # 相对路径基于项目目录解析
        if _project_dir and not os.path.isabs(file_path):
            abs_path = os.path.join(_project_dir, file_path)
        else:
            abs_path = os.path.abspath(file_path)
        # 自动创建父目录
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {"status": "ok", "summary": f"已写入 {file_path} ({len(content)} 字符)"}

    except Exception as e:
        return {"status": "fail", "feedback": f"写入文件失败: {e}"}


TOOL_DEF = ToolDefinition(
    name="write_file",
    description="直接写入文件全文内容。适用于创建或覆写非代码文件（如 .env, .gitignore, README.md, package.json）。对于需要 L0 语法验证的代码文件，推荐使用 create_code/edit_code。",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要写入的文件路径",
            },
            "content": {
                "type": "string",
                "description": "文件内容",
            },
        },
        "required": ["file_path", "content"],
    },
    handler=write_file,
)
