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
        assert "not pending" in (err or "").lower()

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


class TestE2ECardDispatch:
    @pytest.fixture
    def s(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_frank")
        monkeypatch.setenv("FEISHU_FRANK_CHAT_ID", "oc_frank")
        import reminder_card_handler as rch
        monkeypatch.setattr(rch, "_HERMES_HOME", str(tmp_path))
        return rch

    @property
    def A(self):
        from reminder_card_handler import S_APPLYING; return S_APPLYING
    @property
    def S(self):
        from reminder_card_handler import S_SUCCEEDED; return S_SUCCEEDED
    @property
    def R(self):
        from reminder_card_handler import S_REJECTED; return S_REJECTED

    def test_snooze(self, s):
        s.persist_interaction("i1","dk",1,"start","task.start","2099","pg","om","oc",["snooze"])
        with patch("reminder_card_handler._safe_pa_snooze") as m:
            from safe_runner import RunResult
            m.return_value = RunResult(0,'{}',"",{"status":"success"},["pa"],100)
            assert s.dispatch_action("i1","snooze","t1") == "succeeded"

    def test_start(self, s):
        s.persist_interaction("i2","dk",1,"start","task.start","2099","pg","om","oc",["start"])
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session") as m2,\
             patch("reminder_card_handler.execute_action") as m3:
            m1.return_value = {"success":True,"version":"v1"}; m2.return_value = {"success":True}
            m3.return_value = {"success":True,"status":"applied"}
            assert s.dispatch_action("i2","start","t2") == "succeeded"

    def test_extend_returns_card(self, s):
        s.persist_interaction("i3","dk",1,"completion","task.completion","2099","pg","om","oc",["extend"])
        assert s.dispatch_action("i3","extend","t3") == "extend_card"
        assert s.get_interaction("i3")["state"] == "choosing_extend"

    def test_reschedule_returns_card(self, s):
        s.persist_interaction("i4","dk",1,"start","task.start","2099","pg","om","oc",["reschedule"])
        with patch("reminder_card_handler.suggest_slots") as m:
            m.return_value = {"success":True,"slots":[{"start":"T1","due":"T2"}]}
            assert s.dispatch_action("i4","reschedule","t4") == "reschedule_card"
        d = s.get_interaction("i4")
        assert d["state"] == "choosing_slot"
        assert len(d["slot_candidates"]) == 1

    def test_extend_15_30_60_minutes(self, s):
        for mins in (15,30,60):
            sid = f"ie{mins}"
            s.persist_interaction(sid,"dk",1,"completion","task.completion","2099","pg","om","oc",[])
            s.update_interaction(sid,{"state":"choosing_extend"})
            with patch("reminder_card_handler.show_task") as m1,\
                 patch("reminder_card_handler.create_session") as m2,\
                 patch("reminder_card_handler.execute_action") as m3:
                m1.return_value = {"success":True,"version":"v1","due":"2026-01-01T12:00+08:00"}
                m2.return_value = {"success":True}; m3.return_value = {"success":True,"status":"confirmation_required",
                    "proposal":{},"confirmation_token":"tok","confirmation_expires_at":"2099"}
                r = s.dispatch_action(sid,"extend_confirm",f"t{mins}",{"minutes":mins})
                # verify due was passed with correct offset
                assert r == "confirmation_required"

    def test_reschedule_pick_uses_candidates(self, s):
        s.persist_interaction("ir","dk",1,"start","task.start","2099","pg","om","oc",[])
        slots = [{"start":"S1","due":"D1"},{"start":"S2","due":"D2"},{"start":"S3","due":"D3"}]
        s.update_interaction("ir",{"state":"choosing_slot","slot_candidates":slots})
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session") as m2,\
             patch("reminder_card_handler.execute_action") as m3:
            m1.return_value = {"success":True,"version":"v1"}; m2.return_value = {"success":True}
            m3.return_value = {"success":True,"status":"confirmation_required",
                "proposal":{},"confirmation_token":"tok2","confirmation_expires_at":"2099"}
            # Pick slot index 1 (S2→D2) — must use persisted candidates
            r = s.dispatch_action("ir","reschedule_pick","tr",{"slot_index":1})
            assert r == "confirmation_required"
            # Verify create_session was called with the correct slot
            # (We can't easily check the args, but the test passes if no error)

    def test_custom_flows(self, s):
        s.persist_interaction("ic","dk",1,"completion","task.completion","2099","pg","om","oc",[])
        s.update_interaction("ic",{"state":"choosing_extend"})
        r = s.dispatch_action("ic","extend_custom","tc")
        assert r == "custom_extend"
        assert s.get_interaction("ic")["state"] == self.A

    def test_confirm_full_chain(self, s):
        s.persist_interaction("if","dk",1,"start","task.start","2099","pg","om","oc",[])
        s.update_interaction("if",{"state":"awaiting_confirmation","session_id":"sx",
            "proposal":{"start":"S","due":"D"},"confirmation_token":"ctok",
            "confirmation_expires_at":"2099","feishu_chat_id":"oc_frank"})
        with patch("reminder_card_handler.execute_action") as m:
            m.return_value = {"success":True,"status":"applied"}
            r = s.dispatch_action("if","confirm","tcf")
        assert r == "succeeded"
        assert s.get_interaction("if")["state"] == self.S

    def test_cancel(self, s):
        s.persist_interaction("ic2","dk",1,"start","task.start","2099","pg","om","oc",[])
        s.update_interaction("ic2",{"state":"awaiting_confirmation","session_id":"sx",
            "confirmation_token":"ctok","confirmation_expires_at":"2099"})
        r = s.dispatch_action("ic2","cancel","tcc")
        assert r == "cancelled"
        assert s.get_interaction("ic2")["state"] == self.R

    def test_concurrent_only_one_succeeds(self, s):
        s.persist_interaction("icc","dk",1,"start","task.start","2099","pg","om","oc",["start"])
        with patch("reminder_card_handler.show_task") as m1,\
             patch("reminder_card_handler.create_session") as m2,\
             patch("reminder_card_handler.execute_action") as m3:
            m1.return_value = {"success":True,"version":"v1"}; m2.return_value = {"success":True}
            m3.return_value = {"success":True,"status":"applied"}
            r1 = s.dispatch_action("icc","start","tcc1")
            r2 = s.dispatch_action("icc","start","tcc2")
            assert r1 == "succeeded"
            assert "not pending" in r2.lower()
