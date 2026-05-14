"""
constraint_store — 自进化约束存储与管理

管理条件约束规则（JSON 文件持久化），提供匹配、淘汰、矛盾检测功能。
约束来源于 EvoAgent 对失败轨迹的分析，用于在后续交互中预防同类错误。

存储位置：<project_dir>/.astrea/constraints.json
约束级别：hard（强制，≤3 条）/ hint（建议，≤15 条）

设计要点：
- 不碰 L1 system prompt
- 约束按需注入到 L2b（方案 D + 方案 E）
- 自动淘汰过期/无效约束（防过拟合）
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("ConstraintStore")

# 约束总量上限
MAX_HARD_CONSTRAINTS = 3
MAX_HINT_CONSTRAINTS = 15
MAX_TOTAL = MAX_HARD_CONSTRAINTS + MAX_HINT_CONSTRAINTS

# 淘汰阈值（基于 round_counter 轮次）
UNUSED_EXPIRE_ROUNDS = 40     # 创建后 40 轮未触发 → 删除
STALE_EXPIRE_ROUNDS = 60      # 最后触发距今 60 轮 → 删除


class ConstraintStore:
    """约束存储与查询。JSON 文件持久化，内存缓存。"""

    def __init__(self, store_path: str):
        """初始化，从 constraints.json 加载。

        参数:
            store_path: constraints.json 的完整路径
        """
        self._path = store_path
        self._data: Dict[str, Any] = {"version": 1, "constraints": []}
        self._load()

    def _load(self) -> None:
        """从磁盘加载约束。"""
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.debug(f"约束已加载: {len(self._data.get('constraints', []))} 条")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"约束文件读取失败，使用空存储: {e}")
                self._data = {"version": 1, "constraints": []}

    def save(self) -> None:
        """持久化到 JSON 文件。"""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        logger.debug(f"约束已保存: {len(self._data['constraints'])} 条")

    @property
    def constraints(self) -> List[Dict[str, Any]]:
        """所有约束列表（含 disabled）。"""
        return self._data.get("constraints", [])

    def _next_id(self) -> str:
        """生成下一个约束 ID。"""
        existing_ids = [c.get("id", "") for c in self.constraints]
        max_num = 0
        for cid in existing_ids:
            if cid.startswith("c_"):
                try:
                    max_num = max(max_num, int(cid[2:]))
                except ValueError:
                    pass
        return f"c_{max_num + 1:03d}"

    # ═══════════════════════════════════════════════════
    # 添加约束（含矛盾检测 + 总量控制）
    # ═══════════════════════════════════════════════════

    def add(self, constraint: dict) -> str:
        """添加约束，返回 id。自动执行矛盾检测和总量控制。

        参数:
            constraint: 包含 level, condition, advice, trigger_tags 的 dict
        返回:
            分配的约束 id
        """
        # 补全字段
        cid = self._next_id()
        constraint.setdefault("id", cid)
        constraint.setdefault("level", "hint")
        constraint.setdefault("reason", "")
        constraint.setdefault("source", {})
        constraint.setdefault("stats", {
            "triggered_count": 0,
            "last_triggered_at_round": None,
            "prevented_errors": 0,
            "created_at_round": None,  # 由调用方设置
        })
        if "created_at" not in constraint.get("source", {}):
            constraint.setdefault("source", {})["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 矛盾检测
        conflicts = self.check_conflict(constraint)
        for conflict in conflicts:
            old_id = conflict["existing_id"]
            logger.info(f"约束矛盾: 新约束与 {old_id} 重叠 {conflict['overlap_ratio']:.0%}，替换旧约束")
            # 合并 trigger_tags，删除旧约束
            old = self._get_by_id(old_id)
            if old:
                merged_tags = list(set(constraint.get("trigger_tags", [])) | set(old.get("trigger_tags", [])))
                constraint["trigger_tags"] = merged_tags
                self.remove(old_id)

        # 总量控制
        self._enforce_capacity(constraint.get("level", "hint"))

        self._data["constraints"].append(constraint)
        logger.info(f"约束已添加: {cid} - {constraint.get('advice', '')[:50]}")
        return cid

    def _enforce_capacity(self, level: str) -> None:
        """确保添加新约束后不超过总量上限。超限时淘汰最低优先级的。"""
        active = self.get_active()
        hard_count = sum(1 for c in active if c.get("level") == "hard")
        hint_count = sum(1 for c in active if c.get("level") == "hint")

        if level == "hard" and hard_count >= MAX_HARD_CONSTRAINTS:
            # 淘汰 triggered_count 最低的 hard 约束
            hard_list = [c for c in active if c.get("level") == "hard"]
            hard_list.sort(key=lambda c: c.get("stats", {}).get("triggered_count", 0))
            if hard_list:
                self.remove(hard_list[0]["id"])
                logger.info(f"hard 约束超限，淘汰: {hard_list[0]['id']}")

        elif level == "hint" and hint_count >= MAX_HINT_CONSTRAINTS:
            # 淘汰 triggered_count 最低的 hint 约束
            hint_list = [c for c in active if c.get("level") == "hint"]
            hint_list.sort(key=lambda c: c.get("stats", {}).get("triggered_count", 0))
            if hint_list:
                self.remove(hint_list[0]["id"])
                logger.info(f"hint 约束超限，淘汰: {hint_list[0]['id']}")

    # ═══════════════════════════════════════════════════
    # 查询与匹配
    # ═══════════════════════════════════════════════════

    def get_active(self) -> List[Dict[str, Any]]:
        """获取所有活跃约束（排除 disabled）。"""
        return [c for c in self.constraints if not c.get("disabled", False)]

    def match(
        self,
        context_tags: Set[str],
        strict_tool: Optional[str] = None,
        max_results: int = 3,
    ) -> List[Dict[str, Any]]:
        """按标签匹配约束。

        参数:
            context_tags: 上下文标签集合（工具名、OS 类型、关键词等）
            strict_tool: 非空时要求工具名精确匹配（方案 E 使用）
            max_results: 最大返回条数
        """
        matched = []
        for constraint in self.get_active():
            tags = set(constraint.get("trigger_tags", []))
            if strict_tool:
                # 方案 E：工具名必须在 tags 中
                if strict_tool not in tags:
                    continue
                if len(tags & context_tags) >= 1:
                    matched.append(constraint)
            else:
                # 方案 D：标签交集即可
                if tags & context_tags:
                    matched.append(constraint)

        # 按 triggered_count 降序
        matched.sort(key=lambda c: c.get("stats", {}).get("triggered_count", 0), reverse=True)
        return matched[:max_results]

    def record_trigger(self, constraint_id: str, current_round: int = 0) -> None:
        """记录一次触发（更新 triggered_count 和 last_triggered_at_round）。"""
        c = self._get_by_id(constraint_id)
        if c:
            stats = c.setdefault("stats", {})
            stats["triggered_count"] = stats.get("triggered_count", 0) + 1
            stats["last_triggered_at_round"] = current_round

    # ═══════════════════════════════════════════════════
    # 矛盾检测
    # ═══════════════════════════════════════════════════

    def check_conflict(self, new_constraint: dict) -> List[Dict[str, Any]]:
        """检测新约束与已有约束的 trigger_tags 重叠。

        tags 高度重叠（≥ 80%）时视为矛盾。
        """
        conflicts = []
        new_tags = set(new_constraint.get("trigger_tags", []))
        if not new_tags:
            return conflicts

        for old in self.get_active():
            old_tags = set(old.get("trigger_tags", []))
            if not old_tags:
                continue
            union_size = len(new_tags | old_tags)
            if union_size == 0:
                continue
            overlap = len(new_tags & old_tags) / union_size
            if overlap >= 0.8:
                conflicts.append({
                    "existing_id": old["id"],
                    "overlap_ratio": overlap,
                    "existing_advice": old.get("advice", ""),
                    "new_advice": new_constraint.get("advice", ""),
                })
        return conflicts

    # ═══════════════════════════════════════════════════
    # 淘汰清理
    # ═══════════════════════════════════════════════════

    def run_cleanup(self, current_round: int = 0) -> List[str]:
        """执行淘汰清理，返回被删除的约束 id 列表。

        淘汰规则：
        1. triggered_count == 0 且距创建已过 UNUSED_EXPIRE_ROUNDS 轮 → 删除
        2. last_triggered_at_round 距当前已过 STALE_EXPIRE_ROUNDS 轮 → 删除
        """
        to_remove = []
        for c in self.get_active():
            stats = c.get("stats", {})
            created_round = stats.get("created_at_round", 0) or 0
            triggered = stats.get("triggered_count", 0)
            last_triggered = stats.get("last_triggered_at_round")

            # 规则 1：从未触发且已过期
            if triggered == 0 and (current_round - created_round) >= UNUSED_EXPIRE_ROUNDS:
                to_remove.append(c["id"])
                continue

            # 规则 2：长期未触发
            if last_triggered is not None and (current_round - last_triggered) >= STALE_EXPIRE_ROUNDS:
                to_remove.append(c["id"])
                continue

        for cid in to_remove:
            self.remove(cid)
            logger.info(f"约束已淘汰: {cid}")

        return to_remove

    # ═══════════════════════════════════════════════════
    # CRUD 辅助
    # ═══════════════════════════════════════════════════

    def _get_by_id(self, constraint_id: str) -> Optional[Dict[str, Any]]:
        """按 id 查找约束。"""
        for c in self.constraints:
            if c.get("id") == constraint_id:
                return c
        return None

    def remove(self, constraint_id: str) -> bool:
        """删除指定约束，返回是否成功。"""
        before = len(self.constraints)
        self._data["constraints"] = [c for c in self.constraints if c.get("id") != constraint_id]
        return len(self.constraints) < before

    def list_all(self) -> List[Dict[str, Any]]:
        """返回所有约束的摘要信息（用于 CLI 展示）。"""
        result = []
        for c in self.constraints:
            result.append({
                "id": c.get("id"),
                "level": c.get("level"),
                "advice": c.get("advice", "")[:60],
                "trigger_tags": c.get("trigger_tags", []),
                "triggered_count": c.get("stats", {}).get("triggered_count", 0),
                "disabled": c.get("disabled", False),
            })
        return result
