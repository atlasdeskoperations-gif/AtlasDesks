/* Atlas Vision demo on the marketing site — nothing scripted.
   1. Seven short stock clips rotate as if they were seven cameras on one site (or the visitor's own camera).
   2. YOLOv8n (the same detector Atlas Vision runs) executes IN THE BROWSER via onnxruntime-web (WASM) on every frame;
      boxes are drawn on the overlay canvas and counts are tallied.
   3. Every few seconds the current frame + counts go to /api/vision/demo, which streams the vision model's narration
      back token by token (SSE). Each finished narration becomes an EVENT in the browser-side log.
   4. "Ask the cameras" posts the question + that event log to /api/vision/demo/ask: the server retrieves the best
      matching events, tells us which ([#n], highlighted), then streams the model's answer. Nothing is stored server side.
   If the model is unreachable the panels say so — no fake text. */
(function () {
  const sec = document.getElementById("vision");
  if (!sec) return;
  const $ = (id) => document.getElementById(id);
  const video = $("visVideo"), overlay = $("visOverlay"), fpsEl = $("visFps"), inferEl = $("visInfer"),
        countsEl = $("visCounts"), streamEl = $("visStream"), stateEl = $("visLlmState"), detState = $("visDetState"),
        camBtn = $("visCam"), clipBtn = $("visClip"), bar = $("visBar"), camName = $("visCamName"), camDots = $("visCams"),
        logEl = $("visLog"), logCount = $("visLogCount"), askForm = $("visAskForm"), askInput = $("visAskInput"),
        askState = $("visAskState"), answerEl = $("visAnswer"), chips = $("visChips");
  const ORT_URL = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/";
  const MODEL_URL = "models/yolov8n.onnx";
  const SIZE = 416, CONF = 0.35, IOU = 0.45, LLM_EVERY_MS = 6000, LLM_MAX_SIDE = 448, MAX_EVENTS = 40;
  // Stock footage (Mixkit, free licence) standing in for one site's cameras.
  const CAMS = [
    { id: "aisle",     name: "Aisle 4",           area: "shop floor" },
    { id: "fridges",   name: "Fridges",           area: "shop floor" },
    { id: "produce",   name: "Produce",           area: "shop floor" },
    { id: "stockroom", name: "Stock room",        area: "back of house" },
    { id: "factory",   name: "Factory floor",     area: "production" },
    { id: "clinic",    name: "Treatment room 2",  area: "clinic" },
    { id: "meeting",   name: "Meeting room",      area: "office" },
  ];
  const COCO = ["person","bicycle","car","motorcycle","airplane","bus","train","truck","boat","traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch","potted plant","bed","dining table","toilet","tv","laptop","mouse","remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book","clock","vase","scissors","teddy bear","hair drier","toothbrush"];
  const PALETTE = { person: "#2d72d2", car: "#1c8f5f", truck: "#1c8f5f", bus: "#1c8f5f", motorcycle: "#1c8f5f", bicycle: "#1c8f5f", bottle: "#a66a00", cup: "#a66a00", chair: "#7c5cd6", laptop: "#7c5cd6", tv: "#7c5cd6", refrigerator: "#0e7c86" };

  let session = null, running = false, started = false, useCam = false, stream = null, camIdx = 0;
  let lastCounts = {}, lastText = "", llmBusy = false, llmNext = 0, llmBackoff = 0, askBusy = false;
  const events = []; let evN = 0;
  const work = document.createElement("canvas"); work.width = SIZE; work.height = SIZE; const wctx = work.getContext("2d", { willReadFrequently: true });
  const snap = document.createElement("canvas"); const sctx = snap.getContext("2d");
  const octx = overlay.getContext("2d");

  const setDet = (t, cls) => { if (detState) { detState.textContent = t; detState.className = "vis-state " + (cls || ""); } };
  const setLlm = (t, cls) => { if (stateEl) { stateEl.textContent = t; stateEl.className = "vis-state " + (cls || ""); } };
  const setAsk = (t, cls) => { if (askState) { askState.textContent = t; askState.className = "vis-state " + (cls || ""); } };
  const cam = () => (useCam ? { id: "you", name: "Your camera", area: "visitor" } : CAMS[camIdx]);
  const clock = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  // ---- camera rotation -------------------------------------------------------------------------------
  CAMS.forEach(() => { const i = document.createElement("i"); camDots.appendChild(i); });
  function showCam(i) {
    camIdx = (i + CAMS.length) % CAMS.length;
    const c = CAMS[camIdx]; camName.textContent = c.name;
    Array.from(camDots.children).forEach((d, k) => d.classList.toggle("on", k === camIdx));
    video.srcObject = null; video.src = "video/cam-" + c.id + ".mp4"; video.loop = false; video.play().catch(() => {});
    lastText = ""; llmNext = performance.now() + 1200;   // narrate this camera soon after it appears
  }
  video.addEventListener("ended", () => { if (!useCam) showCam(camIdx + 1); });
  video.addEventListener("error", () => { if (!useCam) showCam(camIdx + 1); });

  // ---- load onnxruntime + model lazily (12 MB), with a progress bar --------------------------
  function loadScript(src) { return new Promise((res, rej) => { const s = document.createElement("script"); s.src = src; s.onload = res; s.onerror = () => rej(new Error("failed to load " + src)); document.head.appendChild(s); }); }
  async function fetchWithProgress(url, onp) {
    const r = await fetch(url); if (!r.ok) throw new Error("model HTTP " + r.status);
    const total = +r.headers.get("content-length") || 0; const reader = r.body.getReader(); const chunks = []; let got = 0;
    for (;;) { const { done, value } = await reader.read(); if (done) break; chunks.push(value); got += value.length; onp(total ? got / total : 0); }
    const out = new Uint8Array(got); let o = 0; chunks.forEach((c) => { out.set(c, o); o += c.length; }); return out;
  }
  async function loadDetector() {
    setDet("loading runtime…", "busy");
    await loadScript(ORT_URL + "ort.min.js");
    const ort = window.ort; ort.env.wasm.wasmPaths = ORT_URL; ort.env.wasm.numThreads = 1; ort.env.wasm.simd = true;
    setDet("downloading yolov8n.onnx…", "busy");
    const buf = await fetchWithProgress(MODEL_URL, (p) => { if (bar) bar.style.transform = "scaleX(" + p.toFixed(3) + ")"; });
    setDet("compiling…", "busy");
    const opts = { executionProviders: ["wasm"], graphOptimizationLevel: "all" };
    try {                                                   // run the model in a worker so scrolling/typing never stalls
      ort.env.wasm.proxy = true;
      session = await ort.InferenceSession.create(buf, opts);
    } catch (e) {
      ort.env.wasm.proxy = false;                           // no worker allowed here → main thread, throttled in loop()
      session = await ort.InferenceSession.create(buf, opts);
    }
    if (bar) bar.style.opacity = "0";
    setDet("yolov8n · live", "ok");
  }

  // ---- inference ------------------------------------------------------------------------------
  function letterbox() {
    const vw = video.videoWidth, vh = video.videoHeight; if (!vw || !vh) return null;
    const s = Math.min(SIZE / vw, SIZE / vh), nw = Math.round(vw * s), nh = Math.round(vh * s), dx = (SIZE - nw) >> 1, dy = (SIZE - nh) >> 1;
    wctx.fillStyle = "#727272"; wctx.fillRect(0, 0, SIZE, SIZE); wctx.drawImage(video, dx, dy, nw, nh);
    const px = wctx.getImageData(0, 0, SIZE, SIZE).data, n = SIZE * SIZE, f = new Float32Array(3 * n);
    for (let i = 0, j = 0; i < n; i++, j += 4) { f[i] = px[j] / 255; f[i + n] = px[j + 1] / 255; f[i + 2 * n] = px[j + 2] / 255; }
    return { f, s, dx, dy, vw, vh };
  }
  function decode(out, lb) {
    const d = out.data, N = out.dims[2], nc = out.dims[1] - 4, boxes = [];
    for (let i = 0; i < N; i++) {
      let best = 0, cls = -1;
      for (let c = 0; c < nc; c++) { const v = d[(4 + c) * N + i]; if (v > best) { best = v; cls = c; } }
      if (best < CONF) continue;
      const cx = d[i], cy = d[N + i], w = d[2 * N + i], h = d[3 * N + i];
      boxes.push({ x1: (cx - w / 2 - lb.dx) / lb.s, y1: (cy - h / 2 - lb.dy) / lb.s, x2: (cx + w / 2 - lb.dx) / lb.s, y2: (cy + h / 2 - lb.dy) / lb.s, conf: best, cls });
    }
    boxes.sort((a, b) => b.conf - a.conf);
    const keep = [];
    for (const b of boxes) {
      let ok = true;
      for (const k of keep) { if (k.cls !== b.cls) continue; const ix = Math.max(0, Math.min(b.x2, k.x2) - Math.max(b.x1, k.x1)), iy = Math.max(0, Math.min(b.y2, k.y2) - Math.max(b.y1, k.y1)), inter = ix * iy, u = (b.x2 - b.x1) * (b.y2 - b.y1) + (k.x2 - k.x1) * (k.y2 - k.y1) - inter; if (inter / u > IOU) { ok = false; break; } }
      if (ok) keep.push(b); if (keep.length >= 40) break;
    }
    return keep;
  }
  function draw(dets, lb) {
    const W = overlay.clientWidth, H = overlay.clientHeight;
    if (overlay.width !== W || overlay.height !== H) { overlay.width = W; overlay.height = H; }
    // the video is object-fit: contain — map source pixels onto the letterboxed display box
    const s = Math.min(W / lb.vw, H / lb.vh), ox = (W - lb.vw * s) / 2, oy = (H - lb.vh * s) / 2;
    octx.clearRect(0, 0, W, H);
    octx.lineWidth = 2; octx.font = "600 11px 'IBM Plex Mono', Consolas, monospace"; octx.textBaseline = "top";
    for (const b of dets) {
      const label = COCO[b.cls] || "obj", col = PALETTE[label] || "#7c5cd6", x = ox + b.x1 * s, y = oy + b.y1 * s, w = (b.x2 - b.x1) * s, h = (b.y2 - b.y1) * s;
      octx.strokeStyle = col; octx.strokeRect(x, y, w, h);
      const txt = label + " " + Math.round(b.conf * 100) + "%", tw = octx.measureText(txt).width + 10;
      octx.fillStyle = col; octx.fillRect(x - 1, Math.max(0, y - 16), tw, 16); octx.fillStyle = "#fff"; octx.fillText(txt, x + 4, Math.max(0, y - 16) + 2);
    }
  }
  function tally(dets) {
    const c = {}; dets.forEach((b) => { const l = COCO[b.cls] || "obj"; c[l] = (c[l] || 0) + 1; }); lastCounts = c;
    const parts = Object.entries(c).sort((a, b) => b[1] - a[1]).map(([k, v]) => "<b>" + v + "</b> " + k + (v > 1 && k === "person" ? "s" : ""));
    countsEl.innerHTML = parts.length ? parts.join(" <i>·</i> ") : "<span class='muted'>nothing above " + Math.round(CONF * 100) + "% confidence</span>";
  }

  let frames = 0, fpsT = performance.now(), lastRun = 0;
  const MIN_GAP_MS = 120;                                  // ~8 inferences/s is plenty for a demo feed
  async function loop() {
    if (!running) return;
    if (performance.now() - lastRun < MIN_GAP_MS) { requestAnimationFrame(loop); return; }
    if (video.readyState >= 2 && !video.paused && session) {
      lastRun = performance.now();
      const lb = letterbox();
      if (lb) {
        const t0 = performance.now();
        const out = await session.run({ images: new window.ort.Tensor("float32", lb.f, [1, 3, SIZE, SIZE]) });
        const ms = performance.now() - t0;
        const dets = decode(out.output0, lb); draw(dets, lb); tally(dets);
        inferEl.textContent = Math.round(ms) + " ms";
        frames++; const now = performance.now(); if (now - fpsT > 1000) { fpsEl.textContent = (frames * 1000 / (now - fpsT)).toFixed(1) + " fps"; frames = 0; fpsT = now; }
        if (now >= llmNext && !llmBusy) describe();
      }
    }
    requestAnimationFrame(loop);
  }

  // ---- SSE reader shared by narration and ask ------------------------------------------------------
  async function readSSE(resp, onEvent) {
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); const lines = buf.split("\n"); buf = lines.pop();
      for (const ln of lines) { if (!ln.startsWith("data:")) continue; let ev; try { ev = JSON.parse(ln.slice(5)); } catch (e) { continue; } onEvent(ev); }
    }
  }

  // ---- narration: frame + counts → server → streamed tokens → event ---------------------------------
  function frameJpeg() {
    const vw = video.videoWidth, vh = video.videoHeight, s = Math.min(1, LLM_MAX_SIDE / Math.max(vw, vh));
    snap.width = Math.round(vw * s); snap.height = Math.round(vh * s); sctx.drawImage(video, 0, 0, snap.width, snap.height);
    return snap.toDataURL("image/jpeg", 0.72);
  }
  function addEvent(c, counts, text) {
    const e = { n: ++evN, time: clock(), cam: c.name, counts: Object.assign({}, counts), text };
    events.push(e); while (events.length > MAX_EVENTS) events.shift();
    logEl.querySelectorAll(".hint").forEach((h) => h.remove());
    const li = document.createElement("li"); li.dataset.n = e.n;
    const cs = Object.entries(e.counts).sort((a, b) => b[1] - a[1]).map(([k, v]) => v + " " + k).join(", ") || "nothing detected";
    li.innerHTML = "<span class='n'>#" + e.n + "</span><span class='t'>" + e.time + "</span><span class='c' title='" + e.cam + "'>" + e.cam + "</span><span class='x'>" + cs + " — " + text.replace(/</g, "&lt;") + "</span>";
    logEl.prepend(li); while (logEl.children.length > MAX_EVENTS) logEl.removeChild(logEl.lastChild);
    logCount.textContent = events.length + " event" + (events.length === 1 ? "" : "s") + " · " + new Set(events.map((x) => x.cam)).size + " cameras";
  }
  async function describe() {
    llmBusy = true; const t0 = performance.now(), c = cam(), counts = lastCounts;
    streamEl.querySelectorAll(".hint").forEach((h) => h.remove());
    const cur = document.createElement("p"); cur.className = "cur"; streamEl.appendChild(cur); streamEl.scrollTop = streamEl.scrollHeight;
    setLlm("thinking…", "busy");
    let text = "", ok = false;
    try {
      const r = await fetch("/api/vision/demo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image: frameJpeg(), counts, prev: lastText, source: c.name + " camera, " + c.area }) });
      if (!r.ok || !r.body) { let msg = "HTTP " + r.status; try { const j = JSON.parse((await r.text()).replace(/^data:\s*/m, "").trim().split("\n")[0]); if (j.error) msg = j.error; } catch (e) {} throw new Error(msg); }
      await readSSE(r, (ev) => {
        if (ev.t) { text += ev.t; cur.textContent = text; streamEl.scrollTop = streamEl.scrollHeight; setLlm("streaming · " + ev.model, "busy"); const bd = $("visModelBadge"); if (bd && ev.model) bd.textContent = ev.model + " · streaming"; }
        if (ev.error) throw new Error(ev.error);
        if (ev.done) { ok = true; setLlm((ev.model || "model") + " · " + Math.round((performance.now() - t0) / 100) / 10 + " s", "ok"); }
      });
      if (!ok && !text) throw new Error("empty answer");
      lastText = text; cur.className = "done"; llmBackoff = 0;
      const stamp = document.createElement("time"); stamp.innerHTML = "<b>" + c.name + "</b> · " + clock(); cur.prepend(stamp);
      while (streamEl.children.length > 4) streamEl.removeChild(streamEl.firstChild);
      addEvent(c, counts, text);
    } catch (e) {
      cur.className = "err"; cur.textContent = "vision model unavailable: " + (e.message || e) + " — detections above are still real.";
      llmBackoff = Math.min(60000, (llmBackoff || 6000) * 2); setLlm("offline · retry in " + Math.round(llmBackoff / 1000) + " s", "err");
    }
    llmBusy = false; llmNext = performance.now() + (llmBackoff || LLM_EVERY_MS);
  }

  // ---- ask the cameras: question + event log → retrieval → streamed answer ----------------------------
  async function ask(q) {
    q = (q || "").trim(); if (!q || askBusy) return;
    if (!events.length) { answerEl.innerHTML = "<p class='err'>No events yet — let the analyst narrate a couple of frames first.</p>"; return; }
    askBusy = true; setAsk("retrieving…", "busy");
    logEl.querySelectorAll("li.used").forEach((li) => li.classList.remove("used"));
    answerEl.innerHTML = ""; const p = document.createElement("p"); p.className = "cur"; answerEl.appendChild(p);
    const usedEl = document.createElement("span"); usedEl.className = "used"; p.appendChild(usedEl); const body = document.createTextNode(""); p.appendChild(body);
    let text = "", ok = false; const t0 = performance.now();
    try {
      const r = await fetch("/api/vision/demo/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, events: events.map((e) => ({ n: e.n, time: e.time, camera: e.cam, counts: e.counts, text: e.text })) }) });
      if (!r.ok || !r.body) { let msg = "HTTP " + r.status; try { const j = JSON.parse((await r.text()).replace(/^data:\s*/m, "").trim().split("\n")[0]); if (j.error) msg = j.error; } catch (e) {} throw new Error(msg); }
      await readSSE(r, (ev) => {
        if (ev.used) { usedEl.textContent = "retrieved " + ev.used.length + " of " + ev.of + " events: " + ev.used.map((n) => "#" + n).join(" "); ev.used.forEach((n) => { const li = logEl.querySelector("li[data-n='" + n + "']"); if (li) li.classList.add("used"); }); setAsk("answering…", "busy"); }
        if (ev.t) { text += ev.t; body.textContent = text; }
        if (ev.error) throw new Error(ev.error);
        if (ev.done) { ok = true; setAsk((ev.model || "model") + " · " + Math.round((performance.now() - t0) / 100) / 10 + " s", "ok"); }
      });
      if (!ok && !text) throw new Error("empty answer");
      p.className = "done"; body.textContent = ""; const html = text.replace(/</g, "&lt;").replace(/\[#(\d+)\]/g, "<cite>[#$1]</cite>"); const span = document.createElement("span"); span.innerHTML = html; p.appendChild(span);
    } catch (e) {
      p.className = "err"; p.textContent = "could not answer: " + (e.message || e); setAsk("offline", "err");
    }
    askBusy = false;
  }
  askForm && askForm.addEventListener("submit", (ev) => { ev.preventDefault(); ask(askInput.value); });
  chips && chips.addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (!b) return; askInput.value = b.textContent; ask(b.textContent); });

  // ---- sources ---------------------------------------------------------------------------------------
  async function toCamera() {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 960 } }, audio: false });
      video.pause(); video.removeAttribute("src"); video.srcObject = stream; video.loop = false; await video.play();
      useCam = true; camBtn.hidden = true; clipBtn.hidden = false; camName.textContent = "Your camera"; lastText = ""; llmNext = 0;
      Array.from(camDots.children).forEach((d) => d.classList.remove("on"));
    } catch (e) { setDet("camera blocked: " + (e.name || e), "err"); }
  }
  async function toClip() {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    useCam = false; camBtn.hidden = false; clipBtn.hidden = true; showCam(camIdx);
  }
  camBtn && camBtn.addEventListener("click", toCamera);
  clipBtn && clipBtn.addEventListener("click", toClip);
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) camBtn && (camBtn.hidden = true);

  async function start() {
    if (started) return; started = true;
    showCam(0);
    try { await loadDetector(); } catch (e) { setDet("detector failed: " + (e.message || e), "err"); return; }
    running = true; loop();
  }
  // start only once the section is actually on screen and the page has finished loading — never pull 12 MB
  // while the hero is still painting, and never for visitors who don't scroll here
  const afterLoad = (fn) => document.readyState === "complete" ? setTimeout(fn, 300) : window.addEventListener("load", () => setTimeout(fn, 300), { once: true });
  new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) afterLoad(start); }, { rootMargin: "0px", threshold: 0.2 }).observe(sec);
  // pause work when the section is far off-screen or the tab is hidden
  new IntersectionObserver((es) => { const on = es.some((e) => e.isIntersecting); if (started && session) { running = on; if (on) loop(); } if (on) video.play().catch(() => {}); else if (!useCam) video.pause(); }, { rootMargin: "100px" }).observe(sec);
  document.addEventListener("visibilitychange", () => { if (document.hidden) { running = false; } else if (started && session) { running = true; loop(); } });
})();
