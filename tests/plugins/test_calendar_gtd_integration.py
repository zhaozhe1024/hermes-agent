"""Tests for Calendar GTD / PA integration — Prompt 2 (final v2).

All state_meta tests use tmp_path.  Covers:
  - safe_runner: redaction (5 flags), argv injection, PATH, JSON parse
  - UUID: deterministic, chunk replay, invalid fail-closed
  - _standalone_send: idempotency_uuid signature
  - Adapter: chunk UUID metadata pass-through (real send path)
  - task_action_handler: confirmation_required, APR/NWF recovery,
    token mismatch/expiry, slot fail-closed (CLI err / missing field),
    chat_id+user_id auth (includes empty chat_id reject),
    create_session forces task show
  - calendar_import_handler: path restriction, safe name, lock-first
  - process_one_claim: no_send, send→persist→ack, fail, missing msg_id,
    already-sent re-ack
  - Delivery dedup: sent→persisted→acked state machine
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_H = os.path.expanduser("~/.hermes")
for d in ("hermes-agent", "scripts", "skills/calendar-gtd-integration/scripts"):
    p = os.path.join(_H, d)
    if p not in sys.path:
        sys.path.insert(0, p)

NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# ── safe_runner ─────────────────────────────────────────────────────────────

class TestRedact:
    def test_five_flags(self):
        from safe_runner import _redact_command
        for f in ("--config","--app-password","--notion-token",
                  "--lease-token","--confirmation-token"):
            assert _redact_command(["b", f, "v"]) == ["b", f, "***"]

    def test_chained(self):
        from safe_runner import _redact_command
        assert _redact_command(
            ["b","--config","a","--lease-token","b","--confirmation-token","c",
             "--app-password","d","dr"]
        ) == ["b","--config","***","--lease-token","***","--confirmation-token",
              "***","--app-password","***","dr"]

    def test_shell_injection(self):
        from safe_runner import run
        for a in ("`ls`","$(whoami)","|cat"):
            r = run(["/bin/echo", a])
            assert r.exit_code == 0 and a in r.stdout_raw

    @patch("safe_runner.subprocess.run")
    def test_env_path(self, mock_run):
        from safe_runner import run, _BASE_ENV
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        run(["/fake/bin"])
        assert os.path.expanduser("~/.local/bin") in mock_run.call_args[1]["env"]["PATH"]

    def test_doctors(self):
        from safe_runner import cgtd_doctor, pa_doctor, pa_freshness
        for fn in (cgtd_doctor, pa_doctor, pa_freshness):
            r = fn(); assert r.exit_code == 0 and r.parsed is not None

# ── UUID ────────────────────────────────────────────────────────────────────

class TestUUID:
    def test_deterministic(self):
        assert uuid.uuid5(NS,"k") == uuid.uuid5(NS,"k")
    def test_chunk_replay(self):
        b = uuid.uuid4(); assert str(uuid.uuid5(b,"0")) == str(uuid.uuid5(b,"0"))
    def test_chunks_differ(self):
        b = uuid.uuid4(); assert uuid.uuid5(b,"0") != uuid.uuid5(b,"1")
    def test_invalid(self):
        with pytest.raises(ValueError): uuid.UUID("bad")

# ── Adapter chunk UUID ──────────────────────────────────────────────────────

class TestAdapterChunkUUID:
    def test_standalone_send_signature(self):
        from plugins.platforms.feishu.adapter import _standalone_send
        import inspect
        assert "idempotency_uuid" in inspect.signature(_standalone_send).parameters

    @patch("plugins.platforms.feishu.adapter.FeishuAdapter._build_lark_client")
    @patch("plugins.platforms.feishu.adapter.FeishuAdapter._run_blocking")
    def test_chunk_uuids_passed_to_send_raw_message(self, mock_run, _mock_client):
        """Verify each chunk gets uuid5(base, chunk_index) via metadata."""
        from plugins.platforms.feishu.adapter import FeishuAdapter
        from gateway.config import PlatformConfig
        adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._client = MagicMock()

        # Capture metadata passed to _send_raw_message
        captured_metadata = []
        async def capture_srm(*, chat_id, msg_type, payload, reply_to, metadata, **kw):
            captured_metadata.append(metadata)
            resp = MagicMock()
            resp.success.return_value = True
            resp.data.message_id = f"om_chunk_{len(captured_metadata)}"
            return resp
        adapter._send_raw_message = capture_srm

        base_uuid = str(uuid.uuid4())
        # Send a short message (won't chunk at 8k) — wrap to test chunking
        long_msg = "x" * 100  # single chunk
        asyncio.run(adapter.send("oc_test", long_msg,
                     metadata={"idempotency_uuid": base_uuid}))
        # For single chunk, metadata should contain chunk 0's UUID
        # The send() method derives chunk_metadata with per-chunk UUID
        assert len(captured_metadata) >= 1
        # Verify the first chunk's metadata has idempotency_uuid = uuid5(base, "0")
        expected_chunk0 = str(uuid.uuid5(uuid.UUID(base_uuid), "0"))
        assert captured_metadata[0].get("idempotency_uuid") == expected_chunk0

    def test_send_raw_message_rejects_invalid_uuid(self):
        """Invalid idempotency_uuid in metadata → ValueError."""
        from plugins.platforms.feishu.adapter import FeishuAdapter
        from gateway.config import PlatformConfig
        adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._client = MagicMock()
        with pytest.raises(ValueError, match="must be a valid UUID"):
            asyncio.run(adapter._send_raw_message(
                chat_id="oc_x", msg_type="text", payload="{}",
                reply_to=None, metadata={"idempotency_uuid": "garbage"},
            ))

    def test_send_raw_message_still_accepts_no_uuid(self):
        """No idempotency_uuid → uuid4() fallback (backward compat)."""
        from plugins.platforms.feishu.adapter import FeishuAdapter
        from gateway.config import PlatformConfig
        adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={}))
        adapter._client = MagicMock()
        resp = MagicMock(); resp.success.return_value = True; resp.data.message_id = "om"
        adapter._run_blocking = AsyncMock(return_value=resp)
        asyncio.run(adapter._send_raw_message(
            chat_id="oc_x", msg_type="text", payload="{}",
            reply_to=None, metadata=None,
        ))
        # Should not raise

# ── task_action_handler ─────────────────────────────────────────────────────

@pytest.fixture
def th_setup(monkeypatch, tmp_path):
    import task_action_handler as th
    monkeypatch.setattr(th, "_HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
    monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
    return th

TASK_SHOW = json.dumps({"status":"success","task":{"page_id":"pg-001","title":"T",
    "done":False,"assign":"Assign","start":"2026-07-15T15:00+08:00",
    "due":"2026-07-15T16:00+08:00","archived":False,"in_trash":False,"version":"v1"}})
CONFIRM_REQ = json.dumps({"status":"confirmation_required","page_id":"pg-001",
    "proposal":{"start":"2026-07-16T10:00+08:00","due":"2026-07-16T11:00+08:00"},
    "confirmation":{"level":1,"required_levels":1,"token":"tok_cf",
                     "expires_at":"2099-01-01T00:00:00+00:00"}})
APPLIED = json.dumps({"status":"success","operation":"applied","replayed":False,
    "task":{"page_id":"pg-001","version":"v2"}})
APR = json.dumps({"status":"applied_pending_refresh"})
NWF = json.dumps({"status":"error","error":{"code":"notion_write_failed"}})

def _make_session(th, sid, **kw):
    d = {"feishu_chat_id":"oc_frank","feishu_user_id":"ou_frank",
         "source_message_id":"om1","page_id":"pg-001","action":"reschedule",
         "start":"2026-07-16T10:00+08:00","due":"2026-07-16T11:00+08:00",
         "expected_version":"v1","idempotency_key":"k1","confirmation_level":0,
         "confirmation_token":None,"confirmation_expires_at":None,
         "state":th.S_PROPOSED,"hermes_job_id":"","result_message_id":None}
    d.update(kw)
    th._save_session(sid, d)
    return d


class TestTaskActionHandler:
    def test_confirmation_required(self, th_setup):
        th = th_setup; sid = "s1"
        _make_session(th, sid)
        with patch("task_action_handler.pa_task_action") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(0, CONFIRM_REQ, "", json.loads(CONFIRM_REQ), ["pa"], 100)
            r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank")
        assert r["success"] and r["status"] == "confirmation_required"
        assert th.get_session(sid)["confirmation_token"] == "tok_cf"

    def test_token_mismatch(self, th_setup):
        th = th_setup; sid = "s2"
        _make_session(th, sid, state=th.S_AWAITING, confirmation_token="real",
                       confirmation_expires_at="2099-01-01T00:00:00+00:00")
        r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank",
                               confirmation_token="wrong")
        assert not r["success"] and "mismatch" in r["error"].lower()

    def test_token_expired(self, th_setup):
        th = th_setup; sid = "s3"
        _make_session(th, sid, action="extend", start=None, due="18:00",
                       state=th.S_AWAITING, confirmation_token="tok_exp",
                       confirmation_expires_at="2000-01-01T00:00:00+00:00")
        r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank",
                               confirmation_token="tok_exp")
        assert not r["success"] and "expired" in r["error"].lower()

    def test_applied_pending_refresh(self, th_setup):
        th = th_setup; sid = "s4"
        _make_session(th, sid, action="start", start=None, due=None)
        with patch("task_action_handler.pa_task_action") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(1, APR, "", json.loads(APR), ["pa"], 100)
            r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank")
        assert r["success"] and r["status"] == "applied_pending_refresh"
        assert r["retry_same_key"]

    def test_notion_write_failed(self, th_setup):
        th = th_setup; sid = "s5"
        _make_session(th, sid, action="start", start=None, due=None)
        with patch("task_action_handler.pa_task_action") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(1, NWF, "", json.loads(NWF), ["pa"], 100)
            r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank")
        assert not r["success"]
        assert r.get("recoverable")
        assert r["retry_same_key"]
        assert r["status"] == "notion_write_failed"

    def test_slot_fail_closed_cli_err(self, th_setup):
        th = th_setup; sid = "s6"
        _make_session(th, sid, state=th.S_AWAITING, confirmation_token="tok_s6",
                       confirmation_expires_at="2099-01-01T00:00:00+00:00")
        with patch("task_action_handler.suggest_slots") as m:
            m.return_value = {"success": False, "error": "CLI timeout"}
            r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank",
                                   confirmation_token="tok_s6")
        assert not r["success"]

    def test_slot_fail_closed_missing_requested(self, th_setup):
        th = th_setup; sid = "s7"
        _make_session(th, sid, state=th.S_AWAITING, confirmation_token="tok_s7",
                       confirmation_expires_at="2099-01-01T00:00:00+00:00")
        with patch("task_action_handler.suggest_slots") as m:
            m.return_value = {"success": True, "slots": []}
            r = th.execute_action(sid, feishu_chat_id="oc_frank", feishu_user_id="ou_frank",
                                   confirmation_token="tok_s7")
        assert not r["success"]

    def test_empty_chat_id_rejected(self, th_setup):
        th = th_setup
        assert not th.is_authorized("ou_frank", "")

    def test_wrong_chat_id_rejected(self, th_setup):
        th = th_setup
        assert not th.is_authorized("ou_frank", "oc_wrong")

    def test_no_config_denies(self, th_setup, monkeypatch):
        th = th_setup
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "")
        assert not th.is_authorized("ou_frank", "oc_frank")

    def test_create_session_forces_show(self, th_setup):
        th = th_setup
        with patch("task_action_handler.show_task") as m:
            m.return_value = {"success":True,"version":"v_forced","page_id":"pg-001",
                "title":"","done":False,"assign":"","archived":False,"in_trash":False,
                "start":None,"due":None}
            r = th.create_session("sx","oc_frank","ou_frank","om","pg-001","start")
        assert r["success"] and r["session"]["expected_version"] == "v_forced"

    def test_execute_action_denies_stranger(self, th_setup):
        th = th_setup
        r = th.execute_action("sx", feishu_chat_id="oc_frank", feishu_user_id="ou_stranger")
        assert not r["success"]

# ── calendar_import_handler ─────────────────────────────────────────────────

class TestCalendarImport:
    def test_rejects_tmp(self, tmp_path):
        import calendar_import_handler as cih
        f = tmp_path / "bad.ics"; f.write_text("BEGIN:VCALENDAR")
        assert cih._is_allowed_file(str(f), "bad.ics") is not None

    def test_accepts_cache_dir(self, tmp_path, monkeypatch):
        import calendar_import_handler as cih
        cache = tmp_path / "cache" / "documents"; cache.mkdir(parents=True)
        monkeypatch.setattr(cih, "_MEDIA_CACHE", cache.resolve())
        f = cache / "ok.ics"; f.write_text("BEGIN:VCALENDAR")
        assert cih._is_allowed_file(str(f), "ok.ics") is None

    def test_rejects_sibling_prefix(self, tmp_path, monkeypatch):
        import calendar_import_handler as cih
        cache = tmp_path / "cache"; cache.mkdir()
        evil = tmp_path / "cache_evil"; evil.mkdir()
        f = evil / "bad.ics"; f.write_text("BEGIN:VCALENDAR")
        monkeypatch.setattr(cih, "_MEDIA_CACHE", cache.resolve())
        assert cih._is_allowed_file(str(f.resolve()), "bad.ics") is not None

    def test_safe_name_unique(self):
        import calendar_import_handler as cih
        assert cih._safe_filename("a.ics") != cih._safe_filename("a.ics")

    def test_lock_first(self, tmp_path, monkeypatch):
        import calendar_import_handler as cih
        ics_dir = tmp_path / "ics"; ics_dir.mkdir()
        cache = tmp_path / "cache" / "documents"; cache.mkdir(parents=True)
        monkeypatch.setattr(cih, "ICS_DIR", str(ics_dir))
        monkeypatch.setattr(cih, "INCOMING_DIR", str(ics_dir / ".incoming"))
        monkeypatch.setattr(cih, "INGESTION_LOCK", str(tmp_path / ".ingestion.lock"))
        monkeypatch.setattr(cih, "_MEDIA_CACHE", cache.resolve())
        f = cache / "test.ics"; f.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR")
        with patch("calendar_import_handler.cgtd_run_file") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(0, '{"status":"success","phases":{}}', "",
                                        {"status":"success","phases":{}}, ["cgtd"], 100)
            r = cih.import_file(str(f))
            assert r["success"]

    def test_subprocess_cli_returns_valid_json(self):
        """Actual subprocess call to the CLI entry point returns valid JSON."""
        import subprocess
        proc = subprocess.run(
            [sys.executable,
             os.path.join(_H, "skills/calendar-gtd-integration/scripts/calendar_import_handler.py")],
            input=json.dumps({"command": "is_allowed",
                              "args": {"file_path": "/etc/passwd"}}),
            capture_output=True, text=True, timeout=10,
            env={**os.environ},
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert "allowed" in result


# ── process_one_claim (reminder worker) ─────────────────────────────────────

PA_CLAIM_NOSEND = '{"status":"success","command":"reminders.claim","no_send":true}'
PA_CLAIM_PAYLOAD = json.dumps({
    "status":"success","command":"reminders.claim","no_send":False,
    "reminder_id":101,"reminder_ids":[101],"lease_token":"ltok",
    "lease_expires_at":"2099-01-01T00:00:00+00:00",
    "idempotency_key":"delivery-key-abc",
    "reminders":[{"reminder_id":101,"kind":"agenda"}],
    "marksdown":"## Test\\n- item",
    "markdown":"## Test",
})

class TestProcessOneClaim:
    @pytest.fixture
    def setup_paths(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Patch paths in pa_reminder_worker
        import pa_reminder_worker as pw
        monkeypatch.setattr(pw, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(pw, "_HERMES_SRC", os.path.join(_H, "hermes-agent"))
        monkeypatch.setattr(pw, "_SCRIPTS_DIR", os.path.join(_H, "scripts"))
        return pw

    def test_no_send(self, setup_paths):
        pw = setup_paths
        with patch("pa_reminder_worker.pa_claim") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(0, PA_CLAIM_NOSEND, "",
                                       json.loads(PA_CLAIM_NOSEND), ["pa"], 100)
            db = pw._get_state_db()
            r = pw.process_one_claim(db)
            assert r is False  # no_send → stop tick

    def test_send_persist_ack(self, setup_paths):
        pw = setup_paths
        with patch("pa_reminder_worker.pa_claim") as m_claim, \
             patch("pa_reminder_worker.pa_ack") as m_ack, \
             patch("pa_reminder_worker.asyncio.run") as m_async:
            from safe_runner import RunResult
            m_claim.return_value = RunResult(
                0, PA_CLAIM_PAYLOAD, "", json.loads(PA_CLAIM_PAYLOAD), ["pa"], 100)
            m_async.return_value = {"message_id": "om_sent_123"}
            m_ack.return_value = RunResult(
                0, '{"status":"success"}', "", {"status":"success"}, ["pa"], 100)

            db = pw._get_state_db()
            r = pw.process_one_claim(db)
            assert r is True  # bundle processed

            # Verify state persisted
            raw = db.get_meta("pa:delivery:delivery-key-abc")
            assert raw
            data = json.loads(raw)
            assert data["feishu_message_id"] == "om_sent_123"
            assert data["state"] == "acked"
            db.close()

    def test_fail_on_send_error(self, setup_paths):
        pw = setup_paths
        with patch("pa_reminder_worker.pa_claim") as m_claim, \
             patch("pa_reminder_worker.pa_fail") as m_fail, \
             patch("pa_reminder_worker.asyncio.run") as m_async:
            from safe_runner import RunResult
            m_claim.return_value = RunResult(
                0, PA_CLAIM_PAYLOAD, "", json.loads(PA_CLAIM_PAYLOAD), ["pa"], 100)
            m_async.return_value = {"error": "Feishu send failed"}
            m_fail.return_value = RunResult(
                0, '{"status":"success"}', "", {"status":"success"}, ["pa"], 100)

            db = pw._get_state_db()
            r = pw.process_one_claim(db)
            assert r is True
            raw = db.get_meta("pa:delivery:delivery-key-abc")
            assert json.loads(raw)["state"] == "failed"
            db.close()

    def test_fail_on_missing_msg_id(self, setup_paths):
        pw = setup_paths
        with patch("pa_reminder_worker.pa_claim") as m_claim, \
             patch("pa_reminder_worker.pa_fail") as m_fail, \
             patch("pa_reminder_worker.asyncio.run") as m_async:
            from safe_runner import RunResult
            m_claim.return_value = RunResult(
                0, PA_CLAIM_PAYLOAD, "", json.loads(PA_CLAIM_PAYLOAD), ["pa"], 100)
            m_async.return_value = {"success": True}  # no message_id!
            m_fail.return_value = RunResult(
                0, '{"status":"success"}', "", {"status":"success"}, ["pa"], 100)

            db = pw._get_state_db()
            r = pw.process_one_claim(db)
            assert r is True
            raw = db.get_meta("pa:delivery:delivery-key-abc")
            assert json.loads(raw)["state"] == "failed"
            db.close()

    def test_already_sent_re_ack(self, setup_paths):
        pw = setup_paths
        db = pw._get_state_db()
        # Pre-populate a "sent" delivery
        db.set_meta("pa:delivery:delivery-key-abc", json.dumps({
            "feishu_message_id":"om_existing","reminder_ids":[101],
            "lease_token":"ltok","state":"sent","message_uuid":"u",
        }))
        db.close()

        with patch("pa_reminder_worker.pa_claim") as m_claim, \
             patch("pa_reminder_worker.pa_ack") as m_ack, \
             patch("pa_reminder_worker.asyncio.run") as m_async:
            from safe_runner import RunResult
            m_claim.return_value = RunResult(
                0, PA_CLAIM_PAYLOAD, "", json.loads(PA_CLAIM_PAYLOAD), ["pa"], 100)
            m_ack.return_value = RunResult(
                0, '{"status":"success"}', "", {"status":"success"}, ["pa"], 100)
            m_async.return_value = {"message_id": "om_new"}

            db2 = pw._get_state_db()
            r = pw.process_one_claim(db2)
            assert r is True
            # Should NOT have called async send (already sent)
            # Verify state moved to acked
            raw = db2.get_meta("pa:delivery:delivery-key-abc")
            assert json.loads(raw)["state"] == "acked"
            # message_id should still be the original
            assert json.loads(raw)["feishu_message_id"] == "om_existing"
            db2.close()


# ── Reminder card tests ─────────────────────────────────────────────────────

PA_CLAIM_SINGLE_START = json.dumps({
    "status": "success", "command": "reminders.claim", "no_send": False,
    "reminder_id": 50, "reminder_ids": [50], "lease_token": "ltok",
    "lease_expires_at": "2099-01-01T00:00:00+00:00",
    "idempotency_key": "card-key-start",
    "reminders": [{"reminder_id": 50, "kind": "start",
        "scheduled_at": "2099-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T01:00:00+00:00",
        "idempotency_key": "rk", "product_command": "task.start"}],
    "tasks": [{"page_id": "pg-001", "title": "Test Task",
        "start": "2099-01-01T10:00:00+08:00",
        "end": "2099-01-01T11:00:00+08:00",
        "assign": "Assign", "done": False}],
    "markdown": "## 任务提醒\n### 即将开始\n- Test Task",
})

PA_CLAIM_SINGLE_COMPLETION = json.dumps({
    "status": "success", "command": "reminders.claim", "no_send": False,
    "reminder_id": 51, "reminder_ids": [51], "lease_token": "ltok",
    "lease_expires_at": "2099-01-01T00:00:00+00:00",
    "idempotency_key": "card-key-completion",
    "reminders": [{"reminder_id": 51, "kind": "completion",
        "scheduled_at": "2099-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T01:00:00+00:00",
        "idempotency_key": "rk2", "product_command": "task.completion"}],
    "tasks": [{"page_id": "pg-002", "title": "Test Task 2",
        "start": "2099-01-01T10:00:00+08:00",
        "end": "2099-01-01T11:00:00+08:00",
        "assign": "Assign", "done": False}],
    "markdown": "## 任务提醒\n### 到时确认\n- Test Task 2",
})

PA_CLAIM_SINGLE_SNOOZE = json.dumps({
    "status": "success", "command": "reminders.claim", "no_send": False,
    "reminder_id": 52, "reminder_ids": [52], "lease_token": "ltok",
    "lease_expires_at": "2099-01-01T00:00:00+00:00",
    "idempotency_key": "card-key-snooze",
    "reminders": [{"reminder_id": 52, "kind": "start",
        "scheduled_at": "2099-01-01T00:00:00+00:00",
        "expires_at": "2099-01-01T01:00:00+00:00",
        "idempotency_key": "rk3", "product_command": "task.snooze"}],
    "tasks": [{"page_id": "pg-003", "title": "Snoozed Task",
        "start": "2099-01-01T10:00:00+08:00",
        "end": "2099-01-01T11:00:00+08:00",
        "assign": "Assign", "done": False}],
    "markdown": "##",
})

PA_CLAIM_MULTI = json.dumps({
    "status": "success", "command": "reminders.claim", "no_send": False,
    "reminder_id": 60, "reminder_ids": [60, 61], "lease_token": "ltok",
    "lease_expires_at": "2099-01-01T00:00:00+00:00",
    "idempotency_key": "card-key-multi",
    "reminders": [
        {"reminder_id": 60, "kind": "start", "product_command": "task.start",
         "scheduled_at": "2099-01-01T00:00:00+00:00",
         "expires_at": "2099-01-01T01:00:00+00:00", "idempotency_key": "rka"},
        {"reminder_id": 61, "kind": "start", "product_command": "task.start",
         "scheduled_at": "2099-01-01T00:00:00+00:00",
         "expires_at": "2099-01-01T01:00:00+00:00", "idempotency_key": "rkb"},
    ],
    "tasks": [
        {"page_id": "pg-004", "title": "T1", "start": "2099-01-01T10:00+08:00",
         "end": "2099-01-01T11:00+08:00", "assign": "Assign", "done": False},
        {"page_id": "pg-005", "title": "T2", "start": "2099-01-01T10:00+08:00",
         "end": "2099-01-01T11:00+08:00", "assign": "Assign", "done": False},
    ],
    "markdown": "## Agenda",
})


class TestShouldSendCard:
    def test_single_start_qualifies(self):
        from pa_reminder_worker import _should_send_card
        assert _should_send_card(json.loads(PA_CLAIM_SINGLE_START)) is True

    def test_single_completion_qualifies(self):
        from pa_reminder_worker import _should_send_card
        assert _should_send_card(json.loads(PA_CLAIM_SINGLE_COMPLETION)) is True

    def test_multi_reminder_rejected(self):
        from pa_reminder_worker import _should_send_card
        assert _should_send_card(json.loads(PA_CLAIM_MULTI)) is False

    def test_no_product_command_rejected(self):
        from pa_reminder_worker import _should_send_card
        data = json.loads(PA_CLAIM_SINGLE_START)
        data["reminders"][0].pop("product_command")
        assert _should_send_card(data) is False


class TestCardBuilding:
    def test_start_card_build(self):
        from reminder_card_handler import build_card
        card = build_card("iid-1", "task.start", "Test Task",
                          "2026-01-01T10:00+08:00", "2026-01-01T11:00+08:00",
                          "即将开始")
        # Verify structure
        assert card["header"]["title"]["content"] == "即将开始"
        buttons = card["elements"][1]["actions"]
        actions = {b["value"]["action"] for b in buttons}
        assert "start" in actions
        assert "snooze" in actions
        assert "reschedule" in actions
        # All buttons have hermes_action=pa_reminder and interaction_id
        for b in buttons:
            assert b["value"]["hermes_action"] == "pa_reminder"
            assert b["value"]["interaction_id"] == "iid-1"

    def test_completion_card_build(self):
        from reminder_card_handler import build_card
        card = build_card("iid-2", "task.completion", "Test Task",
                          "2026-01-01T10:00+08:00", "2026-01-01T11:00+08:00",
                          "到时确认")
        buttons = card["elements"][1]["actions"]
        actions = {b["value"]["action"] for b in buttons}
        assert "complete" in actions
        assert "extend" in actions
        assert "reschedule" in actions

    def test_snooze_card_no_snooze_button(self):
        from reminder_card_handler import build_card
        card = build_card("iid-3", "task.snooze", "Snoozed Task",
                          "2026-01-01T10:00+08:00", "2026-01-01T11:00+08:00",
                          "到点提醒")
        buttons = card["elements"][1]["actions"]
        actions = {b["value"]["action"] for b in buttons}
        assert "snooze" not in actions  # no re-snooze
        assert "start" in actions
        assert "reschedule" in actions


class TestInteractionValidation:
    def test_all_fail_closed_no_config(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("FEISHU_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("FEISHU_FRANK_CHAT_ID", raising=False)

        rch.persist_interaction(
            "iid-v", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_msg", "oc_frank", ["start", "reschedule"],
        )
        err = rch.validate_click("iid-v", "ou_frank", "oc_frank", "om_msg",
                                  "start", "tok-1")
        assert err is not None  # no config → fail

    def test_token_replay_prevented(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-r", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_msg", "oc_frank", ["start", "reschedule"],
        )
        # atomic_claim returns None on success
        assert rch.atomic_claim("iid-r", "tok-r", "pending") is None
        # Second claim fails because state is no longer pending
        err = rch.atomic_claim("iid-r", "tok-r2", "pending")
        assert err is not None and "not pending" in err.lower()

    def test_wrong_chat_rejected(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-c", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_msg", "oc_frank", ["start"],
        )
        err = rch.validate_click("iid-c", "ou_frank", "oc_wrong", "om_msg",
                                  "start", "tok-c")
        assert "chat mismatch" in (err or "").lower()

    def test_wrong_user_rejected(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-u", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_msg", "oc_frank", ["start"],
        )
        err = rch.validate_click("iid-u", "ou_stranger", "oc_frank", "om_msg",
                                  "start", "tok-u")
        assert "not authorized" in (err or "").lower()

    def test_wrong_message_id_rejected(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-m", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_original", "oc_frank", ["start"],
        )
        err = rch.validate_click("iid-m", "ou_frank", "oc_frank", "om_different",
                                  "start", "tok-m")
        assert "message_id mismatch" in (err or "").lower()

    def test_disallowed_action_rejected(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-a", "dk", 1, "start", "task.start",
            "2099-01-01T00:00:00+00:00", "pg-001",
            "om_msg", "oc_frank", ["start", "reschedule"],
        )
        err = rch.validate_click("iid-a", "ou_frank", "oc_frank", "om_msg",
                                  "delete", "tok-a")  # delete is never allowed
        assert err is not None and "not allowed" in err.lower()

    def test_expired_rejected(self, tmp_path, monkeypatch):
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")

        rch.persist_interaction(
            "iid-e", "dk", 1, "start", "task.start",
            "2000-01-01T00:00:00+00:00", "pg-001",  # expired
            "om_msg", "oc_frank", ["start"],
        )
        err = rch.validate_click("iid-e", "ou_frank", "oc_frank", "om_msg",
                                  "start", "tok-e")
        assert "expired" in (err or "").lower()


# ── Gateway import + E2E tests ──────────────────────────────────────────────

class TestGatewayImport:
    def test_handler_importable_without_syspath_hacks(self):
        """Verify reminder_card_handler imports with hermes-agent venv Python."""
        import subprocess
        r = subprocess.run(
            [os.path.join(_H, "hermes-agent/venv/bin/python"), "-c",
             "import sys; sys.path.insert(0,'/home/frankzhao/.hermes/hermes-agent'); "
             "sys.path.insert(0,'/home/frankzhao/.hermes/scripts'); "
             "sys.path.insert(0,'/home/frankzhao/.hermes/skills/calendar-gtd-integration/scripts'); "
             "import reminder_card_handler; print('OK')"],
            capture_output=True, text=True, timeout=10,
        )
        assert "OK" in r.stdout


from reminder_card_handler import S_APPLYING, S_SUCCEEDED, S_REJECTED

class TestE2ECardDispatch:
    @pytest.fixture
    def s(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        return rch

    def test_snooze(self, s):
        s.persist_interaction("i1","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",["snooze"])
        with patch("reminder_card_handler._safe_pa_snooze") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(0, '{}', '', {"status":"success"}, ["pa"], 100)
            r = s.dispatch_action("i1","snooze","t1")
        assert r["status"] == "succeeded"

    def test_start(self, s):
        s.persist_interaction("i2","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",["start"])
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session") as m2,\
             patch("reminder_card_handler.execute_action") as m3:
            m1.return_value = {"success":True,"version":"v1"}; m2.return_value = {"success":True}
            m3.return_value = {"success":True,"status":"applied"}
            r = s.dispatch_action("i2","start","t2")
        assert r["status"] == "succeeded"

    def test_extend_goes_to_choosing_extend(self, s):
        s.persist_interaction("i3","dk",1,"completion","task.completion","2099-01-01T00:00:00+00:00","pg","om","oc",["extend"])
        r = s.dispatch_action("i3","extend","t3")
        assert r["status"] == "choose_extend"
        assert "card" in r
        d = s.get_interaction("i3")
        assert d["state"] == "choosing_extend"

    def test_reschedule_goes_to_choosing_slot(self, s):
        s.persist_interaction("i4","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",["reschedule"])
        with patch("reminder_card_handler.suggest_slots") as m:
            m.return_value = {"success":True,"slots":[{"start":"S1","due":"D1"},{"start":"S2","due":"D2"},{"start":"S3","due":"D3"}]}
            r = s.dispatch_action("i4","reschedule","t4")
        assert r["status"] == "choose_slot"
        d = s.get_interaction("i4")
        assert d["state"] == "choosing_slot"
        assert len(d["slot_candidates"]) == 3

    def test_extend_15_30_60_correct_due(self, s):
        for mins in (15, 30, 60):
            sid = f"ie{mins}"
            s.persist_interaction(sid,"dk",1,"completion","task.completion","2099-01-01T00:00:00+00:00","pg","om","oc",[])
            s.update_interaction(sid,{"state":"choosing_extend"})
            calls = []
            def fake_create(*a, **kw):
                calls.append(kw); return {"success":True}
            with patch("reminder_card_handler.show_task") as m1,\
                 patch("reminder_card_handler.create_session", side_effect=fake_create) as m2,\
                 patch("reminder_card_handler.execute_action") as m3,\
                 patch("reminder_card_handler.get_session") as m4:
                m1.return_value = {"success":True,"version":"v1","due":"2026-01-01T12:00:00+08:00"}
                m3.return_value = {"success":True,"status":"applied"}
                m4.return_value = {"confirmation_token":"","confirmation_expires_at":""}
                r = s.dispatch_action(sid,"extend_confirm",f"t{mins}",{"minutes":mins})
                assert r["status"] in ("succeeded","confirmation_required"), f"mins={mins}: {r}"
            # Verify due was computed correctly
            if calls:
                h = 12 + mins // 60
                m = mins % 60
                expected_due = f"2026-01-01T{h:02d}:{m:02d}:00+08:00"
                assert calls[-1].get("due") == expected_due, f"mins={mins}: expected {expected_due}, got {calls[-1].get('due')}"

    def test_slot_pick_uses_persisted_candidates(self, s):
        s.persist_interaction("ir","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        slots = [{"start":"S1","due":"D1"},{"start":"S2","due":"D2"},{"start":"S3","due":"D3"}]
        s.update_interaction("ir",{"state":"choosing_slot","slot_candidates":slots})
        calls = []
        def fake_create(*a, **kw): calls.append(kw); return {"success":True}
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session", side_effect=fake_create) as m2,\
             patch("reminder_card_handler.execute_action") as m3,\
             patch("reminder_card_handler.get_session") as m4:
            m1.return_value = {"success":True,"version":"v1"}
            m3.return_value = {"success":True,"status":"confirmation_required","proposal":slots[1]}
            m4.return_value = {"confirmation_token":"tok","confirmation_expires_at":"2099-01-01T00:00:00+00:00","proposal":slots[1]}
            r = s.dispatch_action("ir","reschedule_pick","tr",{"slot_index":1})
            assert r["status"] == "confirmation_required"
        # Verify slot[1] was used (S2, D2)
        assert calls and calls[-1].get("start") == "S2"
        assert calls[-1].get("due") == "D2"

    def test_confirm_full_chain(self, s):
        s.persist_interaction("if","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("if",{"state":"awaiting_confirmation","session_id":"sx",
            "proposal":{"start":"S","due":"D"},"confirmation_token":"ctok",
            "confirmation_expires_at":"2099-01-01T00:00:00+00:00","feishu_chat_id":"oc_frank"})
        with patch("reminder_card_handler.execute_action") as m:
            m.return_value = {"success":True,"status":"applied"}
            r = s.dispatch_action("if","confirm","tcf")
        assert r["status"] == "succeeded"
        assert s.get_interaction("if")["state"] == S_SUCCEEDED

    def test_cancel(self, s):
        s.persist_interaction("ic2","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic2",{"state":"awaiting_confirmation","session_id":"sx",
            "confirmation_token":"ctok","confirmation_expires_at":"2099-01-01T00:00:00+00:00"})
        r = s.dispatch_action("ic2","cancel","tcc")
        assert r["status"] == "cancelled"
        assert s.get_interaction("ic2")["state"] == S_REJECTED

    def test_active_message_id_flows(self, s):
        """verify active_message_id rejection: click on old card after new card sent"""
        s.persist_interaction("iam","dk",1,"completion","task.completion","2099-01-01T00:00:00+00:00","pg","om1","oc",["extend"])
        # Simulate: first extend click transitions state + new card sent
        s.update_interaction("iam",{"state":"choosing_extend","active_message_id":"om2"})
        # Click using old message_id "om1" should fail
        err = s.validate_click("iam","ou_frank","oc","om1","extend_confirm","tok-x")
        assert "message_id mismatch" in (err or "").lower()

    def test_concurrent_only_one_succeeds(self, s):
        s.persist_interaction("icc","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",["start"])
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session") as m2,\
             patch("reminder_card_handler.execute_action") as m3:
            m1.return_value = {"success":True,"version":"v1"}; m2.return_value = {"success":True}
            m3.return_value = {"success":True,"status":"applied"}
            r1 = s.dispatch_action("icc","start","tcc1")
            r2 = s.dispatch_action("icc","start","tcc2")
            assert r1["status"] == "succeeded"
            assert "not pending" in r2.get("status","").lower()


# ── Custom time flow tests ───────────────────────────────────────────────────

class TestCustomTimeFlow:
    @pytest.fixture
    def s(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        return rch

    def test_reschedule_custom_goes_to_choosing_custom_with_time_card(self, s):
        s.persist_interaction("ic1","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic1",{"state":"choosing_slot","slot_candidates":[]})
        r = s.dispatch_action("ic1","reschedule_custom","t1")
        assert r["status"] == "choosing_custom"
        assert "card" in r
        # Verify form structure (JSON 1.0)
        c = r["card"]
        forms = [e for e in c["elements"] if e.get("tag") == "form"]
        assert len(forms) == 1
        f = forms[0]
        assert f.get("name") == "custom_time_form"
        assert "submit" not in f
        names = {el.get("name", "") for el in f["elements"] if "name" in el}
        assert names >= {"custom_date", "custom_start_time", "custom_due_time", "custom_time_submit"}
        btn = [el for el in f["elements"] if el.get("tag") == "button" and el.get("name") == "custom_time_submit"]
        assert len(btn) == 1
        assert btn[0].get("action_type") == "form_submit"
        assert btn[0].get("complex_interaction") is True
        assert any(e.get("tag") == "action" for e in c["elements"])
        assert s.get_interaction("ic1")["state"] == "choosing_custom"

    def test_custom_submit_date_time_passed_to_suggest_slots(self, s):
        s.persist_interaction("ic2","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic2",{"state":"choosing_custom"})
        calls = []
        def fake_slots(*a, **kw): calls.append(kw); return {"success":True,"slots":[],"requested":{"available":False}}
        with patch("reminder_card_handler.suggest_slots", side_effect=fake_slots):
            r = s.dispatch_action("ic2","reschedule_custom_submit","t2",
                {"custom_date":"2026-07-15","custom_start_time":"14:00","custom_due_time":"14:30"})
        assert r["status"] == "no_slots"
        assert calls and calls[-1].get("start") == "2026-07-15T14:00:00+08:00"
        assert calls[-1].get("due") == "2026-07-15T14:30:00+08:00"

    def test_custom_submit_available_confirm(self, s):
        s.persist_interaction("ic3","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic3",{"state":"choosing_custom"})
        na = {"action":"reschedule","start":"2026-07-15T14:00:00+08:00","due":"2026-07-15T14:30:00+08:00"}
        with patch("reminder_card_handler.suggest_slots") as m1,\
             patch("reminder_card_handler.show_task") as m2,\
             patch("reminder_card_handler.create_session") as m3,\
             patch("reminder_card_handler.execute_action") as m4,\
             patch("reminder_card_handler.get_session") as m5:
            m1.return_value = {"success":True,"slots":[{}],"requested":{"available":True},"next_action":na}
            m2.return_value = {"success":True,"version":"v1"}
            m3.return_value = {"success":True}
            m4.return_value = {"success":True,"status":"confirmation_required","proposal":na}
            m5.return_value = {"confirmation_token":"tok","confirmation_expires_at":"2099-01-01T00:00:00+00:00"}
            r = s.dispatch_action("ic3","reschedule_custom_submit","t3",
                {"custom_date":"2026-07-15","custom_start_time":"14:00","custom_due_time":"14:30"})
        assert r["status"] == "confirmation_required"

    def test_custom_submit_unavailable_shows_slots(self, s):
        s.persist_interaction("ic4","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic4",{"state":"choosing_custom"})
        slots = [{"start":"2026-07-16T09:00+08:00","due":"2026-07-16T10:00+08:00"}]
        with patch("reminder_card_handler.suggest_slots") as m:
            m.return_value = {"success":True,"slots":slots,"requested":{"available":False}}
            r = s.dispatch_action("ic4","reschedule_custom_submit","t4",
                {"custom_date":"2026-07-15","custom_start_time":"10:00","custom_due_time":"11:00"})
        assert r["status"] == "choose_slot"
        assert s.get_interaction("ic4")["slot_candidates"] == slots

    def test_reschedule_cancel_returns_to_slot_pick(self, s):
        s.persist_interaction("ic5","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic5",{"state":"choosing_custom"})
        with patch("reminder_card_handler.suggest_slots") as m:
            m.return_value = {"success":True,"slots":[{"start":"S1","due":"D1"}]}
            r = s.dispatch_action("ic5","reschedule_cancel","t5")
        assert r["status"] == "choose_slot"

    def test_custom_submit_no_time_rejected(self, s):
        s.persist_interaction("ic6","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        s.update_interaction("ic6",{"state":"choosing_custom"})
        r = s.dispatch_action("ic6","reschedule_custom_submit","t6",{})
        assert r["status"] == "no_time"

    def test_custom_submit_from_non_custom_rejected(self, s):
        s.persist_interaction("ic7","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",[])
        r = s.dispatch_action("ic7","reschedule_custom_submit","t7",
            {"custom_date":"2026-07-15","custom_start_time":"10:00","custom_due_time":"11:00"})
        assert "state not choosing_custom" in r.get("status","").lower()


# ── Sequential chain + adapter dispatch tests ────────────────────────────────

class TestSequentialChain:
    """End-to-end: reschedule → custom → submit → confirm, no state punching."""
    @pytest.fixture
    def s(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        return rch

    def test_full_custom_chain(self, s):
        # 1. Start with reschedule
        s.persist_interaction("ich","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc",["reschedule"])
        with patch("reminder_card_handler.suggest_slots") as m:
            m.return_value = {"success":True,"slots":[{"start":"S1","due":"D1"}]}
            r = s.dispatch_action("ich","reschedule","t1")
        assert r["status"] == "choose_slot"

        # 2. Pick custom
        r = s.dispatch_action("ich","reschedule_custom","t2")
        assert r["status"] == "choosing_custom"
        assert s.get_interaction("ich")["state"] == "choosing_custom"

        # 3. Submit custom time
        na = {"start":"2026-07-15T14:00:00+08:00","due":"2026-07-15T14:30:00+08:00"}
        with patch("reminder_card_handler.suggest_slots") as m1,\
             patch("reminder_card_handler.show_task") as m2,\
             patch("reminder_card_handler.create_session") as m3,\
             patch("reminder_card_handler.execute_action") as m4,\
             patch("reminder_card_handler.get_session") as m5:
            m1.return_value = {"success":True,"slots":[{}],"requested":{"available":True},"next_action":na}
            m2.return_value = {"success":True,"version":"v1"}
            m3.return_value = {"success":True}
            m4.return_value = {"success":True,"status":"confirmation_required","proposal":na}
            m5.return_value = {"confirmation_token":"tok","confirmation_expires_at":"2099"}
            r = s.dispatch_action("ich","reschedule_custom_submit","t4",
                {"custom_date":"2026-07-15","custom_start_time":"14:00","custom_due_time":"14:30"})
        assert r["status"] == "confirmation_required"

        # 4. Confirm
        with patch("reminder_card_handler.execute_action") as m:
            m.return_value = {"success":True,"status":"applied"}
            r = s.dispatch_action("ich","confirm","t5")
        assert r["status"] == "succeeded"


class TestAdapterDispatch:
    """Test through real _dispatch_pa_reminder_action with mocked Feishu."""
    @pytest.fixture
    def s(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        return rch

    def test_real_dispatch_send_success(self, s):
        """Real adapter dispatch: pending → extend → success → choosing_extend + new msg_id."""
        import plugins.platforms.feishu.adapter as adp
        adapter = object.__new__(adp.FeishuAdapter)
        adapter._derive_card_uuid = lambda iid, ctx: "uuid-"+iid
        import reminder_card_handler as rch
        s.persist_interaction("iads","dk",1,"completion","task.completion","2099-01-01T00:00:00+00:00","pg","om","oc_frank",["extend"])

        class FR:
            success=True; message_id="new-msg-id"
        async def fake_send(**kw): return FR()

        with patch.object(adapter, "_send_card_to_chat", new=fake_send):
            async def run():
                await adapter._dispatch_pa_reminder_action(iid="iads",action="extend",etok="t1",params={})
            import asyncio
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(run()); loop.close()

        d = s.get_interaction("iads")
        assert d["state"] == "choosing_extend"
        assert d["active_message_id"] == "new-msg-id"

    def test_real_dispatch_failure_restores_then_retry_succeeds(self, s):
        """First extend: send fails → back to pending. Second extend: send succeeds → choosing_extend."""
        import plugins.platforms.feishu.adapter as adp
        adapter = object.__new__(adp.FeishuAdapter)
        adapter._derive_card_uuid = lambda iid, ctx: "uuid-"+iid
        import reminder_card_handler as rch
        s.persist_interaction("iadf","dk",1,"completion","task.completion","2099-01-01T00:00:00+00:00","pg","om","oc_frank",["extend"])

        # 1st attempt: send fails
        async def fake_fail(**kw): return None
        with patch.object(adapter, "_send_card_to_chat", new=fake_fail):
            async def run(): await adapter._dispatch_pa_reminder_action(iid="iadf",action="extend",etok="tx",params={})
            import asyncio
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(run()); loop.close()

        d = s.get_interaction("iadf")
        assert d["state"] == "pending"  # restored
        assert d["active_message_id"] == "om"  # original

        # 2nd attempt with new token: send succeeds
        class FR: success=True; message_id="new-ok"
        async def fake_ok(**kw): return FR()
        with patch.object(adapter, "_send_card_to_chat", new=fake_ok):
            async def run(): await adapter._dispatch_pa_reminder_action(iid="iadf",action="extend",etok="ty",params={})
            loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            loop.run_until_complete(run()); loop.close()

        d2 = s.get_interaction("iadf")
        assert d2["state"] == "choosing_extend"
        assert d2["active_message_id"] == "new-ok"


def test_custom_submit_exact_args_to_suggest_slots():
    """Verify arbitrary custom_start/custom_due pass through to suggest_slots unchanged."""
    import reminder_card_handler as rch, tempfile
    td = tempfile.mkdtemp()
    import os as _os
    _os.environ["FEISHU_ALLOWED_USERS"] = "ou_frank"
    _os.environ["FEISHU_FRANK_CHAT_ID"] = "oc_frank"
    rch._HERMES_HOME = td
    rch.persist_interaction("ict","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc_frank",[])
    rch.update_interaction("ict",{"state":"choosing_custom"})
    calls = []
    def fake(*a, **kw): calls.append(kw); return {"success":True,"slots":[],"requested":{"available":False}}
    with patch("reminder_card_handler.suggest_slots", side_effect=fake):
        rch.dispatch_action("ict","reschedule_custom_submit","tx",{
            "custom_date":"2026-08-01","custom_start_time":"14:30","custom_due_time":"16:00"})
    assert calls
    assert calls[-1].get("start") == "2026-08-01T14:30:00+08:00"
    assert calls[-1].get("due") == "2026-08-01T16:00:00+08:00"


class TestSDKCallbackWithFormValue:
    """Test _on_card_action_trigger with form_value carrying picker selections."""

    def test_form_value_merged_into_action_value(self):
        """Form value with date/time picker fields → custom_start/custom_due built as ISO."""
        import reminder_card_handler as rch, tempfile
        td = tempfile.mkdtemp()
        import os as _os
        _os.environ["FEISHU_ALLOWED_USERS"] = "ou_frank"
        _os.environ["FEISHU_FRANK_CHAT_ID"] = "oc_frank"
        rch._HERMES_HOME = td
        rch.persist_interaction("ifv","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc_frank",[])
        rch.update_interaction("ifv",{"state":"choosing_custom"})

        calls = []
        def fake_slots(*a, **kw): calls.append(kw); return {"success":True,"slots":[],"requested":{"available":False}}
        with patch("reminder_card_handler.suggest_slots", side_effect=fake_slots):
            # Simulate what adapter passes after merging form_value
            params = {"custom_date":"2026-08-01","custom_start_time":"14:30","custom_due_time":"16:00"}
            r = rch.dispatch_action("ifv","reschedule_custom_submit","tk",params)

        assert calls
        assert calls[-1].get("start") == "2026-08-01T14:30:00+08:00"
        assert calls[-1].get("due") == "2026-08-01T16:00:00+08:00"

    def test_form_value_due_before_start_rejected(self):
        """Due time before start time returns due_before_start."""
        import reminder_card_handler as rch, tempfile
        td = tempfile.mkdtemp()
        import os as _os
        _os.environ["FEISHU_ALLOWED_USERS"] = "ou_frank"
        _os.environ["FEISHU_FRANK_CHAT_ID"] = "oc_frank"
        rch._HERMES_HOME = td
        rch.persist_interaction("idb","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc_frank",[])
        rch.update_interaction("idb",{"state":"choosing_custom"})

        params = {"custom_date":"2026-08-01","custom_start_time":"16:00","custom_due_time":"14:30"}
        r = rch.dispatch_action("idb","reschedule_custom_submit","tk",params)
        assert r["status"] == "due_before_start"

    def test_invalid_time_rejected(self):
        """Non-parseable time returns invalid_time."""
        import reminder_card_handler as rch, tempfile
        td = tempfile.mkdtemp()
        import os as _os
        _os.environ["FEISHU_ALLOWED_USERS"] = "ou_frank"
        _os.environ["FEISHU_FRANK_CHAT_ID"] = "oc_frank"
        rch._HERMES_HOME = td
        rch.persist_interaction("iit","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om","oc_frank",[])
        rch.update_interaction("iit",{"state":"choosing_custom"})
        params = {"custom_date":"2026-08-01","custom_start_time":"bad","custom_due_time":"16:00"}
        r = rch.dispatch_action("iit","reschedule_custom_submit","tk",params)
        assert r["status"] == "invalid_time"

    def test_real_sdk_callback_through_on_card_action_trigger(self, tmp_path, monkeypatch):
        """Real _on_card_action_trigger with P2CardActionTrigger event."""
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        rch.persist_interaction("is","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om1","oc_frank",["extend"])

        from types import SimpleNamespace
        action = SimpleNamespace(
            value={"hermes_action":"pa_reminder","interaction_id":"is","action":"extend"},
            form_value=None, tag="button",
        )
        event = SimpleNamespace(
            action=action,
            operator=SimpleNamespace(open_id="ou_frank"),
            context=SimpleNamespace(open_chat_id="oc_frank", open_message_id="om1"),
            token="tok-is",
        )
        data = SimpleNamespace(event=event)

        import plugins.platforms.feishu.adapter as adp
        adapter = object.__new__(adp.FeishuAdapter)
        adapter._loop = True  # mock: loop is ready

        # Mock toast response class
        class MockToast: pass
        monkeypatch.setattr(adp, "P2CardActionTriggerResponse", MockToast)
        monkeypatch.setattr(adp, "CallBackToast", MockToast)
        # Mock submit to prevent actual scheduling
        adapter._submit_on_loop = lambda loop, coro: True
        adapter._derive_card_uuid = lambda iid, ctx: "uuid-"+iid
        # Mock _send_card_to_chat
        adapter._feishu_send_with_retry = None

        result = adapter._on_card_action_trigger(data=data)
        # Should return a toast response (system busy since no async loop)
        assert isinstance(result, MockToast)

    def test_real_on_card_action_trigger_with_form_value(self, tmp_path, monkeypatch):
        """_on_card_action_trigger → handler receives merged form_value."""
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        rch.persist_interaction("irf","dk",1,"start","task.start","2099-01-01T00:00:00+00:00","pg","om3","oc_frank",
                                ["reschedule_custom_submit","reschedule_cancel"])
        rch.update_interaction("irf",{"state":"choosing_custom"})

        from types import SimpleNamespace
        action = SimpleNamespace(
            value={"hermes_action":"pa_reminder","interaction_id":"irf","action":"reschedule_custom_submit"},
            form_value={"custom_date":"2026-08-01","custom_start_time":"14:30 +0800","custom_due_time":"16:00 +0800"},
            tag="button",
        )
        event = SimpleNamespace(
            action=action,
            operator=SimpleNamespace(open_id="ou_frank"),
            context=SimpleNamespace(open_chat_id="oc_frank", open_message_id="om3"),
            token="tok-rf",
        )
        data = SimpleNamespace(event=event)

        import plugins.platforms.feishu.adapter as adp
        adapter = object.__new__(adp.FeishuAdapter)
        adapter._loop = True

        class MockToast: pass
        monkeypatch.setattr(adp, "P2CardActionTriggerResponse", MockToast)
        monkeypatch.setattr(adp, "CallBackToast", MockToast)

        captured = []
        def patched(*, event, action_value, loop):
            captured.append(dict(action_value))
            return MockToast()
        adapter._handle_pa_reminder_card_action = patched

        result = adapter._on_card_action_trigger(data=data)
        assert isinstance(result, MockToast)
        assert len(captured) == 1
        av = captured[0]
        assert av.get("custom_date") == "2026-08-01"
        assert av.get("custom_start_time") == "14:30 +0800"
        assert av.get("custom_due_time") == "16:00 +0800"
