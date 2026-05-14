"""
SkillManager — Skill 发现、加载、冲突检测

Skill 是可插拔的领域知识 + 行为指令 Prompt 片段，注入 Master 的 L1 层。

目录约定：
    skills/
      python-fastapi/
        SKILL.md          # Prompt 文本（注入 L1）
        metadata.json     # 元数据（名称、标签、启用状态等）

设计约束：
- 仅项目级（skills/ 目录），不支持全局 Skill 目录
- 会话开始时加载，运行时冻结
- 多 Skill 共存时执行 Tag 正交性检测
- Token 预算上限：所有启用 Skill 的总字符数 ≤ MAX_SKILL_CHARS
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("SkillManager")

# Token 预算上限（字符数，约 30K Token × 2.5 字符/Token）
MAX_SKILL_CHARS = 75000

# 通用标签白名单（高频出现、不构成冲突）
GENERIC_TAGS = frozenset({
    "programming", "architecture", "python", "javascript", "typescript",
    "backend", "frontend", "fullstack", "devops", "testing",
    "web", "api", "database", "cli", "automation",
})


@dataclass
class SkillInfo:
    """单个 Skill 的完整信息。"""

    name: str
    description: str = ""
    version: str = "1.0"
    tags: List[str] = field(default_factory=list)
    exclusive: List[str] = field(default_factory=list)
    enabled: bool = False
    # 运行时填充
    dir_path: str = ""          # Skill 目录绝对路径
    prompt_text: str = ""       # SKILL.md 文本内容
    char_count: int = 0         # prompt_text 字符数


class SkillManager:
    """Skill 发现、加载、冲突检测。"""

    def __init__(self, skills_dir: str):
        self._skills_dir = skills_dir
        self._available: Dict[str, SkillInfo] = {}   # 所有发现的 Skill
        self._load_order: List[str] = []             # 发现顺序（保持确定性）

    # ────────────────────────────────────────
    # 发现与加载
    # ────────────────────────────────────────

    def discover(self) -> List[SkillInfo]:
        """扫描 skills/ 目录，发现并加载所有合法 Skill。

        合法 Skill = 包含 SKILL.md 的子目录。
        metadata.json 可选：缺失时从 SKILL.md frontmatter 提取元数据。

        返回:
            所有发现的 SkillInfo 列表
        """
        self._available.clear()
        self._load_order.clear()

        if not os.path.isdir(self._skills_dir):
            logger.info(f"Skill 目录不存在: {self._skills_dir}，跳过加载")
            return []

        for entry in sorted(os.listdir(self._skills_dir)):
            skill_dir = os.path.join(self._skills_dir, entry)
            if not os.path.isdir(skill_dir):
                continue

            skill_md = os.path.join(skill_dir, "SKILL.md")

            # 最低要求：SKILL.md 必须存在
            if not os.path.isfile(skill_md):
                logger.debug(f"跳过无 SKILL.md 的目录: {entry}")
                continue

            metadata_json = os.path.join(skill_dir, "metadata.json")
            has_metadata = os.path.isfile(metadata_json)

            try:
                info = self._load_skill(
                    skill_dir, skill_md,
                    metadata_json if has_metadata else None,
                )
                self._available[info.name] = info
                self._load_order.append(info.name)
                status = "启用" if info.enabled else "禁用"
                source = "metadata.json" if has_metadata else "frontmatter"
                logger.info(
                    f"📦 Skill [{info.name}] 已加载 ({status}, "
                    f"{info.char_count} 字符, 来源={source})"
                )
            except Exception as e:
                logger.warning(f"⚠️ Skill [{entry}] 加载失败: {e}")

        logger.info(f"📦 Skill 发现完成: {len(self._available)} 个")
        return list(self._available.values())

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        """从 SKILL.md 的 YAML frontmatter 提取元数据。

        支持格式:
            ---
            name: my-skill
            description: 描述文本
            ---
        """
        import re
        match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
        if not match:
            return {}

        result = {}
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _load_skill(
        skill_dir: str, skill_md: str,
        metadata_json: Optional[str] = None,
    ) -> SkillInfo:
        """加载单个 Skill。

        优先级：metadata.json > SKILL.md frontmatter > 目录名兜底。
        """
        # 读取 SKILL.md
        with open(skill_md, "r", encoding="utf-8") as f:
            prompt_text = f.read().strip()

        # 元数据来源：metadata.json 或 frontmatter
        if metadata_json:
            with open(metadata_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = SkillManager._parse_frontmatter(prompt_text)

        dir_name = os.path.basename(skill_dir)

        return SkillInfo(
            name=meta.get("name", dir_name),
            description=meta.get("description", ""),
            version=meta.get("version", "1.0"),
            tags=meta.get("tags", []),
            exclusive=meta.get("exclusive", []),
            enabled=meta.get("enabled", True if not metadata_json else False),
            dir_path=skill_dir,
            prompt_text=prompt_text,
            char_count=len(prompt_text),
        )

    # ────────────────────────────────────────
    # 启用 / 禁用
    # ────────────────────────────────────────

    def enable(self, name: str) -> str:
        """启用指定 Skill。

        返回:
            操作结果消息
        """
        info = self._available.get(name)
        if not info:
            # 尝试模糊匹配
            candidates = [n for n in self._available if name.lower() in n.lower()]
            if len(candidates) == 1:
                info = self._available[candidates[0]]
            elif candidates:
                return f"匹配到多个 Skill: {', '.join(candidates)}，请精确指定"
            else:
                return f"Skill [{name}] 不存在"

        if info.enabled:
            return f"Skill [{info.name}] 已处于启用状态"

        # Token 预算检查
        current_total = self.total_chars()
        if current_total + info.char_count > MAX_SKILL_CHARS:
            return (
                f"Skill [{info.name}] ({info.char_count} 字符) 超出预算上限 "
                f"({current_total + info.char_count}/{MAX_SKILL_CHARS})"
            )

        info.enabled = True
        self._persist_state(info)
        logger.info(f"📦 Skill [{info.name}] 已启用")
        return f"Skill [{info.name}] 已启用 ({info.char_count} 字符)"

    def disable(self, name: str) -> str:
        """禁用指定 Skill。

        返回:
            操作结果消息
        """
        info = self._available.get(name)
        if not info:
            candidates = [n for n in self._available if name.lower() in n.lower()]
            if len(candidates) == 1:
                info = self._available[candidates[0]]
            else:
                return f"Skill [{name}] 不存在"

        if not info.enabled:
            return f"Skill [{info.name}] 已处于禁用状态"

        info.enabled = False
        self._persist_state(info)
        logger.info(f"📦 Skill [{info.name}] 已禁用")
        return f"Skill [{info.name}] 已禁用"

    def _persist_state(self, info: SkillInfo) -> None:
        """将 Skill 状态写回 metadata.json（无则自动创建）。"""
        meta_path = os.path.join(info.dir_path, "metadata.json")

        # 已有文件则读取并更新，否则从当前 info 生成
        if os.path.isfile(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {}

        meta.update({
            "name": info.name,
            "description": info.description,
            "version": info.version,
            "tags": info.tags,
            "exclusive": info.exclusive,
            "enabled": info.enabled,
        })

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        logger.debug(f"📦 Skill [{info.name}] 状态已持久化到 metadata.json")

    # ────────────────────────────────────────
    # 冲突检测
    # ────────────────────────────────────────

    def check_conflicts(self) -> List[str]:
        """Tag 正交性检测：排除通用标签后，检测特异性标签重叠。

        返回:
            警告消息列表（空 = 无冲突）
        """
        enabled = [info for info in self._available.values() if info.enabled]
        if len(enabled) < 2:
            return []

        warnings = []

        # 计算每个 Skill 的特异性标签
        specific_tags = {
            info.name: set(info.tags) - GENERIC_TAGS
            for info in enabled
        }

        # 两两比较特异性标签交集
        names = [info.name for info in enabled]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                overlap = specific_tags[names[i]] & specific_tags[names[j]]
                if overlap:
                    warnings.append(
                        f"[{names[i]}] 与 [{names[j]}] 存在标签重叠: "
                        f"{', '.join(sorted(overlap))}"
                    )

        # exclusive 维度冲突检测（强警告）
        exclusive_map: Dict[str, str] = {}  # dimension → first_skill_name
        for info in enabled:
            for dim in info.exclusive:
                if dim in exclusive_map:
                    warnings.append(
                        f"⚠️ [{info.name}] 与 [{exclusive_map[dim]}] "
                        f"声明了互斥维度: {dim}"
                    )
                else:
                    exclusive_map[dim] = info.name

        return warnings

    # ────────────────────────────────────────
    # Prompt 构建
    # ────────────────────────────────────────

    def build_segments(self) -> List[str]:
        """返回所有启用 Skill 的 SKILL.md 文本列表。

        按发现顺序排列（确定性）。
        每段前附加目录声明，使 Agent 能将 SKILL.md 中的相对路径
        解析为绝对路径（如 read_file 调用）。
        """
        segments = []
        for name in self._load_order:
            info = self._available.get(name)
            if info and info.enabled and info.prompt_text:
                header = (
                    f"<!-- Skill: {info.name} | "
                    f"Dir: {info.dir_path} -->\n\n"
                )
                segments.append(header + info.prompt_text)
        return segments

    def total_chars(self) -> int:
        """当前启用 Skill 的总字符数。"""
        return sum(
            info.char_count
            for info in self._available.values()
            if info.enabled
        )

    # ────────────────────────────────────────
    # 信息查询
    # ────────────────────────────────────────

    def list_info(self) -> List[Dict]:
        """返回所有 Skill 的摘要信息（供 CLI 展示）。"""
        result = []
        for name in self._load_order:
            info = self._available.get(name)
            if not info:
                continue
            result.append({
                "name": info.name,
                "description": info.description,
                "version": info.version,
                "tags": info.tags,
                "exclusive": info.exclusive,
                "enabled": info.enabled,
                "chars": info.char_count,
                "tokens_est": int(info.char_count / 2.5),
            })
        return result

    def get_enabled_names(self) -> List[str]:
        """返回当前启用的 Skill 名称列表。"""
        return [
            info.name
            for info in self._available.values()
            if info.enabled
        ]

    @property
    def skills_dir(self) -> str:
        return self._skills_dir
