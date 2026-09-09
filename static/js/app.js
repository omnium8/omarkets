(() => {
  const REFRESH_MS = 60_000;
  const THEME_MS = 5_000;

  const els = {
    cards: document.getElementById("cards"),
    watchlist: document.getElementById("watchlist"),
    status: document.getElementById("status"),
    addForm: document.getElementById("add-form"),
    addSymbol: document.getElementById("add-symbol"),
    refresh: document.getElementById("btn-refresh"),
    ac: document.getElementById("ac"),
  };

  let quotes = [];
  let selected = null;
  let loading = false;
  let dragFrom = null;   // index being dragged (shared by cards + watchlist)
  let selectedTf = "I";  // timeframe of the enlarged card: I(ntraday)/D/W/M
  const chartCache = new Map();  // "symbol|tf" -> series (client-side, no refetch)

  const TIMEFRAMES = [["I", "5m"], ["D", "D"], ["W", "W"], ["M", "M"]];

  function applyTheme(theme) {
    if (!theme || !theme.colors) return;
    const c = theme.colors;
    const root = document.documentElement.style;
    if (c.background) root.setProperty("--bg", c.background);
    if (c.dark_background) root.setProperty("--bg-dark", c.dark_background);
    if (c.lighter_background) root.setProperty("--bg-light", c.lighter_background);
    if (c.foreground) root.setProperty("--fg", c.foreground);
    if (c.dark_foreground) root.setProperty("--fg-dim", c.dark_foreground);
    if (c.bright_foreground) root.setProperty("--fg-bright", c.bright_foreground);
    if (c.accent) root.setProperty("--accent", c.accent);
    if (c.red) root.setProperty("--red", c.red);
    if (c.green) root.setProperty("--green", c.green);
    if (c.yellow) root.setProperty("--yellow", c.yellow);
    if (c.selection) root.setProperty("--selection", c.selection);
    if (c.muted) root.setProperty("--muted", c.muted);
    if (c.muted) root.setProperty("--border", c.muted);
    if (theme.fontFamily) root.setProperty("--font", theme.fontFamily);
  }

  async function loadTheme() {
    try {
      const res = await fetch("/api/theme");
      if (!res.ok) return;
      applyTheme(await res.json());
    } catch (_) {
      /* keep defaults */
    }
  }

  function fmtPrice(n, currency) {
    if (n == null || !Number.isFinite(n)) return "—";
    const abs = Math.abs(n);
    let digits = 2;
    if (abs >= 1000) digits = 2;
    else if (abs < 1) digits = 4;
    else if (abs < 10) digits = 3;
    const s = n.toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    return currency ? `${s} ${currency}` : s;
  }

  function fmtChg(pct) {
    if (pct == null || !Number.isFinite(pct)) return { text: "—", cls: "flat" };
    const sign = pct > 0 ? "+" : "";
    return {
      text: `${sign}${pct.toFixed(2)}%`,
      cls: pct > 0.005 ? "up" : pct < -0.005 ? "down" : "flat",
    };
  }

  function drawSpark(canvas, series, up) {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 200;
    const cssH = canvas.clientHeight || 56;
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const pts = (series || []).map((p) => p.c).filter((v) => Number.isFinite(v));
    if (pts.length < 2) {
      ctx.strokeStyle = getCss("--fg-dim");
      ctx.beginPath();
      ctx.moveTo(0, cssH / 2);
      ctx.lineTo(cssW, cssH / 2);
      ctx.stroke();
      return;
    }

    let min = Math.min(...pts);
    let max = Math.max(...pts);
    if (min === max) {
      min -= 1;
      max += 1;
    }
    const pad = 2;
    const w = cssW - pad * 2;
    const h = cssH - pad * 2;

    const color = up === true
      ? getCss("--green")
      : up === false
        ? getCss("--red")
        : getCss("--accent");

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((v, i) => {
      const x = pad + (i / (pts.length - 1)) * w;
      const y = pad + (1 - (v - min) / (max - min)) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // fill under curve
    ctx.lineTo(pad + w, cssH - pad);
    ctx.lineTo(pad, cssH - pad);
    ctx.closePath();
    ctx.fillStyle = color + "22";
    ctx.fill();
  }

  function getCss(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
  }

  function groupLabel(g) {
    return String(g || "other").replace(/_/g, " ");
  }

  // Make `el` a draggable item at position `idx`. Both the card grid and the
  // watchlist reorder the same underlying list, so a drop just calls reorder().
  function makeDraggable(el, idx) {
    el.draggable = true;
    el.dataset.idx = idx;
    el.addEventListener("dragstart", (e) => {
      dragFrom = idx;
      el.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
      try {
        e.dataTransfer.setData("text/plain", String(idx));
      } catch (_) {
        /* some engines require a payload */
      }
    });
    el.addEventListener("dragend", () => {
      dragFrom = null;
      el.classList.remove("is-dragging");
      el.parentElement
        ?.querySelectorAll(".drag-over")
        .forEach((n) => n.classList.remove("drag-over"));
    });
    el.addEventListener("dragover", (e) => {
      if (dragFrom === null || dragFrom === idx) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("drag-over");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", async (e) => {
      e.preventDefault();
      el.classList.remove("drag-over");
      const from = dragFrom;
      dragFrom = null;
      if (from === null || from === idx) return;
      await reorder(from, idx);
    });
  }

  // Series to draw for a card: intraday comes from /api/quotes; D/W/M are
  // fetched on demand for the selected card only and cached.
  function seriesFor(q) {
    if (selected !== q.symbol || selectedTf === "I") return q.series;
    return chartCache.get(q.symbol + "|" + selectedTf) || null;
  }

  async function loadTf(symbol, tf) {
    const key = symbol + "|" + tf;
    if (chartCache.has(key)) return;
    chartCache.set(key, null); // mark in-flight so we don't double-fetch
    try {
      const res = await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&tf=${tf}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      chartCache.set(key, data.series || []);
      if (selected === symbol && selectedTf === tf) renderCards();
    } catch (e) {
      chartCache.delete(key); // allow a retry on next select
      els.status.textContent = `chart ${tf}: ${e.message || e}`;
    }
  }

  function addTfControls(card, q) {
    const bar = document.createElement("div");
    bar.className = "card-tf";
    TIMEFRAMES.forEach(([tf, label]) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tf-btn" + (selectedTf === tf ? " is-active" : "");
      b.textContent = label;
      b.title = tf === "I" ? "intraday (5m)" : tf === "D" ? "daily" : tf === "W" ? "weekly" : "monthly";
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        if (selectedTf === tf) return;
        selectedTf = tf;
        renderCards();
      });
      bar.appendChild(b);
    });
    card.insertBefore(bar, card.querySelector("canvas"));
  }

  function renderCards() {
    if (!quotes.length) {
      els.cards.innerHTML = '<div class="empty">no symbols in watchlist</div>';
      return;
    }
    els.cards.innerHTML = "";
    quotes.forEach((q, idx) => {
      const card = document.createElement("article");
      card.className = "card";
      if (selected === q.symbol) card.classList.add("is-selected");
      if (q.stale) card.classList.add("is-stale");
      card.dataset.symbol = q.symbol;

      const chg = fmtChg(q.changePct);
      const title = q.label || q.shortName || q.symbol;

      card.innerHTML = `
        <div class="card-top">
          <span class="card-sym"></span>
          <span class="card-group"></span>
        </div>
        <div class="card-label"></div>
        <div class="card-price-row">
          <span class="card-price"></span>
          <span class="card-chg ${chg.cls}"></span>
        </div>
        <canvas width="200" height="56" aria-hidden="true"></canvas>
        <div class="card-err" hidden></div>
      `;
      card.querySelector(".card-sym").textContent = q.ticker || q.symbol;
      card.querySelector(".card-sym").title = q.symbol;
      card.querySelector(".card-group").textContent = groupLabel(q.group);
      card.querySelector(".card-label").textContent = title;
      card.querySelector(".card-price").textContent = fmtPrice(q.price, q.currency);
      card.querySelector(".card-chg").textContent = chg.text;
      if (q.error && !q.series?.length) {
        const err = card.querySelector(".card-err");
        err.hidden = false;
        err.textContent = q.series?.length ? q.error : (q.price != null ? "chart pending" : q.error);
      }
      card.addEventListener("click", () => {
        selected = q.symbol;
        renderCards();
        renderWatchlist();
      });
      const isSel = selected === q.symbol;
      if (isSel) addTfControls(card, q);
      makeDraggable(card, idx);
      els.cards.appendChild(card);

      // pick the series for the active timeframe; fall back to intraday while loading
      let series = seriesFor(q);
      let up;
      if (isSel && selectedTf !== "I" && series && series.length >= 2) {
        up = series[series.length - 1].c >= series[0].c; // trend over the period
      } else {
        up = q.changePct == null ? null : q.changePct >= 0;
      }
      drawSpark(card.querySelector("canvas"), series || q.series, up);
      if (isSel && selectedTf !== "I" && !chartCache.has(q.symbol + "|" + selectedTf)) {
        loadTf(q.symbol, selectedTf);
      }
    });
  }

  function renderWatchlist() {
    els.watchlist.innerHTML = "";
    quotes.forEach((q, idx) => {
      const row = document.createElement("div");
      row.className = "wl-item" + (selected === q.symbol ? " is-selected" : "");
      row.innerHTML = `
        <div class="wl-sym"></div>
        <button type="button" class="icon-btn" data-act="up" title="Move up">↑</button>
        <button type="button" class="icon-btn" data-act="rm" title="Remove">×</button>
        <div class="wl-meta"></div>
      `;
      row.querySelector(".wl-sym").textContent = q.ticker || q.symbol;
      row.querySelector(".wl-sym").title = q.symbol;
      row.querySelector(".wl-meta").textContent =
        `${groupLabel(q.group)} · ${fmtPrice(q.price, q.currency)} · ${fmtChg(q.changePct).text}`;
      row.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        selected = q.symbol;
        renderCards();
        renderWatchlist();
        const el = els.cards.querySelector(`[data-symbol="${CSS.escape(q.symbol)}"]`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
      row.querySelector('[data-act="rm"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        await removeSymbol(q.symbol);
      });
      row.querySelector('[data-act="up"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        if (idx === 0) return;
        await reorder(idx, idx - 1);
      });
      makeDraggable(row, idx);
      els.watchlist.appendChild(row);
    });
  }

  async function fetchQuotes(force = false) {
    if (loading) return;
    loading = true;
    els.status.textContent = force ? "refreshing…" : "updating…";
    try {
      const res = await fetch(`/api/quotes${force ? "?force=1" : ""}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      quotes = data.quotes || [];
      const t = new Date((data.fetchedAt || Date.now() / 1000) * 1000);
      const stale = quotes.some((q) => q.stale);
      els.status.textContent =
        `${quotes.length} symbols · ${t.toLocaleTimeString()}` +
        (stale ? " · stale" : "");
      renderCards();
      renderWatchlist();
    } catch (e) {
      els.status.textContent = `error: ${e.message || e}`;
    } finally {
      loading = false;
    }
  }

  async function removeSymbol(symbol) {
    const res = await fetch(`/api/watchlist?symbol=${encodeURIComponent(symbol)}`, {
      method: "DELETE",
    });
    const data = await res.json();
    if (!res.ok) {
      els.status.textContent = data.error || "remove failed";
      return;
    }
    if (selected === symbol) selected = null;
    await fetchQuotes(true);
  }

  async function reorder(from, to) {
    const symbols = quotes.map((q) => ({
      symbol: q.symbol,
      group: q.group,
      label: q.label,
    }));
    const [item] = symbols.splice(from, 1);
    symbols.splice(to, 0, item);
    const res = await fetch("/api/watchlist", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols }),
    });
    const data = await res.json();
    if (!res.ok) {
      els.status.textContent = data.error || "reorder failed";
      return;
    }
    await fetchQuotes(true);
  }

  async function addSymbol(symbol, label, group) {
    if (!symbol) return;
    const res = await fetch("/api/watchlist/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, group: group || "other", label: label || symbol }),
    });
    const data = await res.json();
    if (!res.ok) {
      els.status.textContent = data.error || "add failed";
      return;
    }
    // server normalizes casing/exchange; match the last entry it stored
    const added = data.symbols?.[data.symbols.length - 1];
    selected = added ? added.symbol : symbol;
    els.addSymbol.value = "";
    closeAc();
    await fetchQuotes(true);
  }

  els.addForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (acActive >= 0 && acResults[acActive]) {
      const t = acResults[acActive];
      addSymbol(t.s, t.d || t.n, t.g);
      return;
    }
    addSymbol(els.addSymbol.value.trim().toUpperCase());
  });

  /* ---- ticker autocomplete (top-1000 list) ---- */
  let acResults = [];
  let acActive = -1;
  let acTimer = null;

  function closeAc() {
    acResults = [];
    acActive = -1;
    els.ac.hidden = true;
    els.ac.innerHTML = "";
    els.addSymbol.setAttribute("aria-expanded", "false");
  }

  function renderAc() {
    if (!acResults.length) {
      closeAc();
      return;
    }
    els.ac.innerHTML = "";
    acResults.forEach((t, i) => {
      const li = document.createElement("li");
      li.className = "ac-item" + (i === acActive ? " is-active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === acActive ? "true" : "false");
      li.innerHTML = `<span class="ac-sym"></span><span class="ac-desc"></span>`;
      li.querySelector(".ac-sym").textContent = t.n;
      li.querySelector(".ac-desc").textContent = t.d || t.s;
      li.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep focus, beat blur
        addSymbol(t.s, t.d || t.n, t.g);
      });
      els.ac.appendChild(li);
    });
    els.ac.hidden = false;
    els.addSymbol.setAttribute("aria-expanded", "true");
  }

  async function queryAc(text) {
    const q = text.trim();
    if (q.length < 1 || q.includes(":")) {
      closeAc();
      return;
    }
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      if (!res.ok) return;
      const data = await res.json();
      acResults = data.results || [];
      acActive = -1;
      renderAc();
    } catch (_) {
      /* ignore search errors */
    }
  }

  els.addSymbol.addEventListener("input", () => {
    clearTimeout(acTimer);
    acTimer = setTimeout(() => queryAc(els.addSymbol.value), 120);
  });

  els.addSymbol.addEventListener("keydown", (e) => {
    if (els.ac.hidden || !acResults.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      acActive = (acActive + 1) % acResults.length;
      renderAc();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      acActive = (acActive - 1 + acResults.length) % acResults.length;
      renderAc();
    } else if (e.key === "Escape") {
      closeAc();
    }
  });

  els.addSymbol.addEventListener("blur", () => setTimeout(closeAc, 100));

  els.refresh.addEventListener("click", () => fetchQuotes(true));

  loadTheme();
  fetchQuotes(true);
  setInterval(() => fetchQuotes(false), REFRESH_MS);
  setInterval(loadTheme, THEME_MS);
})();
