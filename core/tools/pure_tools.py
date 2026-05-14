"""
Ulimite 工具集 — 统一聚合导出

所有工具按类别从独立文件导入，统一导出为两个列表：
- ALL_PURE_TOOLS: 无 LLM 的纯函数工具
- ALL_AGENT_TOOLS: 内部封装子 Agent LLM 调用的工具
- ALL_TOOLS: 全部工具（Master 完整工具箱）

新增工具只需：1. 创建 core/tools/xxx/xxx_tool.py  2. 在 core/tools/xxx/__init__.py 导出 TOOL_DEF  3. 在此处 import 并加入列表。
"""

# ═══ 纯函数工具（无 LLM） ═══
from core.tools.read_file import TOOL_DEF as READ_FILE_DEF
from core.tools.write_file import TOOL_DEF as WRITE_FILE_DEF
from core.tools.run_command import TOOL_DEF as RUN_COMMAND_DEF
from core.tools.grep_search import TOOL_DEF as GREP_SEARCH_DEF
from core.tools.update_todo import TOOL_DEF as UPDATE_TODO_DEF
from core.tools.ls import TOOL_DEF as LS_DEF
from core.tools.get_skeleton import TOOL_DEF as GET_SKELETON_DEF
from core.tools.save_phases import TOOL_DEF as SAVE_PHASES_DEF
from core.tools.save_plan import TOOL_DEF as SAVE_PLAN_DEF
from core.tools.create_code import TOOL_DEF as CREATE_CODE_DEF
from core.tools.edit_code import TOOL_DEF as EDIT_CODE_DEF
from core.tools.ask_user import TOOL_DEF as ASK_USER_DEF
from core.tools.recall_step import TOOL_DEF as RECALL_STEP_DEF
from core.tools.submit_subagent_report import TOOL_DEF as SUBMIT_SUBAGENT_REPORT_DEF

ALL_PURE_TOOLS = [
    READ_FILE_DEF,
    WRITE_FILE_DEF,
    RUN_COMMAND_DEF,
    GREP_SEARCH_DEF,
    UPDATE_TODO_DEF,
    LS_DEF,
    GET_SKELETON_DEF,
    SAVE_PHASES_DEF,
    SAVE_PLAN_DEF,
    CREATE_CODE_DEF,
    EDIT_CODE_DEF,
    ASK_USER_DEF,
    RECALL_STEP_DEF,
    SUBMIT_SUBAGENT_REPORT_DEF,
]

# ═══ 子 Agent 工具（内部有 LLM 调用） ═══
# from core.tools.plan_project import TOOL_DEF as PLAN_PROJECT_DEF  # DEPRECATED: 由 save_phases + save_plan 替代
# from core.tools.write_code import TOOL_DEF as WRITE_CODE_DEF  # DEPRECATED: 由 create_code + edit_code 替代（Phase 2 Coder 退役）
# from core.tools.investigate import TOOL_DEF as INVESTIGATE_DEF  # DEPRECATED: 诊断能力内化到 Master error_recovery prompt（Phase 3）
# from core.tools.run_tests import TOOL_DEF as RUN_TESTS_DEF  # Phase 3 再启用
# from core.tools.recall import TOOL_DEF as RECALL_DEF  # DEPRECATED: Coder 记忆系统退役（D10）
from core.tools.task_done import TOOL_DEF as TASK_DONE_DEF
from core.tools.subagent import TOOL_DEF as SPAWN_SUBAGENT_DEF

ALL_AGENT_TOOLS = [
    # PLAN_PROJECT_DEF,  # DEPRECATED
    # WRITE_CODE_DEF,  # DEPRECATED: Phase 2 Coder 退役
    # INVESTIGATE_DEF,  # DEPRECATED: Phase 3 诊断内化
    # RUN_TESTS_DEF,  # Phase 3 再启用
    # RECALL_DEF,  # DEPRECATED: D10
    TASK_DONE_DEF,
    SPAWN_SUBAGENT_DEF,
]

# ═══ Master 完整工具箱 ═══
ALL_TOOLS = ALL_PURE_TOOLS + ALL_AGENT_TOOLS
