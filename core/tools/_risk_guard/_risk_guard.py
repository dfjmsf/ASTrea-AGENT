"""
风险操作拦截器 — 声明式规则表

所有工具的风险拦截规则集中声明在此文件中。
ToolRegistry.call() 在执行 handler 前调用 check()，
根据返回结果决定：直接执行 / 拦截拒绝 / 暂停等待用户确认。

新增拦截规则只需在 RISK_RULES 列表中追加一条 dict。
"""
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class RiskResult:
    """风险检查结果"""
    action: str     # "block" | "confirm"
    label: str      # 用户可读的风险描述
    rule_id: str    # 规则标识（调试用）


# ═══════════════════════════════════════════════════════════════
# 声明式风险规则表
# ═══════════════════════════════════════════════════════════════
#
# 字段说明：
#   tool     — 目标工具名（精确匹配）
#   field    — 检查 args 中的哪个字段（正则匹配）
#   pattern  — 正则模式（field 模式下使用）
#   check    — 自定义检查函数名（与 field/pattern 互斥）
#   action   — "block"（直接拒绝）| "confirm"（暂停等用户确认）
#   label    — 用户可读的风险描述
#   id       — 规则唯一标识

RISK_RULES: List[Dict[str, str]] = [
    # ─── run_command: 绝对拒绝 ───
    {"tool": "run_command", "field": "command", "pattern": r"rm\s+-rf\s+/(?!\S)",
     "action": "block", "label": "递归删根目录", "id": "RC_BLOCK_RMRF_ROOT"},
    {"tool": "run_command", "field": "command", "pattern": r"git\s+reset\s+--hard",
     "action": "block", "label": "Git 硬重置", "id": "RC_BLOCK_GIT_RESET"},
    {"tool": "run_command", "field": "command", "pattern": r"git\s+push",
     "action": "block", "label": "Git 推送远程", "id": "RC_BLOCK_GIT_PUSH"},
    {"tool": "run_command", "field": "command", "pattern": r"sudo\s+",
     "action": "block", "label": "系统级操作", "id": "RC_BLOCK_SUDO"},
    {"tool": "run_command", "field": "command", "pattern": r"chmod\s+777",
     "action": "block", "label": "危险权限设置", "id": "RC_BLOCK_CHMOD777"},
    {"tool": "run_command", "field": "command", "pattern": r">\s*/etc/",
     "action": "block", "label": "写入系统配置", "id": "RC_BLOCK_WRITE_ETC"},
    {"tool": "run_command", "field": "command", "pattern": r"shutdown\b",
     "action": "block", "label": "系统关机", "id": "RC_BLOCK_SHUTDOWN"},
    {"tool": "run_command", "field": "command", "pattern": r"Restart-Computer\b",
     "action": "block", "label": "PowerShell 重启", "id": "RC_BLOCK_RESTART"},
    {"tool": "run_command", "field": "command", "pattern": r"format\s+[a-zA-Z]:",
     "action": "block", "label": "格式化磁盘", "id": "RC_BLOCK_FORMAT"},

    # ─── run_command: 需确认 ───
    {"tool": "run_command", "field": "command", "pattern": r"\brm\s+",
     "action": "confirm", "label": "删除文件 (rm)", "id": "RC_CONFIRM_RM"},
    {"tool": "run_command", "field": "command", "pattern": r"\bdel\s+",
     "action": "confirm", "label": "删除文件 (del)", "id": "RC_CONFIRM_DEL"},
    {"tool": "run_command", "field": "command", "pattern": r"Remove-Item\b",
     "action": "confirm", "label": "删除文件 (Remove-Item)", "id": "RC_CONFIRM_REMOVEITEM"},
    {"tool": "run_command", "field": "command", "pattern": r"\b(?:rd|rmdir)\s+",
     "action": "confirm", "label": "删除目录", "id": "RC_CONFIRM_RMDIR"},
    {"tool": "run_command", "field": "command", "pattern": r"taskkill\b",
     "action": "confirm", "label": "终止进程 (taskkill)", "id": "RC_CONFIRM_TASKKILL"},
    {"tool": "run_command", "field": "command", "pattern": r"Stop-Process\b",
     "action": "confirm", "label": "终止进程 (Stop-Process)", "id": "RC_CONFIRM_STOPPROC"},
    {"tool": "run_command", "field": "command", "pattern": r"kill\s+-\d",
     "action": "confirm", "label": "终止进程 (kill)", "id": "RC_CONFIRM_KILL"},
    {"tool": "run_command", "field": "command", "pattern": r"tskill\b",
     "action": "confirm", "label": "终止进程 (tskill)", "id": "RC_CONFIRM_TSKILL"},

    # ─── create_code: 覆写大文件需确认 ───
    {"tool": "create_code", "check": "overwrite_large_file",
     "action": "confirm", "label": "覆写大文件", "id": "CC_CONFIRM_OVERWRITE_LARGE"},
]


# ═══════════════════════════════════════════════════════════════
# 自定义检查函数（供 check="xxx" 规则使用）
# ═══════════════════════════════════════════════════════════════

def _check_overwrite_large_file(args: Dict[str, Any]) -> bool:
    """检查 create_code 是否正在覆写一个大文件（>200行）"""
    target = args.get("target_file") or args.get("file_path", "")
    project_dir = args.get("_project_dir", "")
    if not target:
        return False
    # 拼绝对路径
    if not os.path.isabs(target):
        target = os.path.join(project_dir, target)
    if not os.path.exists(target):
        return False  # 新文件，不拦截
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
        return line_count > 200
    except Exception:
        return False


# 自定义检查函数注册表
_CUSTOM_CHECKS = {
    "overwrite_large_file": _check_overwrite_large_file,
}


# ═══════════════════════════════════════════════════════════════
# 核心检查函数
# ═══════════════════════════════════════════════════════════════

def check(tool_name: str, args: Dict[str, Any]) -> Optional[RiskResult]:
    """检查工具调用是否命中风险规则。

    优先匹配 block 规则（全部扫描后返回第一个命中）；
    block 未命中时再匹配 confirm 规则。

    Args:
        tool_name: 工具名
        args: 工具参数

    Returns:
        RiskResult 或 None（无风险）
    """
    block_hit = None
    confirm_hit = None

    for rule in RISK_RULES:
        if rule["tool"] != tool_name:
            continue

        matched = False

        # 模式 1: 正则匹配指定字段
        if "field" in rule and "pattern" in rule:
            value = str(args.get(rule["field"], ""))
            if re.search(rule["pattern"], value, re.IGNORECASE):
                matched = True

        # 模式 2: 自定义检查函数
        elif "check" in rule:
            check_fn = _CUSTOM_CHECKS.get(rule["check"])
            if check_fn and check_fn(args):
                matched = True

        if matched:
            result = RiskResult(
                action=rule["action"],
                label=rule["label"],
                rule_id=rule["id"],
            )
            # block 优先级最高，立即返回
            if rule["action"] == "block":
                return result
            # confirm 记录第一个命中
            if confirm_hit is None:
                confirm_hit = result

    return confirm_hit
