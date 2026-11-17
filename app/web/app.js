"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  let snapshot = null;
  let csrfToken = "";
  let busy = false;
  let refreshing = false;
  let activeButton = null;
  let previewModel = null;
  let previewHash = null;
  let previewHashKnown = false;
  let runtimeInstallHint = null;
  const runtimeReleaseUrl = "https://github.com/lbjlaq/Antigravity-Manager/releases/tag/v5.0.3";
  const runtimeWindowsUrl = "https://github.com/lbjlaq/Antigravity-Manager/releases/download/v5.0.3/Antigravity.Tools_5.0.3_x64-setup.exe";
  const activity = [];
  const actionNames = {
    "/api/runtime/setup": "准备并启动服务",
    "/api/runtime/start": "启动服务",
    "/api/runtime/stop": "停止服务",
    "/api/accounts": "导入账号",
    "/api/accounts/refresh": "刷新账号信息",
    "/api/accounts/remove": "移除账号",
    "/api/models/select": "选择模型",
    "/api/check": "测试模型连接",
    "/api/codex/preview": "生成配置预览",
    "/api/codex/apply": "添加 Codex 配置",
    "/api/codex/restore": "恢复 Codex 配置"
  };

  function safeText(value, fallback = "") {
    if (typeof value !== "string" && typeof value !== "number") return fallback;
    return String(value)
      .replace(/1\/\/[A-Za-z0-9_-]+/g, "[凭据已隐藏]")
      .replace(/\b(?:sk-|AIza)[A-Za-z0-9_-]{16,}/g, "[凭据已隐藏]")
      .replace(/((?:api[_-]?key|(?:access|refresh|bearer)[_-]?token|password|secret|authorization)\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, "$1[已隐藏]")
      .slice(0, 2000);
  }

  function displayObject(value) {
    if (typeof value === "string") return safeText(value);
    const clean = (item) => {
      if (Array.isArray(item)) return item.slice(0, 100).map(clean);
      if (item && typeof item === "object") {
        return Object.fromEntries(Object.entries(item).map(([key, val]) => [key,
          /(?:api[_-]?key|token|password|secret|authorization|cookie|credential)/i.test(key)
            ? "[已隐藏]" : clean(val)]));
      }
      return typeof item === "string" ? safeText(item) : item;
    };
    return JSON.stringify(clean(value), null, 2);
  }

  function feedback(message, kind = "info") {
    const target = $("feedback");
    target.className = `feedback ${kind}`;
    target.textContent = safeText(message, "操作未完成，请检查服务状态。");
    target.hidden = false;
  }

  function record(label, result) {
    activity.unshift(`${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · ${label} · ${result}`);
    activity.splice(8);
    $("activity-list").replaceChildren(...activity.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }));
  }

  function badge(id, text, kind = "neutral") {
    const target = $(id);
    target.textContent = text;
    target.className = `badge ${kind}`;
  }

  function runtimeRunning() {
    return Boolean(snapshot && ["running", "ready", "available"].includes(snapshot.runtime?.status));
  }

  function modelValue() {
    return $("model-select").value;
  }

  function updateButtons() {
    const ready = Boolean(snapshot && csrfToken && !busy && !refreshing);
    const running = runtimeRunning();
    const hasModel = Boolean(modelValue());
    const savedModel = hasModel && modelValue() === snapshot?.selected_model;
    const runtime = snapshot?.runtime || {};
    const proxyNeedsStart = running && runtime.proxy_running === false && snapshot?.accounts?.length > 0;
    $("setup-runtime").disabled = !ready || (running && !proxyNeedsStart);
    $("setup-runtime").textContent = $("runtime-install").hidden ? "准备并启动" : "安装后重试";
    $("stop-runtime").disabled = !ready || !running || !runtime.managed;
    $("refresh-state").disabled = busy || refreshing;
    $("refresh-token").disabled = !ready || !running;
    $("import-account").disabled = !ready || !running || !$("refresh-token").value.trim();
    $("model-select").disabled = !ready || !running || !snapshot?.models?.length;
    $("check-model").disabled = !ready || !running || !hasModel;
    $("preview-codex").disabled = !ready || !hasModel;
    $("apply-codex").disabled = !ready || !hasModel || previewModel !== modelValue() || !previewHashKnown;
    $("restore-codex").disabled = !ready || !snapshot?.codex?.restore_available;
    $("copy-connection").disabled = !ready || !snapshot?.api?.base_url || !savedModel;
    if (snapshot) badge("api-badge", savedModel ? "接入信息已准备" : hasModel ? "型号尚未保存" : "等待选择模型", savedModel ? "success" : hasModel ? "warning" : "neutral");
    $("copy-api-key").disabled = !ready || !snapshot?.api?.key_present;
    document.querySelectorAll(".account-mutation").forEach((button) => { button.disabled = !ready || !running; });
  }

  async function request(path, payload) {
    const isPost = payload !== undefined;
    const headers = { Accept: "application/json" };
    if (isPost) {
      if (!csrfToken) throw new Error("页面尚未准备好，请刷新后重试。");
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = csrfToken;
    }
    let response;
    try {
      response = await fetch(path, {
        method: isPost ? "POST" : "GET", headers,
        body: isPost ? JSON.stringify(payload) : undefined,
        credentials: "same-origin", cache: "no-store", redirect: "error"
      });
    } catch (_) {
      throw new Error("无法连接本机控制台。请确认启动窗口仍在运行，再刷新状态。");
    }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error(`服务未返回可读取的状态（HTTP ${response.status}）。`); }
    if (!response.ok || data?.ok === false) {
      // Account import errors never reflect a server-provided payload into the UI.
      if (path === "/api/accounts") throw new Error("账号导入未完成。请检查凭据是否完整、服务是否已启动；输入内容已清空。");
      let reason = safeText(data?.error?.message || data?.message);
      if (/Traceback|\n\s*at\s|File "[^"\n]+", line|\b(?:Exception|Error):/.test(reason)) reason = "";
      throw new Error(reason || `操作未完成（HTTP ${response.status}），请检查服务状态。`);
    }
    return data;
  }

  function maskedLabel(value, index) {
    const label = safeText(value, `账号 ${index + 1}`);
    if (!label.includes("@")) return label;
    const parts = label.split("@");
    return `${parts[0].slice(0, 2)}***@${parts.slice(1).join("@")}`;
  }

  function renderRuntimeInstall(runtime) {
    const visible = runtime.status === "missing" && Boolean(runtime.download_url || runtime.instructions);
    $("runtime-install").hidden = !visible;
    if (!visible) return;
    $("runtime-install-instructions").textContent = safeText(runtime.instructions,
      "请先安装 Antigravity Tools 5.0.3，完成后回到这里点击“安装后重试”。");
    // Never turn an arbitrary backend-provided URL into an external link.
    const downloadUrl = [runtimeReleaseUrl, runtimeWindowsUrl].includes(runtime.download_url)
      ? runtime.download_url : runtimeReleaseUrl;
    $("runtime-install-link").href = downloadUrl;
    $("runtime-install-link").textContent = downloadUrl === runtimeWindowsUrl
      ? "下载上游 Windows 安装包（v5.0.3）↗" : "打开上游发布页（v5.0.3）↗";
  }

  function renderAccounts(accounts) {
    const list = $("accounts-list");
    list.replaceChildren();
    if (!accounts.length) {
      const empty = document.createElement("div"); empty.className = "empty-state";
      const icon = document.createElement("span"); icon.textContent = "＋"; icon.setAttribute("aria-hidden", "true");
      const title = document.createElement("strong"); title.textContent = "还没有账号";
      const text = document.createElement("p"); text.textContent = "启动服务后，在上方完成导入。";
      empty.append(icon, title, text); list.append(empty); return;
    }
    accounts.forEach((account, index) => {
      const card = document.createElement("article"); card.className = "account-card";
      const avatar = document.createElement("span"); avatar.className = "account-avatar"; avatar.textContent = "G"; avatar.setAttribute("aria-hidden", "true");
      const main = document.createElement("div"); main.className = "account-main";
      const title = document.createElement("div"); title.className = "account-title";
      const name = document.createElement("strong"); name.textContent = maskedLabel(account.label, index);
      const tier = document.createElement("span"); tier.className = "badge neutral"; tier.textContent = safeText(account.tier, "权益待核对");
      title.append(name, tier);
      const meta = document.createElement("div"); meta.className = "account-meta";
      meta.textContent = account.disabled ? "此账号当前未启用" : "已导入 · 模型权限以实际返回为准";
      main.append(title, meta);
      const actions = document.createElement("div"); actions.className = "account-actions";
      const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "button quiet account-mutation"; refresh.textContent = "刷新";
      refresh.addEventListener("click", () => mutate("/api/accounts/refresh", { id: account.id }, refresh, "账号信息已刷新。"));
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "button quiet remove account-mutation"; remove.textContent = "移除";
      remove.addEventListener("click", () => {
        if (busy || !confirm(`从本机服务移除“${maskedLabel(account.label, index)}”？\n这会移除保存的登录信息；之后需要重新导入。`)) return;
        mutate("/api/accounts/remove", { id: account.id }, remove, "账号已从本机服务移除。");
      });
      actions.append(refresh, remove); card.append(avatar, main, actions); list.append(card);
    });
  }

  function render() {
    if (!snapshot) { updateButtons(); return; }
    const runtime = snapshot.runtime || {};
    const running = runtimeRunning();
    const accounts = Array.isArray(snapshot.accounts) ? snapshot.accounts : [];
    const models = Array.isArray(snapshot.models) ? snapshot.models : [];
    const codex = snapshot.codex || {};
    const check = snapshot.last_check;
    const version = safeText(runtime.version);
    badge("connection-state", "已连接本机", "success");
    badge("runtime-badge", running ? "服务已启动" : runtime.status === "stopped" ? "服务未启动" : "等待准备", running ? "success" : "neutral");
    $("runtime-detail").textContent = running ? (runtime.managed ? "由本工具管理的服务正在运行。" : "已连接现有服务，保留它当前的运行方式。") : "准备运行组件，然后启动服务继续。";
    if (running && runtime.proxy_running === false) {
      badge("runtime-badge", accounts.length ? "模型服务待启用" : "可以导入账号", accounts.length ? "warning" : "success");
      $("runtime-detail").textContent = accounts.length ? "账号已导入，点击“准备并启动”启用模型服务。" : "本机管理服务已启动，导入账号后继续。";
    }
    if (runtime.status !== "missing") runtimeInstallHint = null;
    renderRuntimeInstall(runtimeInstallHint ? { ...runtimeInstallHint, ...runtime } : runtime);
    $("license-version").textContent = version ? `运行版本：${version}` : "运行版本：尚未读取";
    badge("accounts-badge", accounts.length ? `${accounts.length} 个账号` : "未连接", accounts.length ? "success" : "neutral");
    $("account-count").textContent = `${accounts.length} 个`;
    renderAccounts(accounts);
    const selected = snapshot.selected_model || "";
    const select = $("model-select"); select.replaceChildren();
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = models.length ? "请选择一个完整型号" : "先连接账号，再读取模型"; select.append(placeholder);
    models.forEach((model) => {
      if (!model || typeof model.id !== "string" || !model.id) return;
      const option = document.createElement("option"); option.value = model.id; option.textContent = model.id; select.append(option);
    });
    // Every selection is saved immediately; refreshed server state is authoritative.
    select.value = selected;
    const currentCheck = check && check.model === selected;
    const passed = currentCheck && check.passed === true;
    $("check-result").hidden = !check;
    if (check) {
      const lines = [check.passed === true ? "本次短请求按要求完成。" : "本次检查尚未通过，请按返回信息处理后再试。"];
      if (check.model) lines.push(`请求型号：${safeText(check.model)}`);
      if (check.http_status) lines.push(`HTTP 状态：${safeText(check.http_status)}`);
      if (check.summary) lines.push(safeText(check.summary));
      if (!currentCheck) lines.push("此记录对应之前的型号；请检查当前选中的模型。");
      lines.push("短请求通过后，请在目标 Agent 中完成一次实际任务。");
      $("check-result").textContent = lines.join("\n");
      $("check-result").className = `result-box${passed ? "" : " warning"}`;
    }
    badge("api-badge", selected ? "接入信息已准备" : "等待选择模型", selected ? "success" : "neutral");
    badge("codex-badge", codex.desktop_verified ? "桌面任务已核对" : codex.restore_available ? "有本面板配置记录 · 待核对" : "可选接入 · 未读取配置", codex.desktop_verified ? "success" : codex.restore_available ? "warning" : "neutral");
    $("codex-detail").textContent = codex.desktop_verified ? "已记录桌面端实际任务结果。保留该任务的真实产物与验证记录。" : codex.restore_available ? "保留了本面板的配置恢复记录；当前配置未自动读取。请在客户端核对接入并完成实际任务。" : "仅在你选择预览、添加或恢复时访问 Codex 配置。选择模型后可生成配置预览。";
    $("api-address").textContent = safeText(snapshot.api?.base_url, "等待服务启动");
    $("api-model").textContent = safeText(selected, "尚未选择");
    $("api-key-state").textContent = snapshot.api?.key_present ? "已生成 · 点击下方按钮复制" : "尚未配置";
    $("notice").hidden = !snapshot.notice;
    $("notice").textContent = safeText(snapshot.notice);
    updateButtons();
  }

  async function refreshState() {
    refreshing = true; updateButtons();
    try {
      snapshot = await request("/api/state");
      if (!csrfToken) {
        const csrf = await request("/api/csrf");
        if (typeof csrf.token !== "string" || !csrf.token) throw new Error("页面验证信息尚未就绪，请刷新页面。");
        csrfToken = csrf.token;
      }
      render();

    } catch (error) {
      badge("connection-state", "连接待恢复", "warning");
      csrfToken = "";
      feedback(error.message, "error");
      throw error;
    } finally { refreshing = false; updateButtons(); }
  }

  function showPreview(result, model) {
    const source = Object.prototype.hasOwnProperty.call(result, "before_sha256") ? result : result.preview;
    if (!source || typeof source !== "object" || !Object.prototype.hasOwnProperty.call(source, "before_sha256")) {
      previewHashKnown = false;
      throw new Error("预览未包含原配置校验信息，暂不能应用。请重新准备配置。");
    }
    previewHash = source.before_sha256;
    if (previewHash !== null && typeof previewHash !== "string") {
      previewHashKnown = false;
      throw new Error("原配置校验信息格式不正确，暂不能应用。");
    }
    previewHashKnown = true;
    previewModel = model;
    $("config-review").hidden = false;
    $("config-preview").hidden = false;
    $("preview-content").textContent = displayObject(result.preview ?? result);
  }

  async function mutate(path, payload, button, successMessage) {
    if (busy || !csrfToken) return;
    busy = true; activeButton = button; button?.setAttribute("aria-busy", "true"); updateButtons();
    const label = actionNames[path] || "操作";
    feedback(`${label}中，请保持页面打开…`);
    try {
      if ((path === "/api/check" || path === "/api/codex/preview") && payload.model !== snapshot?.selected_model) {
        await request("/api/models/select", { model: payload.model });
      }
      const result = await request(path, payload);
      if (path === "/api/runtime/setup") {
        const prepared = result.runtime || result;
        if (prepared.status === "missing") {
          runtimeInstallHint = { ...prepared, instructions: prepared.instructions || "请先安装 Antigravity Tools 5.0.3，完成后回到这里点击“安装后重试”。" };
          record(label, "等待安装组件");
          await refreshState();
          feedback("请先安装运行组件，完成后点击“安装后重试”。", "info");
          return;
        }
        runtimeInstallHint = null;
        feedback("组件已准备好，正在启动本机服务…");
        await request("/api/runtime/start", {});
      }
      if (path === "/api/codex/preview") showPreview(result, payload.model);
      if (path === "/api/check") {
        await refreshState();
        if (snapshot.last_check?.passed === true && snapshot.last_check.model === payload.model) {
          successMessage = "短请求已通过，可以把接入信息填入目标 Agent。";
        }
      }
      if (path === "/api/codex/apply" || path === "/api/codex/restore") {
        $("codex-receipt").hidden = false;
        $("codex-receipt").textContent = path.endsWith("restore") ? "恢复操作已完成。重新打开 Codex 后核对当前模型来源。" : "配置已添加，当前默认模型保持不变。请重新打开 Codex，在设置的网关中启用 Gemini Agent Kit，再新建会话选择模型并完成真实任务。";
        previewModel = null; previewHash = null; previewHashKnown = false; $("config-preview").hidden = true; $("config-review").hidden = true;
      }
      record(label, path === "/api/check" ? "检查已结束，见本轮结果" : "操作完成");
      await refreshState();
      feedback(successMessage || `${label}已完成。`, "success");
      if (path === "/api/check" && snapshot.last_check?.passed !== true) feedback("检查已结束，本次未通过。请查看检查结果；不会自动重试。", "info");
    } catch (error) {
      record(label, "未完成"); feedback(error.message, "error");
      // A failed request can still have produced a state change on the server.
      // Do not replay it. The user can explicitly refresh the read-only state.
    } finally {
      busy = false; activeButton?.removeAttribute("aria-busy"); activeButton = null; updateButtons();
    }
  }

  $("refresh-state").addEventListener("click", () => { if (!busy && !refreshing) refreshState().catch(() => {}); });
  $("setup-runtime").addEventListener("click", (event) => mutate(runtimeRunning() ? "/api/runtime/start" : "/api/runtime/setup", {}, event.currentTarget, "服务准备与启动操作已完成，请核对显示的状态。"));
  $("stop-runtime").addEventListener("click", (event) => {
    if (busy || !confirm("停止本工具管理的服务？\n正在使用它的 Agent 请求可能中断，请先结束相关任务。")) return;
    mutate("/api/runtime/stop", {}, event.currentTarget, "停止服务操作已完成。");
  });
  $("refresh-token").addEventListener("input", updateButtons);
  $("import-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || !csrfToken || !runtimeRunning()) return;
    const token = $("refresh-token").value.trim();
    $("refresh-token").value = "";
    if (!token) return;
    mutate("/api/accounts", { refresh_token: token }, $("import-account"), "账号已导入。请核对显示的权益和模型权限。");
  });
  $("model-select").addEventListener("change", (event) => {
    previewModel = null; previewHash = null; previewHashKnown = false;
    $("config-preview").hidden = true; $("config-review").hidden = true;
    if (modelValue()) mutate("/api/models/select", { model: modelValue() }, event.currentTarget, "模型已保存。复制接入信息，再填写到目标 Agent。");
    else updateButtons();
  });
  $("check-model").addEventListener("click", (event) => mutate("/api/check", { model: modelValue() }, event.currentTarget, "检查已结束，请查看本轮结果。"));
  $("preview-codex").addEventListener("click", (event) => mutate("/api/codex/preview", { model: modelValue() }, event.currentTarget, "预览已生成。确认后再添加配置。"));
  $("apply-codex").addEventListener("click", (event) => {
    if (!previewHashKnown || previewModel !== modelValue()) return;
    mutate("/api/codex/apply", { model: modelValue(), activate: false, expected_before_sha256: previewHash }, event.currentTarget, "Codex 配置已添加并备份原配置；还需在客户端完成实测。");
  });
  $("restore-codex").addEventListener("click", (event) => {
    if (busy || !confirm("恢复本工具添加配置前的 Codex 配置？\n若文件之后被修改，服务会先检查，避免覆盖新的设置。")) return;
    mutate("/api/codex/restore", {}, event.currentTarget, "恢复操作已完成，请重新打开 Codex 核对。");
  });
  $("copy-connection").addEventListener("click", async () => {
    if (busy || refreshing || !csrfToken || !snapshot?.api?.base_url || !snapshot?.selected_model || modelValue() !== snapshot.selected_model) {
      feedback("当前型号尚未保存或状态未确认，请刷新后再复制。", "info");
      return;
    }
    const text = `接口类型：OpenAI 兼容\nBase URL：${snapshot?.api?.base_url || ""}\nModel ID：${snapshot?.selected_model || ""}\nAPI Key：请在本机面板点击“复制 API Key”，直接粘进客户端设置。\n请只给我指定的 Agent 添加此服务，保留已有来源和默认设置。此地址仅供同一台电脑使用。`;
    try { await navigator.clipboard.writeText(text); feedback("已复制接入信息。API Key 请单独复制到目标客户端。", "success"); }
    catch (_) { feedback("浏览器未允许复制。可以在上方选中地址和型号手动复制。", "info"); }
  });
  $("show-codex-options").addEventListener("click", () => { $("advanced").open = true; });
  $("copy-api-key").addEventListener("click", async (event) => {
    if (busy || !csrfToken) return;
    const button = event.currentTarget;
    busy = true; button.setAttribute("aria-busy", "true"); updateButtons();
    try {
      // Fetch a credential only after this explicit click. Never render or log it.
      const connection = await request("/api/connection", {});
      if (typeof connection.api_key !== "string" || !connection.api_key) throw new Error("missing key");
      await navigator.clipboard.writeText(connection.api_key);
      feedback("API Key 已复制。请直接粘进目标客户端的密钥框，不要发进聊天。", "success");
    } catch (_) {
      feedback("未能复制 API Key。请让本机 Agent 从本工具的私有状态文件读取并填写客户端；不要把密钥发进聊天。", "info");
    } finally {
      busy = false; button.removeAttribute("aria-busy"); updateButtons();
    }
  });
  window.addEventListener("pagehide", () => { $("refresh-token").value = ""; csrfToken = ""; });
  refreshState().catch(() => {});
})();
