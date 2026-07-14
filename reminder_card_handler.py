#!/usr/bin/env python3
"""Reminder card interaction handler — Calendar GTD / PA integration.

Importable from the hermes-agent root as ``reminder_card_handler``.

Builds interactive cards, persists interaction state, validates clicks,
atomically transitions state via SQLite CAS, and dispatches PA actions.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERMES_HOME = os.path.expanduser("~/.hermes")

import sys as _sys
for _p in (
    os.path.join(_HERMES_HOME, "scripts"),
    os.path.join(_HERMES_HOME, "hermes-agent"),
    os.path.join(_HERMES_HOME, "skills/calendar-gtd-integration/scripts"),
):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from safe_runner import pa_snooze as _safe_pa_snooze
from task_action_handler import (
    show_task, create_session, execute_action, suggest_slots,
    S_PROPOSED, S_AWAITING,
)
from hermes_state import SessionDB

# ── Constants ───────────────────────────────────────────────────────────────

INTERACTION_PREFIX = "pa:interaction:"
TOKEN_PREFIX = "pa:interaction:token:"

S_PENDING = "pending"
S_APPLYING = "applying"
S_SUCCEEDED = "succeeded"
S_CONFLICT = "conflict"
S_REJECTED = "rejected"

CARD_ACTIONS = frozenset({"start", "snooze", "reschedule", "complete", "extend"})


# ── DB helpers ──────────────────────────────────────────────────────────────

def _get_db() -> SessionDB:
    return SessionDB(Path(os.path.join(_HERMES_HOME, "state.db")))


def _iid_key(iid: str) -> str:
    return f"{INTERACTION_PREFIX}{iid}"


def _token_key(token: str) -> str:
    return f"{TOKEN_PREFIX}{token}"


# ── Card building ───────────────────────────────────────────────────────────

def build_card(
    interaction_id: str, product_command: str,
    task_title: str, task_start: Optional[str], task_due: Optional[str],
    heading: str,
) -> dict:
    if product_command == "task.snooze":
        action_defs = [("现在开始","start","primary"),("重新安排时间","reschedule","default")]
    elif product_command == "task.start":
        label = "到 HH:MM 再提醒"
        if task_start:
            try:
                label = f"到 {datetime.fromisoformat(task_start).strftime('%H:%M')} 再提醒"
            except (ValueError, TypeError): pass
        action_defs = [("现在开始","start","primary"),(label,"snooze","default"),("重新安排时间","reschedule","default")]
    elif product_command == "task.completion":
        action_defs = [("标记完成","complete","primary"),("延长时间","extend","default"),("重新安排时间","reschedule","default")]
    else:
        return {"config":{"wide_screen_mode":True},"header":{"title":{"content":"Error","tag":"plain_text"},"template":"red"},"elements":[{"tag":"markdown","content":"Unknown reminder type."}]}

    time_line = ""
    if task_start: time_line += f"开始: {task_start}"
    if task_due: time_line += f"  →  完成: {task_due}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": heading, "tag": "plain_text"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"**{task_title}**\n{time_line}"},
            {"tag": "action", "actions": [
                {"tag":"button","text":{"tag":"plain_text","content":l},"type":t,"value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":a}}
                for l, a, t in action_defs
            ]},
        ],
    }


def build_extend_card(interaction_id: str, task_title: str) -> dict:
    """Secondary card: extend time options."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "延长时间", "tag": "plain_text"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"**{task_title}**\n选择延长时间："},
            {"tag": "action", "actions": [
                {"tag":"button","text":{"tag":"plain_text","content":l},"type":t,"value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":"extend_confirm","minutes":m}}
                for l, m, t in [("+15 分钟",15,"primary"),("+30 分钟",30,"default"),("+60 分钟",60,"default"),("自定义","custom","default")]
            ]},
        ],
    }


def build_reschedule_card(interaction_id: str, task_title: str, slots: list) -> dict:
    """Card showing available time slots to pick from."""
    actions = []
    for i, s in enumerate(slots[:5]):
        label = f"{s.get('start','?')} → {s.get('due','?')}"
        actions.append({"tag":"button","text":{"tag":"plain_text","content":label},"type":"default" if i>0 else "primary",
            "value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":"reschedule_pick","slot_index":i}})
    actions.append({"tag":"button","text":{"tag":"plain_text","content":"自定义时间"},"type":"default",
        "value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":"reschedule_custom"}})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "重新安排时间", "tag": "plain_text"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"**{task_title}**\n选择新时间："},
            {"tag": "action", "actions": actions},
        ],
    }


def build_confirm_card(interaction_id: str, title: str, proposal: dict) -> dict:
    """Confirmation card with confirm/cancel."""
    s = proposal.get("start","?"); d = proposal.get("due","?")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": "确认操作", "tag": "plain_text"}, "template": "orange"},
        "elements": [
            {"tag": "markdown", "content": f"**{title}**\n{s} → {d}\n确认此操作？"},
            {"tag": "action", "actions": [
                {"tag":"button","text":{"tag":"plain_text","content":"确认"},"type":"primary",
                    "value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":"confirm"}},
                {"tag":"button","text":{"tag":"plain_text","content":"取消"},"type":"danger",
                    "value":{"hermes_action":"pa_reminder","interaction_id":interaction_id,"action":"cancel"}},
            ]},
        ],
    }


# ── Interaction persistence ─────────────────────────────────────────────────

def persist_interaction(
    interaction_id: str, idempotency_key: str,
    reminder_id: int, kind: str, product_command: str, expires_at: str,
    page_id: str, feishu_message_id: str, feishu_chat_id: str,
    allowed_actions: List[str],
) -> dict:
    data = {
        "interaction_id": interaction_id, "delivery_idempotency_key": idempotency_key,
        "reminder_id": reminder_id, "kind": kind, "product_command": product_command,
        "expires_at": expires_at, "page_id": page_id,
        "feishu_message_id": feishu_message_id, "feishu_chat_id": feishu_chat_id,
        "allowed_actions": allowed_actions, "state": S_PENDING,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db = _get_db()
    try: db.set_meta(_iid_key(interaction_id), json.dumps(data))
    finally:
        try: db.close()
        except: pass
    return data


def get_interaction(interaction_id: str) -> Optional[dict]:
    db = _get_db()
    try:
        raw = db.get_meta(_iid_key(interaction_id))
        return json.loads(raw) if (raw and raw.strip()) else None
    finally:
        try: db.close()
        except: pass


def update_interaction(interaction_id: str, updates: dict):
    data = get_interaction(interaction_id)
    if not data: return
    data.update(updates)
    db = _get_db()
    try: db.set_meta(_iid_key(interaction_id), json.dumps(data))
    finally:
        try: db.close()
        except: pass


# ── Atomic CAS + token ──────────────────────────────────────────────────────

def atomic_claim(interaction_id: str, token: str) -> Optional[str]:
    """Atomic: CAS pending→applying + INSERT token. Returns None=OK or error."""
    db = _get_db()
    try:
        conn = db._conn
        with conn:
            # 1. CAS: only allow pending
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (_iid_key(interaction_id),)
            ).fetchone()
            if not row or not row[0]:
                return "interaction not found"
            data = json.loads(row[0] if isinstance(row, dict) else row[0])
            if data.get("state") != S_PENDING:
                return f"state not pending ({data.get('state')})"

            # 2. INSERT OR IGNORE token
            conn.execute(
                "INSERT OR IGNORE INTO state_meta (key, value) VALUES(?,?)",
                (_token_key(token), "1"),
            )
            if conn.total_changes == 0:
                return "duplicate token"

            # 3. Update state to applying
            data["state"] = S_APPLYING
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_iid_key(interaction_id), json.dumps(data)),
            )
        return None  # success
    except Exception as exc:
        logger.error("atomic_claim failed: %s", exc)
        return str(exc)
    finally:
        try: db.close()
        except: pass


# ── Validation ──────────────────────────────────────────────────────────────

def validate_click(
    interaction_id: str, operator_open_id: str, open_chat_id: str,
    open_message_id: str, action: str, event_token: str,
) -> Optional[str]:
    data = get_interaction(interaction_id)
    if not data: return "interaction not found"
    if data.get("state") not in (S_PENDING, S_APPLYING):
        return f"interaction closed (state={data['state']})"
    if data.get("feishu_message_id") != open_message_id: return "message_id mismatch"
    configured_chat = os.environ.get("FEISHU_FRANK_CHAT_ID","").strip()
    if not configured_chat or open_chat_id != configured_chat: return "chat mismatch"
    allowed = os.environ.get("FEISHU_ALLOWED_USERS","").strip()
    if not allowed or operator_open_id not in {u.strip() for u in allowed.split(",") if u.strip()}:
        return "user not authorized"
    if action not in data.get("allowed_actions",[]) and action not in (
        "extend_confirm","reschedule_pick","reschedule_custom","confirm","cancel"):
        return f"action not allowed: {action}"
    expires_str = data.get("expires_at")
    if expires_str:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_str):
                return "interaction expired"
        except ValueError: return "unparseable expiry"
    return None


# ── Dispatch ────────────────────────────────────────────────────────────────

def dispatch_action(interaction_id: str, action: str, event_token: str) -> str:
    """Validated dispatch. Returns state string for adapter."""
    # Atomic CAS — must succeed before any action
    err = atomic_claim(interaction_id, event_token)
    if err:
        return err  # "duplicate token" or "state not pending" etc.

    data = get_interaction(interaction_id)
    if not data:
        update_interaction(interaction_id, {"state": S_CONFLICT})
        return "interaction_not_found"

    page_id = data.get("page_id","")
    result = "failed"

    try:
        if action == "snooze":
            result = _do_snooze(data)
        elif action in ("start","complete"):
            result = _do_task_action(data, action)
        elif action == "extend":
            result = "extend_card"  # needs secondary card
        elif action == "extend_confirm":
            result = _do_extend_confirm(data, event_token)
        elif action == "reschedule":
            result = "reschedule_card"  # needs slots card
        elif action == "reschedule_pick":
            result = _do_reschedule_pick(data, event_token)
        elif action == "reschedule_custom":
            result = "reschedule_custom"
        elif action == "confirm":
            result = _do_confirm(data)
        elif action == "cancel":
            update_interaction(interaction_id, {"state": S_REJECTED})
            result = "cancelled"
        else:
            result = "unknown_action"
    except Exception as exc:
        logger.error("dispatch %s failed: %s", action, exc)
        result = "failed"

    # Final state
    if result in ("succeeded","applied","cancelled"):
        update_interaction(interaction_id, {"state": S_SUCCEEDED if result=="succeeded" else
            (S_REJECTED if result=="cancelled" else S_SUCCEEDED),
            "result": result, "dispatched_action": action})
    elif result in ("extend_card","reschedule_card","reschedule_custom","confirmation_required"):
        pass  # state stays applying, secondary card needed
    else:
        update_interaction(interaction_id, {"state": S_CONFLICT, "result": result})
    return result


def _do_snooze(data: dict) -> str:
    r = _safe_pa_snooze(data["reminder_id"], data["page_id"],
                        data["feishu_message_id"], data["delivery_idempotency_key"])
    return "succeeded" if r.ok else "failed"


def _do_task_action(data: dict, action: str) -> str:
    shown = show_task(data["page_id"])
    if not shown.get("success"): return "failed"
    sid = f"card-{uuid.uuid4().hex[:12]}"
    uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    r = create_session(sid, data["feishu_chat_id"], uid,
                       data["feishu_message_id"], data["page_id"], action)
    if not r.get("success"): return "failed"
    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"], feishu_user_id=uid)
    return "succeeded" if exe.get("success") else "failed"


def _do_extend_confirm(data: dict, token: str) -> str:
    """User picked extend minutes → create session with computed due."""
    import json as _j
    # The token value from the extend card button contains minutes
    # For now, use a default +30min; the actual minutes would come from button value
    shown = show_task(data["page_id"])
    if not shown.get("success"): return "failed"
    due = shown.get("due")
    if due:
        try:
            dt = datetime.fromisoformat(due) + __import__("datetime").timedelta(minutes=30)
            due = dt.isoformat()
        except (ValueError, TypeError): pass
    sid = f"card-ext-{uuid.uuid4().hex[:12]}"
    uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    r = create_session(sid, data["feishu_chat_id"], uid,
                       data["feishu_message_id"], data["page_id"], "extend", due=due)
    if not r.get("success"): return "failed"
    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"], feishu_user_id=uid)
    if exe.get("status") == "confirmation_required":
        # Save confirmation token in interaction
        update_interaction(data["interaction_id"], {
            "session_id": sid,
            "proposal": exe.get("proposal",{}),
            "confirmation_token": exe.get("confirmation_token",""),
            "confirmation_expires_at": "",  # parsed from exe
        })
        return "confirmation_required"
    return "succeeded" if exe.get("success") else "failed"


def _do_reschedule_pick(data: dict, token: str) -> str:
    """User picked a slot → create reschedule session."""
    slots_r = suggest_slots(data["page_id"])
    if not slots_r.get("success") or not slots_r.get("slots"): return "failed"
    # Get the slot index from interaction data (set by adapter before dispatch)
    slot_idx = int(data.get("slot_index", 0))
    slot = slots_r["slots"][min(slot_idx, len(slots_r["slots"])-1)]
    shown = show_task(data["page_id"])
    if not shown.get("success"): return "failed"
    sid = f"card-res-{uuid.uuid4().hex[:12]}"
    uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    r = create_session(sid, data["feishu_chat_id"], uid,
                       data["feishu_message_id"], data["page_id"], "reschedule",
                       start=slot.get("start"), due=slot.get("due"))
    if not r.get("success"): return "failed"
    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"], feishu_user_id=uid)
    if exe.get("status") == "confirmation_required":
        update_interaction(data["interaction_id"], {
            "session_id": sid, "proposal": exe.get("proposal",{}),
            "confirmation_token": exe.get("confirmation_token",""),
        })
        return "confirmation_required"
    return "succeeded" if exe.get("success") else "failed"


def _do_confirm(data: dict) -> str:
    """User confirmed a pending proposal."""
    sid = data.get("session_id","")
    uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"],
                         feishu_user_id=uid,
                         confirmation_token=data.get("confirmation_token",""))
    return "succeeded" if exe.get("success") else "failed"
