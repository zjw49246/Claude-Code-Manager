"""Phase 2 smoke: InstanceManager in PTY mode end-to-end with real CC.

Verifies: launch -> CCMBackend -> channel injection -> JSONL events ->
_process_event (DB writes absorbed by fake) -> broadcaster -> proxy exit.
"""
import asyncio, os, sys
from unittest.mock import MagicMock

sys.path.insert(0, "/home/ubuntu/Claude-Code-Manager-dev")
os.environ["USE_PTY_MODE"] = "true"
os.environ["PATH"] = "/home/ubuntu/Claude-Code-Manager-dev/.venv/bin:" + os.environ["PATH"]


class FakeDB:
    def __init__(self, log):
        self.log = log

    async def execute(self, stmt):
        return MagicMock(rowcount=1)

    async def commit(self):
        pass

    async def get(self, model, pk):
        m = MagicMock(); m.current_task_id = 3; return m

    def add(self, obj):
        self.log.append(obj)

    async def refresh(self, obj):
        obj.id = len(self.log)

    async def flush(self):
        pass


class FakeDBFactory:
    def __init__(self):
        self.entries = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeDB(self.entries)

    async def __aexit__(self, *exc):
        return False


async def main():
    from backend.services.instance_manager import InstanceManager

    dbf = FakeDBFactory()
    broadcaster = MagicMock()
    broadcasts = []

    async def record(channel, data):
        broadcasts.append((channel, data.get("event_type") or data.get("event")))

    broadcaster.broadcast = record

    im = InstanceManager(dbf, broadcaster)
    assert im._pty_backend is not None, "PTY backend not initialized!"
    print("PTY backend active:", type(im._pty_backend).__name__)

    cwd = "/tmp/pty-spikes/p2_smoke_ws"
    os.makedirs(cwd, exist_ok=True)

    pid = await im.launch(
        instance_id=1,
        prompt="Run `echo p2-smoke-ok` with the Bash tool and report the output.",
        task_id=3,
        cwd=cwd,
        model="claude-haiku-4-5",
        provider="claude",
    )
    print("launched, proxy pid:", pid)

    process = im.processes[1]
    exit_code = await asyncio.wait_for(process.wait(), timeout=180)
    print("turn finished, exit_code:", exit_code)

    ev_types = [b[1] for b in broadcasts]
    print("broadcast events:", ev_types)
    print("log entries written:", len(dbf.entries))

    assert exit_code == 0
    assert "tool_use" in ev_types, "no tool_use broadcast"
    assert any(t == "message" for t in ev_types), "no assistant message broadcast"
    assert len(dbf.entries) >= 3, "log entries missing"

    # --- second turn: hot session reuse via resume_session_id ---
    pool_sessions = im._pty_backend._pool._sessions
    sid = next(iter(pool_sessions))
    sess = pool_sessions[sid]
    print("pool session alive after turn 1:", sess.is_alive, "sid:", sid[:8])
    pid_before = sess._process.pid

    import time
    t0 = time.monotonic()
    await im.launch(
        instance_id=1,
        prompt="Reply with exactly: second-turn-ok",
        task_id=3,
        cwd=cwd,
        model="claude-haiku-4-5",
        provider="claude",
        resume_session_id=sid,
        chat_initiated=True,
    )
    exit2 = await asyncio.wait_for(im.processes[1].wait(), timeout=120)
    dt = time.monotonic() - t0
    sess2 = im._pty_backend._pool._sessions.get(sid)
    same_proc = sess2 is not None and sess2._process.pid == pid_before
    print("turn2 exit:", exit2, "elapsed: %.1fs" % dt, "same process:", same_proc)
    texts = [b for b in broadcasts if "second-turn-ok" in str(b)]

    await im._pty_backend.shutdown()
    ok = exit2 == 0 and same_proc
    print("SMOKE RESULT:", "PASS" if ok else "FAIL",
          "(events=%d, hot_reuse=%s, turn2=%.1fs)" % (len(dbf.entries), same_proc, dt))

asyncio.run(main())
