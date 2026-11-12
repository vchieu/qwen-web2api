/* Qwen Web2API UI bridge.
   Khong tu fetch completions nua (payload do app tu gui).
   - Chon temporary chat / model trong UI theo job (best-effort, warn neu loi).
   - Tu dien composer, bam send (hoac Enter).
   - Hook fetch/XHR de bat SSE tu chinh request cua app.
   - Day delta ve proxy local.
   Giu tab chat.qwen.ai mo, dang nhap. GET /api/v2/models chi de do danh
   sach model cho picker; temporary mode co the navigate mot lan. */
(() => {
  if (window.__qwenUIBridge) return;
  window.__qwenUIBridge = true;

  const BRIDGE = "http://127.0.0.1:8083";
  const BASE = location.origin;
  const MAX_JOBS = 1;
  const NEW_CHAT_EACH_JOB = true;   // thu bam New Chat truoc moi job
  const SEND_TIMEOUT_MS = 180000;
  const COMPOSER_WAIT_MS = 15000;
  const MODEL_REFRESH_MS = 10 * 60 * 1000;
  const MODE_CHECK_MS = 5000;
  const MODE_NAV_KEY = "qwenUIBridgeModeNav";
  const MODE_NAV_WINDOW_MS = 60000;
  const MODE_NAV_MAX = 2;

  let active = 0;
  let navigating = false;
  let current = null;
  let knownModels = [];
  let lastModelRefresh = 0;
  let lastModeCheck = 0;
  let selectModelEnabled = true;

  const log = (...a) => console.log("[qwen-ui-bridge]", ...a);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function post(path, body) {
    const r = await fetch(BRIDGE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json();
  }

  async function send(jobId, ev) {
    try {
      await post("/event", Object.assign({ job_id: jobId }, ev));
    } catch (e) { /* proxy chet */ }
  }

  function settle(job, err) {
    if (!job || job.settled) return;
    job.settled = true;
    if (err) {
      const msg = String((err && err.message) || err);
      log("job error:", msg);
      send(job.id, { kind: "error", message: msg }).finally(() => job.reject(err));
    } else {
      send(job.id, { kind: "done" }).finally(() => job.resolve());
    }
  }

  // ----------------------------------------- models / temporary-chat mode --
  function textOf(el) {
    if (!el) return "";
    return String(el.getAttribute("aria-label") || el.getAttribute("title")
      || el.innerText || el.textContent || "").trim();
  }

  function normLabel(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/\.0\b/g, "")
      .replace(/[^a-z0-9]+/g, "");
  }

  function labelMatches(targets, text) {
    const n = normLabel(text);
    if (!n) return false;
    return (targets || []).some((t) => {
      const tn = normLabel(t);
      return tn && (n === tn || n.includes(tn));
    });
  }

  function pressEscape() {
    const opts = { key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true };
    const el = document.activeElement || document.body;
    if (!el) return;
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  async function getJSON(path) {
    const r = await fetch(BRIDGE + path);
    if (!r.ok) throw new Error("bridge " + path + " -> " + r.status);
    return r.json();
  }

  async function readStatus() {
    const st = await getJSON("/status");
    if (st && typeof st.select_model === "boolean") selectModelEnabled = st.select_model;
    return st || {};
  }

  // Throttled: lay danh sach model tu chinh API cua trang (chi de do picker).
  async function refreshModels(force) {
    const now = Date.now();
    if (!force && now - lastModelRefresh < MODEL_REFRESH_MS) return knownModels;
    try {
      const r = await origFetch(BASE + "/api/v2/models", {
        headers: { accept: "application/json" },
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error("models -> " + r.status);
      const data = await r.json();
      const src = data && data.data;
      const list = Array.isArray(src)
        ? src
        : (src && (src.data || src.models || src.list));
      const out = [];
      for (const m of Array.isArray(list) ? list : []) {
        if (!m || typeof m.id !== "string" || !m.id.trim()) continue;
        const id = m.id.trim();
        const name = (typeof m.name === "string" && m.name.trim()) || id;
        out.push({ id: id, name: name });
      }
      if (out.length) {
        knownModels = out;
        lastModelRefresh = now;
      }
    } catch (e) {
      log("model refresh skipped:", (e && e.message) || e);
    }
    return knownModels;
  }

  function modelTargets(model) {
    if (!model) return [];
    const raw = String(model).split("@think=")[0].trim();
    if (!raw) return [];
    const targetNorm = normLabel(raw);
    const labels = [raw];
    for (const m of knownModels) {
      if (!m || !m.id) continue;
      if (m.id === raw || normLabel(m.id) === targetNorm) {
        labels.push(m.id);
        if (m.name) labels.push(m.name);
      }
    }
    const out = [];
    for (const l of labels) {
      const t = String(l || "").trim();
      if (!t) continue;
      if (!out.includes(t)) out.push(t);
      const n = normLabel(t);
      if (n && !out.includes(n)) out.push(n);
    }
    return out;
  }

  function findModelTrigger(targets) {
    const nodes = Array.from(document.querySelectorAll(
      'button, [role="button"], [role="combobox"], [aria-haspopup], [class*="model" i]'
    ));
    const norms = (targets || []).map(normLabel).filter(Boolean);
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = textOf(el);
      if (!label) continue;
      const low = label.toLowerCase();
      if (/^(send|stop|gửi|dừng)/i.test(low)) continue;
      const n = normLabel(label);
      let score = 0;
      if (norms.includes(n)) score += 5;
      else if (norms.some((t) => t && n.includes(t)
        && Math.abs(n.length - t.length) <= 3)) score += 3;
      const hint = ((el.getAttribute("aria-label") || "") + " "
        + (el.getAttribute("title") || "")).toLowerCase();
      if (/model|mô hình/.test(hint)) score += 2;
      if (/^(qwen|qwq)/.test(n)) score += 2;
      if (el.closest("form")) score -= 2;
      if (el.matches('button, [role="button"], [role="combobox"], [aria-haspopup]')) score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 3 ? best : null;
  }

  // Chon model theo job.model. Loi UI chi warn, request van chay tiep.
  async function selectModel(job) {
    try {
      if (!job || !job.model || selectModelEnabled === false) return true;
      const targets = modelTargets(job.model);
      if (!targets.length) return true;
      const trigger = findModelTrigger(targets);
      if (!trigger) {
        send(job.id, { kind: "warn",
          message: "khong tim thay model picker de chon " + job.model });
        return false;
      }
      if (labelMatches(targets, textOf(trigger))) return true;

      trigger.click();
      await sleep(600);
      const norms = targets.map(normLabel).filter(Boolean);
      const options = Array.from(document.querySelectorAll(
        '[role="option"], [role="menuitem"], [role="menuitemradio"], li, button, [role="button"]'
      )).filter((el) => visible(el)
        && !trigger.contains(el) && !el.contains(trigger));
      let best = null, bestScore = 0;
      for (const el of options) {
        const n = normLabel(textOf(el));
        if (!n) continue;
        let score = 0;
        if (norms.includes(n)) score += 5;
        else if (norms.some((t) => n.startsWith(t) || n.includes(t))) score += 4;
        else if (norms.some((t) => t.length >= 5 && n.length >= 4 && t.includes(n))) score += 2;
        if (el.closest("form")) score -= 1;
        if (score > bestScore) { best = el; bestScore = score; }
      }
      if (!best) {
        pressEscape();
        send(job.id, { kind: "warn",
          message: "khong chon duoc model " + job.model + " trong UI" });
        return false;
      }
      best.click();
      await sleep(600);
      const ok = labelMatches(targets, textOf(trigger));
      if (!ok) {
        send(job.id, { kind: "warn",
          message: "UI chua hien model " + job.model + " sau khi chon" });
      }
      return ok;
    } catch (e) {
      send(job.id, { kind: "warn",
        message: "model select error: " + ((e && e.message) || e) });
      return false;
    }
  }

  function isTemporaryPage() {
    return /[?&]temporary-chat=true/.test(location.search);
  }

  function modeUrl(want) {
    return BASE + (want ? "/?temporary-chat=true" : "/");
  }

  // Gioi han so lan tu navigate de doi temporary mode: tranh reload lap vo han
  // khi app tu xoa ?temporary-chat=true khoi URL.
  function allowIdleModeNav() {
    const now = Date.now();
    try {
      const prev = JSON.parse(sessionStorage.getItem(MODE_NAV_KEY) || "{}") || {};
      const recent = prev.at && now - prev.at < MODE_NAV_WINDOW_MS;
      const n = recent ? (prev.n || 0) : 0;
      if (n >= MODE_NAV_MAX) return false;
      sessionStorage.setItem(MODE_NAV_KEY, JSON.stringify({ at: now, n: n + 1 }));
      return true;
    } catch (e) {
      return true;
    }
  }

  // Doi temporary mode truoc khi gui. Tra ve false neu phai navigate lai.
  async function ensureMode(job) {
    try {
      const st = await readStatus();
      if (typeof st.temporary_chat !== "boolean") return true;
      const want = st.temporary_chat;
      if (want === isTemporaryPage()) return true;
      job.mode_retries = (job.mode_retries || 0) + 1;
      if (job.mode_retries > 2) {
        send(job.id, { kind: "warn",
          message: "khong doi duoc temporary_chat=" + want
            + "; tiep tuc voi che do hien tai" });
        return true;
      }
      try {
        job.raw.mode_retries = job.mode_retries;
        await post("/requeue", { job: job.raw });
      } catch (e) { /* best-effort */ }
      navigating = true;
      location.assign(modeUrl(want));
      return false;
    } catch (e) {
      log("mode check skipped:", (e && e.message) || e);
      return true;
    }
  }

  // Khi ranh: bao dam tab dang o dung temporary mode (khong co job de requeue).
  async function idleModeCheck() {
    const now = Date.now();
    if (now - lastModeCheck < MODE_CHECK_MS) return true;
    lastModeCheck = now;
    try {
      const st = await readStatus();
      if (typeof st.temporary_chat !== "boolean") return true;
      if (st.temporary_chat === isTemporaryPage()) {
        try { sessionStorage.removeItem(MODE_NAV_KEY); } catch (e) { /* best-effort */ }
        return true;
      }
      if (!allowIdleModeNav()) {
        log("temporary mode khong giu duoc sau " + MODE_NAV_MAX
          + " lan navigate; dung thu lai");
        return true;
      }
      navigating = true;
      location.assign(modeUrl(st.temporary_chat));
      return false;
    } catch (e) {
      log("idle mode check skipped:", (e && e.message) || e);
      return true;
    }
  }

  function isCompletionURL(u) {
    return typeof u === "string" && u.includes("/api/v2/chat/completions");
  }

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url;
    return String(input);
  }

  // Qwen đôi khi gọi tool bằng native function_call thay vì text fence.
  // Gom name + arguments (Qwen stream cumulative) rồi trả cho proxy để client
  // tự chạy tool, KHÔNG để Qwen tự chạy tool trong sandbox của nó.
  function collectNativeCall(job, fc) {
    if (!fc) return;
    const name = typeof fc.name === "string" ? fc.name : "";
    const raw = typeof fc.arguments === "string"
      ? fc.arguments
      : (fc.arguments ? JSON.stringify(fc.arguments) : "");
    if (!Array.isArray(job._nativeCalls)) job._nativeCalls = [];
    let call = job._nativeCalls[job._nativeCalls.length - 1];
    // Tên mới => call mới (Qwen stream tuần tự từng call)
    if (!call || (name && call.name && call.name !== name)) {
      call = { name: name, arguments: "" };
      job._nativeCalls.push(call);
    } else if (name) {
      call.name = name;
    }
    if (!raw) return;
    const prev = call.arguments || "";
    if (!prev) call.arguments = raw;
    else if (raw.startsWith(prev)) call.arguments = raw;   // cumulative
    else call.arguments = prev + raw;                      // incremental
  }

  async function flushNativeCalls(job) {
    const calls = job._nativeCalls || [];
    job._nativeCalls = [];
    const ready = calls.filter((c) => c && c.name);
    if (!ready.length) return false;
    for (const c of ready) {
      await send(job.id, {
        kind: "tool_call",
        name: c.name,
        arguments: c.arguments || "{}",
      });
    }
    settle(job, null);
    return true;
  }

  async function handleData(job, data) {
    if (!job || job.settled) return;
    if (!data || data === "[DONE]") {
      if (data === "[DONE]" && !(await flushNativeCalls(job))) settle(job, null);
      return;
    }
    let obj;
    try { obj = JSON.parse(data); } catch (e) { return; }
    if (obj.success === false) {
      const d = obj.data || {};
      throw new Error(d.details || d.code || JSON.stringify(obj).slice(0, 300));
    }
    if (obj.ret) throw new Error(JSON.stringify(obj).slice(0, 300));
    if (obj.usage) await send(job.id, { kind: "usage", usage: obj.usage });

    // Khởi tạo biến lưu text thinking cũ để tránh gửi trùng lặp (do Qwen hay gửi cumulative array)
    if (typeof job._lastThought !== "string") job._lastThought = "";

    for (const ch of obj.choices || []) {
      const d = ch.delta || ch.message || {};
      const phase = d.phase || "";
      const status = d.status || "";

      // 0. Native function_call: Qwen đang gọi tool bằng structured call.
      // Chỉ can thiệp khi prompt có block "# Tool Use" (client khai báo tools);
      // function_call của tool built-in (web search...) để Qwen tự xử lý.
      if (d.function_call) {
        if (job.toolsActive) collectNativeCall(job, d.function_call);
        continue;
      }

      // Qwen tự chạy tool và trả kết quả ở role="function" (thường là
      // "Tool X does not exists."). Không forward rác này; trả call đã gom
      // cho client thay vì để Qwen xử lý tiếp.
      if (d.role === "function") {
        if (job.toolsActive) await flushNativeCalls(job);
        continue;
      }

      if (status === "finished" && (phase === "answer" || !phase)
          && job.toolsActive && Array.isArray(job._nativeCalls)
          && job._nativeCalls.length) {
        await flushNativeCalls(job);
        continue;
      }

      // 1. Lấy Full Thinking (nếu Qwen trả về phase="thinking" hoặc có reasoning_content)
      if (typeof d.reasoning_content === "string" && d.reasoning_content) {
        await send(job.id, { kind: "delta", phase: "reasoning", text: d.reasoning_content });
      } else if ((phase === "thinking" || phase === "think") && typeof d.content === "string" && d.content) {
        await send(job.id, { kind: "delta", phase: "reasoning", text: d.content });
      }

      // 2. Lấy Summary Thinking (phase="thinking_summary")
      if (phase === "thinking_summary" && d.extra) {
        let thoughtText = "";
        if (d.extra.summary_thought && Array.isArray(d.extra.summary_thought.content)) {
          thoughtText = d.extra.summary_thought.content.join("");
        } else if (typeof d.extra.summary_thought === "string") {
          thoughtText = d.extra.summary_thought;
        }
        
        // Lấy thêm Title (nếu có) để làm tiêu đề in đậm
        let titleText = "";
        if (d.extra.summary_title && Array.isArray(d.extra.summary_title.content)) {
          titleText = d.extra.summary_title.content.join("");
        }

        if (thoughtText && thoughtText !== job._lastThought) {
          // Chỉ gửi phần text mới (delta) để tránh client bị lặp chữ
          let delta = thoughtText.slice(job._lastThought.length);
          job._lastThought = thoughtText;
          
          if (delta) {
            // Nếu đây là chunk đầu tiên và có title, prepend title vào
            if (job._lastThought === thoughtText && titleText && !job._titleSent) {
               delta = `**${titleText}**\n\n${delta}`;
               job._titleSent = true;
            }
            await send(job.id, { kind: "delta", phase: "reasoning", text: delta });
          }
        }
      }

      // 3. Lấy Final Answer (phase="answer" hoặc không có phase)
      const isAnswer = phase === "answer" || phase === "" || !phase;
      if (isAnswer) {
        if (typeof d.content === "string" && d.content) {
          await send(job.id, { kind: "delta", phase: "content", text: d.content });
        } else if (Array.isArray(d.content)) {
          for (const p of d.content) {
            if (p && typeof p.text === "string" && p.text) {
              await send(job.id, { kind: "delta", phase: "content", text: p.text });
            }
          }
        }
      }
    }
  }

  async function pumpFetch(job, resp) {
    const ctype = resp.headers.get("content-type") || "";
    if (!ctype.includes("event-stream")) {
      const text = await resp.text();
      throw new Error("non-SSE (" + resp.status + "): " + text.slice(0, 400));
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (job.settled) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line.startsWith("data:")) continue;
        await handleData(job, line.slice(5).trim());
      }
    }
    settle(job, null);
  }

  async function parseSSEText(job, text) {
    for (const raw of String(text || "").split(/\r?\n/)) {
      const line = raw.trim();
      if (!line.startsWith("data:")) continue;
      await handleData(job, line.slice(5).trim());
      if (job.settled) return;
    }
    settle(job, null);
  }

  // Hook fetch: bat response cua chinh app, khong can doc DOM.
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const resp = await origFetch.apply(this, arguments);
    try {
      if (current && !current.attached && isCompletionURL(urlOf(input))) {
        current.attached = true;
        const clone = resp.clone();
        pumpFetch(current, clone).catch((e) => settle(current, e));
      }
    } catch (e) { /* khong anh huong app */ }
    return resp;
  };

  // Hook XHR phong khi app doi sang XHR.
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__qwenURL = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (current && !current.attached && isCompletionURL(this.__qwenURL)) {
      current.attached = true;
      const job = current;
      this.addEventListener("load", () => {
        parseSSEText(job, this.responseText).catch((e) => settle(job, e));
      });
    }
    return origSend.apply(this, arguments);
  };

  function visible(el) {
    if (!el) return false;
    const r = el.getClientRects();
    if (!r.length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none";
  }

  function findComposers() {
    const nodes = Array.from(document.querySelectorAll(
      'textarea, [contenteditable="true"], [role="textbox"]'
    ));
    return nodes.filter((el) => {
      if (!visible(el)) return false;
      if (el.disabled || el.readOnly) return false;
      if (el.getAttribute("aria-disabled") === "true") return false;
      return true;
    });
  }

  async function waitComposer() {
    const deadline = Date.now() + COMPOSER_WAIT_MS;
    while (Date.now() < deadline) {
      const list = findComposers();
      if (list.length) {
        // Uu tien o co placeholder lien quan chat/message.
        const scored = list.map((el) => {
          const ph = (el.getAttribute("placeholder") || el.getAttribute("aria-label") || "").toLowerCase();
          const score = /ask|message|chat|qwen|nhap|tin nhan|prompt/.test(ph) ? 1 : 0;
          return { el, score };
        });
        scored.sort((a, b) => b.score - a.score);
        return scored[0].el;
      }
      await sleep(200);
    }
    throw new Error("khong tim thay o nhap chat (composer)");
  }

  function setComposer(el, text) {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: text,
      }));
      return;
    }
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, text);
    else el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function findNewChatButton() {
    const rx = /new\s*chat|new\s*conversation|chat\s*mới|cuộc\s*trò\s*chuyện\s*mới|tạo\s*cuộc|new\s*chat/i;
    const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = (el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent || "").trim();
      let score = 0;
      if (rx.test(label)) score += 3;
      if (el.tagName === "A" && el.getAttribute("href") === "/") score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 2 ? best : null;
  }

  async function tryNewChat() {
    if (!NEW_CHAT_EACH_JOB) return;
    const btn = findNewChatButton();
    if (!btn) {
      log("khong tim thay nut New Chat; se gui trong chat hien tai");
      return;
    }
    btn.click();
    await sleep(1200);
  }

  function findSendButton(composer) {
    const scope = composer.closest("form") || document;
    const nodes = Array.from(scope.querySelectorAll('button, [role="button"]'));
    const rx = /send|gửi|gui|submit|arrow/i;
    let best = null, bestScore = 0;
    for (const el of nodes) {
      if (!visible(el)) continue;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") continue;
      const label = (el.getAttribute("aria-label") || el.getAttribute("title")
        || el.getAttribute("data-testid") || el.textContent || "").toLowerCase();
      let score = 0;
      if (el.type === "submit") score += 3;
      if (rx.test(label)) score += 3;
      if (el.querySelector("svg")) score += 1;
      if (score > bestScore) { best = el; bestScore = score; }
    }
    return bestScore >= 3 ? best : null;
  }

  function pressEnter(el) {
    const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true };
    el.dispatchEvent(new KeyboardEvent("keydown", opts));
    el.dispatchEvent(new KeyboardEvent("keypress", opts));
    el.dispatchEvent(new KeyboardEvent("keyup", opts));
  }

  async function submitPrompt(composer, prompt) {
    setComposer(composer, prompt);
    await sleep(350);
    const btn = findSendButton(composer);
    if (btn) {
      btn.click();
      return;
    }
    pressEnter(composer);
    await sleep(500);
    const btn2 = findSendButton(composer);
    if (btn2) btn2.click();
  }

  function composerText(el) {
    if (!el) return "";
    if (el.isContentEditable) return String(el.innerText || el.textContent || "");
    return String(el.value || "");
  }

  // Chụp nhanh các node trông giống attachment để biết paste/upload có tạo file mới.
  function attachmentSnapshot() {
    const sel = '[class*="file" i], [class*="attach" i], [class*="upload" i], '
      + '[class*="document" i], [class*="doc" i], [class*="chip" i], '
      + '[data-testid*="file" i], [data-testid*="attach" i], '
      + '[data-testid*="document" i]';
    return new Set(Array.from(document.querySelectorAll(sel)).filter(visible));
  }

  // Chờ Qwen tạo attachment từ text vừa paste. Ngoài việc xuất hiện node mới,
  // còn dấu hiệu text trong composer bị rút ngắn (paste -> document).
  async function waitForAttachment(composer, before, pastedLen, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const added = Array.from(attachmentSnapshot()).some((n) => !before.has(n));
      if (added) return true;
      if (pastedLen > 500) {
        const now = composerText(composer).trim().length;
        if (now < Math.max(200, pastedLen / 2)) return true;
      }
      await sleep(400);
    }
    return false;
  }

  // Xóa sạch composer (contenteditable lẫn input/textarea).
  function clearComposer(el) {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("delete", false, null);
      if (composerText(el).trim()) {
        el.textContent = "";
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          inputType: "deleteContentBackward",
        }));
      }
      return;
    }
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, "");
    else el.value = "";
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Thay text cũ bằng prompt ngắn; editor đôi khi giữ text cũ sau lần đầu nên
  // kiểm tra độ dài và xóa lại nếu cần.
  function setComposerClean(el, text) {
    clearComposer(el);
    setComposer(el, text);
    if (composerText(el).trim().length > text.length + 200) {
      clearComposer(el);
      setComposer(el, text);
    }
  }

  // Qwen UI biến text dài được paste thành document/txt. Mô phỏng đúng thao tác
  // Ctrl-V thay vì chèn file bằng tay (React thường bỏ qua file inject).
  async function pasteLongText(composer, text) {
    composer.focus();
    try {
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      composer.dispatchEvent(new ClipboardEvent("paste", {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
      }));
    } catch (e) {
      log("paste event failed:", e);
    }
    await sleep(2000);
  }

  async function fetchPromptText(fileInfo) {
    const response = await fetch(`${BRIDGE}/file/${encodeURIComponent(fileInfo.filename)}`);
    if (!response.ok) {
      throw new Error("khong fetch duoc file tu bridge: " + response.status);
    }
    return await response.text();
  }

  // Fallback: đẩy file qua input[type=file] của Qwen UI.
  async function uploadFileViaUI(fileInfo, text) {
    log("uploading large prompt file:", fileInfo.filename);
    const file = new File([text], fileInfo.filename, { type: "text/plain" });

    let fileInput = document.querySelector('input[type="file"]');
    if (!fileInput) {
      // Click nút attachment (kẹp giấy) để UI sinh ra input[type="file"]
      const attachBtn = document.querySelector(
        '[aria-label*="Attach" i], [aria-label*="Đính kèm" i], [aria-label*="dính kèm" i], [data-testid*="attach" i]'
      );
      if (attachBtn) {
        attachBtn.click();
        await sleep(500);
        fileInput = document.querySelector('input[type="file"]');
      }
    }
    if (!fileInput) {
      log("khong tim thay input[type=file] cua Qwen UI");
      return false;
    }

    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "files"
    )?.set;
    if (nativeInputValueSetter) {
      nativeInputValueSetter.call(fileInput, dataTransfer.files);
    } else {
      fileInput.files = dataTransfer.files;
    }
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    fileInput.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(2000);
    return true;
  }

  // Prompt quá dài: paste để Qwen tự tạo document txt; nếu UI bỏ qua paste thì
  // thử upload file, cuối cùng mới gửi nguyên text.
  async function attachLongPrompt(composer, fileInfo) {
    const text = await fetchPromptText(fileInfo);
    const before = attachmentSnapshot();

    await pasteLongText(composer, text);
    const pastedLen = composerText(composer).trim().length;
    log("large prompt pasted:", pastedLen, "chars in composer");

    let attached;
    if (pastedLen > 500) {
      // Paste đã chèn text: chờ Qwen chuyển thành document (có thể mất vài giây).
      attached = await waitForAttachment(composer, before, pastedLen, 20000);
    } else {
      // Không thấy text: hoặc Qwen đã chuyển ngay thành attachment, hoặc paste
      // bị bỏ qua. Chờ attachment ngắn, rồi thử upload file.
      attached = await waitForAttachment(composer, before, 0, 2500);
      if (!attached) {
        attached = await uploadFileViaUI(fileInfo, text);
        if (attached) {
          attached = await waitForAttachment(composer, before, 0, 15000);
        }
      }
    }

    if (attached) {
      // Toàn bộ prompt đã nằm trong document. Không gửi kèm text nào để Qwen
      // không tưởng phải gọi tool read cho tên file tạm qwen_prompt_*.txt.
      await sleep(800);
      setComposerClean(composer, "");
      log("large prompt attached; composer cleared");
    } else {
      // Không tạo được attachment: gửi trực tiếp toàn bộ prompt đã render
      // (file text đã chứa cả tool block + nội dung gốc).
      log("khong tao duoc attachment; gui truc tiep text dai");
      setComposer(composer, text);
    }
    await sleep(400);
  }

  // Chờ send button sẵn sàng (thường là sau khi upload xong) rồi bấm.
  // Nếu UI chưa nhận, thử lại vài lần.
  async function sendPrompt(composer, timeoutMs = 45000) {
    const deadline = Date.now() + timeoutMs;
    let clicks = 0;
    while (Date.now() < deadline) {
      const btn = findSendButton(composer);
      if (!btn) {
        await sleep(400);
        continue;
      }
      const hadText = !!composerText(composer).trim();
      btn.click();
      clicks++;
      log("send click", clicks);
      await sleep(1500);
      if (!findSendButton(composer)) return true;
      if (hadText && !composerText(composer).trim()) return true;
      if (clicks >= 15) break;
    }
    pressEnter(composer);
    return false;
  }

  async function runJob(job) {
    current = job;
    const timer = setTimeout(() => {
      settle(job, new Error("UI send timeout: khong bat duoc completion response"));
    }, SEND_TIMEOUT_MS);
    try {
      await tryNewChat();
      if (!(await ensureMode(job))) return;
      await selectModel(job);
      const composer = await waitComposer();

      if (job.file_info) {
        // Large prompt: paste/upload de Qwen tao document, roi gui attachment.
        await attachLongPrompt(composer, job.file_info);
        await sendPrompt(composer);
      } else {
        // Normal flow
        await submitPrompt(composer, job.prompt);
      }
      await job.promise;
    } catch (e) {
      settle(job, e);
    } finally {
      clearTimeout(timer);
      current = null;
    }
  }

  setInterval(() => { post("/event", { kind: "ping" }).catch(() => {}); }, 5000);

  async function loop() {
    log("started on", BASE, "->", BRIDGE);
    for (;;) {
      if (navigating) { await sleep(500); continue; }
      if (active >= MAX_JOBS) { await sleep(400); continue; }
      try {
        await refreshModels(false);
        if (!(await idleModeCheck())) { await sleep(500); continue; }
        const r = await post("/poll", { worker: location.host, models: knownModels });
        if (r && r.job) {
          active++;
          const job = {
            id: r.job.job_id,
            model: r.job.model,
            prompt: r.job.prompt,
            think: r.job.think,
            file_info: r.job.file_info,
            raw: r.job,
            mode_retries: r.job.mode_retries || 0,
            toolsActive: String(r.job.prompt || "").includes("# Tool Use"),
            attached: false,
            settled: false,
          };
          job.promise = new Promise((resolve, reject) => {
            job.resolve = resolve;
            job.reject = reject;
          });
          runJob(job).finally(() => { active--; });
        }
      } catch (e) {
        await sleep(2000);
      }
    }
  }

  window.__qwenUIBridgeDebug = () => {
    console.log("composers:", findComposers());
    console.log("new chat:", findNewChatButton());
    const c = findComposers()[0];
    console.log("send button:", c ? findSendButton(c) : null);
    console.log("temporary chat page:", isTemporaryPage());
    console.log("known models:", knownModels, "select enabled:", selectModelEnabled);
    const trigger = findModelTrigger(
      knownModels.map((m) => m.id || "").filter(Boolean));
    console.log("model trigger:", trigger, trigger ? textOf(trigger) : "");
  };

  loop();
})();