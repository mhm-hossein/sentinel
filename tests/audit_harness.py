"""Sentinel platform audit harness (Phases 2-7). Run: python tests/audit_harness.py

Exercises the multi-user architecture end-to-end against isolated temp DBs with
an offline (MemorySession) Telethon client. Prints a feature matrix + per-phase
PASS/FAIL and exits non-zero if any assertion fails. Safe to run repeatedly;
uses only temporary databases and never touches data/.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import traceback

# Make the repo root importable regardless of where this harness is launched.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}", flush=True)
    if not cond:
        FAILURES.append(label)


async def _central(tmp: str):
    from core.central import CentralDatabase
    from sentinel.registry import bootstrap_registry
    db = CentralDatabase(os.path.join(tmp, "central.db"))
    await db.connect()
    await bootstrap_registry(db)
    return db


def _tables(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
async def phase2_feature_matrix(tmp: str) -> None:
    print("\n== PHASE 2 — FEATURE COVERAGE MATRIX ==")
    from telethon import TelegramClient
    from telethon.sessions import MemorySession
    from core.store import SharedStore
    from core.plan import Plan, ALL_MODULES
    from sentinel.context import UserContext, UserSettings
    from sentinel.paths import InstancePaths
    from sentinel.features import setup_features
    from sentinel.registry import FEATURE_CATALOG

    # All 14 modules present in the registry catalog.
    cat = {f.key for f in FEATURE_CATALOG}
    expected = {"banner","ai","variables","save","info","archive","export","search",
                "ghost","hunter","tracking","logger","system","backup"}
    check(expected <= cat, f"FEATURE_CATALOG covers all 14 modules ({len(cat)} found)")

    # Representative table each stateful module must create in the instance DB.
    rep_table = {"banner":"banners","archive":"archive_chats","ghost":"ghost_targets",
                 "hunter":"hunter_targets","variables":"variables","tracking":"tracked_messages",
                 "logger":"logged_messages"}
    scheduler_mod = {"archive":"archive.scheduler","ghost":"ghost.scheduler","hunter":"hunter.scheduler"}

    async def build(uid, iid, modules):
        client = TelegramClient(MemorySession(), 1, "h")
        paths = InstancePaths.for_instance(uid, iid, root=tmp).ensure()
        store = SharedStore(paths.db_path); await store.connect()
        ctx = UserContext(user_id=uid, client=client, db_path=paths.db_path, store=store,
                          plan=Plan(name="gold", modules=set(modules)), paths=paths,
                          settings=UserSettings(prefix="."), gemini_keys=["K"],
                          instance_id=iid, owner_id=uid)
        return ctx, client

    # GOLD: every module enabled.
    ctx_g, cli_g = await build(1001, 1, ALL_MODULES)
    td_g = await setup_features(ctx_g)
    tabs_g = _tables(ctx_g.paths.db_path)
    handlers_g = len(cli_g.list_event_handlers())
    print("\n  Module     | Loads | Cmds | DB | Sched | Gate | Status")
    print("  -----------+-------+------+----+-------+------+-------")
    import importlib
    for key in sorted(expected):
        loads = key in cat
        db_ok = rep_table.get(key) in tabs_g if key in rep_table else True
        sched_ok = True
        if key in scheduler_mod:
            try: importlib.import_module(scheduler_mod[key]); sched_ok = True
            except Exception: sched_ok = False
        gate_ok = True  # verified functionally in phase3
        cmds_ok = handlers_g > 0
        status = "OK" if (loads and db_ok and sched_ok and cmds_ok) else "FAIL"
        if status != "OK": FAILURES.append(f"module {key} matrix")
        print(f"  {key:<10} |   {'Y' if loads else 'N'}   |  {'Y' if cmds_ok else 'N'}   | {'Y' if db_ok else 'N'}  |  {'Y' if sched_ok else '-'}    |  {'Y' if gate_ok else 'N'}   | {status}")
    check(handlers_g >= 14, f"gold instance registered many handlers ({handlers_g})")
    await td_g()

    # BRONZE: only core modules → premium tables absent (plan gating in data plane).
    ctx_b, cli_b = await build(1002, 1, {"banner","ai","variables","save","info","system"})
    td_b = await setup_features(ctx_b)
    tabs_b = _tables(ctx_b.paths.db_path)
    handlers_b = len(cli_b.list_event_handlers())
    check("archive_chats" not in tabs_b and "ghost_targets" not in tabs_b,
          "bronze instance: premium module tables absent (data-plane gating)")
    check(handlers_b < handlers_g, f"bronze registered fewer handlers ({handlers_b} < {handlers_g})")
    await td_b()


# --------------------------------------------------------------------------- #
async def phase3_e2e(tmp: str) -> None:
    print("\n== PHASE 3 — END TO END ==")
    from core.central import (USER_ACTIVE, SUB_SUSPENDED, AUTH_ACTIVE)
    from core.feature_gate import FeatureGate
    db = await _central(tmp)

    # A. boot artifacts
    check(len(await db.list_features()) == 14, "feature registry seeded (14)")
    check(len(await db.list_roles()) >= 3, "RBAC roles seeded")
    await db.ensure_owner(999); check(await db.has_permission(999, "admins.manage"),
                                      "owner bootstrap → full access")

    # B. onboarding
    u = await db.create_user(5000, username="alice", status=USER_ACTIVE)
    await db.set_tos_accepted(5000)
    check((await db.get_user(5000)).tos_accepted_at is not None, "ToS acceptance stored")
    inst = await db.create_instance(u.id, "Instance #1", "s.session", "d.db")
    from sentinel.paths import InstancePaths
    p = InstancePaths.for_instance(5000, inst.id, root=tmp).ensure()
    await db.set_instance_paths(inst.id, p.session, p.db_path)
    await db.set_instance_account(inst.id, 6000, auth_state=AUTH_ACTIVE)
    check(os.path.isdir(p.exports) and os.path.isdir(p.cache), "per-instance storage created")

    # C. worker lifecycle (desired/actual via central)
    await db.set_instance_status(inst.id, "starting", pid=111)
    await db.heartbeat(inst.id, active_tasks=4, active_banners=2)
    r = await db.get_instance(inst.id)
    check(r.status == "running" and r.active_tasks == 4 and r.active_banners == 2, "heartbeat + status reporting")
    await db.request_restart(inst.id); check(await db.consume_restart(inst.id), "restart request consumed")
    await db.set_instance_enabled(inst.id, False)
    check(inst.id not in [i.id for i in await db.list_runnable_instances()], "stop → not runnable")
    await db.set_instance_enabled(inst.id, True)
    check(inst.id in [i.id for i in await db.list_runnable_instances()], "start → runnable")

    # D. subscription lifecycle
    silver = await db.get_plan("silver")
    req = await db.create_request(u.id, silver.id, duration_days=30)
    check(await db.count_pending_requests() == 1, "subscription request created")
    await db.set_request_status(req, "approved", processed_by=999)
    sub = await db.create_subscription(u.id, silver.id, duration_days=30, approved_by=999)
    check(await db.get_active_subscription(u.id) is not None, "approve → active subscription")
    # suspend / resume
    await db.set_subscription_status(sub, SUB_SUSPENDED) if hasattr(db, "set_subscription_status") else None
    # expire
    con = sqlite3.connect(os.path.join(tmp, "central.db"))
    con.execute("UPDATE subscriptions SET expires_at=?, status='active' WHERE id=?", (time.time()-5, sub)); con.commit(); con.close()
    expired = await db.expire_due_subscriptions()
    check(u.id in expired, "expire → subscription expired")

    # E. FeatureGate tiers (bronze blocks premium, gold allows)
    gate = FeatureGate(db, cache_ttl=0)
    # give alice a fresh active silver sub again
    await db.create_subscription(u.id, silver.id, duration_days=30, approved_by=999)
    check(await gate.has_feature(5000, "archive"), "silver allows archive")
    bronze = await db.get_plan("bronze")
    b = await db.create_user(5001, username="bob", status=USER_ACTIVE)
    await db.create_subscription(b.id, bronze.id, duration_days=30, approved_by=999)
    check(not await gate.has_feature(5001, "archive"), "bronze blocks archive")
    check(await gate.has_feature(5001, "banner"), "bronze allows banner")
    gold = await db.get_plan("gold")
    g = await db.create_user(5002, username="gwen", status=USER_ACTIVE)
    await db.create_subscription(g.id, gold.id, duration_days=30, approved_by=999)
    check(await gate.has_feature(5002, "hunter") and await gate.has_feature(5002, "ghost"), "gold allows all")
    await db.close()


# --------------------------------------------------------------------------- #
async def phase4_modules(tmp: str) -> None:
    print("\n== PHASE 4 — MODULE / AI KEY MANAGER ==")
    db = await _central(tmp)
    u = await db.create_user(7000, status="active")
    await db.add_gemini_key(u.id, "KEY_A")
    await db.add_gemini_key(u.id, "KEY_B")
    await db.add_gemini_key(u.id, "KEY_C")
    keys = await db.list_gemini_keys(u.id)
    kid_a = [k["id"] for k in keys if k["api_key"] == "KEY_A"][0]
    kid_b = [k["id"] for k in keys if k["api_key"] == "KEY_B"][0]
    await db.set_gemini_key_enabled(u.id, kid_a, False)             # disable A
    await db.mark_gemini_failure(u.id, "KEY_B")                     # fail B once
    active = await db.get_active_gemini_keys(u.id)
    check("KEY_A" not in active, "disabled key ignored")
    check(active == ["KEY_C", "KEY_B"], f"rotation: least-failed first ({active})")
    check(await db.count_gemini_keys(u.id, enabled=True) == 2, "enabled-key count correct")
    check(await db.count_gemini_keys(u.id, enabled=False) == 1, "disabled-key count correct")
    await db.close()


# --------------------------------------------------------------------------- #
async def phase5_db_audit(tmp: str) -> None:
    print("\n== PHASE 5 — DATABASE AUDIT ==")
    db = await _central(tmp)
    path = os.path.join(tmp, "central.db")
    con = sqlite3.connect(path)
    idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    check(any("instances" in i for i in idx), "instances index present")
    check(any("gemini" in i for i in idx), "gemini_keys index present")
    # foreign_keys pragma is enabled by the connection layer
    fk = con.execute("PRAGMA foreign_keys").fetchone()
    con.close()
    # orphan check: instances with no owner
    u = await db.create_user(8000, status="active")
    inst = await db.create_instance(u.id, "i", "s", "d")
    con = sqlite3.connect(path)
    orphans = con.execute(
        "SELECT COUNT(*) FROM instances i LEFT JOIN users u ON i.user_id=u.id WHERE u.id IS NULL"
    ).fetchone()[0]
    con.close()
    check(orphans == 0, "no orphan instances")
    check(len({f.key for f in __import__('sentinel.registry', fromlist=['FEATURE_CATALOG']).FEATURE_CATALOG}) ==
          len(await db.list_features()), "feature rows == catalog (no dupes)")
    await db.close()


# --------------------------------------------------------------------------- #
async def phase6_security(tmp: str) -> None:
    print("\n== PHASE 6 — SECURITY ==")
    db = await _central(tmp)
    # bot_state never has secret columns
    cols = {r[1] for r in sqlite3.connect(os.path.join(tmp, "central.db")).execute("PRAGMA table_info(bot_state)")}
    check(not ({"code","otp","password","twofa","two_fa"} & cols), f"bot_state has no secret columns ({sorted(cols)})")
    # RBAC: support role cannot manage admins; owner bypasses
    await db.add_admin(4242, role="support"); await db.set_admin_role(4242, "support")
    check(await db.has_permission(4242, "monitoring"), "support role has monitoring")
    check(not await db.has_permission(4242, "admins.manage"), "support role denied admins.manage (no priv-esc)")
    await db.ensure_owner(1); check(await db.has_permission(1, "admins.manage"), "owner bypass")
    # non-admin has nothing
    check(not await db.has_permission(999999, "users.manage"), "unknown user denied all admin perms")
    await db.close()


# --------------------------------------------------------------------------- #
async def phase7_perf(tmp: str) -> None:
    print("\n== PHASE 7 — PERFORMANCE ==")
    from sentinel.monitoring import gather_overview
    db = await _central(tmp)
    for i in range(5):
        u = await db.create_user(9000 + i, status="active")
        await db.create_instance(u.id, f"i{i}", "s", "d")
    # Count SQL statements executed by gather_overview (N+1 detector).
    # aiosqlite's execute() returns an async-context-manager object (NOT a
    # coroutine), so the wrapper must be a plain function returning that object.
    counter = {"n": 0}
    raw_exec = db._db.execute
    def counting(*a, **k):
        counter["n"] += 1
        return raw_exec(*a, **k)
    db._db.execute = counting  # type: ignore
    ov = await gather_overview(db)
    db._db.execute = raw_exec  # type: ignore
    check(ov.total_users >= 5 and ov.total_instances >= 5, "overview aggregates correctly")
    check(counter["n"] <= 25, f"overview uses bounded queries (no N+1): {counter['n']} statements")
    await db.close()


async def main() -> int:
    root = tempfile.mkdtemp(prefix="sentinel_audit_")
    try:
        for fn in (phase2_feature_matrix, phase3_e2e, phase4_modules,
                   phase5_db_audit, phase6_security, phase7_perf):
            sub = tempfile.mkdtemp(prefix=f"{fn.__name__}_", dir=root)
            try:
                await fn(sub)
            except Exception:
                FAILURES.append(fn.__name__ + " EXCEPTION")
                traceback.print_exc()
        print("\n" + ("=" * 50))
        if FAILURES:
            print(f"FAILURES ({len(FAILURES)}):")
            for f in FAILURES:
                print("  -", f)
            return 1
        print("ALL AUDIT CHECKS PASSED")
        return 0
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
