# Replicate GPT-Image-2.6 Studio

> **"GIF是网络表达的母语，我们让每位不用OpenArt的人也能通过单一提示流畅地表达。"——白小谦**
> 
> **"让技术不被垄断封锁，让技术为人民服务！"——白小谦**

**这是首个同时本地复刻 GPT-Image-2.6「GIF 动图生成」与「SVG 矢量导出」两项能力的开源工作台（不是模型）。**



**GIF 动图生成**与 **SVG 矢量导出**。一条命令启动，自带 Web 工作台，同时是一个可被任意程序调用的**本地 API**；AI 能力由你在本地配置文件中接入任意 OpenAI 兼容端点（官方 / 中转 / 私有部署），**多 Key 自动轮询**。

| 能力                          | 实现原理                                                                                                                                                 |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GIF 生成**（复刻官方"文本→循环 GIF"） | 视觉模型先产出全片剧本（首帧描述 + 逐帧变化 + 循环闭合要求 + 风格锚）→ 生图 API 出底图 → **以上一帧为参考图**逐帧链式生成（`/images/edits`）→ 规划模型逐帧读图决策（继续/适当停止/一致性评分）→ 首尾闭合检查（未闭合自动重生成末帧一次）→ 合成循环 GIF |
| **SVG 导出**（近似官方"生图直出矢量"）    | 开源矢量库 [vtracer](https://github.com/visioncortex/vtracer)（Rust，MIT）；支持**位图转换**与**AI 生成→SVG** 全链路双模式，可选参考图                                             |

## 效果示例（真实任务产物，采用agnes-3.0-flash+agnes-image-2.1-flash免费完成）

单条提示词「一个极简火柴人拿着剑，游戏风格，把剑来回挥砍」的完整产出：

<p align="center">
  <img src="example-image/stickman_sword.gif" width="360" alt="火柴人挥剑 GIF（6 帧）"><br>
  <sup>GIF · 6 帧 · 导演模式自动分解动作 · 合成用时约 1.5 分钟</sup>
</p>

<p align="center">
  <img src="example-image/stickman_v14.png" width="300" alt="AI 生成原图">　
  <img src="example-image/stickman_v14.svg" width="300" alt="SVG 矢量导出"><br>
  <sup>AI 生成原图（位图）→ SVG 矢量导出，工作台内可拉杆滑动对比</sup>
</p>

**拉杆对比素材**（工作台内由绿色拉杆左右滑动对照位图与 SVG）：

<p align="center">
  <img src="example-image/sword_slider_src.png" width="300" alt="拉杆对比：位图层">　
  <img src="example-image/sword_slider.svg" width="300" alt="拉杆对比：SVG 层">
</p>

以上文件均在 [example-image/](example-image/) 目录。

## 功能特性

- 🎞 **GIF 工厂**：单条提示词（可选参考图作首帧起点）→ 2–12 帧循环 GIF；逐帧事件流（SSE）实时进度、帧时间轴、可取消
- 📐 **SVG 转换器**：拖拽/粘贴/URL 位图转矢量，或提示词生成后直接矢量化；vtracer 全参数可调
- 🔑 **Key 轮询池**：同一端点多 Key Least-Used 轮询、429 指数冷却（60s→15min）、连续失败自动标记故障、全部不可用时返回最快恢复时间
- 🛡 **安全默认**：Key 只存本地 `config.yaml`（不入库）；URL 拉图全链路 SSRF 防护（拒绝环回/私网/保留地址 + DNS 全记录校验 + 重定向逐跳校验）；可选 Bearer 鉴权；下载路由防路径穿越
- 🖥 **开箱即用**：`pip install -r requirements.txt && python run.py`，浏览器打开即用；同时是标准 REST API（含 OpenAPI 文档 `/api/docs`）
- 🧩 **容错设计**：AI 返回 JSON 五级容错解析；规划器故障自动降级沿用剧本；CPU 密集操作线程池隔离，长任务互不阻塞

## 快速开始

### 环境要求

- Python 3.10+（已在 Windows / Python 3.12 完整测试）
- 一个 OpenAI 兼容端点 + API Key（生图模型需支持 `/images/generations` 与 `/images/edits` 参考图编辑；规划模型需支持读图）

### 安装

**Windows 一键启动**：双击 `start.cmd`——自动检查并安装依赖、启动服务（首次启动后点右上角齿轮即可在网页里配置 API）。

**通用方式**：

```bash
# 1. 安装依赖（国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
pip install -r requirements.txt

# 2. 启动（配置可以稍后在网页里做，也可以先建配置文件）
python run.py            # 默认 http://127.0.0.1:8266
```

**配置 API（两种方式任选）**：

- **网页端（推荐）**：打开 `http://127.0.0.1:8266` → 右上角齿轮 → 填入端点地址、API Key（一行一个）、生图/规划模型 → 保存即生效（自动写入 `config.yaml` 并热重建 Key 池，无需重启）
- **配置文件**：复制 `config.example.yaml` 为 `config.yaml` 手工编辑（适合带注释的精细配置；网页保存会重写该文件并丢失注释）

```bash
copy config.example.yaml config.yaml    # Windows
cp config.example.yaml config.yaml      # macOS / Linux
```

**（可选）实测端点三项能力**：读图 / 生图 / 参考图编辑

```bash
python scripts/probe_endpoint.py
```

打开 `http://127.0.0.1:8266` 即见工作台（GIF 工厂 / SVG 转换器）。

> 未配置 provider 时服务也能启动：SVG 位图转换可用，AI 生成类功能返回可读错误提示。

### 配置说明（config.yaml）

```yaml
providers:
  - name: my-provider
    base_url: https://api.example.com/v1   # OpenAI 兼容端点
    api_keys: ["sk-xxx-1", "sk-xxx-2"]     # 多 Key 自动轮询 + 冷却
    image_model: "gpt-image-1.5"           # 生图模型（需支持参考图编辑）
    planner_model: "gpt-4o"                # 视觉规划模型（需支持读图）
```

完整字段（GIF/SVG 参数、鉴权、CORS 等）见 [`config.example.yaml`](config.example.yaml)。

**安全提示**：默认绑定 `127.0.0.1` 仅本机访问；如开放局域网（`host: 0.0.0.0`），请务必设置 `api_token` 并只向信任设备开放。

## 本地 API 调用

服务同时是标准 REST API，交互文档见 [`docs/API.md`](docs/API.md)，也可访问运行时自动文档 `http://127.0.0.1:8266/api/docs`。

### 端点一览

| 方法       | 路径                               | 说明                                        |
| -------- | -------------------------------- | ----------------------------------------- |
| GET      | `/api/health`                    | 健康检查 + 版本 + provider 就绪状态                 |
| GET      | `/api/v1/config`                 | 脱敏配置视图（Key 掩码显示）                          |
| GET/POST | `/api/v1/settings`               | 网页端 API 配置：掩码回显 / 保存写盘并热生效                |
| GET      | `/api/v1/keys`                   | Key 池实时状态（冷却/故障/用量）                       |
| POST     | `/api/v1/keys/reset`             | 强制终止冷却（立即清零全部 Key 的冷却/故障标记）               |
| POST     | `/api/v1/svg`                    | 位图→SVG（`convert`）或 提示词→生图→SVG（`generate`） |
| POST     | `/api/v1/gif`                    | 创建 GIF 生成任务 → `202 + task_id`             |
| GET      | `/api/v1/tasks/{id}`             | 任务详情（帧列表 + 事件历史）                          |
| GET      | `/api/v1/tasks/{id}/events`      | SSE 实时进度流                                 |
| POST     | `/api/v1/tasks/{id}/cancel`      | 取消任务                                      |
| GET      | `/api/v1/files/{task_id}/{name}` | 下载产物（GIF / 单帧 PNG）                        |

### curl 示例

```bash
# 位图转 SVG
curl -X POST http://127.0.0.1:8266/api/v1/svg \
  -F "file=@photo.png" -F 'payload={"mode":"convert"}'

# 提示词直接生成 SVG（全链路）
curl -X POST http://127.0.0.1:8266/api/v1/svg \
  -H "Content-Type: application/json" \
  -d '{"mode":"generate","prompt":"a minimal blue whale icon","size":"1024x1024"}'

# 创建 GIF 任务并轮询
curl -X POST http://127.0.0.1:8266/api/v1/gif \
  -H "Content-Type: application/json" \
  -d '{"prompt":"一只橘猫挥手打招呼","frames":6,"size":"1024x1024"}'
curl http://127.0.0.1:8266/api/v1/tasks/<task_id>
```

### Python 示例

```python
import httpx, time

BASE = "http://127.0.0.1:8266"
# HEADERS = {"Authorization": "Bearer <你的 api_token>"}  # 启用鉴权时

with httpx.Client(timeout=60) as c:
    # GIF：创建 → 轮询 → 下载
    task_id = c.post(f"{BASE}/api/v1/gif",
                     json={"prompt": "a cat waving, seamless loop",
                           "frames": 6, "size": "1024x1024"}).json()["task_id"]
    while True:
        t = c.get(f"{BASE}/api/v1/tasks/{task_id}").json()
        if t["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(3)
    print(t["status"], t.get("result"))
    gif = c.get(BASE + t["result"]["gif"]).content
    open("animation.gif", "wb").write(gif)

    # SVG：位图转换
    r = c.post(f"{BASE}/api/v1/svg",
               json={"mode": "convert",
                     "image_base64": __import__("base64").b64encode(open("in.png","rb").read()).decode()})
    open("out.svg", "w", encoding="utf-8").write(r.json()["svg"])
```

## 验证端点

用 `python scripts/probe_endpoint.py` 即可实测你的端点（读图 / 生图 / 参考图编辑 / 返回格式 / 尺寸遵循）。

## 项目结构

```
├── run.py               # 启动入口
├── config.example.yaml  # 配置模板（复制为 config.yaml 使用）
├── app/
│   ├── config.py        # 配置加载与校验
│   ├── keypool.py       # 多 Key 轮询池（冷却/故障标记）
│   ├── llm.py           # OpenAI 兼容客户端（读图/生图/参考图编辑）
│   ├── jsonx.py         # AI JSON 五级容错解析
│   ├── imaging.py       # 图像预处理 + SSRF 安全下载
│   ├── svg/             # vtracer 矢量化封装
│   ├── gif/             # 规划器 / 链式生成 / GIF 合成
│   ├── tasks.py         # 异步任务注册表（SSE/取消/TTL）
│   └── api/routes.py    # REST 路由
├── static/              # 内置 Web 工作台（无构建，零依赖）
├── scripts/probe_endpoint.py  # 端点能力探针
├── tests/               # pytest 测试套件（66 例，mock LLM 不花钱）
├── docs/                # SPEC / API 文档
└── DESIGN.md            # 前端设计系统（Vercel 风格）
```

## 开发

```bash
python -m pytest tests/ -q     # 运行测试套件（无需真实 Key）
python run.py --reload         # 开发模式
```

## 常见问题（FAQ）

**Q：网页里保存配置/提交任务报"保存失败：NetworkError"或"无法连接本地服务"？**
A：这是浏览器连不上本地服务，按序检查：① 页面地址必须是 `http://127.0.0.1:8266`——**不要直接双击 `static/index.html` 以文件方式打开**（`file://` 下无法调用 API）；② 服务是否在运行（`start.cmd` 或 `python run.py` 窗口别关）；③ 端口是否一致（改过端口就访问对应地址）。正常启动请用 `start.cmd`，它会自动打开浏览器。

**Q：GIF 生成到一半失败怎么办？**
A：每帧失败自动换 Key 重试；规划器故障自动降级"沿用剧本"连续 3 次才终止；任务详情中的事件日志会给出完整原因。

**Q：为什么我的 GIF 帧数少于请求数？**
A：这是"适当停止"机制——规划模型判定动画已自然演完，或帧间一致性低于阈值时提前收敛，避免烧钱生成重复帧。结果中的 `stopped_reason` 会说明原因；`loop_closed: false` 表示末帧未能完全接回首帧。

**Q：照片转出来的 SVG 太大？**
A：照片本就不是矢量化友好输入。调大 `filter_speckle`（≥8）、降低 `color_precision`，或直接用 `colormode: binary`。

**Q：转换用的是什么开源项目？**
A：[visioncortex/vtracer](https://github.com/visioncortex/vtracer)（MIT 协议，Rust 实现，通过官方 Python 绑定集成）。

## 许可与致谢

本项目以 **AGPL-3.0-or-later** 协议开源，详见 [LICENSE](LICENSE)。

- 开发者：**白小谦**
- 反馈邮箱：**qianxlfk@163.com**
- 矢量化核心：[visioncortex/vtracer](https://github.com/visioncortex/vtracer)（MIT）
- Web 框架：[FastAPI](https://fastapi.tiangolo.com)（MIT）；图像处理：[Pillow](https://python-pillow.org)（MIT-CMU）；HTTP：[httpx](https://www.python-httpx.org)（BSD-3）；图标：[Lucide](https://lucide.dev)（ISC）

## 版本

当前版本 **1.6.0**（版本唯一真源：`app/__init__.py`），变更记录见 [CHANGELOG.md](CHANGELOG.md)。


