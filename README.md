# Claude Hub

**One dashboard to route Claude Code through any AI provider — while keeping the native `/model` menu and auto-compact working.**

Claude Hub is a tiny local server (one Python file) that sits between Claude Code and your AI providers (Kiro, DeepSeek, MiniMax, any Anthropic/OpenAI-compatible endpoint). It does three jobs at once:

1. **Web dashboard** to manage providers (add / edit / discover models / set context limits).
2. **Translation proxy** so Claude Code always speaks canonical `opus` / `sonnet` / `haiku` — which is what keeps **auto-compact** and the clean `/model` menu working, no matter which provider is behind it.
3. **API gateway** — a single localhost endpoint (`/gw/v1`) with an API key, exposing `opus` / `sonnet` / `haiku` to any external tool.

---

## Why this exists

Claude Code decides a model's **context window** (and therefore *when to auto-compact*) from the model **name**. If a provider uses a non-canonical name like `deepseek-v4-pro`, `MiniMax-M3`, or `kiro-claude-opus-4.8`, Claude Code doesn't recognise it → auto-compact breaks and the `/model` menu fills up with duplicate "Custom" entries.

Claude Hub fixes this: Claude Code always sends `claude-opus-4-8` / `claude-sonnet-4-6` / `claude-haiku-4-5`, the hub rewrites that to the **active provider's real model name**, and forwards the request. Claude Code stays happy; you get any backend you want.

```
                ┌──────────────── Claude Hub (port 8765) ────────────────┐
  Browser  ───▶ │  Web UI: manage providers, discover models, settings   │
               │                                                          │
  claude-*  ──▶ │  /p/<provider>/v1   → translate opus→provider model     │ ──▶ Provider API
               │                        (auto-compact + clean /model)     │     (DeepSeek, Kiro,
  Ext tool ──▶ │  /gw/v1  (+ API key) → opus/sonnet/haiku gateway         │      MiniMax, …)
               └──────────────────────────────────────────────────────────┘
```

---

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/zesbe/claude-hub/main/install.sh | bash
```

Or clone and run:

```bash
git clone https://github.com/zesbe/claude-hub.git
cd claude-hub
./install.sh
```

The installer:
- drops the server into `~/.claude-hub/`
- installs the `claude-deep` launcher into `~/.local/bin/`
- installs Python deps (uses a venv automatically if your system blocks `pip --user`)
- sets up a **systemd `--user` service** with `Restart=always` + lingering, so the hub **auto-starts on boot and auto-restarts on crash**

**Requirements:** `python3` (3.10+), and `sqlite3` CLI (optional — only needed for the `claude-deep` menu).

---

## Quick start

1. Open the dashboard: **http://localhost:8765/**
2. Click **+ Provider Baru**, fill in:
   - **Name** — e.g. `deepseek` (becomes the command `claude-deepseek`)
   - **Base URL** — e.g. `https://api.deepseek.com/anthropic`
   - **Auth token** — your provider API key
   - Click **🔍 Discover** to auto-load the provider's model list, then click **Opus / Sonnet / Haiku** on a model to assign it to a slot
   - Optionally set **context window** + **max output** per slot
3. Click **⚡ Apply** → generates `~/.local/bin/claude-<name>`
4. In a terminal:

```bash
claude-deepseek          # launch Claude Code routed through DeepSeek
# or
claude-deep              # menu to pick any provider
claude-deep 2            # jump straight to provider #2
```

Open `/model` inside Claude Code — you'll see the clean native menu (Opus / Sonnet / Haiku) and auto-compact works.

---

## How model slots map

Claude Code only ever sends three logical models. The hub maps each to a per-provider model:

| Claude Code sends | Hub slot | You configure → e.g. DeepSeek |
|-------------------|----------|-------------------------------|
| `claude-opus-4-8`   | **opus**   | `deepseek-v4-pro`   |
| `claude-sonnet-4-6` | **sonnet** | `deepseek-v4-flash` |
| `claude-haiku-4-5`  | **haiku**  | `deepseek-v4-flash` |

> The base Opus model is treated as **1M context** by Claude Code automatically — auto-compact triggers near the model's window. Sonnet/Haiku use the standard window. This is native Claude Code behaviour; the hub doesn't override it.

---

## API Gateway (for external tools)

Want to use your providers from *any* tool (not just Claude Code)? Use the gateway.

1. Dashboard → **🔌 API**
2. Pick a **default provider** and set an **API key**
3. Point your tool at:

```
Base URL : http://localhost:8765/gw/v1
API Key  : <your key>
Models   : opus, sonnet, haiku
```

Example:

```bash
curl http://localhost:8765/gw/v1/messages \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"opus","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

- `GET /gw/v1/models` advertises `opus`, `sonnet`, `haiku`.
- Requests are translated to the **default provider's** real model and forwarded.
- The gateway binds to **127.0.0.1 only** (localhost). To reach it from another machine, put it behind a tunnel (e.g. Cloudflare Tunnel) — don't expose the port directly.

---

## Endpoints reference

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | Web dashboard |
| `GET`  | `/api/health` | Health check |
| `GET`  | `/api/profiles` | List providers |
| `POST` | `/api/profiles` | Create provider |
| `PUT`  | `/api/profiles/{id}` | Update provider |
| `DELETE` | `/api/profiles/{id}` | Delete provider |
| `POST` | `/api/profiles/{id}/apply` | Generate `claude-<name>` wrapper |
| `POST` | `/api/profiles/{id}/discover-models` | Auto-load model list |
| `POST` | `/api/profiles/{id}/test` | Ping each slot |
| `GET`/`POST` | `/api/gateway` | Get/set gateway (default provider + API key) |
| `GET`  | `/p/{provider}/v1/models` | Empty list (Claude Code uses built-ins) |
| `POST` | `/p/{provider}/v1/{path}` | Per-provider proxy (used by wrappers) |
| `GET`  | `/gw/v1/models` | opus/sonnet/haiku (for external tools) |
| `POST` | `/gw/v1/{path}` | Gateway proxy (API-key protected) |

---

## Managing the service

```bash
systemctl --user status claude-hub      # status
systemctl --user restart claude-hub     # restart
systemctl --user stop claude-hub        # stop
journalctl --user -u claude-hub -f      # live logs
```

Config & data live in `~/.claude-hub/`:
- `server.py` — the hub
- `profiles.db` — your providers (**contains secrets, never commit**)
- `gateway.json` — gateway api key + default provider (**secret**)

---

## Security notes

- The hub binds to **127.0.0.1** only — nothing is exposed to the network by default.
- `profiles.db` and `gateway.json` hold your provider API keys. They are **git-ignored**; never commit them.
- For remote access, use a tunnel (Cloudflare Tunnel / SSH) rather than opening the port.

---

## Uninstall

```bash
systemctl --user disable --now claude-hub
rm -f ~/.config/systemd/user/claude-hub.service
systemctl --user daemon-reload
rm -rf ~/.claude-hub
rm -f ~/.local/bin/claude-deep ~/.local/bin/claude-*   # remove generated wrappers
```

---

## License

MIT — see [LICENSE](LICENSE).
