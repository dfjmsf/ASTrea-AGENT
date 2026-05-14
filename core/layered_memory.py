"""
LayeredMemory — 三层分层记忆系统（纯 L2b 架构）

物理拼接顺序（缓存优化：前缀完全冻结，L2b 尾部 Append-only）：
  [L1 锚点+用户规则] → [L4 归档] → [L2b 对话链]
   永不变              压缩时才变     只追加

设计目标：
- 防迷失：大项目中不丢失早期决策
- 防失忆：跨会话恢复后立即恢复全局认知
- 防膨胀：长会话中上下文不超出窗口限制
- 极致缓存：L1+L4 前缀在会话生命周期内完全冻结，L2b 每步仅新增尾部 Token

状态获取方式（事件溯源）：
- 项目进度：save_plan / update_todo 返回值进入 L2b
- 文件树：create_code 成功时自动返回 / ls 工具按需获取
- AST 骨架：get_skeleton 工具按需获取单文件骨架
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LayeredMemory")

# Token 预算（DeepSeek 官方：英文 ~0.3 token/char, 中文 ~0.6 token/char, 混合约 2.5 chars/token）
CHAR_PER_TOKEN = 2.5
L2B_COMPRESS_THRESHOLD = int(120_000 * CHAR_PER_TOKEN)  # 300K 字符 ≈ 120K tokens
L2B_KEEP_RATIO = 0.15  # 压缩后保留最近 15%


class LayeredMemory:
    """三层分层记忆系统（纯 L2b 架构）。"""

    def __init__(self, system_prompt: str = "", project_dir: str = ""):
        self.project_dir = project_dir

        # ═══ L1: 锚点层（永不变）═══
        self._system_prompt = system_prompt
        self._user_rules = ""  # ASTrea.md 项目规则（拼接到 L1 后面，属于稳定前缀）
        self._skill_segments: List[str] = []  # Skill Prompt 片段（L1 中 user_rules 之后）
        self._hard_constraints = ""  # 硬约束（command_norms + discipline，L1 最末尾）

        # ═══ Todo 内部数据（供工具读取，不再注入上下文）═══
        self._todo_items: List[Dict[str, str]] = []
        # 每项: {task_id, target_file, description, status, summary}
        # status: pending | in_progress | completed | failed
        self._decisions: List[str] = []
        self._issues: List[str] = []

        # ═══ L4: 归档摘要（压缩时追加）═══
        self._archives: List[str] = []

        # ═══ L2b: 对话链（只追加，用户需求用 XML 标签高亮）═══
        self._conversation: List[Dict[str, Any]] = []

        # ═══ 统计 ═══
        self._compress_count = 0
        self._total_steps = 0
        self._needs_sanitize = True  # 首次 build 时做一次全链路校验
        self._calibrated_cpt: float = 0.0  # 校准后的 chars_per_token（0 = 未校准）
        # 会话唯一标识（创建时生成，save/load 全程不变）
        self._session_id: str = time.strftime("%Y%m%d_%H%M%S") + f"_{id(self) % 10000:04d}"
        self._source_snapshot: str = ""  # 恢复来源路径（用于归档去重）

    # ────────────────────────────────────────
    # L1: System Prompt
    # ────────────────────────────────────────

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def inject_user_rules(self, rules_text: str) -> None:
        """注入用户项目规则（ASTrea.md）到 L1 稳定前缀。"""
        self._user_rules = rules_text.strip() if rules_text else ""

    def set_skill_segments(self, segments: List[str]) -> None:
        """设置 Skill Prompt 片段（会话开始时或 /skill 切换时调用）。"""
        self._skill_segments = segments

    def set_hard_constraints(self, text: str) -> None:
        """设置硬约束文本（从 master_system.md 分离的安全规则，L1 最末尾）。"""
        self._hard_constraints = text.strip() if text else ""

    # ────────────────────────────────────────
    # L2a: Todo 项目态
    # ────────────────────────────────────────

    def init_todo(self, items: List[Dict[str, str]]) -> None:
        """从 plan_project 结果初始化 Todo 列表。

        参数:
            items: [{"task_id": "task_1", "target_file": "models.py",
                     "description": "定义数据模型"}]
        """
        self._todo_items = []
        for item in items:
            self._todo_items.append({
                "task_id": item.get("task_id", ""),
                "target_file": item.get("target_file", ""),
                "description": item.get("description", ""),
                "level": item.get("level", 0),
                "status": "pending",
                "summary": "",
            })
        logger.info(f"📋 Todo 初始化: {len(self._todo_items)} 项")

    def update_todo(self, task_id: str, status: str, summary: str = "") -> Dict[str, Any]:
        """更新 Todo 项状态（字段层）。

        参数:
            task_id: 任务 ID（如 "task_1"）或 target_file（如 "models.py"）
            status: pending | in_progress | completed | failed
            summary: 完成摘要（如 "3 个 CRUD 函数"）
        返回:
            {"status": "ok", "progress": "3/5"} 或 {"status": "not_found"}
        """
        for item in self._todo_items:
            if item["task_id"] == task_id or item["target_file"] == task_id:
                item["status"] = status
                if summary:
                    item["summary"] = summary
                progress = self._todo_progress()
                logger.info(
                    f"📋 Todo 更新: {item['target_file']} → {status} [{progress}]"
                )
                return {"status": "ok", "progress": progress}
        logger.warning(f"⚠️ Todo 未找到: {task_id}")
        return {"status": "not_found", "task_id": task_id}

    def _todo_progress(self) -> str:
        """返回进度字符串，如 '3/5'。"""
        if not self._todo_items:
            return "0/0"
        done = sum(1 for t in self._todo_items if t["status"] == "completed")
        return f"{done}/{len(self._todo_items)}"

    def todo_to_ui(self) -> Dict[str, Any]:
        """UI 层：返回结构化数据供 WebSocket/CLI 渲染。"""
        total = len(self._todo_items)
        completed = sum(1 for t in self._todo_items if t["status"] == "completed")
        failed = sum(1 for t in self._todo_items if t["status"] == "failed")
        in_progress = sum(1 for t in self._todo_items if t["status"] == "in_progress")
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "progress": self._todo_progress(),
            "items": [
                {
                    "task_id": t["task_id"],
                    "file": t["target_file"],
                    "desc": t["description"],
                    "status": t["status"],
                    "summary": t["summary"],
                }
                for t in self._todo_items
            ],
        }

    def add_decision(self, decision: str) -> None:
        """记录用户决策。"""
        self._decisions.append(decision)

    def render_todo_checkbox(self) -> str:
        """渲染 Todo Checkbox 文本（供工具返回值使用）。

        渲染为:
            【项目进度 3/5】
            [x] L0 | models.py: 定义数据模型 → 3 函数
            [/] L1 | routes.py: API 路由 ← 进行中
            [ ] L1 | index.html: 前端界面
            [!] L0 | style.css: 样式 → 语法错误
        """
        if not self._todo_items:
            return ""
        progress = self._todo_progress()
        lines = [f"【项目进度 {progress}】"]
        for t in self._todo_items:
            s = t["status"]
            if s == "completed":
                mark = "x"
                suffix = f" → {t['summary']}" if t["summary"] else ""
            elif s == "in_progress":
                mark = "/"
                suffix = " ← 进行中"
            elif s == "failed":
                mark = "!"
                suffix = f" → {t['summary']}" if t["summary"] else " → 失败"
            else:
                mark = " "
                suffix = ""
            lvl = t.get('level', 0)
            lines.append(f"[{mark}] L{lvl} | {t['target_file']}: {t['description']}{suffix}")
        return "\n".join(lines)

    # ────────────────────────────────────────
    # L4: 归档管理
    # ────────────────────────────────────────

    def add_archive(self, archive_markdown: str) -> None:
        """追加一条归档摘要（压缩时调用）。"""
        self._archives.append(archive_markdown)

    def inject_permanent(self, role: str, content: str) -> None:
        """注入永久上下文（project_spec → L4 归档层）。"""
        self._archives.insert(0, content)

    # ────────────────────────────────────────
    # L2b: 对话链
    # ────────────────────────────────────────

    def add_message(self, message) -> None:
        """添加消息到 L2b 对话链。

        接受 dict 或 OpenAI SDK ChatCompletionMessage 对象。
        SDK 对象会被转为 dict，确保 DeepSeek 的 reasoning_content 被保留
        （思考模式下多轮对话必须回传该字段，否则 API 返回 400）。
        """
        if isinstance(message, dict):
            self._conversation.append(message)
        else:
            # OpenAI SDK 对象 → dict（model_dump 包含 __pydantic_extra__ 中的扩展字段）
            msg_dict = message.model_dump(exclude_none=True, exclude_unset=True)
            # 保留 tool_calls 的完整结构
            if hasattr(message, "tool_calls") and message.tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            self._conversation.append(msg_dict)
        self._total_steps += 1

    def add_tool_result(
        self, tool_call_id: str, name: str, content: str,
        step_id: int = None,
    ) -> None:
        """添加工具结果到 L2b。

        参数:
            step_id: SQLite 自增主键，直接嵌入 content 头部，
                     LLM 可见，压缩器可解析。
        """
        # step_id 立即可见：嵌入 content 头部
        if step_id is not None:
            content = f"[step:{step_id}] {content}"
        self.add_message({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        })

    # ────────────────────────────────────────
    # 核心：build_messages（三层拼装）
    # ────────────────────────────────────────

    def build_messages(self) -> List[Dict[str, Any]]:
        """构建完整的 messages 列表。
        
        拼接顺序：[L1] → [L4] → [L2b]

        L1 内部拼接顺序（后文优先原则，确保硬约束不被 Skill 覆盖）：
            system_prompt（软偏好）
            → 项目规则
            → Skills（领域定制）
            → 硬约束（command_norms + discipline，最高权重）

        L1+L4 前缀在会话生命周期内完全冻结（仅压缩时 L4 追加一次），
        L2b 为 Append-only 对话链，每步仅新增尾部 Token，缓存命中率趋近 100%。
        """
        messages = []

        # L1: System Prompt + 用户规则 + Skills + 硬约束
        if self._system_prompt:
            l1_content = self._system_prompt
            if self._user_rules:
                l1_content += "\n\n【项目规则】\n" + self._user_rules
            if self._skill_segments:
                l1_content += "\n\n【Skills】\n" + "\n\n---\n\n".join(self._skill_segments)
            if self._hard_constraints:
                l1_content += "\n\n" + self._hard_constraints
            messages.append({"role": "system", "content": l1_content})

        # L4: 归档摘要（压缩时才变）
        if self._archives:
            archive_text = "\n\n---\n\n".join(self._archives)
            messages.append({"role": "user", "content": f"【记忆归档】\n{archive_text}"})

        # L2b: 对话链（Append-only）
        if self._needs_sanitize:
            self._sanitize_conversation()
        messages.extend(self._conversation)

        return messages

    def _sanitize_conversation(self) -> None:
        """全链路扫描 L2b 对话链，修复 tool_calls/tool 不对齐问题。
        仅在 _needs_sanitize=True 时由 build_messages 调用（快照恢复/压缩后/首次）。

        API 要求：每个 role=tool 消息必须紧跟在一条含匹配 tool_call_id
        的 assistant(tool_calls) 消息之后。

        正向遍历，逐条验证：
        - assistant(tool_calls) → 记录 pending_ids
        - tool → 检查 tool_call_id 在 pending_ids 中
        - user / assistant(无 tool_calls) → 清空 pending，放行

        不合法的消息被丢弃（而非整组裁掉），最大程度保留有效上下文。
        """
        conv = self._conversation
        if not conv:
            return []

        cleaned = []
        # pending_ids: 当前等待 tool result 的 tool_call_id 集合
        pending_ids: set = set()
        removed = 0

        for msg in conv:
            role = msg.get("role", "")

            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    # 如果上一轮的 pending_ids 还没清完（残缺组），
                    # 需要回退删除那条不完整的 assistant
                    if pending_ids:
                        # 找到并移除上一条残缺的 assistant+tool 组
                        while cleaned and (
                            cleaned[-1].get("role") == "tool"
                            or (cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls"))
                        ):
                            cleaned.pop()
                            removed += 1
                        pending_ids.clear()

                    cleaned.append(msg)
                    pending_ids = set()
                    for tc in tool_calls:
                        tc_id = tc.get("id", "")
                        if tc_id:
                            pending_ids.add(tc_id)
                else:
                    # 纯文本 assistant 回复
                    if pending_ids:
                        # 上一组 tool_calls 还没收全 result，裁掉那组
                        while cleaned and (
                            cleaned[-1].get("role") == "tool"
                            or (cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls"))
                        ):
                            cleaned.pop()
                            removed += 1
                        pending_ids.clear()
                    cleaned.append(msg)

            elif role == "tool":
                tid = msg.get("tool_call_id", "")
                if tid in pending_ids:
                    cleaned.append(msg)
                    pending_ids.discard(tid)
                else:
                    # 孤立 tool 消息（无匹配的 tool_calls）→ 丢弃
                    removed += 1

            elif role == "user":
                if pending_ids:
                    # 上一组未完成，裁掉
                    while cleaned and (
                        cleaned[-1].get("role") == "tool"
                        or (cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls"))
                    ):
                        cleaned.pop()
                        removed += 1
                    pending_ids.clear()
                cleaned.append(msg)

            else:
                # system 等其他角色
                cleaned.append(msg)

        # 尾部残缺处理：pending_ids 还有剩余
        if pending_ids:
            while cleaned and (
                cleaned[-1].get("role") == "tool"
                or (cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls"))
            ):
                cleaned.pop()
                removed += 1

        if removed > 0:
            logger.warning(
                f"⚠️ 对话链修复: 丢弃 {removed} 条不合法消息"
                f"（tool_calls/tool 不对齐，可能由中断或快照损坏导致）"
            )
            self._conversation = cleaned

        self._needs_sanitize = False

    # ────────────────────────────────────────
    # 压缩检测
    # ────────────────────────────────────────

    def l2b_char_count(self) -> int:
        """估算 L2b 当前字符数。"""
        total = 0
        for msg in self._conversation:
            if isinstance(msg, dict):
                total += len(msg.get("content", "") or "")
            else:
                # OpenAI response object
                total += len(getattr(msg, "content", "") or "")
        return total

    def _total_message_chars(self) -> int:
        """计算 build_messages 输出的全部消息总字符数（L1+L4+L2b）。"""
        total = 0
        # L1
        if self._system_prompt:
            total += len(self._system_prompt)
            if self._user_rules:
                total += len(self._user_rules) + 10  # 标签开销
            if self._skill_segments:
                total += sum(len(s) for s in self._skill_segments) + 20
            if self._hard_constraints:
                total += len(self._hard_constraints) + 5
        # L4
        for arc in self._archives:
            total += len(arc) + 15
        # L2b
        total += self.l2b_char_count()
        return total

    def calibrate(self, prompt_tokens: int) -> None:
        """用 API 返回的精确 prompt_tokens 校准 chars_per_token 系数。

        prompt_tokens 包含 L1+L4+L2b+工具schema，但工具 schema 在会话内
        相对固定，整体 chars/token 比率仍然有效。
        """
        if prompt_tokens <= 0:
            return
        total_chars = self._total_message_chars()
        if total_chars <= 0:
            return
        # 滑动平均：新值权重 0.3，历史权重 0.7（防止单次异常值抖动）
        new_cpt = total_chars / prompt_tokens
        if self._calibrated_cpt > 0:
            self._calibrated_cpt = self._calibrated_cpt * 0.7 + new_cpt * 0.3
        else:
            self._calibrated_cpt = new_cpt

    def l2b_token_count(self) -> int:
        """返回 L2b 的 token 估算值（校准后精度 ±5%，未校准则用默认系数）。"""
        chars = self.l2b_char_count()
        cpt = self._calibrated_cpt if self._calibrated_cpt > 0 else CHAR_PER_TOKEN
        return int(chars / cpt) if cpt > 0 else 0

    def should_compress(self) -> bool:
        """L2b 是否超过压缩阈值（校准后用 token 数，未校准用字符数）。"""
        if self._calibrated_cpt > 0:
            # 校准后：直接比较 token 数
            return self.l2b_token_count() > (L2B_COMPRESS_THRESHOLD // CHAR_PER_TOKEN)
        # 未校准：回退到字符比较
        return self.l2b_char_count() > L2B_COMPRESS_THRESHOLD

    # ────────────────────────────────────────
    # 压缩重建（确定性，零 LLM）
    # ────────────────────────────────────────

    # 工具分级：决定压缩时的处理策略
    _DISCARD_TOOLS = frozenset({"read_file", "get_skeleton", "ls", "grep_search"})
    _SKELETON_TOOLS = frozenset({"edit_code", "run_command", "update_todo"})
    # 其余工具（create_code, write_file, save_plan, save_phases,
    #           ask_user, task_done, recall_step）→ 保留摘要

    @staticmethod
    def _extract_step_id(content: str) -> tuple:
        """从 content 中提取 [step:N] 标记和去除标记后的 JSON 字符串。

        返回: (step_tag: str, json_str: str)
        """
        import re
        m = re.match(r'\[step:(\d+)\]\s*', content)
        if m:
            return f"[step:{m.group(1)}]", content[m.end():]
        return "[step:?]", content

    @staticmethod
    def _safe_json_loads(s: str) -> dict:
        """安全解析 JSON，失败返回空 dict。"""
        try:
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _compress_tool_msg(self, msg: dict) -> Optional[str]:
        """确定性压缩单条 tool 消息。

        返回:
            str  → 压缩后的一行摘要
            None → 完全丢弃
        """
        name = msg.get("name", "unknown")
        content = msg.get("content", "")
        step_tag, json_str = self._extract_step_id(content)
        data = self._safe_json_loads(json_str)
        status = data.get("status", data.get("action", "?"))
        target = data.get("target_file", data.get("file_path", ""))

        # ═══ 完全丢弃：纯感知操作 ═══
        if name in self._DISCARD_TOOLS:
            return None

        # ═══ 保留骨架：修改 / 命令 / Todo ═══
        if name == "edit_code":
            desc = data.get("summary", "")[:60]
            lines = data.get("new_line_count", "")
            line_info = f", {lines}行" if lines else ""
            return f"- {step_tag} edit_code → {target} ({status}{line_info}, {desc})"

        if name == "run_command":
            rc = data.get("returncode", "?")
            # 失败时保留 stderr 前 80 字符
            err = ""
            if status == "fail":
                stderr = data.get("stderr", "") or data.get("feedback", "")
                if stderr:
                    err = f" | {stderr[:80]}"
            return f"- {step_tag} run_command ({status}, rc={rc}{err})"

        if name == "update_todo":
            task = data.get("task", "?")
            new_status = data.get("new_status", "?")
            return f"- {step_tag} update_todo → {task} → {new_status}"

        # ═══ 保留摘要：创建 / 写入 / 规划 / 交互 / 完成 ═══
        if name == "create_code":
            lines = data.get("lines", "?")
            summary = data.get("summary", "")[:80]
            return f"- {step_tag} create_code → {target} ({status}, {lines}行, {summary})"

        if name == "write_file":
            summary = data.get("summary", "")[:60]
            return f"- {step_tag} write_file → {summary}"

        if name in ("save_plan", "save_phases"):
            file_count = len(data.get("files", data.get("build_order", [])))
            dag = data.get("dag_levels", "")[:60]
            return f"- {step_tag} {name} → {file_count}文件 ({dag})"

        if name == "ask_user":
            question = data.get("question", "")[:100]
            return f"- {step_tag} ask_user → \"{question}\""

        if name == "task_done":
            summary = data.get("summary", "")[:100]
            return f"- {step_tag} task_done → {summary}"

        # ═══ 兜底：未知工具 ═══
        return f"- {step_tag} {name} ({status})"

    def _compress_assistant_msg(self, msg: dict) -> Optional[str]:
        """压缩 assistant 消息（含 tool_calls）。

        返回:
            str  → 压缩后的描述
            None → 丢弃（无内容的纯 tool_calls 触发消息）
        """
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            # 提取调用的工具列表（不含参数，参数在 tool 消息里）
            calls = []
            for tc in tool_calls:
                func = tc.get("function", {})
                fname = func.get("name", "?")
                # 跳过纯感知工具的 tool_call 记录
                if fname in self._DISCARD_TOOLS:
                    continue
                # 从 arguments 中提取 target_file（如果有）
                try:
                    args = json.loads(func.get("arguments", "{}"))
                    tf = args.get("target_file", args.get("file_path", ""))
                except (json.JSONDecodeError, TypeError):
                    tf = ""
                calls.append(f"{fname}({tf})" if tf else fname)
            if calls:
                return f"- [调用] {', '.join(calls)}"
            return None  # 全是感知工具的 tool_call，整条丢弃

        # 纯文本 assistant 回复（给用户的回复）
        content = (msg.get("content", "") or "").strip()
        if content:
            return f"- [回复] {content[:120]}"
        return None

    def _compress_user_msg(self, msg: dict) -> Optional[str]:
        """压缩 user 消息：保留用户需求的核心摘要。"""
        content = (msg.get("content", "") or "").strip()
        if not content:
            return None
        # 去除 XML 标签
        import re
        clean = re.sub(r'</?user_requirement>', '', content).strip()
        if clean:
            return f"- [用户] {clean[:150]}"
        return None

    def compress_rebuild(self) -> str:
        """确定性压缩重建 L2b 对话链（零 LLM 调用）。

        流程：
        1. 取 L2b 前 85% 旧消息
        2. 按工具分级确定性提取骨架/摘要（完全丢弃纯感知操作）
        3. 归档为结构化 Markdown，追加到 L4
        4. 仅保留最近 15% 原始消息

        返回:
            压缩归档文本
        """
        total = len(self._conversation)
        if total < 10:
            logger.info("L2b 消息过少，跳过压缩")
            return ""

        # 计算分割点（前 85% 压缩，后 15% 保留）
        keep_count = max(int(total * L2B_KEEP_RATIO), 4)
        old_messages = self._conversation[:-keep_count]
        new_messages = self._conversation[-keep_count:]

        logger.info(
            f"🗜️ 压缩重建: {total} 条 → 压缩 {len(old_messages)} 条, "
            f"保留 {len(new_messages)} 条"
        )

        # ═══ 确定性压缩：按消息类型分类处理 ═══
        created = []    # 创建 / 写入
        modified = []   # 编辑
        commands = []   # 命令执行
        planning = []   # 规划 / Todo
        user_decisions = []  # 用户输入
        interactions = []    # ask_user / task_done / 回复
        other = []      # 兜底

        for msg in old_messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")

            if role == "tool":
                name = msg.get("name", "")
                line = self._compress_tool_msg(msg)
                if line is None:
                    continue  # 丢弃
                # 分类归组
                if name in ("create_code", "write_file"):
                    created.append(line)
                elif name == "edit_code":
                    modified.append(line)
                elif name == "run_command":
                    commands.append(line)
                elif name in ("save_plan", "save_phases", "update_todo"):
                    planning.append(line)
                elif name in ("ask_user", "task_done"):
                    interactions.append(line)
                else:
                    other.append(line)

            elif role == "assistant":
                line = self._compress_assistant_msg(msg)
                if line:
                    interactions.append(line)

            elif role == "user":
                line = self._compress_user_msg(msg)
                if line:
                    user_decisions.append(line)

        # ═══ 组装归档 Markdown ═══
        sections = []
        if user_decisions:
            sections.append("### 用户需求\n" + "\n".join(user_decisions))
        if planning:
            sections.append("### 规划\n" + "\n".join(planning))
        if created:
            sections.append("### 创建\n" + "\n".join(created))
        if modified:
            sections.append("### 修改\n" + "\n".join(modified))
        if commands:
            sections.append("### 命令\n" + "\n".join(commands))
        if interactions:
            sections.append("### 交互\n" + "\n".join(interactions))
        if other:
            sections.append("### 其他\n" + "\n".join(other))

        archive_md = "\n\n".join(sections) if sections else "（无可压缩内容）"

        # 追加到 L4 归档
        self._compress_count += 1
        header = f"## 归档 #{self._compress_count} (共 {len(old_messages)} 条消息)"
        self._archives.append(f"{header}\n{archive_md}")

        # 替换 L2b 为最近 15%
        self._conversation = new_messages
        self._needs_sanitize = True  # 切割点可能落在 tool_calls 组中间

        logger.info(
            f"🗜️ 压缩完成: L4 归档 +1 (共 {len(self._archives)}), "
            f"L2b 从 {total} → {len(self._conversation)}"
        )

        return archive_md

    def content_hash(self) -> str:
        """返回会话唯一标识（创建时生成，save/load 全程不变）。"""
        return self._session_id

    def save_to_disk(self, save_path: str = "") -> str:
        """序列化当前全层级记忆到 JSON 文件。

        保存内容：L2b(对话链) + L4(归档) + Todo + 统计
        L1(system_prompt) 不保存——每次启动时重新构建，避免 prompt 更新后快照过期。

        参数:
            save_path: 保存路径。为空则默认 {project_dir}/.astrea/memory_snapshot.json
        返回:
            实际保存路径
        """
        if not save_path:
            base = self.project_dir
            if not base:
                logger.warning("⚠️ 无 project_dir，跳过磁盘持久化")
                return ""
            save_path = os.path.join(base, ".astrea", "memory_snapshot.json")

        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        snapshot = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "content_hash": self.content_hash(),
            "project_dir": self.project_dir,
            "stats": {
                "compress_count": self._compress_count,
                "total_steps": self._total_steps,
            },
            "l4_archives": self._archives,
            "l2b_conversation": self._conversation,
            "todo_items": self._todo_items,
            "decisions": self._decisions,
            "issues": self._issues,
        }

        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

        msg_count = len(self._conversation)
        logger.info(f"💾 记忆已保存: {save_path} ({msg_count} 条消息)")
        return save_path

    def load_from_disk(self, load_path: str = "") -> bool:
        """从 JSON 文件恢复全层级记忆。

        参数:
            load_path: 加载路径。为空则默认 {project_dir}/.astrea/memory_snapshot.json
        返回:
            是否成功恢复
        """
        if not load_path:
            base = self.project_dir
            if not base:
                return False
            load_path = os.path.join(base, ".astrea", "memory_snapshot.json")

        if not os.path.isfile(load_path):
            return False

        try:
            with open(load_path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)

            # 版本检查
            if snapshot.get("version") != 1:
                logger.warning(f"⚠️ 快照版本不兼容: {snapshot.get('version')}")
                return False

            # 恢复各层
            self._archives = snapshot.get("l4_archives", [])
            self._conversation = snapshot.get("l2b_conversation", [])
            self._todo_items = snapshot.get("todo_items", [])
            self._decisions = snapshot.get("decisions", [])
            self._issues = snapshot.get("issues", [])

            # 恢复统计
            stats = snapshot.get("stats", {})
            self._compress_count = stats.get("compress_count", 0)
            self._total_steps = stats.get("total_steps", 0)

            msg_count = len(self._conversation)
            saved_at = snapshot.get("saved_at", "?")
            # 恢复 session_id（保持同一会话身份不变）
            if snapshot.get("content_hash"):
                self._session_id = snapshot["content_hash"]
            # 记录来源路径（归档去重：恢复出的会话不重复归档）
            self._source_snapshot = os.path.abspath(load_path)
            logger.info(
                f"记忆已恢复: {msg_count} 条消息, "
                f"{len(self._archives)} 条归档, "
                f"保存于 {saved_at}"
            )
            self._needs_sanitize = True  # 快照可能含残缺数据
            return True

        except Exception as e:
            logger.warning(f"⚠️ 记忆恢复失败: {e}")
            return False

    # ────────────────────────────────────────
    # 统计
    # ────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "system_prompt_len": len(self._system_prompt),
            "skill_segments": len(self._skill_segments),
            "hard_constraints_len": len(self._hard_constraints),
            "l4_archives": len(self._archives),
            "l2b_messages": len(self._conversation),
            "l2b_chars": self.l2b_char_count(),
            "l2b_tokens_est": self.l2b_char_count() // CHAR_PER_TOKEN,
            "todo_items": len(self._todo_items),
            "compress_count": self._compress_count,
            "total_steps": self._total_steps,
        }

