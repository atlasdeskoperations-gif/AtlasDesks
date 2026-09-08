/* Hero: Atlas deploying a team of agents — LIVE when the server can run the real orchestrator
   (GET /api/orch/live polls a real run on the free model: every token, spawn and hand-back as it happens),
   otherwise a REPLAY of a real run captured on 1 Sep 2026 (window.ORCH, orch-data.js) with its original timing.
   One event-driven engine draws both: nodes spawn out of Atlas, tokens travel the edges, rings fill, checks land. */
(function () {
  const root = document.getElementById("orch");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const canvas = $("orchCanvas"), ctx = canvas.getContext("2d"), panels = $("orchPanels"), merge = $("orchMerge");
  const tagEl = $("orchTag"), taskEl = $("orchTask"), detEl = $("orchDet"), modeEl = $("orchMode");
  const mergeB = merge.querySelector("p"), mergeQ = merge.querySelector(".q"), mergeK = merge.querySelector(".k");
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const PAL = ["#2d72d2", "#7c5cd6", "#1c8f5f", "#a66a00", "#c2412e", "#0e7c86", "#b0369a", "#5b6b7a"];
  const INK = "#10161d", OK = "#1c8f5f";
  const now = () => performance.now();

  // ---- engine state ------------------------------------------------------------------------------------
  let A = {}, order = [], particles = [], queue = [], runStart = 0, meta = { live: false, model: "", label: "" };
  let atlasText = "", atlasLastTok = 0, atlasPhase = "idle", doneEv = null, tokens = 0, approvals = 0, lastTick = now();
  const cards = {};
  function reset(m) {
    A = {}; order = []; particles = []; queue = []; atlasText = ""; atlasPhase = "idle"; doneEv = null; tokens = 0; approvals = 0;
    meta = Object.assign({ live: false, model: "", label: "" }, m || {});
    panels.innerHTML = ""; Object.keys(cards).forEach((k) => delete cards[k]);
    mergeB.textContent = ""; mergeQ.hidden = true; merge.classList.remove("live", "done"); mergeK.textContent = "Atlas · planning";
    if (modeEl) { modeEl.textContent = meta.live ? "live" : "replay"; modeEl.className = "orch-mode " + (meta.live ? "live" : "replay"); }
  }
  function colourFor(id) { let h = 0; for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return PAL[h % PAL.length]; }
  const L = { atlas: { x: 0, y: 0 }, owner: { x: 0, y: 0 }, W: 0, H: 0 };
  function layout() {
    const dpr = Math.min(2, window.devicePixelRatio || 1), W = canvas.clientWidth, H = canvas.clientHeight;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) { canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    L.W = W; L.H = H; L.atlas = { x: 46, y: H / 2 }; L.owner = { x: W - 46, y: H / 2 };
    const byDepth = {}; order.forEach((k) => { const d = A[k].depth || 1; (byDepth[d] = byDepth[d] || []).push(k); });
    Object.keys(byDepth).forEach((d) => {
      const ks = byDepth[d], n = ks.length, x = 46 + (W - 92) * Math.min(0.78, 0.5 + 0.22 * (d - 1));
      ks.forEach((k, i) => { const gap = n > 1 ? Math.min(30, (H - 30) / (n - 1)) : 0; const y0 = H / 2 - gap * (n - 1) / 2; A[k].tx = x; A[k].ty = y0 + i * gap; });
    });
  }

  // ---- apply one event ------------------------------------------------------------------------------------
  function apply(ev) {
    const k = ev.k, id = ev.a || "system", inst = ev.inst || id, d = ev.d || {};
    if (k === "agent_start") {
      if (id === "atlas") { atlasPhase = "planning"; return; }
      const parent = d.parent || "atlas";
      const a = { id, inst, name: (d.name || id).replace(/^Atlas$/, id), parent, depth: parent === "atlas" || !A[parent] ? 1 : (A[parent].depth + 1), assignment: d.assignment || "", text: "", status: "assigned", tok: 0, born: now(), x: L.atlas.x, y: L.atlas.y, tx: 0, ty: 0, col: colourFor(id), ended: 0, lastTok: 0 };
      A[inst] = a; order.push(inst); layout();
      for (let i = 0; i < 3; i++) particles.push({ from: parent === "atlas" || !A[parent] ? L.atlas : A[parent], to: a, born: now() + i * 140, life: 700, col: a.col });
      card(a); atlasPhase = "delegating";
    } else if (k === "token") {
      const x = ev.x || ""; if (!x) return;
      tokens++;
      if (id === "atlas" || inst === "atlas") { if (atlasText && now() - atlasLastTok > 1500 && !/\s$/.test(atlasText)) atlasText += "\n"; atlasText += x; atlasLastTok = now(); if (atlasPhase === "delegating" && order.every((o) => A[o].status === "done")) atlasPhase = "merging"; mergeB.textContent = atlasText.slice(-420); merge.classList.add("live"); mergeK.textContent = atlasPhase === "merging" ? "Atlas · reviewing" : order.length ? "Atlas · coordinating" : "Atlas · planning"; return; }
      const a = A[inst] || A[id]; if (!a) return;
      a.text += x; a.tok++; a.status = "streaming"; a.lastTok = now();
      if (particles.length < 60 && a.tok % 3 === 0) particles.push({ from: a, to: a.parent !== "atlas" && A[a.parent] ? A[a.parent] : L.atlas, born: now(), life: 900, col: a.col });
      const c = cards[a.inst]; if (c) { c.b.textContent = a.text.slice(-260); c.st.textContent = "streaming"; c.el.className = "op live"; }
    } else if (k === "agent_end") {
      if (id === "atlas") return;
      const a = A[inst] || A[id]; if (!a) return;
      a.status = "done"; a.ended = now();
      for (let i = 0; i < 6; i++) particles.push({ from: a, to: a.parent !== "atlas" && A[a.parent] ? A[a.parent] : L.atlas, born: now() + i * 60, life: 700, col: a.col });
      const c = cards[a.inst]; if (c) { c.st.textContent = "done"; c.el.className = "op done"; }
      if (order.every((o) => A[o].status === "done")) atlasPhase = "merging";
    } else if (k === "approval") { approvals++; particles.push({ from: L.atlas, to: L.owner, born: now(), life: 900, col: OK });
    } else if (k === "done") {
      doneEv = ev; atlasPhase = "done"; merge.classList.add("done"); merge.classList.remove("live");
      const txt = (ev.x || "").split("\n---\n")[0].trim(); if (txt) mergeB.textContent = txt.slice(0, 460);
      mergeK.textContent = "Atlas · result"; mergeQ.hidden = false; mergeQ.textContent = approvals ? approvals + " action" + (approvals > 1 ? "s" : "") + " queued for owner approval" : (d.status === "done" ? "finished · verified by the desk" : "run " + (d.status || "ended"));
      for (let i = 0; i < 5; i++) particles.push({ from: L.atlas, to: L.owner, born: now() + i * 90, life: 800, col: OK });
    } else if (k === "error") { const c = cards[inst] || cards[id]; if (c) { c.st.textContent = "error"; c.el.className = "op err"; } }
  }
  function card(a) {
    const el = document.createElement("div"); el.className = "op asg"; el.style.setProperty("--c", a.col);
    el.innerHTML = "<div class='h'><b></b><span class='st'>assigned</span></div><div class='ins'></div><div class='b'></div>";
    el.querySelector("b").textContent = a.name + (a.inst.includes("#") ? " #" + a.inst.split("#")[1] : "");
    el.querySelector(".ins").textContent = a.assignment; panels.appendChild(el);
    cards[a.inst] = { el, b: el.querySelector(".b"), st: el.querySelector(".st") };
    while (panels.children.length > 8) { const first = panels.firstChild; panels.removeChild(first); }
  }

  // ---- drawing ------------------------------------------------------------------------------------------------
  function bez(a, b, t) { const mx = (a.x + b.x) / 2, u = 1 - t; return { x: u * u * u * a.x + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * b.x, y: u * u * u * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t * t * t * b.y }; }
  function edge(a, b, col, alpha, dashed) {
    const mx = (a.x + b.x) / 2; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
    ctx.strokeStyle = col; ctx.globalAlpha = alpha; ctx.lineWidth = 1.2; ctx.setLineDash(dashed ? [3, 4] : []); ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  function label(p, txt, dy) { ctx.fillStyle = INK; ctx.font = "600 10px 'IBM Plex Mono', Consolas, monospace"; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(txt.toUpperCase().slice(0, 14), p.x, p.y + dy); }
  function ring(p, r, col, frac, active, done) {
    ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fillStyle = "#fff"; ctx.fill(); ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.globalAlpha = .45; ctx.stroke(); ctx.globalAlpha = 1;
    if (frac > 0) { ctx.beginPath(); ctx.arc(p.x, p.y, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.min(1, frac)); ctx.strokeStyle = col; ctx.lineWidth = 2.2; ctx.stroke(); }
    if (active) { const t = now() / 600; ctx.beginPath(); ctx.arc(p.x, p.y, r + 4 + Math.sin(t) * 1.5, 0, Math.PI * 2); ctx.strokeStyle = col; ctx.globalAlpha = .3; ctx.lineWidth = 1; ctx.stroke(); ctx.globalAlpha = 1; ctx.beginPath(); ctx.arc(p.x + Math.cos(t * 2.3) * (r + 4), p.y + Math.sin(t * 2.3) * (r + 4), 2, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill(); }
    if (done) { ctx.beginPath(); ctx.arc(p.x, p.y, r - 1, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.6; ctx.beginPath(); ctx.moveTo(p.x - 3.2, p.y); ctx.lineTo(p.x - 0.8, p.y + 2.4); ctx.lineTo(p.x + 3.4, p.y - 2.6); ctx.stroke(); }
  }
  function render() {
    layout(); const t = now(); ctx.clearRect(0, 0, L.W, L.H);
    // node motion: spring toward slot
    order.forEach((k) => { const a = A[k]; const s = Math.min(1, (t - a.born) / 650), e = 1 - Math.pow(1 - s, 3); a.x = L.atlas.x + (a.tx - L.atlas.x) * e; a.y = L.atlas.y + (a.ty - L.atlas.y) * e; });
    // edges
    order.forEach((k) => { const a = A[k], from = a.parent !== "atlas" && A[a.parent] ? A[a.parent] : L.atlas; edge(from, a, a.col, a.status === "assigned" ? .35 : .8, a.status === "assigned"); });
    edge(L.atlas, L.owner, OK, doneEv || approvals ? .8 : .15, !(doneEv || approvals));
    // particles
    particles = particles.filter((p) => t - p.born < p.life);
    particles.forEach((p) => { if (t < p.born) return; const f = (t - p.born) / p.life, q = bez(p.from, p.to, f); ctx.beginPath(); ctx.arc(q.x, q.y, 2.4, 0, Math.PI * 2); ctx.fillStyle = p.col; ctx.globalAlpha = 1 - f * .6; ctx.fill(); ctx.globalAlpha = 1; });
    // nodes
    order.forEach((k) => { const a = A[k]; ring(a, 7, a.col, Math.min(1, a.tok / 220), a.status === "streaming" && t - a.lastTok < 1500, a.status === "done"); label(a, a.name, 10); });
    const atlasActive = atlasPhase === "planning" || atlasPhase === "merging" || t - atlasLastTok < 900;
    ring(L.atlas, 11, INK, atlasPhase === "done" ? 1 : (order.length ? order.filter((k) => A[k].status === "done").length / order.length : 0), atlasActive && atlasPhase !== "done", atlasPhase === "done"); label(L.atlas, "ATLAS", 15);
    ring(L.owner, 9, OK, doneEv ? 1 : 0, !!approvals && !doneEv, !!doneEv); label(L.owner, "OWNER", 13);
    // stats + tag
    const par = order.filter((k) => A[k].status === "streaming").length, elapsed = Math.max(0, (t - runStart) / 1000);
    $("stInst").textContent = 1 + order.length; $("stTok").textContent = tokens; $("stPar").textContent = par;
    $("stWall").textContent = (doneEv ? (doneEv.t || elapsed) : elapsed).toFixed(1) + " s";
    const src = meta.live ? "live · real run · " + (meta.model || "").split("/").pop() : "replay · real run · " + (meta.label || "");
    const ph = atlasPhase === "done" ? "finished" : atlasPhase === "merging" ? "atlas reviewing the results" : par ? par + " specialist" + (par > 1 ? "s" : "") + " streaming in parallel" : order.length ? "specialists assigned" : "atlas reading the job & planning";
    tagEl.textContent = src + " · " + (meta.hold || ph);
  }
  let orchVisible = true;
  new IntersectionObserver((es) => es.forEach((e) => { orchVisible = e.isIntersecting; }), { rootMargin: "120px" }).observe(root);
  function tick() {
    const t = now();
    while (queue.length && runStart + queue[0].t * 1000 <= t) apply(queue.shift());
    if (!orchVisible || document.hidden) { setTimeout(tick, 200); return; }   // keep the clock, skip the paint
    render();
    requestAnimationFrame(tick);
  }
  function push(evs) { evs.forEach((e) => queue.push(e)); queue.sort((a, b) => a.t - b.t); }

  // ---- replay source: a real run captured in orch-data.js, turned into the same event stream ---------------
  function replayEvents(D) {
    const E = [], names = Object.keys(D.plan.instructions);
    D.plan.tokens.forEach(([t, x]) => E.push({ t, k: "token", a: "atlas", inst: "atlas", x }));
    names.forEach((n, i) => E.push({ t: D.spawn_t + i * 0.05, k: "agent_start", a: n.toLowerCase(), inst: n.toLowerCase(), d: { name: n, parent: "atlas", assignment: D.plan.instructions[n] } }));
    names.forEach((n) => { const ag = D.agents[n]; ag.tokens.forEach(([t, x]) => E.push({ t, k: "token", a: n.toLowerCase(), inst: n.toLowerCase(), x })); E.push({ t: ag.last + 0.15, k: "agent_end", a: n.toLowerCase(), inst: n.toLowerCase() }); });
    D.merge.tokens.forEach(([t, x]) => E.push({ t, k: "token", a: "atlas", inst: "atlas", x }));
    E.push({ t: D.merge.last + 0.2, k: "approval", a: "atlas" });
    E.push({ t: D.merge.last + 0.4, k: "done", a: "system", x: D.merge.text, d: { status: "done" } });
    return E;
  }
  function startReplay() {
    const D = window.ORCH; if (!D) return;
    reset({ live: false, label: D.captured || "captured 1 sep 2026" });
    taskEl.textContent = D.task; detEl.textContent = D.detections ? "detector: " + D.detections : "";
    const T0 = 4.6, T_END = D.merge.last + 0.6, evs = replayEvents(D);
    runStart = now() - T0 * 1000; push(evs);
    setTimeout(() => { if (!meta.live) startReplay(); }, (T_END - T0 + 4) * 1000);
  }

  // ---- live source: poll a real run ------------------------------------------------------------------------------
  let liveRun = null, sinceIdx = 0, offline = false, pollTimer = 0, missed = 0;
  async function poll() {
    if (document.hidden) { pollTimer = setTimeout(poll, 2500); return; }
    try {
      const r = await fetch("/api/orch/live?since=" + (liveRun ? sinceIdx : 0) + (liveRun ? "&run=" + encodeURIComponent(liveRun) : ""), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json(); missed = 0;
      if (!j.live) { if (!offline) { offline = true; liveRun = null; startReplay(); } if (modeEl) modeEl.title = j.reason || ""; pollTimer = setTimeout(poll, 20000); return; }
      offline = false;
      if (j.run && j.run.id !== liveRun) {            // a new real run started (or first contact)
        liveRun = j.run.id; sinceIdx = 0; reset({ live: true, model: j.model || j.run.model || "" });
        taskEl.textContent = j.run.task || ""; detEl.textContent = j.run.desk ? "desk: " + j.run.desk + " · started " + new Date(j.run.started * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
      }
      if (j.run) { runStart = now() - (j.run.elapsed || 0) * 1000; if (j.events && j.events.length) { push(j.events); sinceIdx = j.idx; } }
      if (j.run && j.run.done && !queue.length) meta.hold = j.next_in > 0 ? "finished · next live run in " + Math.ceil(j.next_in) + " s" : "finished · next live run starting"; else meta.hold = "";
      if (!j.run) { if (!meta.live) reset({ live: true, model: j.model || "" }); taskEl.textContent = "Waiting for the next job…"; detEl.textContent = ""; meta.hold = j.next_in > 0 ? "next live run in " + Math.ceil(j.next_in) + " s" : "starting a live run"; }
      pollTimer = setTimeout(poll, j.run && !j.run.done ? 700 : 3000);
    } catch (e) {
      if (++missed > 3 && !offline) { offline = true; liveRun = null; startReplay(); }
      pollTimer = setTimeout(poll, 8000);
    }
  }

  if (rm) {                                            // reduced motion: draw the finished replay once, no animation
    const D = window.ORCH; if (D) { reset({ live: false, label: "captured 1 sep 2026" }); taskEl.textContent = D.task; replayEvents(D).forEach(apply); render(); }
    return;
  }
  tick();
  poll();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(pollTimer); poll(); } });
})();
