"""
MCP Manager — MCP Server 生命周期管理 + 工具桥接

原生 async，无桥接层。通过 AsyncExitStack 保持 stdio_client 上下文存活。

工具命名规则：
    mcp_{server_name}_{tool_name}
    如 context7 的 resolve-library-id → mcp_context7_resolve_library_id
"""
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger("MCPManager")

# MCP Server 启动超时（秒）
_CONNECT_TIMEOUT = 60


@dataclass
class MCPServerConnection:
    """单个 MCP Server 的运行时连接状态。"""
    name: str
    config: dict
    session: Optional[ClientSession] = None
    exit_stack: Optional[AsyncExitStack] = None
    tools: List[dict] = field(default_factory=list)
    status: str = "stopped"  # stopped / running / error
    error_msg: str = ""


class MCPManager:
    """MCP Server 生命周期管理 + 工具桥接。原生 async，无桥接层。"""

    def __init__(self, config_path: str):
        self._config_path = config_path
        self._servers: Dict[str, MCPServerConnection] = {}

    def _load_config(self) -> list:
        """加载 mcp_servers.json 配置。"""
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("⚠️ mcp_servers.json 格式错误：期望数组")
                return []
            return data
        except (json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning(f"⚠️ MCP 配置加载失败: {e}")
            return []

    async def start_all(self) -> List[str]:
        """启动所有 enabled 的 MCP Server（子进程），返回警告列表。"""
        import asyncio

        configs = self._load_config()
        warnings: List[str] = []

        for cfg in configs:
            name = cfg.get("name", "unknown")
            if not cfg.get("enabled", True):
                logger.info(f"⏭️ MCP [{name}] 已禁用，跳过")
                continue

            transport = cfg.get("transport", "stdio")
            if transport != "stdio":
                msg = f"MCP [{name}] transport '{transport}' 暂不支持，跳过"
                warnings.append(msg)
                logger.warning(f"⚠️ {msg}")
                continue

            conn = MCPServerConnection(name=name, config=cfg)
            try:
                await asyncio.wait_for(
                    self._start_server(conn),
                    timeout=_CONNECT_TIMEOUT,
                )
                self._servers[name] = conn
                logger.info(
                    f"🔌 MCP [{name}] 启动成功，"
                    f"发现 {len(conn.tools)} 个工具"
                )
            except asyncio.TimeoutError:
                msg = f"MCP [{name}] 启动超时 ({_CONNECT_TIMEOUT}s)"
                conn.status = "error"
                conn.error_msg = msg
                self._servers[name] = conn
                warnings.append(msg)
                logger.error(f"❌ {msg}")
            except Exception as e:
                msg = f"MCP [{name}] 启动失败: {e}"
                conn.status = "error"
                conn.error_msg = msg
                self._servers[name] = conn
                warnings.append(msg)
                logger.error(f"❌ {msg}")

        return warnings

    async def _start_server(self, conn: MCPServerConnection) -> None:
        """启动单个 MCP Server 并完成握手。"""
        cfg = conn.config
        server_params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
        )

        # AsyncExitStack 保持 stdio_client 上下文存活
        stack = AsyncExitStack()
        conn.exit_stack = stack

        # 进入 stdio_client 上下文
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params)
        )

        # 创建并初始化 ClientSession
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        conn.session = session
        conn.status = "running"

        # 拉取工具列表
        result = await session.list_tools()
        conn.tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {},
            }
            for t in result.tools
        ]

    async def stop_all(self) -> None:
        """graceful shutdown 所有 MCP Server。"""
        for name, conn in self._servers.items():
            if conn.exit_stack:
                try:
                    await conn.exit_stack.aclose()
                    conn.status = "stopped"
                    logger.info(f"🔌 MCP [{name}] 已停止")
                except Exception as e:
                    logger.warning(f"⚠️ MCP [{name}] 停止异常: {e}")
        self._servers.clear()

    async def restart_server(self, server_name: str) -> str:
        """重启指定 MCP Server，返回状态消息。"""
        conn = self._servers.get(server_name)
        if not conn:
            return f"MCP Server [{server_name}] 不存在"

        # 先停止
        if conn.exit_stack:
            try:
                await conn.exit_stack.aclose()
            except Exception:
                pass

        # 重新启动
        new_conn = MCPServerConnection(name=server_name, config=conn.config)
        try:
            import asyncio
            await asyncio.wait_for(
                self._start_server(new_conn),
                timeout=_CONNECT_TIMEOUT,
            )
            self._servers[server_name] = new_conn
            return f"MCP [{server_name}] 重启成功，{len(new_conn.tools)} 个工具"
        except Exception as e:
            new_conn.status = "error"
            new_conn.error_msg = str(e)
            self._servers[server_name] = new_conn
            return f"MCP [{server_name}] 重启失败: {e}"

    async def stop_server(self, server_name: str) -> str:
        """停止指定 MCP Server（运行时禁用）。"""
        conn = self._servers.get(server_name)
        if not conn:
            return f"MCP Server [{server_name}] 不存在"
        if conn.status == "stopped":
            return f"MCP [{server_name}] 已处于停止状态"

        if conn.exit_stack:
            try:
                await conn.exit_stack.aclose()
            except Exception:
                pass
        conn.status = "stopped"
        conn.session = None
        conn.exit_stack = None
        conn.tools = []
        logger.info(f"🔌 MCP [{server_name}] 已停止（手动禁用）")
        return f"MCP [{server_name}] 已停止"

    async def start_single_server(self, server_name: str) -> str:
        """启动（或重新启动）指定已注册的 MCP Server。"""
        conn = self._servers.get(server_name)
        if not conn:
            # 尝试从配置中查找
            configs = self._load_config()
            cfg = next((c for c in configs if c.get("name") == server_name), None)
            if not cfg:
                return f"MCP Server [{server_name}] 在配置中不存在"
            conn = MCPServerConnection(name=server_name, config=cfg)

        if conn.status == "running":
            return f"MCP [{server_name}] 已在运行中"

        new_conn = MCPServerConnection(name=server_name, config=conn.config)
        try:
            import asyncio
            await asyncio.wait_for(
                self._start_server(new_conn),
                timeout=_CONNECT_TIMEOUT,
            )
            self._servers[server_name] = new_conn
            return f"MCP [{server_name}] 启动成功，{len(new_conn.tools)} 个工具"
        except Exception as e:
            new_conn.status = "error"
            new_conn.error_msg = str(e)
            self._servers[server_name] = new_conn
            return f"MCP [{server_name}] 启动失败: {e}"

    async def list_tools(self, server_name: str) -> List[dict]:
        """从指定 MCP Server 获取工具列表。"""
        conn = self._servers.get(server_name)
        if not conn or conn.status != "running":
            return []
        return conn.tools

    async def call_tool(
        self, server_name: str, tool_name: str, args: dict
    ) -> dict:
        """调用 MCP 工具 — 直接 await，无桥接。"""
        conn = self._servers.get(server_name)
        if not conn or not conn.session:
            return {"status": "error", "feedback": f"MCP [{server_name}] 未连接"}

        try:
            result = await conn.session.call_tool(tool_name, args)
            # 提取文本内容
            content_parts = []
            if result.content:
                for item in result.content:
                    text = getattr(item, "text", None)
                    if text:
                        content_parts.append(text)

            return {
                "status": "error" if result.isError else "ok",
                "result": "\n".join(content_parts) if content_parts else str(result.content),
            }
        except Exception as e:
            return {"status": "error", "feedback": str(e)}

    async def bridge_to_registry(self, registry) -> int:
        """将所有 MCP 工具注册到 ToolRegistry，返回注册数量。

        工具命名：mcp_{server_name}_{tool_name}
        handler 为 async 函数，registry.call() 自动 await。
        """
        from core.tools import ToolDefinition

        count = 0
        for server_name, conn in self._servers.items():
            if conn.status != "running":
                continue

            for tool_info in conn.tools:
                original_name = tool_info["name"]
                # 规范化名称：连字符转下划线
                safe_name = original_name.replace("-", "_")
                registry_name = f"mcp_{server_name}_{safe_name}"

                description = (
                    f"[MCP:{server_name}] {tool_info['description']}"
                )

                # 构建 OpenAI 参数 schema
                parameters = tool_info.get("input_schema", {})
                if not parameters:
                    parameters = {"type": "object", "properties": {}}

                # 闭包捕获 server_name 和 original_name
                def _make_handler(s_name: str, t_name: str):
                    async def handler(**kwargs):
                        # 过滤掉 Master 注入的 _ 前缀参数
                        clean_args = {
                            k: v for k, v in kwargs.items()
                            if not k.startswith("_")
                        }
                        return await self.call_tool(s_name, t_name, clean_args)
                    return handler

                tool_def = ToolDefinition(
                    name=registry_name,
                    description=description,
                    parameters=parameters,
                    handler=_make_handler(server_name, original_name),
                )
                registry.register(tool_def)
                count += 1

        return count

    def get_status(self) -> List[dict]:
        """返回所有 MCP Server 的状态信息（供 /mcp 命令使用）。"""
        result = []
        for name, conn in self._servers.items():
            result.append({
                "name": name,
                "transport": conn.config.get("transport", "stdio"),
                "status": conn.status,
                "tool_count": len(conn.tools),
                "tools": [t["name"] for t in conn.tools],
                "error": conn.error_msg or None,
            })
        return result
