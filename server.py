#!/usr/bin/env python3
"""
claude-hub — local web UI buat manage Claude Code wrappers.
Port: 8765 (localhost only)
DB:   ~/.claude-hub/profiles.db
Wrappers ditulis ke ~/.local/bin/claude-<name>
Launcher menu: claude-deep (baca DB ini)
"""
from __future__ import annotations

import os
import json
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────
HOME = Path.home()
HUB_DIR = HOME / ".claude-hub"
HUB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = HUB_DIR / "profiles.db"
BIN_DIR = HOME / ".local" / "bin"
BIN_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"

# claude-worker proxy that wrappers route through (per-provider path /p/<name>/v1).
# The hub now serves the translation proxy itself at /p/<name>/v1, so wrappers
# point back at the hub. Override with CLAUDE_WORKER_URL only for legacy setups.
WORKER_URL = os.environ.get("CLAUDE_WORKER_URL", "http://localhost:8765")


# ──────────────────────────────────────────────────────────────────────────────
# DB  (contextmanager: commit on success, rollback on error, ALWAYS close → no fd leak)
# ──────────────────────────────────────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT UNIQUE NOT NULL,
            base_url      TEXT NOT NULL,
            auth_token    TEXT NOT NULL,
            opus_model    TEXT,
            sonnet_model  TEXT,
            haiku_model   TEXT,
            extra_args    TEXT DEFAULT '--dangerously-skip-permissions',
            note          TEXT DEFAULT '',
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id  INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
            event       TEXT,
            slot        TEXT,
            latency_ms  INTEGER,
            detail      TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)


# ──────────────────────────────────────────────────────────────────────────────
# Pre-load existing wrappers on first run (skip .bak files)
# ──────────────────────────────────────────────────────────────────────────────
def maybe_import_existing():
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
    if n > 0:
        return

    for f in sorted(BIN_DIR.glob("claude-*")):
        if not f.is_file() or f.is_symlink():
            continue
        if any(seg in f.name for seg in (".bak", ".old", ".tmp")):
            continue
        try:
            content = f.read_text()
        except Exception:
            continue
        env = {}
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("export "):
                continue
            line = line[len("export "):]
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
        if "ANTHROPIC_BASE_URL" not in env:
            continue
        name = f.name[len("claude-"):]
        try:
            with db() as c:
                c.execute("""
                INSERT INTO profiles (name, base_url, auth_token,
                    opus_model, sonnet_model, haiku_model, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    name,
                    env.get("ANTHROPIC_BASE_URL", ""),
                    env.get("ANTHROPIC_AUTH_TOKEN", ""),
                    env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                        or env.get("ANTHROPIC_MODEL", ""),
                    env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
                    env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
                    f"Imported from {f}",
                ))
        except sqlite3.IntegrityError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Wrapper file generator
# ──────────────────────────────────────────────────────────────────────────────
def render_wrapper(p: dict) -> str:
    # Wrapper routes through the claude-worker proxy via a per-provider path
    # (/p/<name>/v1). The worker rewrites canonical opus/sonnet/haiku to this
    # provider's real model names — so Claude Code keeps seeing canonical names
    # and AUTO-COMPACT WORKS. We deliberately UNSET the DEFAULT model env vars
    # (which is what used to break auto-compact + duplicated the /model menu).
    name = p["name"]
    worker = WORKER_URL.rstrip("/")
    lines = [
        "#!/usr/bin/env bash",
        f"# claude-{name} — generated by claude-hub (hub is the translation proxy)",
        f"# Real provider : {p['base_url']}",
        f"# Slots: opus={p.get('opus_model') or '-'}, "
        f"sonnet={p.get('sonnet_model') or '-'}, "
        f"haiku={p.get('haiku_model') or '-'}",
        f'export ANTHROPIC_BASE_URL="{worker}/p/{name}/v1"',
        f'export ANTHROPIC_AUTH_TOKEN="hub-{name}"',
        "# Unset custom model overrides so Claude Code uses its built-in",
        "# Opus/Sonnet/Haiku slots (clean /model menu + auto-compact).",
        "unset ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "unset ANTHROPIC_SMALL_FAST_MODEL ANTHROPIC_MODEL",
        "",
    ]
    extra = (p.get("extra_args") or "").strip()
    lines.append(f'exec "$HOME/.local/bin/claude" {extra} "$@"')
    return "\n".join(lines) + "\n"


def sync_provider_to_worker(p: dict) -> Optional[str]:
    """Push this profile to claude-worker as a provider (upsert by name) so the
    worker knows how to route /p/<name>/v1. Returns None on success, else error str."""
    payload = {
        "name": p["name"],
        "baseUrl": p["base_url"],
        "authToken": p.get("auth_token") or "",
        "passClientAuth": False,
        "slots": {
            "opus": {
                "model": p.get("opus_model") or "",
                "context_window": p.get("opus_ctx") or 1000000,
                "max_output_tokens": p.get("opus_out") or 128000,
            },
            "sonnet": {
                "model": p.get("sonnet_model") or "",
                "context_window": p.get("sonnet_ctx") or 1000000,
                "max_output_tokens": p.get("sonnet_out") or 64000,
            },
            "haiku": {
                "model": p.get("haiku_model") or "",
                "context_window": p.get("haiku_ctx") or 200000,
                "max_output_tokens": p.get("haiku_out") or 64000,
            },
        },
    }
    try:
        with httpx.Client(timeout=6.0) as cli:
            # Find existing worker provider with same name → reuse its id (update).
            existing = cli.get(f"{WORKER_URL}/api/providers").json().get("providers", [])
            for wp in existing:
                if wp.get("name", "").lower() == p["name"].lower():
                    payload["id"] = wp["id"]
                    break
            r = cli.post(f"{WORKER_URL}/api/providers", json=payload)
            r.raise_for_status()
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def apply_wrapper(p: dict) -> Path:
    # Guard: never overwrite reserved command names. "deep" would clobber the
    # claude-deep launcher menu; "worker"/"hub" clobber other tooling.
    reserved = {"deep", "worker", "hub", "all", "session-sync"}
    if p["name"].lower() in reserved:
        raise HTTPException(
            400,
            f"nama '{p['name']}' dipakai command sistem (claude-{p['name']}). "
            f"Pakai nama lain, mis. '{p['name']}seek' atau '{p['name']}1'.",
        )
    target = BIN_DIR / f"claude-{p['name']}"
    target.write_text(render_wrapper(p))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Hub is now the proxy itself (reads providers from its own DB), so no
    # external worker sync is needed.
    return target


def remove_wrapper(name: str):
    target = BIN_DIR / f"claude-{name}"
    if target.exists():
        target.unlink()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP probes
# ──────────────────────────────────────────────────────────────────────────────
async def fetch_models(base_url: str, token: str, timeout: float = 10.0) -> list[dict]:
    url = base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data.get("data") or data.get("models") or []


async def ping_model(base_url: str, token: str, model: str,
                     timeout: float = 30.0) -> dict:
    if not model:
        return {"ok": False, "latency_ms": 0, "detail": "no model set"}
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/2.1.158",
        "x-app": "claude-hub",
    }
    body = {
        "model": model,
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "ping"}],
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(url, headers=headers, json=body)
        dt = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return {"ok": True, "latency_ms": dt, "detail": "200"}
        return {"ok": False, "latency_ms": dt,
                "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        dt = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "latency_ms": dt, "detail": f"{type(e).__name__}: {e}"}


# ──────────────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────────────
class ProfileIn(BaseModel):
    name: str
    base_url: str
    auth_token: str
    opus_model: Optional[str] = ""
    sonnet_model: Optional[str] = ""
    haiku_model: Optional[str] = ""
    opus_ctx: Optional[int] = 1000000
    opus_out: Optional[int] = 128000
    sonnet_ctx: Optional[int] = 1000000
    sonnet_out: Optional[int] = 64000
    haiku_ctx: Optional[int] = 200000
    haiku_out: Optional[int] = 64000
    extra_args: Optional[str] = "--dangerously-skip-permissions"
    note: Optional[str] = ""


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Claude Hub")


@app.on_event("startup")
def _start():
    init_db()
    maybe_import_existing()


@app.get("/api/health")
def health():
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"]
    return {"ok": True, "profiles": n}


# ── Proxy: translate canonical opus/sonnet/haiku → provider model ─────────────
# This makes the hub itself the translation engine (replaces the standalone
# claude-worker). Wrappers point at /p/<provider>/v1/... and Claude Code keeps
# sending claude-opus-4-8 / sonnet-4-6 / haiku-4-5 → auto-compact works.
def _detect_slot(model: str) -> str:
    s = (model or "").lower()
    if "opus" in s:
        return "opus"
    if "sonnet" in s:
        return "sonnet"
    if "haiku" in s:
        return "haiku"
    return "opus"


def _provider_by_name(name: str) -> Optional[dict]:
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
    return dict(r) if r else None


@app.get("/p/{provider}/v1/models")
def proxy_models(provider: str):
    # Empty list → Claude Code uses its built-in named slots (clean /model menu).
    return {"data": [], "has_more": False, "first_id": None, "last_id": None}


async def _proxy_to_provider(prov: dict, path: str, request: Request, raw: bytes):
    """Shared proxy core: translate opus/sonnet/haiku → provider model, forward."""
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        return JSONResponse(status_code=400, content={"type": "error", "error": {"type": "invalid_request_error", "message": "invalid JSON"}})

    slot = _detect_slot(payload.get("model", ""))
    target = prov.get(f"{slot}_model")
    if not target:
        return JSONResponse(status_code=400, content={"type": "error", "error": {"type": "invalid_request_error", "message": f"provider '{prov.get('name')}' has no model for slot '{slot}'"}})
    payload["model"] = target

    cap = prov.get(f"{slot}_out") or 0
    if cap and isinstance(payload.get("max_tokens"), int) and payload["max_tokens"] > cap:
        payload["max_tokens"] = cap

    token = prov.get("auth_token") or ""
    headers = {
        "content-type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        "authorization": f"Bearer {token}",
        "x-api-key": token,
    }
    if request.headers.get("anthropic-beta"):
        headers["anthropic-beta"] = request.headers["anthropic-beta"]

    base = (prov.get("base_url") or "").rstrip("/")
    candidates = [f"{base}/{path}"]
    if not base.endswith("/v1"):
        candidates.append(f"{base}/v1/{path}")

    is_stream = payload.get("stream") is True
    body = json.dumps(payload)

    async def gen(url_list):
        last = None
        async with httpx.AsyncClient(timeout=600.0) as cli:
            for url in url_list:
                async with cli.stream("POST", url, headers=headers, content=body) as r:
                    if r.status_code == 404 and url != url_list[-1]:
                        last = r
                        continue
                    async for chunk in r.aiter_bytes():
                        yield chunk
                    return
        if last is not None:
            yield b""

    ct = "text/event-stream" if is_stream else "application/json"
    return StreamingResponse(gen(candidates), media_type=ct)


@app.post("/p/{provider}/v1/{path:path}")
async def proxy_messages(provider: str, path: str, request: Request):
    prov = _provider_by_name(provider)
    if not prov:
        return JSONResponse(status_code=404, content={"type": "error", "error": {"type": "not_found_error", "message": f"unknown provider '{provider}'"}})
    raw = await request.body()
    return await _proxy_to_provider(prov, path, request, raw)


# ── Gateway: single API endpoint for external tools (opus/sonnet/haiku) ────────
# External tools (not Claude Code) point at /gw/v1 with an API key. /gw/v1/models
# advertises opus/sonnet/haiku so the tool can pick one; requests are translated
# to the configured default provider's real model.
GATEWAY_PATH = HUB_DIR / "gateway.json"


def load_gateway() -> dict:
    try:
        return json.loads(GATEWAY_PATH.read_text())
    except Exception:
        return {"api_key": "", "default_provider": ""}


def save_gateway(d: dict):
    GATEWAY_PATH.write_text(json.dumps(d, indent=2))


def _check_api_key(request: Request) -> bool:
    gw = load_gateway()
    key = gw.get("api_key") or ""
    if not key:
        return True  # no key set → open (localhost only anyway)
    auth = request.headers.get("authorization", "")
    sent = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    return sent == key


@app.get("/gw/v1/models")
def gateway_models(request: Request):
    now = int(time.time())
    data = [
        {"id": "opus", "type": "model", "display_name": "Opus", "created": now, "owned_by": "claude-hub"},
        {"id": "sonnet", "type": "model", "display_name": "Sonnet", "created": now, "owned_by": "claude-hub"},
        {"id": "haiku", "type": "model", "display_name": "Haiku", "created": now, "owned_by": "claude-hub"},
    ]
    return {"object": "list", "data": data, "has_more": False}


@app.post("/gw/v1/{path:path}")
async def gateway_proxy(path: str, request: Request):
    if not _check_api_key(request):
        return JSONResponse(status_code=401, content={"type": "error", "error": {"type": "authentication_error", "message": "invalid or missing API key"}})
    gw = load_gateway()
    pname = gw.get("default_provider") or ""
    prov = _provider_by_name(pname) if pname else None
    if not prov:
        return JSONResponse(status_code=503, content={"type": "error", "error": {"type": "api_error", "message": "no default provider set for gateway (set it in the hub UI)"}})
    raw = await request.body()
    return await _proxy_to_provider(prov, path, request, raw)


@app.get("/api/gateway")
def get_gateway():
    gw = load_gateway()
    k = gw.get("api_key") or ""
    return {"default_provider": gw.get("default_provider") or "", "api_key": k,
            "api_key_masked": (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("•••" if k else "")}


@app.post("/api/gateway")
async def set_gateway(request: Request):
    body = json.loads((await request.body()) or b"{}")
    gw = load_gateway()
    if "default_provider" in body:
        gw["default_provider"] = body["default_provider"]
    if "api_key" in body:
        gw["api_key"] = body["api_key"]
    save_gateway(gw)
    return {"ok": True}


# ── API: profiles ────────────────────────────────────────────────────────────
@app.get("/api/profiles")
def list_profiles():
    with db() as c:
        rows = c.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        t = d.get("auth_token") or ""
        d["auth_token_masked"] = (t[:8] + "…" + t[-4:]) if len(t) > 14 else "•••"
        d["wrapper_path"] = str(BIN_DIR / f"claude-{d['name']}")
        d["wrapper_exists"] = (BIN_DIR / f"claude-{d['name']}").exists()
        out.append(d)
    return out


@app.get("/api/profiles/{pid}")
def get_profile(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@app.post("/api/profiles")
def create_profile(p: ProfileIn):
    try:
        with db() as c:
            cur = c.execute("""
                INSERT INTO profiles
                  (name, base_url, auth_token, opus_model, sonnet_model,
                   haiku_model, opus_ctx, opus_out, sonnet_ctx, sonnet_out,
                   haiku_ctx, haiku_out, extra_args, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (p.name, p.base_url, p.auth_token, p.opus_model,
                  p.sonnet_model, p.haiku_model,
                  p.opus_ctx, p.opus_out, p.sonnet_ctx, p.sonnet_out,
                  p.haiku_ctx, p.haiku_out, p.extra_args, p.note))
            return {"id": cur.lastrowid}
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, str(e))


@app.put("/api/profiles/{pid}")
def update_profile(pid: int, p: ProfileIn):
    with db() as c:
        exists = c.execute("SELECT 1 FROM profiles WHERE id=?", (pid,)).fetchone()
        if not exists:
            raise HTTPException(404, "profile not found")
        c.execute("""
            UPDATE profiles SET
              name=?, base_url=?, auth_token=?, opus_model=?, sonnet_model=?,
              haiku_model=?, opus_ctx=?, opus_out=?, sonnet_ctx=?, sonnet_out=?,
              haiku_ctx=?, haiku_out=?, extra_args=?, note=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (p.name, p.base_url, p.auth_token, p.opus_model, p.sonnet_model,
              p.haiku_model, p.opus_ctx, p.opus_out, p.sonnet_ctx, p.sonnet_out,
              p.haiku_ctx, p.haiku_out, p.extra_args, p.note, pid))
    return {"ok": True}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int, remove_file: bool = True):
    with db() as c:
        r = c.execute("SELECT name FROM profiles WHERE id=?", (pid,)).fetchone()
        if not r:
            raise HTTPException(404)
        c.execute("DELETE FROM profiles WHERE id=?", (pid,))
    if remove_file:
        remove_wrapper(r["name"])
    return {"ok": True}


# ── API: actions ─────────────────────────────────────────────────────────────
@app.post("/api/profiles/{pid}/apply")
def apply_profile(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = dict(r)
    target = apply_wrapper(p)
    with db() as c:
        c.execute("INSERT INTO usage_log(profile_id, event, detail) VALUES (?,?,?)",
                  (pid, "apply", str(target)))
    return {"ok": True, "path": str(target), "command": f"claude-{p['name']}"}


@app.post("/api/profiles/{pid}/discover-models")
async def discover_models(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = dict(r)
    try:
        models = await fetch_models(p["base_url"], p["auth_token"])
    except Exception as e:
        raise HTTPException(502, f"fetch models failed: {type(e).__name__}: {e}")
    out = []
    for m in models:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name")
            if mid:
                out.append({"id": mid, "raw": m})
    return {"models": out, "count": len(out)}


@app.post("/api/profiles/{pid}/test")
async def test_profile(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    p = dict(r)
    results = {}
    for slot in ("opus", "sonnet", "haiku"):
        m = p.get(f"{slot}_model")
        if not m:
            results[slot] = {"ok": False, "latency_ms": 0,
                             "detail": "not configured", "model": None}
            continue
        res = await ping_model(p["base_url"], p["auth_token"], m)
        res["model"] = m
        results[slot] = res
        with db() as c:
            c.execute("""INSERT INTO usage_log
                (profile_id, event, slot, latency_ms, detail)
                VALUES (?,?,?,?,?)""",
                (pid, "test_ok" if res["ok"] else "test_fail",
                 slot, res["latency_ms"], res["detail"][:500]))
    return results


@app.get("/api/profiles/{pid}/preview")
def preview_wrapper(pid: int):
    with db() as c:
        r = c.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404)
    return PlainTextResponse(render_wrapper(dict(r)))


@app.get("/api/profiles/{pid}/usage")
def profile_usage(pid: int, limit: int = 50):
    with db() as c:
        rows = c.execute("""
          SELECT event, slot, latency_ms, detail, created_at
          FROM usage_log WHERE profile_id = ?
          ORDER BY id DESC LIMIT ?
        """, (pid, limit)).fetchall()
        agg = c.execute("""
          SELECT
            SUM(event='apply')      AS applies,
            SUM(event='test_ok')    AS test_ok,
            SUM(event='test_fail')  AS test_fail,
            MAX(created_at)         AS last_active
          FROM usage_log WHERE profile_id = ?
        """, (pid,)).fetchone()
    return {"events": [dict(r) for r in rows], "summary": dict(agg) if agg else {}}


# ── UI ───────────────────────────────────────────────────────────────────────
INDEX_HTML = r"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Hub</title>
<style>
  :root{
    --bg:#0a0c10; --bg2:#0e1116; --panel:#141922; --panel2:#1b212c;
    --line:#283040; --line2:#323b4d;
    --fg:#e8ecf3; --dim:#8b95a7; --dim2:#5d6678;
    --acc:#7cf0a7; --acc2:#46d6f0; --warn:#f5c451; --err:#ff7c7c; --ok:#7cf0a7;
    --r:12px;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;}
  body{
    font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:radial-gradient(1200px 600px at 80% -10%, #11202a 0%, var(--bg) 55%);
    color:var(--fg); min-height:100vh;
  }
  code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  a{color:var(--acc2);text-decoration:none;}

  /* top bar */
  header{
    position:sticky; top:0; z-index:20; backdrop-filter:blur(10px);
    background:rgba(10,12,16,.82); border-bottom:1px solid var(--line);
  }
  .bar{max-width:1080px;margin:0 auto;padding:14px 22px;display:flex;align-items:center;gap:14px;}
  .logo{font-size:18px;font-weight:700;letter-spacing:.3px;display:flex;align-items:center;gap:9px;}
  .logo .z{color:var(--acc);}
  .status{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--dim2);box-shadow:0 0 0 0 rgba(124,240,167,.5);}
  .dot.up{background:var(--ok);animation:pulse 2.4s infinite;}
  .dot.down{background:var(--err);}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(124,240,167,.45);}70%{box-shadow:0 0 0 7px rgba(124,240,167,0);}100%{box-shadow:0 0 0 0 rgba(124,240,167,0);}}
  .spacer{flex:1;}

  .wrap{max-width:1080px;margin:0 auto;padding:22px;}

  /* buttons */
  button,.btn{
    background:var(--panel2);color:var(--fg);border:1px solid var(--line2);
    padding:8px 14px;border-radius:9px;cursor:pointer;font:inherit;font-size:13px;
    display:inline-flex;align-items:center;gap:6px;transition:.13s;white-space:nowrap;
  }
  button:hover,.btn:hover{border-color:var(--acc);color:var(--acc);transform:translateY(-1px);}
  button:active{transform:translateY(0);}
  button.primary{background:linear-gradient(180deg,#8ff3b4,#5fe39a);color:#06210f;border-color:transparent;font-weight:700;}
  button.primary:hover{color:#06210f;filter:brightness(1.06);}
  button.ghost{background:transparent;}
  button.danger:hover{border-color:var(--err);color:var(--err);}
  button:disabled{opacity:.5;cursor:not-allowed;transform:none;}

  /* launcher hint */
  .hint{
    background:linear-gradient(180deg,var(--panel),var(--bg2));border:1px solid var(--line);
    border-radius:var(--r);padding:14px 16px;margin-bottom:18px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;
  }
  .hint .k{font-size:12px;color:var(--dim);}
  .hint code{background:#06media;background:#070a0e;border:1px solid var(--line);padding:3px 8px;border-radius:6px;color:var(--acc);font-size:13px;}

  /* cards */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;}
  .card{
    background:linear-gradient(180deg,var(--panel),var(--bg2));
    border:1px solid var(--line);border-radius:var(--r);padding:16px;
    transition:.15s;position:relative;overflow:hidden;
  }
  .card:hover{border-color:var(--line2);box-shadow:0 8px 30px rgba(0,0,0,.35);}
  .card .top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:10px;}
  .num{
    flex:none;width:30px;height:30px;border-radius:8px;background:var(--panel2);
    border:1px solid var(--line2);display:grid;place-items:center;font-weight:700;color:var(--acc2);font-size:14px;
  }
  .pname{font-size:16px;font-weight:700;color:var(--fg);}
  .pname .pre{color:var(--dim2);font-weight:500;}
  .badge{font-size:10.5px;padding:2px 8px;border-radius:99px;border:1px solid var(--line2);color:var(--dim);}
  .badge.on{color:var(--ok);border-color:rgba(124,240,167,.4);background:rgba(124,240,167,.08);}
  .url{font-size:12px;color:var(--dim);word-break:break-all;margin-bottom:10px;}
  .url::before{content:"⛓ ";opacity:.6;}
  .slots{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;}
  .slot{display:flex;align-items:center;gap:8px;font-size:12.5px;}
  .slot .lbl{width:54px;color:var(--dim2);font-size:11px;text-transform:uppercase;letter-spacing:.4px;}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:2px 8px;color:var(--fg);font-family:ui-monospace,monospace;font-size:12px;}
  .chip.empty{color:var(--dim2);border-style:dashed;}
  .tok{font-size:11px;color:var(--dim2);margin-bottom:12px;}
  .acts{display:flex;gap:6px;flex-wrap:wrap;}
  .acts button{padding:6px 10px;font-size:12.5px;}
  .testbox{margin-top:11px;display:flex;flex-direction:column;gap:5px;}
  .tr{display:flex;align-items:center;gap:8px;font-size:12px;}
  .tr .lbl{width:54px;color:var(--dim2);text-transform:uppercase;font-size:10.5px;}
  .pill{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:99px;font-size:11px;border:1px solid var(--line2);color:var(--dim);}
  .pill.ok{color:var(--ok);border-color:rgba(124,240,167,.4);}
  .pill.fail{color:var(--err);border-color:rgba(255,124,124,.4);}
  .note{margin-top:11px;font-size:12px;color:var(--dim);border-top:1px solid var(--line);padding-top:9px;}

  .empty{text-align:center;color:var(--dim);padding:60px 20px;}
  .empty .big{font-size:40px;margin-bottom:10px;opacity:.5;}

  /* modal */
  .overlay{position:fixed;inset:0;background:rgba(4,6,9,.66);backdrop-filter:blur(4px);
    display:none;align-items:flex-start;justify-content:center;z-index:50;padding:28px 16px;overflow:auto;}
  .overlay.show{display:flex;}
  .modal{
    width:100%;max-width:620px;background:var(--panel);border:1px solid var(--line2);
    border-radius:16px;padding:22px;box-shadow:0 24px 80px rgba(0,0,0,.6);animation:pop .16s ease;
  }
  @keyframes pop{from{transform:translateY(8px) scale(.98);opacity:0;}to{transform:none;opacity:1;}}
  .modal h2{margin:0 0 4px;font-size:18px;}
  .modal .msub{color:var(--dim);font-size:12.5px;margin-bottom:18px;}
  label{font-size:11.5px;color:var(--dim);display:block;margin:0 0 5px;text-transform:uppercase;letter-spacing:.4px;}
  input,textarea{
    width:100%;background:var(--bg2);border:1px solid var(--line2);color:var(--fg);
    padding:9px 11px;border-radius:8px;font:inherit;font-size:13.5px;
  }
  input:focus,textarea:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px rgba(124,240,167,.12);}
  .field{margin-bottom:13px;}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  .three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}
  .inline{display:flex;gap:9px;align-items:flex-end;}
  .inline .field{flex:1;margin:0;}
  .hr{border:0;border-top:1px solid var(--line);margin:16px 0;}
  .mlist{max-height:210px;overflow:auto;background:var(--bg2);border:1px solid var(--line);border-radius:8px;padding:6px;margin-top:8px;}
  .mrow{display:flex;gap:6px;align-items:center;padding:5px 6px;border-radius:6px;}
  .mrow:hover{background:var(--panel2);}
  .mrow code{flex:1;font-size:12.5px;}
  .mrow button{padding:3px 9px;font-size:11.5px;}
  .modal .foot{display:flex;gap:8px;margin-top:18px;align-items:center;}
  pre.preview{background:#070a0e;border:1px solid var(--line);border-radius:8px;padding:12px;font-size:12px;overflow:auto;max-height:230px;margin-top:12px;color:var(--dim);}

  /* toast */
  .toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel2);
    border:1px solid var(--acc);padding:11px 18px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.5);
    z-index:99;font-size:13px;animation:tin .2s ease;max-width:90vw;}
  .toast.err{border-color:var(--err);color:#ffd2d2;}
  @keyframes tin{from{transform:translate(-50%,10px);opacity:0;}to{transform:translate(-50%,0);opacity:1;}}
  .hidden{display:none!important;}
  .spin{display:inline-block;animation:rot 1s linear infinite;}
  @keyframes rot{to{transform:rotate(360deg);}}
</style>
</head>
<body>
<header>
  <div class="bar">
    <div class="logo"><span class="z">⚡</span> Claude Hub</div>
    <div class="status"><span class="dot" id="dot"></span><span id="statusTxt">cek…</span></div>
    <div class="spacer"></div>
    <button class="ghost" onclick="openGateway()" title="API Gateway">🔌 API</button>
    <button class="ghost" onclick="loadProfiles()" title="Refresh">↻</button>
    <button class="primary" onclick="openNew()">+ Provider Baru</button>
  </div>
</header>

<div class="wrap">
  <div class="hint">
    <div>
      <div class="k">Cara pakai di terminal — satu command, pilih nomor:</div>
      <div style="margin-top:6px;"><code>claude-deep</code> <span class="k">→ menu</span> &nbsp; · &nbsp; <code>claude-deep 2</code> <span class="k">→ langsung provider #2</span></div>
    </div>
    <div class="spacer"></div>
    <div class="k">Nomor di kartu = nomor di menu launcher</div>
  </div>

  <div id="profiles" class="grid"></div>
</div>

<!-- Editor modal -->
<div class="overlay" id="overlay" onclick="if(event.target===this)closeEditor()">
  <div class="modal">
    <h2 id="ed_title">Provider Baru</h2>
    <div class="msub">Isi endpoint Anthropic-compatible + API key, lalu Discover model.</div>
    <input type="hidden" id="ed_id">

    <div class="two">
      <div class="field">
        <label>Nama <span style="text-transform:none;color:var(--dim2);">(jadi claude-&lt;nama&gt;)</span></label>
        <input id="ed_name" placeholder="minimax / deepseek / kimi" autocomplete="off">
      </div>
      <div class="field">
        <label>Base URL</label>
        <input id="ed_base_url" placeholder="https://api.x.com/anthropic" autocomplete="off">
      </div>
    </div>

    <div class="inline" style="margin-bottom:13px;">
      <div class="field">
        <label>Auth Token / API Key</label>
        <input id="ed_auth_token" placeholder="sk-..." autocomplete="off">
      </div>
      <button onclick="discoverModels(this)">🔍 Discover</button>
    </div>

    <div id="ed_models" class="hidden">
      <label>Model tersedia — klik buat pasang ke slot</label>
      <input id="ed_filter" placeholder="filter model…" oninput="renderModelList()" style="margin-bottom:6px;">
      <div class="mlist" id="ed_model_list"></div>
    </div>

    <hr class="hr">

    <div class="three">
      <div class="field"><label>Opus slot 🧠</label><input id="ed_opus_model" placeholder="model kuat">
        <div class="two" style="margin-top:5px;"><input id="ed_opus_ctx" type="number" placeholder="context 1000000"><input id="ed_opus_out" type="number" placeholder="max out 128000"></div>
      </div>
      <div class="field"><label>Sonnet slot ⚖️</label><input id="ed_sonnet_model" placeholder="model default">
        <div class="two" style="margin-top:5px;"><input id="ed_sonnet_ctx" type="number" placeholder="context 1000000"><input id="ed_sonnet_out" type="number" placeholder="max out 64000"></div>
      </div>
      <div class="field"><label>Haiku slot ⚡</label><input id="ed_haiku_model" placeholder="model cepat">
        <div class="two" style="margin-top:5px;"><input id="ed_haiku_ctx" type="number" placeholder="context 200000"><input id="ed_haiku_out" type="number" placeholder="max out 64000"></div>
      </div>
    </div>
    <div class="two">
      <div class="field"><label>Extra args</label><input id="ed_extra_args" value="--dangerously-skip-permissions"></div>
      <div class="field"><label>Catatan</label><input id="ed_note" placeholder="opsional"></div>
    </div>

    <div class="foot">
      <button class="primary" onclick="saveProfile()">💾 Simpan</button>
      <button onclick="closeEditor()">Batal</button>
      <div class="spacer"></div>
      <button class="ghost" onclick="previewWrapper()">📄 Preview</button>
    </div>
    <pre class="preview hidden" id="ed_preview"></pre>
  </div>
</div>

<div class="overlay" id="gwOverlay" onclick="if(event.target===this)closeGateway()">
  <div class="editor">
    <h2>🔌 API Gateway</h2>
    <p class="k" style="margin:0 0 12px;">Endpoint buat tool luar (localhost). Model muncul sebagai <code>opus</code>, <code>sonnet</code>, <code>haiku</code> → diteruskan ke provider default.</p>
    <div class="field"><label>Provider default</label>
      <select id="gw_provider" style="width:100%;padding:8px;background:#1a1d23;color:#eee;border:1px solid #333;border-radius:7px;"></select>
    </div>
    <div class="field" style="margin-top:10px;"><label>API Key (kosongkan = tanpa proteksi)</label>
      <input id="gw_apikey" placeholder="bikin key bebas, mis: sk-myhub-xxxx"></div>
    <div class="field" style="margin-top:12px;">
      <label>Cara pakai (base URL + key di tool luar)</label>
      <pre class="preview" id="gw_usage" style="white-space:pre-wrap;"></pre>
    </div>
    <div class="row" style="margin-top:14px;display:flex;gap:8px;">
      <button class="primary" onclick="saveGateway()">💾 Simpan</button>
      <button onclick="closeGateway()">Tutup</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let MODELS = [];           // hasil discover terakhir
let PROFILES = {};         // id -> profile

function esc(s){ return String(s==null?'':s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function toast(msg, err=false){
  const t=document.createElement('div');
  t.className='toast'+(err?' err':''); t.textContent=msg;
  document.body.appendChild(t); setTimeout(()=>t.remove(),3400);
}

async function api(method, path, body){
  const opts={method,headers:{'Content-Type':'application/json'}};
  if(body) opts.body=JSON.stringify(body);
  const r=await fetch(path,opts);
  const txt=await r.text();
  let data; try{data=JSON.parse(txt);}catch{data=txt;}
  if(!r.ok) throw new Error((data&&data.detail)||txt||('HTTP '+r.status));
  return data;
}

async function checkHealth(){
  try{ const h=await api('GET','/api/health');
    $('dot').className='dot up'; $('statusTxt').textContent='online · '+h.profiles+' provider';
  }catch{ $('dot').className='dot down'; $('statusTxt').textContent='offline'; }
}

async function loadProfiles(){
  await checkHealth();
  let data; try{ data=await api('GET','/api/profiles'); }
  catch(e){ toast(e.message,true); return; }
  PROFILES={}; const root=$('profiles'); root.innerHTML='';
  if(!data.length){
    root.innerHTML='<div class="empty"><div class="big">🗂️</div>Belum ada provider.<br>Klik <b>+ Provider Baru</b> buat mulai.</div>';
    return;
  }
  data.forEach((p,i)=>{
    PROFILES[p.id]=p;
    const n=i+1;
    const slot=(lbl,v,ctx,out)=>`<div class="slot"><span class="lbl">${lbl}</span>`+
      (v?`<span class="chip">${esc(v)}</span>`:`<span class="chip empty">—</span>`)+
      (v&&ctx?`<span class="lbl" style="opacity:.6;font-size:10px;">${Number(ctx).toLocaleString()} / ${Number(out||0).toLocaleString()}</span>`:'')+`</div>`;
    const card=document.createElement('div');
    card.className='card';
    card.innerHTML=`
      <div class="top">
        <div style="display:flex;gap:11px;align-items:center;">
          <div class="num">${n}</div>
          <div class="pname"><span class="pre">claude-</span>${esc(p.name)}</div>
        </div>
        ${p.wrapper_exists?'<span class="badge on">applied</span>':'<span class="badge">menu only</span>'}
      </div>
      <div class="url">${esc(p.base_url)}</div>
      <div class="slots">
        ${slot('opus',p.opus_model,p.opus_ctx,p.opus_out)}
        ${slot('sonnet',p.sonnet_model,p.sonnet_ctx,p.sonnet_out)}
        ${slot('haiku',p.haiku_model,p.haiku_ctx,p.haiku_out)}
      </div>
      <div class="tok mono">🔑 ${esc(p.auth_token_masked)}</div>
      <div class="acts">
        <button data-act="test" data-id="${p.id}">🧪 Test</button>
        <button data-act="apply" data-id="${p.id}">⚡ Apply</button>
        <button data-act="edit" data-id="${p.id}">✏️ Edit</button>
        <button class="danger" data-act="del" data-id="${p.id}">🗑️</button>
      </div>
      <div class="testbox" id="test_${p.id}"></div>
      ${p.note?`<div class="note">📝 ${esc(p.note)}</div>`:''}
    `;
    root.appendChild(card);
  });
}

// event delegation (no inline name injection)
$('profiles').addEventListener('click', e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  const id=+b.dataset.id, act=b.dataset.act;
  if(act==='test') testProfile(id,b);
  else if(act==='apply') applyProfile(id);
  else if(act==='edit') openEdit(id);
  else if(act==='del') deleteProfile(id);
});

async function testProfile(id,btn){
  btn.disabled=true; const old=btn.innerHTML; btn.innerHTML='<span class="spin">⏳</span> Test…';
  try{
    const res=await api('POST',`/api/profiles/${id}/test`);
    let html='';
    for(const s of ['opus','sonnet','haiku']){
      const r=res[s]; const cls=r.ok?'ok':'fail'; const ic=r.ok?'✅':'❌';
      html+=`<div class="tr"><span class="lbl">${s}</span>
        <span class="pill ${cls}">${ic} ${r.latency_ms}ms</span>
        <span class="mono" style="color:var(--dim);font-size:11.5px;">${esc((r.model||'-'))} · ${esc(r.detail.slice(0,46))}</span></div>`;
    }
    $(`test_${id}`).innerHTML=html; toast('Test selesai');
  }catch(e){ toast(e.message,true); }
  finally{ btn.disabled=false; btn.innerHTML=old; }
}

async function applyProfile(id){
  try{ const res=await api('POST',`/api/profiles/${id}/apply`);
    toast('✅ Applied: '+res.command); loadProfiles();
  }catch(e){ toast(e.message,true); }
}

async function deleteProfile(id){
  const p=PROFILES[id]; if(!p) return;
  if(!confirm(`Hapus profile "${p.name}"?\nIni juga hapus ~/.local/bin/claude-${p.name}`)) return;
  try{ await api('DELETE',`/api/profiles/${id}`); toast('Dihapus'); loadProfiles(); }
  catch(e){ toast(e.message,true); }
}

/* ---- editor ---- */
function fill(p){
  $('ed_id').value=p.id||'';
  for(const k of ['name','base_url','auth_token','opus_model','sonnet_model','haiku_model','opus_ctx','opus_out','sonnet_ctx','sonnet_out','haiku_ctx','haiku_out','extra_args','note'])
    $('ed_'+k).value=p[k]||'';
  if(!p.extra_args && !p.id) $('ed_extra_args').value='--dangerously-skip-permissions';
  MODELS=[]; $('ed_models').classList.add('hidden'); $('ed_preview').classList.add('hidden');
}
function openNew(){ $('ed_title').textContent='Provider Baru'; fill({}); $('overlay').classList.add('show'); $('ed_name').focus(); }
async function openEdit(id){
  try{ const p=await api('GET',`/api/profiles/${id}`);
    $('ed_title').textContent='Edit · claude-'+p.name; fill(p); $('overlay').classList.add('show');
  }catch(e){ toast(e.message,true); }
}
function closeEditor(){ $('overlay').classList.remove('show'); }
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ closeEditor(); closeGateway(); } });

/* ---- API Gateway ---- */
async function openGateway(){
  try{
    const profs = await api('GET','/api/profiles');
    const gw = await api('GET','/api/gateway');
    const sel = $('gw_provider');
    sel.innerHTML = profs.map(p=>`<option value="${p.name}" ${p.name===gw.default_provider?'selected':''}>${p.name}</option>`).join('');
    $('gw_apikey').value = gw.api_key || '';
    renderGwUsage();
    $('gwOverlay').classList.add('show');
  }catch(e){ toast(e.message,true); }
}
function closeGateway(){ $('gwOverlay').classList.remove('show'); }
function renderGwUsage(){
  const key = $('gw_apikey').value || '(tanpa key)';
  $('gw_usage').textContent =
`Base URL : http://localhost:8765/gw/v1
API Key  : ${key}
Models   : opus, sonnet, haiku

# contoh curl:
curl http://localhost:8765/gw/v1/messages \\
  -H "Authorization: Bearer ${key}" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '{"model":"opus","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'`;
}
async function saveGateway(){
  try{
    await api('POST','/api/gateway',{default_provider:$('gw_provider').value, api_key:$('gw_apikey').value.trim()});
    toast('💾 Gateway tersimpan'); renderGwUsage();
  }catch(e){ toast(e.message,true); }
}

function readForm(){
  const DEF={opus_ctx:1000000,opus_out:128000,sonnet_ctx:1000000,sonnet_out:64000,haiku_ctx:200000,haiku_out:64000};
  const n=(id)=>{const v=parseInt($(id).value,10);return isNaN(v)?DEF[id.replace('ed_','')]:v;};
  return {name:$('ed_name').value.trim(),base_url:$('ed_base_url').value.trim(),
    auth_token:$('ed_auth_token').value.trim(),opus_model:$('ed_opus_model').value.trim(),
    sonnet_model:$('ed_sonnet_model').value.trim(),haiku_model:$('ed_haiku_model').value.trim(),
    opus_ctx:n('ed_opus_ctx'),opus_out:n('ed_opus_out'),
    sonnet_ctx:n('ed_sonnet_ctx'),sonnet_out:n('ed_sonnet_out'),
    haiku_ctx:n('ed_haiku_ctx'),haiku_out:n('ed_haiku_out'),
    extra_args:$('ed_extra_args').value.trim(),note:$('ed_note').value.trim()};
}

async function saveProfile(){
  const b=readForm();
  if(!b.name||!b.base_url||!b.auth_token){ toast('Nama, Base URL, Token wajib diisi',true); return; }
  if(!/^[a-z0-9_-]+$/i.test(b.name)){ toast('Nama cuma boleh huruf/angka/-/_',true); return; }
  try{
    const id=$('ed_id').value;
    if(id) await api('PUT',`/api/profiles/${id}`,b);
    else await api('POST','/api/profiles',b);
    toast('💾 Tersimpan'); closeEditor(); loadProfiles();
  }catch(e){ toast(e.message,true); }
}

async function discoverModels(btn){
  const b=readForm();
  if(!b.base_url||!b.auth_token){ toast('Isi Base URL + Token dulu',true); return; }
  if(!b.name){ toast('Isi nama dulu',true); return; }
  if(!/^[a-z0-9_-]+$/i.test(b.name)){ toast('Nama cuma boleh huruf/angka/-/_',true); return; }
  btn.disabled=true; const old=btn.innerHTML; btn.innerHTML='<span class="spin">🔍</span>';
  try{
    let id=$('ed_id').value;
    if(!id){ const r=await api('POST','/api/profiles',b); id=r.id; $('ed_id').value=id; $('ed_title').textContent='Edit · claude-'+b.name; }
    else await api('PUT',`/api/profiles/${id}`,b);
    const data=await api('POST',`/api/profiles/${id}/discover-models`);
    MODELS=data.models.map(m=>m.id);
    $('ed_models').classList.remove('hidden'); renderModelList();
    toast(`Ketemu ${data.count} model`);
  }catch(e){ toast(e.message,true); }
  finally{ btn.disabled=false; btn.innerHTML=old; }
}

function renderModelList(){
  const q=$('ed_filter').value.toLowerCase();
  const list=$('ed_model_list'); list.innerHTML='';
  const cur={opus:$('ed_opus_model').value,sonnet:$('ed_sonnet_model').value,haiku:$('ed_haiku_model').value};
  MODELS.filter(m=>m.toLowerCase().includes(q)).forEach(m=>{
    const tags=Object.entries(cur).filter(([k,v])=>v===m).map(([k])=>k);
    const row=document.createElement('div'); row.className='mrow';
    row.innerHTML=`<code>${esc(m)}</code>
      ${tags.map(t=>`<span class="pill ok">${t}</span>`).join('')}
      <button data-slot="opus">Opus</button>
      <button data-slot="sonnet">Sonnet</button>
      <button data-slot="haiku">Haiku</button>`;
    row.querySelectorAll('button[data-slot]').forEach(bt=>{
      bt.onclick=()=>{ $('ed_'+bt.dataset.slot+'_model').value=m; toast(bt.dataset.slot+' → '+m); renderModelList(); };
    });
    list.appendChild(row);
  });
  if(!list.children.length) list.innerHTML='<div style="padding:8px;color:var(--dim2);">tidak ada yang cocok</div>';
}

async function previewWrapper(){
  const id=$('ed_id').value;
  if(!id){ toast('Simpan dulu buat preview',true); return; }
  try{ const r=await fetch(`/api/profiles/${id}/preview`); const t=await r.text();
    $('ed_preview').textContent=t; $('ed_preview').classList.remove('hidden');
  }catch(e){ toast(e.message,true); }
}

loadProfiles();
setInterval(checkHealth, 15000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
