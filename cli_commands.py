"""
ASTrea CLI 斜杠命令路由器

所有 / 命令在本地执行，不消耗 Token，不经过 Master。
"""
import asyncio
import inspect
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

# ── 数据结构 ──


@dataclass
class SlashCommand:
    """斜杠命令定义"""
    name: str
    aliases: List[str] = field(default_factory=list)
    description: str = ""
    usage: str = ""
    handler: Callable = None


class CommandRouter:
    """
    斜杠命令路由器。

    在 cli.py 主循环中拦截所有 / 开头的输入，
    路由到对应的本地命令处理器，不经过 Master。
    """

    def __init__(self, console: Console):
        self.console = console
        self._commands: Dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand):
        """注册一个斜杠命令（含别名）"""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    async def dispatch(self, raw_input: str, context: dict) -> bool:
        """
        解析并执行斜杠命令（支持 sync/async handler）。

        Args:
            raw_input: 用户输入（含 / 前缀）
            context: 运行时上下文 {"master": Master, "cwd": str, ...}

        Returns:
            True 表示已处理，False 表示退出信号
        """
        parts = raw_input.strip().lstrip("/").split(maxsplit=1)
        if not parts:
            return True

        cmd_name = parts[0].lower()
        args = parts[1].split() if len(parts) > 1 else []

        cmd = self._commands.get(cmd_name)
        if not cmd:
            self.console.print(f"[warning]未知命令: /{cmd_name}[/warning]")
            self.console.print("[dim]输入 /help 查看可用命令。[/dim]")
            return True

        result = cmd.handler(args, context)
        # 支持 async handler（如 cmd_mcp）
        if inspect.isawaitable(result):
            return await result
        return result

    def get_all_commands(self) -> List[SlashCommand]:
        """返回去重后的所有命令（排除别名重复）"""
        seen = set()
        result = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(cmd)
        return result


# ══════════════════════════════════════════════
#  命令实现
# ══════════════════════════════════════════════


def cmd_help(args: list, ctx: dict) -> bool:
    """列出所有可用斜杠命令"""
    console: Console = ctx["console"]
    router: CommandRouter = ctx["router"]

    table = Table(
        title="可用命令",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("命令", style="cyan", min_width=20)
    table.add_column("说明", style="dim")

    for cmd in router.get_all_commands():
        name_str = f"/{cmd.name}"
        if cmd.aliases:
            name_str += f"  [dim]({'、'.join('/' + a for a in cmd.aliases)})[/dim]"
        if cmd.usage:
            name_str += f"  [dim italic]{cmd.usage}[/dim italic]"
        table.add_row(name_str, cmd.description)

    console.print(table)
    return True


def cmd_exit(args: list, ctx: dict) -> bool:
    """退出 ASTrea"""
    ctx["console"].print("[dim]再见。[/dim]")
    return False  # False 信号告知主循环退出


def cmd_model(args: list, ctx: dict) -> bool:
    """查看或切换 Master 模型"""
    console: Console = ctx["console"]
    master = ctx["master"]

    if not args:
        # 无参数：列出所有可用模型
        from core.llm_client import default_llm
        table = Table(
            title="可用模型",
            title_style="bold cyan",
            show_header=True,
            header_style="bold",
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("Provider", style="dim")
        table.add_column("Model", style="cyan")
        table.add_column("状态", justify="center")

        current = master.model.lower()
        for provider in default_llm.providers:
            for model_name in provider.models:
                is_current = model_name.lower() == current
                status = "[bold green]← 当前[/bold green]" if is_current else ""
                style = "bold" if is_current else ""
                table.add_row(provider.name, model_name, status, style=style)

        console.print(table)
        console.print("[dim]提示: 直接输入 /model <空格> 即可通过方向键选择模型。[/dim]")
        return True

    # 有参数：切换模型
    target = args[0].lower()

    # 先尝试精确匹配，再尝试模糊匹配
    from core.llm_client import default_llm
    matched = None
    matched_provider = None
    for provider in default_llm.providers:
        for model_name in provider.models:
            if model_name.lower() == target:
                matched = model_name
                matched_provider = provider.name
                break
            elif target in model_name.lower() and matched is None:
                matched = model_name
                matched_provider = provider.name
        if matched and matched.lower() == target:
            break  # 精确匹配优先

    if not matched:
        console.print(f"[error]未找到匹配模型: {target}[/error]")
        console.print("[dim]输入 /model 查看所有可用模型。[/dim]")
        return True

    old_model = master.model
    master.model = matched
    console.print(
        f"[success]模型已切换:[/success] {old_model} → [bold cyan]{matched}[/bold cyan] "
        f"[dim](Provider: {matched_provider})[/dim]"
    )
    return True


def cmd_thinking(args: list, ctx: dict) -> bool:
    """切换深度思考模式"""
    console: Console = ctx["console"]
    master = ctx["master"]

    if not args:
        # 显示当前状态
        status = "开启" if master.enable_thinking else "关闭"
        effort = master._reasoning_effort or "N/A"
        console.print(
            f"[info]深度思考: {status}[/info]"
            + (f" [dim](强度: {effort})[/dim]" if master.enable_thinking else "")
        )
        console.print("[dim]用法: /thinking off | on | max[/dim]")
        return True

    val = args[0].lower()
    if val in ("off", "false", "0"):
        master.enable_thinking = False
        master._reasoning_effort = None
        console.print("[success]深度思考已关闭[/success]")
    elif val in ("on", "true", "high", "1"):
        master.enable_thinking = True
        master._reasoning_effort = "high"
        console.print("[success]深度思考已开启[/success] [dim](强度: high)[/dim]")
    elif val == "max":
        master.enable_thinking = True
        master._reasoning_effort = "max"
        console.print("[success]深度思考已开启[/success] [dim](强度: max)[/dim]")
    else:
        console.print(f"[warning]无效参数: {val}[/warning]")
        console.print("[dim]可选值: off | on | max[/dim]")

    return True


def cmd_stats(args: list, ctx: dict) -> bool:
    """显示运行统计"""
    console: Console = ctx["console"]
    master = ctx["master"]

    from core.llm_client import token_tracker

    stats = master.get_stats()
    report = token_tracker.estimate_cost()

    table = Table(
        title="运行统计",
        title_style="bold cyan",
        show_header=False,
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("项目", style="dim", min_width=16)
    table.add_column("值", style="cyan")

    table.add_row("模型", stats.get("model", "?"))
    table.add_row("深度思考", "开启" if master.enable_thinking else "关闭")
    table.add_row("推理强度", master._reasoning_effort or "N/A")
    table.add_row("累计步数", str(stats.get("step", 0)))
    table.add_row("API 请求数", str(report["request_count"]))
    table.add_row("输入 Tokens", f'{report["total_prompt"]:,}')
    table.add_row("输出 Tokens", f'{report["total_completion"]:,}')
    if report["total_reasoning"]:
        table.add_row("思考 Tokens", f'{report["total_reasoning"]:,} [dim](含在输出内)[/dim]')
    if report["total_cache_hit"]:
        table.add_row("缓存命中", f'{report["total_cache_hit"]:,}')
    table.add_row("总 Tokens", f'[bold]{report["total_tokens"]:,}[/bold]')
    table.add_row("估算费用", f'[yellow]¥{report["total_cost"]:.4f}[/yellow]')
    table.add_row("已注册工具", str(len(stats.get("tools", []))))
    table.add_row("SQLite 步数", str(stats.get("sqlite_steps", "?")))

    # 内存统计
    mem = stats.get("memory", {})
    if isinstance(mem, dict):
        table.add_row("L2b 消息数", str(mem.get("l2b_count", "?")))

    console.print(table)
    return True


def cmd_trust(args: list, ctx: dict) -> bool:
    """管理工作区信任模式（on/off）"""
    console: Console = ctx["console"]
    master = ctx["master"]
    cwd = ctx["cwd"]

    # 无参数：显示当前状态
    if not args:
        status = "[bold green]Trusted[/bold green]" if ctx.get("trusted") else "[bold yellow]Isolated[/bold yellow]"
        console.print(f"当前模式: {status}")
        console.print("[dim]用法: /trust on | off[/dim]")
        return True

    action = args[0].lower()

    if action == "on":
        if ctx.get("trusted"):
            console.print("[dim]当前已处于 trusted 模式。[/dim]")
            return True

        master.project_dir = cwd
        ctx["trusted"] = True

        # 持久化信任记录
        astrea_dir = os.path.join(cwd, ".astrea")
        os.makedirs(astrea_dir, exist_ok=True)
        config_path = os.path.join(astrea_dir, "config.json")
        config = {}
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
        config["trusted"] = True
        config["trusted_at"] = datetime.now().isoformat()
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        console.print(
            f"[success]已解锁:[/success] Agent 现在可以读写 [cyan]{cwd}[/cyan] 下的所有文件"
        )
        return True

    elif action == "off":
        if not ctx.get("trusted"):
            console.print("[dim]当前已处于隔离模式。[/dim]")
            return True

        workspace = os.path.join(cwd, ".astrea", "workspace")
        os.makedirs(workspace, exist_ok=True)
        master.project_dir = workspace
        ctx["trusted"] = False

        config_path = os.path.join(cwd, ".astrea", "config.json")
        config = {}
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
        config["trusted"] = False
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

        console.print("[success]已切换回隔离模式[/success]")
        console.print(f"[dim]Agent 工作区: {workspace}[/dim]")
        return True

    else:
        console.print("[warning]用法: /trust on | off[/warning]")
        return True


# ══════════════════════════════════════════════
#  P1 命令扩展
# ══════════════════════════════════════════════

def cmd_tree(args: list, ctx: dict) -> bool:
    """打印项目目录树"""
    console: Console = ctx["console"]
    master = ctx["master"]
    cwd = ctx.get("cwd", ".")
    
    project_dir = getattr(master, 'project_dir', cwd)
    
    from rich.tree import Tree
    tree = Tree(f"[bold cyan]{os.path.basename(project_dir) or 'workspace'}[/bold cyan]")
    
    def build_tree(dir_path: str, current_tree: Tree, depth: int = 0):
        if depth > 4:
            current_tree.add("[dim]...[/dim]")
            return
        try:
            paths = sorted(os.listdir(dir_path))
        except PermissionError:
            return
            
        for p in paths:
            if p in ('.git', '.venv', '__pycache__', '.astrea', 'node_modules', '.idea', '.vscode'):
                continue
            full_path = os.path.join(dir_path, p)
            if os.path.isdir(full_path):
                branch = current_tree.add(f"[bold blue]{p}[/bold blue]/")
                build_tree(full_path, branch, depth + 1)
            else:
                if p.endswith('.py'):
                    current_tree.add(f"[green]{p}[/green]")
                elif p.endswith('.md'):
                    current_tree.add(f"[yellow]{p}[/yellow]")
                else:
                    current_tree.add(p)
                    
    build_tree(project_dir, tree)
    console.print(tree)
    return True


def cmd_todo(args: list, ctx: dict) -> bool:
    """显示待办事项面板"""
    console: Console = ctx["console"]
    master = ctx["master"]
    project_dir = getattr(master, 'project_dir', ctx.get("cwd", "."))
    
    plan_path = os.path.join(project_dir, ".astrea", "plan.md")
    if os.path.isfile(plan_path):
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                content = f.read()
            from rich.markdown import Markdown
            console.print(Panel(Markdown(content), title="[bold cyan]项目方案 (plan.md)[/bold cyan]", border_style="cyan"))
        except Exception as e:
            console.print(f"[error]读取方案失败: {e}[/error]")
    else:
        console.print("[dim]未找到 .astrea/plan.md，当前无进行中的项目方案。[/dim]")
        
    return True


def cmd_providers(args: list, ctx: dict) -> bool:
    """列出所有已注册的 LLM Provider"""
    console: Console = ctx["console"]
    from core.llm_client import default_llm
    
    table = Table(
        title="已注册 LLM Providers",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("Provider", style="cyan")
    table.add_column("Base URL", style="dim")
    table.add_column("Models", style="green")
    
    for provider in default_llm.providers:
        # OpenAI client internally stores base_url as httpx.URL, convert to string
        base_url = str(provider.client.base_url) if hasattr(provider.client, 'base_url') else "-"
        table.add_row(
            provider.name,
            base_url,
            str(len(provider.models))
        )
        
    console.print(table)
    return True


# ══════════════════════════════════════════════
#  配置命令
# ══════════════════════════════════════════════

_PROVIDER_PRESETS = {
    "deepseek": {
        "prefix": "DEEPSEEK",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "prefix": "QWEN",
        "name": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "openai": {
        "prefix": "OPENAI",
        "name": "OpenAI-Compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "gpt": {
        "prefix": "GPT",
        "name": "GPT",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
}


def _prompt_value(label: str, default: str = "", password: bool = False) -> str:
    """读取一项交互输入。"""
    from prompt_toolkit import prompt as pt_prompt

    suffix = f" [{default}]" if default else ""
    value = pt_prompt(f"{label}{suffix}: ", is_password=password).strip()
    return value or default


def _clean_base_url_input(url: str) -> str:
    """清洗用户误填的完整 completions endpoint。"""
    clean = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/chat"):
        if clean.lower().endswith(suffix):
            clean = clean[:len(clean) - len(suffix)]
            break
    return clean.rstrip("/")


def _write_user_env(provider_key: str, api_key: str, base_url: str, models: str) -> str:
    """写入用户级 .env。"""
    from core.config_paths import get_user_env_path

    preset = _PROVIDER_PRESETS[provider_key]
    prefix = preset["prefix"]
    first_model = models.split(",", 1)[0].strip()

    env_path = get_user_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join([
        "# ASTrea global configuration",
        "# Generated by /config init",
        f"{prefix}_API_KEY={api_key}",
        f"{prefix}_BASE_URL={base_url}",
        f"{prefix}_MODELS={models}",
        f"MODEL_MASTER={first_model}",
        "THINKING_MASTER=false",
        "",
    ])
    env_path.write_text(content, encoding="utf-8")

    if os.name != "nt":
        try:
            env_path.chmod(0o600)
        except OSError:
            pass

    return str(env_path)


def cmd_config(args: list, ctx: dict) -> bool:
    """管理 ASTrea 用户级配置。"""
    console: Console = ctx["console"]

    action = args[0].lower() if args else "path"

    if action == "path":
        from core.config_paths import find_project_env_path, get_user_env_path

        project_env = find_project_env_path()
        table = Table(
            title="ASTrea 配置路径",
            title_style="bold cyan",
            show_header=False,
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("类型", style="dim")
        table.add_column("路径", style="cyan")
        table.add_row("用户级 .env", str(get_user_env_path()))
        table.add_row("项目级 .env", str(project_env) if project_env else "-")
        console.print(table)
        return True

    if action != "init":
        console.print("[dim]用法: /config init | /config path[/dim]")
        return True

    from core.config_paths import get_user_env_path, load_astrea_env

    env_path = get_user_env_path()
    if env_path.is_file():
        try:
            from prompt_toolkit import prompt as pt_prompt
            overwrite = pt_prompt(
                f"用户级配置已存在：{env_path}\n是否覆盖？[y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            overwrite = ""
        if overwrite not in ("y", "yes"):
            console.print("[dim]已取消。[/dim]")
            return True

    console.print("[bold cyan]初始化 ASTrea 用户级 LLM 配置[/bold cyan]")
    console.print("[dim]API Key 输入时不会回显。[/dim]")

    provider_text = ", ".join(_PROVIDER_PRESETS.keys())
    try:
        provider_key = _prompt_value("模型提供商", "deepseek").lower()
        if provider_key not in _PROVIDER_PRESETS:
            console.print(
                f"[error]不支持的提供商: {provider_key}[/error]\n"
                f"[dim]可选值: {provider_text}[/dim]"
            )
            return True

        preset = _PROVIDER_PRESETS[provider_key]
        api_key = _prompt_value("API Key", password=True)
        if not api_key:
            console.print("[error]API Key 不能为空。[/error]")
            return True

        base_url = _clean_base_url_input(_prompt_value("Base URL", preset["base_url"]))
        models = _prompt_value("模型名称（多个用英文逗号分隔）", preset["model"])
        models = ",".join(m.strip() for m in models.split(",") if m.strip())
        if not models:
            console.print("[error]模型名称不能为空。[/error]")
            return True
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]已取消。[/dim]")
        return True

    written = _write_user_env(provider_key, api_key, base_url, models)
    load_astrea_env()

    from core.llm_client import default_llm
    default_llm.reload_providers()

    master = ctx.get("master")
    if master:
        old_model = getattr(master, "model", "")
        master.model = os.getenv("MODEL_MASTER", models.split(",", 1)[0].strip())
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_MASTER", "false"))
        master.enable_thinking = _et
        master._reasoning_effort = _re
        model_msg = f"{old_model} → {master.model}" if old_model != master.model else master.model
    else:
        model_msg = os.getenv("MODEL_MASTER", "-")

    console.print(f"[success]配置已写入:[/success] [cyan]{written}[/cyan]")
    console.print(f"[success]Provider 已重载:[/success] {len(default_llm.providers)} 个")
    console.print(f"[success]当前 Master 模型:[/success] {model_msg}")
    return True


# ══════════════════════════════════════════════
#  工作区安全检测
# ══════════════════════════════════════════════


def check_workspace_safety(cwd: str, console: Console) -> dict:
    """
    启动时检测工作区安全状态。

    Returns:
        {
            "trusted": bool,         # 是否信任
            "project_dir": str,      # 实际项目目录
            "is_empty": bool,        # CWD 是否为空
            "has_git": bool,         # 是否有 .git
        }
    """
    entries = [e for e in os.listdir(cwd) if not e.startswith(".astrea")]
    is_empty = len(entries) == 0
    has_git = os.path.isdir(os.path.join(cwd, ".git"))

    # 空目录：直接信任
    if is_empty:
        return {
            "trusted": True,
            "project_dir": cwd,
            "is_empty": True,
            "has_git": False,
        }

    # 检查持久化信任记录
    config_path = os.path.join(cwd, ".astrea", "config.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if config.get("trusted"):
                console.print(
                    f"[dim]检测到信任记录，自动进入 trusted 模式[/dim]"
                )
                return {
                    "trusted": True,
                    "project_dir": cwd,
                    "is_empty": False,
                    "has_git": has_git,
                }
        except Exception:
            pass

    # 非空 + 无信任记录：隔离模式
    file_count = sum(1 for e in entries if os.path.isfile(os.path.join(cwd, e)))
    dir_count = sum(1 for e in entries if os.path.isdir(os.path.join(cwd, e)))

    git_hint = "[green]有 .git 版本控制[/green]" if has_git else "[yellow]无版本控制[/yellow]"

    console.print(
        Panel(
            f"[bold]该目录非空[/bold]\n\n"
            f"  路径: [cyan]{cwd}[/cyan]\n"
            f"  内容: {file_count} 个文件, {dir_count} 个子目录\n"
            f"  版本控制: {git_hint}\n\n"
            f"[dim]Agent 将在 .astrea/workspace/ 隔离目录中工作。\n"
            f"如需操作原有文件，请使用 [bold]/trust[/bold] 命令解锁。[/dim]",
            title="[yellow]工作区检测[/yellow]",
            border_style="yellow",
        )
    )

    workspace = os.path.join(cwd, ".astrea", "workspace")
    os.makedirs(workspace, exist_ok=True)

    return {
        "trusted": False,
        "project_dir": workspace,
        "is_empty": False,
        "has_git": has_git,
    }


# ══════════════════════════════════════════════
#  会话管理命令
# ══════════════════════════════════════════════


def _archive_if_unique(master, sessions_dir: str) -> tuple:
    """归档当前会话，若当前会话源自已有快照则跳过。
    返回 (saved: bool, msg_count: int, filename: str)
    """
    if not master.memory._conversation:
        return False, 0, ""

    # 核心去重：如果当前会话是从 sessions_dir 中恢复的，跳过归档
    source = getattr(master.memory, "_source_snapshot", "")
    if source and os.path.isfile(source):
        source_norm = os.path.normcase(os.path.abspath(source))
        sessions_norm = os.path.normcase(os.path.abspath(sessions_dir))
        if source_norm.startswith(sessions_norm):
            return False, 0, ""  # 来自已有快照，跳过

    # 备选：通过 content_hash 去重（兼容 session_id 机制）
    current_hash = master.memory.content_hash()
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("content_hash") == current_hash:
                    return False, 0, ""
            except Exception:
                continue

    os.makedirs(sessions_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = os.path.join(sessions_dir, f"memory_{timestamp}.json")
    saved = master.memory.save_to_disk(archive_path)
    msg_count = len(master.memory._conversation)
    return bool(saved), msg_count, os.path.basename(archive_path)


def cmd_new(args: list, ctx: dict) -> bool:
    """归档当前会话并开始全新会话"""
    console: Console = ctx["console"]
    master = ctx["master"]
    project_dir = getattr(master, 'project_dir', ctx.get("cwd", "."))

    # 归档当前会话到 sessions 目录（去重）
    sessions_dir = os.path.join(project_dir, ".astrea", "sessions")
    saved, msg_count, fname = _archive_if_unique(master, sessions_dir)
    if saved:
        console.print(
            f"[success]当前会话已归档[/success] "
            f"[dim]({msg_count} 条消息 → {fname})[/dim]"
        )
    elif master.memory._conversation:
        console.print("[dim]当前会话与已有快照相同，跳过归档[/dim]")

    # 重置记忆
    from core.layered_memory import LayeredMemory
    old_prompt = master.memory._system_prompt
    master.memory = LayeredMemory(
        system_prompt=old_prompt,
        project_dir=project_dir,
    )
    master._step = 0

    # 清除主快照（避免下次启动时恢复旧会话）
    main_snapshot = os.path.join(project_dir, ".astrea", "memory_snapshot.json")
    if os.path.isfile(main_snapshot):
        try:
            os.remove(main_snapshot)
        except Exception:
            pass

    console.print("[success]全新会话已开始[/success]")
    return True


def cmd_sessions(args: list, ctx: dict) -> bool:
    """列出并恢复历史会话"""
    console: Console = ctx["console"]
    master = ctx["master"]
    project_dir = getattr(master, 'project_dir', ctx.get("cwd", "."))

    sessions_dir = os.path.join(project_dir, ".astrea", "sessions")
    if not os.path.isdir(sessions_dir):
        console.print("[dim]无历史会话快照。[/dim]")
        return True

    # 扫描快照文件
    snapshots = []
    for fname in sorted(os.listdir(sessions_dir), reverse=True):
        if fname.startswith("memory_") and fname.endswith(".json"):
            fpath = os.path.join(sessions_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                snapshots.append({
                    "path": fpath,
                    "filename": fname,
                    "saved_at": data.get("saved_at", "?"),
                    "msg_count": len(data.get("l2b_conversation", [])),
                    "size_kb": os.path.getsize(fpath) // 1024,
                })
            except Exception:
                continue

    if not snapshots:
        console.print("[dim]无历史会话快照。[/dim]")
        return True

    # 无参数：列出所有快照
    if not args:
        table = Table(
            title="历史会话",
            title_style="bold cyan",
            show_header=True,
            header_style="bold",
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("#", style="cyan", justify="right", width=4)
        table.add_column("时间", style="dim")
        table.add_column("消息数", justify="right")
        table.add_column("大小", justify="right", style="dim")

        for i, snap in enumerate(snapshots, 1):
            table.add_row(
                str(i),
                snap["saved_at"],
                str(snap["msg_count"]),
                f"{snap['size_kb']}KB",
            )

        console.print(table)
        console.print("[dim]用法: /sessions 1 恢复第 1 个会话[/dim]")
        return True

    # 有参数：恢复指定快照
    raw = args[0].strip("#<>")
    try:
        idx = int(raw) - 1
    except ValueError:
        console.print(f"[error]请输入有效序号，如: /sessions 1[/error]")
        return True

    if idx < 0 or idx >= len(snapshots):
        console.print(f"[error]序号超出范围 (1-{len(snapshots)})[/error]")
        return True

    target = snapshots[idx]

    # 先归档当前会话（去重）
    if master.memory._conversation:
        saved, _, _ = _archive_if_unique(master, sessions_dir)
        if saved:
            console.print("[dim]当前会话已自动归档[/dim]")

    # 恢复目标快照
    if master.memory.load_from_disk(target["path"]):
        console.print(
            f"[success]已恢复[/success] "
            f"会话 {target['saved_at']} ({target['msg_count']} 条消息)"
        )
    else:
        console.print("[error]恢复失败[/error]")

    return True


def cmd_verbose(args: list, ctx: dict) -> bool:
    """切换日志详细/紧凑模式"""
    console: Console = ctx["console"]
    handler = ctx.get("cli_handler")
    if not handler:
        console.print("[error]无法访问日志处理器[/error]")
        return True

    if args:
        val = args[0].lower()
        if val in ("on", "true", "1"):
            handler.verbose = True
        elif val in ("off", "false", "0"):
            handler.verbose = False
        else:
            console.print(f"[warning]无效参数: {val}[/warning]")
            console.print("[dim]可选值: on | off（无参数切换）[/dim]")
            return True
    else:
        # 无参数：切换
        handler.verbose = not handler.verbose

    mode = "详细模式（逐行输出）" if handler.verbose else "紧凑模式（工具调用摘要）"
    console.print(f"[success]日志已切换:[/success] {mode}")
    return True


# ══════════════════════════════════════════════
#  Phase 9 命令：Skill / Prompt / Compress
# ══════════════════════════════════════════════


def cmd_skill(args: list, ctx: dict) -> bool:
    """管理 Skill 系统（list / enable / disable / reload）"""
    console: Console = ctx["console"]
    master = ctx["master"]
    sm = getattr(master, "skill_manager", None)

    if not sm:
        console.print("[warning]Skill 系统未初始化[/warning]")
        return True

    if not args:
        # 无参数：列出所有 Skill
        skills = sm.list_info()
        if not skills:
            console.print(f"[dim]未发现任何 Skill（目录: {sm.skills_dir}）[/dim]")
            console.print("[dim]创建 skills/<name>/SKILL.md + metadata.json 即可注册[/dim]")
            return True

        from core.skill_manager import MAX_SKILL_CHARS
        table = Table(
            title="已注册 Skills",
            title_style="bold cyan",
            show_header=True,
            header_style="bold",
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("名称", style="cyan", min_width=18)
        table.add_column("状态", justify="center", width=6)
        table.add_column("字符数", justify="right", style="dim")
        table.add_column("标签", style="dim")
        table.add_column("说明", style="dim")

        for sk in skills:
            status = "[bold green]✓[/bold green]" if sk["enabled"] else "[dim]✗[/dim]"
            tags = ", ".join(sk["tags"][:5]) if sk["tags"] else "-"
            table.add_row(
                sk["name"], status, str(sk["chars"]),
                tags, sk["description"][:40],
            )

        console.print(table)

        total = sm.total_chars()
        console.print(
            f"[dim]已用预算: {total:,}/{MAX_SKILL_CHARS:,} 字符 "
            f"(~{total // 3:,} Token)[/dim]"
        )

        # 冲突警告
        warnings = sm.check_conflicts()
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")

        console.print(
            "[dim]用法: /skill enable|disable <name> | /skill reload[/dim]"
        )
        return True

    action = args[0].lower()

    if action == "reload":
        sm.discover()
        console.print(
            f"[success]Skill 重新扫描完成[/success] "
            f"[dim]({len(sm.list_info())} 个 Skill)[/dim]"
        )
        # 重新加载后同步 memory
        master.memory.set_skill_segments(sm.build_segments())
        warnings = sm.check_conflicts()
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")
        return True

    if action in ("enable", "disable") and len(args) >= 2:
        name = args[1]

        # 会话中途切换：检查是否有进行中的对话
        has_conversation = bool(master.memory._conversation)
        if has_conversation:
            console.print(
                Panel(
                    "[bold yellow]⚠️ 切换 Skill 将导致 KV Cache 重建。[/bold yellow]\n\n"
                    "  建议: 先执行 [bold]/compress[/bold] 压缩当前上下文到归档层，再切换。\n\n"
                    "  [cyan][1][/cyan] 先 /compress 再切换（推荐）\n"
                    "  [cyan][2][/cyan] 直接切换（Cache 重建，上下文保留但性能降低）\n"
                    "  [cyan][3][/cyan] 取消\n",
                    title="[yellow]Skill 切换[/yellow]",
                    border_style="yellow",
                )
            )
            try:
                from prompt_toolkit import prompt as pt_prompt
                choice = pt_prompt("选择 [1/2/3]: ").strip()
            except (ImportError, EOFError, KeyboardInterrupt):
                choice = "3"

            if choice == "1":
                # 先压缩
                _do_compress(console, master)
                # 继续执行 enable/disable
            elif choice == "3":
                console.print("[dim]已取消[/dim]")
                return True
            # choice == "2" 直接继续

        if action == "enable":
            msg = sm.enable(name)
        else:
            msg = sm.disable(name)

        console.print(f"[info]{msg}[/info]")

        # 同步到 memory
        master.memory.set_skill_segments(sm.build_segments())

        # 冲突检测
        warnings = sm.check_conflicts()
        for w in warnings:
            console.print(f"[yellow]⚠ {w}[/yellow]")

        return True

    console.print("[dim]用法: /skill [enable|disable <name>] | reload[/dim]")
    return True


def _do_compress(console: Console, master) -> None:
    """执行压缩并输出统计。"""
    before_count = len(master.memory._conversation)
    archive = master.memory.compress_rebuild()

    after_count = len(master.memory._conversation)
    archive_len = len(archive) if archive else 0
    archive_count = len(master.memory._archives)

    console.print(
        f"[success]压缩完成[/success] "
        f"[dim]消息: {before_count} → {after_count}, "
        f"归档 +{archive_len} 字符 (共 {archive_count} 条)[/dim]"
    )


def cmd_compress(args: list, ctx: dict) -> bool:
    """手动触发 L2b 压缩归档"""
    console: Console = ctx["console"]
    master = ctx["master"]

    if not master.memory._conversation:
        console.print("[dim]当前对话为空，无需压缩。[/dim]")
        return True

    _do_compress(console, master)
    return True


def cmd_context(args: list, ctx: dict) -> bool:
    """查看当前上下文空间使用情况"""
    console: Console = ctx["console"]
    master = ctx["master"]
    memory = master.memory

    from core.layered_memory import CHAR_PER_TOKEN

    cpt = memory._calibrated_cpt if memory._calibrated_cpt > 0 else CHAR_PER_TOKEN
    calibrated = memory._calibrated_cpt > 0

    def chars_to_tokens(chars: int) -> int:
        return int(chars / cpt) if cpt > 0 else 0

    # ── 各层字符数 ──
    sys_prompt_chars = len(memory._system_prompt) if memory._system_prompt else 0
    user_rules_chars = len(memory._user_rules) + 10 if memory._user_rules else 0
    skills_chars = sum(len(s) for s in memory._skill_segments) + 20 if memory._skill_segments else 0
    hard_constraints_chars = len(memory._hard_constraints) + 5 if memory._hard_constraints else 0

    # 工具 schema（估算：每个工具 ~750 字符的 JSON schema）
    tool_count = len(master.registry._tools)
    tools_chars = tool_count * 750

    # L4 归档
    l4_chars = sum(len(arc) + 15 for arc in memory._archives)

    # L2b 对话链
    l2b_chars = memory.l2b_char_count()

    # 转为 token
    sys_prompt_tokens = chars_to_tokens(sys_prompt_chars + user_rules_chars + hard_constraints_chars)
    skills_tokens = chars_to_tokens(skills_chars)
    tools_tokens = chars_to_tokens(tools_chars)
    l4_tokens = chars_to_tokens(l4_chars)
    l2b_tokens = chars_to_tokens(l2b_chars)

    total_used = sys_prompt_tokens + skills_tokens + tools_tokens + l4_tokens + l2b_tokens
    context_limit = 200_000
    free_tokens = max(context_limit - total_used, 0)

    # ── 网格可视化 ──
    grid_total = 200  # 5x20 网格
    grid_cols = 20

    categories = []
    if sys_prompt_tokens > 0:
        categories.append(("cyan", sys_prompt_tokens, "System prompt"))
    if tools_tokens > 0:
        categories.append(("yellow", tools_tokens, "工具 Schema"))
    if skills_tokens > 0:
        categories.append(("magenta", skills_tokens, "Skills"))
    if l4_tokens > 0:
        categories.append(("blue", l4_tokens, "L4 归档"))
    if l2b_tokens > 0:
        categories.append(("white", l2b_tokens, "对话消息"))

    # 构建网格 cell 列表
    cells = []
    for color, tokens, _ in categories:
        count = max(1, round(tokens / context_limit * grid_total)) if tokens > 0 else 0
        cells.extend([("■", color)] * count)

    free_cells = grid_total - len(cells)
    if free_cells > 0:
        cells.extend([("⛶", "dim")] * free_cells)
    cells = cells[:grid_total]

    # 右侧标注
    model = getattr(master, "model", "unknown")
    precision = "★" if calibrated else "≈"

    right_labels = [
        f"[bold]{model}[/bold] [dim](200K)[/dim]",
        f"{precision} {total_used / 1000:.1f}K / 200K tokens ({total_used * 100 // context_limit}%)",
        "",
        "[bold]上下文构成[/bold]",
    ]
    for color, tokens, label in categories:
        pct = tokens * 100 / context_limit
        right_labels.append(
            f"[{color}]■[/{color}] {label}: {tokens / 1000:.1f}K tokens ({pct:.1f}%)"
        )
    right_labels.append(
        f"[dim]□ 可用空间: {free_tokens / 1000:.1f}K tokens ({free_tokens * 100 // context_limit}%)[/dim]"
    )

    # 构建网格文本（每行用简单方块字符）
    grid_text_lines = []
    for row_idx in range(grid_total // grid_cols):
        row_start = row_idx * grid_cols
        row_cells = cells[row_start:row_start + grid_cols]
        row_text = " ".join(f"[{c}]{s}[/{c}]" for s, c in row_cells)
        grid_text_lines.append(row_text)

    # 用 Table 双列对齐
    layout = Table(
        show_header=False,
        box=None,
        padding=(0, 2),
        expand=False,
    )
    layout.add_column(no_wrap=True)  # 网格
    layout.add_column(no_wrap=True)  # 标注

    total_rows = max(len(grid_text_lines), len(right_labels))
    for i in range(total_rows):
        left = grid_text_lines[i] if i < len(grid_text_lines) else ""
        right = right_labels[i] if i < len(right_labels) else ""
        layout.add_row(left, right)

    console.print(Panel(layout, title="[cyan]Context Space[/cyan]", border_style="cyan"))
    return True


# ══════════════════════════════════════════════
#  Phase 9b-2: MCP 命令
# ══════════════════════════════════════════════


async def cmd_mcp(args: list, ctx: dict) -> bool:
    """查看 MCP Server 状态或管理（enable/disable/restart）"""
    console: Console = ctx["console"]
    master = ctx["master"]
    mgr = getattr(master, "mcp_manager", None)

    if not mgr:
        console.print("[dim]MCP 未初始化（无 config/mcp_servers.json 或启动时跳过）[/dim]")
        return True

    # /mcp enable <name>
    if args and args[0].lower() == "enable" and len(args) >= 2:
        server_name = args[1]
        console.print(f"[dim]正在启动 MCP [{server_name}]...[/dim]")
        try:
            result_msg = await mgr.start_single_server(server_name)
            console.print(f"[info]{result_msg}[/info]")
            # 同步工具到 Registry
            await mgr.bridge_to_registry(master.registry)
            master._refresh_tool_inventory()
        except Exception as e:
            console.print(f"[error]启动失败: {e}[/error]")
        return True

    # /mcp disable <name>
    if args and args[0].lower() == "disable" and len(args) >= 2:
        server_name = args[1]
        console.print(f"[dim]正在停止 MCP [{server_name}]...[/dim]")
        try:
            result_msg = await mgr.stop_server(server_name)
            console.print(f"[info]{result_msg}[/info]")
            # 从 Registry 移除该 server 的工具
            prefix = f"mcp_{server_name}_"
            removed = master.registry.remove_by_prefix(prefix)
            if removed:
                console.print(f"[dim]已从 ToolRegistry 移除 {len(removed)} 个工具[/dim]")
            master._refresh_tool_inventory()
        except Exception as e:
            console.print(f"[error]停止失败: {e}[/error]")
        return True

    # /mcp restart <name>
    if args and args[0].lower() == "restart" and len(args) >= 2:
        server_name = args[1]
        console.print(f"[dim]正在重启 MCP [{server_name}]...[/dim]")
        try:
            result_msg = await mgr.restart_server(server_name)
            console.print(f"[info]{result_msg}[/info]")
            # 重新桥接
            await mgr.bridge_to_registry(master.registry)
            master._refresh_tool_inventory()
        except Exception as e:
            console.print(f"[error]重启失败: {e}[/error]")
        return True

    status_list = mgr.get_status()

    # /mcp — 列出所有 Server 状态
    if not status_list:
        console.print("[dim]无已注册的 MCP Server。[/dim]")
        return True

    table = Table(
        title="MCP Servers",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("名称", style="cyan", min_width=12)
    table.add_column("Transport", style="dim")
    table.add_column("状态", justify="center")
    table.add_column("工具数", justify="right")
    table.add_column("工具列表", style="dim")

    for s in status_list:
        if s["status"] == "running":
            status_str = "[bold green]● running[/bold green]"
        elif s["status"] == "error":
            status_str = f"[bold red]✗ error[/bold red]"
        else:
            status_str = "[dim]○ stopped[/dim]"

        tools_str = ", ".join(s["tools"][:5])
        if len(s["tools"]) > 5:
            tools_str += f" (+{len(s['tools']) - 5})"

        table.add_row(
            s["name"],
            s["transport"],
            status_str,
            str(s["tool_count"]),
            tools_str or "-",
        )

    console.print(table)

    # 错误详情
    for s in status_list:
        if s.get("error"):
            console.print(
                f"[yellow]  ⚠ [{s['name']}] {s['error']}[/yellow]"
            )

    console.print("[dim]用法: /mcp enable|disable|restart <name>[/dim]")
    return True


# ══════════════════════════════════════════════
#  路由器工厂
# ══════════════════════════════════════════════


def create_router(console: Console) -> CommandRouter:
    """创建并注册所有 P0 命令的路由器"""
    router = CommandRouter(console)

    router.register(SlashCommand(
        name="help", aliases=["h"],
        description="列出所有可用命令",
        handler=cmd_help,
    ))
    router.register(SlashCommand(
        name="exit", aliases=["quit", "q"],
        description="退出 ASTrea",
        handler=cmd_exit,
    ))
    router.register(SlashCommand(
        name="model", aliases=["m"],
        description="查看或切换 Master 模型",
        usage="[model_name]",
        handler=cmd_model,
    ))
    router.register(SlashCommand(
        name="thinking", aliases=["t"],
        description="切换深度思考模式",
        usage="[off|on|max]",
        handler=cmd_thinking,
    ))
    router.register(SlashCommand(
        name="stats", aliases=["s"],
        description="查看运行统计（Token/步数/内存）",
        handler=cmd_stats,
    ))
    router.register(SlashCommand(
        name="trust", aliases=[],
        description="管理工作区信任模式",
        usage="[on|off]",
        handler=cmd_trust,
    ))
    router.register(SlashCommand(
        name="tree", aliases=[],
        description="打印项目目录树",
        handler=cmd_tree,
    ))
    router.register(SlashCommand(
        name="todo", aliases=[],
        description="显示待办方案面板",
        handler=cmd_todo,
    ))
    router.register(SlashCommand(
        name="providers", aliases=["p"],
        description="列出所有已注册的 LLM Provider",
        handler=cmd_providers,
    ))
    router.register(SlashCommand(
        name="config", aliases=["cfg"],
        description="初始化或查看用户级配置",
        usage="[init|path]",
        handler=cmd_config,
    ))
    router.register(SlashCommand(
        name="new", aliases=[],
        description="归档当前会话并开始全新会话",
        handler=cmd_new,
    ))
    router.register(SlashCommand(
        name="sessions", aliases=["ss"],
        description="查看并恢复历史会话",
        usage="[序号]",
        handler=cmd_sessions,
    ))
    router.register(SlashCommand(
        name="verbose", aliases=["v"],
        description="切换日志详细/紧凑模式",
        usage="[on|off]",
        handler=cmd_verbose,
    ))
    # Phase 9: Skill 系统
    router.register(SlashCommand(
        name="skill", aliases=["sk"],
        description="管理 Skill 系统（list/enable/disable/reload）",
        usage="[enable|disable <name>|reload]",
        handler=cmd_skill,
    ))
    router.register(SlashCommand(
        name="context", aliases=["ctx"],
        description="查看上下文空间使用情况",
        handler=cmd_context,
    ))
    router.register(SlashCommand(
        name="compress", aliases=["gc"],
        description="手动压缩 L2b 到 L4 归档",
        handler=cmd_compress,
    ))
    # Phase 9b-2: MCP 系统
    router.register(SlashCommand(
        name="mcp", aliases=[],
        description="查看 MCP Server 状态 / 重启",
        usage="[restart <name>]",
        handler=cmd_mcp,
    ))
    # 自进化系统
    router.register(SlashCommand(
        name="evo", aliases=[],
        description="自进化开关与状态",
        usage="[on|off|status|config]",
        handler=cmd_evo,
    ))
    router.register(SlashCommand(
        name="synth", aliases=[],
        description="查看合成工具",
        usage="[list]",
        handler=cmd_synth,
    ))
    router.register(SlashCommand(
        name="constraint", aliases=["const"],
        description="查看/管理约束规则",
        usage="[list|remove <id>]",
        handler=cmd_constraint,
    ))
    router.register(SlashCommand(
        name="tools", aliases=["tl"],
        description="列出所有已注册的工具",
        handler=cmd_tools,
    ))

    return router


# ══════════════════════════════════════════════
#  工具列表命令
# ══════════════════════════════════════════════

def cmd_tools(args: list, ctx: dict) -> bool:
    """列出所有已注册的工具"""
    console: Console = ctx["console"]
    master = ctx["master"]

    tools = master.registry._tools
    if not tools:
        console.print("[dim]当前无已注册工具。[/dim]")
        return True

    table = Table(
        title=f"已注册工具 ({len(tools)} 个)",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("名称", style="cyan")
    table.add_column("说明")

    for i, tool in enumerate(tools.values(), 1):
        desc = tool.description.split("\n")[0][:60] if tool.description else ""
        table.add_row(str(i), tool.name, desc)

    console.print(table)
    return True


# ══════════════════════════════════════════════
#  自进化系统命令
# ══════════════════════════════════════════════


def cmd_evo(args: list, ctx: dict) -> bool:
    """自进化开关与状态"""
    console: Console = ctx["console"]
    master = ctx["master"]

    config = master._load_evo_config()

    if not args or args[0].lower() == "status":
        enabled = config.get("evo_enabled", True)
        round_counter = config.get("round_counter", 0)
        last_mine_round = config.get("last_mine_round", 0)

        table = Table(
            title="自进化状态",
            title_style="bold cyan",
            show_header=False,
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("项目", style="dim", min_width=20)
        table.add_column("值", style="cyan")

        table.add_row("全局开关", "[green]开启[/green]" if enabled else "[red]关闭[/red]")
        table.add_row("累计轮次", str(round_counter))
        table.add_row("上次挖掘轮次", str(last_mine_round))
        table.add_row("挖掘冷却", f"{config.get('mine_cooldown_rounds', 10)} 轮")
        table.add_row("最小新轨迹", f"{config.get('min_new_trajectories', 5)} 条")

        # 统计
        traj_count = master._trajectory_logger.count_since(None)
        new_count = master._trajectory_logger.count_since(config.get("last_mine_traj_id"))
        table.add_row("总轨迹数", str(traj_count))
        table.add_row("新轨迹数", str(new_count))

        # 约束数
        if master._constraint_store:
            active = master._constraint_store.get_active()
            table.add_row("活跃约束", str(len(active)))

        console.print(table)
        return True

    action = args[0].lower()
    if action == "on":
        config["evo_enabled"] = True
        master._save_evo_config(config)
        console.print("[success]自进化已开启[/success]")
    elif action == "off":
        config["evo_enabled"] = False
        master._save_evo_config(config)
        console.print("[success]自进化已关闭[/success]")
    elif action == "config":
        console.print(Panel(
            json.dumps(config, ensure_ascii=False, indent=2),
            title="[cyan]自进化配置[/cyan]",
            border_style="cyan",
        ))
    else:
        console.print("[dim]用法: /evo [on|off|status|config][/dim]")

    return True


def cmd_synth(args: list, ctx: dict) -> bool:
    """查看合成工具"""
    console: Console = ctx["console"]
    master = ctx["master"]

    try:
        from core.tool_synthesizer import ToolSynthesizer
        synth = ToolSynthesizer(master.project_dir)
        tools = synth.list_tools()
    except Exception as e:
        console.print(f"[error]加载合成工具信息失败: {e}[/error]")
        return True

    if not tools:
        console.print("[dim]暂无合成工具。[/dim]")
        return True

    table = Table(
        title="合成工具",
        title_style="bold cyan",
        show_header=True,
        header_style="bold",
        padding=(0, 2),
        box=box.SIMPLE,
    )
    table.add_column("名称", style="cyan")
    table.add_column("说明", style="dim")
    table.add_column("调用次数", justify="right")
    table.add_column("成功次数", justify="right")
    table.add_column("状态", justify="center")

    for t in tools:
        status = "[red]禁用[/red]" if t.get("disabled") else "[green]活跃[/green]"
        table.add_row(
            t["name"],
            t.get("description", "")[:40],
            str(t.get("call_count", 0)),
            str(t.get("success_count", 0)),
            status,
        )

    console.print(table)
    return True


def cmd_constraint(args: list, ctx: dict) -> bool:
    """查看/管理约束规则"""
    console: Console = ctx["console"]
    master = ctx["master"]

    if not master._constraint_store:
        console.print("[dim]约束存储未初始化[/dim]")
        return True

    if not args or args[0].lower() == "list":
        all_constraints = master._constraint_store.list_all()
        if not all_constraints:
            console.print("[dim]暂无约束规则。[/dim]")
            return True

        table = Table(
            title="约束规则",
            title_style="bold cyan",
            show_header=True,
            header_style="bold",
            padding=(0, 2),
            box=box.SIMPLE,
        )
        table.add_column("ID", style="cyan", width=6)
        table.add_column("级别", justify="center", width=5)
        table.add_column("建议", style="dim")
        table.add_column("标签", style="dim")
        table.add_column("触发次数", justify="right")
        table.add_column("状态", justify="center")

        for c in all_constraints:
            level = "[red]hard[/red]" if c.get("level") == "hard" else "[cyan]hint[/cyan]"
            status = "[red]禁用[/red]" if c.get("disabled") else "[green]活跃[/green]"
            tags = ", ".join(c.get("trigger_tags", [])[:3])
            table.add_row(
                c.get("id", "?"),
                level,
                c.get("advice", "")[:40],
                tags,
                str(c.get("triggered_count", 0)),
                status,
            )

        console.print(table)
        return True

    action = args[0].lower()
    if action == "remove" and len(args) >= 2:
        cid = args[1]
        if master._constraint_store.remove(cid):
            master._constraint_store.save()
            console.print(f"[success]约束 {cid} 已删除[/success]")
        else:
            console.print(f"[error]未找到约束 {cid}[/error]")
    else:
        console.print("[dim]用法: /constraint [list|remove <id>][/dim]")

    return True

