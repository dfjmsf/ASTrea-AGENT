"""风险操作拦截模块 — 声明式规则表 + 中央拦截入口"""
from core.tools._risk_guard._risk_guard import check, RiskResult, RISK_RULES

__all__ = ["check", "RiskResult", "RISK_RULES"]
