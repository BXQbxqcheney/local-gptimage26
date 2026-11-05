# API 文档

基础地址：`http://127.0.0.1:8266`（可用 `run.py --host/--port` 或 `config.yaml` 修改）。
交互式文档：`/api/docs`（OpenAPI/Swagger）。

## 鉴权

`config.yaml` 的 `server.api_token` 非空时，除 `/api/health` 与静态页面外，所有端点要求：

```
Authorization: Bearer <api_token>
```

## GET /api/health

健康检查（无需鉴权）。

```json
{"status": "ok", "version": "1.1.0", "image_provider_ready": true, "planner_provider_ready": true}
```

## GET /api/v1/config

脱敏配置视图：provider 名、base_url、模型名、Key 掩码列表、GIF/SVG 默认参数。

## GET /api/v1/keys

Key 池实时状态：每个 Key 的掩码、占用中/累计使用、是否可用、冷却剩余秒数、连续失败数、故障标记。

## GET /api/v1/settings

网页端配置回显（**Key 一律掩码**）：

```json
{"config_file": "config.yaml",
 "providers": [{"name": "my-provider", "base_url": "https://api.example.com/v1",
                "api_keys": ["sk-a1...wxyz"], "image_model": "gpt-image-1.5",
                "planner_model": "gpt-4o", "image_provider": true, "planner_provider": true}]}
```

## POST /api/v1/settings

保存 providers 配置：校验 → 掩码回填 → 写入 `config.yaml`（保留 server/gif/svg 段；**文件注释会丢失**）→ 热重建客户端与 Key 池（立即生效，无需重启）。

```json
{"providers": [{
  "name": "my-provider",
  "base_url": "https://api.example.com/v1",
  "api_keys": ["sk-a1...wxyz"],
  "image_model": "gpt-image-1.5",
  "planner_model": "gpt-4o",
  "image_provider": true,
  "planner_provider": true
}]}
```

- `api_keys` 中的值若与已存 Key 的**掩码一致**（如 GET 返回的 `sk-a1...wxyz`），则保留已存真实 Key——前端不改 Key 时原样提交掩码即可；否则视为新的明文 Key
- `server` 段（host/port/api_token）**不开放网页修改**（防止经 API 关闭鉴权），请直接编辑 `config.yaml`
- 校验规则与 `config.yaml` 相同（非空 name/base_url/至少 1 个 Key；生图 provider 需有 image_model）
- 响应 `{"ok": true, "config_file": "...", "providers": [掩码视图]}`

## POST /api/v1/svg

位图→SVG（`convert`）或提示词→生图→SVG（`generate`）。

**请求**（三选一通道）：

```jsonc
// convert · JSON
{"mode": "convert", "image_base64": "…", "params": {"filter_speckle": 8}}

// convert · URL
{"mode": "convert", "image_url": "https://example.com/a.png"}

// generate · 全链路（可选参考图）
{"mode": "generate", "prompt": "a minimal blue whale icon",
 "size": "1024x1024",
 "reference_image_base64": "…",          // 或 reference_image_url
 "params": {"colormode": "color"}}
```

也支持 `multipart/form-data`：`file=<图片或参考图>` + `payload=<上述 JSON 字符串>`。

**vtracer 参数**（`params`，全部可选，白名单校验非法回默认）：
`colormode`(color/binary)、`hierarchical`(stacked/cutout)、`mode`(spline/polygon)、
`filter_speckle`(1-128)、`color_precision`(1-8)、`layer_difference`(1-256)、
`corner_threshold`(1-179)、`length_threshold`(0.5-100)、`splice_threshold`(1-90)、`path_precision`(0-8)

**响应**：

```json
{
  "ok": true,
  "meta": {"svg_bytes": 494, "paths": 2, "original_size": [128,128],
            "output_size": [128,128], "downscaled": false, "mode": "convert",
            "oversized_warning": null},
  "svg": "<?xml version=\"1.0\"…"
}
```

**错误**：400（格式/参数/SSRF 拒绝）、413（超过 20MB 或 10MP）、502（生图失败）、503（未配置 provider）。

## POST /api/v1/gif

创建 GIF 生成任务（异步），立即返回 `202`。

**请求**：

```json
{
  "prompt": "一只橘猫挥手打招呼，无缝循环",   // 必填
  "frames": 6,                    // 2 到 max_frames（默认上限 12）
  "size": "1024x1024",            // 512x512 | 768x768 | 1024x1024
  "duration_ms": 200,             // 帧间隔 20-5000
  "loop": true,                   // 无限循环
  "background": "#FFFFFF",
  "reference_image_base64": "…",  // 可选参考图（或 reference_image_url；multipart 时用 file 字段）
}
```

**响应**：`202 {"task_id": "ab12cd34ef56", "status": "pending"}`

**错误**：400（参数）、429（并发任务超上限，默认 3）、503（未配置 provider）。

## GET /api/v1/tasks/{task_id}

任务详情：

```json
{
  "id": "ab12cd34ef56", "status": "done",   // pending|running|done|failed|cancelled
  "prompt": "…", "params": {"frames": 6, "…": "…"},
  "frames": [{"index": 1, "file": "frame_001.png", "actual_size": [1024,1024], "note": "first frame"}],
  "events": [{"ts": 1762300000.0, "level": "frame", "message": "第 1 帧完成", "data": {}}],
  "result": {
    "gif": "/api/v1/files/ab12cd34ef56/animation.gif",
    "frame_files": ["/api/v1/files/ab12cd34ef56/frame_001.png"],
    "loop_closed": true,
    "stopped_reason": "reached target frames (6)",
    "frames_generated": 6
  },
  "error": null
}
```

`loop_closed: false` 表示末帧未能完全接回首帧（已尽力重生成一次）；
`stopped_reason` 说明提前停止原因（规划器判定停止 / 一致性低于阈值 / 达到目标帧数）。

## GET /api/v1/tasks/{task_id}/events

SSE 事件流（`text/event-stream`），每 15s 发 keepalive。事件级别：
`info`（进度）、`warn`（降级/重试）、`frame`（帧完成，data 带 index/path）、`done`、`error`。
断线后请回退轮询 `GET /api/v1/tasks/{id}`（其 `events` 字段含完整历史）。

## POST /api/v1/tasks/{task_id}/cancel

协作式取消：当前帧生成完成后终止，已生成帧保留。响应 `{"status": "cancelling", "cancelled_now": true}`。

## GET /api/v1/files/{task_id}/{name}

下载任务产物（`animation.gif` / `frame_XXX.png`）。文件名白名单校验 + 路径限制在任务目录内。

## GET /api/v1/tasks/stats

任务统计：总数与各状态计数。

## 速率与限制

- 同时运行 GIF 任务默认上限 3（`gif.max_concurrent_tasks`）
- 完成任务保留 1 小时（`gif.task_ttl_seconds`），过期自动清理；**服务重启任务记录即失**（v1 不持久化）
- SVG 上传默认上限 20MB / 10MP，长边超 4096 自动等比缩小（响应 `downscaled: true` 注明）
