"""API 集成测试：TestClient 全链路（SVG 真实转换、GIF mock、鉴权、SSRF、路径穿越）。"""

import io
import json
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import AppConfig, ProviderConfig
from app.main import create_app


def _png_bytes(w=32, h=32) -> bytes:
    img = Image.new("RGB", (w, h), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def app_no_provider(tmp_path, monkeypatch):
    """无 AI provider 的应用（SVG convert 可用）。"""
    cfg = AppConfig()
    cfg.outputs_dir = tmp_path / "outputs"
    app = create_app(cfg)
    app.state.config.outputs_dir = cfg.outputs_dir
    yield app


@pytest.fixture
def client_no_provider(app_no_provider):
    return TestClient(app_no_provider)


def test_health(client_no_provider):
    r = client_no_provider.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["image_provider_ready"] is False  # 未配置 provider


def test_svg_convert_multipart(client_no_provider):
    r = client_no_provider.post(
        "/api/v1/svg",
        files={"file": ("test.png", _png_bytes(), "image/png")},
        data={"payload": json.dumps({"mode": "convert"})},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["svg"].lstrip().startswith("<?xml")
    assert "<svg" in body["svg"]
    assert body["meta"]["paths"] >= 1


def test_svg_convert_base64(client_no_provider):
    import base64

    r = client_no_provider.post(
        "/api/v1/svg",
        json={"mode": "convert", "image_base64": base64.b64encode(_png_bytes()).decode()},
    )
    assert r.status_code == 200
    assert "<svg" in r.json()["svg"]


def test_svg_convert_rejects_garbage(client_no_provider):
    r = client_no_provider.post(
        "/api/v1/svg",
        files={"file": ("evil.png", b"not an image at all", "image/png")},
        data={"payload": json.dumps({"mode": "convert"})},
    )
    assert r.status_code == 400


def test_svg_rejects_two_channels(client_no_provider):
    import base64

    r = client_no_provider.post(
        "/api/v1/svg",
        json={
            "mode": "convert",
            "image_base64": base64.b64encode(_png_bytes()).decode(),
            "image_url": "https://example.com/x.png",
        },
    )
    assert r.status_code == 400


def test_svg_url_ssrf_private_blocked(client_no_provider):
    # localhost 应被拒绝（SSRF 防护）；公网但不存在域名的错误也应为 400 可读
    r = client_no_provider.post(
        "/api/v1/svg", json={"mode": "convert", "image_url": "http://127.0.0.1/x.png"}
    )
    assert r.status_code == 400
    assert "拒绝" in r.json()["detail"] or "环回" in r.json()["detail"]


def test_svg_oversize_rejected(client_no_provider):
    r = client_no_provider.post(
        "/api/v1/svg",
        files={"file": ("big.png", b"x" * (21 * 1024 * 1024), "image/png")},
        data={"payload": json.dumps({"mode": "convert"})},
    )
    assert r.status_code == 413


def test_gif_requires_provider(client_no_provider):
    r = client_no_provider.post("/api/v1/gif", json={"prompt": "cat"})
    assert r.status_code == 503


# ----------------------------------------------------------------------
# 带 provider 的应用：mock 掉 LLM 客户端（不花真钱）
# ----------------------------------------------------------------------


class _MockClient:
    def __init__(self) -> None:
        self._seq = 0

    def _next_png(self) -> bytes:
        """每次返回颜色渐变的不同帧（模拟真实生成：帧帧不同）。"""
        self._seq += 1
        img = Image.new("RGB", (32, 32), (self._seq * 30 % 255, 60, 160))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def chat_vision(self, prompt, images_data_url=None, system=None):
        from app.llm import LLMResult

        if "制定剧本" in prompt:
            content = json.dumps(
                {
                    "first_frame_prompt": "a cat",
                    "frame_changes": ["wave"],
                    "loop_closure": "back",
                    "style_anchor": "cartoon",
                }
            )
        elif "连贯性检查" in prompt:
            content = json.dumps({"closed": True, "transition_prompt": "", "reason": "ok"})
        else:
            content = json.dumps(
                {"action": "continue", "next_frame_prompt": "wave", "reason": "ok", "consistency": 0.9}
            )
        return LLMResult(content=content, image_png=None, key_id="k", provider="mock")

    async def chat_vision_stream(self, prompt, images_data_url=None, system=None, on_delta=None):
        result = await self.chat_vision(prompt, images_data_url, system)
        if on_delta is not None and result.content:
            await on_delta(result.content[: len(result.content) // 2])
            await on_delta(result.content[len(result.content) // 2 :])
        return result

    async def generate_image(self, prompt, size):
        from app.llm import LLMResult

        return LLMResult(content="", image_png=self._next_png(), key_id="k", provider="mock")

    async def edit_image(self, image_png, prompt, size):
        from app.llm import LLMResult

        return LLMResult(content="", image_png=self._next_png(), key_id="k", provider="mock")


@pytest.fixture
def app_with_mock(tmp_path, monkeypatch):
    cfg = AppConfig(
        providers=[
            ProviderConfig(
                name="mock",
                base_url="https://mock/v1",
                api_keys=["sk-test-1", "sk-test-2"],
                image_model="i",
                planner_model="p",
            )
        ]
    )
    cfg.outputs_dir = tmp_path / "outputs"
    app = create_app(cfg)
    app.state.config.outputs_dir = cfg.outputs_dir

    mock = _MockClient()
    monkeypatch.setattr(type(app.state.registry), "image_client", lambda self: mock)
    monkeypatch.setattr(type(app.state.registry), "planner_client", lambda self: mock)
    yield app


def test_gif_full_flow_with_mock(app_with_mock):
    with TestClient(app_with_mock) as c:
        r = c.post("/api/v1/gif", json={"prompt": "a waving cat", "frames": 3, "size": "512x512"})
        assert r.status_code == 202, r.text
        task_id = r.json()["task_id"]

        # 轮询直到终态（mock 下应秒级完成）
        import time

        for _ in range(50):
            detail = c.get(f"/api/v1/tasks/{task_id}").json()
            if detail["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.1)
        assert detail["status"] == "done", detail.get("error")
        assert detail["result"]["frames_generated"] == 3
        assert detail["result"]["loop_closed"] is True

        # 产物可下载
        gif_url = detail["result"]["gif"]
        resp = c.get(gif_url)
        assert resp.status_code == 200
        img = Image.open(io.BytesIO(resp.content))
        assert img.format == "GIF"
        assert getattr(img, "n_frames", 1) == 3


def test_gif_validation_errors(app_with_mock):
    with TestClient(app_with_mock) as c:
        assert c.post("/api/v1/gif", json={"frames": 3}).status_code == 400  # 缺 prompt
        assert c.post("/api/v1/gif", json={"prompt": "x", "frames": 99}).status_code == 400
        assert c.post("/api/v1/gif", json={"prompt": "x", "size": "100x100"}).status_code == 400


def test_gif_task_not_found(app_with_mock):
    with TestClient(app_with_mock) as c:
        assert c.get("/api/v1/tasks/nonexistent").status_code == 404


def test_file_download_traversal_blocked(app_with_mock):
    with TestClient(app_with_mock) as c:
        # 先跑一个任务拿合法 task_id，再用非法文件名尝试穿越
        r = c.post("/api/v1/gif", json={"prompt": "cat", "frames": 2})
        tid = r.json()["task_id"]
        bad = urllib.parse.quote("../../config.yaml")
        assert c.get(f"/api/v1/files/{tid}/{bad}").status_code in (400, 404)


def test_auth_enforced(tmp_path):
    cfg = AppConfig(
        server=__import__("app.config", fromlist=["ServerConfig"]).ServerConfig(api_token="secret-token")
    )
    cfg.outputs_dir = tmp_path / "outputs"
    app = create_app(cfg)
    app.state.config.outputs_dir = cfg.outputs_dir
    with TestClient(app) as c:
        # 无 token → 401
        assert c.get("/api/v1/config").status_code == 401
        # 错 token → 401
        assert c.get("/api/v1/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # 对 token → 200
        assert c.get("/api/v1/config", headers={"Authorization": "Bearer secret-token"}).status_code == 200
        # health 不需要鉴权
        assert c.get("/api/health").status_code == 200


def test_svg_generate_mode_uses_llm(app_with_mock):
    with TestClient(app_with_mock) as c:
        r = c.post("/api/v1/svg", json={"mode": "generate", "prompt": "a red ball"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["meta"]["mode"] == "generate"
        assert "<svg" in body["svg"]


def test_config_view_masks_keys(app_with_mock):
    with TestClient(app_with_mock) as c:
        body = c.get("/api/v1/config").json()
        assert "sk-test-1" not in str(body)
        assert any("..." in k for p in body["providers"] for k in p["api_keys"])


def test_keys_endpoint(app_with_mock):
    with TestClient(app_with_mock) as c:
        body = c.get("/api/v1/keys").json()
        assert len(body["pools"][0]["keys"]) == 2


def test_presets_endpoint(app_with_mock):
    with TestClient(app_with_mock) as c:
        body = c.get("/api/v1/presets").json()
        ids = [p["id"] for p in body["presets"]]
        assert "agnes" in ids
        assert "dashscope" in ids
        assert "volcengine" in ids
        agnes = next(p for p in body["presets"] if p["id"] == "agnes")
        assert agnes["edit_style"] == "generations_extra_body"
        assert agnes["base_url"].endswith("/v1")


def test_keys_reset_endpoint(app_with_mock):
    with TestClient(app_with_mock) as c:
        r = c.post("/api/v1/keys/reset")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["reset_keys"] == 2
