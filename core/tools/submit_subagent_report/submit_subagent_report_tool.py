"""submit_subagent_report 工具 — subagent 专用结构化出口。"""
from typing import Any, Dict, List

from core.tools import ToolDefinition


def submit_subagent_report(
    conclusion: str,
    findings: List[Dict[str, Any]],
    suggestion: str,
    _caller_role: str = "",
) -> Dict[str, Any]:
    """提交 subagent 调查报告。

    该工具没有副作用；仅作为 SubagentRunner 的结构化出口。
    Master 直接调用会被拒绝。
    """
    if _caller_role != "subagent":
        return {
            "status": "blocked",
            "feedback": (
                "submit_subagent_report 仅允许 SUBAGENT_READONLY_WORKER 调用。"
                "Master 完成任务必须使用 task_done，不得调用此工具。"
            ),
        }

    if not isinstance(findings, list) or not findings:
        return {"status": "fail", "feedback": "findings 必须是非空数组"}

    normalized = []
    for item in findings:
        if not isinstance(item, dict):
            return {"status": "fail", "feedback": "findings 条目必须是对象"}
        if "file" not in item or "line" not in item or "detail" not in item:
            return {
                "status": "fail",
                "feedback": "findings 条目必须包含 file/line/detail",
            }
        if not str(item.get("detail", "")).strip():
            return {"status": "fail", "feedback": "findings.detail 不能为空"}
        normalized.append({
            "file": item.get("file"),
            "line": item.get("line"),
            "detail": str(item.get("detail", "")).strip(),
        })

    if not str(conclusion).strip():
        return {"status": "fail", "feedback": "conclusion 不能为空"}
    if not str(suggestion).strip():
        return {"status": "fail", "feedback": "suggestion 不能为空"}

    return {
        "status": "ok",
        "report": {
            "conclusion": str(conclusion).strip(),
            "findings": normalized,
            "suggestion": str(suggestion).strip(),
        },
    }


TOOL_DEF = ToolDefinition(
    name="submit_subagent_report",
    description=(
        "【SUBAGENT 专用出口，Master 绝对禁止调用】"
        "仅当当前身份是 SUBAGENT_READONLY_WORKER 时，用此工具提交只读调查报告并结束 subagent。"
        "Master 完成用户任务必须调用 task_done，禁止调用 submit_subagent_report。"
        "普通助手回复、规划、编码、测试、用户汇报都禁止使用此工具。"
        "报告必须只包含当前 subtask 范围内的 conclusion/findings/suggestion。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "conclusion": {
                "type": "string",
                "description": "2-5 句摘要；如果任务范围不清，以 scope_unclear: 开头",
            },
            "findings": {
                "type": "array",
                "description": "与当前 subtask 直接相关的证据，必须非空",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "证据所在绝对路径；没有具体文件时传空字符串",
                        },
                        "line": {
                            "type": "integer",
                            "description": "证据行号；没有具体行号时传 0",
                        },
                        "detail": {
                            "type": "string",
                            "description": "具体发现或缺失边界说明",
                        },
                    },
                    "required": ["file", "line", "detail"],
                },
            },
            "suggestion": {
                "type": "string",
                "description": "只围绕当前 subtask 给 Master 的下一步建议",
            },
        },
        "required": ["conclusion", "findings", "suggestion"],
    },
    handler=submit_subagent_report,
)
