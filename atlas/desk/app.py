"""Atlas Desk portal (Flask).

  /            marketing site (atlas/site)
  /desk        client portal SPA (accounts + one or more desks per account)
  /api/...     JSON API used by the portal — every data endpoint is scoped to the current desk

Run:  py -m atlas.desk            (PORT env, default 8094)
Env:  DESK_MODE=demo|live|auto     demo = scripted provider (no API key needed); auto = live if key present
      DESK_PROVIDER=openrouter     live mode provider (default: providers.json default_provider)
      DESK_SECRET=...              session-cookie secret (auto-generated into data/secret.key if unset)
      DESK_OPEN=1                  skip accounts (portal public on the built-in demo desk) — local dev only
      DEMO_DELAY=0.6               demo provider pacing
"""
from __future__ import annotations

import json
import os
import base64
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory, session, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

from .. import config as cfg
from .. import designer as DS
from .. import integrations as I
from .. import metrics as MX
from .. import templates
from .. import tools as T
from .. import vision as V
from . import scheduler
from ..orchestrator import Event, Orchestrator
from ..store import Store

ROOT = cfg.ROOT
SITE_DIR = ROOT / "site"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_PATH = cfg.DATA_DIR / "desk.db"

app = Flask(__name__, static_folder=None)
# DATABASE_URL (postgres://...) makes accounts, desks and approvals survive deploys; without it SQLite in DATA_DIR.
store = Store(DB_PATH, url=os.environ.get("DATABASE_URL", "").strip() or None)


def _secret() -> str:
    env = os.environ.get("DESK_SECRET", "").strip()
    if env:
        return env
    f = cfg.DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")), PERMANENT_SESSION_LIFETIME=30 * 86400)
OPEN = os.environ.get("DESK_OPEN", "").strip() in ("1", "true", "yes")
DEFAULT_TEMPLATE = os.environ.get("DESK_TEMPLATE", "sales_desk")

_runs: dict[str, dict[str, Any]] = {}          # run_id -> {"events": [...], "thread":..., "orch":..., "desk_id":...}
_runs_lock = threading.Lock()
_feed: list[dict[str, Any]] = []               # global live feed (all desks, incl. token deltas) for /api/stream
_feed_lock = threading.Lock()
_feed_seq = 0
_FEED_MAX = 30000
_last_error: dict[int, dict[str, Any]] = {}     # desk_id -> most recent error event (surfaced on the health panel)
_alert_sent: dict[str, float] = {}              # alert key -> last notification time (rate limit)
RUN_MAX_S = float(os.environ.get("RUN_MAX_S", "900"))          # watchdog: a run older than this is killed and marked failed
_BOOT_TS = time.time()


# ---------------------------------------------------------------------------- live feed
def _push_feed(run_id: str, desk_id: int, ev: Event):
    global _feed_seq
    with _feed_lock:
        _feed_seq += 1
        _feed.append({"seq": _feed_seq, "run_id": run_id, "desk_id": desk_id, "ts": ev.ts, "kind": ev.kind,
                      "agent": ev.agent, "text": ev.text, "data": ev.data})
        if len(_feed) > _FEED_MAX:
            del _feed[: _FEED_MAX // 3]


def _feed_after(seq: int) -> list[dict[str, Any]]:
    with _feed_lock:
        if not _feed or _feed[-1]["seq"] <= seq:
            return []
        lo = max(0, len(_feed) - (_feed[-1]["seq"] - seq))   # seqs are contiguous, so index by offset
        return _feed[lo:]


# ---------------------------------------------------------------------------- mode / configs
def _mode() -> str:
    m = os.environ.get("DESK_MODE", "auto").lower()
    if m in ("demo", "live"):
        return m
    return "live"      # auto = live. Scripted agents only when DESK_MODE=demo is set on purpose, never as a silent fallback.


def _live_reason() -> str:
    """Why real runs cannot happen right now ('' = ready). A live desk without a model key says so instead of
    quietly running the scripted designer/agents in its place."""
    if _mode() == "demo":
        return ""
    prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    name = os.environ.get("DESK_PROVIDER", "").strip() or prov.get("default_provider", "openrouter")
    pc = (prov.get("providers") or {}).get(name) or cfg.DEFAULT_PROVIDERS["providers"].get(name) or {"type": "anthropic"}
    base = str(pc.get("base_url") or "")
    needs_key = pc.get("type") == "anthropic" or "openrouter" in base or "api.openai.com" in base
    if not needs_key or cfg.resolve_api_key({**pc, "name": name}):
        return ""
    env = "ANTHROPIC_API_KEY" if pc.get("type") == "anthropic" else "OPENROUTER_API_KEY"
    return f"no model key: set {env} for provider '{name}' - real models only, no scripted stand-in"


def _require_live() -> None:
    r = _live_reason()
    if r:
        abort(Response(json.dumps({"error": "no_model_key", "message": r}), 503, mimetype="application/json"))


_LEGACY_COLOURS = {"#c084fc": "#0b5fcb", "#60a5fa": "#7c3aed", "#f472b6": "#db2777", "#34d399": "#1f9d63", "#fbbf24": "#b45309",
                   "#f87171": "#dc2626", "#fb923c": "#ea580c", "#a855f7": "#0b5fcb", "#22d3ee": "#0e7490", "#a78bfa": "#6d28d9",
                   "#4ade80": "#15803d", "#e879f9": "#a21caf", "#38bdf8": "#0369a1"}   # dark-theme palette -> light-theme palette


def desk_configs(desk: dict[str, Any]) -> dict[str, Any]:
    """Engine config for one desk: template + the desk's stored overrides + model tier + provider."""
    t = templates.get(desk.get("template") or DEFAULT_TEMPLATE)
    over = desk.get("config") or {}
    business = {**t["business"], **(over.get("business") or {})}
    agents = over.get("agents") or t["agents"]
    workflows = over.get("workflows") or t["workflows"]
    for a in agents:                                   # a roster edited in the portal is stored raw: fill what the engine expects
        a.setdefault("enabled", True)
        a.setdefault("tools", ["read_file", "list_files"])
        if a.get("id") != "atlas" and templates._SPECIALIST_SUFFIX.strip() not in (a.get("system_prompt") or ""):
            a["system_prompt"] = (a.get("system_prompt") or f"You are {a.get('name', a.get('id'))}, {a.get('role', '')}.").rstrip() + templates._SPECIALIST_SUFFIX
    for a in agents:                                   # what the owner granted, before any engine (Hermes) strips or adds tools
        a["granted_tools"] = list(a.get("tools", []))
    for a in agents:                                   # desks built before the rename stored the orchestrator as "hermes"
        if a.get("id") == "hermes":
            a["id"], a["name"] = "atlas", "Atlas"
        a["color"] = _LEGACY_COLOURS.get((a.get("color") or "").lower(), a.get("color"))
    mode = _mode()
    if mode == "demo":
        providers = {"default_provider": "demo", "providers": {"demo": {"type": "demo", "delay": float(os.environ.get("DEMO_DELAY", "0.6")),
                                                                        "fault_rate": float(os.environ.get("DEMO_FAULT_RATE", "0") or 0),
                                                                        "fault_sleep": float(os.environ.get("DEMO_FAULT_SLEEP", "8") or 8)}}}
        for a in agents:
            a["provider"] = "demo"
            a["model"] = ""
    else:
        providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
        for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():   # backfill presets added later
            providers.setdefault("providers", {}).setdefault(name, dict(pc))
        prov = os.environ.get("DESK_PROVIDER", "").strip() or providers.get("default_provider", "openrouter")
        providers["default_provider"] = prov
        for a in agents:
            a["provider"] = prov
        if prov == "openrouter":
            templates.apply_tier(agents, desk.get("tier") or "free")
        else:
            for a in agents:
                a["model"] = ""
        for a in agents:                               # per-agent landscape overrides beat the tier default
            ov = (over.get("models") or {}).get(a["id"])
            if not ov:
                continue
            if ov.get("engine"):
                a["engine"] = ov["engine"]
            if ov.get("model") and ov["model"] != "hermes-agent":
                a["model"] = ov["model"]
                if ov.get("provider") and ov["provider"] != "hermes_agent":
                    a["provider"] = ov["provider"]
    # Hermes Agent engine (the real Nous Research agent, via its API server). Config comes from the desk's
    # hermes_agent connector, or globally from HERMES_AGENT_URL / HERMES_AGENT_KEY env. When
    # DESK_DEFAULT_ENGINE=hermes_agent, every specialist runs on it unless the agent explicitly says engine=atlas.
    # Atlas itself stays on our loop - the API server executes its own tools and cannot call delegate/queue_action.
    hcfg: dict[str, Any] | None = None
    _conns = store.connectors(desk["id"])
    if any(c["kind"] == "camera" for c in _conns):    # desks built before cameras existed keep their stored agent list
        for a in agents:
            if a["id"] == "atlas" or "camera_look" in a.get("tools", []):
                a["tools"] = list(dict.fromkeys(list(a.get("tools", [])) + ["camera_look", "camera_events"]))
    for a in agents:                                   # the lead can always redesign its team and watch videos
        if a["id"] == "atlas":
            a["tools"] = list(dict.fromkeys(list(a.get("tools", [])) + ["assemble_team", "video_describe"]))
    hconn = next((c for c in _conns if c["kind"] == "hermes_agent"), None)
    if hconn:
        hcfg = hconn["config"]
    elif os.environ.get("HERMES_AGENT_URL", "").strip():
        hcfg = {"base_url": os.environ["HERMES_AGENT_URL"].strip(), "api_key": os.environ.get("HERMES_AGENT_KEY", "").strip(),
                "session_prefix": "atlas"}
    default_hermes = os.environ.get("DESK_DEFAULT_ENGINE", "").strip().lower() == "hermes_agent" and mode != "demo"
    # The LEAD may also run on the Hermes Agent runtime (Model landscape engine override for atlas, or
    # DESK_LEAD_ENGINE=hermes_agent). Hermes ignores client tool schemas, so the lead keeps our desk tools through
    # the <atlas>{...}</atlas> text protocol (orchestrator.parse_text_calls) - policy and approvals unchanged.
    lead_ov = (over.get("models") or {}).get("atlas") or {}
    lead_hermes = mode != "demo" and (lead_ov.get("engine") == "hermes_agent"
                                      or os.environ.get("DESK_LEAD_ENGINE", "").strip().lower() == "hermes_agent")
    for a in agents:
        if a["id"] == "atlas":
            if lead_hermes and hcfg:
                tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
                hmodel = (lead_ov.get("model") if lead_ov.get("model") and lead_ov["model"] != "hermes-agent"
                          else tier_cfg.get("hermes_model", templates.FREE_MODEL))
                providers.setdefault("providers", {})["hermes_agent_atlas"] = {
                    "type": "hermes_agent", "base_url": hcfg.get("base_url", ""), "api_key": hcfg.get("api_key", ""),
                    "default_model": hmodel,
                    "session_key": f"{hcfg.get('session_prefix') or 'atlas'}:desk{desk['id']}:atlas"}
                a["provider"], a["model"], a["engine"], a["text_tools"] = "hermes_agent_atlas", hmodel, "hermes_agent", True
            elif lead_hermes:
                a["engine_note"] = "no Hermes Agent instance configured - lead running on the built-in engine"
            continue
        wants = a.get("engine") == "hermes_agent" or (default_hermes and a.get("engine") != "atlas")
        if not wants:
            continue
        if not hcfg:
            a["engine_note"] = "no Hermes Agent instance configured - running on the built-in engine"
            continue
        pname = f"hermes_agent_{a['id']}"
        # the tier decides which model runs INSIDE the Hermes runtime (free MiniMax / Haiku / Sonnet); a per-agent
        # override from the Model landscape wins if it names a real model
        tier_cfg = templates.TIERS.get(desk.get("tier") or "free", templates.TIERS["free"])
        ov = (over.get("models") or {}).get(a["id"]) or {}
        hmodel = ov.get("model") if ov.get("model") and ov["model"] != "hermes-agent" else tier_cfg.get("hermes_model", templates.FREE_MODEL)
        providers.setdefault("providers", {})[pname] = {"type": "hermes_agent", "base_url": hcfg.get("base_url", ""),
                                                         "api_key": hcfg.get("api_key", ""), "default_model": hmodel,
                                                         "session_key": f"{hcfg.get('session_prefix') or 'atlas'}:desk{desk['id']}:{a['id']}"}
        a["provider"], a["model"], a["tools"], a["engine"] = pname, hmodel, [], "hermes_agent"
    return {"providers": providers, "orchestration": cfg.load("orchestration", cfg.DEFAULT_ORCHESTRATION),
            "business": business, "agents": agents, "workflows": workflows, "ui": {}, "mode": mode,
            "desk_id": desk["id"]}


def ensure_demo_desk() -> dict[str, Any]:
    d = store.desk(1)
    if d is None:                                      # DESK_TEMPLATE picks the built-in desk's template (default sales_desk)
        name = "Acme Estates" if DEFAULT_TEMPLATE == "sales_desk" else templates.get(DEFAULT_TEMPLATE)["business"].get("name", "Demo desk")
        d = store.add_desk(0, name, DEFAULT_TEMPLATE, "free", templates.build_desk(DEFAULT_TEMPLATE, {}))
    return d


# ---------------------------------------------------------------------------- auth + desk context
PUBLIC_API = {"/api/health", "/api/stats", "/api/me", "/api/vision/demo", "/api/vision/demo/ask", "/api/orch/live"}


def current_user() -> dict[str, Any] | None:
    uid = session.get("uid")
    return store.user(int(uid)) if uid else None


def current_desk() -> dict[str, Any] | None:
    u = current_user()
    if OPEN and not u:                     # testing mode: any desk, no account
        did = session.get("desk")
        d = store.desk(int(did)) if did else None
        return d or (store.all_desks() or [ensure_demo_desk()])[0]
    if not u:
        return None
    did = session.get("desk")
    d = store.desk(int(did)) if did else None
    if d and d["owner_id"] == u["id"]:
        return d
    ds_ = store.desks_for(u["id"])
    if ds_:
        session["desk"] = ds_[0]["id"]
        return ds_[0]
    return None


def need_desk() -> dict[str, Any]:
    d = current_desk()
    if not d:
        abort(Response(json.dumps({"error": "no_desk"}), 409, mimetype="application/json"))
    return d


def ds():
    return store.for_desk(need_desk()["id"])


from .. import secure as SEC

_RL_HOOK = SEC.RateLimiter(int(os.environ.get("RL_HOOK_PER_MIN", "30")), 60)          # per token+IP
_RL_AUTH = SEC.RateLimiter(int(os.environ.get("RL_AUTH_PER_10MIN", "20")), 600)       # per IP: login/signup attempts
_RL_MODEL = SEC.RateLimiter(int(os.environ.get("RL_MODEL_PER_MIN", "20")), 60)        # per IP: model-backed design calls
_RL_VISION = SEC.RateLimiter(int(os.environ.get("RL_VISION_PER_MIN", "12")), 60)      # per IP: public site vision demo
_VISION_SLOTS = threading.BoundedSemaphore(int(os.environ.get("VISION_DEMO_CONCURRENCY", "3") or 3))


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=(), payment=()")   # camera: the site's vision demo
    if os.environ.get("RENDER"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


@app.before_request
def _rate_limits():
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    p = request.path
    if p.startswith("/hook/") and not _RL_HOOK.allow(f"{p}:{ip}"):
        return Response(json.dumps({"error": "rate limited"}), 429, mimetype="application/json")
    if request.method == "POST" and p in ("/login", "/signup", "/desk/login") and not _RL_AUTH.allow(ip):
        return Response(json.dumps({"error": "too many attempts - wait a few minutes"}), 429, mimetype="application/json")
    if request.method == "POST" and p.startswith("/api/design/") and (p.endswith("/say") or p.endswith("/study")) and not _RL_MODEL.allow(ip):
        return Response(json.dumps({"error": "rate limited - slow down a little"}), 429, mimetype="application/json")
    return None


@app.before_request
def _guard():
    if OPEN:                                   # open test mode: no accounts anywhere, auth pages go straight to the desk
        if request.method == "GET" and request.path in ("/login", "/signup", "/desk/login"):
            return redirect("/desk")
        return None
    p = request.path
    if p.startswith("/hook/"):
        return None
    if p.startswith("/api") and p not in PUBLIC_API:
        if not current_user():
            abort(401)
    elif p.startswith("/desk") and not p.startswith("/desk/static/") and p != "/desk/login":
        if not current_user():
            return redirect("/login?next=" + p)
    return None


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _page(template: str, **vars_) -> str:
    html = (STATIC_DIR / template).read_text(encoding="utf-8")
    for k, v in vars_.items():
        html = html.replace("{{%s}}" % k, _esc(str(v)))
    return html


@app.route("/login", methods=["GET", "POST"])
@app.route("/desk/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password") or ""
        u = store.user_by_email(email) if email else None
        if not u or not check_password_hash(u["pw_hash"], pw):
            time.sleep(0.8)   # slow down guessing
            if request.is_json:
                return jsonify({"error": "wrong email or password"}), 401
            return _page("login.html", error="Wrong email or password.", email=email), 401
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        store.touch_login(u["id"])
        if request.is_json:
            return jsonify({"ok": True})
        nxt = request.args.get("next", "/desk")
        return redirect(nxt if nxt.startswith("/desk") else "/desk")
    if current_user():
        return redirect("/desk")
    return _page("login.html", error="", email="")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        d = request.form if request.form else (request.get_json(silent=True) or {})
        name = (d.get("name") or "").strip()
        company = (d.get("company") or "").strip()
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password") or ""
        err = ""
        if not name or not email or "@" not in email:
            err = "Name and a valid email are required."
        elif len(pw) < 8:
            err = "Password must be at least 8 characters."
        elif store.user_by_email(email):
            err = "An account with that email already exists — log in instead."
        if err:
            if request.is_json:
                return jsonify({"error": err}), 400
            return _page("signup.html", error=err, name=name, company=company, email=email), 400
        u = store.add_user(email, name, company, generate_password_hash(pw))
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        if request.is_json:
            return jsonify({"ok": True})
        return redirect("/desk")
    if current_user():
        return redirect("/desk")
    return _page("signup.html", error="", name="", company="", email="")


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect("/")


@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(u or {"id": None, "name": "Guest", "email": "", "company": "", "open": OPEN})


from . import showrun as _SR

_SHOW = _SR.ShowRunner({"store": store, "desk_configs": desk_configs, "mode": _mode, "blocked": _live_reason,
                        "templates": templates, "template": DEFAULT_TEMPLATE})


@app.get("/api/orch/live")
def api_orch_live():
    """Public: the site hero's live orchestration feed (a real run on the free model while people watch).
    ?since=<n> returns events after index n; ?run=<id> lets the client notice a new run. Polling, not SSE, so it
    never holds a gunicorn thread."""
    try:
        since = int(request.args.get("since", "0") or 0)
    except ValueError:
        since = 0
    resp = jsonify(_SHOW.snapshot(since, request.args.get("run", "")))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/health")
def api_health():
    reason = _live_reason()
    return jsonify({"ok": True, "mode": _mode(), "live_ready": not reason, "live_reason": reason,
                    "hermes_agent": bool(os.environ.get("HERMES_AGENT_URL", "").strip()),
                    "db": store.backend, "db_ok": store.ping()})


# ---------------------------------------------------------------------------- static
def _site_page(name: str):
    """Serve a marketing page. In open test mode every Log in / Sign up link opens the desk directly."""
    if not OPEN or not name.endswith(".html"):
        return send_from_directory(SITE_DIR, name)
    html = (SITE_DIR / name).read_text(encoding="utf-8")
    html = re.sub(r'href="/(?:login|signup)(?:\?[^"]*)?"', 'href="/desk"', html)
    html = re.sub(r">\s*Log in\s*<", ">Open desk<", html)
    html = re.sub(r">\s*Sign up(?: free)?\s*<", ">Open the desk<", html)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/")
def site_index():
    return _site_page("index.html")


@app.route("/site/<path:path>")
def site_files(path):
    return _site_page(path) if (SITE_DIR / path).is_file() else abort(404)


@app.route("/<path:path>")
def site_root_files(path):
    if (SITE_DIR / path).is_file():
        return _site_page(path)
    abort(404)


@app.route("/desk")
@app.route("/desk/")
def desk_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/desk/static/<path:path>")
def desk_static(path):
    resp = send_from_directory(STATIC_DIR, path)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------------------- api: desks (onboarding + switching)
def _desk_public(d: dict[str, Any]) -> dict[str, Any]:
    b = (d.get("config") or {}).get("business") or {}
    return {"id": d["id"], "name": d["name"], "template": d["template"], "tier": d.get("tier", "free"),
            "business_name": b.get("name", d["name"]), "created": d.get("created"),
            "ui_level": (d.get("config") or {}).get("ui_level") or "simple"}


# tools a specialist may be given from the Team page. Everything else in ORCHESTRATOR_ONLY stays with Atlas;
# finish / assemble_team are never assignable. Approvals still gate every outbound tool.
SPECIALIST_OK = {"crm_lookup", "crm_update", "queue_action", "camera_look", "camera_events", "remember", "recall",
                 "browse", "http_request", "calendar_free_slots", "calendar_book", "generate_media", "video_describe"}
NEVER_ASSIGN = {"finish", "assemble_team"}
_COLOURS = ["#7c3aed", "#1f9d63", "#db2777", "#ea580c", "#b45309", "#0891b2"]


def _clean_roster(raw: Any, current: list[dict[str, Any]]) -> list[dict[str, Any]] | str:
    """Validate a roster from the portal. Returns the cleaned list or an error string."""
    if not isinstance(raw, list) or not raw:
        return "agents must be a non-empty list"
    prev = {a["id"]: a for a in current}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, a in enumerate(raw):
        if not isinstance(a, dict):
            return f"agent #{i + 1} is not an object"
        aid = re.sub(r"[^a-z0-9_]+", "_", str(a.get("id") or a.get("name") or "").strip().lower()).strip("_")[:24]
        if not aid:
            return f"agent #{i + 1} needs a name"
        if aid in seen:
            return f"two agents share the id '{aid}'"
        seen.add(aid)
        name = str(a.get("name") or aid).strip()[:40]
        role = str(a.get("role") or "").strip()[:200]
        prompt = str(a.get("system_prompt") or "").strip()[:6000]
        tools_in = [str(t) for t in (a.get("tools") or []) if isinstance(t, str)]
        if aid == "atlas":
            tools = [t for t in tools_in if t in T.SCHEMAS or t == "mcp"]
            for must in ("delegate", "list_agents", "save_deliverable", "read_file", "list_files"):
                if must not in tools:
                    tools.append(must)
            prompt = prompt or templates._BASE_ATLAS_PROMPT
            enabled = True
        else:
            tools = [t for t in tools_in if (t in T.SCHEMAS and (t not in T.ORCHESTRATOR_ONLY or t in SPECIALIST_OK)) or t == "mcp"]
            tools = [t for t in tools if t not in NEVER_ASSIGN]
            enabled = bool(a.get("enabled", True))
        colour = str(a.get("color") or "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            colour = prev.get(aid, {}).get("color") or ("#0b5fcb" if aid == "atlas" else _COLOURS[len(out) % len(_COLOURS)])
        keep = prev.get(aid, {})
        ent = {"id": aid, "name": name, "role": role, "enabled": enabled, "provider": keep.get("provider", ""),
               "model": keep.get("model", "") if aid != "atlas" else "", "tools": tools, "system_prompt": prompt, "color": colour}
        if keep.get("engine") in ("atlas", "hermes_agent"):
            ent["engine"] = keep["engine"]
        pos = a.get("pos") if isinstance(a.get("pos"), dict) else keep.get("pos")
        if isinstance(pos, dict):
            try:
                ent["pos"] = {"x": max(0.0, min(8000.0, float(pos.get("x")))), "y": max(0.0, min(8000.0, float(pos.get("y"))))}
            except (TypeError, ValueError):
                pass
        out.append(ent)
    if "atlas" not in seen:
        return "the roster must include Atlas (id 'atlas')"
    out.sort(key=lambda x: 0 if x["id"] == "atlas" else 1)
    return out


@app.get("/api/templates")
def api_templates():
    return jsonify({"desks": templates.DESK_TYPES, "tiers": [{"id": k, **v} for k, v in templates.TIERS.items()],
                    "mode": _mode()})


@app.get("/api/desks")
def api_desks():
    u = current_user()
    rows = store.desks_for(u["id"]) if u else ((store.all_desks() or [ensure_demo_desk()]) if OPEN else [])
    cur = current_desk()
    return jsonify({"desks": [_desk_public(d) for d in rows], "current": cur["id"] if cur else None})


@app.post("/api/desks")
def api_create_desk():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    d = request.get_json(force=True) or {}
    template = d.get("template") if d.get("template") in templates.BUILTIN else DEFAULT_TEMPLATE
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    name = (d.get("name") or "").strip() or "My desk"
    conf = templates.build_desk(template, d)
    desk = store.add_desk(u["id"] if u else 0, name, template, tier, conf)
    session["desk"] = desk["id"]
    return jsonify(_desk_public(desk))


@app.post("/api/desks/<int:did>/select")
def api_select_desk(did):
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN) or (not u and not OPEN):
        abort(404)
    session["desk"] = did
    return jsonify(_desk_public(d))


@app.patch("/api/desks/<int:did>")
def api_update_desk(did):
    u = current_user()
    d = store.desk(did)
    if not d or (u and d["owner_id"] != u["id"] and not OPEN):
        abort(404)
    body = request.get_json(force=True) or {}
    fields: dict[str, Any] = {}
    if body.get("tier") in templates.TIERS:
        fields["tier"] = body["tier"]
    if body.get("name"):
        fields["name"] = str(body["name"]).strip()
    if isinstance(body.get("business"), dict):
        conf = d.get("config") or {}
        conf["business"] = {**(conf.get("business") or {}), **body["business"]}
        fields["config"] = conf
    if body.get("ui_level") in ("simple", "full"):
        conf = fields.get("config") or d.get("config") or {}
        conf["ui_level"] = body["ui_level"]
        fields["config"] = conf
    if body.get("reset_agents"):
        conf = fields.get("config") or d.get("config") or {}
        conf.pop("agents", None)
        fields["config"] = conf
    elif "agents" in body:
        conf = fields.get("config") or d.get("config") or {}
        cur = conf.get("agents") or templates.get(d.get("template") or DEFAULT_TEMPLATE)["agents"]
        roster = _clean_roster(body["agents"], cur)
        if isinstance(roster, str):
            return jsonify({"error": roster}), 400
        conf["agents"] = roster
        fields["config"] = conf
    if isinstance(body.get("models"), dict):           # per-agent model landscape: {agent_id: {model, provider, engine}}
        conf = fields.get("config") or d.get("config") or {}
        cur = conf.get("models") or {}
        for aid, ov in body["models"].items():
            if not isinstance(ov, dict):
                continue
            ent = {k: str(ov[k]) for k in ("model", "provider", "engine") if ov.get(k)}
            if ent:
                cur[str(aid)] = ent
            else:
                cur.pop(str(aid), None)
        conf["models"] = cur
        fields["config"] = conf
    store.update_desk(did, **fields)
    return jsonify(_desk_public(store.desk(did)))


# curated model catalogue for the landscape picker. `tools` = supports native tool-calling on OpenRouter.
MODEL_CATALOG = [
    {"id": "minimax/minimax-m3:free", "label": "MiniMax M3 (free)", "provider": "openrouter", "tools": True, "cost": "free tier", "engine": "atlas"},
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "label": "Nemotron 3 Super (free)", "provider": "openrouter", "tools": True, "cost": "free tier", "engine": "atlas"},
    {"id": "anthropic/claude-sonnet-4.5", "label": "Claude Sonnet 4.5", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£2.3/M in", "engine": "atlas", "paid": True},
    {"id": "anthropic/claude-haiku-4.5", "label": "Claude Haiku 4.5", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£0.8/M in", "engine": "atlas", "paid": True},
    {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash (vision)", "provider": "openrouter", "tools": True, "vision": True, "cost": "≈£0.25/M in", "engine": "atlas", "paid": True,
     "note": "cheap eyes - good default for camera_look / video_describe (set VISION_MODEL to use it for vision calls)"},
    {"id": "qwen/qwen2.5-vl-72b-instruct", "label": "Qwen 2.5 VL 72B (vision)", "provider": "openrouter", "tools": False, "vision": True, "cost": "≈£0.5/M in", "engine": "atlas", "paid": True,
     "note": "strong open-weights VLM; no native tool-calling - use as VISION_MODEL, not as an agent brain"},
    {"id": "nousresearch/hermes-4-70b", "label": "Nous Hermes 4 70B", "provider": "openrouter", "tools": False, "cost": "≈£0.1/M in", "engine": "atlas", "paid": True,
     "note": "no native tool-calling on OpenRouter - best used INSIDE the Hermes Agent runtime"},
    {"id": "hermes-agent", "label": "Hermes Agent runtime (own tools, memory, skills)", "provider": "hermes_agent", "tools": True, "cost": "runtime + its model", "engine": "hermes_agent"},
]


@app.get("/api/models")
def api_models():
    return jsonify({"models": MODEL_CATALOG, "paid_unlocked": not _free_tier_hint()})


def _free_tier_hint() -> bool:
    return True                                        # flips once the OpenRouter account has credits; informational only


# ---------------------------------------------------------------------------- api: overview
@app.get("/api/config")
def api_config():
    desk = current_desk()
    if not desk:
        return jsonify({"mode": _mode(), "needs_desk": True, "protected": not OPEN})
    c = desk_configs(desk)
    reason = _live_reason()
    return jsonify({
        "mode": c["mode"], "live_ready": not reason, "live_reason": reason,
        "template": desk["template"], "tier": desk.get("tier", "free"), "business": c["business"],
        "desk": _desk_public(desk),
        "ui_level": (desk.get("config") or {}).get("ui_level") or "simple",
        "custom_roster": bool((desk.get("config") or {}).get("agents")),
        "tools": [{"id": n, "description": (sc.get("description") or "").split(". ")[0][:140], "orchestrator_only": n in T.ORCHESTRATOR_ONLY}
                  for n, sc in T.SCHEMAS.items() if n not in NEVER_ASSIGN] + [{"id": "mcp", "description": "Tools from connected MCP servers", "orchestrator_only": False}],
        "agents": [{"id": a["id"], "name": a["name"], "role": a.get("role", ""), "color": a.get("color", ""),
                    "tools": a.get("granted_tools") or a.get("tools", []), "runtime_tools": a.get("tools", []),
                    "model": a.get("model", "") or "(provider default)",
                    "engine": a.get("engine") or "atlas", "provider": a.get("provider") or "",
                    "engine_note": a.get("engine_note") or "", "enabled": a.get("enabled", True), "pos": a.get("pos"),
                    "system_prompt": (a.get("system_prompt") or "").replace(templates._SPECIALIST_SUFFIX, "")} for a in c["agents"]],
        "workflows": [{"id": w["id"], "name": w["name"], "description": w.get("description", "")} for w in c["workflows"]],
        "protected": not OPEN,
    })


@app.get("/api/stats")
def api_stats():
    desk = current_desk()
    if not desk:
        return jsonify({"leads": 0, "pending": 0, "approved": 0, "qualified": 0, "active_runs": 0, "tokens_in": 0, "tokens_out": 0, "needs_desk": True})
    s = store.stats(desk["id"])
    s["active_runs"] = sum(1 for r in _runs.values() if r["desk_id"] == desk["id"] and r["thread"].is_alive())
    return jsonify(s)


# ---------------------------------------------------------------------------- api: leads + runs
def _lead_task(lead: dict[str, Any]) -> str:
    return (f"New inbound lead — handle end to end.\n"
            f"Name: {lead['name']}\nCompany: {lead['company'] or '(individual)'}\nEmail: {lead['email']}\n"
            f"Phone: {lead['phone'] or '-'}\nSource: {lead['source']}\n\nEnquiry:\n{lead['notes']}")


_KEEP_FINISHED_S = 3600


def _prune_runs() -> None:
    """Drop in-memory holders of runs that finished over an hour ago (their events live in the DB)."""
    now = time.time()
    for rid, h in list(_runs.items()):
        if not h["thread"].is_alive() and now - h.get("ended", h.get("started", now)) > _KEEP_FINISHED_S:
            _runs.pop(rid, None)


def _start_run(desk: dict[str, Any], task: str, mode: str, lead_id: int | None = None) -> str:
    _require_live()
    configs = desk_configs(desk)
    paid = any(":free" not in (a.get("model") or "") and a.get("model") for a in configs["agents"])
    if configs["mode"] != "demo" and paid:                # free-tier desks cost nothing and are never blocked by the spend cap
        blocked = _spend_blocked()
        if blocked:
            abort(Response(json.dumps({"error": blocked}), 402, mimetype="application/json"))
    dstore = store.for_desk(desk["id"])
    events: list[dict[str, Any]] = []
    orch: Orchestrator

    def emit(ev: Event):
        if ev.kind != "token":                     # deltas are live-only; the full text arrives as a `log` event
            events.append({"ts": ev.ts, "kind": ev.kind, "agent": ev.agent, "text": ev.text, "data": ev.data})
        if ev.kind == "error":
            _last_error[desk["id"]] = {"ts": ev.ts, "run_id": orch.run_id, "text": (ev.text or "")[:400]}
        _push_feed(orch.run_id, desk["id"], ev)

    orch = Orchestrator(configs, dstore, emit)
    holder: dict[str, Any] = {"events": events, "orch": orch, "task": task, "desk_id": desk["id"], "started": time.time()}

    def work():
        try:
            res = orch.run(task, mode)
            status = res.status
        except BaseException as exc:               # the orchestrator already catches run errors; this is the last line of defence
            status = "error"
            msg = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                dstore.finish_run(orch.run_id, "error", "crashed: " + msg, orch.tokens_in, orch.tokens_out)
            except Exception:
                pass
            _push_feed(orch.run_id, desk["id"], Event(kind="error", agent="system", text="run crashed: " + msg))
            _push_feed(orch.run_id, desk["id"], Event(kind="done", agent="system", text=msg, data={"status": "error"}))
        holder["ended"] = time.time()
        if lead_id is not None:
            dstore.set_lead(lead_id, status=("processed" if status == "done" else status), run_id=orch.run_id)

    th = threading.Thread(target=work, daemon=True)
    holder["thread"] = th
    th.start()
    for _ in range(50):                            # wait briefly for run_id to exist
        if orch.run_id:
            break
        time.sleep(0.02)
    with _runs_lock:
        _runs[orch.run_id] = holder
        _prune_runs()
    if lead_id is not None:
        dstore.set_lead(lead_id, status="running", run_id=orch.run_id)
    return orch.run_id


@app.get("/api/leads")
def api_leads():
    return jsonify(ds().leads())


@app.post("/api/leads")
def api_add_lead():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip()
    if not name or not email:
        return jsonify({"error": "name and email required"}), 400
    dstore = store.for_desk(desk["id"])
    lid = dstore.add_lead(name, (d.get("company") or "").strip(), email, (d.get("phone") or "").strip(),
                          (d.get("source") or "web form").strip(), (d.get("notes") or "").strip())
    dstore.upsert_contact(email, {"name": name, "company": d.get("company", ""), "email": email,
                                  "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound lead"})
    run_id = None
    if d.get("run", True):
        run_id = _start_run(desk, _lead_task(dstore.lead(lid)), d.get("mode", "auto"), lid)
    return jsonify({"id": lid, "run_id": run_id})


@app.post("/api/leads/<int:lid>/run")
def api_run_lead(lid):
    desk = need_desk()
    lead = store.lead(lid)
    if not lead or lead.get("desk_id") != desk["id"]:
        abort(404)
    mode = (request.get_json(silent=True) or {}).get("mode", "auto")
    return jsonify({"run_id": _start_run(desk, _lead_task(lead), mode, lid)})


@app.post("/api/runs")
def api_run_task():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    task = (d.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task required"}), 400
    return jsonify({"run_id": _start_run(desk, task, d.get("mode", "auto"))})


@app.get("/api/runs")
def api_runs():
    rows = ds().runs(60)
    for r in rows:
        r["active"] = r["id"] in _runs and _runs[r["id"]]["thread"].is_alive()
        r["summary"] = (r.get("summary") or "")[:160]        # the list only needs a teaser; /api/runs/<id> has the full text
    return jsonify(rows)


@app.get("/api/runs/<run_id>")
def api_run(run_id):
    desk = need_desk()
    row = store.run(run_id)
    live = _runs.get(run_id)
    if not row and live and live["desk_id"] == desk["id"]:   # thread started but DB row not written yet
        row = {"id": run_id, "created": time.time(), "task": live["task"], "mode": "", "status": "running",
               "summary": "", "tokens_in": 0, "tokens_out": 0, "run_dir": "", "desk_id": desk["id"]}
    if not row or row.get("desk_id") != desk["id"]:
        abort(404)
    if live:
        events = [e for e in live["events"] if e["kind"] != "usage"]
        row["active"] = live["thread"].is_alive()
    else:
        events = store.events(run_id)
        row["active"] = False
    row["events"] = events
    run_dir = Path(row["run_dir"]) if row.get("run_dir") else None
    row["deliverables"] = []
    if run_dir and run_dir.is_dir():
        for p in sorted(run_dir.glob("*.md")):
            if p.name not in ("TASK.md",):
                row["deliverables"].append({"name": p.name, "content": p.read_text(encoding="utf-8", errors="replace")[:20000]})
    return jsonify(row)


@app.get("/api/stream")
def api_stream():
    """Server-sent events for the current desk: every orchestrator event, including token deltas.
    ?since=<seq> resumes after a sequence number; ?since=now (default) starts from the present."""
    desk = need_desk()
    arg = request.args.get("since", "now")
    run_filter = request.args.get("run", "")
    with _feed_lock:
        start = _feed_seq if arg == "now" else max(0, min(int(arg or 0), _feed_seq))
    desk_id = desk["id"]

    def gen():
        last = start
        idle = 0
        yield "retry: 2000\n\n"
        while True:
            new = _feed_after(last)
            if new:
                idle = 0
                for e in new:
                    last = e["seq"]
                    if e["desk_id"] != desk_id or (run_filter and e["run_id"] != run_filter):
                        continue
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                if idle % 100 == 0:
                    yield ": ping\n\n"
                time.sleep(0.1)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/live")
def api_live():
    desk = need_desk()
    out = []
    with _runs_lock:
        items = list(_runs.items())
    for rid, h in items:
        if h["desk_id"] != desk["id"]:
            continue
        alive = h["thread"].is_alive()
        if not alive and not request.args.get("all"):
            continue
        out.append({"id": rid, "active": alive, "task": h.get("task", ""),
                    "events": [e for e in h["events"] if e["kind"] != "usage"],
                    "tokens_in": h["orch"].tokens_in, "tokens_out": h["orch"].tokens_out})
    with _feed_lock:
        seq = _feed_seq
    return jsonify({"seq": seq, "runs": out})


@app.post("/api/runs/<run_id>/cancel")
def api_cancel(run_id):
    desk = need_desk()
    live = _runs.get(run_id)
    if live and live["desk_id"] == desk["id"]:
        live["orch"].cancel()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: approvals
@app.get("/api/actions")
def api_actions():
    return jsonify(ds().actions(request.args.get("status", "")))


@app.post("/api/actions/<int:aid>/decide")
def api_decide(aid):
    desk = need_desk()
    dstore = store.for_desk(desk["id"])
    row = dstore.action(aid)
    if not row or row.get("desk_id") != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    status = d.get("status")
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be approved|rejected"}), 400
    u = current_user()
    by = (u["name"] if u else d.get("by", "owner"))
    row = dstore.decide_action(aid, status, by=by, note=d.get("note", ""), body=d.get("body"), subject=d.get("subject"))
    if status == "approved":
        note = d.get("note", "")
        try:
            result = _dispatch(desk, row)
            row = dstore.decide_action(aid, "sent", by=by, note=(note + " " + result).strip())
            dstore.add_event(row["run_id"], "sent", "owner", f"{row['kind']} → {row['to']} — {row['subject']} ({result})")
            if row["kind"] in ("email", "whatsapp", "sms") and row["to"]:
                contact = dstore.upsert_contact(row["to"], {"stage": "Contacted", "next_action": "Follow up in 3 days if no reply"})
                for m in I.crm_sync(dstore.connectors(), contact):
                    dstore.add_event(row["run_id"], "tool", "owner", f"crm sync → {m}")
            I.notify(dstore.connectors(), f":outbox_tray: *Sent* — {row['kind']} → {row['to']}: {row['subject'] or (row['body'] or '')[:80]} {result}")
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            row = dstore.decide_action(aid, "failed", by=by, note=(note + " send failed: " + err).strip())
            dstore.add_event(row["run_id"], "error", "owner", f"{row['kind']} → {row['to']} failed: {err}")
    else:
        dstore.add_event(row["run_id"], "rejected", "owner", f"{row['kind']} to {row['to']} rejected — {d.get('note','')}")
    return jsonify(row)


# ---------------------------------------------------------------------------- api: crm / audit / report
@app.get("/api/contacts")
def api_contacts():
    return jsonify(ds().contacts(request.args.get("q", "")))


@app.post("/api/contacts")
def api_upsert_contact():
    d = request.get_json(force=True) or {}
    contact = d.get("contact") or d.get("email") or d.get("name")
    if not contact:
        return jsonify({"error": "contact required"}), 400
    return jsonify(ds().upsert_contact(contact, d.get("fields") or d))


@app.get("/api/audit")
def api_audit():
    return jsonify(ds().all_events(400))


@app.get("/api/metrics")
def api_metrics():
    desk = need_desk()
    since = request.args.get("since")
    m = MX.compute(store, desk_id=desk["id"], since=float(since) if since else None)
    m.pop("per_run", None) if request.args.get("full") is None else None
    return jsonify(m)


@app.get("/api/capacity")
def api_capacity():
    active = sum(1 for r in _runs.values() if r["thread"].is_alive())
    return jsonify(MX.capacity(store, active))


# ---------------------------------------------------------------------------- ops: health, alerts, watchdog
def _health(desk_id: int | None, window_h: float = 24.0) -> dict[str, Any]:
    now = time.time()
    since = now - window_h * 3600
    runs = store.runs_between(since, desk_id)
    by = {}
    for r in runs:
        by[r["status"]] = by.get(r["status"], 0) + 1
    finished = [r for r in runs if r["status"] in ("done", "error", "failed", "interrupted", "cancelled")]
    bad = [r for r in finished if r["status"] in ("error", "failed", "interrupted")]
    durs = sorted((r["ended"] or now) - r["created"] for r in finished if r.get("ended"))
    p = lambda q: round(durs[min(len(durs) - 1, int(q * len(durs)))], 1) if durs else None
    with _runs_lock:
        live = [h for h in _runs.values() if h["thread"].is_alive() and (desk_id is None or h["desk_id"] == desk_id)]
        stalled = [h for h in live if now - h["started"] > RUN_MAX_S * 0.75]
    zombies = [r for r in store.running_runs(RUN_MAX_S) if desk_id is None or r["desk_id"] == desk_id]
    oldest = store.oldest_pending_action(desk_id)
    jobs = store.jobs(desk_id) if desk_id is not None else []
    cons = store.connectors(desk_id) if desk_id is not None else []
    tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in runs)
    err = _last_error.get(desk_id) if desk_id is not None else (max(_last_error.values(), key=lambda e: e["ts"]) if _last_error else None)
    alerts: list[dict[str, str]] = []
    if zombies:
        alerts.append({"level": "critical", "key": "zombie", "text": f"{len(zombies)} run(s) stuck in 'running' beyond the watchdog limit"})
    if stalled:
        alerts.append({"level": "warn", "key": "stalled", "text": f"{len(stalled)} live run(s) older than {int(RUN_MAX_S * 0.75 / 60)} min - watchdog will kill at {int(RUN_MAX_S / 60)} min"})
    if len(finished) >= 5 and len(bad) / len(finished) > 0.2:
        alerts.append({"level": "critical", "key": "failure_rate", "text": f"failure rate {len(bad)}/{len(finished)} in the last {int(window_h)}h"})
    elif bad:
        alerts.append({"level": "warn", "key": "failures", "text": f"{len(bad)} failed run(s) in the last {int(window_h)}h"})
    if oldest and now - oldest > 24 * 3600:
        alerts.append({"level": "warn", "key": "approval_age", "text": f"oldest pending approval is {round((now - oldest) / 3600)}h old"})
    for j in jobs:
        if j.get("enabled") and j.get("last_status") == "error":
            alerts.append({"level": "warn", "key": f"job:{j['id']}", "text": f"automation '{j['name']}' failing: {str(j.get('last_result'))[:120]}"})
    for c in cons:
        if str(c.get("status") or "").startswith("error"):
            alerts.append({"level": "warn", "key": f"conn:{c['id']}", "text": f"connector '{c['name']}' unhealthy: {str(c['status'])[:120]}"})
    if err and now - err["ts"] < 3600:
        alerts.append({"level": "warn", "key": "provider", "text": "recent error: " + err["text"][:160]})
    sp = _spend_poll()
    blocked = _spend_blocked()
    if blocked:
        alerts.append({"level": "critical", "key": "spend", "text": blocked + " - new runs are refused"})
    elif SPEND_CAP_USD and sp.get("total_usage") is not None and sp["total_usage"] >= 0.8 * SPEND_CAP_USD:
        alerts.append({"level": "warn", "key": "spend80", "text": f"model spend ${sp['total_usage']:.2f} is over 80% of the ${SPEND_CAP_USD:.2f} cap"})
    return {
        "spend": {"used_usd": sp.get("total_usage"), "credits_usd": sp.get("total_credits"), "cap_usd": SPEND_CAP_USD or None,
                  "blocked": bool(blocked), "error": sp.get("error")},
        "ok": not any(a["level"] == "critical" for a in alerts), "window_h": window_h, "uptime_s": round(now - _BOOT_TS),
        "runs": {"total": len(runs), "by_status": by, "failure_rate": round(len(bad) / len(finished), 3) if finished else 0.0,
                 "p50_s": p(0.5), "p90_s": p(0.9), "active": len(live), "stalled": len(stalled), "zombies": len(zombies)},
        "tokens": tokens, "cost_gbp": round(tokens / 1e6 * 5 * 0.78, 2),
        "queue": {"pending": len(store.actions("pending", limit=100000, desk_id=desk_id)),
                  "oldest_pending_h": round((now - oldest) / 3600, 1) if oldest else 0},
        "jobs": [{"id": j["id"], "name": j["name"], "kind": j["kind"], "enabled": bool(j.get("enabled")), "last_status": j.get("last_status") or "",
                  "last_run": j.get("last_run"), "last_result": (j.get("last_result") or "")[:160]} for j in jobs],
        "connectors": [{"id": c["id"], "name": c["name"], "kind": c["kind"], "status": (c.get("status") or "untested")[:80]} for c in cons],
        "last_error": err, "alerts": alerts, "watchdog_s": RUN_MAX_S,
    }


@app.get("/api/health/full")
def api_health_full():
    desk = need_desk()
    return jsonify(_health(desk["id"], float(request.args.get("hours") or 24)))


@app.get("/api/health/all")
def api_health_all():
    """Cross-desk view for the soak monitor and ops dashboards (open mode / operator only)."""
    if not OPEN and not current_user():
        abort(401)
    return jsonify(_health(None, float(request.args.get("hours") or 24)))


def _notify_alerts(desk: dict[str, Any], alerts: list[dict[str, str]]) -> None:
    """Push critical alerts out through the desk's Slack / email connectors, at most once per hour per alert key."""
    now = time.time()
    due = [a for a in alerts if a["level"] == "critical" and now - _alert_sent.get(f"{desk['id']}:{a['key']}", 0) > 3600]
    if not due:
        return
    for a in due:
        _alert_sent[f"{desk['id']}:{a['key']}"] = now
    text = f"[Atlas Desk] {desk.get('name')}: " + " | ".join(a["text"] for a in due)
    try:
        cons = store.connectors(desk["id"])
        I.notify(cons, text)
        to = os.environ.get("ALERT_EMAIL", "").strip()
        smtp = next((c for c in cons if c["kind"] == "smtp"), None)
        if to and smtp:
            I.send_email(smtp["config"], to, "Atlas Desk alert", text)
    except Exception as exc:
        print("alert delivery failed:", exc)


SPEND_CAP_USD = float(os.environ.get("SPEND_CAP_USD", "0") or 0)          # 0 = no cap
_spend: dict[str, Any] = {"total_credits": None, "total_usage": None, "ts": 0.0, "error": ""}


def _spend_poll(force: bool = False) -> dict[str, Any]:
    """Read the OpenRouter balance at most every 5 minutes (real money now flows through the key)."""
    if not force and time.time() - _spend["ts"] < 300:
        return _spend
    try:
        prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)["providers"].get("openrouter") or {}
        key = cfg.resolve_api_key({**prov, "name": "openrouter"})
        if key:
            import httpx
            r = httpx.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            d = r.json().get("data", {}) if r.status_code < 400 else {}
            _spend.update({"total_credits": d.get("total_credits"), "total_usage": d.get("total_usage"), "error": "" if d else f"HTTP {r.status_code}"})
    except Exception as exc:
        _spend["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    _spend["ts"] = time.time()
    return _spend


def _spend_blocked() -> str:
    """Non-empty reason when new model work must not start: cap exceeded or balance gone."""
    s = _spend_poll()
    used, total = s.get("total_usage"), s.get("total_credits")
    if SPEND_CAP_USD and used is not None and used >= SPEND_CAP_USD:
        return f"spend cap reached: ${used:.2f} of ${SPEND_CAP_USD:.2f} used - raise SPEND_CAP_USD to continue"
    if used is not None and total is not None and total > 0 and used >= total:
        return f"OpenRouter balance exhausted (${used:.2f} of ${total:.2f})"
    return ""


def _watchdog_loop() -> None:
    """Every 15s: kill runs past RUN_MAX_S, mark orphaned DB rows failed, push critical alerts."""
    while True:
        try:
            now = time.time()
            _spend_poll()
            with _runs_lock:
                holders = list(_runs.items())
            for rid, h in holders:
                if h["thread"].is_alive() and now - h["started"] > RUN_MAX_S and not h.get("killed"):
                    h["killed"] = True
                    h["orch"].cancel()
                    msg = f"watchdog: run exceeded {int(RUN_MAX_S)}s and was killed"
                    store.finish_run(rid, "failed", msg, h["orch"].tokens_in, h["orch"].tokens_out)
                    _push_feed(rid, h["desk_id"], Event(kind="error", agent="system", text=msg))
                    _push_feed(rid, h["desk_id"], Event(kind="done", agent="system", text=msg, data={"status": "failed"}))
                    _last_error[h["desk_id"]] = {"ts": now, "run_id": rid, "text": msg}
                    h["ended"] = now
            live_ids = {rid for rid, h in holders if h["thread"].is_alive()}
            acted: dict[int, list[str]] = {}
            for rid, h in holders:
                if h.get("killed") and not h.get("reported"):
                    h["reported"] = True
                    acted.setdefault(h["desk_id"], []).append(f"killed {rid} after {int(RUN_MAX_S)}s")
            for r in store.running_runs(RUN_MAX_S):
                if r["id"] not in live_ids:                      # DB says running, nothing in memory owns it
                    store.finish_run(r["id"], "failed", "lost: no worker owns this run (crashed or restarted)", 0, 0)
                    _last_error[r["desk_id"]] = {"ts": now, "run_id": r["id"], "text": "lost run marked failed by watchdog"}
                    acted.setdefault(r["desk_id"], []).append(f"marked lost run {r['id']} failed")
            for d in store.all_desks():
                hz = _health(d["id"], 24)
                alerts = list(hz["alerts"])
                if acted.get(d["id"]):
                    alerts.append({"level": "critical", "key": "watchdog", "text": "watchdog acted: " + "; ".join(acted[d["id"]])})
                if any(a["level"] == "critical" for a in alerts):
                    _notify_alerts(d, alerts)
        except Exception as exc:
            print("watchdog error:", exc)
        time.sleep(15)


@app.get("/api/report")
def api_report():
    dstore = ds()
    s = dstore.stats()
    acts = dstore.actions()
    decided = [a for a in acts if a["status"] in ("sent", "approved", "rejected")]
    approval_rate = round(100 * sum(1 for a in decided if a["status"] != "rejected") / len(decided)) if decided else None
    runs = dstore.runs(500)
    done = [r for r in runs if r["status"] == "done"]
    flagged = sum(1 for a in acts if a.get("flags"))
    mins_saved = len(done) * 35  # assumption: ~35 min of research + drafting + CRM per lead
    return jsonify({
        "period": time.strftime("%B %Y"),
        "leads_handled": s["leads"], "runs_done": len(done), "runs_failed": len([r for r in runs if r["status"] == "error"]),
        "actions_queued": len(acts), "approval_rate": approval_rate, "policy_flagged": flagged,
        "sent": sum(1 for a in acts if a["status"] == "sent"), "rejected": s["rejected"], "pending": s["pending"],
        "contacts": s["contacts"], "qualified": s["qualified"],
        "hours_saved": round(mins_saved / 60, 1), "assumption": "35 min saved per processed lead (research + draft + CRM)",
        "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"],
        "est_model_cost_gbp": round((s["tokens_in"] * 5 + s["tokens_out"] * 25) / 1_000_000 * 0.78, 2),
        "changes": ["Policy layer now blocks figures, placeholders and invented time slots before the queue",
                    "CRM stage held at New until you approve the first send"],
    })


# ---------------------------------------------------------------------------- demo helpers
def _sample_leads(desk: dict[str, Any]) -> list[dict[str, str]]:
    """Template samples for the stock business; for a customised business ask the (cheap) model to invent
    three realistic enquiries so the demo matches what the client actually does."""
    stock = templates.SAMPLE_LEADS.get(desk["template"], templates.SAMPLE_LEADS["sales_desk"])
    c = desk_configs(desk)
    b = c["business"]
    default_name = templates.get(desk["template"])["business"].get("name")
    if c["mode"] == "demo" or b.get("name") == default_name:
        return stock
    try:
        from ..providers import ProviderPool
        pool = ProviderPool(c["providers"])
        prov = pool.get()
        model = templates.FREE_MODEL if c["providers"].get("default_provider") == "openrouter" else ""
        prompt = (f"Invent 3 realistic inbound enquiries for this business. Return ONLY a JSON array of objects with keys "
                  f"name, company, email, phone, source, notes (notes = 1-2 sentence enquiry in the customer's words). "
                  f"Use example.com emails and +44 7700 9001xx phones.\n\nBusiness: {b.get('name')}\n{b.get('description','')}\n"
                  f"Services: {', '.join(b.get('services', []))}\nTarget clients: {b.get('target_clients','')}")
        r = prov.chat("You output strict JSON only.", [prov.user_message(prompt)], [], model)
        txt = r.text.strip()
        txt = txt[txt.index("["): txt.rindex("]") + 1]
        rows = json.loads(txt)
        out = []
        for x in rows[:3]:
            out.append({"name": str(x.get("name", "")).strip() or "Enquiry", "company": str(x.get("company", "") or ""),
                        "email": str(x.get("email", "") or "lead@example.com"), "phone": str(x.get("phone", "") or ""),
                        "source": str(x.get("source", "") or "website form"), "notes": str(x.get("notes", "") or "")})
        return out or stock
    except Exception:
        return stock


@app.post("/api/demo/seed")
def api_seed():
    desk = need_desk()
    dstore = store.for_desk(desk["id"])
    ids = []
    for L in _sample_leads(desk):
        lid = dstore.add_lead(L["name"], L["company"], L["email"], L["phone"], L["source"], L["notes"])
        dstore.upsert_contact(L["email"], {"name": L["name"], "company": L["company"], "email": L["email"], "phone": L["phone"], "stage": "New", "notes": "Inbound lead"})
        ids.append({"id": lid, "run_id": _start_run(desk, _lead_task(dstore.lead(lid)), "auto", lid)})
        time.sleep(0.05)
    return jsonify(ids)


@app.post("/api/demo/reset")
def api_reset():
    desk = need_desk()
    if any(r["desk_id"] == desk["id"] and r["thread"].is_alive() for r in _runs.values()):
        return jsonify({"error": "runs in progress"}), 409
    store.for_desk(desk["id"]).reset()
    for rid in [k for k, v in _runs.items() if v["desk_id"] == desk["id"]]:
        _runs.pop(rid, None)
    return jsonify({"ok": True})


def _dispatch(desk: dict[str, Any], row: dict[str, Any]) -> str:
    """Perform an approved action for real when a connector exists; otherwise simulate and say so."""
    dstore = store.for_desk(desk["id"])
    kind = row["kind"]
    if kind in I.CHANNELS:
        conn = I.outbound_connector(dstore.connectors(), kind)
        if not conn:
            return f"[simulated {kind} — no {' / '.join(I.CHANNELS[kind])} connector; add one under Integrations]"
        return "[" + I.deliver(conn, kind, row["to"], row["subject"] or "", row["body"] or "") + "]"
    if kind == "browser_action":
        from .. import browser as BR
        try:
            spec = json.loads(row["body"] or "{}")
            return "[" + BR.perform_pending(spec.get("pending") or {}, spec.get("profile") or f"desk{desk['id']}") + "]"
        except Exception as exc:
            return f"[browser action failed - {type(exc).__name__}: {str(exc)[:160]}]"
    if kind == "api_call":
        conn = dstore.connector_by_name(row["to"])
        if not conn:
            return "[failed — connector missing]"
        spec = json.loads(row["body"] or "{}")
        if conn["kind"] == "higgsfield" or "media" in spec:
            res = I.higgsfield_generate(conn["config"], spec.get("media", "image"), spec.get("prompt", ""), spec.get("image_url") or "",
                                        spec.get("duration"), spec.get("aspect_ratio") or "16:9")
            urls = ", ".join(res["outputs"]) or "no output URL returned"
            return f"[Higgsfield {res['model']} {res['status']}: {urls}]"
        if conn["kind"] == "mcp" or "mcp_tool" in spec:
            from .. import mcp_client as M
            out = M.REGISTRY.get(conn).call(spec["mcp_tool"], spec.get("arguments") or {})
            return f"[MCP {spec['mcp_tool']}: {out[:160]}]"
        res = I.http_call(conn["config"], spec.get("method", "POST"), spec.get("path", ""), spec.get("params"), spec.get("body"))
        return f"[HTTP {res['status']} {res['url']}]"
    return f"[simulated {kind} — no connector for this channel yet]"


# ---------------------------------------------------------------------------- api: connectors (integrations)
def _conn_public(c: dict[str, Any]) -> dict[str, Any]:
    return {**c, "config": I.mask(c.get("config") or {})}


@app.get("/api/connectors")
def api_connectors():
    desk = need_desk()
    hook = request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"])
    return jsonify({"connectors": [_conn_public(c) for c in store.connectors(desk["id"])],
                    "kinds": I.KINDS, "hook_url": hook, "whatsapp_hook_url": hook + "/whatsapp", "sms_hook_url": hook + "/sms",
                    "channels": {k: bool(I.outbound_connector(store.connectors(desk["id"]), k)) for k in I.CHANNELS}})


@app.post("/api/connectors")
def api_add_connector():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    kind = d.get("kind")
    if kind not in I.KINDS:
        return jsonify({"error": "unknown kind"}), 400
    name = (d.get("name") or kind).strip()
    if store.connector_by_name(desk["id"], name):
        return jsonify({"error": "a connector with that name exists"}), 400
    c = store.add_connector(desk["id"], kind, name, d.get("config") or {}, bool(d.get("auto")))
    return jsonify(_conn_public(c))


@app.patch("/api/connectors/<int:cid>")
def api_update_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    fields: dict[str, Any] = {}
    if "config" in d:
        fields["config"] = I.merge_secrets(c["config"], d["config"] or {})
    if "auto" in d:
        fields["auto"] = 1 if d["auto"] else 0
    if d.get("name"):
        fields["name"] = str(d["name"]).strip()
    store.update_connector(cid, **fields)
    return jsonify(_conn_public(store.connector(cid)))


@app.delete("/api/connectors/<int:cid>")
def api_delete_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    store.delete_connector(cid)
    from .. import mcp_client as M
    M.REGISTRY.drop(cid)
    return jsonify({"ok": True})


@app.post("/api/connectors/<int:cid>/test")
def api_test_connector(cid):
    desk = need_desk()
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"]:
        abort(404)
    try:
        msg = I.test_connector(c["kind"], c["config"])
        store.update_connector(cid, status="ok: " + msg, last_test=time.time())
        return jsonify({"ok": True, "result": msg})
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update_connector(cid, status="error: " + err, last_test=time.time())
        return jsonify({"ok": False, "result": err}), 400


@app.get("/api/memory")
def api_memory():
    return jsonify(ds().recall(request.args.get("q", ""), 200))


@app.post("/api/memory")
def api_memory_add():
    d = request.get_json(force=True) or {}
    if not d.get("key"):
        return jsonify({"error": "key required"}), 400
    return jsonify(ds().remember(d["key"], d.get("value", ""), source="owner"))


@app.delete("/api/memory/<path:key>")
def api_memory_del(key):
    ds().forget(key)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: jobs (automations)
JOB_KINDS = {
    "task": "Run a task (once or every N minutes)",
    "inbox_watch": "Watch the inbox — every new email becomes a lead",
    "followups": "Chase contacts stuck at Contacted for N days",
    "http_poll": "Poll an HTTP API and hand the result to the desk",
    "camera_watch": "Watch the cameras — detect, log, wake the desk when a rule fires",
}


@app.get("/api/jobs")
def api_jobs():
    desk = need_desk()
    return jsonify({"jobs": store.jobs(desk["id"]), "kinds": JOB_KINDS, "now": time.time()})


@app.post("/api/jobs")
def api_add_job():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    kind = d.get("kind") if d.get("kind") in JOB_KINDS else "task"
    every = int(d.get("every_min") or 0)
    delay = int(d.get("in_min") or 0)
    nxt = time.time() + (delay * 60 if delay else (every * 60 if every and not d.get("run_now") else 0))
    task = d.get("task") or ""
    if kind == "followups" and not task:
        task = json.dumps({"days": int(d.get("days") or 3)})
    if kind in ("http_poll", "camera_watch") and isinstance(task, dict):
        task = json.dumps(task)
    j = store.add_job(desk["id"], kind, d.get("name") or JOB_KINDS[kind], task, every, nxt)
    return jsonify(j)


@app.patch("/api/jobs/<int:jid>")
def api_update_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    d = request.get_json(force=True) or {}
    fields = {k: d[k] for k in ("name", "task", "every_min") if k in d}
    if "enabled" in d:
        fields["enabled"] = 1 if d["enabled"] else 0
        if d["enabled"] and not j.get("next_run"):
            fields["next_run"] = time.time()
    store.update_job(jid, **fields)
    return jsonify(store.job(jid))


@app.post("/api/jobs/<int:jid>/run")
def api_run_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    store.update_job(jid, next_run=time.time() - 1, enabled=1)
    return jsonify({"ok": True, "note": "will run within 20 seconds"})


@app.delete("/api/jobs/<int:jid>")
def api_delete_job(jid):
    desk = need_desk()
    j = store.job(jid)
    if not j or j["desk_id"] != desk["id"]:
        abort(404)
    store.delete_job(jid)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- api: cameras / vision
def _camera(cid: int, desk: dict[str, Any]) -> dict[str, Any]:
    c = store.connector(cid)
    if not c or c["desk_id"] != desk["id"] or c["kind"] != "camera":
        abort(404)
    return c


def _vev_public(e: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in e.items() if k != "snapshot"}
    out["snapshot_url"] = f"/api/vision/snapshot/{e['id']}" if e.get("snapshot") else ""
    out["seen"] = V.counts_text(e.get("counts") or {})
    return out


@app.get("/api/cameras")
def api_cameras():
    desk = need_desk()
    cams = []
    for c in store.connectors(desk["id"]):
        if c["kind"] != "camera":
            continue
        seen = scheduler.last_seen(desk["id"], c["name"]) or {}
        last = store.last_vision_event(desk["id"], c["name"])
        cams.append({**_conn_public(c), "source_kind": V.source_kind(str(c["config"].get("source", ""))),
                     "rule": V.rule_config(c["config"]),
                     "seen": {k: v for k, v in seen.items() if k not in ("annotated", "detections")},
                     "last_event": _vev_public(last) if last else None,
                     "watch_job": next((j for j in store.jobs(desk["id"]) if j["kind"] == "camera_watch" and
                                        (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0), None)})
    hook = request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]) + "/vision"
    nodes = [{**n, "last_event": _vev_public(n["last_event"]) if n.get("last_event") else None}
             for n in store.hook_cameras(desk["id"], time.time() - 86400, tuple(c["name"] for c in cams))]
    return jsonify({"cameras": cams, "nodes": nodes, "mode": _mode(),
                    "detector": {"available": V.DETECTOR.available, "error": V.DETECTOR.error, "weights": os.path.basename(V.YOLO_WEIGHTS)},
                    "vlm": {"ready": V.vlm_ready() and _mode() != "demo", "model": V.DEFAULT_VLM},
                    "stats": store.vision_stats(desk["id"], time.time() - 86400), "hook_url": hook})


@app.post("/api/cameras/<int:cid>/look")
def api_camera_look(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    d = request.get_json(silent=True) or {}
    try:
        r = scheduler.camera_tick(store, desk, c, _start_run, _mode() != "demo",
                                  question=str(d.get("question") or "").strip()[:500], force=bool(d.get("trigger")))
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.update_connector(cid, status="error: " + msg, last_test=time.time())
        return jsonify({"ok": False, "error": msg}), 400
    store.update_connector(cid, status=f"ok: {V.counts_text(r['counts'])} ({r['backend']})", last_test=time.time())
    r.pop("annotated", None)
    return jsonify({"ok": True, **r})


@app.get("/api/cameras/<int:cid>/frame.jpg")
def api_camera_frame(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    seen = scheduler.last_seen(desk["id"], c["name"])
    if seen and seen.get("annotated"):
        return Response(seen["annotated"], mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    last = store.last_vision_event(desk["id"], c["name"])
    if last and last.get("snapshot") and Path(last["snapshot"]).is_file():
        return send_file(last["snapshot"], mimetype="image/jpeg", max_age=0)
    abort(404)


@app.post("/api/cameras/<int:cid>/watch")
def api_camera_watch(cid):
    desk = need_desk()
    c = _camera(cid, desk)
    d = request.get_json(silent=True) or {}
    existing = [j for j in store.jobs(desk["id"]) if j["kind"] == "camera_watch" and (j["task"] or "").find(f'"connector": "{c["name"]}"') >= 0]
    if not d.get("on", True):
        for j in existing:
            store.delete_job(j["id"])
        return jsonify({"ok": True, "watching": False})
    if existing:
        store.update_job(existing[0]["id"], enabled=1, next_run=time.time())
        return jsonify({"ok": True, "watching": True, "job": store.job(existing[0]["id"])})
    every_s = max(20, min(int(d.get("every_s") or 30), 3600))
    j = store.add_job(desk["id"], "camera_watch", f"Watch {c['name']}", json.dumps({"connector": c["name"], "every_s": every_s}), 1, time.time())
    return jsonify({"ok": True, "watching": True, "job": j})


@app.post("/api/vision/video")
def api_vision_video():
    """Upload a video file (or pass {url}) and have the vision analyst describe it as a timeline.
    The result is logged as a vision event, so 'ask the cameras' can recall it later."""
    desk = need_desk()
    from .. import vision as V
    question = (request.form.get("question") or (request.json or {}).get("question", "") if not request.files else request.form.get("question", "")) or ""
    try:
        frames = min(int(request.form.get("frames") or 6), 10)
    except ValueError:
        frames = 6
    f = request.files.get("video")
    if f and f.filename:
        vdir = Path("data") / "videos" / f"desk{desk['id']}"
        vdir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\- ]+", "_", f.filename)[-80:] or "clip.mp4"
        src = vdir / f"{int(time.time())}_{name}"
        f.save(str(src))
        if src.stat().st_size > 300 * 1024 * 1024:
            src.unlink(missing_ok=True)
            return jsonify({"error": "video too large (max 300 MB)"}), 413
        label = name
        src = str(src)
    else:
        src = str((request.json or {}).get("url", "") or "").strip()
        if not src:
            return jsonify({"error": "attach a video file or pass {url}"}), 400
        from .. import secure as SEC
        why = SEC.private_url_reason(src)
        if why:
            return jsonify({"error": f"refusing to fetch: {why}"}), 400
        label = src.rsplit("/", 1)[-1][:60]
    try:
        res = V.describe_video(src, question, frames=frames, context=desk.get("name", ""))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    snap = ""
    try:
        snap = V.save_snapshot(desk["id"], "video", res["frames"][0]["jpeg"])
    except Exception:
        pass
    ev = store.add_vision_event(desk["id"], f"video:{label}", {}, backend="vlm", source="video",
                                question=question or "describe", answer=(res["answer"] or "")[:2000], snapshot=snap)
    return jsonify({"ok": True, "answer": res["answer"], "duration_s": res["duration_s"],
                    "frames": len(res["frames"]), "event_id": ev["id"]})


@app.get("/api/theatre/clips")
def api_theatre_clips():
    need_desk()
    d = Path(os.environ.get("THEATRE_CLIPS", "data/theatre_clips"))
    clips = sorted(f.name for f in d.glob("*.mp4")) if d.is_dir() else []
    return jsonify({"clips": clips})


@app.get("/desk/theatre/clip/<path:name>")
def desk_theatre_clip(name):
    need_desk()
    d = Path(os.environ.get("THEATRE_CLIPS", "data/theatre_clips")).resolve()
    f = (d / name).resolve()
    if d not in f.parents or not f.is_file():
        abort(404)
    return send_file(f, mimetype="video/mp4", conditional=True, max_age=3600)


@app.get("/desk/theatre")
def desk_theatre():
    need_desk()
    return send_from_directory(STATIC_DIR, "theatre.html")


@app.post("/api/vision/detect")
def api_vision_detect():
    """Local YOLO on one frame — milliseconds, free, no external call. Powers the theatre's live overlay."""
    need_desk()
    from .. import vision as V
    j = request.json or {}
    try:
        jpeg = base64.b64decode(j.get("image", ""))
    except Exception:
        jpeg = b""
    if len(jpeg) < 100:
        return jsonify({"error": "no frame"}), 400
    if not V.DETECTOR.available:
        return jsonify({"detector": "", "boxes": [], "counts": {}})
    dets = V.DETECTOR.detect(jpeg)
    return jsonify({"detector": "yolo", "boxes": dets, "counts": V.counts(dets)})


@app.post("/api/vision/live")
def api_vision_live():
    """Live theatre: one frame in, commentary tokens streamed straight back (chunked text).
    The finished note is logged as a vision event so the RAG log covers live watching too."""
    desk = need_desk()
    from .. import vision as V
    j = request.json or {}
    try:
        jpeg = base64.b64decode(j.get("image", ""))
    except Exception:
        jpeg = b""
    if len(jpeg) < 100:
        return jsonify({"error": "no frame"}), 400
    camera = re.sub(r"[^\w.\- ]+", "_", str(j.get("camera", "feed")))[:60] or "feed"
    model = str(j.get("model", "") or "")
    context = str(j.get("context", "") or "")
    t = str(j.get("t", "") or "")
    _base, _key, chosen = V._vlm_cfg(model)

    def gen():
        parts = []
        try:
            for delta in V.describe_stream(jpeg, "What is happening in this frame? What changed?", model=model,
                                           context=context):
                if isinstance(delta, dict):                    # provider/model meta arrives first
                    yield "\x01" + json.dumps(delta) + "\n"
                    continue
                parts.append(delta)
                yield delta
        except Exception as exc:
            yield f"[vision error: {exc}]"
        note = "".join(parts).strip()
        if note and not note.startswith("[vision error"):
            try:
                snap = V.save_snapshot(desk["id"], f"live-{camera}", jpeg)
                store.add_vision_event(desk["id"], f"live:{camera}", {}, backend="vlm", source="live",
                                       question=f"live frame at clip {t}s" if t else "live frame",
                                       answer=note[:1000], snapshot=snap)
            except Exception:
                pass

    resp = Response(stream_with_context(gen()), mimetype="text/plain")
    resp.headers["X-Vision-Model"] = chosen
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/vision/events")
def api_vision_events():
    desk = need_desk()
    try:
        hours = float(request.args.get("hours") or 24)
    except ValueError:
        hours = 24.0
    rows = store.vision_events(desk["id"], request.args.get("camera", ""), time.time() - hours * 3600,
                               request.args.get("q", ""), min(int(request.args.get("limit") or 100), 500),
                               request.args.get("alerts") in ("1", "true"))
    return jsonify([_vev_public(r) for r in rows])


@app.get("/api/vision/snapshot/<int:vid>")
def api_vision_snapshot(vid):
    desk = need_desk()
    e = store.vision_event(vid)
    if not e or e["desk_id"] != desk["id"] or not e.get("snapshot") or not Path(e["snapshot"]).is_file():
        abort(404)
    return send_file(e["snapshot"], mimetype="image/jpeg", max_age=3600)


def _rag_rows(desk_id: int, question: str, camera: str, hours: float, limit: int = 60) -> list[dict[str, Any]]:
    """Retrieval for 'ask the cameras': keyword-scored over the recent event log, recency as tiebreak."""
    rows = store.vision_events(desk_id, camera, time.time() - hours * 3600, "", 600)
    words = {w for w in re.findall(r"[a-z]{3,}", question.lower())
             if w not in {"the", "was", "were", "what", "when", "how", "many", "did", "there", "any", "last", "this", "that", "and", "with", "from"}}
    def score(r):
        blob = f"{r['camera']} {json.dumps(r['counts'])} {r.get('reason','')} {r.get('answer','')} {r.get('question','')}".lower()
        return sum(1 for w in words if w in blob) + (2 if r.get("triggered") else 0)
    rows.sort(key=lambda r: (score(r), r["ts"]), reverse=True)
    picked = rows[:limit]
    picked.sort(key=lambda r: r["ts"])
    return picked


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/vision/demo")
def api_vision_demo():
    """Public site demo: one small frame + the browser detector's counts in, the vision model's narration streamed
    out as SSE (`data: {"t": "..."}` per token, then `{"done": true}` or `{"error": "..."}`). Rate limited per IP,
    a few concurrent slots, tiny answers, subject to the spend cap — real model, no scripted fallback."""
    import base64

    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    hdr = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if not _RL_VISION.allow(ip):
        return Response(_sse({"error": "rate limited - one narration every few seconds per visitor"}), 429, mimetype="text/event-stream", headers=hdr)
    data = request.get_json(silent=True) or {}
    raw = str(data.get("image") or "")
    if "," in raw[:40]:
        raw = raw.split(",", 1)[1]
    try:
        jpeg = base64.b64decode(raw, validate=False)
    except Exception:
        jpeg = b""
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 400_000:
        return Response(_sse({"error": "expected a JPEG frame under 400 KB"}), 400, mimetype="text/event-stream", headers=hdr)
    counts = {}
    for k, v in list((data.get("counts") or {}).items())[:12]:
        try:
            counts[re.sub(r"[^a-z ]", "", str(k).lower())[:24]] = max(0, min(99, int(v)))
        except (TypeError, ValueError):
            continue
    prev = re.sub(r"\s+", " ", str(data.get("prev") or ""))[:400]
    source = re.sub(r"[^a-zA-Z ]", "", str(data.get("source") or "camera"))[:40]
    base, key, model = _demo_cfg()
    if not key:
        return Response(_sse({"error": "no vision model key configured on the server"}), 503, mimetype="text/event-stream", headers=hdr)
    if ":free" not in model:
        blocked = _spend_blocked()
        if blocked:
            return Response(_sse({"error": blocked}), 503, mimetype="text/event-stream", headers=hdr)
    jpeg = V._shrink(jpeg, 448)
    counts_txt = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing above threshold"
    system = ("You narrate a live camera feed for a small business's operations desk, one frame at a time. "
              "Say only what is visible. Count carefully; the on-device detector's counts are a hint, not the truth - "
              "if they look wrong say what you actually see. Note what changed since the previous narration when there was one, "
              "otherwise describe the scene. 25 to 45 words, plain text, one paragraph, no markdown, no lists. "
              "Never identify a person, never read number plates or text that could identify someone.")
    user_txt = (f"Source: {source}. Detector counts this frame: {counts_txt}. "
                + (f"Previous narration: \"{prev}\" " if prev else "This is the first frame. ")
                + "Narrate now.")
    payload = {"model": model, "max_tokens": 110, "temperature": 0.2, "stream": True,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": [{"type": "text", "text": user_txt},
                                                         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}]}]}
    headers = _demo_headers(key, "Atlas Desks site vision demo")

    return Response(_demo_stream(base, headers, payload, model), mimetype="text/event-stream", headers=hdr)


def _demo_cfg() -> tuple[str, str, str]:
    """Provider for the public site demos. VISION_DEMO_KEY / VISION_DEMO_MODEL let the demo run on its own key and a
    free model while the desks stay in scripted mode (DESK_MODE=demo)."""
    base, key, model = V._vlm_cfg(os.environ.get("VISION_DEMO_MODEL", ""))
    key = (os.environ.get("VISION_DEMO_KEY") or key or "").strip()
    return base, key, model


def _demo_headers(key: str, title: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream",
            "HTTP-Referer": "https://atlas-ops.onrender.com", "X-Title": title}


def _demo_stream(base: str, headers: dict[str, str], payload: dict[str, Any], model: str, first: dict[str, Any] | None = None):
    """SSE generator shared by the site demos: one concurrency slot, tokens as {"t": ...}, then {"done": true}."""
    import httpx
    t0 = time.time()
    if not _VISION_SLOTS.acquire(blocking=False):          # acquired inside the generator so a slot is never leaked
        yield _sse({"error": "busy - a few other visitors are using the demo right now, retrying shortly"})
        return
    try:
        if first:
            yield _sse(first)
        with httpx.Client(timeout=60) as c, c.stream("POST", base + "/chat/completions", headers=headers, json=payload) as r:
            if r.status_code >= 400:
                body = b"".join(r.iter_bytes())[:200].decode("utf-8", "replace")
                yield _sse({"error": f"model HTTP {r.status_code}: {body}"})
                return
            sent = 0
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    j = json.loads(chunk)
                except ValueError:
                    continue
                if j.get("error"):
                    yield _sse({"error": str(j["error"].get("message") if isinstance(j["error"], dict) else j["error"])[:200]})
                    return
                for ch in j.get("choices") or []:
                    t = (ch.get("delta") or {}).get("content") or ""
                    if t:
                        sent += len(t)
                        yield _sse({"t": t, "model": model.split("/")[-1]})
            if not sent:
                yield _sse({"error": "model returned no text"})
                return
            yield _sse({"done": True, "model": model.split("/")[-1], "ms": int((time.time() - t0) * 1000)})
    except Exception as exc:
        yield _sse({"error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    finally:
        _VISION_SLOTS.release()


_ASK_STOP = {"the", "was", "were", "what", "when", "how", "many", "did", "there", "any", "last", "this", "that", "and",
             "with", "from", "have", "has", "been", "are", "you", "see", "camera", "cameras", "time", "times"}


@app.post("/api/vision/demo/ask")
def api_vision_demo_ask():
    """Public site demo, the RAG half: the browser sends the question plus its own event log (one entry per narration:
    counts + analyst text + clock time). We retrieve the best-matching events, tell the browser which ones ([#n]) were
    used, then stream the model's answer, which must cite them. Stateless - nothing is stored server side."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?")
    hdr = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if not _RL_VISION.allow(ip):
        return Response(_sse({"error": "rate limited - a few questions a minute per visitor"}), 429, mimetype="text/event-stream", headers=hdr)
    data = request.get_json(silent=True) or {}
    q = re.sub(r"\s+", " ", str(data.get("question") or "")).strip()[:300]
    if len(q) < 3:
        return Response(_sse({"error": "ask a question first"}), 400, mimetype="text/event-stream", headers=hdr)
    events: list[dict[str, Any]] = []
    for raw in list(data.get("events") or [])[:80]:
        if not isinstance(raw, dict):
            continue
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        counts = {}
        for k, v in list((raw.get("counts") or {}).items())[:12]:
            try:
                counts[re.sub(r"[^a-z ]", "", str(k).lower())[:24]] = max(0, min(99, int(v)))
            except (TypeError, ValueError):
                continue
        events.append({"n": n, "time": re.sub(r"[^0-9:]", "", str(raw.get("time") or ""))[:8],
                       "camera": re.sub(r"[^A-Za-z0-9 ]", "", str(raw.get("camera") or ""))[:32],
                       "counts": counts, "text": re.sub(r"\s+", " ", str(raw.get("text") or ""))[:400]})
    if not events:
        return Response(_sse({"error": "no events yet - let the analyst narrate a few frames first"}), 400, mimetype="text/event-stream", headers=hdr)
    words = {w for w in re.findall(r"[a-z]{3,}", q.lower()) if w not in _ASK_STOP}

    def score(e: dict[str, Any]) -> int:
        blob = (" ".join(f"{v} {k}" for k, v in e["counts"].items()) + " " + e["text"]).lower()
        return sum(1 for w in words if w in blob)
    ranked = sorted(events, key=lambda e: (score(e), e["n"]), reverse=True)
    picked = sorted(ranked[:12], key=lambda e: e["n"])
    used = [e["n"] for e in picked]
    base, key, model = _demo_cfg()
    if not key:
        return Response(_sse({"error": "no model key configured on the server"}), 503, mimetype="text/event-stream", headers=hdr)
    if ":free" not in model:
        blocked = _spend_blocked()
        if blocked:
            return Response(_sse({"error": blocked}), 503, mimetype="text/event-stream", headers=hdr)
    log = "\n".join(f"[#{e['n']}] {e['time']} | " + (f"camera: {e['camera']} | " if e["camera"] else "") + (", ".join(f"{v} {k}" for k, v in e['counts'].items()) or "nothing detected")
                    + (f" | analyst: {e['text']}" if e["text"] else "") for e in picked)
    system = ("You answer questions about a camera feed using ONLY the event log below, which an operations desk built from "
              "the feed (one line per narrated frame: clock time, camera name, detector counts, analyst note). Cite the events you rely on "
              "as [#n]. If the log cannot answer, say so plainly and say what would be needed. 30 to 70 words, plain text, "
              "no markdown, no lists. Never identify a person.\n\nEVENT LOG\n" + log)
    payload = {"model": model, "max_tokens": 160, "temperature": 0.2, "stream": True,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": q}]}
    headers = _demo_headers(key, "Atlas Desks site vision ask")
    return Response(_demo_stream(base, headers, payload, model, first={"used": used, "of": len(events)}),
                    mimetype="text/event-stream", headers=hdr)


@app.post("/api/vision/ask")
def api_vision_ask():
    desk = need_desk()
    d = request.get_json(force=True) or {}
    q = str(d.get("question") or "").strip()
    if not q:
        return jsonify({"error": "question required"}), 400
    try:
        hours = float(d.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    rows = _rag_rows(desk["id"], q, str(d.get("camera") or ""), hours)
    lines = [f"[#{r['id']}] {time.strftime('%a %d %b %H:%M', time.localtime(r['ts']))} | {r['camera']} | {V.counts_text(r['counts'])} | motion {r['motion']:.2f}"
             + (" | ALERT " + (r.get("reason") or "") if r.get("triggered") else (" | " + r["reason"] if r.get("reason") else ""))
             + (f" | analyst: {r['answer'][:200]}" if r.get("answer") else "") for r in rows]
    if not rows:
        return jsonify({"answer": f"The camera log has nothing in the last {hours:g} hours" + (f" for {d.get('camera')}" if d.get("camera") else "") + ".",
                        "evidence": [], "mode": _mode()})
    if _mode() == "demo":
        cams = sorted({r["camera"] for r in rows})
        alerts = [r for r in rows if r.get("triggered")]
        tot: dict[str, int] = {}
        for r in rows:
            for k, v in (r["counts"] or {}).items():
                tot[k] = tot.get(k, 0) + v
        last = rows[-1]
        answer = (f"Demo mode (no live model). In the last {hours:g}h the log has {len(rows)} event(s) on {', '.join(cams)}; "
                  f"{len(alerts)} woke the desk. Totals seen: {V.counts_text(tot)}. Most recent: {last['camera']} at "
                  f"{time.strftime('%H:%M', time.localtime(last['ts']))} — {V.counts_text(last['counts'])}"
                  + (f"; analyst said: {last['answer'][:160]}" if last.get("answer") else "") + ".")
    else:
        from ..providers import ProviderPool
        configs = desk_configs(desk)
        atlas_agent = next((a for a in configs["agents"] if a["id"] == "atlas"), {})
        pool = ProviderPool(configs["providers"])
        prov = pool.get(atlas_agent.get("provider") or "")
        system = ("You answer the owner's questions about what their cameras and sensors saw, using ONLY the event log below. "
                  "Each line: [#id] time | camera | objects counted | motion score | alert reason | analyst answer (from a vision model that saw the frame). "
                  "Give times, cameras and counts. Cite event ids in square brackets. If the log does not contain the answer, say so plainly — never invent. "
                  f"Today is {time.strftime('%A %d %B %Y %H:%M')}. Business context: {configs['business'].get('name','')} — {configs['business'].get('extra_context','')[:400]}")
        prompt = "Event log (oldest first):\n" + "\n".join(lines) + f"\n\nQuestion: {q}"
        try:
            resp = prov.chat(system, [prov.user_message(prompt)], [], model=atlas_agent.get("model", ""))
            answer = (resp.text or "").strip() or "(no answer)"
        except Exception as exc:
            return jsonify({"error": f"model error: {type(exc).__name__}: {str(exc)[:200]}", "evidence": [_vev_public(r) for r in rows]}), 502
    return jsonify({"answer": answer, "evidence": [_vev_public(r) for r in rows[-12:]], "mode": _mode(), "events_considered": len(rows)})


@app.route("/hook/<token>/vision", methods=["GET", "POST"])
def hook_vision(token):
    """External detectors post here: ESP32-CAM / PIR nodes, Frigate, NVR alarm outputs, Home Assistant.
    JSON {camera, labels: {"person": 2} | ["person","person"], note, image (base64 JPEG, optional), trigger (default true)}."""
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "post": "JSON {camera, labels, note, image(base64 jpeg), trigger}"})
    d = request.get_json(silent=True) or request.form.to_dict() or {}
    camera = str(d.get("camera") or d.get("source") or d.get("device") or "sensor").strip()[:60]
    labels = d.get("labels") or d.get("objects") or {}
    counts: dict[str, int] = {}
    if isinstance(labels, dict):
        for k, v in labels.items():
            try:
                counts[V._norm_label(str(k))] = int(v)
            except (TypeError, ValueError):
                continue
    elif isinstance(labels, list):
        for k in labels:
            kk = V._norm_label(str(k))
            counts[kk] = counts.get(kk, 0) + 1
    elif isinstance(labels, str) and labels.strip():
        for k in labels.split(","):
            if k.strip():
                kk = V._norm_label(k)
                counts[kk] = counts.get(kk, 0) + 1
    note = str(d.get("note") or d.get("message") or d.get("event") or "").strip()[:500]
    snap = ""
    if d.get("image"):
        try:
            import base64 as _b64
            raw = _b64.b64decode(str(d["image"]).split(",")[-1])
            if raw[:2] == b"\xff\xd8":
                snap = V.save_snapshot(desk["id"], camera, V.annotate(V._shrink(raw), [], f"{time.strftime('%d %b %H:%M:%S')}  {camera}: {note[:60]}"))
        except Exception:
            snap = ""
    trigger = str(d.get("trigger", "1")).lower() not in ("0", "false", "no")
    backend = "".join(ch for ch in str(d.get("backend") or "external")[:32] if ch.isalnum() or ch in "/-_.") or "external"
    dstore = store.for_desk(desk["id"])
    ev = dstore.add_vision_event(camera, counts, motion=float(d.get("motion") or 0), backend=backend,
                                 reason=note or "external event", snapshot=snap, triggered=trigger, source="hook")
    rid = ""
    if trigger:
        task = ("Assess this sensor/camera event, log it, and tell the right person only if it matters.\n\n"
                f"EXTERNAL EVENT — {camera} at {time.strftime('%A %d %B %H:%M')}\n"
                f"Reported: {V.counts_text(counts) if counts else 'no object counts'}" + (f"; note: {note}" if note else "") + "\n"
                f"Event id: {ev['id']}." + (" Snapshot attached." if snap else "") + " Use camera_events for history; camera_look works only for cameras the desk can reach itself.")
        rid = _start_run(desk, task, "auto")
        dstore.set_vision_run(ev["id"], rid)
    return jsonify({"ok": True, "event_id": ev["id"], "run_id": rid})


# ---------------------------------------------------------------------------- inbound webhook (public, token-addressed)
# ---------------------------------------------------------------------------- api: design studio
def _providers_cfg() -> dict[str, Any]:
    providers = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
    for name, pc in cfg.DEFAULT_PROVIDERS["providers"].items():
        providers.setdefault("providers", {}).setdefault(name, dict(pc))
    prov = os.environ.get("DESK_PROVIDER", "").strip() or providers.get("default_provider", "openrouter")
    providers["default_provider"] = prov
    return providers


def _design_session(sid: str) -> DS.DesignSession:
    s = DS.SESSIONS.get(sid)
    if not s:
        saved = store.load_design_session(sid)          # design chats survive restarts and deploys
        if saved:
            s = DS.DesignSession.from_dict(saved)
            DS.SESSIONS[s.id] = s
    if not s:
        abort(Response(json.dumps({"error": "no_session"}), 404, mimetype="application/json"))
    return s


def _save_design(s: DS.DesignSession) -> None:
    try:
        store.save_design_session(s.id, s.to_dict())
    except Exception as exc:
        print("design session save failed:", exc)


@app.post("/api/design/start")
def api_design_start():
    u = current_user()
    if not u and not OPEN:
        abort(401)
    _require_live()
    d = request.get_json(silent=True) or {}
    tier = d.get("tier") if d.get("tier") in templates.TIERS else "free"
    s = DS.new_session(_mode(), tier)
    if u and u.get("company"):
        s.transcript[0]["text"] = s.transcript[0]["text"].replace("Tell me what your business does", f"Tell me what {u['company']} does")
    links = d.get("links") or []
    if isinstance(links, str):
        links = re.split(r"[\s,]+", links)
    s.links = [str(x).strip() for x in links if str(x).strip()][:4]
    _save_design(s)
    return jsonify(s.public())


@app.post("/api/design/<sid>/study")
def api_design_study(sid):
    """Read the owner's links (website, socials, booking page), stream progress, then seed the designer with a company
    profile + first-draft blueprint. SSE: {"t":"status","d":...}* then {"t":"done", profile, transcript, suggestions, blueprint}."""
    from .. import study as ST
    s = _design_session(sid)
    d = request.get_json(silent=True) or {}
    links = d.get("links") or s.links
    if isinstance(links, str):
        links = re.split(r"[\s,]+", links)
    links = [str(x).strip() for x in links if str(x).strip()][:4]
    if not links:
        return jsonify({"error": "no links"}), 400
    s.links = links
    if s.mode != "demo":
        _require_live()
    import queue
    q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    providers = None if s.mode == "demo" else _providers_cfg()

    def work():
        try:
            prov, model = None, ""
            if providers:
                from ..providers import ProviderPool
                prov = ProviderPool(providers).get()
                if providers.get("default_provider") == "openrouter":
                    model = templates.STRONG_MODEL if s.tier != "free" else templates.FREE_MODEL
            profile = ST.study(links, on_status=lambda t: q.put({"t": "status", "d": t}), provider=prov, model=model)
            with s.lock:
                s.apply_profile(profile)
            _save_design(s)
            q.put({"t": "done", "profile": profile, "transcript": s.transcript, "suggestions": s.suggestions,
                   "blueprint": s.blueprint, "ready": s.ready})
        except Exception as exc:
            q.put({"t": "error", "error": f"{type(exc).__name__}: {exc}"})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield "retry: 1000\n\n"
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/design/<sid>")
def api_design_get(sid):
    return jsonify(_design_session(sid).public())


@app.post("/api/design/<sid>/say")
def api_design_say(sid):
    """One designer turn, streamed as SSE: {"t":"tok","d":...}* then {"t":"done", ...result}."""
    s = _design_session(sid)
    d = request.get_json(force=True) or {}
    text = str(d.get("text") or "").strip()[:4000]
    if not text:
        return jsonify({"error": "empty"}), 400
    if s.mode != "demo":
        _require_live()
    import queue
    q: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
    providers = None if s.mode == "demo" else _providers_cfg()
    model = ""
    if providers and providers.get("default_provider") == "openrouter":
        model = templates.STRONG_MODEL if s.tier != "free" else templates.FREE_MODEL

    def work():
        try:
            def on_tok(t: str):
                if t.startswith(DS.STATUS_MARK):
                    q.put({"t": "status", "d": t[1:]})
                else:
                    q.put({"t": "tok", "d": t})
            res = DS.reply(s, text, on_token=on_tok, providers_cfg=providers, designer_model=model)
            _save_design(s)
            q.put({"t": "done", **res})
        except Exception as exc:
            q.put({"t": "error", "error": f"{type(exc).__name__}: {exc}"})
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        yield "retry: 1000\n\n"
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/api/design/<sid>/blueprint")
def api_design_blueprint(sid):
    """Owner edits from the sketch canvas (rename, tools, add/remove agent, triggers)."""
    s = _design_session(sid)
    d = request.get_json(force=True) or {}
    bp = DS.normalise(d.get("blueprint"), s.blueprint)
    if bp:
        s.blueprint = bp
        s.ready = bool(bp.get("agents"))
        _save_design(s)
    return jsonify({"blueprint": s.blueprint, "ready": s.ready})


@app.post("/api/design/<sid>/build")
def api_design_build(sid):
    """Approve the blueprint: create the desk, schedule its triggers, return what still needs connecting."""
    s = _design_session(sid)
    u = current_user()
    if not u and not OPEN:
        abort(401)
    d = request.get_json(force=True) or {}
    bp = DS.normalise(d.get("blueprint"), s.blueprint) or s.blueprint
    if not bp or not bp.get("agents"):
        return jsonify({"error": "The blueprint has no agents yet - keep talking to the designer first."}), 400
    tier = d.get("tier") if d.get("tier") in templates.TIERS else s.tier
    conf = DS.blueprint_to_desk(bp, tier)
    name = (d.get("name") or (bp.get("business") or {}).get("name") or "New desk").strip()
    if s.desk_id and store.desk(s.desk_id):
        store.update_desk(s.desk_id, name=name, tier=tier, config=conf)
        desk = store.desk(s.desk_id)
    else:
        desk = store.add_desk(u["id"] if u else 0, name, "custom", tier, conf)
        s.desk_id = desk["id"]
    session["desk"] = desk["id"]
    s.blueprint = bp
    # triggers -> automations
    existing = {j["name"] for j in store.jobs(desk["id"])}
    jobs = []
    for w in bp.get("workflows") or []:
        t = w.get("trigger") or {}
        jname = f"{w['name']} ({t.get('kind')})"
        if jname in existing:
            continue
        if t.get("kind") == "schedule":
            jobs.append(store.add_job(desk["id"], "task", jname, f"Run workflow '{w['name']}': {t.get('detail') or 'scheduled sweep'}. Mode: {w['id']}", 1440, time.time() + 86400))
        elif t.get("kind") == "inbox":
            jobs.append(store.add_job(desk["id"], "inbox_watch", jname, "", 2, time.time() + 120))
    _save_design(s)
    return jsonify({"desk": _desk_public(desk), "connect": _connect_plan(desk, bp), "jobs": jobs})


def _connect_plan(desk: dict[str, Any], bp: dict[str, Any]) -> dict[str, Any]:
    have = store.connectors(desk["id"])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for c in have:
        by_kind.setdefault(c["kind"], []).append(c)
    items = []
    wanted = list(bp.get("connectors") or [])
    if any(a.get("engine") == "hermes_agent" for a in bp.get("agents") or []) and not any(c["kind"] == "hermes_agent" for c in wanted):
        who = ", ".join(a["name"] for a in bp["agents"] if a.get("engine") == "hermes_agent")
        wanted.append({"kind": "hermes_agent", "name": "Hermes Agent", "purpose": f"Runs {who}", "required": True})
    for c in wanted:
        match = (by_kind.get(c["kind"]) or [None])[0]
        items.append({**c, "fields": I.KINDS[c["kind"]]["fields"], "hint": I.KINDS[c["kind"]]["hint"],
                      "connector_id": match["id"] if match else None, "status": (match or {}).get("status") or ("built-in" if c["kind"] == "webhook" else "not connected")})
    return {"connectors": items, "hook_url": request.host_url.rstrip("/") + "/hook/" + store.ensure_hook_token(desk["id"]),
            "kinds": I.KINDS}


@app.get("/api/design/<sid>/connect")
def api_design_connect(sid):
    s = _design_session(sid)
    if not s.desk_id or not store.desk(s.desk_id):
        return jsonify({"error": "not built"}), 400
    return jsonify(_connect_plan(store.desk(s.desk_id), s.blueprint or {}))



@app.route("/hook/<token>", methods=["GET", "POST"])
def hook(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    if request.method == "GET":
        return jsonify({"ok": True, "desk": desk["name"], "post": "JSON {name, email, phone, company, notes, source}"})
    d = request.get_json(silent=True) or request.form.to_dict() or {}
    name = (d.get("name") or d.get("full_name") or "").strip()
    email_ = (d.get("email") or "").strip()
    notes = (d.get("notes") or d.get("message") or d.get("enquiry") or "").strip()
    if not name and not email_:
        return jsonify({"error": "name or email required"}), 400
    dstore = store.for_desk(desk["id"])
    lid = dstore.add_lead(name or email_.split("@")[0], (d.get("company") or "").strip(), email_, (d.get("phone") or "").strip(),
                          (d.get("source") or "webhook").strip(), notes)
    if email_:
        dstore.upsert_contact(email_, {"name": name, "company": d.get("company", ""), "email": email_, "phone": d.get("phone", ""), "stage": "New", "notes": "Inbound via webhook"})
    rid = _start_run(desk, _lead_task(dstore.lead(lid)), "auto", lid)
    return jsonify({"ok": True, "lead_id": lid, "run_id": rid})


def _inbound_message(desk: dict[str, Any], phone: str, name: str, text: str, source: str) -> str:
    """A WhatsApp / SMS message from a prospect: new lead + run (existing contact keeps its name/company)."""
    dstore = store.for_desk(desk["id"])
    phone = "+" + I._digits(phone) if phone else ""
    known = next((c for c in dstore.contacts(phone) if c.get("phone") == phone), None) if phone else None
    name = name or (known or {}).get("name") or (phone or "unknown")
    lid = dstore.add_lead(name, (known or {}).get("company", ""), (known or {}).get("email", "") or "", phone, source, text)
    key = (known or {}).get("email") or name
    dstore.upsert_contact(key, {"name": name, "phone": phone, "stage": (known or {}).get("stage") or "New",
                                "notes": f"Inbound via {source}: {text[:200]}"})
    task = _lead_task(dstore.lead(lid)) + f"\n\nThis arrived by {source}. Reply on the same channel (queue_action kind={'whatsapp' if 'whatsapp' in source else 'sms'}, to={phone}) — keep it short."
    return _start_run(desk, task, "auto", lid)


@app.route("/hook/<token>/whatsapp", methods=["GET", "POST"])
def hook_whatsapp(token):
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    conn = next((c for c in store.connectors(desk["id"]) if c["kind"] == "whatsapp"), None)
    if request.method == "GET":                      # Meta verification handshake
        want = (conn or {}).get("config", {}).get("verify_token") or ""
        if request.args.get("hub.mode") == "subscribe" and want and request.args.get("hub.verify_token") == want:
            return request.args.get("hub.challenge", ""), 200
        return jsonify({"error": "verify_token mismatch or no WhatsApp connector"}), 403
    msgs = I.parse_whatsapp_webhook(request.get_json(silent=True) or {})
    runs = [_inbound_message(desk, m["from"], m["name"], m["text"], "whatsapp") for m in msgs if m.get("from")]
    return jsonify({"ok": True, "messages": len(msgs), "runs": runs})


@app.post("/hook/<token>/sms")
def hook_sms(token):
    """Twilio inbound webhook (SMS or WhatsApp sandbox): form fields From, Body, ProfileName."""
    desk = store.desk_by_token(token)
    if not desk:
        abort(404)
    f = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
    frm, body = (f.get("From") or f.get("from") or ""), (f.get("Body") or f.get("body") or "")
    if not frm:
        return jsonify({"error": "From required"}), 400
    source = "twilio whatsapp" if frm.startswith("whatsapp:") else "sms"
    rid = _inbound_message(desk, frm.replace("whatsapp:", ""), f.get("ProfileName") or "", body, source)
    return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", mimetype="application/xml", headers={"X-Atlas-Run": rid})


_n_interrupted = store.mark_interrupted()            # restart recovery: nothing can still be running from a previous process
if _n_interrupted:
    print(f"marked {_n_interrupted} orphaned run(s) as interrupted")
_n_enc = store.encrypt_legacy_connectors()           # secrets at rest: encrypt any plaintext connector rows from before
if _n_enc:
    print(f"encrypted {_n_enc} legacy connector config(s)")
if OPEN and os.environ.get("RENDER"):
    print("WARNING: DESK_OPEN=1 on a public deployment - the portal and every desk are reachable without login")
scheduler.LIVE = lambda: _mode() != "demo" and not _live_reason()
scheduler.start(store, _start_run, store.desk)
threading.Thread(target=_watchdog_loop, daemon=True, name="atlas-watchdog").start()


def main():
    port = int(os.environ.get("PORT", "8094"))
    print(f"Atlas Desk  mode={_mode()}  accounts={'off' if OPEN else 'on'}  http://localhost:{port}/desk")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
