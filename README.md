# Sentinel Platform

[![CI](https://github.com/mhm-hossein/sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/mhm-hossein/sentinel/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A production-ready, **multi-user · multi-instance Telegram automation platform**
built with Python 3.12+, Telethon, asyncio and SQLite. Users onboard through a
Telegram **control bot**, subscribe to a **plan**, and run one or more isolated
**selfbot instances** — each a separate Telegram account driven by the full
Sentinel feature stack in its own worker process.

> ⚠️ **Selfbots automate a personal Telegram account, which is against Telegram's
> Terms of Service and can lead to limits or bans.** Use dedicated accounts, keep
> intervals reasonable, and operate at your own risk.

---

## Install (one command)

On a fresh **Debian/Ubuntu** VPS as root:

```bash
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo bash
```

The installer asks for your 4 secrets (API id/hash, control-bot token, owner id),
then sets up a systemd service and verifies the platform actually booted. It is
**idempotent** — re-run it to update. Re-running with a populated `.env` keeps
your secrets and only pulls the new code.

Have the credentials ready first:

| Value | Where to get it |
| --- | --- |
| `SENTINEL_API_ID` / `SENTINEL_API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `CONTROL_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `SENTINEL_OWNER_ID` | [@userinfobot](https://t.me/userinfobot) |

Fully unattended (CI/Ansible) — export the variables and fail instead of prompting:

```bash
export SENTINEL_API_ID=123456 SENTINEL_API_HASH=abcdef... \
       CONTROL_BOT_TOKEN=12345:AA... SENTINEL_OWNER_ID=123456789 \
       SENTINEL_NONINTERACTIVE=1
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo -E bash
```

Installer options (env vars): `SENTINEL_INSTALL_DIR` (default `/opt/sentinel`),
`SENTINEL_BRANCH`, `SENTINEL_SERVICE_USER`, `SENTINEL_SERVICE_NAME`,
`SENTINEL_PYTHON`.

After it finishes: **DM your control bot → `/start`** and run the registration
wizard (phone → OTP → 2FA). Your owner id gets the admin panel automatically.

```bash
systemctl status sentinel      # is it up?
journalctl -u sentinel -f      # live logs
```

Prefer doing it by hand, or not on Debian/Ubuntu? See
[Deployment (manual)](#deployment-manual).

---

## Overview

Sentinel is split into a **control plane** (the platform: onboarding, billing by
plan, supervision, monitoring) and a **data plane** (the selfbots themselves).

- **Telegram Control Bot** — a Bot-API bot (inline keyboards only) that onboards
  users (phone → OTP → 2FA), manages their instances, Gemini keys, plan requests
  and support tickets, and exposes a full admin panel.
- **Multi-user architecture** — every person who talks to the control bot is a
  *panel user* with their own account, plan, subscription and settings, stored in
  a single **central database** (`data/central.db`).
- **Multi-instance architecture** — one user can own **N selfbot instances**
  (subject to their plan's `max_instances`), each a separately-authorized
  Telegram account.
- **Worker model** — the **supervisor** runs exactly **one OS process per
  running instance** (`python -m sentinel.worker --instance-id <id>`), spawning,
  restarting (with backoff) and stopping them as the desired state changes.
- **Central database** — cross-tenant control-plane state: users, instances,
  plans, features, subscriptions, requests, admins/roles/permissions, Gemini
  keys, support tickets, audit logs and alert state.
- **Per-instance database isolation** — every instance has its **own**
  `sentinel.db`, session file and storage directories, so tenants never share
  data.

### Architecture diagram

```
        Telegram User
             │
             ▼
        Control Bot            ← onboarding, panels, admin (inline keyboards)
             │
             ▼
      Central Database         ← users, instances, plans, features, subs, RBAC…
             │
             ▼
         Supervisor            ← reconciles desired vs actual; 1 process / instance
             │
             ▼
      Worker Processes         ← python -m sentinel.worker --instance-id N
             │
             ▼
   Sentinel Selfbot Instances  ← the feature modules run here, per instance
```

---

## System architecture

| Layer | What it is | Key modules |
| --- | --- | --- |
| **Control Plane** | The platform process (`python start.py`) | `sentinel/platform/*`, `start.py` |
| **Data Plane** | The per-instance selfbot runtime | `sentinel/worker.py`, `sentinel/features.py` |
| **CentralDatabase** | One SQLite file for all cross-tenant state | `core/central.py` |
| **Feature Registry** | Dynamic catalog of capabilities (auto-registered) | `sentinel/registry.py` |
| **Subscription System** | Plans, requests, subscriptions, expiry | `core/central.py`, `sentinel/platform/subscription_checker.py` |
| **FeatureGate** | The single DB-driven permission check | `core/feature_gate.py` |
| **RBAC** | Roles + permissions for admins; owner bypass | `core/central.py` |
| **Monitoring** | Platform overview + per-instance metrics | `sentinel/monitoring.py`, `sentinel/usage.py` |
| **Support System** | Tickets + threaded messages (survive restart) | `core/central.py`, control bot |
| **Audit Log** | Every admin action recorded | `core/central.py`, control bot |

### Control plane vs. data plane

- The **control plane** never touches a user's selfbot directly. It records the
  *desired* state in the central DB (`users.status`, `instances.enabled`,
  `restart_requested`); the **supervisor** reconciles that against the *actual*
  state (`instances.status`, heartbeats) and spawns/stops worker processes.
- The **data plane** is the original single-user Sentinel star topology,
  unchanged: `setup_features(ctx)` wires the feature modules for one
  `UserContext`. Modules depend only on `core` and the Telethon client — never on
  each other.

### Feature Registry & FeatureGate (no hardcoded permissions)

Every capability is a **feature** row (`banner`, `ai`, `archive`, …). Plans are
linked to features through the `plan_features` many-to-many table. Access is
**always** resolved from the database:

```
subscription → plan → plan_features → feature
```

`FeatureGate.has_feature(telegram_user_id, "ghost")` is the single runtime check.
A plan's enabled modules come from `plan_features` (`resolve_plan`), so **adding a
new module = registering one feature spec** — it then appears in the admin panel
and can be attached to any plan with no code changes.

### RBAC

Admins have a **role** (`owner`, `admin`, `support` by default — editable) and
each role grants **permission keys** (`users.manage`, `plans.manage`,
`features.manage`, `requests.manage`, `admins.manage`, `broadcast`, `stats.view`,
`monitoring`, `instance_control`, `ticket_management`, `audit_view`). The
**owner** (`SENTINEL_OWNER_ID`) bypasses all checks. Every admin panel action is
permission-gated and written to the **audit log**.

---

## Features

All feature modules run **inside each selfbot instance** (the data plane) and are
**plan-gated**: a module is only wired for an instance if its owner's plan
includes that feature (via `plan_features`). Default command prefix is `.`
(per-user configurable).

| Module | Purpose | Commands | Plan gating |
| --- | --- | --- | --- |
| **Banner** | Re-post a message on a repeating interval (forward/copy) | `.banner <time>` · `copy` · `list` · `info` · `edit` · `pause` · `resume` · `stop` · `send` · `del` · `stopall` · `delall` | `banner` + `max_banners` limit |
| **AI** | Google Gemini assistant (Q&A, vision, docs, translate) with fallback chain | `.ai <q>` · `summarize`/`explain`/`describe`/`review`/`translate` · `model` · `trust` | `ai` + per-user Gemini keys + `ai_requests_per_day` |
| **Variables** | Reusable text aliases | `.var add/edit/remove/show/list` · `.<keyword>` | `variables` |
| **Save** | Copy protected/restricted media into Saved Messages | `.save` · `.save <n>` | `save` |
| **Info** | Rich report on a user/group/channel | `.info` · `.info me` | `info` |
| **Archive** | Mirror selected chats incrementally (watermark + dedup) | `.archive add/remove/list/sync/info/status/export` | `archive` + `archive_chats_max` |
| **Export** | Live chat export → TXT/JSON → Saved Messages | `.export [text|media] [target]` | `export` |
| **Search** | Live global Telegram search | `.search <q>` · `limit` · `media` · `export` | `search` |
| **Ghost Tracker** | Watch users' profiles; report every change | `.ghost add/remove/list/status/info/export` | `ghost` + `ghost_targets_max` |
| **Username Hunter** | Monitor usernames; auto-claim when freed | `.hunter add/remove/list/status/check/export` | `hunter` + `hunter_targets_max` |
| **Tracking** | Autosave view-once, anti-delete, edit tracker | `.autosave` · `.antidel` · `.antiedit` | `tracking` |
| **Logger** | Log selected chats to SQLite + export | `.logger start/stop/status/export` | `logger` |
| **System** | VPS monitor (CPU/RAM/disk/net/uptime/ping) | `.vps [cpu|ram|disk|ping|services]` | `system` |
| **Backup** | Zip DB + configs + sessions → Saved Messages | `.backup create/list/restore` | `backup` |

**Global commands (always available in an instance):** `.help`, `.status`,
`.stats`.

> **Notes:** AI keys, models and rate limits come from the user's plan + their
> own Gemini keys (never a global key). Archive/Ghost/Hunter enforce their
> per-plan `*_max` limits at add-time. Any module can be enabled or disabled for a
> plan from the admin panel without touching code.

---

## Multi-instance

One user owns many instances; each is fully isolated:

```
        Owner User (telegram_user_id)
             │
     ┌───────┼───────┐
     ▼       ▼       ▼
 Instance 1  Instance 2  Instance 3   (each a separate Telegram account)
```

### Storage layout

```
data/
├── central.db                              # control-plane database
└── users/<telegram_user_id>/
    └── instances/<instance_id>/
        ├── session.session                 # this instance's authorized Telethon session
        ├── sentinel.db                     # this instance's feature-module database
        ├── exports/                        # export / search / archive / ghost / hunter dumps
        ├── backups/                        # backup zips
        └── cache/                          # tracking autosave + transient media
```

A worker also writes a `worker.log` under its instance directory (tailed by the
admin “View Logs” action).

---

## Subscriptions

- **Plans** (`plans`) — editable bundles with a `name`, `description`,
  `max_instances`, an `active` flag and a JSON `limits` map. Seeded defaults:
  **Bronze** (1 instance), **Silver** (2), **Gold** (unlimited) — all editable
  from the admin panel; nothing hardcodes a tier at runtime.
- **Features** (`features`) — the capability catalog, auto-created at startup by
  the **Feature Registry**.
- **plan_features** — the many-to-many mapping that *is* a plan's feature set.
- **FeatureGate** — resolves access `subscription → plan → features` at runtime.
- **Subscription requests** (`subscription_requests`) — a user taps **📦 Plans →
  Request**, an admin approves with a duration (30 / 60 / 90 / 180 / 365 / custom
  days), which creates a **subscription** (`subscriptions`) that activates
  immediately.
- **Expiration** — the **Subscription Checker** runs on an interval, marks
  past-due subscriptions `expired`, alerts the owner, and (optionally, with
  `SENTINEL_STOP_ON_EXPIRE=1`) disables the user's instances so the supervisor
  stops their workers. Once expired, `FeatureGate` blocks premium features and
  new-instance creation.

---

## Admin panel

Accessed from the control bot by admins (owner + anyone in `admins`). Every
action is permission-gated and audited.

| Panel | Function | Permission |
| --- | --- | --- |
| **📊 Platform Overview** | Live dashboard: users, active/expired subs, active/stopped workers, instances, Gemini keys, databases, storage, avg worker memory/uptime | `monitoring` |
| **👥 Users** | List → user → **instances**; view, suspend/unsuspend, extend subscription, change plan; per-instance live monitor + remote control (▶/⏹/🔄/📦 backup/📤 export DB/📋 logs) | `users.manage` / `instance_control` |
| **📦 Plans** | Create, rename, set `max_instances`, toggle active, delete, attach/detach features — all via inline buttons (no SQL) | `plans.manage` |
| **⚙ Features** | List registered features | `features.manage` |
| **📝 Requests** | Review pending upgrade requests; approve with a duration, or reject | `requests.manage` |
| **👑 Admins** | Add/remove admins, assign roles (owner-only) | `admins.manage` |
| **📊 Statistics** | Platform counts | `stats.view` |
| **🎫 Support Center** | Open/closed tickets, reply, close, reopen, assign | `ticket_management` |
| **📜 Audit Logs** | Every admin action, filterable | `audit_view` |
| **📢 Broadcast** | Message every user's control chat | `broadcast` |

---

## User panel

What a normal user sees when they DM the control bot.

### Registration wizard

```
/start
  → 🛰 Welcome
  → 📜 Terms of Service  → ✅ Accept
  → 📱 Phone number
  → 💬 OTP code
  → 🔐 2FA password (only if the account has one)
  → ✅ Session created
  → 🤖 Worker started
```

The wizard is **persisted** (`bot_state`), so it survives a control-plane
restart. **OTP codes, 2FA passwords and API keys are never logged and never
stored** — the message carrying such a secret is deleted after it is read.

### Panel

- **My Account / My Plan** — plan, expiration date, features included, instances
  used vs. allowed.
- **🤖 My Selfbot** — list instances with **▶ Start / ⏹ Stop / 🔄 Restart / 📋
  Status** and **➕ Add Instance** (gated by `max_instances`).
- **🔑 Gemini Keys** — add / list active / list disabled / enable / disable /
  test (real probe) / delete. Key count is limited by the plan; keys are never
  auto-deleted.
- **📦 Plans** — browse plans and **request an upgrade** (creates a subscription
  request for admin approval).
- **🎫 Support** — create / view / close tickets.
- **⚙ Settings** — change the selfbot command prefix.

---

## Environment variables

All variables actually read by the code (verified by source search). Copy
`.env.example` to `.env` and fill it in. Secrets are git-ignored.

### Platform (control plane) — required

| Variable | Purpose |
| --- | --- |
| `SENTINEL_API_ID` | Telegram API id (from https://my.telegram.org) — authorizes all user logins |
| `SENTINEL_API_HASH` | Telegram API hash |
| `CONTROL_BOT_TOKEN` | Bot token from @BotFather (the control bot) |
| `SENTINEL_OWNER_ID` | Owner Telegram id — full admin access, receives alerts |

### Platform — optional

| Variable | Default | Purpose |
| --- | --- | --- |
| `SENTINEL_ADMIN_IDS` | — | Comma-separated extra admin ids seeded at boot |
| `SENTINEL_DATA_ROOT` | `data` | Root for `central.db` and per-user storage |
| `SENTINEL_CENTRAL_DB` | `<data_root>/central.db` | Explicit central DB path |
| `SENTINEL_DEFAULT_PLAN` | `bronze` | Plan assigned to new users |
| `SENTINEL_MAX_WORKERS` | `0` (∞) | Cap on concurrent worker processes |
| `SENTINEL_RECONCILE_SECONDS` | `10` | Supervisor reconcile cadence |
| `SENTINEL_HEARTBEAT_SECONDS` | `30` | Worker heartbeat cadence |
| `SENTINEL_SUBCHECK_SECONDS` | `300` | Subscription-expiry check cadence |
| `SENTINEL_STOP_ON_EXPIRE` | `0` | If `1`, stop a user's workers on expiry |
| `SENTINEL_ALERT_SECONDS` | `120` | Alert health-loop cadence |
| `SENTINEL_ALERT_DISK_FREE_PCT` | `10` | Low-disk alert threshold (% free) |
| `SENTINEL_ALERT_RAM_PCT` | `90` | High-RAM alert threshold (% used) |
| `SENTINEL_ALERT_GEMINI_FAILS` | `20` | Gemini failure-count alert threshold |
| `SENTINEL_SUPPORT_CONTACT` | `@your_support` | Shown on the Support screen |
| `SENTINEL_TOS_URL` | — | Optional full Terms-of-Service URL |

### Instance runtime & feature modules

| Variable | Default | Purpose |
| --- | --- | --- |
| `BANNER_PREFIX` | `.` | Command prefix (per-user overridable) |
| `BANNER_LOG_LEVEL` | `INFO` | Log level |
| `DEFAULT_AI_MODEL` | `gemini-2.5-flash` | Default Gemini model |
| `AI_MODELS` | — | Comma-separated extra selectable models |
| `AI_FALLBACK_MODELS` | `gemini-2.0-flash,gemini-2.0-flash-lite` | Fallback chain |
| `MAX_FILE_SIZE_MB` | `20` | Max file size for AI / tracking media |
| `HUNTER_CLAIM_TARGET` | `channel` | Reserve entity type: `channel` or `group` |
| `GHOST_CHECK_INTERVAL_MINUTES` | `30` | Ghost profile-scan cadence |
| `HUNTER_CHECK_INTERVAL_SECONDS` | `60` | Hunter availability-check cadence |
| `ARCHIVE_INITIAL_MESSAGES` | `1000` | Backfill size when a chat is archived |
| `ARCHIVE_SYNC_INTERVAL_MINUTES` | `30` | Archive sync cadence |
| `PING_HOST` | `8.8.8.8` | VPS-monitor latency target |
| `EXPORTS_PATH` / `BACKUP_PATH` | per-instance dirs | Overridden per instance at runtime |

### Development / single-user mode only

| Variable | Purpose |
| --- | --- |
| `BANNER_API_ID` / `BANNER_API_HASH` | Fallback API credentials for `run_local` |
| `BANNER_PHONE` | Phone for the interactive dev login |
| `BANNER_SESSION` | Dev session name |
| `GEMINI_API_KEY` | Single Gemini key for dev mode |
| `SENTINEL_DB_PATH` / `BANNER_DB_PATH` | Legacy single-file DB path (dev) |

---

## Deployment (Ubuntu)

### One command (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/mhm-hossein/sentinel/main/install.sh | sudo bash
```

Handles packages, dedicated user, venv, `.env`, systemd unit, and boot
verification. Details and options: [Install (one command)](#install-one-command).

### Manual

```bash
# 1. System packages (Python 3.12+)
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2. Get the code
git clone https://github.com/mhm-hossein/sentinel.git sentinel && cd sentinel

# 3. Virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 4. Dependencies
pip install -r requirements.txt

# 5. Configure
cp .env.example .env
#   set SENTINEL_API_ID, SENTINEL_API_HASH, CONTROL_BOT_TOKEN, SENTINEL_OWNER_ID

# 6. Start the platform (creates data/central.db on first run)
python start.py
```

**Database initialization** is automatic: on first `python start.py`, the central
database is created, the feature registry and RBAC roles are seeded, default
plan→feature mappings are applied, and the owner account is bootstrapped. Startup
prints a diagnostic checklist and then `🛰 Platform Ready`.

### Running as a service (systemd)

```ini
# /etc/systemd/system/sentinel.service
[Unit]
Description=Sentinel Platform
After=network-online.target

[Service]
WorkingDirectory=/opt/sentinel
ExecStart=/opt/sentinel/.venv/bin/python start.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now sentinel
journalctl -u sentinel -f          # live logs
```

### Monitoring

- In-Telegram: the admin **📊 Platform Overview** and per-instance monitor.
- Automatic **owner alerts** for worker crashes, restart loops, backup failures,
  subscription expirations, low disk, high RAM and Gemini failure spikes
  (rate-limited).

### Backup

- Per instance: **📦 Backup** (user or admin) zips the instance DB + configs +
  session → Saved Messages / owner.
- Whole platform: back up `data/central.db` (WAL — copy `*.db`, `*.db-wal`,
  `*.db-shm`) and the `data/users/` tree.

### Upgrade process

```bash
cd /opt/sentinel
sudo systemctl stop sentinel
git pull
source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl start sentinel
```

Database migrations are **additive and automatic** — new columns/tables are
created idempotently on startup (`_ensure_columns`), and no existing table is
ever removed, so upgrades never lose data.

---

## Development mode (single-user)

For debugging the feature modules against a single account **without** the
control plane, use the preserved single-user harness:

```bash
python -m sentinel.run_local        # or: python -m sentinel.run_local
```

`sentinel/run_local.py` logs in interactively (`BANNER_API_ID`/`BANNER_API_HASH`,
`BANNER_PHONE`) once, builds a single `UserContext` with an *unlimited* plan, and
runs the full feature stack inline under `data/users/<id>/`. This is the fastest
way to verify command behavior; it does **not** start the supervisor, control bot
or subscription system.

> `python start.py` is the **production** entry point (full control plane).
> `python -m sentinel.run_local` is the **development** entry point (one account).

### Testing / audit harness

A self-contained audit harness exercises the whole platform (feature matrix,
onboarding, worker lifecycle, subscriptions, FeatureGate tiers, DB audit,
security/RBAC, and an N+1 performance check) against isolated temporary
databases — it never touches `data/`:

```bash
PYTHONUTF8=1 python tests/audit_harness.py
```

It prints a per-check `[PASS]/[FAIL]` report and exits non-zero on any failure.
Deployment artifacts (systemd unit + ops runbook) live in `deploy/`.

---

## Project layout

```
sentinel/
├── start.py                    # ← production entry point (control plane)
├── core/                       # shared infra + control-plane data model
│   ├── central.py              # CentralDatabase (all cross-tenant tables)
│   ├── feature_gate.py         # FeatureGate.has_feature (DB-driven)
│   ├── plan.py                 # Plan model + limit helpers
│   ├── migrations.py / store.py / stats.py / commands.py
├── sentinel/
│   ├── context.py              # UserContext (per-instance injection seam)
│   ├── paths.py                # UserPaths / InstancePaths (storage layout)
│   ├── features.py             # setup_features(ctx) — wires the data plane
│   ├── runtime.py              # serve(ctx) run loop
│   ├── registry.py             # feature catalog + bootstrap_registry
│   ├── monitoring.py / usage.py# dashboard + per-instance metrics (read-only)
│   ├── worker.py               # per-instance worker process
│   ├── run_local.py            # single-user dev harness
│   └── platform/
│       ├── __main__.py         # `python -m sentinel.platform` → start.main
│       ├── supervisor.py       # spawns/reconciles worker processes
│       ├── control_bot.py      # onboarding + user/admin panels (inline)
│       ├── subscription_checker.py  # expiry loop
│       └── alerts.py           # owner alerts + health loop
├── banner/ ai/ variables/ save/ info/ archive/ export/ search/
├── ghost/ hunter/ tracking/ logger/ system/ backup/   # feature modules
├── requirements.txt
└── .env.example
```

---

## License / disclaimer

Provided as-is for educational and authorized use. Automating user accounts may
violate Telegram's Terms of Service; you are responsible for how you deploy and
use this software.
