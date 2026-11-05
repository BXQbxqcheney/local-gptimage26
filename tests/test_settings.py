"""settings 端点测试：网页端 API 配置（掩码回显/回填/写盘/热重建）。

安全说明：本文件所有读写的 cfg_path 均来自 pytest tmp_path 夹具
（pytest 保证其位于系统临时目录内），并在夹具内断言父目录归属，
不涉及任何用户可控路径。
"""

import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from app.config import AppConfig, load_config, ProviderConfig  # noqa: F401
from app.main import create_app


@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    """返回 (app, cfg_path)；cfg_path 限定在 tmp_path 内。"""
    cfg_path = tmp_path / "config.yaml"
    assert cfg_path.parent == tmp_path  # 防御断言：路径不可逃逸出临时目录
    cfg_path.write_text(
        "server:\n  port: 8266\nproviders:\n"
        "  - name: old\n"
        "    base_url: https://old.example.com/v1\n"
        '    api_keys: ["sk-real-key-123456"]\n'
        "    image_model: img-m\n"
        "    planner_model: plan-m\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("IMG26_CONFIG", str(cfg_path))
    cfg = load_config(cfg_path)
    cfg.outputs_dir = tmp_path / "outputs"
    app = create_app(cfg)
    app.state.config.outputs_dir = cfg.outputs_dir
    yield app, cfg_path
    monkeypatch.delenv("IMG26_CONFIG", raising=False)


def _read_cfg(cfg_path: Path) -> str:
    """读取临时配置文件（夹具保证其位于 tmp_path）。"""
    assert cfg_path.parent.match("*") and "config.yaml" == cfg_path.name
    return cfg_path.read_text(encoding="utf-8")


def test_settings_get_masks_keys(cfg_env):
    app, _ = cfg_env
    with TestClient(app) as c:
        body = c.get("/api/v1/settings").json()
        assert "sk-real-key-123456" not in str(body)
        assert body["providers"][0]["name"] == "old"
        assert "..." in body["providers"][0]["api_keys"][0]


def test_settings_save_new_provider(cfg_env):
    app, cfg_path = cfg_env
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/settings",
            json={
                "providers": [
                    {
                        "name": "new",
                        "base_url": "https://new.example.com/v1",
                        "api_keys": ["sk-brand-new-999"],
                        "image_model": "i",
                        "planner_model": "p",
                        "image_provider": True,
                        "planner_provider": True,
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert "sk-brand-new-999" not in str(body)  # 返回仍是掩码
        disk = _read_cfg(cfg_path)
        assert "new.example.com" in disk
        assert "sk-brand-new-999" in disk
        # server 段保留
        assert "port: 8266" in disk
        # 热重建生效
        assert c.get("/api/health").json()["image_provider_ready"] is True


def test_settings_masked_key_preserves_stored_value(cfg_env):
    """提交掩码值 → 后端回填已存真实 Key（前端不改 Key 场景）。"""
    app, cfg_path = cfg_env
    with TestClient(app) as c:
        masked = c.get("/api/v1/settings").json()["providers"][0]["api_keys"][0]
        r = c.post(
            "/api/v1/settings",
            json={
                "providers": [
                    {
                        "name": "old",
                        "base_url": "https://old.example.com/v1",
                        "api_keys": [masked],  # 掩码原样提交
                        "image_model": "img-m2",
                        "planner_model": "plan-m",
                    }
                ]
            },
        )
        assert r.status_code == 200
        disk = _read_cfg(cfg_path)
        assert "sk-real-key-123456" in disk  # 真实 Key 保留
        assert "img-m2" in disk  # 模型已更新


def test_settings_rejects_invalid(cfg_env):
    app, _ = cfg_env
    with TestClient(app) as c:
        # 缺 base_url
        r = c.post(
            "/api/v1/settings",
            json={"providers": [{"name": "x", "api_keys": ["k"]}]},
        )
        assert r.status_code == 400
        # 空 providers
        assert c.post("/api/v1/settings", json={"providers": []}).status_code == 400


def test_settings_requires_image_model_role(cfg_env):
    app, _ = cfg_env
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/settings",
            json={
                "providers": [
                    {
                        "name": "only-planner",
                        "base_url": "https://x/v1",
                        "api_keys": ["k"],
                        "image_model": "",
                        "planner_model": "p",
                        "image_provider": True,
                        "planner_provider": True,
                    }
                ]
            },
        )
        assert r.status_code == 400
        assert "image_model" in r.json()["detail"]


def test_settings_auth_enforced(tmp_path, monkeypatch):
    """启用 api_token 时 settings 同样需要鉴权（防经 API 篡改配置）。"""
    from app.config import ServerConfig

    cfg_path = tmp_path / "config.yaml"
    assert cfg_path.parent == tmp_path
    monkeypatch.setenv("IMG26_CONFIG", str(cfg_path))
    cfg = AppConfig(server=ServerConfig(api_token="tok"))
    cfg.outputs_dir = tmp_path / "outputs"
    app = create_app(cfg)
    app.state.config.outputs_dir = cfg.outputs_dir
    with TestClient(app) as c:
        assert c.get("/api/v1/settings").status_code == 401
        ok = c.get("/api/v1/settings", headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 200
    monkeypatch.delenv("IMG26_CONFIG", raising=False)
