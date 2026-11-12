/* Qwen Web2API UI bridge.
   Khong dung fetch tu dung request nua.
   - Tu dien composer, bam send (hoac Enter).
   - Hook fetch/XHR de bat SSE tu chinh request cua app.
   - Day delta ve proxy local.
   Giu tab chat.qwen.ai mo, dang nhap, va chon model/thinking trong UI. */
(() => {
  if (window.__qwenUIBridge) return;
  window.__qwenUIBridge = true;

  const BRIDGE = "http://127.0.0.1:8083";
  const BASE = location.origin;
  const MAX_JOBS = 1;
  const NEW_CHAT_EACH_JOB = true;   // thu bam New Chat truoc moi job
  const SEND_TIMEOUT_MS = 180000;
  const COMPOSER_WAIT_MS = 15000;

  let active = 0;
  let current = null;

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

  function isCompletionURL(u) {
    return typeof u === "string" && u.includes("/api/v2/chat/completions");
  }

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url;
    return String(input);
  }

  async function handleData(job, data) {
    if (!job || job.settled) return;
    if (!data || data === "[DONE]") {
      if (data === "[DONE]") settle(job, null);
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

  async function runJob(job) {
    current = job;
    const timer = setTimeout(() => {
      settle(job, new Error("UI send timeout: khong bat duoc completion response"));
    }, SEND_TIMEOUT_MS);
    try {
      await tryNewChat();
      const composer = await waitComposer();
      await submitPrompt(composer, job.prompt);
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
      if (active >= MAX_JOBS) { await sleep(400); continue; }
      try {
        const r = await post("/poll", { worker: location.host });
        if (r && r.job) {
          active++;
          const job = {
            id: r.job.job_id,
            model: r.job.model,
            prompt: r.job.prompt,
            think: r.job.think,
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
  };

  loop();
})();