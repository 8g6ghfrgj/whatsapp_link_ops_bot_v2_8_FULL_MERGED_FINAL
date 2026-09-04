from __future__ import annotations

import json
from ..db import connect, now_iso


async def log_admin_event(actor_id:int,event_type:str,entity_type:str|None=None,entity_id:str|int|None=None,details:dict|None=None):
    db=await connect()
    try:
        await db.execute(
            "INSERT INTO admin_events(actor_id,event_type,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (int(actor_id),event_type,entity_type,str(entity_id) if entity_id is not None else None,json.dumps(details or {},ensure_ascii=False),now_iso()),
        )
        await db.commit()
    finally:
        await db.close()


async def log_system_error(source:str,message:str,details:str|None=None,severity:str='error'):
    db=await connect()
    try:
        await db.execute(
            "INSERT INTO system_errors(source,severity,message,details,created_at,resolved) VALUES(?,?,?,?,?,0)",
            (source,severity,(message or '')[:1200],(details or '')[:5000] or None,now_iso()),
        )
        # Keep the table bounded on long-running phones.
        await db.execute("DELETE FROM system_errors WHERE id NOT IN (SELECT id FROM system_errors ORDER BY id DESC LIMIT 1000)")
        await db.commit()
    finally:
        await db.close()


async def recent_errors(limit:int=20,open_only:bool=False):
    db=await connect()
    try:
        q="SELECT * FROM system_errors"
        args=[]
        if open_only:q+=" WHERE resolved=0"
        q+=" ORDER BY id DESC LIMIT ?"; args.append(max(1,min(100,int(limit))))
        return await (await db.execute(q,args)).fetchall()
    finally: await db.close()


async def resolve_error(error_id:int):
    db=await connect()
    try:
        await db.execute("UPDATE system_errors SET resolved=1 WHERE id=?",(int(error_id),)); await db.commit()
    finally: await db.close()


async def recent_admin_events(limit:int=30):
    db=await connect()
    try:
        return await (await db.execute("SELECT * FROM admin_events ORDER BY id DESC LIMIT ?",(max(1,min(100,int(limit))),))).fetchall()
    finally: await db.close()


async def upsert_chat_meta(owner_id:int,account_slot_id:int,remote_jid:str,*,display_name:str|None=None,note:str|None=None,status:str|None=None,follow_up_at:str|None=None):
    db=await connect(); now=now_iso()
    try:
        await db.execute("""INSERT INTO chat_metadata(owner_id,account_slot_id,remote_jid,display_name,note,status,follow_up_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_id,account_slot_id,remote_jid) DO UPDATE SET
              display_name=COALESCE(excluded.display_name,chat_metadata.display_name),
              note=CASE WHEN ? IS NULL THEN chat_metadata.note ELSE excluded.note END,
              status=CASE WHEN ? IS NULL THEN chat_metadata.status ELSE excluded.status END,
              follow_up_at=CASE WHEN ? IS NULL THEN chat_metadata.follow_up_at ELSE excluded.follow_up_at END,
              updated_at=excluded.updated_at""",
            (owner_id,account_slot_id,remote_jid,display_name,note,status or 'new',follow_up_at,now,note,status,follow_up_at))
        await db.commit()
    finally: await db.close()


async def add_tag(owner_id:int,entity_type:str,entity_key:str,tag:str):
    tag=' '.join((tag or '').strip().split())[:50]
    if not tag:return False
    db=await connect()
    try:
        await db.execute("INSERT OR IGNORE INTO entity_tags(owner_id,entity_type,entity_key,tag,created_at) VALUES(?,?,?,?,?)",(owner_id,entity_type,entity_key,tag,now_iso())); await db.commit(); return True
    finally: await db.close()


async def list_tags(owner_id:int,entity_type:str,entity_key:str):
    db=await connect()
    try:
        rows=await (await db.execute("SELECT tag FROM entity_tags WHERE owner_id=? AND entity_type=? AND entity_key=? ORDER BY tag",(owner_id,entity_type,entity_key))).fetchall()
        return [r['tag'] for r in rows]
    finally: await db.close()
