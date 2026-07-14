#!/usr/bin/env python3
"""Reminder card interaction handler — Calendar GTD / PA integration.

Importable from the hermes-agent root as ``reminder_card_handler``.

Builds interactive cards, persists interaction state, validates clicks,
and dispatches PA actions via safe_runner + task_action_handler.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Import safe_runner (no sys.path hack) ───────────────────────────────────
_SCRIPTS = os.path.join(os.path.expanduser("~/.hermes"), "scripts")
import sys as _sys
if _SCRIPTS not in _sys.path:
    _sys.path.insert(0, _SCRIPTS)
_SKILL = os.path.join(os.path.expanduser("~/.hermes"),
                       "skills/calendar-gtd-integration/scripts")
if _SKILL not in _sys.path:
    _sys.path.insert(0, _SKILL)

from safe_runner import pa_snooze as _safe_pa_snooze
from task_action_handler import (
    show_task, create_session, execute_action, suggest_slots,
    S_PROPOSED, S_AWAITING, S_SUCCEEDED, S_CONFLICT,
)
from hermes_state import SessionDB

_HERMES_HOME = os.path.expanduser("~/.hermes")

# ── Constants ───────────────────────────────────────────────────────────────

INTERACTION_META_PREFIX = "pa:interaction:"
INTERACTION_TOKEN_PREFIX = "pa:interaction:token:"

S_PENDING = "pending"
S_APPLYING = "applying"
S_SUCCEEDED_INTERACTION = "succeeded"
S_CONFLICT_INTERACTION = "conflict"
S_REJECTED = "rejected"
S_EXPIRED_INTERACTION = "expired"

CARD_ACTIONS = frozenset({"start", "snooze", "reschedule", "complete", "extend"})

_START_ACTIONS = [
    ("现在开始", "start", "primary"),
    ("重新安排时间", "reschedule", "default"),
]
_SNOOZE_ACTIONS = [
    ("现在开始", "start", "primary"),
    ("重新安排时间", "reschedule", "default"),
]
_COMPLETION_ACTIONS = [
    ("标记完成", "complete", "primary"),
    ("延长时间", "extend", "default"),
    ("重新安排时间", "reschedule", "default"),
]


# ── DB helpers ──────────────────────────────────────────────────────────────

def _get_db() -> SessionDB:
    return SessionDB(Path(os.path.join(_HERMES_HOME, "state.db")))


def _iid_key(iid: str) -> str:
    return f"{INTERACTION_META_PREFIX}{iid}"


def _token_key(token: str) -> str:
    return f"{INTERACTION_TOKEN_PREFIX}{token}"


# ── Card building ───────────────────────────────────────────────────────────

def build_card(
    interaction_id: str,
    product_command: str,
    task_title: str,
    task_start: Optional[str],
    task_due: Optional[str],
    heading: str,
) -> dict:
    """Build an interactive Feishu card for a reminder."""
    if product_command in ("task.start", "task.snooze"):
        if product_command == "task.snooze":
            action_defs = _SNOOZE_ACTIONS
        else:
            snooze_label = "到 HH:MM 再提醒"
            if task_start:
                try:
                    dt = datetime.fromisoformat(task_start)
                    snooze_label = f"到 {dt.strftime('%H:%M')} 再提醒"
                except (ValueError, TypeError):
                    pass
            action_defs = [
                ("现在开始", "start", "primary"),
                (snooze_label, "snooze", "default"),
                ("重新安排时间", "reschedule", "default"),
            ]
    elif product_command == "task.completion":
        action_defs = _COMPLETION_ACTIONS
    else:
        logger.error("Unknown product_command: %s", product_command)
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"content": "Error", "tag": "plain_text"}, "template": "red"},
            "elements": [{"tag": "markdown", "content": "Unknown reminder type."}],
        }

    actions = []
    for label, action, btn_type in action_defs:
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": btn_type,
            "value": {
                "hermes_action": "pa_reminder",
                "interaction_id": interaction_id,
                "action": action,
            },
        })

    time_line = ""
    if task_start:
        time_line += f"开始: {task_start}"
    if task_due:
        time_line += f"  →  完成: {task_due}"

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"content": heading, "tag": "plain_text"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"**{task_title}**\n{time_line}"},
            {"tag": "action", "actions": actions},
        ],
    }


# ── Interaction state ───────────────────────────────────────────────────────

def persist_interaction(
    interaction_id: str,
    idempotency_key: str,
    reminder_id: int,
    kind: str,
    product_command: str,
    expires_at: str,
    page_id: str,
    feishu_message_id: str,
    feishu_chat_id: str,
    allowed_actions: List[str],
) -> dict:
    data = {
        "interaction_id": interaction_id,
        "delivery_idempotency_key": idempotency_key,
        "reminder_id": reminder_id,
        "kind": kind,
        "product_command": product_command,
        "expires_at": expires_at,
        "page_id": page_id,
        "feishu_message_id": feishu_message_id,
        "feishu_chat_id": feishu_chat_id,
        "allowed_actions": allowed_actions,
        "state": S_PENDING,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db = _get_db()
    try:
        db.set_meta(_iid_key(interaction_id), json.dumps(data))
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


def update_interaction_state(interaction_id: str, state: str, extra: dict = None):
    data = get_interaction(interaction_id)
    if not data:
        return
    data["state"] = state
    if extra:
        data.update(extra)
    db = _get_db()
    try:
        db.set_meta(_iid_key(interaction_id), json.dumps(data))
    finally:
        try: db.close()
        except: pass


# ── Token idempotency ───────────────────────────────────────────────────────

def claim_token(token: str) -> bool:
    """Atomically claim a callback token. Returns True if this is the first claim."""
    db = _get_db()
    try:
        key = _token_key(token)
        existing = db.get_meta(key)
        if existing and existing.strip():
            return False  # already claimed
        db.set_meta(key, "1")
        return True
    finally:
        try: db.close()
        except: pass


# ── Click validation ────────────────────────────────────────────────────────

def validate_click(
    interaction_id: str,
    operator_open_id: str,
    open_chat_id: str,
    open_message_id: str,
    action: str,
    event_token: str,
) -> Optional[str]:
    """Validate a card click. Returns None on success or an error string."""
    data = get_interaction(interaction_id)
    if not data:
        return "interaction not found"

    if data.get("state") not in (S_PENDING, S_APPLYING):
        return f"interaction closed (state={data['state']})"

    if data.get("feishu_message_id") != open_message_id:
        return "message_id mismatch"

    configured_chat = os.environ.get("FEISHU_FRANK_CHAT_ID", "").strip()
    if not configured_chat or open_chat_id != configured_chat:
        return "chat mismatch"

    allowed_users = os.environ.get("FEISHU_ALLOWED_USERS", "").strip()
    if not allowed_users or operator_open_id not in {
        u.strip() for u in allowed_users.split(",") if u.strip()
    }:
        return "user not authorized"

    if action not in data.get("allowed_actions", []):
        return f"action not allowed: {action}"

    expires_str = data.get("expires_at")
    if expires_str:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_str):
                return "interaction expired"
        except ValueError:
            return "unparseable expiry"

    return None


# ── Action dispatch (called by adapter) ─────────────────────────────────────

def dispatch_action(interaction_id: str, action: str, event_token: str) -> str:
    """Dispatch a validated action. Returns a result state string."""
    # Atomic token claim
    if not claim_token(event_token):
        return "duplicate"

    data = get_interaction(interaction_id)
    if not data:
        return "interaction_not_found"

    # Transition to applying
    update_interaction_state(interaction_id, S_APPLYING)

    page_id = data.get("page_id", "")
    result = "failed"

    try:
        if action == "snooze":
            result = _do_snooze(data)
        elif action in ("start", "complete"):
            result = _do_task_action(data, action)
        elif action == "extend":
            result = _do_extend(data)
        elif action == "reschedule":
            result = _do_reschedule(data)
        else:
            result = "unknown_action"
    except Exception as exc:
        logger.error("dispatch %s failed: %s", action, exc)
        result = "failed"

    final_state = S_SUCCEEDED_INTERACTION if result == "succeeded" else (
        S_REJECTED if result in ("confirmation_required",) else S_CONFLICT_INTERACTION
    )
    if result in ("succeeded", "applied"):
        update_interaction_state(interaction_id, S_SUCCEEDED_INTERACTION,
                                  {"result": result, "dispatched_action": action})
    elif result == "confirmation_required":
        update_interaction_state(interaction_id, S_REJECTED,
                                  {"result": result, "dispatched_action": action})
    else:
        update_interaction_state(interaction_id, S_CONFLICT_INTERACTION,
                                  {"result": result, "dispatched_action": action})
    return result


def _do_snooze(data: dict) -> str:
    r = _safe_pa_snooze(
        data["reminder_id"],
        data["page_id"],
        data["feishu_message_id"],
        data["delivery_idempotency_key"],
    )
    return "succeeded" if r.ok else "failed"


def _do_task_action(data: dict, action: str) -> str:
    import uuid
    # First show to get version
    shown = show_task(data["page_id"])
    if not shown.get("success"):
        return "failed"

    version = shown.get("version", "")
    sid = f"card-{uuid.uuid4().hex[:12]}"
    r = create_session(
        sid, data["feishu_chat_id"],
        os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip(),
        data["feishu_message_id"], data["page_id"], action,
    )
    if not r.get("success"):
        return "failed"

    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"],
                         feishu_user_id=os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip())
    if exe.get("success"):
        return "succeeded"
    return "failed"


def _do_extend(data: dict) -> str:
    import uuid
    shown = show_task(data["page_id"])
    if not shown.get("success"):
        return "failed"

    version = shown.get("version", "")
    sid = f"card-ext-{uuid.uuid4().hex[:12]}"
    r = create_session(
        sid, data["feishu_chat_id"],
        os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip(),
        data["feishu_message_id"], data["page_id"], "extend",
        due=data.get("due") or shown.get("due"),
    )
    if not r.get("success"):
        return "failed"

    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"],
                         feishu_user_id=os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip())
    if exe.get("status") == "confirmation_required":
        return "confirmation_required"
    if exe.get("success"):
        return "succeeded"
    return "failed"


def _do_reschedule(data: dict) -> str:
    import uuid

    shown = show_task(data["page_id"])
    if not shown.get("success"):
        return "failed"

    slots = suggest_slots(data["page_id"])
    if not slots.get("success"):
        return "failed"

    if not slots.get("slots"):
        return "no_slots"

    # Take first available slot
    first = slots["slots"][0]
    version = shown.get("version", "")
    sid = f"card-res-{uuid.uuid4().hex[:12]}"
    r = create_session(
        sid, data["feishu_chat_id"],
        os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip(),
        data["feishu_message_id"], data["page_id"], "reschedule",
        start=first.get("start"), due=first.get("due"),
    )
    if not r.get("success"):
        return "failed"

    exe = execute_action(sid, feishu_chat_id=data["feishu_chat_id"],
                         feishu_user_id=os.environ.get("FEISHU_ALLOWED_USERS", "").split(",")[0].strip())
    if exe.get("status") == "confirmation_required":
        return "confirmation_required"
    if exe.get("success"):
        return "succeeded"
    return "failed"
