"""
ASTrea CLI - Rich + prompt_toolkit 终端界面

用法: 
    astrea          # 全局命令
    python cli.py   # 直接运行
"""
import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.shortcuts import CompleteStyle
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.theme import Theme
from rich.align import Align
from rich.markdown import Markdown
from rich import box

# ── 常量 ──

PROJECTS_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "projects"
)

# 去除 emoji 的正则
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0001F900-\U0001F9FF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U00002B50"
    "\U000023CF"
    "\U000023E9-\U000023F3"
    "\U0001FA70-\U0001FAFF"
    "]+",
    flags=re.UNICODE,
)

# Rich 主题
THEME = Theme({
    "info": "dim cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "tool": "magenta",
    "master": "bold cyan",
    "user_input": "bold white",
})


# ── 日志处理器 ──

class CLILogHandler(logging.Handler):
    """将 Python logger 输出重定向到 Rich Console。
    
    支持两种模式：
    - verbose=True:  所有日志逐行输出（调试用）
    - verbose=False: 紧凑模式，tool call 相关 INFO 日志收集到缓冲区，
                     仅 WARNING/ERROR 立即输出
    """

    def __init__(self, console: Console, verbose: bool = True):
        super().__init__()
        self.console = console
        self.verbose = verbose
        # 紧凑模式下的 tool call 事件缓冲
        self._tool_buffer: list = []

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            # 去除 emoji
            msg = EMOJI_RE.sub("", msg).strip()
            if not msg:
                return

            # 简化 logger 名称
            name = record.name
            name = name.replace("Tool.", "").lower()

            level = record.levelno

            # ── 紧凑模式：收集而非打印 ──
            if not self.verbose and level < logging.WARNING:
                # 捕获 tool_call 和 tool_result 事件
                if "tool_call:" in msg.lower() or "tool_result:" in msg.lower():
                    self._tool_buffer.append({"name": name, "msg": msg})
                    return
                # 其它 INFO 日志在紧凑模式下也静默
                if name in ("master",) and level <= logging.INFO:
                    # 启动阶段汇总日志直接放行
                    if any(kw in msg for kw in ("注册", "恢复")):
                        pass  # 不 return，走下方直接输出
                    else:
                        # 仅保留关键状态行到缓冲区
                        if any(kw in msg for kw in ("Step", "LLM", "压缩")):
                            self._tool_buffer.append({"name": name, "msg": msg})
                        return

            # ── 详细模式或高级别日志：直接输出 ──
            if level >= logging.ERROR:
                style = "error"
            elif level >= logging.WARNING:
                style = "warning"
            else:
                style = "info"

            self.console.print(
                f"  [{style}][{name}][/{style}] {msg}"
            )
        except Exception:
            pass

    def flush_tool_buffer(self) -> list:
        """取出并清空 tool call 缓冲区。"""
        buf = self._tool_buffer[:]
        self._tool_buffer.clear()
        return buf


def render_tool_calls_compact(console: Console, buffer: list):
    """将收集的 tool call 日志渲染为紧凑表格。"""
    if not buffer:
        return

    # 解析 tool_call 条目
    import re as _re
    calls = []
    for item in buffer:
        msg = item["msg"]
        # 匹配 "Master tool_call: func_name(args...)" 格式
        m = _re.search(r'tool_call:\s*(\w+)\((.*)\)', msg)
        if m:
            calls.append({
                "tool": m.group(1),
                "args": m.group(2)[:60],  # 截断参数
                "status": "…",
            })

    if not calls:
        # 没有解析到 tool_call，输出原始摘要
        console.print(f"  [dim]({len(buffer)} 条日志已折叠，/verbose 查看详情)[/dim]")
        return

    # 紧凑单行输出
    for i, c in enumerate(calls, 1):
        args_display = c['args'] if c['args'] else ''
        console.print(
            f"  [dim]{i:>2}.[/dim] [cyan]{c['tool']:<16}[/cyan] "
            f"[dim]{args_display}[/dim]"
        )
    console.print(f"  [dim]共 {len(calls)} 次工具调用 | /verbose 切换详细日志[/dim]")


# ── Todo 渲染 ──

def render_todo(console: Console, todo_data: dict):
    """渲染 Todo 进度面板。"""
    if not todo_data or not todo_data.get("items"):
        return

    items = todo_data["items"]
    completed = todo_data.get("completed", 0)
    total = todo_data.get("total", len(items))

    table = Table(
        show_header=False,
        box=None,
        padding=(0, 1),
        title=f"[bold]项目进度 {completed}/{total}[/bold]",
        title_style="cyan",
    )
    table.add_column(width=3)
    table.add_column(ratio=1)

    for item in items:
        status = item.get("status", "pending")
        name = item.get("file", "unknown")
        desc = item.get("desc", "")
        summary = item.get("summary", "")

        if status == "completed":
            mark = "[green][x][/green]"
            detail = f"[green]{name}[/green]"
            if summary:
                detail += f" [dim]-> {summary}[/dim]"
        elif status == "in_progress":
            mark = "[yellow][/][/yellow]"
            detail = f"[yellow]{name}[/yellow] [dim]<- 进行中[/dim]"
        elif status == "failed":
            mark = "[red][!][/red]"
            detail = f"[red]{name}[/red]"
            if summary:
                detail += f" [dim]-> {summary}[/dim]"
        else:
            mark = "[ ]"
            detail = f"[dim]{name}[/dim]"

        if desc and status == "pending":
            detail += f" [dim italic]({desc[:30]})[/dim italic]"

        table.add_row(mark, detail)

    console.print(table)
    console.print()


# ── Banner ──

BANNER_LINES = [
    "██╗   █████╗ ███████╗████████╗██████╗ ███████╗ █████╗ ",
    "╚██╗ ██╔══██╗██╔════╝╚══██╔══╝██╔══██╗██╔════╝██╔══██╗",
    " ╚██╗███████║███████╗   ██║   ██████╔╝█████╗  ███████║",
    " ██╔╝██╔══██║╚════██║   ██║   ██╔══██╗██╔══╝  ██╔══██║",
    "██╔╝ ██║  ██║███████║   ██║   ██║  ██║███████╗██║  ██║",
    "╚═╝  ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝",
]


def print_banner(console: Console, workspace_hint: str = ""):
    """打印启动 Banner（左侧 logo + 右侧系统信息）。"""
    from core.llm_client import default_llm

    # 右侧信息行（与 banner 行数对齐）
    info_lines = [
        "  Version: v0.7.1",
        f"  {workspace_hint}" if workspace_hint else "",
        "",
        "  双击 Ctrl+C 退出",
        "  Enter 发送 | Ctrl+J 换行",
        "",
    ]

    # 补齐行数
    max_lines = max(len(BANNER_LINES), len(info_lines))
    while len(info_lines) < max_lines:
        info_lines.append("")

    table = Table(
        show_header=False,
        box=None,
        padding=(0, 0),
        expand=False,
    )
    table.add_column(style="cyan", no_wrap=True)        # banner
    table.add_column(style="cyan", width=1, no_wrap=True) # 分隔线
    table.add_column(style="cyan", no_wrap=True)          # 信息

    for i in range(max_lines):
        banner_line = BANNER_LINES[i] if i < len(BANNER_LINES) else ""
        info_line = info_lines[i] if i < len(info_lines) else ""
        sep = "│" if banner_line or info_line else " "
        table.add_row(banner_line, sep, info_line)

    console.print(
        Panel(table, border_style="cyan", padding=(0, 1))
    )
    console.print()


# ── 主循环 ──

def create_key_bindings():
    """键绑定：Enter 提交，Ctrl+J 换行。"""
    kb = KeyBindings()

    @kb.add(Keys.ControlJ)
    def _(event):
        """Ctrl+J 插入换行。"""
        event.current_buffer.insert_text("\n")

    return kb


async def async_main():
    console = Console(theme=THEME)

    # 工作区安全检测
    from cli_commands import check_workspace_safety, create_router
    cwd = os.getcwd()
    safety = check_workspace_safety(cwd, console)
    project_dir = safety["project_dir"]
    is_trusted = safety["trusted"]

    trust_tag = "[bold green]Trusted[/bold green]" if is_trusted else "[bold yellow]Isolated[/bold yellow]"
    workspace_hint = f"工作区: {project_dir} [{trust_tag}]"

    print_banner(console, workspace_hint)

    # 设置全局日志 -> CLILogHandler
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 清除已有 handler（防止重复输出）
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    cli_handler = CLILogHandler(console)
    cli_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(cli_handler)

    # 初始化 Master
    console.print("[dim]正在初始化 Master...[/dim]")

    try:
        from core.master import Master
    except ImportError as e:
        console.print(f"[error]Master 导入失败: {e}[/error]")
        console.print("[dim]请确认在 ASTrea 项目根目录下运行。[/dim]")
        sys.exit(1)

    dir_name = os.path.basename(cwd) or "workspace"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_id = f"{dir_name}_{timestamp}"

    master = Master(project_id=project_id, project_dir=project_dir)

    # 启动 MCP Server（async，需要事件循环）
    try:
        await master.init_mcp()
    except Exception as _mcp_err:
        console.print(f"[warning]MCP 初始化异常（不影响主流程）: {_mcp_err}[/warning]")

    # 启动时尝试恢复上次会话记忆
    snapshot_path = os.path.join(project_dir, ".astrea", "memory_snapshot.json")
    if os.path.isfile(snapshot_path):
        try:
            import json as _json
            with open(snapshot_path, "r", encoding="utf-8") as _f:
                _snap = _json.load(_f)
            _saved_at = _snap.get("saved_at", "?")
            _msg_count = len(_snap.get("l2b_conversation", []))
            console.print(
                f"[info]检测到上次会话快照[/info] "
                f"[dim]({_saved_at}, {_msg_count} 条消息)[/dim]"
            )
            if master.memory.load_from_disk(snapshot_path):
                console.print("[success]记忆已恢复[/success]")
            else:
                console.print("[warning]记忆恢复失败，以全新会话启动[/warning]")
        except Exception as _e:
            console.print(f"[warning]快照读取失败: {_e}[/warning]")

    console.print(
        f"[success]Master 就绪[/success] "
        f"| 项目: [cyan]{project_id}[/cyan] "
        f"| 模型: [dim]{master.model}[/dim]"
    )
    try:
        from core.config_paths import get_user_env_path, has_env_file
        from core.llm_client import default_llm
        if not has_env_file():
            console.print(
                Panel(
                    "未发现 `.env` 配置文件。\n\n"
                    "  运行 [bold cyan]/config init[/bold cyan] 可按向导创建用户级全局配置。\n"
                    f"  默认写入路径: [cyan]{get_user_env_path()}[/cyan]",
                    title="[yellow]配置提示[/yellow]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
        elif not default_llm.providers:
            console.print(
                Panel(
                    "已发现 `.env`，但没有加载到可用 LLM Provider。\n\n"
                    "  请检查 API Key / Base URL / MODELS，或运行 "
                    "[bold cyan]/config init[/bold cyan] 重新生成用户级配置。",
                    title="[yellow]Provider 未就绪[/yellow]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
    except Exception as _cfg_err:
        console.print(f"[dim]配置检查跳过: {_cfg_err}[/dim]")
    console.print("[dim]输入 /help 查看可用命令。[/dim]\n")

    # ═══ 风险操作确认回调（prompt_toolkit 竖向选择器）═══
    async def _risk_confirm(label: str, tool_name: str, args: dict) -> str:
        """当工具触发 confirm 级风险时，暂停并渲染竖向选择器让用户决定。"""
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.formatted_text import HTML

        cmd_preview = str(args.get("command", args.get("target_file", "")))[:60]

        options = [
            ("execute", "▶ 执行", "green"),
            ("skip",    "✕ 跳过", "yellow"),
            ("abort",   "■ 终止", "red"),
        ]
        selected = [0]  # 当前选中索引
        result = ["skip"]  # 默认跳过

        def _get_options_text():
            lines = []
            for i, (_, text, color) in enumerate(options):
                if i == selected[0]:
                    lines.append(f"<b><{color}>  › {text}</{color}></b>")
                else:
                    lines.append(f"<style fg='gray'>    {text}</style>")
            return HTML("\n".join(lines))

        options_control = FormattedTextControl(_get_options_text)

        kb = KeyBindings()

        @kb.add("up")
        def _up(event):
            selected[0] = max(0, selected[0] - 1)

        @kb.add("down")
        def _down(event):
            selected[0] = min(len(options) - 1, selected[0] + 1)

        @kb.add("enter")
        def _enter(event):
            result[0] = options[selected[0]][0]
            event.app.exit()

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event):
            result[0] = "skip"
            event.app.exit()

        # 顶部风险信息（Rich 渲染）
        console.print()
        console.print(
            Panel(
                f"[bold yellow]⚠ 风险操作[/bold yellow]: {label}\n"
                f"[dim]{tool_name}: {cmd_preview}[/dim]",
                border_style="yellow",
                padding=(0, 1),
                width=50,
            )
        )

        # prompt_toolkit 选择器
        layout = Layout(
            HSplit([
                Window(content=options_control, height=3),
            ])
        )

        app = Application(layout=layout, key_bindings=kb, full_screen=False)
        await app.run_async()

        return result[0]

    master.registry.set_confirm_callback(_risk_confirm)

    # 创建命令路由器
    router = create_router(console)
    cmd_context = {
        "console": console,
        "master": master,
        "router": router,
        "cwd": cwd,
        "trusted": is_trusted,
        "cli_handler": cli_handler,
    }

    # prompt_toolkit 会话
    kb = create_key_bindings()
    
    @kb.add(Keys.Enter)
    def _(event):
        """覆盖 multiline 默认的 Enter 换行行为，改为提交。"""
        event.current_buffer.validate_and_handle()

    @kb.add('escape', 'enter')
    def _(event):
        """支持 Alt+Enter (或 Esc, Enter) 进行换行。"""
        event.current_buffer.insert_text('\n')

    # ── 输入框布局定制 ──
    from prompt_toolkit.styles import Style as PTStyle
    from prompt_toolkit.layout.processors import AfterInput
    from prompt_toolkit.layout.containers import HSplit, Window

    def get_prompt():
        """动态 prompt：顶部直线 + 提示符"""
        cols = os.get_terminal_size().columns
        top = "─" * (cols - 1)
        return [
            ("class:box-line", f"{top}\n"),
            ("class:prompt", " > "),
        ]

    def get_bottom_box():
        """底部边框 + 状态栏集成在 bottom_toolbar 中"""
        model = getattr(master, "model", "unknown")
        effort = getattr(master, "_reasoning_effort", None) or "off"
        thinking = "on" if getattr(master, "enable_thinking", False) else "off"
        trusted_str = "Trusted" if cmd_context.get("trusted") else "Isolated"

        cols = os.get_terminal_size().columns

        left_text = f" 模型: {model} [{trusted_str}] "

        # L2b 预算进度条（校准后用 API 精确值，未校准用默认系数）
        from core.layered_memory import L2B_COMPRESS_THRESHOLD, CHAR_PER_TOKEN
        budget_tokens = L2B_COMPRESS_THRESHOLD // CHAR_PER_TOKEN  # 80K
        used_tokens = master.memory.l2b_token_count()
        calibrated = master.memory._calibrated_cpt > 0
        used_k = used_tokens // 1000
        budget_k = budget_tokens // 1000
        pct = min(int(used_tokens / budget_tokens * 100), 100) if budget_tokens else 0
        bar_total = 10
        bar_filled = int(bar_total * pct / 100)
        bar = "█" * bar_filled + "░" * (bar_total - bar_filled)
        precision = "★" if calibrated else "≈"
        mid_text = f" 上下文预算 {bar} {precision}{used_k}/{budget_k}K({pct}%) "

        log_mode = "详细" if cli_handler.verbose else "紧凑"
        right_text = f" 日志: {log_mode}  深度思考: {thinking}  推理: {effort} "

        left_w = sum(2 if ord(c) > 0x7F else 1 for c in left_text)
        mid_w = sum(2 if ord(c) > 0x7F else 1 for c in mid_text)
        right_w = sum(2 if ord(c) > 0x7F else 1 for c in right_text)

        gap_total = max(cols - left_w - mid_w - right_w, 0)
        gap_left = gap_total // 2
        gap_right = gap_total - gap_left
        status_line = left_text + " " * gap_left + mid_text + " " * gap_right + right_text

        return [
            ("class:box-line", "─" * cols),
            ("", "\n"),
            ("class:toolbar.status", status_line),
        ]

    pt_style = PTStyle.from_dict({
        "bottom-toolbar": "default bg:default noreverse",
        "box-line": "#555555",
        "toolbar.status": "cyan",
        # CC 风格暗色补全菜单
        "completion-menu": "bg:#1e1e1e #aaaaaa",
        "completion-menu.completion": "bg:#1e1e1e #aaaaaa",
        "completion-menu.completion.current": "bg:#1e1e1e #ffffff bold noreverse",
        "completion-menu.meta.completion": "bg:#1e1e1e #666666",
        "completion-menu.meta.completion.current": "bg:#1e1e1e #aaaaaa noreverse",
    })

    # ── 悬浮菜单补全器 ──

    # 静态子参数映射：{命令名: {子命令: 描述}}
    _STATIC_SUBARGS = {
        "thinking": {"off": "关闭深度思考", "on": "开启 (high)", "max": "最大强度"},
        "verbose": {"on": "详细模式", "off": "紧凑模式"},
        "prompt": {"skills": "查看 Skill 段", "constraints": "查看硬约束段"},
        "mcp": {"enable": "启动 Server", "disable": "停止 Server", "restart": "重启 Server"},
        "skill": {"enable": "启用 Skill", "disable": "禁用 Skill", "reload": "重新扫描"},
        "config": {"init": "初始化用户级 .env", "path": "查看配置路径"},
    }

    def _get_mcp_server_names() -> list:
        """获取所有 MCP Server 名称（含已停止的）。"""
        mgr = getattr(master, "mcp_manager", None)
        if not mgr:
            return []
        # 从运行时状态获取
        names = [s["name"] for s in mgr.get_status()]
        if names:
            return names
        # 回退：从配置文件加载
        try:
            configs = mgr._load_config()
            return [c.get("name", "") for c in configs if c.get("name")]
        except Exception:
            return []

    def _get_skill_names() -> list:
        """获取所有 Skill 名称。"""
        sm = getattr(master, "skill_manager", None)
        if not sm:
            return []
        return [s["name"] for s in sm.list_info()]

    class SlashCompleter(Completer):
        """/ 命令补全：支持命令名 + 子参数 + 目标名三级补全"""
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor.lstrip()

            if not text.startswith("/"):
                return

            parts = text[1:].split()
            has_trailing_space = text.endswith(" ")

            # ── 已有完整命令名，补全子参数 ──
            if len(parts) >= 1 and (len(parts) > 1 or has_trailing_space):
                cmd_name = parts[0].lower()

                # /model <name> — 模型补全
                if cmd_name in ("model", "m"):
                    prefix = parts[1].lower() if len(parts) > 1 else ""
                    from core.llm_client import default_llm
                    for provider in default_llm.providers:
                        for model_name in provider.models:
                            if model_name.lower().startswith(prefix) or prefix in model_name.lower():
                                yield Completion(
                                    model_name,
                                    start_position=-len(prefix),
                                    display=model_name,
                                    display_meta=f"({provider.name})"
                                )
                    return

                # /mcp <action> <server_name> — 三级补全
                if cmd_name == "mcp":
                    if len(parts) == 1 and has_trailing_space:
                        # 第二段：补全 action
                        for sub, desc in _STATIC_SUBARGS["mcp"].items():
                            yield Completion(sub, start_position=0, display=sub, display_meta=desc)
                        return
                    if len(parts) == 2 and not has_trailing_space:
                        # 正在输入 action
                        prefix = parts[1].lower()
                        for sub, desc in _STATIC_SUBARGS["mcp"].items():
                            if sub.startswith(prefix):
                                yield Completion(sub, start_position=-len(prefix), display=sub, display_meta=desc)
                        return
                    if len(parts) >= 2 and parts[1].lower() in ("enable", "disable", "restart"):
                        # 第三段：补全 server 名称
                        prefix = parts[2].lower() if len(parts) > 2 else ""
                        for name in _get_mcp_server_names():
                            if name.lower().startswith(prefix) or prefix in name.lower():
                                yield Completion(name, start_position=-len(prefix), display=name, display_meta="MCP Server")
                        return
                    return

                # /skill <action> <skill_name> — 三级补全
                if cmd_name in ("skill", "sk"):
                    if len(parts) == 1 and has_trailing_space:
                        for sub, desc in _STATIC_SUBARGS["skill"].items():
                            yield Completion(sub, start_position=0, display=sub, display_meta=desc)
                        return
                    if len(parts) == 2 and not has_trailing_space:
                        prefix = parts[1].lower()
                        for sub, desc in _STATIC_SUBARGS["skill"].items():
                            if sub.startswith(prefix):
                                yield Completion(sub, start_position=-len(prefix), display=sub, display_meta=desc)
                        return
                    if len(parts) >= 2 and parts[1].lower() in ("enable", "disable"):
                        prefix = parts[2].lower() if len(parts) > 2 else ""
                        for name in _get_skill_names():
                            if name.lower().startswith(prefix) or prefix in name.lower():
                                yield Completion(name, start_position=-len(prefix), display=name, display_meta="Skill")
                        return
                    return

                # 静态二级补全：/thinking, /verbose, /prompt
                if cmd_name in _STATIC_SUBARGS and cmd_name not in ("mcp", "skill"):
                    prefix = parts[1].lower() if len(parts) > 1 else ""
                    for sub, desc in _STATIC_SUBARGS[cmd_name].items():
                        if sub.startswith(prefix):
                            yield Completion(sub, start_position=-len(prefix), display=sub, display_meta=desc)
                    return

                # 别名适配
                alias_map = {"t": "thinking", "v": "verbose", "cfg": "config"}
                if cmd_name in alias_map:
                    real = alias_map[cmd_name]
                    prefix = parts[1].lower() if len(parts) > 1 else ""
                    for sub, desc in _STATIC_SUBARGS[real].items():
                        if sub.startswith(prefix):
                            yield Completion(sub, start_position=-len(prefix), display=sub, display_meta=desc)
                    return

                return

            # ── 第一段：补全命令名 ──
            prefix = parts[0] if parts else ""
            for cmd in router.get_all_commands():
                if cmd.name.startswith(prefix):
                    yield Completion(
                        "/" + cmd.name,
                        start_position=-len(text),
                        display=f"/{cmd.name}",
                        display_meta=cmd.description,
                    )

    session = PromptSession(
        key_bindings=kb,
        style=pt_style,
        completer=SlashCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.COLUMN,
        reserve_space_for_menu=0,
        bottom_toolbar=get_bottom_box,
    )

    # 修改 prompt_toolkit 底层布局，将补全菜单强行下推一行，并增加底部占位防止反转
    from prompt_toolkit.layout.containers import FloatContainer
    def _hack_floats(container):
        if isinstance(container, FloatContainer):
            found_menu = False
            # 1. 给菜单顶部加透明占位，使其下移一行（跳过底边框）
            for f in container.floats:
                if f.content.__class__.__name__ in ('CompletionsMenu', 'MultiColumnCompletionsMenu'):
                    f.content = HSplit([
                        Window(height=1), # 透明占位
                        f.content
                    ])
                    found_menu = True
                    break
            # 2. 只有找到了主浮动层，才给底层 HSplit 底部追加 6 行空白防止反转
            if found_menu:
                if isinstance(container.content, HSplit):
                    container.content.children.append(Window(height=6, style="class:transparent"))
                return True
        if hasattr(container, 'content') and container.content is not None:
            if _hack_floats(container.content): return True
        if hasattr(container, 'children'):
            for child in container.children:
                if _hack_floats(child): return True
        return False
        
    _hack_floats(session.app.layout.container)

    # 双击 Ctrl+C 退出保护
    _last_ctrl_c = 0.0

    def _save_and_exit():
        """退出前自动保存记忆 + 打印 Token 消耗面板。"""
        # 保存记忆
        try:
            saved = master.memory.save_to_disk()
            if saved:
                console.print(f"[dim]记忆已保存: {saved}[/dim]")
        except Exception as e:
            console.print(f"[warning]记忆保存失败: {e}[/warning]")

        # Token 消耗面板
        try:
            from core.llm_client import token_tracker
            report = token_tracker.estimate_cost()

            if report["request_count"] == 0:
                console.print("[dim]本次会话未产生 Token 消耗。[/dim]")
                return

            from rich.table import Table
            table = Table(
                title="本次会话 Token 消耗",
                title_style="bold cyan",
                show_header=True,
                header_style="bold",
                padding=(0, 1),
                box=box.SIMPLE_HEAVY,
            )
            table.add_column("模型", style="cyan")
            table.add_column("请求数", justify="right")
            table.add_column("输入", justify="right")
            table.add_column("输出", justify="right")
            table.add_column("思考", justify="right", style="dim")
            table.add_column("缓存命中", justify="right", style="dim")
            table.add_column("费用(元)", justify="right", style="yellow")

            for d in report["details"]:
                reasoning_str = f'{d["reasoning"]:,}' if d["reasoning"] else "-"
                cache_str = f'{d["cache_hit"]:,}' if d["cache_hit"] else "-"
                table.add_row(
                    d["model"],
                    str(d["requests"]),
                    f'{d["prompt"]:,}',
                    f'{d["completion"]:,}',
                    reasoning_str,
                    cache_str,
                    f'¥{d["cost"]:.4f}',
                )

            # 汇总行
            total_tokens = report["total_tokens"]
            table.add_row(
                "[bold]合计[/bold]", str(report["request_count"]),
                f'[bold]{report["total_prompt"]:,}[/bold]',
                f'[bold]{report["total_completion"]:,}[/bold]',
                f'{report["total_reasoning"]:,}' if report["total_reasoning"] else "-",
                f'{report["total_cache_hit"]:,}' if report["total_cache_hit"] else "-",
                f'[bold yellow]¥{report["total_cost"]:.4f}[/bold yellow]',
            )

            console.print()
            console.print(table)
            console.print(
                f"[dim]总 Token: {total_tokens:,} | "
                f"价格基于 DeepSeek 官方费率，可通过 .env 中 PRICE_<MODEL>_INPUT/OUTPUT/CACHE 覆盖[/dim]"
            )
        except Exception as e:
            console.print(f"[dim]Token 统计异常: {e}[/dim]")

    try:
        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, session.prompt, get_prompt,
                )

                user_input = user_input.strip()
                if not user_input:
                    continue

                # 拦截 / 命令
                if user_input.startswith("/"):
                    # 如果 dispatch 返回 False，说明是 /exit
                    should_continue = await router.dispatch(user_input, cmd_context)
                    if not should_continue:
                        _save_and_exit()
                        break
                    continue

                # 显示用户输入
                console.print(Rule(style="dim"))
                console.print(f"[user_input]> {user_input}[/user_input]")
                console.print()

                # 调用 Master
                t0 = time.time()
                console.print("[dim]Master 正在处理...[/dim]")

                try:
                    response = await master.handle_user_message(user_input)
                except KeyboardInterrupt:
                    console.print("\n[warning]中断 Master 执行[/warning]")
                    continue
                except Exception as e:
                    console.print(f"[error]Master 执行异常: {e}[/error]")
                    continue

                elapsed = time.time() - t0

                # 紧凑模式下渲染工具调用摘要
                if not cli_handler.verbose:
                    tool_buffer = cli_handler.flush_tool_buffer()
                    if tool_buffer:
                        render_tool_calls_compact(console, tool_buffer)

                # 渲染 Todo 进度
                todo_data = master.memory.todo_to_ui()
                if todo_data:
                    console.print()
                    render_todo(console, todo_data)

                # 渲染 Master 回复（Markdown 渲染）
                console.print(Rule(style="dim"))
                if response:
                    rendered = Markdown(response)
                else:
                    rendered = Text("无回复", style="dim")
                console.print(
                    Panel(
                        rendered,
                        title="[master]Master[/master]",
                        border_style="cyan",
                        padding=(1, 2),
                    )
                )
                console.print(f"[dim]耗时 {elapsed:.1f}s[/dim]")

            except KeyboardInterrupt:
                now = time.time()
                if now - _last_ctrl_c < 1.5:
                    # 1.5 秒内双击 → 保存并退出
                    _save_and_exit()
                    console.print("\n[dim]再见。[/dim]")
                    break
                else:
                    _last_ctrl_c = now
                    console.print("\n[warning]再按一次 Ctrl+C 退出（1.5秒内）[/warning]")
                    continue
            except EOFError:
                _save_and_exit()
                break
    finally:
        # MCP 清理：必须在事件循环退出前 await 完成
        if master.mcp_manager:
            try:
                await master.shutdown_mcp()
            except Exception as e:
                console.print(f"[dim]MCP 关闭异常: {e}[/dim]")


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
