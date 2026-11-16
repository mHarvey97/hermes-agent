# Worker receipt budget: bounded upstream staging

This branch stages the budget-only correction already proposed in
[upstream PR #155440](https://github.com/NousResearch/hermes-agent/pull/155440),
not a competing implementation or a change to execution ownership.

Source commits (Bunny Fu, authorship preserved by cherry-pick):
- e73221f1bea3c9ff13dc1446a077e80e035f346c
- 663e683bd73c0516387635295d0eb2ddad6b0b6f

Base: 3b42157858, the source used by the affected installation. A fresh upstream
main fetch at d65e6800fc still has the same worker-side 20-second receive call.
The unrelated diagnostics/localization commits from the upstream PR are not
included. Existing upstream PR #155440 remains the delivery carrier. This fork
branch is a reviewable staging artifact, not approval to install a fork.

## Proven defect and boundary

`WorkerRPC.__call__` waits 20 seconds, but
`hermes_state_runtime.apply_worker_receipt` uses the owner's 60-second
`SessionDB._TRANSCRIPT_WRITE_PATIENCE_S`. A valid receipt can commit while the
worker has already timed out. `RuntimeSessionStore.retry_pending` retains the
unacknowledged journal entry and latches the timeout. A later in-place Kanban
recovery turn then fails acquiring its lease because the prior failure is sticky.
The managed execution becomes unknown and the submitter's fail-closed
`Kanban execution unknown; retained by owner` trailer reports that disposition.
It is not proof that owner admission or an execution lookup rejected the claim.

The borrowed correction shares the finite 60-second constant through a
lightweight constants module. It does not change ownership checks, receipt
identity, sequencing, replay policy, worker admission, failure counters, or
unknown-execution handling. It changes the shared WorkerRPC answer budget,
including register/adopt calls that use that client, not the Kanban admission
answer budget. The upstream correction knowingly retains a fail-safe residual
race near the deadline: client and server timers do not begin at the same instant.
It is not a cure for every stalled owner or lost transport.

## Actual validation on Windows

New `tests/agent/test_worker_rpc_receipt_budget.py` covers:
- finite budget equality with the owner and the actual RPC call site;
- a real canonical receipt committed through the native register/mutate/finish
  lifecycle, carried over a real loopback WebSocket with its ACK held for 21s;
- subsequent lease acquisition and finish: exactly three receipts, no resend;
- missing execution, foreign session and wrong generation refused without writes;
- exhausted wait remains sticky with a retained journal and no automatic resend;
- an unknown Kanban receipt still refuses even beside a purported exit-0 result.

Baseline: restoring only the old `link.receive(20)` call makes the delayed-ACK
case fail with `TimeoutError: worker_receipt_timeout` (1 failed, 27.35s).
The initial exploratory client helper was discarded; it is not in this branch.

Final gate used `uv run --no-project --with pytest --with pytest-asyncio` with
Python 3.14, the installation's dependency site-packages on PYTHONPATH, and Git
cmd/usr/bin prepended to the subprocess PATH. Test arguments:

```
python -m pytest \
 tests/agent/test_worker_rpc_receipt_budget.py \
 tests/agent/test_worker_rpc_connection_reuse.py \
 tests/agent/test_runtime_session_store.py \
 tests/agent/test_worker_rpc_proxy.py \
 tests/agent/test_worker_rpc_loop_diagnostic.py \
 tests/hermes_state/test_runtime_worker.py \
 tests/gateway/test_worker_admission_reservation.py \
 tests/gateway/test_managed_worker_fencing.py \
 tests/hermes_cli/test_kanban_worker_admission_wait.py \
 tests/hermes_cli/test_kanban_worker_owner_wait.py \
 tests/hermes_cli/test_kanban_owner_receipt_reclaim.py \
 tests/agent/test_kanban_turn_recovery.py \
 tests/gateway/test_kanban_finite_admission.py \
 tests/hermes_cli/test_sessions_prune_via_owner.py -q -rs --tb=short
```

Result: **122 passed, 11 skipped, 0 failed in 64.69s**. Skips are existing
POSIX/macOS/Linux-only cases, not waived failing tests. Pytest-asyncio emits its
existing unset fixture-loop-scope deprecation warning.

`uv run --no-project --with ruff python -m ruff check --config
ruff.pre-commit.toml` on all five changed Python files: PASS. `git diff --check`:
PASS. The local commit hook requires `just`, absent on this host; its Ruff check
was run directly instead. No dependency lockfile is changed or included.

The socket discovery/ticket boundary is the existing test fixture, not an actual
production owner. Native claim/adopt fencing also passed in the existing gateway
reservation tests. This is synthetic source qualification, NOT an installed
full-dispatcher smoke or an operational recovery receipt.

## Release boundary

Independent review must precede installation. Integrate through existing upstream
PR #155440 (or an explicitly approved bounded backport); do not create a second
dispatcher, hot-edit a running checkout, or manually launch a production worker.
Use the owning installation's documented update/installer and authorized
restart/drain procedure only. This branch does not authorize an update-channel
change, a gateway restart, a production card replay, or removal of a privacy hold.
