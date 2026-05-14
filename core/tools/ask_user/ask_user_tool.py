"""
ask_user 工具 — Master 中途暂停 ReAct 循环，向用户展示消息并等待回复

场景：
- 需求澄清（技术栈 / 功能边界模糊时追问）
- 规划审批（save_plan 后展示摘要等用户确认）
- 失败求助（连续修复失败，请求用户指导）
- 方案决策（多种实现路径，让用户选择）

零 LLM 调用。返回特殊标记让 master.py loop 暂停。
"""
import logging
from typing import Any, Dict

from core.tools import ToolDefinition

logger = logging.getLogger("Tool.AskUser")


def ask_user(
    question: str,
    context: str = "",
    options: str = "",
    _project_dir: str = "",
    _project_id: str = "",
) -> Dict[str, Any]:
    """向用户提问并暂停执行，等待用户回复后继续。

    参数:
        question: 要向用户展示的问题或报告内容（必填）
        context: 补充上下文信息（可选，如规划摘要、错误详情）
        options: 可选项列表（可选，如 "1. CDN 单文件  2. npm 工程化"）
    返回:
        {"status": "ask_user", "question": "...", "context": "...", "options": "..."}
        master.py loop 检测到 status="ask_user" 后暂停循环，等待用户输入
    """
    if not question or not question.strip():
        return {"status": "fail", "feedback": "question（问题内容）是必填项"}

    logger.info(f"🙋 ask_user: {question[:100]}")
    return {
        "status": "ask_user",
        "question": question.strip(),
        "context": context.strip() if context else "",
        "options": options.strip() if options else "",
    }


TOOL_DEF = ToolDefinition(
    name="ask_user",
    description=(
        "向用户汇报进展、确认方向或展示方案，暂停执行等待用户回复。"
        "这是与用户协作的核心工具——优秀的工程师会主动同步进度，而不是闷头做到底。\n"
        "典型场景：\n"
        "- 规划完成后展示方案摘要，等用户确认再动手\n"
        "- 遇到多种可行方案时，列出选项让用户选择\n"
        "- 阶段性工作完成后汇报进度\n"
        "- 需求存在模糊边界时主动澄清"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要向用户展示的问题或报告内容（必填）",
            },
            "context": {
                "type": "string",
                "description": "补充上下文（如规划摘要、DAG 分级、错误详情）",
            },
            "options": {
                "type": "string",
                "description": "可选项列表（如 '1. CDN 单文件  2. npm 工程化'）",
            },
        },
        "required": ["question"],
    },
    handler=ask_user,
)
