"""
Master — Ulimite 架构核心：ReAct 自主调度循环

Master 是整个系统的大脑，通过 Tool Calling 自主决定：
- 调什么工具
- 传什么参数
- 何时向用户汇报

不再有 Engine 硬编码流程。Master 拥有全局视野和自主决策权。
"""
import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from core.llm_client import default_llm
from core.layered_memory import LayeredMemory
from core.tools import ToolRegistry
from core.tools.pure_tools import ALL_TOOLS
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("Master")

# 最大 ReAct 步数（防止无限循环）
MAX_STEPS = 50

# Master System Prompt — 从独立文件加载（与脚本分离，便于独立维护和微调）
def _load_master_prompt() -> str:
    """从 prompts/master_system.md 加载 Master System Prompt。"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "prompts", "master_system.md",
    )
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning(f"⚠️ Master Prompt 文件不存在: {prompt_path}，使用内置兜底")
        return "你是 ASTrea，一个自主型软件工程助手。通过调度工具完成开发任务。"


MASTER_SYSTEM_PROMPT = _load_master_prompt()


def _split_prompt(prompt: str) -> tuple:
    """将 master_system.md 按 XML 标签拆分为软偏好和硬约束。

    硬约束 = <command_norms>...</command_norms> + <discipline>...</discipline>
    软偏好 = 其余所有内容

    返回:
        (soft_prompt, hard_constraints)
    """
    import re
    hard_tags = ["command_norms", "discipline"]
    hard_parts = []
    soft_text = prompt

    for tag in hard_tags:
        pattern = re.compile(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            re.DOTALL,
        )
        match = pattern.search(soft_text)
        if match:
            hard_parts.append(match.group(0))  # 保留完整 XML 标签
            soft_text = soft_text[:match.start()] + soft_text[match.end():]

    # 清理多余空行
    soft_text = re.sub(r"\n{3,}", "\n\n", soft_text).strip()
    hard_text = "\n\n".join(hard_parts).strip()

    return soft_text, hard_text


class Master:
    """Ulimite 架构核心 — ReAct 自主调度循环。"""

    def __init__(self, project_id: str = "default", project_dir: str = ""):
        self.project_id = project_id
        self.project_dir = project_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects", project_id
        )
        self.model = os.getenv("MODEL_MASTER", os.getenv("MODEL_PRO", "deepseek-v4-pro"))
        _et, _re = default_llm.parse_thinking_config(
            os.getenv("THINKING_MASTER", "false")
        )
        self.enable_thinking = _et
        self._reasoning_effort = _re
        
        # 工具注册表
        self.registry = ToolRegistry()
        for tool in ALL_TOOLS:
            self.registry.register(tool)
        logger.info(f"工具注册完成: {len(ALL_TOOLS)} 个内置工具")

        # Skill 管理器（项目级：<project_dir>/.astrea/skills/）
        from core.skill_manager import SkillManager
        skills_dir = os.path.join(self.project_dir, ".astrea", "skills")
        os.makedirs(skills_dir, exist_ok=True)
        self.skill_manager = SkillManager(skills_dir)
        self.skill_manager.discover()

        # Skill 冲突检测（启动时一次性）
        skill_warnings = self.skill_manager.check_conflicts()
        for w in skill_warnings:
            logger.warning(f"⚠️ Skill 冲突: {w}")

        # L1 Prompt 重构：拆分 master_system.md 为软偏好 + 硬约束
        soft_prompt, hard_constraints = _split_prompt(MASTER_SYSTEM_PROMPT)

        # 五层分层记忆
        tool_inventory = self.registry.to_inventory_text()
        # 运行时信息（动态注入模型型号、环境信息，供 Master 感知上下文）
        thinking_status = f"开启 (强度: {self._reasoning_effort})" if self.enable_thinking else "关闭"
        import platform
        os_name = platform.system()  # Windows / Linux / Darwin
        shell_hint = "PowerShell" if os_name == "Windows" else "bash"
        # 虚拟环境探测
        venv_python = ""
        if self.project_dir:
            for venv_dir in [".venv", "venv", "env"]:
                candidate = os.path.join(self.project_dir, venv_dir)
                if os.path.isdir(candidate):
                    if os_name == "Windows":
                        venv_python = os.path.join(candidate, "Scripts", "python.exe")
                    else:
                        venv_python = os.path.join(candidate, "bin", "python")
                    break
        runtime_info = (
            f"\n\n## 运行时信息\n"
            f"- 底层模型: {self.model}\n"
            f"- 深度思考: {thinking_status}\n"
            f"- 操作系统: {os_name}\n"
            f"- 默认 Shell: {shell_hint}\n"
            f"- 项目根目录: {self.project_dir}\n"
            f"- run_command 默认工作目录: 项目根目录（每次命令在独立子进程中执行）\n"
            f"- Python 路径: {venv_python or 'system python'}\n"
        )
        # 加载编码规则（环境约束）
        coding_rules = ""
        coding_rules_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "coding_rules.md",
        )
        try:
            with open(coding_rules_path, "r", encoding="utf-8") as f:
                coding_rules = f"\n\n{f.read().strip()}\n"
        except FileNotFoundError:
            pass

        # L1 拆分拼装：软偏好 + 运行时信息 + 编码规则 + 工具清单
        # Skills 和硬约束通过 memory.set_skill_segments / set_hard_constraints 单独注入
        full_prompt = f"{soft_prompt}{runtime_info}{coding_rules}\n\n## 可用工具\n{tool_inventory}"
        self.memory = LayeredMemory(
            system_prompt=full_prompt,
            project_dir=self.project_dir,
        )
        # 注入 Skill 片段 + 硬约束
        self.memory.set_skill_segments(self.skill_manager.build_segments())
        self.memory.set_hard_constraints(hard_constraints)

        # SQLite 永久记忆（延迟导入，project_dir 可能还不存在）
        self._memory_db = None

        # 步数计数器
        self._step = 0

        # ═══ 自进化数据层 ═══
        from core.trajectory_logger import TrajectoryLogger
        self._trajectory_logger = TrajectoryLogger(self.project_dir)
        self._session_id = str(uuid.uuid4())  # 每次启动生成唯一会话 ID
        self._step_counter = 0  # 全局 tool_call 步计数器（用于间隔复现判断）

        # ═══ 自进化约束层 ═══
        from core.constraint_store import ConstraintStore
        constraints_path = os.path.join(self.project_dir, ".astrea", "constraints.json")
        self._constraint_store = ConstraintStore(constraints_path)

        # ═══ 合成工具加载 ═══
        self._load_synth_tools()

        # MCP Manager（延迟初始化，由 cli.py 调用 init_mcp()）
        self.mcp_manager = None

    async def init_mcp(self) -> None:
        """启动 MCP Server 并桥接工具到 Registry。

        由 cli.py 在 async_main 中调用（需要事件循环）。
        若配置文件不存在则静默跳过。
        """
        mcp_config = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "mcp_servers.json",
        )
        if not os.path.isfile(mcp_config):
            return

        from core.mcp_manager import MCPManager
        self.mcp_manager = MCPManager(mcp_config)
        warnings = await self.mcp_manager.start_all()
        tool_count = await self.mcp_manager.bridge_to_registry(self.registry)

        if tool_count > 0:
            logger.info(f"🔌 MCP: {tool_count} 个工具已桥接")
            # 刷新 L1 工具清单（MCP 工具注册后需要更新 system prompt）
            self._refresh_tool_inventory()
        for w in warnings:
            logger.warning(f"⚠️ MCP: {w}")

    def _refresh_tool_inventory(self) -> None:
        """刷新 system prompt 中的工具清单段。"""
        tool_inventory = self.registry.to_inventory_text()
        # 替换 system prompt 中 "## 可用工具" 之后的内容
        prompt = self.memory._system_prompt
        marker = "\n\n## 可用工具\n"
        idx = prompt.find(marker)
        if idx >= 0:
            self.memory._system_prompt = prompt[:idx] + marker + tool_inventory
        else:
            self.memory._system_prompt += marker + tool_inventory

    async def shutdown_mcp(self) -> None:
        """graceful shutdown 所有 MCP Server。"""
        if self.mcp_manager:
            await self.mcp_manager.stop_all()

    def _get_memory_db(self):
        """延迟初始化 SQLite 连接。"""
        if self._memory_db is None:
            from core import memory_db
            self._memory_db = memory_db
        return self._memory_db

    def _save_to_sqlite(
        self, role: str, content: str,
        tool_name: str = None, target_file: str = None
    ) -> int | None:
        """双写：同步写入 SQLite 永久记忆。返回 step_id。"""
        try:
            db = self._get_memory_db()
            step_id = db.save_step(
                self.project_dir, role, content,
                tool_name=tool_name, target_file=target_file,
            )
            return step_id
        except Exception as e:
            logger.warning(f"⚠️ SQLite 写入失败（不影响主流程）: {e}")
            return None



    async def handle_user_message(self, user_message: str) -> str:
        """处理用户消息：启动 ReAct 循环直到产出用户可见的回复。
        
        参数:
            user_message: 用户输入
        返回:
            Master 给用户的回复文本
        """
        # 递增轮次计数器（自进化系统的时间基准）
        self.increment_round()

        # 将用户消息加入 L2b（XML 标签高亮）
        tagged_message = f"<user_requirement>\n{user_message}\n</user_requirement>"

        # ═══ 方案 D：条件注入约束（决策前）═══
        hints = self._match_constraints(user_message)
        if hints:
            hints_text = "\n".join(f"- [{h['level']}] {h['advice']}" for h in hints)
            tagged_message += f"\n\n<operational_hints>\n{hints_text}\n</operational_hints>"

        self.memory.add_message({"role": "user", "content": tagged_message})
        self._save_to_sqlite("user", user_message)

        global_broadcaster.emit_sync(
            "Master", "thinking", f"🧠 Master 正在思考: {user_message[:60]}..."
        )

        # ReAct 循环
        final_response = ""
        for step in range(MAX_STEPS):
            self._step += 1
            logger.info(f"🔄 Master Step {self._step} (loop {step + 1}/{MAX_STEPS})")

            # 构建消息 + 工具 schema
            messages = self.memory.build_messages()
            tools_schema = self.registry.to_openai_tools()

            # 调用 LLM
            try:
                response = await default_llm.chat_completion(
                    model=self.model,
                    messages=messages,
                    tools=tools_schema,
                    temperature=0.1,
                    enable_thinking=self.enable_thinking,
                    reasoning_effort=self._reasoning_effort,
                )
            except Exception as e:
                logger.error(f"❌ Master LLM 调用失败: {e}")
                final_response = f"LLM 调用失败: {e}"
                break

            # 校准 token 估算系数（用 API 返回的精确 prompt_tokens）
            from core.llm_client import token_tracker
            if token_tracker.last_prompt_tokens > 0:
                self.memory.calibrate(token_tracker.last_prompt_tokens)

            # 情况 1: LLM 没有调用工具 → 回复用户
            if not getattr(response, "tool_calls", None):
                content = getattr(response, "content", "") or ""
                # 传入 SDK 对象，add_message 会转为 dict 并保留 reasoning_content
                self.memory.add_message(response)
                self._save_to_sqlite("assistant", content)
                final_response = content
                logger.info(f"✅ Master 回复用户: {content[:100]}...")
                break

            # 情况 2: LLM 调用了工具 → 执行工具
            # 先将 assistant 消息（含 tool_calls）加入记忆
            self.memory.add_message(response)

            # 推送思考过程
            thinking = getattr(response, "content", "") or ""
            if thinking.strip():
                global_broadcaster.emit_sync(
                    "Master", "thinking", f"💭 {thinking[:200]}"
                )

            # ═══ Subagent 并行预执行 ═══
            # 如果本轮有多个 spawn_subagent，先并行执行，缓存结果
            # 非 subagent 工具仍走下方的串行循环
            import asyncio
            _subagent_results = {}  # tc.id → result
            subagent_calls = [
                tc for tc in response.tool_calls
                if tc.function.name == "spawn_subagent"
            ]
            if len(subagent_calls) > 1:
                # 并行执行所有 subagent
                async def _run_subagent(tc):
                    try:
                        sa_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        sa_args = {}
                    sa_args["_project_id"] = self.project_id
                    sa_args["_project_dir"] = self.project_dir
                    # 剥离末尾带 tool_calls 的 assistant 消息（同串行路径）
                    fork_msgs = self.memory.build_messages()
                    while (
                        fork_msgs
                        and isinstance(fork_msgs[-1], dict)
                        and fork_msgs[-1].get("role") == "assistant"
                        and fork_msgs[-1].get("tool_calls")
                    ):
                        fork_msgs = fork_msgs[:-1]
                    sa_args["_master_context"] = {
                        "messages": fork_msgs,
                        "registry": self.registry,
                        "model": self.model,
                        "enable_thinking": self.enable_thinking,
                        "reasoning_effort": self._reasoning_effort,
                    }
                    try:
                        return tc.id, await self.registry.call("spawn_subagent", **sa_args)
                    except Exception as e:
                        return tc.id, {"status": "error", "feedback": f"工具执行异常: {e}"}

                logger.info(f"🔀 并行执行 {len(subagent_calls)} 个 subagent")
                gather_results = await asyncio.gather(*[
                    _run_subagent(tc) for tc in subagent_calls
                ])
                _subagent_results = dict(gather_results)

            # 执行每个 tool_call（subagent 使用缓存结果，其余串行执行）
            for tc in response.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                # 递增全局步计数器（用于失败事件的间隔复现判断）
                self._step_counter += 1

                logger.info(
                    f"🔧 Master tool_call: {func_name}("
                    f"{', '.join(f'{k}={str(v)[:50]}' for k, v in args.items())})"
                )
                global_broadcaster.emit_sync(
                    "Master", "tool_call",
                    f"🔧 {func_name}({', '.join(f'{k}={str(v)[:30]}' for k, v in args.items())})",
                )

                # SQLite: 记录 tool_call
                target_file = args.get("target_file", args.get("file_path", None))
                self._save_to_sqlite(
                    "assistant", json.dumps(args, ensure_ascii=False, default=str)[:500],
                    tool_name=func_name, target_file=target_file,
                )

                # 自动注入项目上下文（工具通过 _ 前缀参数接收）
                args["_project_id"] = self.project_id
                args["_project_dir"] = self.project_dir
                # save_plan 需要 memory 来初始化 Todo 和注入动态上下文
                if func_name == "save_plan":
                    args["_memory"] = self.memory
                # spawn_subagent 需要完整上下文快照（fork 模型）
                if func_name == "spawn_subagent":
                    # 快照 messages，剥离末尾带 tool_calls 的 assistant 消息
                    # （该消息是当前轮次 Master 的 tool 调度，尚无对应 tool response，
                    #  直接传给 subagent 会导致 API 400: "tool_calls must be followed by tool messages"）
                    fork_messages = self.memory.build_messages()
                    while (
                        fork_messages
                        and isinstance(fork_messages[-1], dict)
                        and fork_messages[-1].get("role") == "assistant"
                        and fork_messages[-1].get("tool_calls")
                    ):
                        fork_messages = fork_messages[:-1]
                    args["_master_context"] = {
                        "messages": fork_messages,
                        "registry": self.registry,
                        "model": self.model,
                        "enable_thinking": self.enable_thinking,
                        "reasoning_effort": self._reasoning_effort,
                    }

                # 执行工具（并行预执行过的 subagent 使用缓存结果）
                if tc.id in _subagent_results:
                    result = _subagent_results[tc.id]
                else:
                    try:
                        result = await self.registry.call(func_name, **args)
                    except Exception as e:
                        result = {"status": "error", "feedback": f"工具执行异常: {e}"}
                        logger.error(f"❌ 工具 {func_name} 异常: {e}")

                # ═══ 自进化：失败事件记录 ═══
                if isinstance(result, dict) and result.get("status") in ("error", "fail"):
                    try:
                        error_msg = str(result.get("feedback", result.get("stderr", "")))
                        error_type = self._classify_error(error_msg)
                        self._trajectory_logger.log_failure(
                            session_id=self._session_id,
                            tool_name=func_name,
                            error_msg=error_msg,
                            step_index=self._step_counter,
                            error_type=error_type,
                        )
                    except Exception as e:
                        logger.debug(f"失败事件记录异常（不影响主流程）: {e}")

                # 序列化结果
                if isinstance(result, dict):
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                else:
                    result_str = str(result)

                # 截断过长的结果（防止上下文爆炸）
                if len(result_str) > 15000:
                    result_str = result_str[:15000] + "\n... (结果过长已截断)"

                # ═══ 方案 E：Tool Result 附加约束（执行后）═══
                tool_hints = self._match_constraints_for_tool(func_name, result)
                if tool_hints:
                    hints_text = " | ".join(f"[{h['level']}] {h['advice']}" for h in tool_hints)
                    result_str += f"\n\n⚠️ 历史经验提示: {hints_text}"

                # 工具结果加入记忆 + SQLite
                tool_step_id = self._save_to_sqlite(
                    "tool", result_str[:2000],  # SQLite 只存前 2000 字符
                    tool_name=func_name, target_file=target_file,
                )
                self.memory.add_tool_result(
                    tc.id, func_name, result_str, step_id=tool_step_id,
                )

                # ═══ save_plan 成功 → 广播 UI 事件（Todo 面板刷新）═══
                if func_name == "save_plan" and isinstance(result, dict):
                    if result.get("status") == "ok":
                        global_broadcaster.emit_sync(
                            "Master", "todo_init",
                            json.dumps(self.memory.todo_to_ui(), ensure_ascii=False),
                        )
                        logger.info(
                            "📋 save_plan 完成: %s",
                            result.get("summary", "?"),
                        )

                # ═══ update_todo → 写入 memory + WebSocket 广播 ═══
                if func_name == "update_todo" and isinstance(result, dict):
                    todo_result = self.memory.update_todo(
                        task_id=result.get("task", ""),
                        status=result.get("new_status", "pending"),
                        summary=result.get("summary", ""),
                    )
                    global_broadcaster.emit_sync(
                        "Master", "todo_update",
                        json.dumps(self.memory.todo_to_ui(), ensure_ascii=False),
                    )



                # 广播工具执行结果
                status = result.get("status", "?") if isinstance(result, dict) else "?"
                global_broadcaster.emit_sync(
                    "Master", "tool_result",
                    f"📦 {func_name} → {status}",
                )

                # ═══ task_done 出口：立即退出 ReAct 循环 ═══
                if func_name == "task_done" and isinstance(result, dict):
                    summary = result.get("summary", "任务完成")
                    files = result.get("files_created", "")
                    next_steps = result.get("next_steps", "")
                    parts = [summary]
                    if files:
                        parts.append(f"\n\n**创建的文件：**\n{files}")
                    if next_steps:
                        parts.append(f"\n\n**下一步：**\n{next_steps}")
                    final_response = "\n".join(parts)
                    msg = {"role": "assistant", "content": final_response}
                    if self.enable_thinking:
                        msg["reasoning_content"] = ""
                    self.memory.add_message(msg)
                    self._save_to_sqlite("assistant", final_response)

                    # ═══ 自进化：轨迹采集 ═══
                    try:
                        tool_calls_seq = self._extract_tool_call_sequence()
                        self._trajectory_logger.log_trajectory(
                            session_id=self._session_id,
                            tool_calls=tool_calls_seq,
                            task_summary=summary,
                            success=True,
                        )
                    except Exception as e:
                        logger.debug(f"轨迹采集异常（不影响主流程）: {e}")

                    logger.info(f"✅ Master task_done 退出: {summary[:100]}")
                    global_broadcaster.emit_sync("Master", "done", "✅ Master 完成")
                    return final_response

                # ═══ ask_user 出口：暂停 ReAct 循环，等待用户输入 ═══
                if func_name == "ask_user" and isinstance(result, dict):
                    if result.get("status") == "ask_user":
                        question = result.get("question", "")
                        context = result.get("context", "")
                        options = result.get("options", "")
                        parts = [question]
                        if context:
                            parts.append(f"\n\n{context}")
                        if options:
                            parts.append(f"\n\n{options}")
                        final_response = "\n".join(parts)
                        msg = {"role": "assistant", "content": final_response}
                        if self.enable_thinking:
                            msg["reasoning_content"] = ""
                        self.memory.add_message(msg)
                        self._save_to_sqlite("assistant", final_response)
                        logger.info(f"🙋 Master ask_user 暂停: {question[:100]}")
                        global_broadcaster.emit_sync("Master", "ask_user", "🙋 等待用户回复")
                        return final_response

            # 压缩检测：L2b 超阈值时触发压缩重建
            if self.memory.should_compress():
                logger.info("🗜️ L2b 超过阈值，触发压缩重建...")
                self._compress_rebuild()

        else:
            # 达到最大步数
            logger.warning(f"⚠️ Master 达到最大步数 ({MAX_STEPS})")
            final_response = (
                "我执行了太多步骤，可能陷入了循环。"
                "请检查目前的进度并告诉我下一步该怎么做。"
            )
            msg = {"role": "assistant", "content": final_response}
            if self.enable_thinking:
                msg["reasoning_content"] = ""
            self.memory.add_message(msg)

        global_broadcaster.emit_sync("Master", "done", f"✅ Master 完成")
        return final_response

    def _compress_rebuild(self) -> None:
        """触发确定性压缩重建（零 LLM 调用）。"""
        archive = self.memory.compress_rebuild()
        if archive:
            logger.info(f"🗜️ Master 压缩完成: {len(archive)} 字符归档")

    def get_stats(self) -> Dict[str, Any]:
        """返回 Master 状态统计"""
        stats = {
            "project_id": self.project_id,
            "model": self.model,
            "step": self._step,
            "tools": self.registry.list_names(),
            "memory": self.memory.get_stats(),
        }
        # SQLite 统计
        try:
            db = self._get_memory_db()
            stats["sqlite_steps"] = db.get_step_count(self.project_dir)
        except Exception:
            stats["sqlite_steps"] = -1
        return stats

    # ═══════════════════════════════════════════════════
    # 自进化辅助方法
    # ═══════════════════════════════════════════════════

    def _extract_tool_call_sequence(self) -> list:
        """从 L2b 对话链中回溯提取本轮所有 tool_calls。

        返回:
            [{name, args_keys, args_pattern, status}]
        """
        sequence = []
        messages = self.memory._conversation  # 直接访问 L2b 消息列表

        for msg in messages:
            role = msg.get("role", "")
            # assistant 消息中的 tool_calls
            if role == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    sequence.append({
                        "name": name,
                        "args_keys": list(args.keys()),
                        "args_pattern": self._build_args_pattern(name, args),
                        "status": "ok",  # 默认成功，后续可从 tool result 修正
                    })
            # tool result 可用于修正 status
            elif role == "tool":
                content = msg.get("content", "")
                if '"status": "error"' in content or '"status": "fail"' in content:
                    if sequence:
                        sequence[-1]["status"] = "error"

        return sequence

    @staticmethod
    def _build_args_pattern(tool_name: str, args: dict) -> dict:
        """生成参数的结构特征（不记录完整值，防止隐私泄露和存储膨胀）。

        规则：
        - 文件路径类：记录文件名或通配符模式
        - 代码类：记录前 30 字符
        - 命令类：记录命令前缀
        - 其他：跳过（不记录）
        """
        pattern = {}
        # 过滤内部注入参数
        filtered = {k: v for k, v in args.items() if not k.startswith("_")}

        for key, value in filtered.items():
            if not isinstance(value, str):
                continue
            if key in ("target_file", "file_path", "path"):
                # 文件路径：取 basename
                pattern[key] = os.path.basename(value) if value else ""
            elif key in ("code", "content", "new_code"):
                # 代码类：前 30 字符作为内容指纹
                pattern["code_prefix"] = value[:30] if value else ""
            elif key in ("command", "cmd"):
                # 命令类：前 30 字符
                pattern["command_prefix"] = value[:30] if value else ""
        return pattern

    @staticmethod
    def _classify_error(error_msg: str) -> str:
        """基于关键词简单分类错误类型（EvoAgent 后续精分类）。"""
        msg_lower = error_msg.lower()
        if any(k in msg_lower for k in ("not found", "no such file", "filenotfounderror")):
            return "param_error"
        if any(k in msg_lower for k in ("permission", "access denied")):
            return "env_error"
        if any(k in msg_lower for k in ("timeout", "timed out")):
            return "env_error"
        if any(k in msg_lower for k in ("encoding", "decode", "unicode")):
            return "param_error"
        return "unknown"

    # ═══════════════════════════════════════════════════
    # 约束匹配（方案 D + 方案 E）
    # ═══════════════════════════════════════════════════

    def _match_constraints(self, user_message: str, max_hints: int = 3) -> list:
        """方案 D：基于关键词匹配，从 ConstraintStore 中筛选相关约束。

        匹配逻辑：用户消息关键词 + 环境标签 + 上轮工具名 → 标签交集匹配。
        """
        if not self._constraint_store:
            return []

        context_tags = set()
        # 用户消息中的工具名关键词
        msg_lower = user_message.lower()
        for tool_name in self.registry.list_names():
            if tool_name in msg_lower:
                context_tags.add(tool_name)
        # 环境标签
        import platform
        context_tags.add(platform.system().lower())  # "windows" / "linux"
        # 上一轮 tool call 的工具名
        context_tags.update(self._get_recent_tool_names(n=3))

        matched = self._constraint_store.match(context_tags, max_results=max_hints)
        # 记录触发
        for c in matched:
            self._constraint_store.record_trigger(c["id"])
        return matched

    def _match_constraints_for_tool(
        self, tool_name: str, result: Any, max_hints: int = 2
    ) -> list:
        """方案 E：匹配与特定工具执行结果相关的约束（更严格）。

        要求工具名必须在 trigger_tags 中，且与上下文有交集。
        """
        if not self._constraint_store:
            return []

        context_tags = {tool_name}
        # 失败时加入错误类型标签
        if isinstance(result, dict) and result.get("status") in ("error", "fail"):
            error_msg = str(result.get("feedback", result.get("stderr", "")))
            for keyword in ["FileNotFoundError", "PermissionError", "timeout", "encoding"]:
                if keyword.lower() in error_msg.lower():
                    context_tags.add(keyword.lower())

        matched = self._constraint_store.match(
            context_tags, strict_tool=tool_name, max_results=max_hints
        )
        for c in matched:
            self._constraint_store.record_trigger(c["id"])
        return matched

    def _get_recent_tool_names(self, n: int = 3) -> list:
        """从 L2b 对话链中提取最近 n 个工具调用的工具名。"""
        names = []
        for msg in reversed(self.memory._conversation):
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    if name:
                        names.append(name)
                    if len(names) >= n:
                        return names
        return names

    # ═══════════════════════════════════════════════════
    # 合成工具加载
    # ═══════════════════════════════════════════════════

    def _load_synth_tools(self) -> None:
        """从 .astrea/synth_tools/ 加载合成工具到 Registry。"""
        try:
            from core.tool_synthesizer import ToolSynthesizer
            synth = ToolSynthesizer(self.project_dir)
            loaded = synth.load_from_disk(self.registry)
            if loaded > 0:
                self._refresh_tool_inventory()
                logger.info(f"🔧 已加载 {loaded} 个合成工具")
        except Exception as e:
            logger.debug(f"合成工具加载异常（不影响主流程）: {e}")

    # ═══════════════════════════════════════════════════
    # 自进化闭环编排
    # ═══════════════════════════════════════════════════

    def _load_evo_config(self) -> dict:
        """加载自进化配置（.astrea/config.json）。"""
        config_path = os.path.join(self.project_dir, ".astrea", "config.json")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        # 默认配置
        return {
            "evo_enabled": True,
            "round_counter": 0,
            "last_mine_traj_id": None,
            "last_mine_round": 0,
            "mine_cooldown_rounds": 10,
            "mine_window_size": 50,
            "min_new_trajectories": 5,
        }

    def _save_evo_config(self, config: dict) -> None:
        """保存自进化配置。"""
        config_path = os.path.join(self.project_dir, ".astrea", "config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    def increment_round(self) -> int:
        """递增轮次计数器（每次 handle_user_message 调用时调用）。"""
        config = self._load_evo_config()
        config["round_counter"] = config.get("round_counter", 0) + 1
        self._save_evo_config(config)
        return config["round_counter"]

    def _should_run_evolution(self) -> dict:
        """分层前置条件检查，返回 {"mine": bool, "analyze": bool}。

        两条通道独立判断，互不影响。
        """
        config = self._load_evo_config()
        result = {"mine": False, "analyze": False}

        # Gate 0: 全局开关
        if not config.get("evo_enabled", True):
            return result

        # ── Procedural Memory 通道 ──
        new_traj_count = self._trajectory_logger.count_since(
            config.get("last_mine_traj_id")
        )
        rounds_since_last_mine = (
            config.get("round_counter", 0) - config.get("last_mine_round", 0)
        )
        min_new = config.get("min_new_trajectories", 5)
        cooldown = config.get("mine_cooldown_rounds", 10)
        if new_traj_count >= min_new and rounds_since_last_mine >= cooldown:
            result["mine"] = True

        # ── Constraint Evolution 通道 ──
        recurrent = self._trajectory_logger.get_recurrent_failures(
            self._session_id, min_step_gap=3
        )
        if len(recurrent) > 0:
            result["analyze"] = True

        return result

    async def shutdown(self) -> None:
        """优雅关闭：保存记忆 + 后台触发自进化分析。"""
        # 保存记忆快照
        self.memory.save_to_disk()

        # 关闭 MCP
        await self.shutdown_mcp()

        # 保存约束
        if self._constraint_store:
            self._constraint_store.save()

        # 递增轮次
        self.increment_round()

        # 触发自进化分析（后台，不阻塞退出）
        gates = self._should_run_evolution()
        if gates["mine"] or gates["analyze"]:
            import threading
            t = threading.Thread(
                target=self._run_evolution_sync_wrapper,
                args=(gates,),
                daemon=True,
                name="evo-analysis",
            )
            t.start()
            logger.debug("自进化分析已提交后台线程")
        else:
            # 即使不触发分析，也执行熵减淘汰（零成本）
            self._run_cleanup_only()

    def _run_evolution_sync_wrapper(self, gates: dict) -> None:
        """后台线程入口：创建独立事件循环执行进化分析。"""
        import asyncio
        try:
            asyncio.run(self._run_evolution_analysis(gates))
        except (Exception, KeyboardInterrupt):
            # daemon 线程，中断或异常均静默忽略
            pass

    async def _run_evolution_analysis(self, gates: dict) -> None:
        """会话结束时的自进化分析。"""
        from core.evo_agent import EvoAgent
        from core.llm_client import default_llm

        evo = EvoAgent(llm_client=default_llm)
        config = self._load_evo_config()

        # ── 通道 1: 模式挖掘 → 工具合成 ──
        if gates.get("mine"):
            try:
                from core.pattern_miner import PatternMiner
                from core.tool_synthesizer import ToolSynthesizer

                synth = ToolSynthesizer(self.project_dir)
                miner = PatternMiner(self.project_dir)

                candidates = miner.mine(
                    min_freq=3, min_len=3,
                    since_traj_id=config.get("last_mine_traj_id"),
                    window_size=config.get("mine_window_size", 50),
                    exclude_hashes=synth.get_synthesized_hashes(),
                )
                for pattern in candidates:
                    result = await evo.synthesize_tool(pattern)
                    if result:
                        await synth.validate_and_register(
                            result["code"], result["metadata"], self.registry,
                        )

                # 更新增量游标
                config["last_mine_traj_id"] = self._trajectory_logger.get_latest_traj_id()
                config["last_mine_round"] = config.get("round_counter", 0)
                self._save_evo_config(config)

                # 熵减
                synth.run_entropy_reduction(
                    max_tools=20, current_round=config.get("round_counter", 0),
                )
            except Exception as e:
                logger.warning(f"模式挖掘/工具合成失败: {e}")

        # ── 通道 2: 失败分析 → 约束生成 ──
        if gates.get("analyze"):
            try:
                recurrent = self._trajectory_logger.get_recurrent_failures(
                    self._session_id, min_step_gap=3,
                )
                if recurrent:
                    new_constraints = await evo.analyze_failures(recurrent)
                    for constraint in new_constraints:
                        self._constraint_store.add(constraint)
                    self._constraint_store.save()
            except Exception as e:
                logger.warning(f"失败分析/约束生成失败: {e}")

        # ── 约束淘汰（无条件）──
        current_round = config.get("round_counter", 0)
        self._constraint_store.run_cleanup(current_round)
        self._constraint_store.save()

    def _run_cleanup_only(self) -> None:
        """仅执行熵减淘汰（不触发 LLM 分析）。"""
        try:
            config = self._load_evo_config()
            current_round = config.get("round_counter", 0)
            self._constraint_store.run_cleanup(current_round)
            self._constraint_store.save()

            from core.tool_synthesizer import ToolSynthesizer
            synth = ToolSynthesizer(self.project_dir)
            synth.run_entropy_reduction(max_tools=20, current_round=current_round)
        except Exception as e:
            logger.debug(f"熵减淘汰异常: {e}")




