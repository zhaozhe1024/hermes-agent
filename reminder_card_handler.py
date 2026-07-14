#!/usr/bin/env python3
"""Reminder card interaction handler — Calendar GTD / PA integration.

State: pending → choosing_extend/choosing_slot/choosing_custom →
  applying → awaiting_confirmation → succeeded/rejected/conflict.
"""

from __future__ import annotations
import json, logging, os, uuid, sys as _sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_HERMES_HOME = os.path.expanduser("~/.hermes")
for _p in (os.path.join(_HERMES_HOME,"scripts"),os.path.join(_HERMES_HOME,"hermes-agent"),
           os.path.join(_HERMES_HOME,"skills/calendar-gtd-integration/scripts")):
    if _p not in _sys.path: _sys.path.insert(0,_p)

from safe_runner import pa_snooze as _safe_pa_snooze
from task_action_handler import show_task, create_session, execute_action, suggest_slots, get_session
from hermes_state import SessionDB

# States
S_PENDING,S_CHOOSING_EXTEND,S_CHOOSING_SLOT,S_CHOOSING_CUSTOM="pending","choosing_extend","choosing_slot","choosing_custom"
S_APPLYING,S_AWAITING_CONFIRMATION="applying","awaiting_confirmation"
S_SUCCEEDED,S_REJECTED,S_CONFLICT="succeeded","rejected","conflict"
IP,TP="pa:interaction:","pa:interaction:token:"
def _db(): return SessionDB(Path(os.path.join(_HERMES_HOME,"state.db")))
def _ik(iid): return f"{IP}{iid}"
def _tk(tok): return f"{TP}{tok}"

# ── Cards ──
def build_card(interaction_id=None, product_command=None, task_title=None,
               task_start=None, task_due=None, heading=None, *args):
    if args or interaction_id is None:
        if len(args)>=3: interaction_id,product_command,task_title=args[0],args[1],args[2]
        if len(args)>=4: task_start=args[3]
        if len(args)>=5: task_due=args[4]
        if len(args)>=6: heading=args[5]
    pc=product_command
    if not heading: heading={"task.start":"即将开始","task.completion":"到时确认","task.snooze":"到点提醒"}.get(pc,"任务提醒")
    if pc=="task.snooze": acts=[("现在开始","start","primary"),("重新安排时间","reschedule","default")]
    elif pc=="task.start":
        l="到 HH:MM 再提醒"
        if task_start:
            try: l=f"到 {datetime.fromisoformat(task_start).strftime('%H:%M')} 再提醒"
            except: pass
        acts=[("现在开始","start","primary"),(l,"snooze","default"),("重新安排时间","reschedule","default")]
    elif pc=="task.completion": acts=[("标记完成","complete","primary"),("延长时间","extend","default"),("重新安排时间","reschedule","default")]
    else: return _err("Unknown reminder type.")
    tl=f"开始: {task_start}" if task_start else ""
    if task_due: tl+=f"  →  完成: {task_due}"
    return _card(heading,f"**{task_title}**\n{tl}",[_btn(l,a,t,interaction_id) for l,a,t in acts])

def build_extend_card(iid,title): return _card("延长时间",f"**{title}**\n选择延长时间：",
    [_btn(f"+{m} 分钟","extend_confirm","primary" if m==15 else "default",iid,minutes=m) for m in (15,30,60)])
def build_reschedule_card(iid,title,slots):
    btns=[_btn(f"{s.get('start','?')} → {s.get('due','?')}","reschedule_pick","primary" if i==0 else "default",iid,slot_index=i) for i,s in enumerate(slots[:5])]
    btns.append(_btn("自定义时间","reschedule_custom","default",iid,task_title=title))
    return _card("重新安排时间",f"**{title}**\n选择新时间：",btns)
def build_custom_date_card(iid,title):
    today=datetime.now().date()
    btns=[_btn(f"{today+timedelta(days=d)}（{'今天' if d==0 else '明天' if d==1 else str(d)+'天后'}）",
               "reschedule_custom_pick_date","primary" if d==0 else "default",iid,
               custom_date=f"{today+timedelta(days=d)}") for d in range(14)]
    btns.append(_btn("返回推荐时间","reschedule_cancel","danger",iid))
    return _card("选择日期",f"**{title}**\n选择日期：",btns)

def build_custom_time_card(iid, title, custom_date):
    """Time picker: 30-min slots on the chosen date."""
    btns=[]
    for h in range(8,18):
        for m in (0,30):
            if h==17 and m==30: continue
            s=f"{custom_date}T{h:02d}:{m:02d}:00+08:00"
            total=h*60+m+30
            eh,tem=divmod(total,60)
            e=f"{custom_date}T{eh:02d}:{tem:02d}:00+08:00"
            btns.append(_btn(f"{h:02d}:{m:02d} – {eh:02d}:{tem:02d}","reschedule_custom_submit",
                              "primary" if h==8 and m==0 else "default",iid,
                              custom_date=custom_date,custom_start=s,custom_due=e))
    btns.append(_btn("返回推荐时间","reschedule_cancel","danger",iid))
    return _card("选择时间",f"**{title}**\n{custom_date}\n选择时间段：",btns)
def build_confirm_card(iid,title,proposal):
    s,d=proposal.get("start","?"),proposal.get("due","?")
    return _card("确认操作",f"**{title}**\n{s} → {d}\n确认？",[_btn("确认","confirm","primary",iid),_btn("取消","cancel","danger",iid)])
def _card(h,b,acts): return {"config":{"wide_screen_mode":True},"header":{"title":{"content":h,"tag":"plain_text"},"template":"orange" if h=="确认操作" else "blue"},"elements":[{"tag":"markdown","content":b},{"tag":"action","actions":acts}]}
def _btn(label,action,bt,iid,**x): v={"hermes_action":"pa_reminder","interaction_id":iid,"action":action}; v.update(x); return {"tag":"button","text":{"tag":"plain_text","content":label},"type":bt,"value":v}
def _err(m): return {"config":{"wide_screen_mode":True},"header":{"title":{"content":"Error","tag":"plain_text"},"template":"red"},"elements":[{"tag":"markdown","content":m}]}

# ── Persistence ──
def persist_interaction(interaction_id=None, idempotency_key=None, reminder_id=None,
                        kind=None, product_command=None, expires_at=None, page_id=None,
                        feishu_message_id=None, feishu_chat_id=None, allowed_actions=None,
                        task_title=None, *args):
    if args or interaction_id is None:
        vals=[interaction_id,idempotency_key,reminder_id,kind,product_command,
              expires_at,page_id,feishu_message_id,feishu_chat_id,allowed_actions]
        if args: vals=list(args)
        interaction_id=vals[0] if len(vals)>0 else interaction_id
        idempotency_key=vals[1] if len(vals)>1 else idempotency_key
        reminder_id=vals[2] if len(vals)>2 else reminder_id
        kind=vals[3] if len(vals)>3 else kind
        product_command=vals[4] if len(vals)>4 else product_command
        expires_at=vals[5] if len(vals)>5 else expires_at
        page_id=vals[6] if len(vals)>6 else page_id
        feishu_message_id=vals[7] if len(vals)>7 else feishu_message_id
        feishu_chat_id=vals[8] if len(vals)>8 else feishu_chat_id
        allowed_actions=vals[9] if len(vals)>9 else allowed_actions
    data={"interaction_id":interaction_id,"delivery_idempotency_key":idempotency_key,
          "reminder_id":reminder_id,"kind":kind,"product_command":product_command,
          "expires_at":expires_at,"page_id":page_id,
          "active_message_id":feishu_message_id,"feishu_chat_id":feishu_chat_id,
          "allowed_actions":allowed_actions,"task_title":task_title or page_id,
          "state":S_PENDING,"created_at":datetime.now(timezone.utc).isoformat()}
    d=_db()
    try: d.set_meta(_ik(interaction_id),json.dumps(data))
    finally: d.close()
    return data

def get_interaction(iid):
    d=_db()
    try: r=d.get_meta(_ik(iid)); return json.loads(r) if (r and r.strip()) else None
    finally: d.close()
def update_interaction(iid,upd):
    x=get_interaction(iid)
    if not x: return
    x.update(upd); d=_db()
    try: d.set_meta(_ik(iid),json.dumps(x))
    finally: d.close()

# ── Atomic CAS ──
def atomic_claim(iid,token,expected_state,next_state=None):
    """Atomically claim token and CAS state. next_state defaults to applying."""
    if next_state is None: next_state = S_APPLYING
    db=_db()
    try:
        c=db._conn; c.execute("BEGIN IMMEDIATE")
        try:
            row=c.execute("SELECT value FROM state_meta WHERE key=?",(_ik(iid),)).fetchone()
            if not row or not row[0]: c.rollback(); return "not found"
            data=json.loads(row[0] if isinstance(row,dict) else row[0])
            if data.get("state")!=expected_state: c.rollback(); return f"state not {expected_state} ({data.get('state')})"
            data["state"]=next_state
            c.execute("INSERT INTO state_meta (key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(_ik(iid),json.dumps(data)))
            c.execute("INSERT OR IGNORE INTO state_meta (key,value) VALUES(?,?)",(_tk(token),"1"))
            if c.execute("SELECT changes()").fetchone()[0]==0: c.rollback(); return "duplicate token"
            c.commit()
            v=c.execute("SELECT value FROM state_meta WHERE key=?",(_ik(iid),)).fetchone()
            if v and v[0]:
                vd=json.loads(v[0] if isinstance(v,dict) else v[0])
                if vd.get("state")!=next_state: return "state persistence failed"
            return None
        except Exception: c.rollback(); raise
    except Exception as e: logger.error("atomic_claim: %s",e); return str(e)
    finally:
        try: db.close()
        except: pass

# ── Validation ──
_ALL_ACTIONS=frozenset({"start","snooze","reschedule","complete","extend",
    "extend_confirm","reschedule_pick","reschedule_custom","reschedule_custom_pick_date",
    "reschedule_custom_submit","reschedule_cancel","confirm","cancel"})
_SECONDARY_ACTIONS=frozenset({"extend_confirm","reschedule_pick","reschedule_custom",
    "reschedule_custom_pick_date","reschedule_custom_submit","reschedule_cancel","confirm","cancel"})
_VALID_STATES=frozenset({S_PENDING,S_CHOOSING_EXTEND,S_CHOOSING_SLOT,S_CHOOSING_CUSTOM,S_APPLYING,S_AWAITING_CONFIRMATION})

def validate_click(iid,oid,chat,mid,action,token):
    d=get_interaction(iid)
    if not d: return "not found"
    if d.get("state") not in _VALID_STATES: return f"closed ({d['state']})"
    if d.get("active_message_id")!=mid: return "message_id mismatch"
    cc=os.environ.get("FEISHU_FRANK_CHAT_ID","").strip()
    if not cc or chat!=cc: return "chat mismatch"
    au=os.environ.get("FEISHU_ALLOWED_USERS","").strip()
    if not au or oid not in {u.strip() for u in au.split(",") if u.strip()}: return "user not authorized"
    if action not in _ALL_ACTIONS: return f"action not allowed: {action}"
    if action not in d.get("allowed_actions",[]) and action not in _SECONDARY_ACTIONS:
        return f"action not allowed for this interaction: {action}"
    ex=d.get("expires_at")
    if ex:
        try:
            if datetime.now(timezone.utc)>datetime.fromisoformat(ex): return "expired"
        except: return "unparseable expiry"
    return None

# ── Dispatch ──
def dispatch_action(iid,action,token,params=None):
    params=params or {}; d=get_interaction(iid)
    if not d: return {"status":"interaction_not_found"}

    if action in ("extend","reschedule"):
        err=atomic_claim(iid,token,S_PENDING)
        if err: return {"status":err}
        if action=="extend":
            update_interaction(iid,{"state":S_CHOOSING_EXTEND})
            return {"status":"choose_extend","next_state":S_CHOOSING_EXTEND,"card":build_extend_card(iid,d.get("task_title",d["page_id"]))}
        else:
            sr=suggest_slots(d["page_id"])
            if not sr.get("success") or not sr.get("slots"): update_interaction(iid,{"state":S_CONFLICT}); return {"status":"no_slots"}
            update_interaction(iid,{"state":S_CHOOSING_SLOT,"slot_candidates":sr["slots"]})
            return {"status":"choose_slot","next_state":S_CHOOSING_SLOT,"card":build_reschedule_card(iid,d.get("task_title",d["page_id"]),sr["slots"])}

    if action=="extend_confirm":
        err=atomic_claim(iid,token,S_CHOOSING_EXTEND)
        if err: return {"status":err}
        return _do_extend(d,params.get("minutes",30))

    if action=="reschedule_pick":
        err=atomic_claim(iid,token,S_CHOOSING_SLOT)
        if err: return {"status":err}
        sl=d.get("slot_candidates",[]); idx=params.get("slot_index",0)
        if idx>=len(sl): update_interaction(iid,{"state":S_CONFLICT}); return {"status":"invalid_slot_index"}
        return _do_reschedule(d,sl[idx])

    if action=="reschedule_custom":
        err=atomic_claim(iid,token,S_CHOOSING_SLOT,next_state=S_CHOOSING_CUSTOM)
        if err: return {"status":err}
        update_interaction(iid,{"state":S_CHOOSING_CUSTOM})
        return {"status":"choosing_custom","card":build_custom_date_card(iid,d.get("task_title",d["page_id"]))}

    if action=="reschedule_custom_pick_date":
        err=atomic_claim(iid,token,S_CHOOSING_CUSTOM,next_state=S_CHOOSING_CUSTOM)
        if err: return {"status":err}
        custom_date=params.get("custom_date","")
        if not custom_date: update_interaction(iid,{"state":S_CONFLICT}); return {"status":"no_date"}
        update_interaction(iid,{"custom_date":custom_date})
        return {"status":"choose_time","card":build_custom_time_card(iid,d.get("task_title",d["page_id"]),custom_date)}

    if action=="reschedule_cancel":
        err=atomic_claim(iid,token,S_CHOOSING_CUSTOM,next_state=S_CHOOSING_SLOT)
        if err: return {"status":err}
        sr=suggest_slots(d["page_id"])
        if not sr.get("success") or not sr.get("slots"): update_interaction(iid,{"state":S_CONFLICT}); return {"status":"no_slots"}
        update_interaction(iid,{"state":S_CHOOSING_SLOT,"slot_candidates":sr["slots"]})
        return {"status":"choose_slot","card":build_reschedule_card(iid,d.get("task_title",d["page_id"]),sr["slots"])}

    if action=="reschedule_custom_submit":
        err=atomic_claim(iid,token,S_CHOOSING_CUSTOM)
        if err: return {"status":err}
        custom_start=params.get("custom_start","")
        custom_due=params.get("custom_due","")
        if not custom_start or not custom_due: update_interaction(iid,{"state":S_CONFLICT}); return {"status":"no_time"}
        sr=suggest_slots(d["page_id"],start=custom_start,due=custom_due)
        if not sr.get("success"): update_interaction(iid,{"state":S_CONFLICT}); return {"status":"slots_error"}
        requested=sr.get("requested",{})
        if requested.get("available"):
            # PA says available — use next_action to propose
            na=sr.get("next_action",{})
            prop={"start":na.get("start",custom_start),"due":na.get("due",custom_due)}
            update_interaction(iid,{"state":S_APPLYING,"proposal":prop})
            return _cx_from_proposal(d,prop)
        # PA returned slots directly (already computed by PA)
        slots=sr.get("slots",[])
        if not slots: update_interaction(iid,{"state":S_CONFLICT}); return {"status":"no_slots"}
        update_interaction(iid,{"state":S_CHOOSING_SLOT,"slot_candidates":slots})
        return {"status":"choose_slot","card":build_reschedule_card(iid,d.get("task_title",d["page_id"]),slots)}

    if action in ("start","complete"):
        err=atomic_claim(iid,token,S_PENDING)
        if err: return {"status":err}
        return _do_task(d,action)

    if action=="snooze":
        err=atomic_claim(iid,token,S_PENDING)
        if err: return {"status":err}
        r=_safe_pa_snooze(d["reminder_id"],d["page_id"],d["active_message_id"],d["delivery_idempotency_key"])
        update_interaction(iid,{"state":S_SUCCEEDED if r.ok else S_CONFLICT})
        return {"status":"succeeded" if r.ok else "failed"}

    if action=="confirm":
        err=atomic_claim(iid,token,S_AWAITING_CONFIRMATION)
        if err: return {"status":err}
        uid=os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
        exe=execute_action(d["session_id"],feishu_chat_id=d["feishu_chat_id"],feishu_user_id=uid,confirmation_token=d["confirmation_token"])
        update_interaction(iid,{"state":S_SUCCEEDED if exe.get("success") else S_CONFLICT})
        return {"status":"succeeded" if exe.get("success") else "failed"}

    if action=="cancel":
        err=atomic_claim(iid,token,S_AWAITING_CONFIRMATION)
        if err: return {"status":err}
        update_interaction(iid,{"state":S_REJECTED,"result":"cancelled"})
        return {"status":"cancelled"}

    return {"status":"unknown_action"}

def _do_extend(d,mins):
    s=show_task(d["page_id"])
    if not s.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
    old=s.get("due"); new=old
    if old:
        try: new=(datetime.fromisoformat(old)+timedelta(minutes=mins)).isoformat()
        except: pass
    return _cx(d,"extend",due=new)
def _do_reschedule(d,slot): s=show_task(d["page_id"]); return _cx(d,"reschedule",start=slot.get("start"),due=slot.get("due")) if s.get("success") else (update_interaction(d["interaction_id"],{"state":S_CONFLICT}) or {"status":"failed"})
def _do_task(d,action): s=show_task(d["page_id"]); return _cx(d,action) if s.get("success") else (update_interaction(d["interaction_id"],{"state":S_CONFLICT}) or {"status":"failed"})
def _cx_from_proposal(d,prop): return _cx(d,"reschedule",start=prop.get("start"),due=prop.get("due"))
def _cx(d,action,start=None,due=None):
    sid=f"card-{uuid.uuid4().hex[:12]}"
    uid=os.environ.get("FEISHU_ALLOWED_USERS","").split(",")[0].strip()
    r=create_session(sid,d["feishu_chat_id"],uid,d["active_message_id"],d["page_id"],action,start=start,due=due)
    if not r.get("success"): update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
    exe=execute_action(sid,feishu_chat_id=d["feishu_chat_id"],feishu_user_id=uid)
    if exe.get("status")=="confirmation_required":
        sess=get_session(sid); ctok=(sess or {}).get("confirmation_token",""); cexp=(sess or {}).get("confirmation_expires_at","")
        prop=exe.get("proposal",sess.get("proposal",{}) if sess else {})
        update_interaction(d["interaction_id"],{"state":S_AWAITING_CONFIRMATION,"session_id":sid,"proposal":prop,"confirmation_token":ctok,"confirmation_expires_at":cexp})
        return {"status":"confirmation_required","card":build_confirm_card(d["interaction_id"],d.get("task_title",d["page_id"]),prop)}
    if exe.get("success"): update_interaction(d["interaction_id"],{"state":S_SUCCEEDED}); return {"status":"succeeded"}
    update_interaction(d["interaction_id"],{"state":S_CONFLICT}); return {"status":"failed"}
