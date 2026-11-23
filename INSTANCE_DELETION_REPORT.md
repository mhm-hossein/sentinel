# INSTANCE DELETION REPORT

Result: **9/9 scenarios passed**

Harness: `tests/test_instance_deletion.py` — drives the real `ControlBot`
delete handlers, `CentralDatabase`, and `InstancePaths` against isolated
temporary databases and a temporary data root with real files on disk.

| # | Scenario | Result | Detail |
| --- | --- | --- | --- |
| 1 | Delete stopped instance | ✅ PASS | confirm-screen=True, row_gone=True |
| 2 | Delete instance with files | ✅ PASS | dir_before=True, dir_after=False |
| 3 | Delete with missing files (no error) | ✅ PASS | missing_before=True, no_raise=True |
| 4 | Attempt delete while running | ✅ PASS | confirm_blocked=True, perform_refused=True, still_exists=True |
| 5 | Attempt delete another user's instance | ✅ PASS | ownership_denied=True, survived=True, files_intact=True |
| 6 | Verify DB cleanup (row + related state) | ✅ PASS | row_gone=True, related_bot_state_rows=0 |
| 7 | Verify filesystem cleanup | ✅ PASS | files_before=True, all_removed=True |
| 8 | Verify audit entry (INSTANCE_DELETED) | ✅ PASS | logged=True, details=account_id=507 |
| 9 | Monitoring no longer shows instance | ✅ PASS | total 2->1, listed=False |

## Safety properties verified

- **Ownership** is validated from the database (`_owned_instance` → `get_instance` + `get_user`), never from the callback payload — a foreign user is denied at both confirm and perform (test 5).
- **Running/enabled instances are refused** at confirm *and* perform (defence against a stale confirm button) with "Please stop the instance before deleting it." (test 4).
- **Atomic DB cleanup**: `delete_instance` removes the instance row (all runtime metadata) + related `bot_state` rows in one transaction (test 6).
- **Recursive, missing-safe filesystem cleanup** via `InstancePaths.purge` (tests 2, 3, 7).
- **Audit**: an `INSTANCE_DELETED` entry with admin/user id, instance id and account id is written (test 8).
- **Monitoring/statistics** immediately stop counting the instance (test 9).
- **No orphan worker state**: the supervisor coordinates purely via the DB; once the row is gone it is absent from `list_runnable_instances`, and any lingering child is dropped by the next reconcile (`_stop_child`).
