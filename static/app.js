// Ellie front end. Talks only to app.py's own /api/* endpoints, which are
// thin wrappers over the already-tested ellie_chat.py / query_functions.py
// logic -- no chat or game-data logic lives here, only rendering, polling
// and the theme switch.
(() => {
  "use strict";

  const STATUS_POLL_MS = 6000;
  const RANK_FIELDS = [
    { key: "combat_rank", nameKey: "combat_rank_name", label: "Combat" },
    { key: "trade_rank", nameKey: "trade_rank_name", label: "Trade" },
    { key: "explore_rank", nameKey: "explore_rank_name", label: "Exploration" },
    { key: "cqc_rank", nameKey: "cqc_rank_name", label: "CQC" },
    { key: "federation_rank", nameKey: "federation_rank_name", label: "Federation" },
    { key: "empire_rank", nameKey: "empire_rank_name", label: "Empire" },
  ];
  const MATERIAL_ORDER = ["Raw", "Manufactured", "Encoded"];
  const THEMES = [
    { id: "violet", label: "VIOLET" },
    { id: "orange", label: "ORANGE" },
  ];

  const $ = (id) => document.getElementById(id);

  const log = $("log");
  const chatForm = $("chatForm");
  const chatInput = $("chatInput");
  const sendBtn = $("sendBtn");
  const resetBtn = $("resetBtn");
  const themeBtn = $("themeBtn");
  const connIndicator = $("connIndicator");
  const connLabel = $("connLabel");
  const statusToggle = $("statusToggle");
  const statuspanel = $("statuspanel");
  const statuspanelBackdrop = $("statuspanelBackdrop");

  const els = {
    cmdrName: $("cmdrName"),
    cmdrShip: $("cmdrShip"),
    dockedStatus: $("dockedStatus"),
    curSystem: $("curSystem"),
    curStation: $("curStation"),
    ranks: $("ranks"),
    credits: $("credits"),
    materials: $("materials"),
    panelNotice: $("panelNotice"),
    noticeText: $("noticeText"),
  };
  const dataPanels = [$("panelIdentity"), $("panelLocation"), $("panelRanks"), $("panelMaterials")];

  let prevStatus = null;

  function escapeHtml(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }

  // ---------- theme ----------

  function currentThemeIndex() {
    const i = THEMES.findIndex((t) => t.id === document.documentElement.dataset.theme);
    return i === -1 ? 0 : i;
  }
  function showThemeLabel() {
    themeBtn.textContent = "THEME: " + THEMES[currentThemeIndex()].label;
  }
  themeBtn.addEventListener("click", () => {
    const next = THEMES[(currentThemeIndex() + 1) % THEMES.length];
    document.documentElement.dataset.theme = next.id;
    try { localStorage.setItem("ellie-theme", next.id); } catch (e) { /* storage blocked: theme just won't persist */ }
    showThemeLabel();
  });
  showThemeLabel();

  // ---------- console log ----------

  function timeNow() {
    return new Date().toLocaleTimeString("en-US", { hour12: false });
  }

  // The model sometimes wraps a word in markdown emphasis even though it is
  // speaking plain text (ellie_chat.py's terminal version shows the same
  // asterisks). Escape first -- this is untrusted model output -- then turn
  // back only **bold**, *italic* and `code` into tags, never model HTML.
  function formatEllieText(raw) {
    return escapeHtml(raw)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)/g, "<em>$1</em>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  }

  const WHO = { ellie: "Ellie", cmdr: "Cmdr", system: "System", error: "Error" };

  function makeRow(role, whoText) {
    const row = document.createElement("div");
    row.className = `msg msg--${role}`;
    const who = document.createElement("div");
    who.className = "msg__who";
    who.textContent = whoText;
    const body = document.createElement("div");
    const text = document.createElement("div");
    text.className = "msg__text";
    body.appendChild(text);
    row.appendChild(who);
    row.appendChild(body);
    return { row, who, text };
  }

  function appendMessage(role, text) {
    const { row, who, text: textEl } = makeRow(role, WHO[role] || "System");
    if (role === "ellie") textEl.innerHTML = formatEllieText(text);
    else textEl.textContent = text;
    if (role !== "system") {
      const time = document.createElement("span");
      time.className = "msg__time";
      time.textContent = timeNow();
      who.appendChild(time);
    }
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  function appendThinking() {
    const { row, text } = makeRow("ellie", WHO.ellie);
    row.classList.add("thinking");
    text.innerHTML = 'Working<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  // ---------- chat ----------

  async function sendMessage(message) {
    chatInput.disabled = true;
    sendBtn.disabled = true;
    resetBtn.disabled = true;
    const thinkingRow = appendThinking();

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      thinkingRow.remove();
      if (!resp.ok) {
        appendMessage("error", `Ellie's server returned an error (HTTP ${resp.status}). Try again.`);
        setConn("offline");
        return;
      }
      const data = await resp.json();
      appendMessage("ellie", data.reply || "(no response)");
    } catch (err) {
      thinkingRow.remove();
      appendMessage("error", "Lost the connection to Ellie's server. Is app.py still running?");
      setConn("offline");
    } finally {
      chatInput.disabled = false;
      sendBtn.disabled = false;
      resetBtn.disabled = false;
      chatInput.focus();
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    appendMessage("cmdr", message);
    chatInput.value = "";
    sendMessage(message);
  });

  // Explicit Enter handling rather than relying only on the browser's native
  // "Enter in a form's text input submits it", which not every input source
  // triggers reliably.
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      chatForm.requestSubmit();
    }
  });

  resetBtn.addEventListener("click", async () => {
    resetBtn.disabled = true;
    try {
      await fetch("/api/reset", { method: "POST" });
    } catch (err) {
      /* worst case the server-side history just stays as it was */
    }
    log.innerHTML = "";
    appendMessage("system", "New conversation started.");
    resetBtn.disabled = false;
    chatInput.focus();
  });

  // ---------- connection indicator ----------

  const CONN_LABELS = { connecting: "CONNECTING", online: "ONLINE", setup: "SETUP NEEDED", offline: "OFFLINE" };
  function setConn(state) {
    connIndicator.dataset.state = state;
    connLabel.textContent = CONN_LABELS[state];
  }

  // ---------- status panel ----------

  function flash(el) {
    el.classList.remove("flash");
    void el.offsetWidth; // restart the animation if it is already running
    el.classList.add("flash");
  }

  function setText(el, text, changed) {
    if (el.textContent === text) return;
    el.textContent = text;
    if (changed) flash(el);
  }

  function makeRowEl(label) {
    const row = document.createElement("div");
    row.className = "row";
    const l = document.createElement("span");
    l.className = "row__label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "row__value";
    row.appendChild(l);
    row.appendChild(v);
    return { row, value: v };
  }

  function renderRanks(status, prev) {
    els.ranks.innerHTML = "";
    for (const rank of RANK_FIELDS) {
      const { row, value } = makeRowEl(rank.label);
      value.textContent = status[rank.nameKey] || "—";
      if (prev && prev[rank.key] !== status[rank.key]) flash(value);
      els.ranks.appendChild(row);
    }
  }

  function renderMaterials(summary, prevSummary) {
    els.materials.innerHTML = "";
    const categories = Object.keys(summary).sort((a, b) => {
      const ia = MATERIAL_ORDER.indexOf(a);
      const ib = MATERIAL_ORDER.indexOf(b);
      if (ia === -1 && ib === -1) return a.localeCompare(b);
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
    if (categories.length === 0) {
      const empty = document.createElement("div");
      empty.className = "sub";
      empty.textContent = "No materials tracked yet.";
      els.materials.appendChild(empty);
      return;
    }
    for (const cat of categories) {
      const info = summary[cat];
      const { row, value } = makeRowEl(cat);
      value.innerHTML =
        `<span class="num">${info.distinct_materials}</span> <span class="unit">TYPES</span>` +
        `<span class="sep">|</span>` +
        `<span class="num">${info.total_units}</span> <span class="unit">UNITS</span>`;
      const prevInfo = prevSummary && prevSummary[cat];
      if (prevInfo && prevInfo.total_units !== info.total_units) flash(value);
      els.materials.appendChild(row);
    }
  }

  function renderStatus(payload) {
    if (payload.error) {
      setConn("setup");
      for (const p of dataPanels) p.hidden = true;
      els.credits.textContent = "—";
      els.panelNotice.hidden = false;
      els.noticeText.innerHTML =
        `${escapeHtml(payload.error)}<br><br>Run <code>python setup.py</code> in the project folder. This panel fills in on its own afterwards.`;
      prevStatus = null;
      return;
    }

    els.panelNotice.hidden = true;
    for (const p of dataPanels) p.hidden = false;

    const status = payload.status || {};
    const materials = payload.materials_summary || {};
    const prev = prevStatus ? prevStatus.status : null;
    const prevMat = prevStatus ? prevStatus.materials_summary : null;

    if (!status.commander_name) {
      setConn("setup");
      els.cmdrName.textContent = "No data yet";
      els.cmdrShip.textContent = "Waiting for the journal watcher to report in.";
    } else {
      setConn("online");
      setText(els.cmdrName, status.commander_name, prev && prev.commander_name !== status.commander_name);
      setText(els.cmdrShip, status.current_ship || "Unknown vessel", prev && prev.current_ship !== status.current_ship);
    }

    const docked = status.docked;
    els.dockedStatus.dataset.docked = docked == null ? "unknown" : String(docked);
    els.dockedStatus.textContent = docked == null ? "Status unknown" : docked ? "Docked" : "In transit";
    setText(els.curSystem, status.current_system || "Unknown system", prev && prev.current_system !== status.current_system);
    els.curStation.textContent = status.current_station || "";

    renderRanks(status, prev);

    if (status.credits != null) {
      setText(els.credits, Number(status.credits).toLocaleString("en-US"), prev && prev.credits !== status.credits);
    } else {
      els.credits.textContent = "—";
    }

    renderMaterials(materials, prevMat);
    prevStatus = payload;
  }

  async function pollStatus() {
    try {
      const resp = await fetch("/api/status");
      if (!resp.ok) {
        setConn("offline");
        return;
      }
      renderStatus(await resp.json());
    } catch (err) {
      setConn("offline");
    }
  }

  // ---------- narrow-width status drawer ----------

  function setDrawer(open) {
    statuspanel.classList.toggle("is-open", open);
    statuspanelBackdrop.classList.toggle("is-open", open);
    statusToggle.setAttribute("aria-expanded", String(open));
  }
  statusToggle.addEventListener("click", () => setDrawer(!statuspanel.classList.contains("is-open")));
  statuspanelBackdrop.addEventListener("click", () => setDrawer(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setDrawer(false); });

  // ---------- boot ----------

  appendMessage("system", "Ask about blueprints, materials, engineers, or your status.");
  pollStatus();
  setInterval(pollStatus, STATUS_POLL_MS);
})();
