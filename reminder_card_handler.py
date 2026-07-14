#!/usr/bin/env python3
"""Reminder card interaction handler — Calendar GTD / PA integration.

State machine:
  pending → choosing_extend/choosing_slot → applying → awaiting_confirmation
  → succeeded / rejected / conflict

dispatch_action returns structured dicts, not raw strings.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HERMES_HOME = os.path.expanduser("~/.hermes")

import sys as _sys
for _p in (os.path.join(_HERMES_HOME, "scripts"),
           os.path.join(_HERMES_HOME, "hermes-agent"),
           os.path.join(_HERMES_HOME, "skills/calendar-gtd-integration/scripts")):
    if _p not in _sys.path: _sys.path.insert(0, _p)

from safe_runner import pa_snooze as _safe_pa_snooze
from task_action_handler import (
    show_task, create_session, execute_action, suggest_slots, get_session,
)
from hermes_state import SessionDB

# ── States ──────────────────────────────────────────────────────────────────
S_PENDING = "pending"
S_CHOOSING_EXTEND = "choosing_extend"
S_CHOOSING_SLOT = "choosing_slot"
S_APPLYING = "applying"
S_AWAITING_CONFIRMATION = "awaiting_confirmation"
S_SUCCEEDED = "succeeded"
S_REJECTED = "rejected"
S_CONFLICT = "conflict"

IP = "pa:interaction:"
TP = "pa:interaction:token:"


def _db():
    return SessionDB(Path(os.path.join(_HERMES_HOME, "state.db")))
def _ik(iid): return f"{IP}{iid}"
def _tk(tok): return f"{TP}{tok}"


# ── Card builders ───────────────────────────────────────────────────────────

def build_card(iid, pc, title, start, due, heading):
    if pc == "task.snooze":
        acts = [("现在开始","start","primary"),("重新安排时间","reschedule","default")]
    elif pc == "task.start":
        l = "到 HH:MM 再提醒"
        if start:
            try: l = f"到 {datetime.fromisoformat(start).strftime('%H:%M')} 再提醒"
            except: pass
        acts = [("现在开始","start","primary"),(l,"snooze","default"),("重新安排时间","reschedule","default")]
    elif pc == "task.completion":
        acts = [("标记完成","complete","primary"),("延长时间","extend","default"),("重新安排时间","reschedule","default")]
    else:
        return _err("Unknown reminder type.")
    tl = f"开始: {start}" if start else ""
    if due: tl += f"  →  完成: {due}"
    return _card(heading, f"**{title}**\n{tl}", [
        _btn(l, a, t, iid) for l, a, t in acts
    ])


def build_extend_card(iid, title):
    return _card("延长时间", f"**{title}**\n选择延长时间：", [
        _btn(f"+{m} 分钟", "extend_confirm", "primary" if m==15 else "default", iid, minutes=m)
        for m in (15, 30, 60)
    ])


def build_reschedule_card(iid, title, slots):
    return _card("重新安排时间", f"**{title}**\n选择新时间：",
        [_btn(f"{s.get('start','?')} → {s.get('due','?')}", "reschedule_pick",
              "primary" if i==0 else "default", iid, slot_index=i)
         for i, s in enumerate(slots[:5])])


def build_confirm_card(iid, title, proposal):
    s, d = proposal.get("start","?"), proposal.get("due","?")
    return _card("确认操作", f"**{title}**\n{s} → {d}\n确认？", [
        _btn("确认","confirm","primary",iid), _btn("取消","cancel","danger",iid)])


def _card(heading, body, actions):
    return {"config":{"wide_screen_mode":True},
            "header":{"title":{"content":heading,"tag":"plain_text"},"template":"orange" if heading=="确认操作" else "blue"},
            "elements":[{"tag":"markdown","content":body},{"tag":"action","actions":actions}]}
def _btn(label, action, btn_type, iid, **extra):
    v = {"hermes_action":"pa_reminder","interaction_id":iid,"action":action}; v.update(extra)
    return {"tag":"button","text":{"tag":"plain_text","content":label},"type":btn_type,"value":v}
def _err(msg):
    return {"config":{"wide_screen_mode":True},"header":{"title":{"content":"Error","tag":"plain_text"},"template":"red"},"elements":[{"tag":"markdown","content":msg}]}


# ── Persistence ─────────────────────────────────────────────────────────────

def persist_interaction(iid, dk, rid, kind, pc, expires, pid, fmid, chat, allowed):
    data = {"interaction_id":iid,"delivery_idempotency_key":dk,"reminder_id":rid,
            "kind":kind,"product_command":pc,"expires_at":expires,"page_id":pid,
            "active_message_id":fmid,"feishu_chat_id":chat,"allowed_actions":allowed,
            "state":S_PENDING,"created_at":datetime.now(timezone.utc).isoformat()}
    d = _db()
    try: d.set_meta(_ik(iid), json.dumps(data))
    finally: d.close()
    return data

def get_interaction(iid):
    d = _db()
    try: r = d.get_meta(_ik(iid)); return json.loads(r) if (r and r.strip()) else None
    finally: d.close()

def update_interaction(iid, updates):
    x = get_interaction(iid)
    if not x: return
    x.update(updates)
    d = _db()
    try: d.set_meta(_ik(iid), json.dumps(x))
    finally: d.close()


# ── Atomic CAS ──────────────────────────────────────────────────────────────

def atomic_claim(iid: str, token: str, expected_state: str) -> Optional[str]:
    db = _db()
    try:
        c = db._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute("SELECT value FROM state_meta WHERE key=?", (_ik(iid),)).fetchone()
            if not row or not row[0]: c.rollback(); return "not found"
            data = json.loads(row[0] if isinstance(row, dict) else row[0])
            if data.get("state") != expected_state:
                c.rollback(); return f"state not {expected_state} ({data.get('state')})"
            data["state"] = S_APPLYING
            c.execute("INSERT INTO state_meta (key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_ik(iid), json.dumps(data)))
            c.execute("INSERT OR IGNORE INTO state_meta (key,value) VALUES(?,?)", (_tk(token), "1"))
            if c.execute("SELECT changes()").fetchone()[0] == 0:
                c.rollback(); return "duplicate token"
            c.commit()
            # Re-read to ensure WAL visibility
            verify = c.execute("SELECT value FROM state_meta WHERE key=?", (_ik(iid),)).fetchone()
            if verify and verify[0]:
                vd = json.loads(verify[0] if isinstance(verify, dict) else verify[0])
                if vd.get("state") != S_APPLYING:
                    return "state persistence failed"
            return None
        except Exception: c.rollback(); raise
    except Exception as e: logger.error("atomic_claim: %s", e); return str(e)
    finally:
        try: db.close()
        except: pass


# ── Validation ──────────────────────────────────────────────────────────────

def validate_click(iid, oid, chat, mid, action, token):
    d = get_interaction(iid)
    if not d: return "not found"
    if d.get("state") not in (S_PENDING, S_CHOOSING_EXTEND, S_CHOOSING_SLOT, S_APPLYING, S_AWAITING_CONFIRMATION):
        return f"closed ({d['state']})"
    if d.get("active_message_id") != mid: return "message_id mismatch"
    cc = os.environ.get("FEISHU_FRANK_CHAT_ID","").strip()
    if not cc or chat != cc: return "chat mismatch"
    au = os.environ.get("FEISHU_ALLOWED_USERS","").strip()
    if not au or oid not in {u.strip() for u in au.split(",") if u.strip()}: return "user not authorized"
    _ALL = frozenset({"start","snooze","reschedule","complete","extend",
                       "extend_confirm","reschedule_pick","confirm","cancel"})
    if action not in _ALL: return f"action not allowed: {action}"
    if action not in d.get("allowed_actions",[]) and action not in (
        "extend_confirm","reschedule_pick","confirm","cancel"):
        return f"action not allowed for this interaction: {action}"
    ex = d.get("expires_at")
    if ex:
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(ex): return "expired"
        except: return "unparseable expiry"
    return None


# ── Dispatch (returns structured dict) ──────────────────────────────────────

def dispatch_action(iid: str, action: str, token: str, params: dict = None) -> dict:
    """Return structured outcome dict with status, next_state, card, etc."""
    params = params or {}
    d = get_interaction(iid)
    if not d: return {"status":"interaction_not_found"}

    # ── Primary actions from pending ──
    if action in ("extend", "reschedule"):
        err = atomic_claim(iid, token, S_PENDING)
        if err: return {"status":err}
        if action == "extend":
            update_interaction(iid, {"state": S_CHOOSING_EXTEND})
            return {"status":"choose_extend", "next_state":S_CHOOSING_EXTEND,
                    "card": build_extend_card(iid, d["page_id"])}
        else:
            slots_r = suggest_slots(d["page_id"])
            if not slots_r.get("success") or not slots_r.get("slots"):
                update_interaction(iid, {"state":S_CONFLICT})
                return {"status":"no_slots"}
            update_interaction(iid, {"state":S_CHOOSING_SLOT, "slot_candidates":slots_r["slots"]})
            return {"status":"choose_slot", "next_state":S_CHOOSING_SLOT,
                    "card": build_reschedule_card(iid, d["page_id"], slots_r["slots"])}

    # ── Choosing ──
    if action == "extend_confirm":
        err = atomic_claim(iid, token, S_CHOOSING_EXTEND)
        if err: return {"status":err}
        return _do_extend(d, params.get("minutes", 30))

    if action == "reschedule_pick":
        err = atomic_claim(iid, token, S_CHOOSING_SLOT)
        if err: return {"status":err}
        slots = d.get("slot_candidates", [])
        idx = params.get("slot_index", 0)
        if idx >= len(slots):
            update_interaction(iid, {"state":S_CONFLICT})
            return {"status":"invalid_slot_index"}
        return _do_reschedule(d, slots[idx])

    # ── Direct ──
    if action in ("start", "complete"):
        err = atomic_claim(iid, token, S_PENDING)
        if err: return {"status":err}
        return _do_task(d, action)

    if action == "snooze":
        err = atomic_claim(iid, token, S_PENDING)
        if err: return {"status":err}
        r = _safe_pa_snooze(d["reminder_id"], d["page_id"], d["active_message_id"], d["delivery_idempotency_key"])
        update_interaction(iid, {"state":S_SUCCEEDED if r.ok else S_CONFLICT})
        return {"status":"succeeded" if r.ok else "failed"}

    # ── Confirmation ──
    if action == "confirm":
        err = atomic_claim(iid, token, S_AWAITING_CONFIRMATION)
        if err: return {"status":err}
        uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
        exe = execute_action(d["session_id"], feishu_chat_id=d["feishu_chat_id"],
                             feishu_user_id=uid, confirmation_token=d["confirmation_token"])
        update_interaction(iid, {"state":S_SUCCEEDED if exe.get("success") else S_CONFLICT,
                                  "result":"confirmed"})
        return {"status":"succeeded" if exe.get("success") else "failed"}

    if action == "cancel":
        err = atomic_claim(iid, token, S_AWAITING_CONFIRMATION)
        if err: return {"status":err}
        update_interaction(iid, {"state":S_REJECTED, "result":"cancelled"})
        return {"status":"cancelled"}

    return {"status":"unknown_action"}


def _do_extend(d, mins):
    shown = show_task(d["page_id"])
    if not shown.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
    old = shown.get("due")
    new = old
    if old:
        try: new = (datetime.fromisoformat(old) + timedelta(minutes=mins)).isoformat()
        except: pass
    return _do_create_execute(d, "extend", due=new)


def _do_reschedule(d, slot):
    shown = show_task(d["page_id"])
    if not shown.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
    return _do_create_execute(d, "reschedule", start=slot.get("start"), due=slot.get("due"))


def _do_task(d, action):
    shown = show_task(d["page_id"])
    if not shown.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
    return _do_create_execute(d, action)


def _do_create_execute(d, action, start=None, due=None):
    sid = f"card-{uuid.uuid4().hex[:12]}"
    uid = os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    r = create_session(sid, d["feishu_chat_id"], uid, d["active_message_id"],
                       d["page_id"], action, start=start, due=due)
    if not r.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}

    exe = execute_action(sid, feishu_chat_id=d["feishu_chat_id"], feishu_user_id=uid)
    if exe.get("status") == "confirmation_required":
        session = get_session(sid)
        ctok = (session or {}).get("confirmation_token", "")
        cexp = (session or {}).get("confirmation_expires_at", "")
        proposal = exe.get("proposal", session.get("proposal",{}) if session else {})
        update_interaction(d["interaction_id"], {
            "state": S_AWAITING_CONFIRMATION, "session_id": sid,
            "proposal": proposal, "confirmation_token": ctok, "confirmation_expires_at": cexp,
        })
        return {"status":"confirmation_required", "card": build_confirm_card(
            d["interaction_id"], d["page_id"], proposal)}
    if exe.get("success"):
        update_interaction(d["interaction_id"],{"state":S_SUCCEEDED}); return {"status":"succeeded"}
    update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
