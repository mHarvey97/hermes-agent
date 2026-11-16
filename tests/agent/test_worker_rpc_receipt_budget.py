"""Lost-ACK regression: the owner committed, but the worker's 20s deadline expired.

All state and WebSocket traffic are disposable/synthetic. Discovery is the fixture's
loopback endpoint; receipts and execution fencing use the real owner ledger.
"""
import json
import math
import threading
import time

import pytest

from tests.agent.test_worker_rpc_connection_reuse import owner  # shared fixture


def test_persistence_answer_budget_matches_owner_write_patience():
    from agent.runtime_session_store import RECEIPT_BUDGET_S
    from hermes_state import SessionDB
    assert math.isfinite(RECEIPT_BUDGET_S)
    assert RECEIPT_BUDGET_S == SessionDB._TRANSCRIPT_WRITE_PATIENCE_S


def test_rpc_uses_the_shared_receipt_budget(tmp_path, monkeypatch):
    from agent.runtime_session_store import RECEIPT_BUDGET_S, WorkerRPC
    budgets = []
    class Link:
        def send(self, request_id, message):
            pass
        def receive(self, timeout):
            budgets.append(timeout)
            return {'result': {}}
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: Link())
    assert rpc('worker.persist') == {}
    assert budgets == [RECEIPT_BUDGET_S]


def test_committed_receipt_delayed_past_old_deadline_does_not_poison_worker(owner, tmp_path):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
    db = SessionDB(tmp_path / 'state.db')
    rpc = WorkerRPC(tmp_path)
    store = None
    committed = threading.Event()
    try:
        db.create_session('synthetic', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='synthetic-owner')
        scope = dict(epoch=epoch, execution_id='synthetic-worker', session_id='synthetic', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='synthetic-secret')

        def persist(params):
            result = mutate_worker_execution(db, **params)
            if params['sequence'] == 1:
                # Real committed receipt, real WebSocket held beyond the old deadline.
                owner.relay.stall(21.0)
                committed.set()
            return result
        owner.persist = persist
        store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
        started = time.monotonic()
        assert store.refresh_session_turn_lease('synthetic', 'synthetic-holder') is False
        assert committed.is_set() and time.monotonic() - started >= 20.0
        assert store.failure is None and store.journal['pending'] == []
        # The next turn's lease acquisition used to raise the sticky timeout here.
        assert store.try_acquire_session_turn_lease('synthetic', 'next-turn') is True
        assert store._apply('execution.finish', {}) == {'status': 'terminal'}
        assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 3
        assert db._read_one('SELECT status FROM worker_executions')[0] == 'terminal'
        assert owner.requests == ['worker.persist'] * 3
        assert owner.connections == 1  # no resend/re-adoption to hide the timeout
    finally:
        if store is not None:
            store._outbox_owner.close()
        rpc.close()
        db.close()


@pytest.mark.parametrize(('change', 'reason'), [
    ({'execution_id': 'unknown'}, 'not_found'),
    ({'session_id': 'foreign'}, 'permission_denied'),
    ({'generation': 99}, 'stale_generation'),
])
def test_unknown_or_foreign_execution_remains_fail_closed(tmp_path, change, reason):
    from hermes_state import SessionDB
    from hermes_state_runtime import (RuntimeStoreError, begin_runtime_epoch,
                                      mutate_worker_execution, register_worker_execution)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('synthetic', 'cli')
        db.create_session('foreign', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='synthetic-owner')
        scope = dict(epoch=epoch, execution_id='synthetic-worker', session_id='synthetic', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='synthetic-secret')
        with pytest.raises(RuntimeStoreError, match=reason):
            mutate_worker_execution(db, **(scope | change), sequence=1,
                                    operation='turn.renew', payload={'holder': 'h', 'ttl_seconds': 300.0})
        assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 0
        assert db._read_one('SELECT last_sequence FROM worker_executions')[0] == 0
    finally:
        db.close()


def test_exhausted_receipt_deadline_does_not_resend_or_clear_pending(tmp_path, monkeypatch):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    calls = []
    class SilentLink:
        def send(self, request_id, message):
            calls.append(json.loads(message))
        def receive(self, timeout):
            assert math.isfinite(timeout)
            raise TimeoutError('worker_receipt_timeout')
    rpc = WorkerRPC(tmp_path)
    monkeypatch.setattr(rpc, '_connection', lambda: SilentLink())
    store = RuntimeSessionStore(rpc, {'session_id': 'synthetic'}, tmp_path / 'outbox')
    try:
        with pytest.raises(TimeoutError, match='worker_receipt_timeout'):
            store.try_acquire_session_turn_lease('synthetic', 'h')
        assert len(calls) == 1
        assert store.failure == 'worker_receipt_timeout'
        assert len(store.journal['pending']) == 1
    finally:
        store._outbox_owner.close()


def test_unknown_kanban_receipt_never_becomes_success(tmp_path, monkeypatch):
    from gateway import session_kanban
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli.kanban_worker_client import _exit_code
    # Even a purported exit-0 event cannot override an unknown owner receipt.
    monkeypatch.setattr(session_kanban, 'worker_result', lambda *a: {'exit_code': 0})
    with pytest.raises(GatewayClientError, match='Kanban execution unknown; retained by owner'):
        _exit_code({'status': 'unknown'}, {}, str(tmp_path / 'kanban.db'))
