"""配置加载测试：合法/非法/缺失配置与脱敏视图。"""

import pytest

from app.config import ConfigError, load_config


def _write(tmp_path, content: str):
    f = tmp_path / "config.yaml"
    f.write_text(content, encoding="utf-8")
    return str(f)


VALID = """
server:
  host: 127.0.0.1
  port: 9000
providers:
  - name: test
    base_url: https://api.test.com/v1/
    api_keys: ["sk-a", "sk-b"]
    image_model: img-model
    planner_model: planner-model
gif:
  default_frames: 4
"""


def test_valid_config(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    assert cfg.server.port == 9000
    assert cfg.providers[0].base_url == "https://api.test.com/v1"  # 去尾斜杠
    assert cfg.gif.default_frames == 4
    assert cfg.find_image_provider().name == "test"
    assert cfg.find_planner_provider().name == "test"
    assert cfg.providers[0].edit_style == "edits_multipart"  # 默认风格


def test_base_url_normalization_strips_endpoint_suffix(tmp_path):
    """误填完整端点路径时自动剥到 /v1。"""
    cfg = load_config(
        _write(
            tmp_path,
            VALID.replace(
                "base_url: https://api.test.com/v1/",
                "base_url: https://api.test.com/v1/chat/completions",
            ),
        )
    )
    assert cfg.providers[0].base_url == "https://api.test.com/v1"


def test_edit_style_valid_and_invalid(tmp_path):
    with_edit = VALID.replace(
        "    planner_model: planner-model\n",
        "    planner_model: planner-model\n    edit_style: generations_extra_body\n",
    )
    cfg = load_config(_write(tmp_path, with_edit))
    assert cfg.providers[0].edit_style == "generations_extra_body"

    with_bad = VALID.replace(
        "    planner_model: planner-model\n",
        "    planner_model: planner-model\n    edit_style: wrong\n",
    )
    with pytest.raises(ConfigError, match="edit_style"):
        load_config(_write(tmp_path, with_bad))


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg.providers == []
    assert cfg.server.port == 8266


def test_provider_without_keys_rejected(tmp_path):
    bad = """
providers:
  - name: t
    base_url: https://x.com/v1
    api_keys: []
"""
    with pytest.raises(ConfigError, match="api_keys"):
        load_config(_write(tmp_path, bad))


def test_missing_image_model_rejected_when_providers_exist(tmp_path):
    bad = """
providers:
  - name: t
    base_url: https://x.com/v1
    api_keys: ["k"]
    image_model: ""
    planner_model: p
"""
    with pytest.raises(ConfigError, match="image_model"):
        load_config(_write(tmp_path, bad))


def test_missing_planner_model_rejected(tmp_path):
    bad = """
providers:
  - name: t
    base_url: https://x.com/v1
    api_keys: ["k"]
    image_model: i
    planner_model: ""
"""
    with pytest.raises(ConfigError, match="planner_model"):
        load_config(_write(tmp_path, bad))


def test_bad_yaml_rejected(tmp_path):
    with pytest.raises(ConfigError, match="YAML"):
        load_config(_write(tmp_path, "{{{not yaml"))


def test_port_out_of_range(tmp_path):
    bad = "server:\n  port: 99999\n"
    with pytest.raises(ConfigError, match="port"):
        load_config(_write(tmp_path, bad))


def test_masked_view_never_leaks_keys(tmp_path):
    cfg = load_config(_write(tmp_path, VALID))
    view = cfg.masked_view()
    assert "sk-a" not in str(view)
    assert "sk-b" not in str(view)
    # 短 Key 全星号掩码，长 Key 前后 4 位掩码；掩码值绝不等于明文
    masked = view["providers"][0]["api_keys"]
    assert all(m != "sk-a" and m != "sk-b" for m in masked)
