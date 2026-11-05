/* img26 前端：OpenArt 风格工作台。
   含：侧边栏导航、阶段 stepper、逐帧实时上画、导演剧本卡、
   预设一键配置、帧数模式（固定/AI 自主）、位图导出、SSE+轮询兜底。 */
"use strict";

/* ============ 基础工具 ============ */

const $ = (id) => document.getElementById(id);

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function apiToken() {
  return localStorage.getItem("img26_token") || "";
}

/** 网络层错误统一转可读提示（服务未启动 / file:// 打开页面 / 端口不对）。 */
async function apiFetch(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const token = apiToken();
  if (token) headers["Authorization"] = "Bearer " + token;
  let resp;
  try {
    resp = await fetch(path, Object.assign({}, options, { headers }));
  } catch (e) {
    if (location.protocol === "file:") {
      throw new Error(
        "无法连接本地服务：当前页面是以文件方式打开的。请先运行 start.cmd（或 python run.py），然后通过 http://127.0.0.1:8266 访问"
      );
    }
    throw new Error(
      "无法连接本地服务：请确认服务已启动（start.cmd 或 python run.py）且端口正确"
    );
  }
  if (resp.status === 401) {
    toast("需要 API Token（左下角设置中填写）", true);
    openSettings();
    throw new Error("unauthorized");
  }
  return resp;
}

/* ============ 状态栏 ============ */

async function refreshStatus() {
  const pill = $("status-pill");
  const text = $("status-text");
  try {
    const resp = await fetch("/api/health");
    const health = await resp.json();
    $("footer-version").textContent = "v" + health.version;
    if (health.image_provider_ready && health.planner_provider_ready) {
      pill.className = "pill pill-ok";
      text.textContent = "服务正常";
    } else {
      pill.className = "pill pill-warn";
      text.textContent = "未配置 AI provider";
    }
    try {
      const cfgResp = await apiFetch("/api/v1/config");
      if (cfgResp.ok) {
        const cfg = await cfgResp.json();
        const parts = (cfg.providers || []).map(
          (p) =>
            `${p.name}（生图:${p.image_model || "-"} / 规划:${p.planner_model || "-"}；Key×${p.api_keys.length}）`
        );
        $("settings-providers").textContent = parts.join("\n") || "未配置 provider";
      }
    } catch (e) {
      /* 401 时静默：状态栏已有基础信息 */
    }
  } catch (e) {
    pill.className = "pill pill-err";
    text.textContent = "服务不可达";
    if (location.protocol === "file:") {
      pill.title = "当前是 file:// 方式打开的页面，无法调用本地 API。请先运行 start.cmd，再访问 http://127.0.0.1:8266";
      toast("页面以文件方式打开，API 不可用——请通过 http://127.0.0.1:8266 访问", true);
    }
  }
}

/* ============ 主题切换（浅色/深色，首次跟随系统，localStorage 记忆） ============ */

function currentThemeIsDark() {
  return document.documentElement.getAttribute("data-theme") === "dark";
}

function applyThemeIcons() {
  const dark = currentThemeIsDark();
  $("theme-icon-moon").classList.toggle("hidden", dark); // 深色时显示太阳（点击回浅色）
  $("theme-icon-sun").classList.toggle("hidden", !dark);
}

function toggleTheme() {
  const next = currentThemeIsDark() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("img26_theme", next);
  applyThemeIcons();
  toast(next === "dark" ? "已切换到深色模式" : "已切换到浅色模式");
}

/* ============ 侧边栏导航 ============ */

function bindNav() {
  const tabs = [
    [$("tab-gif"), $("panel-gif")],
    [$("tab-svg"), $("panel-svg")],
  ];
  for (const [btn, panel] of tabs) {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".workspace").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      panel.classList.add("active");
    });
  }
}

/* ============ 拖拽/粘贴/选择 通用绑定 ============ */

function bindImagePicker(dropId, inputId, previewId) {
  const drop = $(dropId);
  const input = $(inputId);
  const state = { file: null };

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      input.click();
    }
  });
  drop.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("dragover");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragover"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("dragover");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) setFile(f);
  });
  input.addEventListener("change", () => {
    if (input.files && input.files[0]) setFile(input.files[0]);
  });

  function setFile(file) {
    if (!file.type.startsWith("image/")) {
      toast("请选择图片文件", true);
      return;
    }
    state.file = file;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const img = $(previewId + "-img");
      if (img) img.src = ev.target.result;
      $(previewId).classList.remove("hidden");
    };
    reader.readAsDataURL(file);
  }
  return state;
}

/* ============ 阶段 stepper ============ */

const STAGE_ORDER = ["plan", "first_frame", "frames", "closure", "encode"];

function resetStages() {
  document.querySelectorAll("#gif-stagebar .stage").forEach((s) => {
    s.classList.remove("active", "done");
    const num = s.querySelector(".stage-num");
    if (num) num.textContent = num.dataset.n || num.textContent;
  });
}

function setStage(stage, state) {
  const el = document.querySelector(`#gif-stagebar .stage[data-stage="${stage}"]`);
  if (!el) return;
  el.classList.remove("active", "done");
  if (state === "active") el.classList.add("active");
  if (state === "done") {
    el.classList.add("done");
    const num = el.querySelector(".stage-num");
    if (num && !num.dataset.n) {
      num.dataset.n = num.textContent;
      num.textContent = "✓";
    }
  }
  // 其余阶段回到默认
  for (const s of STAGE_ORDER) {
    if (s === stage) continue;
    const other = document.querySelector(`#gif-stagebar .stage[data-stage="${s}"]`);
    if (other && !other.classList.contains("done")) other.classList.remove("active");
  }
}

/* ============ GIF 工作台 ============ */

const gifRef = bindImagePicker("gif-drop", "gif-file", "gif-ref-preview");

let currentTask = null;
let currentTaskStatus = "";
const gifSeenEvents = new Set();
let framesMode = "fixed";

function gifLog(level, message, stage) {
  const log = $("gif-log");
  if (!log) return;
  const p = document.createElement("p");
  const t = new Date().toLocaleTimeString();
  p.className = "lv-" + level;
  const tag = stage ? `<span class="log-tag">[${stageLabel(stage)}]</span> ` : "";
  p.innerHTML = `${tag}[${t}] ${escapeText(message)}`;
  log.appendChild(p);
  log.scrollTop = log.scrollHeight;
}

function stageLabel(stage) {
  return {
    plan: "剧本",
    first_frame: "底图",
    frames: "逐帧",
    closure: "闭合",
    encode: "合成",
  }[stage] || stage || "";
}

function setCanvasState(text) {
  const el = $("gif-canvas-state");
  if (!text) {
    el.classList.add("hidden");
    return;
  }
  el.textContent = text;
  el.classList.remove("hidden");
}

function showFrameOnCanvas(url, caption) {
  const img = $("gif-canvas-img");
  img.src = url;
  img.classList.remove("hidden");
  document.querySelector("#gif-canvas .canvas-placeholder").classList.add("hidden");
  setCanvasState(caption || "");
}

/* 剧本流式：原始 JSON 打字机 + 完成后结构化卡片 */
let scriptRawText = "";

function scriptStreamBegin() {
  scriptRawText = "";
  const card = $("script-card");
  card.classList.remove("hidden");
  card.open = true;
  const raw = $("script-raw");
  raw.textContent = "";
  raw.classList.remove("hidden");
  $("script-changes").innerHTML = "";
}

function scriptStreamAppend(chunk) {
  scriptRawText += chunk;
  const raw = $("script-raw");
  raw.textContent = scriptRawText;
  raw.scrollTop = raw.scrollHeight;
}

function showScriptCard(script, framesPlanned, mode) {
  const card = $("script-card");
  card.classList.remove("hidden");
  card.open = true;
  $("script-raw").classList.add("hidden"); // 结构化卡片替换原始流文本
  $("script-first").textContent = script.first_frame_prompt || "";
  const label =
    (mode === "auto" ? "AI 自主设计 " : "") + framesPlanned + " 帧";
  card.querySelector("summary").textContent = "导演剧本（" + label + "）";
  const changes = $("script-changes");
  changes.innerHTML = "";
  (script.frame_changes || []).forEach((c, i) => {
    const p = document.createElement("p");
    p.className = "script-line";
    p.innerHTML = "<b>帧 " + (i + 2) + "：</b>" + escapeText(c);
    changes.appendChild(p);
  });
}

async function submitGif() {
  const prompt = $("gif-prompt").value.trim();
  if (!prompt) {
    toast("请先填写动画描述", true);
    return;
  }
  const body = {
    prompt,
    frames: parseInt($("gif-frames").value, 10),
    frames_mode: framesMode,
    size: $("gif-size").value,
    duration_ms: parseInt($("gif-duration").value, 10),
    loop: $("gif-loop").checked,
  };

  const fd = new FormData();
  fd.append("payload", JSON.stringify(body));
  if (gifRef.file) fd.append("file", gifRef.file);

  $("gif-submit").disabled = true;
  $("gif-cancel").classList.remove("hidden");
  $("gif-result-bar").classList.add("hidden");
  resetStages();
  $("gif-log").innerHTML = "";
  $("gif-log-card").classList.remove("hidden");
  $("gif-log-card").open = true;
  gifSeenEvents.clear();
  showFrameOnCanvasPlaceholder();

  try {
    const resp = await apiFetch("/api/v1/gif", { method: "POST", body: fd });
    const data = await resp.json();
    if (resp.status !== 202) throw new Error(data.detail || "创建任务失败");
    currentTask = { id: data.task_id, pollTimer: null, abortCtrl: new AbortController() };
    gifLog("info", "任务已创建：" + data.task_id);
    setCanvasState("等待调度…");
    watchGifTask(currentTask);
  } catch (e) {
    if (e.message !== "unauthorized") {
      gifLog("error", "创建失败：" + e.message);
      toast("创建失败：" + e.message, true);
      resetGifButtons();
    }
  }
}

function showFrameOnCanvasPlaceholder() {
  $("gif-canvas-img").classList.add("hidden");
  document.querySelector("#gif-canvas .canvas-placeholder").classList.remove("hidden");
  setCanvasState("");
}

function resetGifButtons() {
  $("gif-submit").disabled = false;
  $("gif-cancel").classList.add("hidden");
}

/** fetch 流式读取 SSE（可带鉴权头）；失败自动降级轮询。 */
async function watchGifTask(task) {
  let sseOk = false;
  try {
    const resp = await apiFetch(`/api/v1/tasks/${task.id}/events`, {
      signal: task.abortCtrl.signal,
    });
    if (resp.ok && resp.body) {
      sseOk = true;
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          for (const line of chunk.split("\n")) {
            if (line.startsWith("data: ")) handleGifEvent(JSON.parse(line.slice(6)));
          }
        }
        if (["done", "failed", "cancelled"].includes(currentTaskStatus)) break;
      }
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    /* SSE 失败 → 轮询兜底 */
  }
  if (!sseOk) startGifPolling(task);
}

function startGifPolling(task) {
  gifLog("warn", "事件流不可用，切换轮询模式…");
  task.pollTimer = setInterval(async () => {
    try {
      const resp = await apiFetch(`/api/v1/tasks/${task.id}`);
      const detail = await resp.json();
      for (const evt of detail.events || []) {
        const key = evt.ts + evt.message;
        if (!gifSeenEvents.has(key)) {
          gifSeenEvents.add(key);
          handleGifEvent(evt);
        }
      }
      if (["done", "failed", "cancelled"].includes(detail.status)) {
        clearInterval(task.pollTimer);
        finishGifTask(detail);
      }
    } catch (e) {
      clearInterval(task.pollTimer);
    }
  }, 1500);
}

function handleGifEvent(evt) {
  const data = evt.data || {};
  if (evt.level === "plan_delta") {
    // 剧本流式打字机：不进日志，追加到原始流文本区
    if (!gifSeenEvents.has(evt.ts + evt.message)) {
      gifSeenEvents.add(evt.ts + evt.message);
      scriptStreamAppend(data.delta || "");
    }
    setStage("plan", "active");
    return;
  }
  if (gifSeenEvents.has(evt.ts + evt.message)) return;
  gifSeenEvents.add(evt.ts + evt.message);
  if (data.stage) setStage(data.stage, data.state);
  gifLog(evt.level, evt.message, data.stage);

  if (evt.level === "info" && data.stage) {
    setCanvasState(evt.message);
  }
  if (data.script) {
    showScriptCard(data.script, data.frames_planned || 0, data.frames_mode || "fixed");
  }
  if (evt.level === "frame" && data.index) {
    // 帧完成：直接上画布（含规划备注）
    showFrameOnCanvas(
      currentTask ? `/api/v1/files/${currentTask.id}/${data.path}` : "",
      `帧 ${data.index}${data.note ? " · " + data.note : ""}`
    );
  }
  if (evt.level === "done") finishGifTaskFromEvent(data);
  if (evt.level === "error") resetGifButtons();
}

async function refreshGifDetail() {
  if (!currentTask) return;
  const resp = await apiFetch(`/api/v1/tasks/${currentTask.id}`);
  finishGifTask(await resp.json());
}

function finishGifTaskFromEvent(data) {
  resetGifButtons();
  refreshGifDetail();
}

function finishGifTask(detail) {
  currentTaskStatus = detail.status;
  if (detail.status === "done" && detail.result) {
    resetGifButtons();
    const url = detail.result.gif;
    showFrameOnCanvas(url + "?t=" + Date.now(), "");
    setCanvasState("");
    const badge = $("gif-loop-badge");
    if (detail.result.loop_closed) {
      badge.className = "pill pill-ok";
      badge.textContent = "循环（剧本闭合指令）";
    } else {
      badge.className = "pill pill-warn";
      badge.textContent = "首尾未完全闭合";
    }
    $("gif-result-meta").textContent = `${detail.result.frames_generated} 帧 · 停止：${detail.result.stopped_reason}`;
    $("gif-download").onclick = () => {
      window.location.href = url;
    };
    $("gif-result-bar").classList.remove("hidden");
    toast("GIF 生成完成");
  } else if (detail.status === "failed") {
    resetGifButtons();
    setCanvasState("");
    gifLog("error", "任务失败：" + (detail.error || "未知错误"));
    toast("任务失败：" + (detail.error || ""), true);
  } else if (detail.status === "cancelled") {
    resetGifButtons();
    setCanvasState("");
    gifLog("warn", "任务已取消");
  }
}

async function cancelGif() {
  if (!currentTask) return;
  try {
    await apiFetch(`/api/v1/tasks/${currentTask.id}/cancel`, { method: "POST" });
    gifLog("warn", "已发送取消请求…");
  } catch (e) {
    /* 401 已由 apiFetch 处理 */
  }
}

/* ============ SVG 工作台 ============ */

let svgMode = "generate";
const svgInput = bindImagePicker("svg-drop", "svg-file", "svg-ref-preview");
const svgGenRef = bindImagePicker("svg-gen-drop", "svg-gen-file", "svg-gen-ref-preview");

function setSvgMode(mode) {
  svgMode = mode;
  $("svg-mode-convert").classList.toggle("active", mode === "convert");
  $("svg-mode-generate").classList.toggle("active", mode === "generate");
  $("svg-convert-inputs").classList.toggle("hidden", mode !== "convert");
  $("svg-generate-inputs").classList.toggle("hidden", mode !== "generate");
  $("svg-submit-label").textContent = mode === "convert" ? "转换为 SVG" : "生成并矢量化";
}

function svgParams() {
  return {
    colormode: $("p-colormode").value,
    hierarchical: $("p-hierarchical").value,
    mode: $("p-mode").value,
    filter_speckle: parseInt($("p-filter_speckle").value, 10),
    color_precision: parseInt($("p-color_precision").value, 10),
    layer_difference: parseInt($("p-layer_difference").value, 10),
  };
}

async function submitSvg() {
  $("svg-state").textContent = "处理中…";
  $("svg-state").classList.remove("hidden");
  $("svg-result-bar").classList.add("hidden");
  $("svg-compare").classList.add("hidden");
  $("svg-submit").disabled = true;

  let resp;
  try {
    if (svgMode === "convert") {
      const url = $("svg-url").value.trim();
      if (svgInput.file) {
        const fd = new FormData();
        fd.append("file", svgInput.file);
        fd.append("payload", JSON.stringify({ mode: "convert", params: svgParams() }));
        resp = await apiFetch("/api/v1/svg", { method: "POST", body: fd });
      } else if (url) {
        resp = await apiFetch("/api/v1/svg", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: "convert", image_url: url, params: svgParams() }),
        });
      } else {
        throw new Error("请上传图片或输入图片 URL");
      }
    } else {
      const prompt = $("svg-prompt").value.trim();
      if (!prompt) throw new Error("请填写生成描述");
      const body = {
        mode: "generate",
        prompt,
        size: $("svg-gen-size").value,
        params: svgParams(),
      };
      if (svgGenRef.file) {
        const fd = new FormData();
        fd.append("file", svgGenRef.file);
        fd.append("payload", JSON.stringify(body));
        resp = await apiFetch("/api/v1/svg", { method: "POST", body: fd });
      } else {
        resp = await apiFetch("/api/v1/svg", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
    }
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "转换失败");
    renderSvgResult(data);
  } catch (e) {
    if (e.message !== "unauthorized") {
      $("svg-state").textContent = "失败：" + e.message;
      toast("失败：" + e.message, true);
    }
  } finally {
    $("svg-submit").disabled = false;
    setTimeout(() => $("svg-state").classList.add("hidden"), 1500);
  }
}

/* ============ SVG 拉杆对比 ============ */

function setSliderPos(pct) {
  const slider = $("svg-compare");
  const clamped = Math.max(0, Math.min(100, pct));
  slider.style.setProperty("--pos", clamped + "%");
  $("svg-handle").setAttribute("aria-valuenow", String(Math.round(clamped)));
}

function bindCompareSlider() {
  const slider = $("svg-compare");
  const handle = $("svg-handle");
  let dragging = false;

  const fromEvent = (e) => {
    const rect = slider.getBoundingClientRect();
    setSliderPos(((e.clientX - rect.left) / rect.width) * 100);
  };
  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (dragging) fromEvent(e);
  });
  handle.addEventListener("pointerup", () => (dragging = false));
  handle.addEventListener("pointercancel", () => (dragging = false));
  // 点击滑杆任意处跳转；双击回中
  slider.addEventListener("pointerdown", (e) => {
    if (e.target === handle || handle.contains(e.target)) return;
    fromEvent(e);
  });
  slider.addEventListener("dblclick", () => setSliderPos(50));
  // 键盘可访问性
  handle.addEventListener("keydown", (e) => {
    const cur = parseFloat(getComputedStyle(slider).getPropertyValue("--pos")) || 50;
    if (e.key === "ArrowLeft") { setSliderPos(cur - 3); e.preventDefault(); }
    if (e.key === "ArrowRight") { setSliderPos(cur + 3); e.preventDefault(); }
    if (e.key === "Home") { setSliderPos(0); e.preventDefault(); }
    if (e.key === "End") { setSliderPos(100); e.preventDefault(); }
  });
}

function renderSvgResult(data) {
  const { svg, meta, png_base64 } = data;
  if (typeof svg !== "string" || !svg.includes("<svg")) {
    throw new Error("SVG 内容非法");
  }
  $("svg-compare").classList.remove("hidden");
  $("svg-state").classList.add("hidden");

  // 拉杆左层：位图（convert=输入图 / generate=AI 生成原图）
  const srcImg = $("svg-src-img");
  if (svgMode === "generate" && png_base64) {
    srcImg.src = "data:image/png;base64," + png_base64;
    $("svg-src-label").textContent = "AI 生成原图";
    const a = $("svg-png-download");
    a.href = "data:image/png;base64," + png_base64;
    a.download = "generated.png";
    a.classList.remove("hidden");
  } else if (svgMode === "convert") {
    srcImg.src = svgInput.file ? URL.createObjectURL(svgInput.file) : $("svg-url").value.trim();
    $("svg-src-label").textContent = "输入位图";
    $("svg-png-download").classList.add("hidden");
  } else {
    srcImg.removeAttribute("src");
    $("svg-src-label").textContent = "位图";
    $("svg-png-download").classList.add("hidden");
  }
  $("svg-out-box").innerHTML = svg; // 拉杆右层：SVG
  setSliderPos(50);

  const kb = (meta.svg_bytes / 1024).toFixed(1);
  $("svg-meta").textContent = `${meta.paths} 条路径 · ${kb} KB · ${meta.output_size[0]}×${meta.output_size[1]} · ${(meta.elapsed_ms / 1000).toFixed(1)}s · 拖动中间拉杆对比`;
  $("svg-result-bar").classList.remove("hidden");
  if (meta.oversized_warning) toast(meta.oversized_warning, true);

  const blob = new Blob([svg], { type: "image/svg+xml" });
  const a = $("svg-download");
  a.href = URL.createObjectURL(blob);
  $("svg-copy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(svg);
      toast("SVG 代码已复制");
    } catch (e) {
      toast("复制失败，请手动选择", true);
    }
  };
}

/* ============ 设置弹层（Token + 预设 + providers） ============ */

let PRESETS = [];

async function openSettings() {
  $("settings-token").value = apiToken();
  $("modal-settings").classList.remove("hidden");
  await Promise.all([loadSettingsProviders(), loadPresets()]);
}

async function loadPresets() {
  const sel = $("preset-select");
  try {
    const resp = await apiFetch("/api/v1/presets");
    const data = await resp.json();
    PRESETS = data.presets || [];
    for (const opt of Array.from(sel.querySelectorAll("option")).slice(1)) opt.remove();
    for (const p of PRESETS) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      sel.appendChild(opt);
    }
  } catch (e) {
    /* 预设拉取失败不阻塞设置页 */
  }
}

function applyPreset(pid) {
  const notes = $("preset-notes");
  const p = PRESETS.find((x) => x.id === pid);
  if (!p) {
    notes.classList.add("hidden");
    return;
  }
  const card = ensureProvCard();
  card.querySelector(".pf-name").value = p.id;
  card.querySelector(".pf-url").value = p.base_url;
  card.querySelector(".pf-image-model").value = p.image_model || "";
  card.querySelector(".pf-planner-model").value = p.planner_model || "";
  card.querySelector(".pf-edit-style").value = p.edit_style || "edits_multipart";
  notes.textContent = p.notes || "";
  notes.classList.remove("hidden");
  card.querySelector(".pf-keys").focus();
}

function ensureProvCard() {
  let card = document.querySelector(".prov-card");
  if (!card) renderProvCard(null);
  return document.querySelector(".prov-card");
}

async function loadSettingsProviders() {
  const list = $("prov-list");
  list.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const resp = await apiFetch("/api/v1/settings");
    const data = await resp.json();
    list.innerHTML = "";
    const providers = data.providers || [];
    if (providers.length === 0) {
      list.innerHTML = '<p class="hint">尚未配置端点——选择上方预设或点击「添加端点」。</p>';
    }
    for (const p of providers) renderProvCard(p);
  } catch (e) {
    if (e.message !== "unauthorized") {
      list.innerHTML = '<p class="hint">加载失败：' + escapeText(e.message) + "</p>";
    }
  }
}

const X_ICON =
  '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

function renderProvCard(p) {
  p = p || { name: "", base_url: "", api_keys: [], image_model: "", planner_model: "", image_provider: true, planner_provider: true, edit_style: "edits_multipart" };
  const card = document.createElement("div");
  card.className = "prov-card";
  card.innerHTML = `
    <div class="prov-card-head">
      <label>名称<input class="pf-name text-input" value="${escapeAttr(p.name)}" placeholder="my-provider"></label>
      <button class="btn-danger" title="删除此端点">${X_ICON} 删除</button>
    </div>
    <label class="full">Base URL（填到 /v1 即可）<input class="pf-url text-input" value="${escapeAttr(p.base_url)}" placeholder="https://api.example.com/v1"></label>
    <div class="prov-grid">
      <label>API Keys（一行一个，多 Key 轮询）
        <textarea class="pf-keys" placeholder="sk-...">${escapeText((p.api_keys || []).join("\n"))}</textarea>
      </label>
      <label>模型与编辑模式
        <input class="pf-image-model text-input" value="${escapeAttr(p.image_model || "")}" placeholder="生图模型（如 gpt-image-1.5）">
        <input class="pf-planner-model text-input" value="${escapeAttr(p.planner_model || "")}" placeholder="规划模型（需支持读图，如 gpt-4o）">
        <select class="pf-edit-style">
          <option value="edits_multipart" ${p.edit_style !== "generations_extra_body" && p.edit_style !== "generations_image_field" ? "selected" : ""}>图生图：OpenAI edits（multipart）</option>
          <option value="generations_extra_body" ${p.edit_style === "generations_extra_body" ? "selected" : ""}>图生图：generations + extra_body.image（Agnes 式）</option>
          <option value="generations_image_field" ${p.edit_style === "generations_image_field" ? "selected" : ""}>图生图：generations + 顶层 image（火山方舟式）</option>
        </select>
      </label>
      <div class="prov-roles">
        <label><input type="checkbox" class="pf-role-image" ${p.image_provider !== false ? "checked" : ""}> 生图</label>
        <label><input type="checkbox" class="pf-role-planner" ${p.planner_provider !== false ? "checked" : ""}> 规划</label>
      </div>
    </div>`;
  card.querySelector(".btn-danger").addEventListener("click", () => card.remove());
  $("prov-list").appendChild(card);
  return card;
}

function collectProviders() {
  const cards = document.querySelectorAll(".prov-card");
  const out = [];
  for (const c of cards) {
    const name = c.querySelector(".pf-name").value.trim();
    const baseUrl = c.querySelector(".pf-url").value.trim();
    const keys = c.querySelector(".pf-keys").value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!name || !baseUrl || keys.length === 0) continue;
    out.push({
      name,
      base_url: baseUrl,
      api_keys: keys,
      image_model: c.querySelector(".pf-image-model").value.trim(),
      planner_model: c.querySelector(".pf-planner-model").value.trim(),
      image_provider: c.querySelector(".pf-role-image").checked,
      planner_provider: c.querySelector(".pf-role-planner").checked,
      edit_style: c.querySelector(".pf-edit-style").value,
    });
  }
  return out;
}

async function saveSettings() {
  const providers = collectProviders();
  if (providers.length === 0) {
    toast("请至少填写一个完整端点（名称/URL/Key）", true);
    return;
  }
  const btn = $("settings-save");
  btn.disabled = true;
  try {
    const resp = await apiFetch("/api/v1/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "保存失败");
    toast("配置已保存并生效");
    $("settings-providers").textContent =
      "已保存 " + data.providers.length + " 个端点 → " + data.config_file;
    refreshStatus();
    loadSettingsProviders();
  } catch (e) {
    if (e.message !== "unauthorized") toast("保存失败：" + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function escapeText(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  return escapeText(s).replace(/"/g, "&quot;");
}

/* ============ 全局粘贴 ============ */

document.addEventListener("paste", (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (!file) continue;
      const gifActive = $("panel-gif").classList.contains("active");
      const input = gifActive ? $("gif-file") : $("svg-file");
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change"));
      toast("已从剪贴板粘贴图片");
      e.preventDefault();
      return;
    }
  }
});

/* ============ 右侧参数栏拖拽调宽 ============ */

function makeResizable(gridId, storageKey) {
  const grid = $(gridId);
  if (!grid) return;
  const params = grid.querySelector(".params-col");
  if (!params) return;
  const saved = parseInt(localStorage.getItem(storageKey) || "", 10);
  if (saved >= 260 && saved <= 520) {
    grid.style.gridTemplateColumns = `minmax(0,1fr) 5px ${saved}px`;
  }
  const splitter = grid.querySelector(".grid-splitter");
  if (!splitter) return;
  let startX = 0;
  let startW = 0;
  let dragging = false;
  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    startX = e.clientX;
    startW = params.getBoundingClientRect().width;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const w = Math.max(260, Math.min(520, Math.round(startW + (startX - e.clientX))));
    grid.style.gridTemplateColumns = `minmax(0,1fr) 5px ${w}px`;
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const w = Math.round(params.getBoundingClientRect().width);
    localStorage.setItem(storageKey, String(w));
  });
}

/* ============ 初始化 ============ */

function init() {
  bindNav();
  makeResizable("gif-grid", "gifParamsW");
  makeResizable("svg-grid", "svgParamsW");
  applyThemeIcons();
  $("theme-toggle").addEventListener("click", toggleTheme);
  // 深链：/#svg 直达 SVG 工作台
  if (location.hash === "#svg") {
    $("tab-svg").click();
  }
  $("btn-settings").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", () => $("modal-settings").classList.add("hidden"));
  $("settings-save").addEventListener("click", async () => {
    localStorage.setItem("img26_token", $("settings-token").value.trim());
    await saveSettings();
  });
  $("preset-select").addEventListener("change", (e) => applyPreset(e.target.value));
  $("prov-add").addEventListener("click", () => renderProvCard(null));
  $("keys-reset").addEventListener("click", async () => {
    const btn = $("keys-reset");
    btn.disabled = true;
    try {
      const resp = await apiFetch("/api/v1/keys/reset", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "重置失败");
      toast(`已强制终止冷却：${data.reset_keys} 个 Key 已恢复可用`);
    } catch (e) {
      if (e.message !== "unauthorized") toast("重置失败：" + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("gif-submit").addEventListener("click", submitGif);
  $("gif-cancel").addEventListener("click", cancelGif);
  $("gif-ref-clear").addEventListener("click", () => {
    gifRef.file = null;
    $("gif-ref-preview").classList.add("hidden");
  });

  // 帧数模式切换
  $("fm-fixed").addEventListener("click", () => {
    framesMode = "fixed";
    $("fm-fixed").classList.add("active");
    $("fm-auto").classList.remove("active");
    $("gif-frames-label").textContent = "帧数";
  });
  $("fm-auto").addEventListener("click", () => {
    framesMode = "auto";
    $("fm-auto").classList.add("active");
    $("fm-fixed").classList.remove("active");
    $("gif-frames-label").textContent = "最大帧数";
  });

  $("svg-mode-convert").addEventListener("click", () => setSvgMode("convert"));
  $("svg-mode-generate").addEventListener("click", () => setSvgMode("generate"));
  $("svg-submit").addEventListener("click", submitSvg);
  bindCompareSlider();
  $("svg-ref-clear").addEventListener("click", () => {
    svgInput.file = null;
    $("svg-ref-preview").classList.add("hidden");
    $("svg-url").value = "";
  });
  $("svg-gen-ref-clear").addEventListener("click", () => {
    svgGenRef.file = null;
    $("svg-gen-ref-preview").classList.add("hidden");
  });

  const sliders = [
    ["gif-frames", "gif-frames-val"],
    ["p-filter_speckle", "p-speckle-val"],
    ["p-color_precision", "p-precision-val"],
    ["p-layer_difference", "p-layer-val"],
  ];
  for (const [sid, lid] of sliders) {
    $(sid).addEventListener("input", () => ($(lid).textContent = $(sid).value));
  }

  refreshStatus();
  setInterval(refreshStatus, 30000);
}

document.addEventListener("DOMContentLoaded", init);
