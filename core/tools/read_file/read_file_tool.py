"""read_file 工具 — 读取文件内容（带视觉行号注入 + 行号范围支持）"""
import os
from typing import Any, Dict, Optional

from core.tools import ToolDefinition


def _inject_line_numbers(lines: list, start: int) -> str:
    """为文件行列表注入右对齐行号前缀（纯内存操作，不修改磁盘文件）。

    参数:
        lines: 文件行列表
        start: 起始行号（1-indexed）

    格式示例（总行数 150 时，宽度 3）：
      1| def foo():
      2|     pass
    ...
    150| # end
    """
    end = start + len(lines) - 1
    width = len(str(max(1, end)))
    return "\n".join(
        f"{i:>{width}}| {line}"
        for i, line in enumerate(lines, start)
    )


def read_file(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    _project_dir: str = "",
) -> Dict[str, Any]:
    """读取文件内容，返回带行号标注的视觉增强版本。

    参数:
        file_path: 要读取的文件的绝对路径或相对于项目根目录的路径
        start_line: 起始行号（1-indexed，含此行）。不传则从第 1 行开始
        end_line: 结束行号（1-indexed，含此行）。不传则到文件末尾
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "content": "带行号的内容", "lines": 总行数, "range": "L1-L50"}
        或 {"status": "fail", "feedback": "..."}

    注意: 行号前缀是视觉辅助标记，不是文件的实际内容。
    """
    try:
        # 相对路径基于项目目录解析
        if _project_dir and not os.path.isabs(file_path):
            abs_path = os.path.join(_project_dir, file_path)
        else:
            abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            return {"status": "fail", "feedback": f"文件不存在: {file_path}"}

        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().split("\n")

        total_lines = len(all_lines)

        # 计算行范围（1-indexed → 0-indexed 切片）
        s = max(1, start_line or 1)
        e = min(total_lines, end_line or total_lines)
        if s > total_lines:
            return {"status": "fail", "feedback": f"start_line={s} 超出文件总行数 {total_lines}"}
        if s > e:
            return {"status": "fail", "feedback": f"start_line={s} 大于 end_line={e}"}

        selected = all_lines[s - 1 : e]

        # 行号注入（纯内存，不改磁盘）
        numbered_content = _inject_line_numbers(selected, s)

        return {
            "status": "ok",
            "content": numbered_content,
            "lines": total_lines,
            "range": f"L{s}-L{e}",
        }

    except Exception as e:
        return {"status": "fail", "feedback": f"读取文件失败: {e}"}


TOOL_DEF = ToolDefinition(
    name="read_file",
    description=(
        "【禁止盲目微调行号滚动阅读】！在编辑代码后若不确定行号，必须先调用 get_skeleton 或 grep_search 精确定位，"
        "或者直接不传行号参数读取全文！严禁通过反复修改 start_line/end_line 来瞎猜寻找代码段。\n"
        "读取文件内容。支持行号范围读取（start_line / end_line），减少大文件的 token 消耗。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号（1-indexed，含此行）。不传则从第 1 行开始",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（1-indexed，含此行）。不传则到文件末尾",
            },
        },
        "required": ["file_path"],
    },
    handler=read_file,
)
