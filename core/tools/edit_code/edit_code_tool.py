"""
edit_code 工具 — Master 指定行号范围 + 替换代码块，工具做确定性替换 + L0 验证

核心特性：
- 动态密钥机制（硬校验 + 软密钥）
- 批量编辑逆序应用（防行号漂移）
- L0 失败时返回 AST 骨架（错误恢复）
- 零 LLM 调用
"""
import logging
import os
import re
from typing import Any, Dict, List, Tuple

from core.tools import ToolDefinition
from core.tools._code_utils import run_l0_checks, extract_exports_summary, generate_ast_skeleton

logger = logging.getLogger("Tool.EditCode")


# ════════════════════════════════════════
# 核心：行号解析 + 确定性替换
# ════════════════════════════════════════

def _parse_range(range_str: str) -> Tuple[int, int]:
    """解析行号范围字符串（如 'L20-L25' 或 '20-25'）。

    返回: (start_line, end_line)，1-indexed。
    """
    # 支持格式: "L20-L25", "L20-25", "20-25"
    cleaned = range_str.strip().upper().replace("L", "")
    match = re.match(r"(\d+)\s*-\s*(\d+)", cleaned)
    if not match:
        raise ValueError(f"行号范围格式错误: '{range_str}'，期望 'L20-L25' 或 '20-25'")
    start = int(match.group(1))
    end = int(match.group(2))
    if start > end:
        raise ValueError(f"起始行 {start} 不能大于结束行 {end}")
    if start < 1:
        raise ValueError(f"行号必须 >= 1，但收到 {start}")
    return start, end


def _format_actual_content(lines: List[str], start: int, end: int) -> str:
    """格式化行号范围内的实际内容（用于锚定失败时返回）。"""
    width = len(str(end))
    selected = lines[start - 1 : end]
    return "\n".join(
        f"{i:>{width}}| {line}"
        for i, line in enumerate(selected, start)
    )


def _apply_edits(
    lines: List[str],
    edits: List[Dict[str, Any]],
    total_lines: int,
) -> Tuple[List[str], List[str]]:
    """批量应用编辑，从底部向上（防行号漂移）。

    Master（LLM）输出的代码缩进视为正确，工具不做二次修正。
    支持 anchor 锚定校验：验证指定行号范围内是否包含预期内容。

    参数:
        lines: 原始文件行列表
        edits: [{"range": "L20-L25", "code": "...", "anchor": "..."}, ...]
        total_lines: 文件总行数

    返回: (修改后的行列表, 错误列表)
    """
    errors = []

    # 解析并排序（按起始行降序 → 从底部向上）
    parsed_edits = []
    for i, edit in enumerate(edits):
        try:
            start, end = _parse_range(edit["range"])
            # 硬密钥校验：行号范围必须在文件行数内
            if end > total_lines:
                errors.append(
                    f"编辑 #{i+1}: 行号越界 — 声称 L{end}，但文件仅有 {total_lines} 行"
                )
                continue

            # ═══ 锚定校验 ═══
            anchor = edit.get("anchor", "").strip()
            if anchor:
                region_text = "\n".join(lines[start - 1 : end])
                if anchor not in region_text:
                    actual = _format_actual_content(lines, start, end)
                    errors.append(
                        f"编辑 #{i+1}: 锚定失败 — L{start}-L{end} 中不包含 '{anchor[:80]}'"
                        f"\n实际内容:\n{actual}"
                    )
                    continue

            code = edit.get("code", "")
            parsed_edits.append((start, end, code, i))
        except (ValueError, KeyError) as e:
            errors.append(f"编辑 #{i+1}: {e}")

    if errors:
        return lines, errors

    # 按起始行降序排列（从底部向上）
    parsed_edits.sort(key=lambda x: x[0], reverse=True)

    # 重叠检查（降序后相邻比较：上方编辑的 end >= 下方编辑的 start → 重叠）
    for j in range(len(parsed_edits) - 1):
        start_lower, _, _, idx_lower = parsed_edits[j]      # 更靠下的编辑
        _, end_upper, _, idx_upper = parsed_edits[j + 1]     # 更靠上的编辑
        if end_upper >= start_lower:
            errors.append(
                f"编辑 #{idx_upper+1} 和 #{idx_lower+1} 存在行号重叠"
            )
            return lines, errors

    # 逆序应用（直接替换，不做缩进修正 — Master 输出的缩进视为正确）
    result = list(lines)
    for start, end, code, _ in parsed_edits:
        new_code_lines = code.split("\n") if code else []
        # 行号替换（1-indexed → 0-indexed 切片）
        result[start - 1 : end] = new_code_lines

    return result, errors


# ════════════════════════════════════════
# 工具主函数
# ════════════════════════════════════════

def edit_code(
    target_file: str,
    edits: Any,
    description: str = "",
    expected_behavior: str = "",
    _project_dir: str = "",
) -> Dict[str, Any]:
    """编辑已有代码文件。

    参数:
        target_file: 目标文件路径（相对于项目根目录）
        edits: 编辑指令列表，格式：[{"range": "L20-L25", "code": "新代码"}, ...]
        description: 修改内容描述（软密钥）
        expected_behavior: 修改后的预期行为（软密钥）
        _project_dir: 项目目录（Master 自动注入）
    返回:
        成功: {"status": "ok", "new_line_count": N, "summary": "..."}
        失败: {"status": "fail", "feedback": "...", "skeleton": "AST 骨架"}
    """
    # ═══ 软密钥强制填写检查 ═══
    if not description:
        return {"status": "fail", "feedback": "description（修改内容描述）是必填项"}

    # ═══ 硬密钥校验：文件存在性 ═══
    if _project_dir:
        abs_path = os.path.join(_project_dir, target_file)
    else:
        abs_path = os.path.abspath(target_file)

    if not os.path.isfile(abs_path):
        return {"status": "fail", "feedback": f"文件不存在: {target_file}"}

    # ═══ 解析 edits 参数 ═══
    if isinstance(edits, str):
        import json
        try:
            edits = json.loads(edits)
        except json.JSONDecodeError as e:
            return {"status": "fail", "feedback": f"edits 参数 JSON 解析失败: {e}"}

    if not isinstance(edits, list) or not edits:
        return {"status": "fail", "feedback": "edits 必须是非空数组"}

    # ═══ 读取当前文件 ═══
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return {"status": "fail", "feedback": f"读取文件失败: {e}"}

    lines = content.split("\n")
    total_lines = len(lines)

    # ═══ 批量编辑（逆序应用） ═══
    new_lines, edit_errors = _apply_edits(lines, edits, total_lines)
    if edit_errors:
        return {
            "status": "fail",
            "feedback": "编辑参数错误:\n" + "\n".join(edit_errors),
            "actual_line_count": total_lines,
        }

    # ═══ 组合新内容 ═══
    new_content = "\n".join(new_lines)

    # ═══ L0 检查 ═══
    l0_pass, l0_feedback = run_l0_checks(target_file, new_content)

    if l0_pass:
        # 写入磁盘
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return {"status": "fail", "feedback": f"写入磁盘失败: {e}"}

        new_line_count = len(new_lines)
        lines_removed = total_lines - new_line_count
        summary = extract_exports_summary(new_content, target_file)
        logger.info(f"✅ edit_code 成功: {target_file} ({total_lines} → {new_line_count} 行)")

        # ═══ 编辑异常检测：内容大幅缩减时警告 ═══
        # 条件：行数减少 >20 行 且 减少比例 >40%（小文件 <50 行豁免比例检查）
        content_loss_ratio = lines_removed / max(1, total_lines)
        is_large_file = total_lines >= 50
        if lines_removed > 20 and (not is_large_file or content_loss_ratio > 0.4):
            logger.warning(
                f"⚠️ 编辑异常检测: {target_file} 行数从 {total_lines} → {new_line_count}"
                f" (减少 {lines_removed} 行, {content_loss_ratio:.0%})"
            )
            return {
                "status": "warning",
                "warning": (
                    f"编辑导致文件大幅缩减: {total_lines} → {new_line_count} 行"
                    f" (减少 {lines_removed} 行, {content_loss_ratio:.0%})。"
                    f"文件已写入磁盘，但建议调用 read_file 检查内容是否符合预期。"
                ),
                "new_line_count": new_line_count,
                "summary": summary,
                "target_file": target_file,
            }

        # ═══ 提取上下文并返回 ═══
        context_str = ""
        try:
            if isinstance(edits, list) and edits:
                first_edit = edits[0]
                import re
                m = re.match(r"^L(\d+)", first_edit.get("range", "").strip())
                if m:
                    orig_start = int(m.group(1))
                    code_len = len(first_edit.get("code", "").split("\n"))
                    s = max(1, orig_start - 10)
                    e = min(new_line_count, orig_start + code_len + 10)
                    from core.tools.read_file import _inject_line_numbers
                    ctx_lines = new_lines[s-1 : e]
                    context_str = f"L{s}-L{e}:\n" + _inject_line_numbers(ctx_lines, s)
        except Exception:
            pass

        result_dict = {
            "status": "ok",
            "new_line_count": new_line_count,
            "summary": summary,
            "target_file": target_file,
        }
        if context_str:
            result_dict["context"] = f"<edit_context>\n{context_str}\n</edit_context>"

        return result_dict

    # ═══ L0 失败 → 返回 AST 骨架（D7 错误恢复） ═══
    # 不写入磁盘（保留原文件不变）
    skeleton = generate_ast_skeleton(new_content)
    logger.warning(f"⚠️ edit_code L0 失败: {l0_feedback[:200]}")
    return {
        "status": "fail",
        "feedback": l0_feedback,
        "skeleton": skeleton,
        "target_file": target_file,
    }


TOOL_DEF = ToolDefinition(
    name="edit_code",
    description=(
        "编辑已有代码文件。传入行号范围、锚定内容和替换代码块，工具做确定性替换 + L0 验证。"
        "anchor 用于防止行号偏移导致误改：工具会验证指定范围内是否包含 anchor 文本，不匹配则拒绝执行并返回实际内容。"
        "支持一次多处编辑（从底部向上应用，防止行号漂移）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_file": {
                "type": "string",
                "description": "目标文件路径（相对于项目根目录）",
            },
            "edits": {
                "type": "array",
                "description": (
                    "编辑指令数组。每项包含 range（行号范围）、anchor（被替换区域的原文片段，用于校验行号是否正确）和 code（替换代码）。"
                    "多处编辑时，工具自动从底部向上应用，无需担心行号漂移。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "range": {
                            "type": "string",
                            "description": "行号范围（如 'L20-L25'）",
                        },
                        "anchor": {
                            "type": "string",
                            "description": "被替换区域中的一段原文内容（通常为首行或关键标识行），用于验证行号是否准确",
                        },
                        "code": {
                            "type": "string",
                            "description": "替换代码内容",
                        },
                    },
                    "required": ["range", "anchor", "code"],
                },
            },
            "description": {
                "type": "string",
                "description": "修改内容描述（必填）",
            },
            "expected_behavior": {
                "type": "string",
                "description": "修改后的预期行为",
            },
        },
        "required": ["target_file", "edits", "description"],
    },
    handler=edit_code,
)
