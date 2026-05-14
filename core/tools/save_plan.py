"""
save_plan 工具 — 文件规划持久化 + DAG 计算 + Todo 初始化 + 动态上下文注入

零 LLM 调用。接收 Master 自主规划的文件清单，执行：
1. JSON 解析 + task_id 自动分配
2. DAG 拓扑分级（_compute_dag_levels）
3. Todo 初始化（memory.init_todo）
4. DAG 调度动态上下文注入（load_and_inject）
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from core.tools import ToolDefinition

logger = logging.getLogger("Tool.SavePlan")


def _compute_dag_levels(files: List[dict]) -> None:
    """拓扑分级：根据 deps 计算每个文件的 level（原地修改）。

    Level 0 = 无依赖，Level N = 依赖的文件中最高 level + 1。

    使用迭代不动点算法：反复遍历直到所有 level 稳定，
    不受输入顺序影响。

    deps 支持两种格式：task_id（如 "task_1"）或文件路径（如 "models.py"）。
    """
    # 双索引：同时支持 task_id 和 path 查找
    dep_to_idx = {}
    for i, f in enumerate(files):
        dep_to_idx[f["task_id"]] = i
        if f.get("path"):
            dep_to_idx[f["path"]] = i

    levels = [0] * len(files)

    # 迭代直到收敛（最多 N 轮，N = 文件数）
    for _ in range(len(files)):
        changed = False
        for i, f in enumerate(files):
            for dep_id in f.get("deps", []):
                if dep_id in dep_to_idx:
                    dep_idx = dep_to_idx[dep_id]
                    new_level = levels[dep_idx] + 1
                    if new_level > levels[i]:
                        levels[i] = new_level
                        changed = True
        if not changed:
            break

    for i, f in enumerate(files):
        f["level"] = levels[i]

    level_summary = {}
    for f in files:
        level_summary.setdefault(f["level"], []).append(f["path"])
    logger.info(f"📊 DAG 分级: {dict(level_summary)}")




def _build_dag_levels_text(files: List[dict]) -> str:
    """构建 DAG 分级摘要文本（供动态上下文模板渲染）。"""
    levels = {}
    for f in files:
        lvl = f.get("level", 0)
        levels.setdefault(lvl, []).append(f.get("path", ""))

    dag_lines = []
    for lvl in sorted(levels.keys()):
        files_str = ", ".join(levels[lvl])
        parallel_hint = "（可并行）" if len(levels[lvl]) > 1 else ""
        dag_lines.append(f"Level {lvl}: {files_str} {parallel_hint}")

    return "\n".join(dag_lines)


def save_plan(
    files: str,
    tech_stack: Optional[str] = None,
    _project_dir: str = "",
    _project_id: str = "default",
    _memory=None,
) -> Dict[str, Any]:
    """将文件规划持久化，并初始化 Todo + DAG 动态上下文。

    参数:
        files: 文件清单 JSON 字符串（含 path, description, deps）
        tech_stack: 技术栈字符串（可选）
        _project_dir: 项目目录（Master 自动注入）
        _project_id: 项目 ID（Master 自动注入）
        _memory: LayeredMemory 实例（Master 自动注入）
    返回:
        {"status": "ok", "files": [...], "build_order": [...], "dag_levels": "..."}
    """
    # ═══ 1. 解析 JSON ═══
    try:
        files_data = json.loads(files) if isinstance(files, str) else files
    except json.JSONDecodeError as e:
        return {"status": "fail", "feedback": f"files JSON 解析失败: {e}"}

    # 兼容两种格式：直接列表 或 {"files": [...]}
    if isinstance(files_data, dict):
        files_list = files_data.get("files", [])
    elif isinstance(files_data, list):
        files_list = files_data
    else:
        return {"status": "fail", "feedback": f"files 格式错误: 预期 list 或 dict，得到 {type(files_data).__name__}"}

    if not files_list:
        return {"status": "fail", "feedback": "文件清单为空"}

    # ═══ 2. 自动分配 task_id ═══
    for i, f in enumerate(files_list):
        if "task_id" not in f:
            f["task_id"] = f"task_{i + 1}"
        if "path" not in f and "target_file" in f:
            f["path"] = f["target_file"]
        if "deps" not in f:
            f["deps"] = f.get("dependencies", [])

    # ═══ 3. DAG 拓扑分级 ═══
    _compute_dag_levels(files_list)

    # ═══ 4. 构建输出 ═══
    build_order = [f["path"] for f in files_list]
    dag_levels_text = _build_dag_levels_text(files_list)

    # ═══ 5. 初始化 Todo ═══
    if _memory:
        todo_items = [
            {
                "task_id": f.get("task_id", f"task_{i+1}"),
                "target_file": f.get("path", ""),
                "description": f.get("description", ""),
                "level": f.get("level", 0),
            }
            for i, f in enumerate(files_list)
            if f.get("path")
        ]
        if todo_items:
            _memory.init_todo(todo_items)
            logger.info(f"📋 Todo 已初始化: {len(todo_items)} 项")

    # ═══ 6. 构建 Todo Checkbox 快照（进入 L2b）═══
    todo_snapshot = ""
    if _memory:
        todo_snapshot = _memory.render_todo_checkbox()

    # ═══ 7. 写入磁盘 ═══
    if _project_dir:
        astrea_dir = os.path.join(_project_dir, ".astrea")
        os.makedirs(astrea_dir, exist_ok=True)
        plan_path = os.path.join(astrea_dir, "plan.json")
        try:
            plan_data = {
                "tech_stack": tech_stack or "",
                "files": files_list,
                "build_order": build_order,
            }
            with open(plan_path, "w", encoding="utf-8") as f:
                json.dump(plan_data, f, ensure_ascii=False, indent=2)
            logger.info(f"📋 Plan 已写入: {plan_path}")
        except Exception as e:
            logger.warning(f"⚠️ Plan 写入磁盘失败: {e}")

    result = {
        "status": "ok",
        "files": files_list,
        "build_order": build_order,
        "dag_levels": dag_levels_text,
        "tech_stack": tech_stack or "",
        "summary": f"{len(files_list)} 个文件, DAG {len(set(f.get('level', 0) for f in files_list))} 层",
    }
    if todo_snapshot:
        result["todo_snapshot"] = todo_snapshot
    return result


TOOL_DEF = ToolDefinition(
    name="save_plan",
    description=(
        "将文件规划持久化并初始化构建队列。"
        "输入文件清单（含路径、描述、依赖关系），自动计算 DAG 分级和构建顺序。"
        "调用后按 DAG Level 并行 write_code。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "string",
                "description": (
                    "文件清单的 JSON 字符串。每个文件包含: "
                    "path(文件路径), description(功能描述), "
                    "deps(依赖的其他文件 task_id 列表)。"
                    "deps 决定 DAG 分级和并行策略，必须准确声明。"
                    "例如 routes.py 依赖 models.py，则 routes 的 deps 填 models 的 task_id。"
                    "未声明 deps 的文件会被归入 Level 0 同时并行写入。"
                ),
            },
            "tech_stack": {
                "type": "string",
                "description": "技术栈（可选，如 'Flask + SQLite + Jinja2'）",
            },
        },
        "required": ["files"],
    },
    handler=save_plan,
)
