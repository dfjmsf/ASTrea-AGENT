"""
Ulimite 工具注册表
Master 通过此模块获取所有可用工具的定义和实现。

工具分两类：
- 子 Agent 工具（内部有独立 LLM）：plan_project / write_code / investigate / run_tests
- 纯函数工具（无 LLM，直接执行）：read_file / write_file / run_command / grep_search / recall
"""
import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger("ToolRegistry")


class ToolDefinition:
    """单个工具的定义：名称 + 描述 + 参数 schema + 实现函数"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_openai_schema(self) -> Dict[str, Any]:
        """转为 OpenAI Function Calling 的 tools JSON 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """工具注册中心，Master 通过此类发现和调用工具"""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._confirm_callback = None  # CLI 层注入的用户确认回调

    def set_confirm_callback(self, callback) -> None:
        """注入用户确认回调函数。

        回调签名: async def callback(label: str, tool_name: str, args: dict) -> str
        返回值: "execute" | "skip" | "abort"
        """
        self._confirm_callback = callback

    def register(self, tool: ToolDefinition) -> None:
        """注册一个工具"""
        if tool.name in self._tools:
            logger.warning(f"⚠️ 工具 [{tool.name}] 已存在，将被覆盖")
        self._tools[tool.name] = tool
        logger.debug(f"工具 [{tool.name}] 已注册")

    def get(self, name: str) -> ToolDefinition:
        """获取工具定义"""
        if name not in self._tools:
            raise KeyError(f"工具 [{name}] 未注册")
        return self._tools[name]

    async def call(self, name: str, **kwargs) -> Any:
        """调用工具（含风险拦截 + 自动过滤参数 + sync/async 适配）。"""
        import inspect

        # ═══ 风险拦截层 ═══
        from core.tools._risk_guard import check
        risk = check(name, kwargs)
        if risk:
            if risk.action == "block":
                logger.warning(f"🚫 风险拦截 [{risk.rule_id}]: {risk.label}")
                return {"status": "blocked", "feedback": f"命令被安全策略拦截: {risk.label}"}
            if risk.action == "confirm":
                if self._confirm_callback:
                    decision = await self._confirm_callback(risk.label, name, kwargs)
                    if decision == "abort":
                        return {"status": "aborted", "feedback": f"用户终止操作: {risk.label}"}
                    if decision == "skip":
                        return {"status": "skipped", "feedback": f"用户跳过操作: {risk.label}"}
                    # decision == "execute" → 继续执行
                else:
                    # 无回调时回退为拒绝（安全优先）
                    logger.warning(f"⚠️ 风险操作需确认但无回调，自动跳过: {risk.label}")
                    return {"status": "confirm_required", "feedback": f"需要用户确认: {risk.label}"}

        # ═══ 正常执行 ═══
        tool = self.get(name)
        sig = inspect.signature(tool.handler)
        params = sig.parameters

        # 如果 handler 接受 **kwargs，直接全传
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            filtered = kwargs
        else:
            # 否则只传 handler 声明的参数
            filtered = {k: v for k, v in kwargs.items() if k in params}

        # async handler（MCP 等）→ await；同步 handler → 直接调用
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(**filtered)
        return tool.handler(**filtered)

    def remove_by_prefix(self, prefix: str) -> List[str]:
        """移除所有名称以 prefix 开头的工具，返回被移除的工具名列表。"""
        to_remove = [name for name in self._tools if name.startswith(prefix)]
        for name in to_remove:
            del self._tools[name]
            logger.info(f"🗑️ 工具 [{name}] 已移除")
        return to_remove

    def list_names(self) -> List[str]:
        """列出所有已注册的工具名"""
        return list(self._tools.keys())

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """导出所有工具为 OpenAI tools JSON 格式（供 LLM API 调用）"""
        return [tool.to_openai_schema() for tool in self._tools.values()]

    def to_inventory_text(self) -> str:
        """生成简洁的工具清单文本（供注入 System Prompt）。"""
        lines = []
        for tool in self._tools.values():
            # 取描述的第一句（截止到第一个句号或 50 字符）
            desc = tool.description.split("。")[0].split(".")[0][:50]
            lines.append(f"- `{tool.name}`: {desc}")
        return "\n".join(lines)
