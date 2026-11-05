"""AI 返回 JSON 的五级容错解析。

LLM 输出的 JSON 常见污染：```json 围栏、前后闲聊、截断、单引号、尾逗号。
解析链（逐级降级，永不抛出，最终返回 None 由调用方走默认值）：
  1. 直接 json.loads
  2. 剥代码块围栏（```...```）后重试
  3. 大括号/中括号配平截取首个完整 JSON 片段
  4. 常见语法修补（单引号→双引号、去尾逗号、python 字面量）后重试
  5. 正则逐字段提取（只对 dict 目标生效）
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """剥掉 Markdown 代码块围栏，返回围栏内内容（无围栏则原样）。"""
    m = _FENCE_RE.search(text)
    return m.group(1) if m else text


def _balanced_extract(text: str) -> str | None:
    """从文本中配平截取首个完整的 {...} 或 [...] 片段。"""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _repair_common(text: str) -> str:
    """修补常见 JSON 语法问题：尾逗号；失败后尝试 Python 字面量风格。"""
    # 去掉 ] 或 } 前的尾逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    return repaired


def _python_literal(text: str) -> Any:
    """按 Python 字面量解析（处理单引号/True/False/None 风格输出）。"""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _regex_fields(text: str) -> dict[str, Any] | None:
    """最后一级：正则逐字段提取顶层 "key": value 对（尽力而为）。"""
    result: dict[str, Any] = {}
    # 只匹配简单的标量值与短字符串，复杂嵌套放弃
    pattern = re.compile(
        r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*'
        r'(?P<val>"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)',
        re.DOTALL,
    )
    found_any = False
    for m in pattern.finditer(text):
        found_any = True
        raw = m.group("val")
        if raw.startswith('"'):
            try:
                result[m.group("key")] = json.loads(raw)
            except json.JSONDecodeError:
                result[m.group("key")] = raw.strip('"')
        elif raw in ("true", "false"):
            result[m.group("key")] = raw == "true"
        elif raw == "null":
            result[m.group("key")] = None
        else:
            try:
                v = float(raw)
                result[m.group("key")] = int(v) if v.is_integer() else v
            except ValueError:
                result[m.group("key")] = raw
    return result if found_any else None


def parse_json_robust(text: str) -> Any:
    """五级容错解析入口。返回解析结果；彻底失败返回 None。"""
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    # 1. 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 剥围栏后重试
    stripped = _strip_fences(text).strip()
    if stripped != text:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # 3. 配平截取（对原文与剥围栏文本都尝试）
    for candidate in (stripped, text):
        frag = _balanced_extract(candidate)
        if frag:
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                # 4. 修补后再试
                try:
                    return json.loads(_repair_common(frag))
                except json.JSONDecodeError:
                    py = _python_literal(frag)
                    if isinstance(py, (dict, list, int, float, str, bool)) and py is not None:
                        return py

    # 5. 正则逐字段
    return _regex_fields(stripped or text)
