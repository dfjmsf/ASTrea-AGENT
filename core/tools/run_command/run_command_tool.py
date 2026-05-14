"""run_command 工具 — 在用户环境中执行终端命令

安全拦截已迁移至 core/tools/_risk_guard/，由 ToolRegistry.call() 中央拦截。
此 handler 只负责命令执行。
"""
import subprocess
from typing import Any, Dict, Optional

from core.tools import ToolDefinition


def run_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = 60,
    _project_dir: str = "",
) -> Dict[str, Any]:
    """在用户环境中执行终端命令。

    参数:
        command: 要执行的命令
        cwd: 工作目录（可选，默认项目根目录）
        timeout: 超时秒数（默认 60）
        _project_dir: Master 自动注入的项目目录
    返回:
        {"status": "ok"/"fail", ...}
    """
    # 自动设置工作目录：优先 LLM 传入的 cwd，其次 Master 注入的 _project_dir
    effective_cwd = cwd or _project_dir or None

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=effective_cwd,
            encoding="utf-8",
            errors="replace",
        )

        status = "ok" if result.returncode == 0 else "fail"
        return {
            "status": status,
            "returncode": result.returncode,
            "stdout": result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
            "stderr": result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
        }

    except subprocess.TimeoutExpired:
        return {"status": "fail", "feedback": f"命令超时（{timeout}秒）: {command}"}
    except Exception as e:
        return {"status": "fail", "feedback": f"命令执行失败: {e}"}


TOOL_DEF = ToolDefinition(
    name="run_command",
    description=(
        "【绝对禁止执行单独的 cd 命令】！由于子进程隔离，单独的 cd 不会产生任何工作目录持久化效果。"
        "查看目录结构请强制使用 ls 工具。如需在子目录执行命令，请使用 `cwd` 参数或 `cd subdir && command` 组合。\n"
        "在用户环境中执行终端命令。用于安装依赖、启动服务、验证脚本、git 操作等。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的终端命令",
            },
            "cwd": {
                "type": "string",
                "description": "工作目录（可选，默认项目根目录）",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数（默认 60）",
            },
        },
        "required": ["command"],
    },
    handler=run_command,
)
