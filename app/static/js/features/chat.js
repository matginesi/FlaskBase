/* Chat UI + queue orchestration with full-response rendering (no streaming effect). */
(function () {
  "use strict";

  const escapeHtml = window.AppUtils?.escapeHtml || String;
  const fmtSeconds = window.AppUtils?.fmtSeconds || (() => null);
  const sanitizeUrl = window.AppUtils?.sanitizeUrl || (() => "#");

  function highlightCode(code, lang) {
    const src = String(code || "");
    const language = String(lang || "").toLowerCase();

    const kwRe = /\b(?:and|as|async|await|break|case|catch|class|const|continue|def|default|del|do|elif|else|except|export|extends|finally|for|from|function|if|import|in|is|lambda|let|match|new|pass|raise|return|static|switch|throw|try|var|while|with|yield)\b/;
    const typeRe = /\b(?:Array|Boolean|Date|Dict|List|Map|Number|Object|Set|String|Tuple|any|bool|bytes|char|dict|float|int|list|number|object|str|string|void)\b/;
    const litRe = /\b(?:None|False|True|null|undefined|NaN|Infinity|this|self|super)\b/;
    const numRe = /\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b/;
    const fnRe = /^[A-Za-z_]\w*$/;
    const opRe = /^(?:===|!==|==|!=|<=|>=|=>|->|\+\+|--|\|\||&&|[-+*/%=&|!<>^~?:]+)$/;
    const puncRe = /^[()[\]{}.,;]$/;
    const tokenRe = /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/|\b(?:Array|Boolean|Date|Dict|List|Map|Number|Object|Set|String|Tuple|any|bool|bytes|char|dict|float|int|list|number|object|str|string|void)\b|\b(?:None|False|True|null|undefined|NaN|Infinity|this|self|super)\b|\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b|\b[A-Za-z_]\w*(?=\s*\()|\b(?:and|as|async|await|break|case|catch|class|const|continue|def|default|del|do|elif|else|except|export|extends|finally|for|from|function|if|import|in|is|lambda|let|match|new|pass|raise|return|static|switch|throw|try|var|while|with|yield)\b|===|!==|==|!=|<=|>=|=>|->|\+\+|--|\|\||&&|[-+*/%=&|!<>^~?:]+|[()[\]{}.,;]/g;

    let out = "";
    let last = 0;
    for (const m of src.matchAll(tokenRe)) {
      const idx = m.index || 0;
      const token = m[0] || "";
      if (idx > last) out += escapeHtml(src.slice(last, idx));

      let cls = "";
      if (token.startsWith('"') || token.startsWith("'") || token.startsWith("`")) cls = "str";
      else if (token.startsWith("//") || token.startsWith("/*") || token.startsWith("#")) cls = "com";
      else if (kwRe.test(token)) cls = "kw";
      else if (typeRe.test(token)) cls = "typ";
      else if (litRe.test(token)) cls = "lit";
      else if (numRe.test(token)) cls = "num";
      else if (fnRe.test(token)) cls = "fn";
      else if (opRe.test(token)) cls = "op";
      else if (puncRe.test(token)) cls = "punc";

      out += cls ? `<span class="chat-tk-${cls}">${escapeHtml(token)}</span>` : escapeHtml(token);
      last = idx + token.length;
    }

    if (last < src.length) out += escapeHtml(src.slice(last));
    if (language === "diff") {
      out = out
        .replace(/^(\+.*)$/gm, '<span class="chat-tk-add">$1</span>')
        .replace(/^(-.*)$/gm, '<span class="chat-tk-del">$1</span>');
    }

    return out;
  }

  function mdToHtml(markdown) {
    const src = String(markdown || "").replace(/\r\n?/g, "\n");
    const blocks = [];

    let txt = src.replace(/```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g, (_match, lang, code) => {
      const index = blocks.length;
      blocks.push({ lang: (lang || "").trim(), code: String(code || "") });
      return `@@CODEBLOCK_${index}@@`;
    });

    txt = escapeHtml(txt);
    txt = txt.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label, url) => {
      const safe = sanitizeUrl(url);
      const target = safe === "#" ? "" : ' target="_blank" rel="noopener noreferrer"';
      return `<a href="${safe}"${target}>${label}</a>`;
    });
    txt = txt.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    txt = txt.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    txt = txt.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    txt = txt.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    txt = txt.replace(/_([^_\n]+)_/g, "<em>$1</em>");

    const lines = txt.split("\n");
    const out = [];
    let inUl = false;
    let inOl = false;

    const closeLists = () => {
      if (inUl) {
        out.push("</ul>");
        inUl = false;
      }
      if (inOl) {
        out.push("</ol>");
        inOl = false;
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      if (!line.trim()) {
        closeLists();
        continue;
      }

      if (/^@@CODEBLOCK_\d+@@$/.test(line.trim())) {
        closeLists();
        out.push(line.trim());
        continue;
      }

      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        closeLists();
        const level = Math.min(3, heading[1].length);
        out.push(`<h${level}>${heading[2]}</h${level}>`);
        continue;
      }

      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        closeLists();
        out.push(`<blockquote>${quote[1] || "&nbsp;"}</blockquote>`);
        continue;
      }

      const ul = line.match(/^[-*+]\s+(.+)$/);
      if (ul) {
        if (inOl) {
          out.push("</ol>");
          inOl = false;
        }
        if (!inUl) {
          out.push("<ul>");
          inUl = true;
        }
        out.push(`<li>${ul[1]}</li>`);
        continue;
      }

      const ol = line.match(/^\d+\.\s+(.+)$/);
      if (ol) {
        if (inUl) {
          out.push("</ul>");
          inUl = false;
        }
        if (!inOl) {
          out.push("<ol>");
          inOl = true;
        }
        out.push(`<li>${ol[1]}</li>`);
        continue;
      }

      closeLists();
      out.push(`<p>${line}</p>`);
    }

    closeLists();

    let html = out.join("\n");
    html = html.replace(/@@CODEBLOCK_(\d+)@@/g, (_m, idxStr) => {
      const idx = parseInt(idxStr, 10);
      const block = blocks[idx];
      if (!block) return "";
      const lang = block.lang ? `<span class="chat-code-lang">${escapeHtml(block.lang)}</span>` : "";
      return `<pre class="chat-code"><code>${highlightCode(block.code, block.lang)}</code></pre>${lang}`;
    });

    return html || "<p></p>";
  }

  function initChat() {
    const chatForm = document.getElementById("chatForm");
    const chatInput = document.getElementById("chatInput");
    const chatMsgs = document.getElementById("chatMessages");
    if (!chatForm || !chatInput || !chatMsgs) return;

    const sendBtn = chatForm.querySelector("button[type=submit]");
    const stopBtn = document.getElementById("chatStopBtn");
    const chatEndpoint = (chatForm.dataset.endpoint || "/chat/send").trim();
    const startEndpoint = (chatForm.dataset.startEndpoint || "/chat/start").trim();
    const jobEndpointBase = (chatForm.dataset.jobEndpointBase || "/chat/job").trim().replace(/\/+$/, "");
    const userKey = (chatForm.dataset.userKey || "anon").trim();
    const storageKey = `chat_state_v3:${userKey}`;

    let activeJobId = null;
    let pollTimer = null;
    let pendingRef = null;
    let scrollRaf = 0;
    let autoStickBottom = true;

    function isNearBottom() {
      const delta = chatMsgs.scrollHeight - (chatMsgs.scrollTop + chatMsgs.clientHeight);
      return delta < 88;
    }

    function scheduleScrollBottom(force = false) {
      if (force) autoStickBottom = true;
      if (!force && !autoStickBottom && !isNearBottom()) return;
      if (scrollRaf) return;
      scrollRaf = window.requestAnimationFrame(() => {
        scrollRaf = 0;
        chatMsgs.scrollTop = chatMsgs.scrollHeight;
      });
    }

    function setBubbleContent(contentEl, text, markdown) {
      if (!contentEl) return;
      const raw = String(text || "");
      const mode = markdown ? "md" : "txt";
      if (contentEl.dataset.raw === raw && contentEl.dataset.mode === mode) return;
      if (markdown) contentEl.innerHTML = mdToHtml(raw);
      else contentEl.innerHTML = `<span class="ws-pre-wrap">${escapeHtml(raw)}</span>`;
      contentEl.dataset.raw = raw;
      contentEl.dataset.mode = mode;
    }

    function nowIso() {
      return new Date().toISOString();
    }

    function defaultState() {
      return { messages: [], activeJobId: null };
    }

    function loadState() {
      try {
        const raw = localStorage.getItem(storageKey);
        if (!raw) return defaultState();
        const parsed = JSON.parse(raw);
        if (!parsed || !Array.isArray(parsed.messages)) return defaultState();
        return {
          messages: parsed.messages.slice(-80),
          activeJobId: Number.isFinite(parsed.activeJobId) ? parsed.activeJobId : null,
        };
      } catch (_) {
        return defaultState();
      }
    }

    function saveState(state) {
      try {
        localStorage.setItem(storageKey, JSON.stringify(state));
      } catch (_) {
        // No-op: storage may be unavailable.
      }
    }

    function setBusy(isBusy) {
      if (!sendBtn) return;
      const canStop = !!isBusy && !!activeJobId;
      sendBtn.disabled = !!isBusy;
      sendBtn.classList.toggle("d-none", canStop);
      sendBtn.innerHTML = isBusy && !canStop
        ? '<span class="spinner-border spinner-border-sm"></span>'
        : '<i class="bi bi-send"></i>';
      if (stopBtn) {
        stopBtn.classList.toggle("d-none", !canStop);
        stopBtn.disabled = !canStop;
      }
    }

    function statusMeta(job) {
      const bits = [];
      const status = String(job.status || "queued");
      bits.push(status);
      if (Number.isFinite(job.progress)) bits.push(`${job.progress}%`);
      if (job.message) bits.push(String(job.message));
      return `Bot | ${bits.join(" | ")}`;
    }

    function finalMeta(result) {
      const bits = [];
      const sec = fmtSeconds(Number.isFinite(result.elapsed_ms) ? result.elapsed_ms : null);
      if (sec) bits.push(sec);
      if (Number.isFinite(result.total_tokens)) bits.push(`${result.total_tokens} tok`);
      if (Number.isFinite(result.tokens_per_sec)) bits.push(`${result.tokens_per_sec} tok/s`);
      bits.push((result.model || "model").toString());
      return `Bot | ${bits.join(" | ")}`;
    }

    function chatAppend(text, cls, user, opts = {}) {
      const thinking = typeof opts.thinking === "string" ? opts.thinking.trim() : "";
      const pageLanguage = document.documentElement?.lang || "en";
      const now = new Date().toLocaleTimeString(pageLanguage, { hour: "2-digit", minute: "2-digit" });
      const wrap = document.createElement("div");
      wrap.className = "chat-row " + (cls === "me" ? "is-me" : "is-other");

      const bubble = document.createElement("div");
      bubble.className = "chat-bubble " + cls;

      let thinkingBlock = "";
      if (thinking) {
        thinkingBlock = `<details class="chat-thinking"><summary>Thinking</summary><div class="chat-content chat-thinking-content">${mdToHtml(thinking)}</div></details>`;
      }

      const markdownEnabled = opts.markdown !== false && cls === "bot";
      const contentHtml = markdownEnabled
        ? mdToHtml(text)
        : `<span class="ws-pre-wrap">${escapeHtml(text)}</span>`;
      bubble.innerHTML = `<div class="chat-content">${contentHtml}</div>${thinkingBlock}<div class="bubble-meta">${opts.meta || `${user || ""} ${now}`}</div>`;
      const contentEl = bubble.querySelector(".chat-content");
      if (contentEl) {
        contentEl.dataset.raw = String(text || "");
        contentEl.dataset.mode = markdownEnabled ? "md" : "txt";
      }
      bubble.dataset.thinking = thinking;
      wrap.appendChild(bubble);
      const wait = document.createElement("div");
      wait.className = "chat-wait-indicator d-none fs-xs text-muted mt-1";
      wait.innerHTML = '<span class="spinner-border spinner-border-sm me-1 chat-spinner-xs"></span>Processing...';
      wrap.appendChild(wait);
      chatMsgs.appendChild(wrap);
      scheduleScrollBottom(true);
      return { wrap, bubble };
    }

    function chatSetPendingMeta(ref, label = "Thinking") {
      if (!ref || !ref.bubble) return;
      const meta = ref.bubble.querySelector(".bubble-meta");
      if (!meta) return;
      meta.innerHTML = `<span class="spinner-border spinner-border-sm me-1 chat-spinner-sm"></span>${label}`;
    }

    function chatUpdatePendingBubble(ref, text, opts = {}) {
      if (!ref || !ref.bubble) return;

      const contentEl = ref.bubble.querySelector(".chat-content");
      if (contentEl) {
        const markdownEnabled = opts.markdown !== false && ref.bubble.classList.contains("bot");
        setBubbleContent(contentEl, text || "", markdownEnabled);
      }

      const nextThinking = String(opts.thinking || "");
      const prevThinking = String(ref.bubble.dataset.thinking || "");
      if (nextThinking !== prevThinking) {
        ref.bubble.querySelectorAll(".chat-thinking").forEach((el) => el.remove());
      }
      if (nextThinking && nextThinking !== prevThinking) {
        const details = document.createElement("details");
        details.className = "chat-thinking";
        details.innerHTML = `<summary>Thinking</summary><div class="chat-content chat-thinking-content">${mdToHtml(nextThinking)}</div>`;
        if (contentEl) contentEl.insertAdjacentElement("afterend", details);
      }
      ref.bubble.dataset.thinking = nextThinking;

      const meta = ref.bubble.querySelector(".bubble-meta");
      if (meta && meta.textContent !== (opts.meta || "Bot")) meta.textContent = opts.meta || "Bot";
      scheduleScrollBottom();
    }

    function chatSetWaitIndicator(ref, waiting, label = "Sta pensando...") {
      if (!ref || !ref.wrap) return;
      const el = ref.wrap.querySelector(".chat-wait-indicator");
      if (!el) return;
      const safeLabel = String(label || "Sta pensando...");
      if (waiting) {
        el.innerHTML = `<span class="spinner-border spinner-border-sm me-1 chat-spinner-xs"></span>${escapeHtml(safeLabel)}`;
      }
      el.classList.toggle("d-none", !waiting);
    }

    function spinnerLabelFor(job, result) {
      const msg = String(job?.message || "").toLowerCase();
      if (msg.includes("generazione") || msg.includes("complet")) return "Finalizzazione risposta...";
      return "Sta pensando...";
    }

    function appendAndTrack(msg) {
      const cls = msg.role === "me" ? "me" : "bot";
      const ref = chatAppend(msg.text || "", cls, msg.role === "me" ? "Tu" : "Bot", {
        meta: msg.meta || undefined,
        thinking: msg.thinking || "",
      });

      if (!ref || cls !== "bot") return ref;
      if (msg.error) {
        ref.bubble.classList.remove("bot");
        ref.bubble.classList.add("error");
      }

      return ref;
    }

    function renderFromState(state) {
      chatMsgs.innerHTML = "";
      pendingRef = null;
      if (!state.messages.length) {
        chatAppend("Ciao! Sono il bot di sistema. Scrivi qualcosa e ti rispondero.", "bot", "Bot");
        return;
      }

      for (const message of state.messages) {
        const ref = appendAndTrack(message);
        if (message.role === "bot" && message.jobId && message.pending) {
          pendingRef = ref;
        }
      }
    }

    function upsertPendingMessage(jobId, patch) {
      const state = loadState();
      const idx = state.messages.findIndex((m) => m.role === "bot" && Number(m.jobId) === Number(jobId));
      if (idx < 0) return;
      state.messages[idx] = { ...state.messages[idx], ...patch };
      saveState(state);
    }

    function stopPolling() {
      if (!pollTimer) return;
      clearInterval(pollTimer);
      pollTimer = null;
    }

    async function pollJob(jobId) {
      try {
        const response = await fetch(`${jobEndpointBase}/${jobId}`, { credentials: "same-origin" });
        if (!response.ok) throw new Error("poll failed");

        const job = await response.json();
        let result = {};
        if (job && typeof job.result === "object" && job.result) {
          result = job.result;
        } else if (job && typeof job.result === "string" && job.result.trim()) {
          try {
            const parsed = JSON.parse(job.result);
            if (parsed && typeof parsed === "object") result = parsed;
          } catch (_) {
            // Keep result empty on malformed payload.
          }
        }

        if (!pendingRef) return;

        if (job.status === "completed") {
          const doneReply = (typeof result.reply === "string" && result.reply.trim()) ? result.reply : "(risposta vuota)";

          const finalThinking = (typeof result.thinking === "string") ? result.thinking : "";
          chatUpdatePendingBubble(pendingRef, doneReply, {
            thinking: finalThinking,
            meta: finalMeta(result),
          });
          upsertPendingMessage(jobId, {
            text: doneReply,
            thinking: finalThinking,
            meta: finalMeta(result),
            pending: false,
          });
          chatSetWaitIndicator(pendingRef, false);

          activeJobId = null;
          stopPolling();
          const state = loadState();
          state.activeJobId = null;
          saveState(state);
          setBusy(false);
          return;
        }

        if (job.status === "failed" || job.status === "stopped") {
          const errText = job.message || "Generazione interrotta";
          chatUpdatePendingBubble(pendingRef, errText, { meta: `Bot | ${job.status}` });
          pendingRef.bubble.classList.remove("bot");
          pendingRef.bubble.classList.add("error");
          upsertPendingMessage(jobId, {
            text: errText,
            meta: `Bot | ${job.status}`,
            pending: false,
            error: true,
          });
          chatSetWaitIndicator(pendingRef, false);

          activeJobId = null;
          stopPolling();
          const state = loadState();
          state.activeJobId = null;
          saveState(state);
          setBusy(false);
          return;
        }

        chatUpdatePendingBubble(pendingRef, "Sto elaborando la risposta...", {
          thinking: "",
          meta: statusMeta(job),
        });
        chatSetWaitIndicator(pendingRef, true, spinnerLabelFor(job, result));
        upsertPendingMessage(jobId, {
          text: "Sto elaborando la risposta...",
          thinking: "",
          meta: statusMeta(job),
          pending: true,
        });
      } catch (_) {
        // Transient network errors are expected during polling.
      }
    }

    function startPolling(jobId) {
      stopPolling();
      activeJobId = Number(jobId);
      setBusy(true);
      pollJob(activeJobId);
      pollTimer = setInterval(() => pollJob(activeJobId), 650);
    }

    async function requestStopActiveJob() {
      if (!activeJobId) return;
      if (stopBtn) stopBtn.disabled = true;
      try {
        const headers = {
          "X-CSRFToken": (document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || ""),
        };
        const response = await fetch(`${jobEndpointBase}/${activeJobId}/stop`, {
          method: "POST",
          headers,
          credentials: "same-origin",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.message || "Stop non riuscito");
        if (typeof window.showToast === "function") {
          window.showToast("Interruzione richiesta inviata", "warning");
        }
      } catch (err) {
        if (typeof window.showToast === "function") {
          window.showToast(String(err?.message || "Errore stop job"), "danger");
        }
        if (stopBtn) stopBtn.disabled = false;
      }
    }

    window.clearChatState = function () {
      stopPolling();
      activeJobId = null;
      saveState(defaultState());
      chatMsgs.innerHTML = "";
      chatAppend("Chat azzerata. Scrivi qualcosa!", "bot", "Bot");
      setBusy(false);
    };

    // Restore persisted state and continue an in-flight job if needed.
    {
      const state = loadState();
      renderFromState(state);
      if (Number.isFinite(state.activeJobId)) {
        activeJobId = state.activeJobId;
        if (!pendingRef) {
          pendingRef = chatAppend("Ripristino risposta in corso...", "bot", "Bot");
          state.messages.push({
            role: "bot",
            text: "Ripristino risposta in corso...",
            thinking: "",
            meta: "Bot | queued",
            pending: true,
            jobId: activeJobId,
            createdAt: nowIso(),
          });
          saveState(state);
        }
        startPolling(activeJobId);
      } else {
        setBusy(false);
      }
    }

    chatForm.addEventListener("submit", async (event) => {
      event.preventDefault();

      if (activeJobId) {
        if (typeof window.showToast === "function") {
          window.showToast("Attendi il completamento della risposta in corso", "warning");
        }
        return;
      }

      const msg = (chatInput.value || "").trim();
      if (!msg) return;
      autoStickBottom = true;

      chatInput.value = "";
      appendAndTrack({ role: "me", text: msg, meta: "Tu" });
      pendingRef = chatAppend("Sto elaborando la risposta...", "bot", "Bot");
      chatSetPendingMeta(pendingRef, "Processing");
      chatSetWaitIndicator(pendingRef, true, "Sta pensando...");
      setBusy(true);

      const state = loadState();
      state.messages.push({ role: "me", text: msg, meta: "Tu", createdAt: nowIso() });
      state.messages.push({
        role: "bot",
        text: "Sto elaborando la risposta...",
        thinking: "",
        meta: "Bot | queued",
        pending: true,
        jobId: null,
        createdAt: nowIso(),
      });
      saveState(state);

      try {
        try {
          const headers = {
            "Content-Type": "application/json",
            "X-CSRFToken": (document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || ""),
          };
          const response = await fetch(startEndpoint, {
            method: "POST",
            headers,
            body: JSON.stringify({ message: msg }),
            credentials: "same-origin",
          });
          const data = await response.json();
          if (!response.ok || !Number.isFinite(data.job_id)) {
            throw new Error(data.message || "Errore avvio job chat");
          }

          const jid = Number(data.job_id);
          const cur = loadState();
          for (let i = cur.messages.length - 1; i >= 0; i--) {
            if (cur.messages[i].role === "bot" && cur.messages[i].pending && !cur.messages[i].jobId) {
              cur.messages[i].jobId = jid;
              break;
            }
          }
          cur.activeJobId = jid;
          saveState(cur);
          startPolling(jid);
        } catch (jobErr) {
          // Fallback to direct sync endpoint when queue path is unavailable.
          try {
            const headers = {
              "Content-Type": "application/json",
              "X-CSRFToken": (document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || ""),
            };
            const response = await fetch(chatEndpoint, {
              method: "POST",
              headers,
              body: JSON.stringify({ message: msg }),
              credentials: "same-origin",
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || String(jobErr || "Errore chat"));
            const finalReply = (data.reply || "").trim() || "(risposta vuota)";
            const finalThinking = (data.thinking || "").trim();
            chatUpdatePendingBubble(pendingRef, finalReply, {
              thinking: finalThinking,
              meta: finalMeta(data),
            });
            const endState = loadState();
            for (let i = endState.messages.length - 1; i >= 0; i--) {
              if (endState.messages[i].role === "bot" && endState.messages[i].pending) {
                endState.messages[i] = {
                  ...endState.messages[i],
                  text: finalReply,
                  thinking: finalThinking,
                  meta: finalMeta(data),
                  pending: false,
                };
                break;
              }
            }
            endState.activeJobId = null;
            saveState(endState);
          } catch {
            throw jobErr;
          }

          activeJobId = null;
          setBusy(false);
          chatSetWaitIndicator(pendingRef, false);
        }
      } catch (fatalErr) {
        chatUpdatePendingBubble(
          pendingRef,
          String(fatalErr?.message || fatalErr || "Errore di rete"),
          { meta: "Sistema" },
        );
        pendingRef?.bubble?.classList.remove("bot");
        pendingRef?.bubble?.classList.add("error");
        chatSetWaitIndicator(pendingRef, false);
        activeJobId = null;
        setBusy(false);
      } finally {
        chatInput.focus();
      }
    });

    // Send on Enter, keep Shift+Enter for newline.
    chatInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatForm.requestSubmit();
      }
    });

    chatMsgs.addEventListener("scroll", () => {
      autoStickBottom = isNearBottom();
    });

    if (stopBtn) {
      stopBtn.addEventListener("click", requestStopActiveJob);
    }
  }

  window.AppChat = {
    initChat,
  };
})();
