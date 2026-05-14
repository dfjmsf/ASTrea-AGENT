"""
create_code 工具 — Master 直接传入完整代码，工具做确定性写入 + L0 验证

零 LLM 调用。Master 在 tool_call 参数中直接编写代码内容。
"""
import logging
import os
from typing import Any, Dict

from core.tools import ToolDefinition
from core.tools._code_utils import run_l0_checks, extract_exports_summary

logger = logging.getLogger("Tool.CreateCode")


def create_code(
    target_file: str,
    code: str,
    description: str = "",
    _project_dir: str = "",
) -> Dict[str, Any]:
    """创建新代码文件。

    参数:
        target_file: 目标文件路径（相对于项目根目录）
        code: 完整的文件代码内容
        description: 文件功能描述（软密钥，强制填写但不校验）
        _project_dir: 项目目录（Master 自动注入）
    返回:
        {"status": "ok", "summary": "...", "lines": N}
        或 {"status": "fail", "feedback": "..."}
    """
    # ═══ 参数校验 ═══
    if not code or not code.strip():
        return {"status": "fail", "feedback": "代码内容为空"}

    if not target_file:
        return {"status": "fail", "feedback": "target_file 不能为空"}

    # ═══ 写入磁盘 ═══
    try:
        if _project_dir:
            abs_path = os.path.join(_project_dir, target_file)
        else:
            abs_path = os.path.abspath(target_file)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(code)
        logger.info(f"📝 create_code 写入: {target_file} ({len(code)} chars)")
    except Exception as e:
        return {"status": "fail", "feedback": f"写入磁盘失败: {e}"}

    # ═══ L0 确定性检查 ═══
    l0_pass, l0_feedback = run_l0_checks(target_file, code)

    if l0_pass:
        summary = extract_exports_summary(code, target_file)
        total_lines = code.count("\n") + 1
        # 新建文件后返回最新文件树（事件溯源：状态变更随工具结果进入 L2b）
        file_tree = ""
        if _project_dir:
            try:
                from core.tools.ls import _build_tree
                file_tree = _build_tree(_project_dir, max_depth=2)
            except Exception:
                pass
        result = {
            "status": "ok",
            "summary": summary,
            "target_file": target_file,
            "lines": total_lines,
        }
        if file_tree:
            result["file_tree"] = f"<current_file_tree>\n{file_tree}\n</current_file_tree>"
        logger.info(f"✅ create_code 成功: {target_file} ({summary})")
        return result

    # L0 失败 → 返回错误（文件已写入磁盘，Master 可用 edit_code 修复）
    logger.warning(f"⚠️ create_code L0 失败: {l0_feedback[:200]}")
    return {
        "status": "fail",
        "feedback": l0_feedback,
        "target_file": target_file,
    }


TOOL_DEF = ToolDefinition(
    name="create_code",
    description=(
        "创建新代码文件。直接传入完整代码内容，工具负责写入磁盘并执行 L0 语法验证。"
        "自带 AST 检查和格式化，推荐用于所有需保证语法正确的源文件（Python、JS 等）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "目标文件路径（相对于项目根目录）",
            },
            "code": {
                "type": "string",
                "description": "完整的文件代码内容",
            },
            "description": {
                "type": "string",
                "description": "文件功能简述（必填）",
            },
        },
        "required": ["target_file", "code", "description"],
    },
    handler=create_code,
)
