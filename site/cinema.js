/* Cinema — scroll-driven motion for the Atlas Desks site.
   GSAP + ScrollTrigger (+ Lenis inertia scroll when present). Everything reverses
   when you scroll back up. Falls back to IntersectionObserver reveals when GSAP is
   missing or the visitor prefers reduced motion. */
(function () {
  const rm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const G = window.gsap, ST = window.ScrollTrigger;
  window.Cinema = { ready: false };

  // ---- chrome: progress bar, nav glass, section dots ----------------------------
  const bar = document.createElement("div"); bar.className = "progress"; bar.innerHTML = "<i></i>"; document.body.appendChild(bar);
  const nav = document.querySelector(".nav");
  const onScroll = () => { nav && nav.classList.toggle("scrolled", window.scrollY > 24); };
  window.addEventListener("scroll", onScroll, { passive: true }); onScroll();
  const scenes = Array.from(document.querySelectorAll("[data-title]"));
  let dots = null;
  if (scenes.length > 1) {
    dots = document.createElement("nav"); dots.className = "dots"; dots.setAttribute("aria-label", "sections");
    scenes.forEach((s, i) => {
      const a = document.createElement("a"); a.href = "#" + (s.id || ("s" + i)); if (!s.id) s.id = "s" + i;
      a.innerHTML = "<i></i><span>" + s.dataset.title + "</span>"; dots.appendChild(a);
    });
    document.body.appendChild(dots);
  }
  const REVEAL = ".card,.step,.plan,.quote,.layer,.guard,.shots figure,details,.band,.rv,.proof>div,.cards.info .card,.formwrap,.list li";
  const HEAD = "section .eyebrow, section h2, section p.lead, .phero h1, .phero .lead, .phero .crumb";

  // ---- fallback --------------------------------------------------------------------
  document.querySelectorAll(".film video").forEach((v) => { if (rm) { v.remove(); return; } });
  if (!G || !ST || rm) {
    document.querySelectorAll(".film video").forEach((v) => {
      const webm = v.dataset.srcWebm, mp4 = v.dataset.srcMp4;
      if (mp4) { const s = document.createElement("source"); s.src = mp4; s.type = "video/mp4"; v.appendChild(s); }
      if (webm) { const s = document.createElement("source"); s.src = webm; s.type = "video/webm"; v.appendChild(s); }
      v.addEventListener("canplay", () => { v.classList.add("on"); v.play().catch(() => {}); }, { once: true }); v.load(); v.play().catch(() => {});
    });
    const els = document.querySelectorAll(REVEAL + "," + HEAD);
    els.forEach((el) => el.classList.add("rv"));
    const io = new IntersectionObserver((es) => es.forEach((e) => e.isIntersecting && e.target.classList.add("in")), { threshold: 0.1 });
    els.forEach((el) => io.observe(el));
    setTimeout(() => els.forEach((el) => el.classList.add("in")), 1500);
    if (dots) {
      const io2 = new IntersectionObserver((es) => es.forEach((e) => {
        if (e.isIntersecting) dots.querySelectorAll("a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + e.target.id));
      }), { threshold: 0.4 });
      scenes.forEach((s) => io2.observe(s));
    }
    return;
  }

  G.registerPlugin(ST);
  G.config({ nullTargetWarn: false });
  document.documentElement.classList.add("cine");

  // ---- inertia scroll --------------------------------------------------------------
  let lenis = null;
  if (window.Lenis) {
    lenis = new window.Lenis({ lerp: 0.13, smoothWheel: true, wheelMultiplier: 1 });
    lenis.on("scroll", () => { ST.update(); onScroll(); window.Orbit && window.Orbit.setScroll(window.scrollY); });
    G.ticker.add((t) => lenis.raf(t * 1000));
    G.ticker.lagSmoothing(0);
    document.documentElement.classList.add("lenis");
  } else {
    window.addEventListener("scroll", () => window.Orbit && window.Orbit.setScroll(window.scrollY), { passive: true });
  }
  const go = (target) => {
    const el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el) return;
    if (lenis) lenis.scrollTo(el, { offset: -70, duration: 1.4 }); else el.scrollIntoView({ behavior: "smooth" });
  };
  document.querySelectorAll('a[href^="#"]').forEach((a) => a.addEventListener("click", (e) => {
    const id = a.getAttribute("href"); if (id.length < 2 || !document.querySelector(id)) return;
    e.preventDefault(); go(id); history.replaceState(null, "", id);
  }));
  if (location.hash && document.querySelector(location.hash)) setTimeout(() => go(location.hash), 400);

  // ---- progress bar + dots ---------------------------------------------------------
  G.to(bar.firstChild, { scaleX: 1, ease: "none", scrollTrigger: { start: 0, end: "max", scrub: 0.3 } });
  if (dots) scenes.forEach((s) => ST.create({
    trigger: s, start: "top 55%", end: "bottom 45%",
    onToggle: (self) => { if (self.isActive) dots.querySelectorAll("a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + s.id)); },
  }));

  // ---- reveals (reverse on the way up) --------------------------------------------
  G.utils.toArray(HEAD).filter((el) => !el.closest(".pstep") && !el.closest(".band")).forEach((el) => G.from(el, {
    opacity: 0, y: 40, duration: 1, ease: "power3.out",
    scrollTrigger: { trigger: el, start: "top 90%", toggleActions: "play none none reverse" },
  }));
  G.utils.toArray("section, .phero, footer.site").forEach((sec) => {
    const items = sec.querySelectorAll(REVEAL);
    if (!items.length) return;
    G.set(items, { opacity: 0, y: 56 });
    ST.batch(items, {
      start: "top 90%",
      onEnter: (b) => G.to(b, { opacity: 1, y: 0, duration: 1, stagger: 0.09, ease: "power3.out", overwrite: true }),
      onLeaveBack: (b) => G.to(b, { opacity: 0, y: 56, duration: 0.45, overwrite: true }),
    });
  });

  // ---- hero: pinned, fades under the next scene ----------------------------------
  const hero = document.querySelector(".hero.cine");
  if (hero) {
    G.from(hero.querySelectorAll(".h-in > *"), { opacity: 0, y: 44, stagger: 0.11, duration: 1.1, ease: "power3.out", delay: 0.15 });
    const showcase = hero.querySelector(".desk, .orch");
    if (showcase) G.from(showcase, { opacity: 0, y: 70, duration: 1.4, ease: "power3.out", delay: 0.45 });
    if (hero.querySelector(".scroll-hint")) G.from(hero.querySelector(".scroll-hint"), { opacity: 0, y: -10, duration: 1, delay: 1.4 });
    G.to(hero.querySelector(".wrap"), { y: -140, opacity: 0, scale: 0.94, ease: "none", scrollTrigger: { trigger: hero, start: "top top", end: "bottom 25%", scrub: true } });
    if (hero.querySelector(".scroll-hint")) G.to(hero.querySelector(".scroll-hint"), { opacity: 0, ease: "none", scrollTrigger: { trigger: hero, start: "top top", end: "20% top", scrub: true } });
    ST.create({ trigger: hero, start: "top top", end: "bottom top", pin: true, pinSpacing: false });
    // desk mockup: rows arrive one by one, loop
    const rows = hero.querySelectorAll(".desk .row");
    if (rows.length) {
      const tl = G.timeline({ repeat: -1, repeatDelay: 2.2, delay: 1.6 });
      tl.from(rows, { autoAlpha: 0, x: -18, duration: 0.55, stagger: 0.7, ease: "power2.out" })
        .to(hero.querySelector(".desk .row.approve"), { borderColor: "#4c90f0", duration: 0.5, yoyo: true, repeat: 3 }, "+=0.4")
        .to(hero.querySelector(".desk .row.approve .a span:first-child"), { scale: 0.92, duration: 0.12, yoyo: true, repeat: 1 })
        .to(hero.querySelector(".desk .sent"), { autoAlpha: 1, y: 0, duration: 0.5 })
        .to(rows, { autoAlpha: 0, x: 18, duration: 0.4, stagger: 0.06 }, "+=1.8")
        .set(hero.querySelector(".desk .sent"), { autoAlpha: 0, y: 8 });
    }
  }

  // ---- pipeline: pinned, scrubbed; the background camera follows -----------------
  const pipe = document.querySelector("#pipeline");
  if (pipe) {
    const steps = pipe.querySelectorAll(".pstep"), n = steps.length;
    const fill = pipe.querySelector(".pipe-bar i"), lab = pipe.querySelector(".pipe-count");
    G.set(steps, { autoAlpha: 0, y: 48 });
    const tl = G.timeline({
      scrollTrigger: {
        trigger: pipe, start: "top top", end: "+=" + (n * 90) + "%", pin: true, scrub: 0.7, anticipatePin: 1,
        onLeave: () => window.Orbit && window.Orbit.setStage(-1),
        onLeaveBack: () => window.Orbit && window.Orbit.setStage(-1),
        onEnter: () => window.Orbit && window.Orbit.setStage(0),
        onEnterBack: () => window.Orbit && window.Orbit.setStage(n - 1),
      },
    });
    tl.eventCallback("onUpdate", () => {
      const s = Math.min(n - 1, Math.floor(tl.time() / 3.4));
      window.Orbit && window.Orbit.setStage(s);
      if (lab) lab.textContent = "0" + (s + 1) + " / 0" + n;
    });
    steps.forEach((s, i) => {
      tl.to(s, { autoAlpha: 1, y: 0, duration: 1, ease: "power2.out" });
      if (i < n - 1) tl.to(s, { autoAlpha: 0, y: -48, duration: 1, ease: "power2.in" }, "+=1.4");
    });
    if (fill) G.to(fill, { scaleY: 1, ease: "none", scrollTrigger: { trigger: pipe, start: "top top", end: "+=" + (n * 90) + "%", scrub: 0.7 } });
  }

  // ---- gallery: horizontal scroll while pinned (desktop) --------------------------
  const track = document.querySelector(".shots-track");
  if (track) {
    const mm = G.matchMedia();
    mm.add("(min-width: 821px)", () => {
      const dist = () => track.scrollWidth - window.innerWidth + 48;
      G.to(track, {
        x: () => -dist(), ease: "none",
        scrollTrigger: { trigger: "#demo", start: "top top", end: () => "+=" + dist(), pin: true, scrub: 0.8, invalidateOnRefresh: true, anticipatePin: 1 },
      });
    });
  }

  // ---- parallax bits ---------------------------------------------------------------
  G.utils.toArray("[data-speed]").forEach((el) => G.to(el, {
    y: () => (1 - parseFloat(el.dataset.speed)) * -220, ease: "none",
    scrollTrigger: { trigger: el, start: "top bottom", end: "bottom top", scrub: true },
  }));
  // ---- background film: load lazily, fade in when it can play; parallax drift ------
  document.querySelectorAll(".film video").forEach((v) => {
    const webm = v.dataset.srcWebm, mp4 = v.dataset.srcMp4;
    if (mp4) { const s = document.createElement("source"); s.src = mp4; s.type = "video/mp4"; v.appendChild(s); }
    if (webm) { const s = document.createElement("source"); s.src = webm; s.type = "video/webm"; v.appendChild(s); }
    v.addEventListener("canplay", () => { v.classList.add("on"); v.play().catch(() => {}); }, { once: true });
    v.load(); v.play().catch(() => {});
    G.to(v, { yPercent: 10, ease: "none", scrollTrigger: { trigger: v.closest("header, section"), start: "top top", end: "bottom top", scrub: 0.5 } });
  });

  window.addEventListener("load", () => ST.refresh());
  window.Cinema.ready = true;
  window.Cinema.go = go;
})();
