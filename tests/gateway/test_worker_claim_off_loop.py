"""Worker claim/verification must not park the gateway loop behind a DB lock."""
import asyncio
import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,blocked', [
    ('register', '_claim'), ('adopt', '_claim'), ('persist', '_claim'),
    ('adopt', '_verify'), ('persist', '_verify'),
])
async def test_claim_and_verification_leave_loop_responsive(monkeypatch, operation, blocked):
    from gateway import session_authorities, session_worker as worker

    monkeypatch.setattr(session_authorities, 'require_serving', lambda authority: None)
    authority = SimpleNamespace(db=object(), epoch='epoch',
                                _require_admission_open=lambda: None,
                                authorize=lambda *args: None)
    connection = SimpleNamespace(authority=authority,
                                 actor=SimpleNamespace(capabilities={'worker:adopt'}))
    params = dict(profile_id='p', session_id='s', execution_id='e', generation=0,
                  pid=123, birth=1, secret='s' * 32)
    if operation == 'register':
        params['kind'] = 'compute'
    elif operation == 'persist':
        params.update(epoch='epoch', sequence=1, operation='message.append', payload={})
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocking(*args):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5), 'loop failed to release blocked worker'
        return 'claim'

    monkeypatch.setattr(worker, '_claim', lambda *args: 'claim')
    monkeypatch.setattr(worker, '_verify', lambda *args: None)
    monkeypatch.setattr(worker, blocked, blocking)
    for name in ('register_worker_execution', 'adopt_worker_execution', 'mutate_worker_execution'):
        monkeypatch.setattr(worker, name, lambda *args, **kwargs: {'status': 'ok'})
    task = asyncio.create_task(worker.worker_request(connection, None, params, operation=operation))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert not task.done()
        # This continuation is itself the liveness proof while the lock is held.
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        assert await asyncio.wait_for(task, 2) == {'status': 'ok'}
    finally:
        release.set()
        if not task.done():
            await task
