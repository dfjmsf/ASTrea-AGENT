"""
evo_agent — 自进化独立特化 Agent

独立于 Master 的特化 Agent，复用 LLMClient，无工具调用能力。
负责两个核心任务：
1. analyze_failures()：LLM 驱动的错误分类 + 约束生成
2. synthesize_tool()：模板填空式工具合成（Sprint 3 实现）

设计要点：
- 复用 MODEL_MASTER（后续可分层独立配置）
- 输出强制 JSON 格式，容错处理 markdown 代码块
- 每次调用是独立的 one-shot，不维护对话历史
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("EvoAgent")

# 失败分析的系统提示
_FAILURE_ANALYSIS_SYSTEM = """你是一个错误分析专家。你的任务是分析 Agent 在执行过程中遇到的重复错误，并生成简短的操作建议。

输出要求：
- 输出必须是合法 JSON 数组，不要包含 markdown 代码块标记
- 每条约束的 advice 不超过 50 字
- level 只能是 "hint"（建议）或 "hard"（强制，仅用于严重错误如数据丢失、无限循环）
- 绝大多数情况使用 "hint"
- 措辞规范：使用"建议..."、"优先..."、"当[条件]时，[行为]"，禁止"绝对不要"、"永远不能"、"禁止"

输出格式（JSON 数组）：
[
  {
    "level": "hint",
    "condition": "触发条件描述",
    "advice": "建议行为（< 50 字）",
    "trigger_tags": ["标签1", "标签2"]
  }
]
"""

# 失败分析的用户提示模板
_FAILURE_ANALYSIS_USER = """以下是本次会话中反复出现的错误模式（已过滤连续重试噪声，仅保留间隔复现的系统性问题）：

{failures_text}

请分析这些错误模式，为每个错误生成一条简短的操作建议。输出合法 JSON 数组。"""


class EvoAgent:
    """独立特化 Agent：工具合成 + 失败分析。

    复用 LLMClient，不维护对话历史，每次调用是独立的 one-shot。
    """

    def __init__(self, llm_client, model: Optional[str] = None):
        """初始化 EvoAgent。

        参数:
            llm_client: LLMClient 实例（复用 Master 的）
            model: 使用的模型（默认复用 MODEL_MASTER）
        """
        self._llm = llm_client
        self._model = model or os.getenv("MODEL_MASTER", os.getenv("MODEL_PRO", "deepseek-v4-pro"))

    # ═══════════════════════════════════════════════════
    # 失败分析 → 约束生成
    # ═══════════════════════════════════════════════════

    async def analyze_failures(self, failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """分析失败事件，生成约束建议。

        参数:
            failures: 失败事件列表，每项含 error_signature, occurrences, max_gap

        返回:
            约束列表 [{level, condition, advice, trigger_tags}]
        """
        if not failures:
            return []

        # 构建失败描述文本
        failures_text = self._format_failures(failures)

        # 调用 LLM
        messages = [
            {"role": "system", "content": _FAILURE_ANALYSIS_SYSTEM},
            {"role": "user", "content": _FAILURE_ANALYSIS_USER.format(failures_text=failures_text)},
        ]

        try:
            response = await self._llm.chat_completion(
                model=self._model,
                messages=messages,
                temperature=0.1,
            )
            raw = getattr(response, "content", "") or ""
            constraints = self._parse_constraints_response(raw)
            logger.info(f"EvoAgent 分析完成: 生成 {len(constraints)} 条约束")
            return constraints
        except Exception as e:
            logger.warning(f"EvoAgent 失败分析调用失败: {e}")
            return []

    def _format_failures(self, failures: List[Dict[str, Any]]) -> str:
        """将失败事件格式化为 LLM 可读的文本。"""
        parts = []
        for i, f in enumerate(failures, 1):
            sig = f.get("error_signature", "unknown")
            max_gap = f.get("max_gap", 0)
            occurrences = f.get("occurrences", [])
            count = len(occurrences)

            # 提取典型错误信息
            typical_msg = ""
            if occurrences:
                typical_msg = occurrences[0].get("error_msg", "")[:200]

            parts.append(
                f"### 错误 {i}\n"
                f"- 错误签名: {sig}\n"
                f"- 复现间隔: 最大间隔 {max_gap} 步\n"
                f"- 出现次数: {count} 次\n"
                f"- 典型错误信息: {typical_msg}\n"
            )
        return "\n".join(parts)

    def _parse_constraints_response(self, raw: str) -> List[Dict[str, Any]]:
        """解析 LLM 输出为约束列表。容错处理 markdown 代码块。"""
        text = raw.strip()
        if not text:
            return []

        # 容错：去除可能的 ```json ... ``` 包裹
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if "```" in text:
                text = text.rsplit("```", 1)[0]

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"EvoAgent 约束输出非法 JSON，丢弃。原始内容: {text[:200]}")
            return []

        # 支持单个 dict 或 list
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            logger.warning(f"EvoAgent 约束输出格式异常，期望 list，得到 {type(result)}")
            return []

        # 校验必需字段
        required = {"level", "condition", "advice", "trigger_tags"}
        valid = []
        for item in result:
            if not isinstance(item, dict):
                continue
            if not required.issubset(item.keys()):
                missing = required - set(item.keys())
                logger.warning(f"EvoAgent 约束输出缺少字段: {missing}")
                continue
            # 强制 advice 长度
            if len(item.get("advice", "")) > 80:
                item["advice"] = item["advice"][:77] + "..."
            # 强制 level 合法值
            if item.get("level") not in ("hint", "hard"):
                item["level"] = "hint"
            valid.append(item)

        return valid

    # ═══════════════════════════════════════════════════
    # 工具合成（模板填空式）
    # ═══════════════════════════════════════════════════

    async def synthesize_tool(
        self, pattern: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """模板填空式工具合成。

        参数:
            pattern: PatternMiner 输出的候选模式，含 pattern, instances, task_summaries 等

        返回:
            {"code": str, "metadata": dict} 或 None（合成失败）
        """
        tool_sequence = pattern.get("pattern", [])
        instances = pattern.get("instances", [])
        task_summaries = pattern.get("task_summaries", [])
        pattern_hash = pattern.get("pattern_hash", "unknown")

        if not tool_sequence or not instances:
            return None

        # 参数变异度分析
        param_analysis = self._analyze_param_variability(instances)

        # 生成工具名（从 task_summaries 推断）
        tool_name = f"synth_{'_'.join(tool_sequence[:3])}"
        # 清理非法字符
        tool_name = "".join(c if c.isalnum() or c == "_" else "_" for c in tool_name)

        # 构建合成 prompt
        synth_prompt = self._build_synth_prompt(
            tool_sequence, param_analysis, instances, task_summaries, tool_name,
        )

        messages = [
            {"role": "system", "content": _SYNTH_SYSTEM},
            {"role": "user", "content": synth_prompt},
        ]

        try:
            response = await self._llm.chat_completion(
                model=self._model,
                messages=messages,
                temperature=0.1,
            )
            raw = getattr(response, "content", "") or ""
            result = self._parse_synth_response(raw, tool_name, pattern)
            if result:
                logger.info(f"工具合成成功: {tool_name}")
            else:
                logger.info(f"工具合成失败: {tool_name}")
            return result
        except Exception as e:
            logger.warning(f"EvoAgent 工具合成调用失败: {e}")
            return None

    @staticmethod
    def _analyze_param_variability(
        instances: List[List[Dict]],
    ) -> Dict[int, Dict[str, str]]:
        """分析模式中每个 step 的每个参数的变异类型。

        返回:
            {step_index: {param_key: "fixed"|"variable"|"templated"}}
        """
        if not instances or not instances[0]:
            return {}

        num_steps = len(instances[0])
        result = {}

        for step_idx in range(num_steps):
            step_params: Dict[str, List[str]] = {}

            for instance in instances:
                if step_idx >= len(instance):
                    continue
                tc = instance[step_idx]
                pattern = tc.get("args_pattern", {})
                for key, value in pattern.items():
                    step_params.setdefault(key, []).append(str(value))

            step_analysis = {}
            for key, values in step_params.items():
                unique_values = set(values)
                if len(unique_values) == 1:
                    step_analysis[key] = "fixed"
                elif len(unique_values) == len(values):
                    step_analysis[key] = "variable"
                else:
                    step_analysis[key] = "templated"

            if step_analysis:
                result[step_idx] = step_analysis

        return result

    def _build_synth_prompt(
        self,
        tool_sequence: List[str],
        param_analysis: Dict,
        instances: List,
        task_summaries: List[str],
        tool_name: str,
    ) -> str:
        """构建合成 prompt。"""
        # 提取固定值
        fixed_values = {}
        if instances and instances[0]:
            for step_idx, analysis in param_analysis.items():
                step_fixed = {}
                for key, vtype in analysis.items():
                    if vtype == "fixed" and step_idx < len(instances[0]):
                        tc = instances[0][step_idx]
                        pattern = tc.get("args_pattern", {})
                        if key in pattern:
                            step_fixed[key] = pattern[key]
                if step_fixed:
                    fixed_values[step_idx] = step_fixed

        # 提取 variable 参数作为函数参数
        variable_params = []
        for step_idx, analysis in param_analysis.items():
            for key, vtype in analysis.items():
                if vtype in ("variable", "templated"):
                    param_name = f"step{step_idx}_{key}"
                    variable_params.append(param_name)

        summaries_text = "\n".join(f"  - {s}" for s in task_summaries[:5] if s)

        return (
            f"以下是一个高频操作模式的分析结果，请生成一个 Python 函数的函数体。\n\n"
            f"## 模式信息\n"
            f"- 工具序列: {' → '.join(tool_sequence)}\n"
            f"- 函数名: {tool_name}\n"
            f"- 出现频率: {len(instances)} 次\n"
            f"- 典型任务描述:\n{summaries_text}\n\n"
            f"## 参数分析\n"
            f"- 固定参数（硬编码到函数体）: {json.dumps(fixed_values, ensure_ascii=False)}\n"
            f"- 可变参数（暴露为函数参数）: {variable_params}\n"
            f"- 参数变异度: {json.dumps(param_analysis, ensure_ascii=False, default=str)}\n\n"
            f"## 输出要求\n"
            f"请只输出函数体代码（不含函数定义行和 return），使用 4 空格缩进。\n"
            f"- `fixed` 参数直接在函数体中使用固定值\n"
            f"- `variable` 参数通过函数参数引用（已在签名中声明）\n"
            f"- 函数可以调用 os 模块的文件操作\n"
            f"- 不要使用任何外部依赖（仅限标准库）\n"
            f"- 用 ```python ``` 包裹代码\n"
        )

    def _parse_synth_response(
        self, raw: str, tool_name: str, pattern: Dict
    ) -> Optional[Dict[str, Any]]:
        """解析合成响应，生成完整的工具代码和元数据。"""
        text = raw.strip()
        if not text:
            return None

        # 提取代码块
        if "```python" in text:
            code_block = text.split("```python", 1)[1]
            if "```" in code_block:
                code_block = code_block.rsplit("```", 1)[0]
        elif "```" in text:
            code_block = text.split("```", 1)[1]
            if "```" in code_block:
                code_block = code_block.rsplit("```", 1)[0]
        else:
            code_block = text

        function_body = code_block.strip()
        if not function_body:
            return None

        # 缩进函数体（确保 4 空格缩进）
        indented_body = "\n".join(
            f"    {line}" if line.strip() else ""
            for line in function_body.split("\n")
        )

        # 组装完整代码
        description = f"合成工具：{'→'.join(pattern.get('pattern', []))}"
        full_code = (
            f'"""{description}"""\n'
            f'import os\n\n'
            f'def {tool_name}(**kwargs) -> dict:\n'
            f'    """合成工具：{description}"""\n'
            f'    project_dir = kwargs.get("_project_dir", ".")\n\n'
            f'{indented_body}\n\n'
            f'    return {{"status": "ok", "summary": "完成"}}\n'
        )

        # 元数据
        import time
        metadata = {
            "name": tool_name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
            "source_pattern": {
                "tool_sequence": pattern.get("pattern", []),
                "pattern_hash": pattern.get("pattern_hash", ""),
                "frequency": pattern.get("frequency", 0),
                "sessions": pattern.get("sessions", []),
            },
            "stats": {
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "created_at_round": None,
                "call_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "last_called_at": None,
                "last_called_at_round": None,
            },
        }

        return {"code": full_code, "metadata": metadata}


# ═══════════════════════════════════════════════════
# 模块级常量（合成 prompt）
# ═══════════════════════════════════════════════════

_SYNTH_SYSTEM = """你是一个代码合成专家。你的任务是根据高频操作模式，生成 Python 函数体代码。

规则：
- 只输出函数体代码，不含函数定义行
- 使用 4 空格缩进
- 只使用 Python 标准库
- fixed 参数直接硬编码
- variable 参数通过 kwargs 获取
- 用 ```python ``` 包裹代码
"""

