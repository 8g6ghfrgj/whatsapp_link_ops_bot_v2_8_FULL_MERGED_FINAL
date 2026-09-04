from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..config import settings
from ..db import connect, now_iso
from .jobs import create_job, job_signal, set_job
from .telegram_sessions import list_sessions, session_file, source_session, verify_session
from .telegram_sources import ingest_source_text
from .alerts import emit_alert


_SOURCE_LOCKS: dict[int, asyncio.Lock] = {}


def _session_file() -> Path:
    """Compatibility helper for the single-session V2.5/V2.6 layout."""
    return session_file(settings.telegram_session_path)


# V2.5-compatible entry shape: async def history_status(verify:bool=False)
async def history_status(verify: bool=False, owner_id: int | None=None):
    """Return aggregate status for legacy and bot-created Telethon sessions."""
    configured=bool(settings.telegram_api_id and settings.telegram_api_hash)
    if not configured:
        return {'configured':False,'authorized':False,'session_present':False,'session_count':0,'reason':'api_credentials_missing'}
    owner_id=int(owner_id or settings.owner_id)
    rows=await list_sessions(owner_id)
    if not verify:
        authorized=[r for r in rows if r['enabled'] and r['health'] in {'authorized','authorized_unverified'} and session_file(r['session_path']).exists()]
        present=any(session_file(r['session_path']).exists() for r in rows) or _session_file().exists()
        return {'configured':True,'authorized':bool(authorized),'session_present':present,'session_count':len(rows),
                'authorized_count':len(authorized),'verified':False}
    for row in rows:
        if row['enabled']:
            await verify_session(owner_id,int(row['id']))
    rows=await list_sessions(owner_id)
    authorized=[r for r in rows if r['enabled'] and r['health'] in {'authorized','authorized_unverified'} and session_file(r['session_path']).exists()]
    present=any(session_file(r['session_path']).exists() for r in rows) or _session_file().exists()
    return {'configured':True,'authorized':bool(authorized),'session_present':present,'session_count':len(rows),
            'authorized_count':len(authorized),'verified':True}


async def _source_entity(client, src):
    entity=None
    username=(src['username'] or '').strip()
    if username:
        try:
            entity=await client.get_entity(username if username.startswith('@') else '@'+username)
        except Exception:
            entity=None
    if entity is None:
        try:
            entity=await client.get_entity(int(src['chat_id']))
        except Exception:
            async for dialog in client.iter_dialogs():
                entity_id=int(getattr(dialog.entity,'id',0) or 0)
                dialog_id=int(getattr(dialog,'id',0) or 0)
                if entity_id==abs(int(src['chat_id'])) or dialog_id==int(src['chat_id']):
                    entity=dialog.entity; break
    return entity


async def _import_locked(source_id: int, owner_id: int, job_id: int | None=None):
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        return {'error':'telegram_api_credentials_missing'}
    try:
        from telethon import TelegramClient
    except Exception:
        return {'error':'telethon_not_installed'}
    db=await connect()
    try:
        src=await (await db.execute('SELECT * FROM telegram_sources WHERE id=? AND owner_id=?',(source_id,owner_id))).fetchone()
    finally:
        await db.close()
    if not src:
        return {'error':'source_not_found'}
    spec=await source_session(owner_id,src)
    if not spec:
        return {'error':'telegram_session_not_authorized'}
    cursor=int(src['history_cursor_id'] or 0) if 'history_cursor_id' in src.keys() else 0
    client=TelegramClient(spec['session_path'],settings.telegram_api_id,settings.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {'error':'telegram_session_not_authorized','session_id':spec.get('id')}
        entity=await _source_entity(client,src)
        if entity is None:
            return {'error':'telegram_source_not_accessible_by_session','session_id':spec.get('id')}

        processed=found=new=dup=queued=blocked=smart_channels=0
        last_id=cursor; pending_new=0

        async def report(complete: bool=False):
            nonlocal pending_new
            snapshot={'source_id':source_id,'session_id':spec.get('id'),'processed_messages':processed,'found':found,
                      'new':new,'duplicates':dup,'queued':queued,'blocked':blocked,'smart_channels':smart_channels,
                      'cursor':last_id,'completed':complete}
            db2=await connect()
            try:
                await db2.execute('''UPDATE telegram_sources
                    SET collected_links=collected_links+?,history_cursor_id=CASE WHEN history_cursor_id>? THEN history_cursor_id ELSE ? END,
                        history_complete=CASE WHEN ? THEN 1 ELSE history_complete END,last_sync_at=?,updated_at=? WHERE id=? AND owner_id=?''',
                    (pending_new,last_id,last_id,1 if complete else 0,now_iso(),now_iso(),source_id,owner_id))
                await db2.commit(); pending_new=0
            finally:
                await db2.close()
            if job_id:
                await set_job(job_id,'running',snapshot)
            return snapshot

        # reverse=True yields oldest -> newest. min_id makes pause/resume continue
        # after the highest message already processed for this source.
        async for msg in client.iter_messages(entity,reverse=True,min_id=cursor):
            sig=await job_signal(job_id)
            if sig:
                snapshot=await report(False)
                snapshot.update({'paused':sig=='pause_requested','cancelled':sig=='cancel_requested'})
                return snapshot
            msg_id=int(getattr(msg,'id',0) or 0)
            text=getattr(msg,'message',None) or ''
            processed+=1
            if text:
                rep=await ingest_source_text(text,src,owner_id,f'telegram_history:{src["chat_id"]}',src['title'])
                found+=rep['found']; new+=rep['new']; pending_new+=rep['new']; dup+=rep['duplicates']
                queued+=rep['queued']; blocked+=rep['blocked']; smart_channels+=rep.get('smart_channels',0)
            if msg_id>last_id:
                last_id=msg_id
            if processed % 250==0:
                await report(False)
        return await report(True)
    finally:
        await client.disconnect()


async def import_source_history(source_id: int, owner_id: int, job_id: int | None=None):
    """Import all old posts once, then only new posts, serialized per source."""
    lock=_SOURCE_LOCKS.setdefault(int(source_id),asyncio.Lock())
    async with lock:
        return await _import_locked(source_id,owner_id,job_id)


async def _auto_job(owner_id: int, source_id: int) -> int:
    payload={'source_id':int(source_id),'automatic':True}
    payload_json=json.dumps(payload,ensure_ascii=False)
    db=await connect()
    try:
        row=await (await db.execute("SELECT id FROM jobs WHERE operator_id=? AND kind='telegram_auto_sync' AND payload_json=? AND COALESCE(hidden,0)=0 ORDER BY id DESC LIMIT 1",
                                   (owner_id,payload_json))).fetchone()
    finally:
        await db.close()
    return int(row['id']) if row else await create_job(owner_id,'telegram_auto_sync',payload)


async def telegram_sync_forever(bot=None):
    """Continuously import new posts for every enabled session-backed source."""
    while True:
        try:
            db=await connect()
            try:
                sources=await (await db.execute('''SELECT ts.* FROM telegram_sources ts
                    WHERE ts.enabled=1 AND ((ts.telegram_session_id IS NOT NULL AND EXISTS(
                      SELECT 1 FROM telegram_sessions s WHERE s.id=ts.telegram_session_id AND s.owner_id=ts.owner_id
                        AND s.enabled=1 AND s.health IN ('authorized','authorized_unverified')))
                      OR (ts.telegram_session_id IS NULL AND EXISTS(
                      SELECT 1 FROM telegram_sessions s WHERE s.owner_id=ts.owner_id AND s.enabled=1
                        AND s.health IN ('authorized','authorized_unverified'))))
                    ORDER BY ts.id''')).fetchall()
            finally:
                await db.close()
            for src in sources:
                jid=await _auto_job(int(src['owner_id']),int(src['id']))
                await set_job(jid,'running',{'source_id':int(src['id']),'automatic':True})
                rep=await import_source_history(int(src['id']),int(src['owner_id']),jid)
                status='paused' if rep.get('paused') else ('cancelled' if rep.get('cancelled') else ('failed' if rep.get('error') else 'completed'))
                await set_job(jid,status,{**rep,'automatic':True})
                if bot and rep.get('error'):
                    await emit_alert(bot,settings.owner_id,'telegram_source_failed',f"فشل مصدر Telegram: {src['title'] or src['chat_id']}",f"المصدر #{src['id']} — {rep.get('error')}",dedupe_key=f"tgsource:{src['id']}:{rep.get('error')}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if bot:
                await emit_alert(bot,settings.owner_id,'telegram_source_failed','توقف مزامن مصادر Telegram',str(exc),severity='critical',dedupe_key='telegram_sync_loop')
        await asyncio.sleep(settings.telegram_sync_interval_seconds)
