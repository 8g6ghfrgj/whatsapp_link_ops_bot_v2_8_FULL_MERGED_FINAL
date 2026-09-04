from __future__ import annotations
import asyncio
from ..db import connect, now_iso, upsert_link
from ..link_utils import extract_urls, normalize_url, classify_link
from ..config import settings
from .wa_provider import provider, ProviderError
from .alerts import emit_alert

async def sync_account(row,bot=None):
    slot=int(row['id']); pid=row['provider_account_id']; op=int(row['operator_id'])
    try:
        st=await provider.status(pid)
    except Exception as e:
        db=await connect()
        try:
            await db.execute("UPDATE account_slots SET health='provider_error',last_error=?,last_seen_at=? WHERE id=?",(str(e)[:1000],now_iso(),slot)); await db.commit()
        finally: await db.close()
        if bot and row['health']=='connected':
            await emit_alert(bot,settings.owner_id,'account_disconnected',f"انقطع حساب WhatsApp: {row['label']}",f"الحساب #{slot} للمشرف {op}\nالسبب: {e}",severity='critical',dedupe_key=f'account:{slot}')
        return
    status=st.get('status') or 'unknown'
    if status!='connected':
        db=await connect()
        try:
            await db.execute('UPDATE account_slots SET health=?,last_error=?,last_seen_at=? WHERE id=?',(status,(st.get('last_error') or '')[:1000] or None,now_iso(),slot)); await db.commit()
        finally: await db.close()
        if bot and row['health']=='connected':
            await emit_alert(bot,settings.owner_id,'account_disconnected',f"انقطع حساب WhatsApp: {row['label']}",f"الحساب #{slot} للمشرف {op}\nالحالة: {status}\n{st.get('last_error') or ''}",severity='critical',dedupe_key=f'account:{slot}')
        return
    boot=st.get('boot_id') or ''
    db=await connect()
    try:
        cur=await db.execute('SELECT provider_boot_id,last_event_id FROM wa_sync_cursors WHERE account_slot_id=?',(slot,)); c=await cur.fetchone()
        after=0 if not c or c['provider_boot_id']!=boot else int(c['last_event_id'])
    finally: await db.close()
    try: data=await provider.events(pid,after=after,limit=settings.wa_sync_batch)
    except Exception: return
    boot=data.get('boot_id') or boot
    evs=data.get('events') or []
    if not evs: return
    db=await connect()
    try:
        for ev in evs:
            if ev.get('type') not in {'message','history_message'}: continue
            text=ev.get('text') or ''
            try:
                await db.execute('''INSERT OR IGNORE INTO wa_messages(account_slot_id,provider_boot_id,provider_event_id,remote_jid,message_id,participant,message_ts,text,history,inserted_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?)''',(slot,boot,int(ev.get('id') or 0),ev.get('jid'),ev.get('message_id'),ev.get('participant'),int(ev.get('timestamp') or 0),text,1 if ev.get('type')=='history_message' else 0,now_iso()))
            except Exception: pass
        last=int(data.get('last_id') or evs[-1].get('id') or after)
        await db.execute('''INSERT INTO wa_sync_cursors(account_slot_id,provider_boot_id,last_event_id,updated_at) VALUES(?,?,?,?)
                            ON CONFLICT(account_slot_id) DO UPDATE SET provider_boot_id=excluded.provider_boot_id,last_event_id=excluded.last_event_id,updated_at=excluded.updated_at''',(slot,boot,last,now_iso()))
        await db.execute("UPDATE account_slots SET health='connected',last_error=NULL,last_seen_at=? WHERE id=?",(now_iso(),slot))
        await db.commit()
    finally: await db.close()

async def live_watch_ingest():
    db=await connect()
    try:
        watches=await (await db.execute('SELECT * FROM watches WHERE enabled=1')).fetchall()
    finally: await db.close()
    for w in watches:
        db=await connect()
        try:
            q='SELECT id,text FROM wa_messages WHERE account_slot_id=? AND remote_jid=? AND id>? ORDER BY id LIMIT 5000'
            rows=await (await db.execute(q,(w['account_slot_id'],w['remote_jid'],w['last_message_row_id']))).fetchall()
        finally: await db.close()
        last=int(w['last_message_row_id'])
        for r in rows:
            last=int(r['id'])
            for u in extract_urls(r['text'] or ''):
                n=normalize_url(u); cat=classify_link(u)
                if n and (w['category']=='all' or cat==w['category']):
                    await upsert_link(u,n,cat,int(w['operator_id']),f"wa-live:{w['remote_jid']}")
        if last!=int(w['last_message_row_id']):
            db=await connect()
            try: await db.execute('UPDATE watches SET last_message_row_id=? WHERE id=?',(last,w['id'])); await db.commit()
            finally: await db.close()

async def worker_forever(bot=None):
    while True:
        try:
            db=await connect()
            try: rows=await (await db.execute('SELECT * FROM account_slots WHERE enabled=1 AND provider_account_id IS NOT NULL')).fetchall()
            finally: await db.close()
            await asyncio.gather(*(sync_account(r,bot) for r in rows),return_exceptions=True)
            await live_watch_ingest()
        except Exception: pass
        await asyncio.sleep(max(1.0,settings.wa_sync_interval))
