/* Background film for the Atlas Desks site: a canvas of terminal panes running desk scripts and one pane
   streaming model tokens. Rendered at half resolution and blurred by CSS (see cinema.css .film canvas).
   Every `.film` gets its own canvas; loops pause when off-screen or the tab is hidden. No video needed. */
(function () {
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const films = Array.from(document.querySelectorAll(".film"));
  if (!films.length) return;

  const C = { paper: "#ffffff", line: "#c1cad6", bar: "#eef1f6", text: "#10161d", muted: "#5f6b7a", faint: "#b3bdc9",
              blue: "#2d72d2", blueSoft: "#e4eefc", ok: "#1c8f5f", warn: "#a66a00", danger: "#c23030", purple: "#7c5cd6", pink: "#c2409a" };

  // ---- content pools ------------------------------------------------------------------
  const SCRIPTS = [
    [["$ ", "p"], ["py -m atlas run \"qualify lead: Northgate Plumbing\" --mode auto", "t"]],
    [["[atlas]    ", "b"], ["plan: research → writer → crm → qa  (parallel)", "t"]],
    [["[research] ", "b"], ["GET companies-house/search?q=northgate … 200  412 ms", "m"]],
    [["[research] ", "b"], ["service fit: SW1V ✓  24/7 call-out ✓  gas-safe ✓", "t"]],
    [["[policy]   ", "w"], ["draft 218 words > max 90 · markdown → sent back", "w"]],
    [["[writer]   ", "k"], ["draft v2 · 87 words · plain text · no prices", "t"]],
    [["[qa]       ", "y"], ["facts ✓  tone ✓  policy ✓  edits applied: 2", "t"]],
    [["[crm]      ", "g"], ["stage → Qualified · follow-up +2d · owner notified", "t"]],
    [["✓ ", "g"], ["queued for approval · 136 s end to end", "g"]],
    [["$ ", "p"], ["py -m atlas eval --suite corner-store --frames 12", "t"]],
    [["  detect   ", "m"], ["yolov8n   person=2  car=1  bottle=6   38 ms/frame", "t"]],
    [["  vlm      ", "m"], ["claude-haiku-4.5  \"two customers at the counter, shelf 3 half empty\"", "t"]],
    [["  rule     ", "m"], ["alert_on_motion  22:00–06:00  → no trigger", "t"]],
    [["  PASS ", "g"], ["14/14  ·  3.9 s", "g"]],
    [["$ ", "p"], ["git push render master", "t"]],
    [["remote:    ", "m"], ["build ok · gunicorn -w 1 --threads 32 · /api/health 200", "m"]],
    [["$ ", "p"], ["py -m atlas soak --desk northgate --days 7", "t"]],
    [["[watchdog] ", "b"], ["runs 1 284 · failed 3 · p95 148 s · spend $2.85", "t"]],
    [["def ", "k"], ["delegate(task, agents):", "t"]],
    [["    ", "t"], ["plan = self.plan(task)", "t"]],
    [["    ", "t"], ["return gather(*(a.run(step) for a, step in plan))", "t"]],
    [["[approval] ", "w"], ["waiting: owner · 1 item · reply to lead #4812", "w"]],
    [["[approval] ", "g"], ["approved by owner · sent via gmail · 09:41", "g"]],
    [["$ ", "p"], ["curl -s /api/cameras/yard/look -d '{\"q\":\"anyone at the gate?\"}'", "t"]],
    [["{\"answer\": ", "m"], ["\"No. One silver hatchback parked by the generator.\"}", "t"]],
    [["[scheduler]", "b"], ["09:00 daily pipeline report → 3 recipients", "t"]],
    [["[hermes]   ", "y"], ["browser fallback: bot-walled site · 2 retries · ok", "t"]],
    [["$ ", "p"], ["py -m atlas scenario corner-store --minutes 8", "t"]],
    [["[vision]   ", "b"], ["frame 41 · motion .18 · person=1 · snapshot saved", "t"]],
  ];
  const TOKENS = [
    "The lead is a two-person plumbing firm in Pimlico. Their site lists emergency call-outs but no gas-safety page, so the reply should mention the check explicitly. Tone: direct, no prices in writing. Booking window tomorrow 08:00–10:00 is free.",
    "A silver hatchback is parked beside the yellow generator. The yard is otherwise empty; the gate on the right is open. No people visible. Since the last frame nothing has moved.",
    "Three of the twelve leads today came from the web form, the rest from WhatsApp. Two need a same-day visit. I have drafted replies for all and queued the two that mention a boiler fault for owner approval.",
    "QA note: the draft claims a 30-minute response time, which is not in the policy. Replaced with 'same day'. Removed markdown. Word count 87.",
    "Shelf three is half empty and one customer is waiting at the counter. Recommend restocking before the 17:00 rush; last week the same shelf emptied by 16:40.",
    "Monthly summary: 1 284 runs, 96% approved without edits, median 2 m 14 s from lead to queued reply. Model spend $2.85. Two prompts tuned, one workflow added for cancellations.",
  ];
  const COL = { p: C.blue, t: C.text, m: C.muted, b: C.blue, w: C.warn, k: C.purple, y: C.pink, g: C.ok };

  // ---- pane models --------------------------------------------------------------------
  function mkTerm(title, seed) {
    return { kind: "term", title, lines: [], cur: null, idx: seed % SCRIPTS.length, char: 0, wait: 0, blink: 0 };
  }
  function mkTokens(title, seed) {
    return { kind: "tok", title, text: TOKENS[seed % TOKENS.length], pos: 0, ti: seed, wait: 0, tps: 38, done: 0 };
  }
  function stepTerm(t, dt) {
    t.blink += dt;
    if (t.wait > 0) { t.wait -= dt; return; }
    if (!t.cur) { t.cur = { parts: SCRIPTS[t.idx], shown: 0 }; t.idx = (t.idx + 1) % SCRIPTS.length; t.char = 0; }
    const full = t.cur.parts.map((p) => p[0]).join("");
    const isCmd = t.cur.parts[0][1] === "p";
    t.char += dt * (isCmd ? 0.028 : 0.16);           // commands are typed by a human; output lands fast
    t.cur.shown = Math.min(full.length, Math.floor(t.char));
    if (t.cur.shown >= full.length) {
      t.lines.push(t.cur.parts); t.cur = null;
      if (t.lines.length > 14) t.lines.shift();
      t.wait = isCmd ? 120 : 140 + Math.random() * 520;
    }
  }
  function stepTok(t, dt) {
    if (t.wait > 0) { t.wait -= dt; return; }
    if (t.pos >= t.text.length) {
      t.done += dt; if (t.done > 2600) { t.ti++; t.text = TOKENS[t.ti % TOKENS.length]; t.pos = 0; t.done = 0; }
      return;
    }
    // next "token": a chunk of 2-7 chars, like a BPE stream
    const n = 2 + Math.floor(Math.random() * 6);
    t.pos = Math.min(t.text.length, t.pos + n);
    t.tps = Math.round(34 + Math.sin(performance.now() / 900) * 9 + Math.random() * 4);
    t.wait = 1000 / t.tps * n / 3.2;
  }

  // ---- layout per film ------------------------------------------------------------------
  function build(film, i) {
    film.querySelectorAll("video").forEach((v) => v.remove());
    const cv = document.createElement("canvas"); film.appendChild(cv);
    const vctx = cv.getContext("2d", { alpha: true });
    const off = document.createElement("canvas");                 // scene is drawn here, then blurred once onto cv
    const ctx = off.getContext("2d", { alpha: true });
    const panes = [];
    const seed = i * 7;
    const names = ["atlas · run 4812", "bash — deploy", "research · northgate", "vision · yard cam", "qa · policy", "scheduler"];
    for (let k = 0; k < 6; k++) panes.push({ m: k === 2 ? mkTokens("claude-haiku-4.5 · streaming", seed + k) : mkTerm(names[k], seed + k * 5), u: 0, v: 0, w: 0, h: 0, ph: Math.random() * 6.28 });
    const state = { cv, vctx, off, ctx, panes, W: 0, H: 0, S: 0.45, visible: false, last: 0, film };
    const size = () => {
      const r = film.getBoundingClientRect();
      state.W = Math.max(320, r.width); state.H = Math.max(240, r.height);
      cv.width = off.width = Math.round(state.W * state.S); cv.height = off.height = Math.round(state.H * state.S);
      // grid: 3 columns x 2 rows on desktop; panes overflow the edges a little so the blur has no frame
      const cols = state.W > 900 ? 3 : 2, rows = cols === 3 ? 2 : 3;
      const cw = state.W / cols, ch = state.H / rows;
      panes.forEach((p, k) => {
        const c = k % cols, rr = Math.floor(k / cols) % rows;
        p.w = cw * (0.88 + (k % 3) * 0.05); p.h = ch * (0.84 + (k % 2) * 0.1);
        p.u = c * cw + cw * 0.05 - (k % 2) * 30; p.v = rr * ch + ch * 0.06 + (k % 3) * 12;
      });
    };
    size();
    new ResizeObserver(size).observe(film);
    new IntersectionObserver((es) => es.forEach((e) => { state.visible = e.isIntersecting; }), { rootMargin: "200px" }).observe(film);
    setTimeout(() => cv.classList.add("on"), 200);
    return state;
  }

  function roundRect(ctx, x, y, w, h, r) {
    if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r);
  }

  function drawPane(ctx, p, S, t) {
    const x = (p.u + Math.sin(t / 4200 + p.ph) * 14) * S, y = (p.v + Math.cos(t / 5100 + p.ph) * 10) * S;
    const w = p.w * S, h = p.h * S, r = 3 * S;
    ctx.fillStyle = "rgba(16,22,29,.07)"; ctx.beginPath(); roundRect(ctx, x, y + 5 * S, w, h, r); ctx.fill();   // cheap drop shadow (shadowBlur is slow)
    ctx.fillStyle = C.paper; ctx.beginPath(); roundRect(ctx, x, y, w, h, r); ctx.fill();
    ctx.strokeStyle = C.line; ctx.lineWidth = 1; ctx.beginPath(); roundRect(ctx, x + .5, y + .5, w - 1, h - 1, r); ctx.stroke();
    // title bar
    const bh = 22 * S;
    ctx.fillStyle = C.bar; ctx.fillRect(x + 1, y + 1, w - 2, bh);
    ctx.fillStyle = C.line; ctx.fillRect(x + 1, y + bh, w - 2, 1);
    for (let k = 0; k < 3; k++) { ctx.fillStyle = C.faint; ctx.beginPath(); ctx.arc(x + (12 + k * 12) * S, y + bh / 2, 3.2 * S, 0, 6.28); ctx.fill(); }
    ctx.font = `500 ${9.5 * S}px "IBM Plex Mono", Consolas, monospace`; ctx.fillStyle = C.muted; ctx.textBaseline = "middle";
    ctx.fillText(p.m.title.toUpperCase(), x + 50 * S, y + bh / 2);
    const fs = 13.5 * S, lh = 19 * S, pad = 12 * S; let cy = y + bh + pad + fs * .5;
    ctx.font = `500 ${fs}px "IBM Plex Mono", Consolas, monospace`;
    ctx.save(); ctx.beginPath(); ctx.rect(x, y + bh, w, h - bh); ctx.clip();
    const maxLines = Math.floor((h - bh - pad * 2) / lh);
    if (p.m.kind === "term") {
      const m = p.m;
      const all = m.lines.slice(); if (m.cur) all.push(m.cur.parts);
      const vis = all.slice(-maxLines);
      vis.forEach((parts, li) => {
        let cx = x + pad; const isCur = m.cur && li === vis.length - 1;
        let budget = isCur ? m.cur.shown : 1e9;
        parts.forEach((pt) => {
          const s = pt[0].slice(0, Math.max(0, budget)); budget -= pt[0].length;
          ctx.fillStyle = COL[pt[1]] || C.text; ctx.fillText(s, cx, cy); cx += ctx.measureText(s).width;
        });
        if (isCur && Math.floor(m.blink / 500) % 2 === 0) { ctx.fillStyle = C.blue; ctx.fillRect(cx + 2 * S, cy - fs * .55, 6 * S, fs * 1.1); }
        cy += lh;
      });
      if (!m.cur && Math.floor(m.blink / 500) % 2 === 0) { ctx.fillStyle = C.blue; ctx.fillText("$", x + pad, cy); ctx.fillRect(x + pad + 12 * S, cy - fs * .55, 6 * S, fs * 1.1); }
    } else {
      const m = p.m;
      ctx.fillStyle = C.muted; ctx.fillText(`stream  ·  ${m.tps} tok/s  ·  temp 0.1`, x + pad, cy); cy += lh * 1.2;
      const shown = m.text.slice(0, m.pos), words = shown.split(" ");
      const maxW = w - pad * 2; let line = "", lines = [];
      words.forEach((wd) => { const test = line ? line + " " + wd : wd; if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = wd; } else line = test; });
      lines.push(line);
      lines.slice(-(maxLines - 2)).forEach((ln, li, arr) => {
        const last = li === arr.length - 1;
        ctx.fillStyle = C.text; ctx.fillText(ln, x + pad, cy);
        if (last && m.pos < m.text.length) {
          const tail = ln.slice(-5), tw = ctx.measureText(tail).width, lw = ctx.measureText(ln).width;
          ctx.fillStyle = C.blueSoft; ctx.fillRect(x + pad + lw - tw, cy - fs * .6, tw + 2 * S, fs * 1.2);
          ctx.fillStyle = C.blue; ctx.fillText(tail, x + pad + lw - tw, cy);
        }
        cy += lh;
      });
    }
    ctx.restore();
  }

  const states = films.map(build);
  const canFilter = "filter" in CanvasRenderingContext2D.prototype;
  function blit(s) {                                       // one blur pass at half resolution, then the GPU only upscales
    const v = s.vctx; v.clearRect(0, 0, s.cv.width, s.cv.height);
    if (canFilter) v.filter = "blur(2px) saturate(1.05)";
    v.drawImage(s.off, 0, 0);
    if (canFilter) v.filter = "none";
  }
  if (rm) {
    states.forEach((s) => {
      s.ctx.clearRect(0, 0, s.cv.width, s.cv.height);
      s.panes.forEach((p) => { for (let k = 0; k < 40; k++) p.m.kind === "term" ? stepTerm(p.m, 400) : stepTok(p.m, 400); drawPane(s.ctx, p, s.S, 0); });
      blit(s);
    });
    return;
  }

  let last = performance.now();
  function frame(now) {
    const dt = Math.min(80, now - last); last = now;
    if (!document.hidden) states.forEach((s) => {
      s.panes.forEach((p) => p.m.kind === "term" ? stepTerm(p.m, dt) : stepTok(p.m, dt));
      if (!s.visible) return;
      if (now - s.last < 50) return;                       // 20 fps is plenty under a blur
      s.last = now;
      const ctx = s.ctx; ctx.clearRect(0, 0, s.cv.width, s.cv.height);
      ctx.strokeStyle = "rgba(45,114,210,.05)"; ctx.lineWidth = 1;
      for (let gx = 0; gx < s.cv.width; gx += 48 * s.S) { ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, s.cv.height); ctx.stroke(); }
      for (let gy = 0; gy < s.cv.height; gy += 48 * s.S) { ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(s.cv.width, gy); ctx.stroke(); }
      s.panes.forEach((p) => drawPane(ctx, p, s.S, now));
      blit(s);
    });
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  window.BgTerm = { states };
})();
