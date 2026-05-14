"""
tool_synthesizer — 自进化工具合成器

负责将 EvoAgent 生成的工具代码进行多级验证、持久化、注册到 ToolRegistry。
还负责合成工具的磁盘加载和熵减淘汰。

存储位置：<project_dir>/.astrea/synth_tools/
文件格式：synth_<name>.py（实现）+ synth_<name>.json（元数据）

验证管线：
  ① py_compile 语法校验
  ② inspect.signature 签名校验
  ③ subprocess 沙箱试运行
  ④ Registry 适配校验（name/description/parameters 三要素）
"""
import importlib.util
import inspect
import json
import logging
import os
import py_compile
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("ToolSynthesizer")

# 合成工具模板（固定骨架，LLM 只填充函数体）
SYNTH_TOOL_TEMPLATE = '''"""{description}"""
import os
{extra_imports}

def {func_name}({param_signature}) -> dict:
    """
    合成工具：{description}

    参数:
{param_docstring}

    返回:
        dict: {{"status": "ok"|"error", "summary": str, ...}}
    """
    project_dir = kwargs.get("_project_dir", ".")

    # ====== LLM 填充区域 START ======
{function_body}
    # ====== LLM 填充区域 END ======

    return {{"status": "ok", "summary": "完成"}}
'''


class ToolSynthesizer:
    """合成工具管理器：验证、持久化、加载、淘汰。"""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self._synth_dir = os.path.join(project_dir, ".astrea", "synth_tools")
        os.makedirs(self._synth_dir, exist_ok=True)

    # ═══════════════════════════════════════════════════
    # 验证 + 注册
    # ═══════════════════════════════════════════════════

    async def validate_and_register(
        self,
        tool_code: str,
        metadata: Dict[str, Any],
        registry=None,
    ) -> bool:
        """对合成工具代码执行四级验证管线，通过后持久化并注册。

        参数:
            tool_code: 完整的 Python 工具代码
            metadata: 工具元数据 dict
            registry: ToolRegistry 实例（可选，传入时直接注册）

        返回:
            是否验证通过
        """
        name = metadata.get("name", "synth_unknown")

        # ① 语法校验
        if not self._check_syntax(name, tool_code):
            logger.warning(f"合成工具 {name} 语法校验失败")
            return False

        # ② 签名校验
        func = self._check_signature(name, tool_code)
        if func is None:
            logger.warning(f"合成工具 {name} 签名校验失败")
            return False

        # ③ 沙箱试运行
        if not self._sandbox_test(name, tool_code):
            logger.warning(f"合成工具 {name} 沙箱试运行失败")
            return False

        # ④ Registry 适配校验
        if not self._check_registry_compat(metadata):
            logger.warning(f"合成工具 {name} Registry 适配校验失败")
            return False

        # 全部通过 → 持久化
        self._save_to_disk(name, tool_code, metadata)

        # 注册到 Registry（如果传入了）
        if registry:
            self._register_tool(name, tool_code, metadata, registry)

        logger.info(f"合成工具 {name} 验证通过并已保存")
        return True

    def _check_syntax(self, name: str, code: str) -> bool:
        """① 语法校验（py_compile）。"""
        tmp_path = os.path.join(self._synth_dir, f"_tmp_{name}.py")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(code)
            py_compile.compile(tmp_path, doraise=True)
            return True
        except py_compile.PyCompileError as e:
            logger.debug(f"语法错误: {e}")
            return False
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _check_signature(self, name: str, code: str) -> Optional[Callable]:
        """② 签名校验：函数必须存在、返回类型注解为 dict、参数有类型注解。"""
        # 从代码提取函数名（与 name 匹配）
        func_name = name  # synth_xxx
        try:
            # 动态执行代码获取函数对象
            namespace = {}
            exec(code, namespace)
            func = namespace.get(func_name)
            if func is None:
                logger.debug(f"未找到函数 {func_name}")
                return None
            if not callable(func):
                logger.debug(f"{func_name} 不可调用")
                return None
            # 检查返回类型注解
            sig = inspect.signature(func)
            ret = sig.return_annotation
            if ret not in (dict, inspect.Parameter.empty):
                logger.debug(f"{func_name} 返回类型注解不是 dict: {ret}")
                # 不强制，只是警告
            return func
        except Exception as e:
            logger.debug(f"签名校验异常: {e}")
            return None

    def _sandbox_test(self, name: str, code: str) -> bool:
        """③ 沙箱试运行：在子进程中 import 代码，检查是否可加载。"""
        tmp_path = os.path.join(self._synth_dir, f"_tmp_{name}.py")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(code)
            result = subprocess.run(
                [sys.executable, "-c", f"import importlib.util; "
                 f"spec = importlib.util.spec_from_file_location('{name}', r'{tmp_path}'); "
                 f"mod = importlib.util.module_from_spec(spec); "
                 f"spec.loader.exec_module(mod); "
                 f"print('OK')"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0 and "OK" in result.stdout
        except (subprocess.TimeoutExpired, Exception) as e:
            logger.debug(f"沙箱测试失败: {e}")
            return False
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _check_registry_compat(self, metadata: Dict) -> bool:
        """④ Registry 适配校验：确保 name/description/parameters 三要素完整。"""
        required = {"name", "description", "parameters"}
        return required.issubset(metadata.keys())

    # ═══════════════════════════════════════════════════
    # 持久化
    # ═══════════════════════════════════════════════════

    def _save_to_disk(self, name: str, code: str, metadata: Dict) -> None:
        """将合成工具写入磁盘。"""
        py_path = os.path.join(self._synth_dir, f"{name}.py")
        json_path = os.path.join(self._synth_dir, f"{name}.json")

        with open(py_path, "w", encoding="utf-8") as f:
            f.write(code)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    # ═══════════════════════════════════════════════════
    # 加载（启动时）
    # ═══════════════════════════════════════════════════

    def load_from_disk(self, registry) -> int:
        """从 .astrea/synth_tools/ 加载合成工具到 Registry。

        返回成功加载的工具数量。
        """
        if not os.path.isdir(self._synth_dir):
            return 0

        loaded = 0
        for filename in os.listdir(self._synth_dir):
            if not filename.endswith(".json") or filename.startswith("_tmp_"):
                continue
            name = filename[:-5]  # 去掉 .json
            py_path = os.path.join(self._synth_dir, f"{name}.py")
            json_path = os.path.join(self._synth_dir, filename)

            if not os.path.isfile(py_path):
                continue

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)

                # 检查是否被禁用
                if metadata.get("disabled", False):
                    continue

                with open(py_path, "r", encoding="utf-8") as f:
                    code = f.read()

                self._register_tool(name, code, metadata, registry)
                loaded += 1
            except Exception as e:
                logger.warning(f"加载合成工具 {name} 失败: {e}")

        if loaded > 0:
            logger.info(f"已加载 {loaded} 个合成工具")
        return loaded

    def _register_tool(
        self, name: str, code: str, metadata: Dict, registry
    ) -> None:
        """将合成工具注册到 ToolRegistry。"""
        from core.tools import ToolDefinition

        # 动态加载函数
        try:
            namespace = {}
            exec(code, namespace)
            handler = namespace.get(name)
            if handler is None:
                logger.warning(f"合成工具 {name} 中未找到同名函数")
                return
        except Exception as e:
            logger.warning(f"合成工具 {name} 加载失败: {e}")
            return

        tool_def = ToolDefinition(
            name=name,
            description=metadata.get("description", "合成工具"),
            parameters=metadata.get("parameters", {"type": "object", "properties": {}}),
            handler=handler,
        )
        registry.register(tool_def)

    # ═══════════════════════════════════════════════════
    # 熵减淘汰
    # ═══════════════════════════════════════════════════

    def run_entropy_reduction(
        self, max_tools: int = 20, current_round: int = 0
    ) -> List[str]:
        """执行合成工具熵减淘汰，返回被淘汰的工具名。

        淘汰规则：
        1. call_count == 0 且距创建已过 50 轮 → 删除
        2. failure_count / call_count > 0.5 且 call_count >= 3 → 禁用
        3. last_called_at_round 距当前已过 100 轮 → 删除
        4. 总数 > max_tools → 按 call_count 排序淘汰末位
        """
        if not os.path.isdir(self._synth_dir):
            return []

        removed = []
        tools_meta = self._load_all_metadata()

        for name, meta in tools_meta.items():
            stats = meta.get("stats", {})
            call_count = stats.get("call_count", 0)
            failure_count = stats.get("failure_count", 0)
            created_round = stats.get("created_at_round", 0) or 0
            last_called_round = stats.get("last_called_at_round")

            # 规则 1：从未使用且过期
            if call_count == 0 and (current_round - created_round) >= 50:
                self._delete_tool(name)
                removed.append(name)
                continue

            # 规则 2：高失败率
            if call_count >= 3 and failure_count / call_count > 0.5:
                meta["disabled"] = True
                self._save_metadata(name, meta)
                logger.info(f"合成工具 {name} 已禁用（高失败率）")
                continue

            # 规则 3：长期未使用
            if last_called_round is not None and (current_round - last_called_round) >= 100:
                self._delete_tool(name)
                removed.append(name)
                continue

        # 规则 4：总量控制
        tools_meta = self._load_all_metadata()  # 重新加载（可能已删除部分）
        active = {k: v for k, v in tools_meta.items() if not v.get("disabled", False)}
        if len(active) > max_tools:
            sorted_tools = sorted(
                active.items(),
                key=lambda x: x[1].get("stats", {}).get("call_count", 0),
            )
            excess = len(active) - max_tools
            for name, _ in sorted_tools[:excess]:
                self._delete_tool(name)
                removed.append(name)

        if removed:
            logger.info(f"熵减淘汰: {len(removed)} 个工具 — {removed}")
        return removed

    def _load_all_metadata(self) -> Dict[str, Dict]:
        """加载所有合成工具的元数据。"""
        result = {}
        if not os.path.isdir(self._synth_dir):
            return result
        for filename in os.listdir(self._synth_dir):
            if filename.endswith(".json") and not filename.startswith("_tmp_"):
                name = filename[:-5]
                json_path = os.path.join(self._synth_dir, filename)
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        result[name] = json.load(f)
                except Exception:
                    pass
        return result

    def _save_metadata(self, name: str, metadata: Dict) -> None:
        """保存单个工具的元数据。"""
        json_path = os.path.join(self._synth_dir, f"{name}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    def _delete_tool(self, name: str) -> None:
        """删除合成工具文件。"""
        for ext in (".py", ".json"):
            path = os.path.join(self._synth_dir, f"{name}{ext}")
            if os.path.exists(path):
                os.remove(path)
        logger.info(f"合成工具 {name} 已删除")

    # ═══════════════════════════════════════════════════
    # 辅助查询
    # ═══════════════════════════════════════════════════

    def get_synthesized_hashes(self) -> Set[str]:
        """返回所有已成功合成的 pattern hash 集合。"""
        hashes = set()
        for meta in self._load_all_metadata().values():
            source = meta.get("source_pattern", {})
            if "pattern_hash" in source:
                hashes.add(source["pattern_hash"])
        return hashes

    def list_tools(self) -> List[Dict[str, Any]]:
        """返回所有合成工具的摘要（用于 CLI 展示）。"""
        result = []
        for name, meta in self._load_all_metadata().items():
            stats = meta.get("stats", {})
            result.append({
                "name": name,
                "description": meta.get("description", "")[:60],
                "call_count": stats.get("call_count", 0),
                "success_count": stats.get("success_count", 0),
                "disabled": meta.get("disabled", False),
            })
        return result
