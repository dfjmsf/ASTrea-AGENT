"""
spawn_subagent 工具 — Fork Master 上下文，执行只读调查子任务。
"""
from copy import deepcopy

from core.tools import ToolDefinition
from core.subagent import SubagentRunner


async def spawn_subagent(
    task: str,
    max_steps: int = 25,
    _project_dir: str = "",
    _master_context: dict = None,
) -> dict:
    """Fork Master 当前上下文，执行只读调查子任务。

    Args:
        task: 子任务描述（由于 subagent 继承完整上下文，只需写指令）
        max_steps: 最大步数
        _project_dir: 项目根目录（Master 自动注入）
        _master_context: Master 上下文快照（Master 自动注入）

    Returns:
        结构化调查报告 dict
    """
    if _master_context is None:
        import logging
        logging.getLogger("Subagent").error(
            f"spawn_subagent 缺少 _master_context, 收到的参数: "
            f"task={task[:50]}, _project_dir={_project_dir}"
        )
        return {
            "status": "error",
            "result": "spawn_subagent 缺少 _master_context（内部错误）",
            "steps_used": 0,
            "tokens_used": 0,
        }

    runner = SubagentRunner(
        messages_snapshot=deepcopy(_master_context["messages"]),
        registry=_master_context["registry"],
        model=_master_context["model"],
        project_dir=_project_dir,
        max_steps=max_steps,
        enable_thinking=_master_context.get("enable_thinking", False),
        reasoning_effort=_master_context.get("reasoning_effort", "low"),
    )
    return await runner.run(task)


TOOL_DEF = ToolDefinition(
    name="spawn_subagent",
    description=(
        "派出侦察分身收集信息。fork 当前完整上下文，共享 KV Cache 前缀。\n"
        "适用于：定位 bug、分析依赖、搜索代码模式、检查性能瓶颈等需要多步读取的调查任务。\n"
        "subagent 只能读取和搜索，不能修改文件。返回结构化调查报告。\n"
        "简单的单步读取（如看一个文件）不需要 subagent，直接用 read_file。\n"
        "可在同一次回复中调用多个 spawn_subagent 并行调查不同方向。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "子任务描述。由于 subagent 继承你的完整上下文，"
                    "这里只需写指令（做什么），不需要重述背景。"
                    "具体说明调查范围、在哪找、排除什么。"
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": "最大步数（默认 25，复杂任务可调高）",
            },
        },
        "required": ["task"],
    },
    handler=spawn_subagent,
)
