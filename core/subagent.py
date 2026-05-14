"""
SubagentRunner — Master 的一次性侦察兵。

fork Master 的完整上下文（L1+L4+L2B），在独立 ReAct 循环中
执行只读调查任务，返回结构化报告后自动销毁。

设计原则：
- 用完即弃：中间过程不持久化，仅通过 logger.debug 可追溯
- 只读边界：工具黑名单过滤所有写操作 + 递归 spawn
- 模型一致：与 Master 使用同一模型，保证 KV Cache 前缀共享
"""
import json
import logging
import re

from core.llm_client import default_llm, token_tracker
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("Subagent")


class SubagentRunner:
    """Master 的一次性侦察兵 — fork 上下文，只读调查，返回结论。"""

    # subagent 只暴露只读调查工具；用白名单抵消 Master L1 工具清单的干扰。
    TOOL_ALLOWLIST = {
        "grep_search", "read_file", "get_skeleton", "ls", "run_command",
        "submit_subagent_report",
    }

    def __init__(
        self,
        messages_snapshot: list,
        registry,               # ToolRegistry（共享引用）
        model: str,
        project_dir: str,
        max_steps: int = 25,
        enable_thinking: bool = False,
        reasoning_effort: str = "low",
    ):
        self._messages = messages_snapshot
        self._registry = registry
        self._model = model
        self._project_dir = project_dir
        self._max_steps = max_steps
        self._enable_thinking = enable_thinking
        self._reasoning_effort = reasoning_effort

    def _filtered_tools_schema(self) -> list:
        """返回 subagent 真实可用的工具 schema。"""
        return [
            t for t in self._registry.to_openai_tools()
            if t["function"]["name"] in self.TOOL_ALLOWLIST
        ]

    async def run(self, task_description: str) -> dict:
        """执行 subagent 只读 ReAct 循环。"""
        from core.tools.subagent.prompts import SUBTASK_PROMPT

        # 注入子任务指令到 messages 尾部
        self._messages.append({
            "role": "user",
            "content": SUBTASK_PROMPT.format(task=task_description),
        })

        tools_schema = self._filtered_tools_schema()
        total_tokens = 0

        for step in range(self._max_steps):
            # LLM 调用（参数与 Master 一致，保证模型行为对齐）
            try:
                response = await default_llm.chat_completion(
                    model=self._model,
                    messages=self._messages,
                    tools=tools_schema,
                    temperature=0.1,
                    enable_thinking=self._enable_thinking,
                    reasoning_effort=self._reasoning_effort,
                )
            except Exception as e:
                logger.error(f"❌ Subagent LLM 调用失败: {e}")
                return {
                    "status": "error",
                    "result": f"Subagent LLM 调用失败: {e}",
                    "steps_used": step,
                    "tokens_used": total_tokens,
                }

            # Token 统计（累计，但不校准 Master 的 memory）
            total_tokens += (
                token_tracker.last_prompt_tokens
                + getattr(
                    getattr(response, "usage", None),
                    "completion_tokens", 0
                )
            )

            # 无工具调用 → 任务完成，解析报告
            if not getattr(response, "tool_calls", None):
                content = getattr(response, "content", "") or ""
                report = self._parse_report(content)
                if report.get("_invalid_report"):
                    logger.warning(
                        f"⚠️ Subagent 报告格式非法，尝试 JSON 收口: "
                        f"{report.get('validation_error')}"
                    )
                    repaired_report, repair_tokens = await self._repair_report(
                        content, report.get("validation_error", "报告格式非法")
                    )
                    total_tokens += repair_tokens
                    if not repaired_report.get("_invalid_report"):
                        report = repaired_report
                status = "invalid_report" if report.get("_invalid_report") else "ok"
                logger.info(
                    f"✅ Subagent 完成 ({step + 1} steps, {total_tokens} tokens)"
                )
                return {
                    "status": status,
                    "result": report,
                    "steps_used": step + 1,
                    "tokens_used": total_tokens,
                }

            # 有工具调用 → 将 assistant message 加入 messages
            assistant_msg = self._response_to_dict(response)
            self._messages.append(assistant_msg)

            # 执行每个 tool_call
            for tc in response.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                # 注入 _project_dir（工具需要知道项目根目录）
                args["_project_dir"] = self._project_dir
                if func_name == "submit_subagent_report":
                    args["_caller_role"] = "subagent"

                logger.debug(
                    f"🔧 Subagent tool: {func_name}"
                    f"({', '.join(f'{k}={str(v)[:30]}' for k, v in args.items())})"
                )
                arg_preview = ", ".join(
                    f"{k}={str(v)[:50]}" for k, v in args.items()
                    if not k.startswith("_")
                )
                logger.info(f"🔧 Subagent tool_call: {func_name}({arg_preview})")
                global_broadcaster.emit_sync(
                    "Subagent", "tool_call",
                    f"🔧 {func_name}({arg_preview[:120]})",
                    payload={
                        "tool_name": func_name,
                        "args": {k: v for k, v in args.items() if not k.startswith("_")},
                    },
                )

                # 拦截白名单外工具（防 Master 上下文诱导递归或结束任务）
                if func_name not in self.TOOL_ALLOWLIST:
                    if func_name == "spawn_subagent":
                        feedback = (
                            "递归委派被拒绝：你当前就是 SUBAGENT_READONLY_WORKER，"
                            "不是 Master。请明确自己的身份，直接完成当前 <subtask>，"
                            "不要再调用 spawn_subagent。"
                        )
                    else:
                        feedback = f"Subagent 无权使用此工具: {func_name}"
                    result = {"status": "error", "feedback": feedback}
                    logger.warning(f"🚫 Subagent 尝试调用非白名单工具被拦截: {func_name}")
                else:
                    # 通过 registry.call 执行（含风险拦截）
                    # subagent 不设 confirm_callback，confirm 级风险操作自动跳过
                    try:
                        result = await self._registry.call(func_name, **args)
                    except Exception as e:
                        result = {"status": "error", "feedback": f"工具异常: {e}"}
                        logger.debug(f"❌ Subagent 工具异常: {func_name} → {e}")

                # 序列化结果
                if isinstance(result, dict):
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                else:
                    result_str = str(result)

                # 截断（subagent 侧也需要防止上下文爆炸）
                if len(result_str) > 10000:
                    result_str = result_str[:10000] + "\n... (截断)"

                status = result.get("status", "?") if isinstance(result, dict) else "?"
                logger.info(f"📦 Subagent tool_result: {func_name} → {status}")
                global_broadcaster.emit_sync(
                    "Subagent", "tool_result",
                    f"📦 {func_name} → {status}",
                    payload={
                        "tool_name": func_name,
                        "status": status,
                    },
                )

                if func_name == "submit_subagent_report" and status == "ok":
                    logger.info(
                        f"✅ Subagent 通过 submit_subagent_report 完成 "
                        f"({step + 1} steps, {total_tokens} tokens)"
                    )
                    return {
                        "status": "ok",
                        "result": result["report"],
                        "steps_used": step + 1,
                        "tokens_used": total_tokens,
                    }

                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        # 步数耗尽
        logger.warning(f"⚠️ Subagent 步数耗尽 ({self._max_steps} steps)")
        return {
            "status": "max_steps",
            "result": "子任务步数耗尽，未得出完整结论",
            "steps_used": self._max_steps,
            "tokens_used": total_tokens,
        }

    @staticmethod
    def _response_to_dict(response) -> dict:
        """将 SDK response 对象转为 messages 可用的 dict。

        注意：DeepSeek 思考模式要求 assistant 消息必须回传 reasoning_content，
        否则 API 报 400: "reasoning_content in thinking mode must be passed back"。
        """
        msg = {"role": "assistant"}
        content = getattr(response, "content", None)
        if content:
            msg["content"] = content
        # DeepSeek 思考模式：必须保留 reasoning_content
        reasoning = getattr(response, "reasoning_content", None)
        if reasoning is not None:
            msg["reasoning_content"] = reasoning
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        return msg

    async def _repair_report(self, bad_content: str, validation_error: str) -> tuple[dict, int]:
        """用一轮无工具 LLM 调用把非法报告重写为标准 JSON。"""
        repair_messages = list(self._messages)
        repair_messages.append({
            "role": "assistant",
            "content": bad_content,
        })
        repair_messages.append({
            "role": "user",
            "content": (
                "<json_report_repair priority=\"highest\">\n"
                f"上一条输出非法：{validation_error}\n"
                "不要继续调查，不要调用工具，不要输出 markdown。\n"
                "必须只输出一个合法 JSON 对象，顶层字段必须且只能遵循：\n"
                "{\n"
                "  \"conclusion\": \"2-5句摘要；如果范围不清，以 scope_unclear: 开头\",\n"
                "  \"findings\": [\n"
                "    {\"file\": \"绝对路径或null\", \"line\": 行号或null, \"detail\": \"具体发现或缺失边界\"}\n"
                "  ],\n"
                "  \"suggestion\": \"只围绕当前 <subtask> 给 Master 的下一步建议\"\n"
                "}\n"
                "findings 不能为空。不要使用 status、summary、files_created、next_steps 等 Master/task_done 风格字段。\n"
                "</json_report_repair>"
            ),
        })

        try:
            response = await default_llm.chat_completion(
                model=self._model,
                messages=repair_messages,
                tools=None,
                temperature=0.0,
                enable_thinking=self._enable_thinking,
                reasoning_effort=self._reasoning_effort,
            )
        except Exception as e:
            logger.error(f"❌ Subagent JSON 收口失败: {e}")
            return self._parse_report(f"JSON 收口 LLM 调用失败: {e}"), 0

        tokens_used = (
            token_tracker.last_prompt_tokens
            + getattr(
                getattr(response, "usage", None),
                "completion_tokens", 0
            )
        )
        content = getattr(response, "content", "") or ""
        report = self._parse_report(content)
        if report.get("_invalid_report"):
            logger.warning(
                f"⚠️ Subagent JSON 收口仍非法: {report.get('validation_error')}"
            )
        return report, tokens_used

    @staticmethod
    def _parse_report(content: str) -> dict:
        """尝试从 subagent 输出中提取结构化 JSON 报告。

        LLM 可能输出纯 JSON、markdown 包裹的 JSON、或混合文本。
        解析优先级：纯 JSON → 提取 JSON 块 → 原始文本兜底。
        """
        def invalid(reason: str) -> dict:
            return {
                "_invalid_report": True,
                "validation_error": reason,
                "conclusion": "Subagent 输出不符合报告协议",
                "findings": [
                    {
                        "file": None,
                        "line": None,
                        "detail": reason,
                    }
                ],
                "suggestion": "重跑该 subagent 或收紧子任务描述",
                "raw_report": content[:4000],
            }

        def validate(parsed) -> dict:
            if not isinstance(parsed, dict):
                return invalid("报告不是 JSON 对象")
            findings = parsed.get("findings")
            if not isinstance(findings, list) or not findings:
                return invalid("findings 缺失或为空")
            for item in findings:
                if not isinstance(item, dict):
                    return invalid("findings 中存在非对象条目")
                if "file" not in item or "line" not in item or "detail" not in item:
                    return invalid("findings 条目缺少 file/line/detail 字段")
                if not str(item.get("detail", "")).strip():
                    return invalid("findings 条目的 detail 为空")
            if "conclusion" not in parsed or "suggestion" not in parsed:
                return invalid("报告缺少 conclusion 或 suggestion 字段")
            return parsed

        # 尝试 1：整体就是 JSON
        try:
            return validate(json.loads(content.strip()))
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试 2：从 markdown 代码块或混合文本中提取 JSON
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            try:
                return validate(json.loads(json_match.group()))
            except (json.JSONDecodeError, ValueError):
                pass

        return invalid("未能从输出中解析出合法 JSON 报告")
