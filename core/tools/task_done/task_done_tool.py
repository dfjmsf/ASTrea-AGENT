"""
task_done 工具 — Master 显式退出信号

当 Master 认为任务完成时调用此工具，触发 ReAct 循环退出。
这是一个"退出匝道"，解决 LLM 不主动停止调用工具的问题。
"""
from typing import Any, Dict

from core.tools import ToolDefinition


def task_done(
    summary: str,
    files_created: str = "",
    next_steps: str = "",
) -> Dict[str, Any]:
    """标记当前任务完成并向用户报告。
    
    参数:
        summary: 向用户的最终报告（必须包含完成了什么）
        files_created: 本次创建/修改的文件列表
        next_steps: 建议用户的下一步操作
    返回:
        {"status": "done", "summary": "..."}
    """
    return {
        "status": "done",
        "summary": summary,
        "files_created": files_created,
        "next_steps": next_steps,
    }


TOOL_DEF = ToolDefinition(
    name="task_done",
    description=(
        "任务完成时调用此工具向用户报告。"
        "全部文件写完且依赖安装后，必须立即调用此工具结束任务。"
        "调用后 Master 停止工作，不再调用其他工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "向用户的最终报告，说明完成了什么",
            },
            "files_created": {
                "type": "string",
                "description": "本次创建/修改的文件列表",
            },
            "next_steps": {
                "type": "string",
                "description": "建议用户的下一步操作（如启动命令）",
            },
        },
        "required": ["summary"],
    },
    handler=task_done,
)
