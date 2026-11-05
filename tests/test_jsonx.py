"""jsonx 五级容错解析测试（含非法 JSON 回归用例）。"""

from app.jsonx import parse_json_robust


def test_level1_direct_parse():
    assert parse_json_robust('{"a": 1}') == {"a": 1}
    assert parse_json_robust("[1, 2, 3]") == [1, 2, 3]


def test_level2_strip_fences():
    text = '```json\n{"a": 1}\n```'
    assert parse_json_robust(text) == {"a": 1}
    text2 = '前置闲聊```{"b": 2}```后置闲聊'
    assert parse_json_robust(text2) == {"b": 2}


def test_level3_balanced_extract():
    text = '好的，这是结果：{"a": {"b": [1, 2]}, "c": "x}y"} 以上。'
    parsed = parse_json_robust(text)
    assert parsed == {"a": {"b": [1, 2]}, "c": "x}y"}


def test_level3_truncated_text():
    # 截断的 JSON：配平提取不到完整片段 → 正则字段级
    text = '结果如下 {"action": "continue", "next_frame_prompt": "a cat moves", "consistency": 0.9'
    parsed = parse_json_robust(text)
    assert parsed is not None
    assert parsed["action"] == "continue"
    assert parsed["consistency"] == 0.9


def test_level4_trailing_comma():
    text = '{"a": 1, "b": 2,}'
    assert parse_json_robust(text) == {"a": 1, "b": 2}


def test_level4_python_literal():
    text = "{'a': 1, 'b': True}"
    assert parse_json_robust(text) == {"a": 1, "b": True}


def test_level5_regex_fields():
    text = '抱歉格式出错了 action: "stop" reason: 已完成 {"action": "stop", "consistency": 0.95, "reason": "动画已完整"}'
    parsed = parse_json_robust(text)
    assert parsed is not None
    assert parsed.get("action") == "stop"
    assert parsed.get("consistency") == 0.95


def test_garbage_returns_none():
    assert parse_json_robust("完全不是 JSON 的文本") is None
    assert parse_json_robust("") is None
    assert parse_json_robust(None) is None  # type: ignore[arg-type]


def test_nested_braces_in_strings():
    # 字符串内的花括号不应干扰配平
    text = '{"prompt": "draw {curly} braces", "n": 1}'
    assert parse_json_robust(text) == {"prompt": "draw {curly} braces", "n": 1}
