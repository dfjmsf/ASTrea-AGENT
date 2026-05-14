"""
_code_utils — create_code / edit_code 共用的确定性工具函数

包含：
- L0 检查（语法 + 骨架残留）
- AST 骨架生成（错误恢复用）
- 导出摘要提取
- 行号注入
"""
import ast
import os
import py_compile
import tempfile
import logging
from typing import List, Tuple

logger = logging.getLogger("CodeUtils")


# ════════════════════════════════════════
# HTML 标签闭合检测（零依赖）
# ════════════════════════════════════════

# 自闭合标签（无需 </xxx>）
_VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


def _check_html_tags(content: str) -> str:
    """检测 HTML/Vue/JSX 中的标签闭合问题。

    返回: 空字符串（通过）或错误描述。
    仅检测常规 HTML 标签配对，不解析 JS 表达式或模板语法。
    """
    from html.parser import HTMLParser

    class _TagChecker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []  # [(tag, line)]
            self.errors = []

        def handle_starttag(self, tag, attrs):
            tag_lower = tag.lower()
            if tag_lower not in _VOID_ELEMENTS:
                self.stack.append((tag_lower, self.getpos()[0]))

        def handle_endtag(self, tag):
            tag_lower = tag.lower()
            if tag_lower in _VOID_ELEMENTS:
                return
            # 查找最近匹配的开标签
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag_lower:
                    self.stack.pop(i)
                    return
            self.errors.append(f"多余闭合标签 </{tag}> (第 {self.getpos()[0]} 行)")

    checker = _TagChecker()
    try:
        checker.feed(content)
    except Exception:
        return ""  # 解析器异常时不阻塞（容错）

    # 检查未闭合的标签
    if checker.errors:
        return "; ".join(checker.errors[:3])

    unclosed = [(t, ln) for t, ln in checker.stack if t not in ("html", "head", "body")]
    if len(unclosed) > 3:
        samples = ", ".join(f"<{t}> (第 {ln} 行)" for t, ln in unclosed[:3])
        return f"共 {len(unclosed)} 个标签未闭合: {samples} ..."
    elif unclosed:
        return "; ".join(f"<{t}> (第 {ln} 行) 未闭合" for t, ln in unclosed)

    return ""


# ════════════════════════════════════════
# L0 确定性检查（零 LLM）
# ════════════════════════════════════════

def run_l0_checks(target_file: str, code_content: str) -> Tuple[bool, str]:
    """L0 轻量确定性检查。

    检查项：
    - L0.0: 骨架残留检测（... 占位符）
    - L0.1: 语法检查（ast.parse + py_compile）

    返回: (通过, 错误信息)
    """
    ext = os.path.splitext(target_file)[1].lower()

    # 非 Python 文件
    if ext != ".py":
        if not code_content or not code_content.strip():
            return False, f"[L0.1] {target_file} 内容为空"

        # 前端文件：标签闭合检测
        if ext in (".html", ".htm", ".vue", ".jsx", ".tsx", ".svg"):
            unclosed = _check_html_tags(code_content)
            if unclosed:
                return False, (
                    f"[L0.2 标签未闭合] {target_file}: {unclosed}\n"
                    f"请检查是否遗漏了闭合标签。"
                )

        # JSON 合法性检查
        if ext == ".json":
            import json as _json
            try:
                _json.loads(code_content)
            except _json.JSONDecodeError as e:
                return False, f"[L0.1 JSON 语法错误] {target_file}: {e}"

        return True, ""

    # L0.0: 骨架残留检测
    try:
        tree = ast.parse(code_content)
        stub_funcs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                # 跳过 docstring
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]
                if len(body) == 1:
                    stmt = body[0]
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                        if stmt.value.value is ...:
                            stub_funcs.append(node.name)
        if stub_funcs:
            return False, (
                f"[L0.0 骨架残留] {target_file}: 以下函数仍是 `...` 占位未实现: "
                f"{', '.join(stub_funcs)}。请将所有 `...` 替换为完整的业务实现代码。"
            )
    except SyntaxError:
        pass  # 让 L0.1 捕获

    # L0.1: 语法检查
    try:
        ast.parse(code_content)
    except SyntaxError as e:
        return False, f"[L0.1 语法错误] {target_file} 第 {e.lineno} 行: {e.msg}"

    # L0.1b: py_compile 二次验证
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(code_content)
            tmp_path = tmp.name
        py_compile.compile(tmp_path, doraise=True)
        os.unlink(tmp_path)
    except py_compile.PyCompileError as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False, f"[L0.1 编译错误] {target_file}: {e}"

    return True, ""


# ════════════════════════════════════════
# AST 骨架生成（D7 错误恢复）
# ════════════════════════════════════════

def generate_ast_skeleton(code_content: str) -> str:
    """从代码生成带行号的 AST 骨架（函数/类签名列表）。

    用于 L0 失败时返回给 Master，帮助精确定位问题行号。

    示例输出：
      1| import os
      5| class TaxCalculator:
      6|     def __init__(self, rate: float)    # L6-L8
     10|     def calculate(self, amount) -> float  # L10-L15  ← 语法错误
     17| def main():  # L17-L25
    """
    lines = code_content.split("\n")
    width = len(str(max(1, len(lines))))
    skeleton_parts: List[str] = []

    try:
        tree = ast.parse(code_content)
    except SyntaxError:
        # 语法错误时无法解析 AST，返回原始带行号内容的前 30 行
        return "\n".join(
            f"{i:>{width}}| {line}"
            for i, line in enumerate(lines[:30], 1)
        ) + "\n... (语法错误，无法解析完整 AST)"

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            skeleton_parts.append(
                f"{node.lineno:>{width}}| import {', '.join(a.name for a in node.names)}"
            )
        elif isinstance(node, ast.ImportFrom):
            skeleton_parts.append(
                f"{node.lineno:>{width}}| from {node.module or '.'} import ..."
            )
        elif isinstance(node, ast.ClassDef):
            end_line = node.end_lineno or node.lineno
            skeleton_parts.append(
                f"{node.lineno:>{width}}| class {node.name}:  # L{node.lineno}-L{end_line}"
            )
            # 类内方法
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item_end = item.end_lineno or item.lineno
                    # 提取参数签名
                    args_str = ", ".join(a.arg for a in item.args.args)
                    prefix = "async " if isinstance(item, ast.AsyncFunctionDef) else ""
                    skeleton_parts.append(
                        f"{item.lineno:>{width}}|     {prefix}def {item.name}({args_str})  "
                        f"# L{item.lineno}-L{item_end}"
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = node.end_lineno or node.lineno
            args_str = ", ".join(a.arg for a in node.args.args)
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            skeleton_parts.append(
                f"{node.lineno:>{width}}| {prefix}def {node.name}({args_str})  "
                f"# L{node.lineno}-L{end_line}"
            )

    return "\n".join(skeleton_parts) if skeleton_parts else "(空文件)"


# ════════════════════════════════════════
# 导出摘要
# ════════════════════════════════════════

def extract_exports_summary(code_content: str, target_file: str) -> str:
    """从代码中提取关键导出摘要（函数/类/路由数量）。"""
    import re

    funcs = re.findall(r"^(?:def|async def)\s+(\w+)", code_content, re.MULTILINE)
    classes = re.findall(r"^class\s+(\w+)", code_content, re.MULTILINE)
    routes = re.findall(r"@\w+\.(?:route|get|post|put|delete)", code_content)

    parts = []
    if funcs:
        parts.append(f"{len(funcs)} 个函数({', '.join(funcs[:5])}{'...' if len(funcs) > 5 else ''})")
    if classes:
        parts.append(f"{len(classes)} 个类({', '.join(classes[:3])})")
    if routes:
        parts.append(f"{len(routes)} 个路由")

    return ", ".join(parts) if parts else f"{len(code_content)} 字符"
