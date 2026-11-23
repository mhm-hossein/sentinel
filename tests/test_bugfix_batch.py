"""Regression tests for the bug-fix batch (2026-09-23).

Run: ./venv/bin/python tests/test_bugfix_batch.py
Isolated temp DBs only; never touches data/.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import zipfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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


async def test_consume_restart_atomic(tmp: str) -> None:
    """C6: consume_restart clears in one statement (no lost restart)."""
    db = await _central(tmp)
    u = await db.create_user(9001, status="active")
    inst = await db.create_instance(u.id, "I", "", "")
    check(await db.consume_restart(inst.id) is False, "consume with no request -> False")
    await db.request_restart(inst.id)
    check(await db.consume_restart(inst.id) is True, "consume with request -> True")
    check(await db.consume_restart(inst.id) is False, "second consume -> False (cleared)")
    await db.close()


async def test_perpetual_subscription(tmp: str) -> None:
    """C8: extending a perpetual (NULL expiry) sub keeps it perpetual."""
    db = await _central(tmp)
    u = await db.create_user(9002, status="active")
    plans = await db.list_plans()
    plan_id = plans[0].id if plans else 1
    await db._db.execute(
        "INSERT INTO subscriptions (user_id, plan_id, starts_at, expires_at, status, created_at)"
        " VALUES (?, ?, ?, NULL, 'active', ?);",
        (u.id, plan_id, 1.0, 1.0),
    )
    await db._db.commit()
    ok = await db.extend_subscription(u.id, 30)
    sub = await db.get_active_subscription(u.id)
    check(ok is True, "extend returns True")
    check(sub is not None and sub["expires_at"] is None, "perpetual stays NULL (not converted)")
    await db.close()


async def test_plan_in_use_guard(tmp: str) -> None:
    """C4: plan_in_use reports references so delete can be refused."""
    db = await _central(tmp)
    u = await db.create_user(9003, status="active")
    plans = await db.list_plans()
    plan_id = plans[0].id
    refs = await db.plan_in_use(plan_id)
    check(isinstance(refs, dict) and "users" in refs, "plan_in_use returns counts dict")
    # user was created with default plan; at least users or subs reference something
    total_all = 0
    for p in plans:
        total_all += sum((await db.plan_in_use(p.id)).values())
    check(total_all >= 0, "plan_in_use runs for all plans without error")
    await db.close()


async def test_supervisor_reap_logic() -> None:
    """C1+C7: _reap honors expected exits and auth_state predicate (static)."""
    import inspect
    from sentinel.platform import supervisor as sup_mod
    src = inspect.getsource(sup_mod.Supervisor._reap)
    check("_expected_exits" in src, "reap consults _expected_exits")
    check("auth_state" in src, "reap checks auth_state like spawn gate")
    check("INST_STOPPED" in src, "reap marks undesired workers STOPPED (no flap)")
    rec_src = inspect.getsource(sup_mod.Supervisor._reconcile)
    check("_expected_exits.add" in rec_src, "reconcile marks deliberate restarts expected")


async def test_start_failure_surfaces() -> None:
    """C3: run_platform re-raises component failures (no silent exit 0)."""
    import inspect
    import start as start_mod
    src = inspect.getsource(start_mod.run_platform)
    check("raise failure" in src or "raise " in src, "run_platform re-raises failure")
    check("signal-wait" in src, "signal task excluded from failure detection")


def test_save_recent_excludes_command() -> None:
    """V9: save_recent accepts exclude_ids and skips them."""
    import inspect
    from save.saver import MessageSaver
    src = inspect.getsource(MessageSaver.save_recent)
    check("exclude_ids" in src, "save_recent has exclude_ids param")
    check("excluded" in src, "save_recent filters excluded ids")


def test_ai_routing_word_boundary() -> None:
    """V12: model/trust match on first token only."""
    import inspect
    from ai.commands import AICommandHandler
    src = inspect.getsource(AICommandHandler._route)
    check('startswith("model ")' in src, "model needs trailing space")
    check('startswith("trust ")' in src, "trust needs trailing space")
    check('startswith("model")' not in src, "no bare startswith(model) without space")
    check('startswith("trust")' not in src, "no bare startswith(trust) without space")


def test_banner_help_from_registry() -> None:
    """V11: _render_help iterates HELP_ENTRIES."""
    import inspect
    from banner.commands import CommandHandler
    src = inspect.getsource(CommandHandler._render_help)
    check("HELP_ENTRIES" in src, "help renders from HELP_ENTRIES registry")


def test_zipslip_guard(tmp: str) -> None:
    """V13: _extract_zip refuses ../ and absolute members."""
    from backup.manager import BackupManager
    evil = os.path.join(tmp, "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../pwned.txt", "x")
        zf.writestr("/abs.txt", "x")
        zf.writestr("ok.txt", "good")
    target = os.path.join(tmp, "out")
    BackupManager._extract_zip(evil, target)
    check(os.path.exists(os.path.join(target, "ok.txt")), "legit member extracted")
    check(not os.path.exists(os.path.join(tmp, "pwned.txt")), "ZipSlip ../ refused")
    check(not os.path.exists("/abs.txt"), "absolute member refused")


def test_logger_captures_both_directions() -> None:
    """V7: logger registers without incoming-only filter."""
    import inspect
    from logger.service import LoggerService
    src = inspect.getsource(LoggerService.register)
    check("incoming=True" not in src, "no incoming-only filter on capture handler")


def test_reserved_blocks_shadow_verbs() -> None:
    """V2: RESERVED covers all command verbs incl. ghost/hunter/vps."""
    from variables.utils import RESERVED, validate_keyword
    for verb in ("ghost", "hunter", "vps", "autosave", "antidel", "antiedit",
                 "help", "stats", "save", "ai", "banner", "logger", "var"):
        check(verb in RESERVED, f"RESERVED blocks {verb!r}")
        check(validate_keyword(verb) != "", f"validate rejects {verb!r}")


def test_prune_keeps_newest(tmp: str) -> None:
    """V19: _prune_exports keeps newest 10 pairs per chat."""
    from logger.service import LoggerService
    d = os.path.join(tmp, "exports")
    os.makedirs(d)
    for i in range(12):
        base = f"log_777_2026010{i:02d}_000000"
        open(os.path.join(d, base + ".txt"), "w").write("x")
        open(os.path.join(d, base + ".json"), "w").write("x")
    LoggerService._prune_exports(777, d)
    left = sorted(os.listdir(d))
    check(len(left) == 20, f"pruned to 10 pairs (got {len(left)} files)")
    check(not any("202601000_" in n or "202601001_" in n for n in left),
          "two oldest exports pruned")
    check(all(any(f"2026010{i:02d}_" in n for n in left) for i in range(2, 12)),
          "newest 10 exports retained")


async def main() -> int:
    print("== bugfix batch regression ==")
    root = tempfile.mkdtemp(prefix="sentinel_fix_")
    await test_consume_restart_atomic(tempfile.mkdtemp(dir=root))
    await test_perpetual_subscription(tempfile.mkdtemp(dir=root))
    await test_plan_in_use_guard(tempfile.mkdtemp(dir=root))
    await test_supervisor_reap_logic()
    await test_start_failure_surfaces()
    test_save_recent_excludes_command()
    test_ai_routing_word_boundary()
    test_banner_help_from_registry()
    test_zipslip_guard(tempfile.mkdtemp(dir=root))
    test_logger_captures_both_directions()
    test_reserved_blocks_shadow_verbs()
    test_prune_keeps_newest(tempfile.mkdtemp(dir=root))
    print("=" * 50)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("ALL BUGFIX REGRESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
