from __future__ import annotations

import asyncio
import os
import re
import secrets
from pathlib import Path

from ..config import settings
from ..db import connect, now_iso


_PENDING: dict[int, dict] = {}
_PENDING_LOCK = asyncio.Lock()


def session_file(path: str) -> Path:
    raw=Path(path)
    return raw if raw.suffix=='.session' else Path(str(raw)+'.session')


def _phone(value: str) -> str:
    digits=re.sub(r'\D','',value or '')
    if not 7 <= len(digits) <= 16:
        raise ValueError('invalid_phone')
    return '+'+digits


def _phone_hint(value: str | None) -> str | None:
    digits=re.sub(r'\D','',value or '')
    return ('••••'+digits[-4:]) if digits else None


async def ensure_legacy_session(owner_id: int) -> None:
    """Register the old V2.5/V2.6 session without moving or rewriting it."""
    if not session_file(settings.telegram_session_path).exists():
        return
    db=await connect(); now=now_iso()
    try:
        exists=await (await db.execute('SELECT 1 FROM telegram_sessions WHERE session_path=?',(settings.telegram_session_path,))).fetchone()
        if not exists:
            label='جلسة Telegram القديمة'
            suffix=1
            while await (await db.execute('SELECT 1 FROM telegram_sessions WHERE owner_id=? AND label=?',(owner_id,label))).fetchone():
                suffix+=1; label=f'جلسة Telegram القديمة {suffix}'
            await db.execute('''INSERT INTO telegram_sessions(owner_id,label,session_path,enabled,health,created_at,updated_at)
                VALUES(?,?,?,1,'authorized_unverified',?,?)''',(owner_id,label,settings.telegram_session_path,now,now))
            await db.commit()
    finally:
        await db.close()


async def list_sessions(owner_id: int):
    await ensure_legacy_session(owner_id)
    db=await connect()
    try:
        return await (await db.execute('SELECT * FROM telegram_sessions WHERE owner_id=? ORDER BY id',(owner_id,))).fetchall()
    finally:
        await db.close()


async def get_session(owner_id: int, session_id: int):
    db=await connect()
    try:
        return await (await db.execute('SELECT * FROM telegram_sessions WHERE id=? AND owner_id=?',(session_id,owner_id))).fetchone()
    finally:
        await db.close()


async def _disconnect_pending(owner_id: int, health: str | None=None, error: str | None=None) -> None:
    item=_PENDING.pop(int(owner_id),None)
    if not item:
        return
    try:
        await item['client'].disconnect()
    except Exception:
        pass
    if health:
        db=await connect()
        try:
            await db.execute('UPDATE telegram_sessions SET health=?,last_error=?,updated_at=? WHERE id=? AND owner_id=?',
                             (health,error,now_iso(),item['session_id'],owner_id))
            await db.commit()
        finally:
            await db.close()


async def cancel_login(owner_id: int) -> None:
    async with _PENDING_LOCK:
        await _disconnect_pending(owner_id,'cancelled','login_cancelled')


async def begin_login(owner_id: int, label: str, phone: str) -> dict:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        return {'ok':False,'error':'api_credentials_missing'}
    try:
        phone=_phone(phone)
    except ValueError:
        return {'ok':False,'error':'invalid_phone'}
    label=(label or '').strip()[:80]
    if not label:
        return {'ok':False,'error':'invalid_label'}
    try:
        from telethon import TelegramClient
    except Exception:
        return {'ok':False,'error':'telethon_not_installed'}

    async with _PENDING_LOCK:
        await _disconnect_pending(owner_id,'cancelled','replaced_by_new_login')
        os.makedirs(settings.telegram_sessions_dir,exist_ok=True)
        path=os.path.join(settings.telegram_sessions_dir,f'tg_{owner_id}_{secrets.token_hex(8)}')
        now=now_iso(); db=await connect()
        try:
            duplicate=await (await db.execute('SELECT 1 FROM telegram_sessions WHERE owner_id=? AND label=?',(owner_id,label))).fetchone()
            if duplicate:
                return {'ok':False,'error':'duplicate_label'}
            cur=await db.execute('''INSERT INTO telegram_sessions(owner_id,label,session_path,phone_hint,enabled,health,created_at,updated_at)
                VALUES(?,?,?,?,1,'awaiting_code',?,?)''',(owner_id,label,path,_phone_hint(phone),now,now))
            await db.commit(); sid=int(cur.lastrowid)
        finally:
            await db.close()

        client=TelegramClient(path,settings.telegram_api_id,settings.telegram_api_hash)
        try:
            await asyncio.wait_for(client.connect(),timeout=20)
            sent=await asyncio.wait_for(client.send_code_request(phone),timeout=30)
        except Exception as e:
            try: await client.disconnect()
            except Exception: pass
            db=await connect()
            try:
                await db.execute("UPDATE telegram_sessions SET health='failed',last_error=?,updated_at=? WHERE id=?",(str(e)[:500],now_iso(),sid)); await db.commit()
            finally: await db.close()
            return {'ok':False,'error':'send_code_failed','details':str(e)[:300]}
        _PENDING[int(owner_id)]={'client':client,'session_id':sid,'phone':phone,'phone_code_hash':sent.phone_code_hash}
        return {'ok':True,'session_id':sid,'phone_hint':_phone_hint(phone)}


async def _finalize(owner_id: int, item: dict) -> dict:
    client=item['client']; me=await client.get_me(); now=now_iso()
    username=getattr(me,'username',None)
    phone=getattr(me,'phone',None) or item.get('phone')
    db=await connect()
    try:
        await db.execute('''UPDATE telegram_sessions SET health='authorized',last_error=NULL,telegram_user_id=?,username=?,phone_hint=?,last_seen_at=?,updated_at=?
            WHERE id=? AND owner_id=?''',(int(getattr(me,'id',0) or 0) or None,username,_phone_hint(phone),now,now,item['session_id'],owner_id))
        await db.commit()
    finally: await db.close()
    try: await client.disconnect()
    finally: _PENDING.pop(int(owner_id),None)
    return {'ok':True,'authorized':True,'session_id':item['session_id'],'username':username,'phone_hint':_phone_hint(phone)}


async def submit_code(owner_id: int, code_text: str) -> dict:
    item=_PENDING.get(int(owner_id))
    if not item:
        return {'ok':False,'error':'login_not_pending'}
    code=re.sub(r'\D','',code_text or '')
    if not 4 <= len(code) <= 8:
        return {'ok':False,'error':'invalid_code_format'}
    try:
        from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError
        try:
            await item['client'].sign_in(phone=item['phone'],code=code,phone_code_hash=item['phone_code_hash'])
        except SessionPasswordNeededError:
            db=await connect()
            try:
                await db.execute("UPDATE telegram_sessions SET health='awaiting_password',updated_at=? WHERE id=?",(now_iso(),item['session_id'])); await db.commit()
            finally: await db.close()
            return {'ok':True,'password_required':True,'session_id':item['session_id']}
        except PhoneCodeInvalidError:
            return {'ok':False,'error':'invalid_code'}
        except PhoneCodeExpiredError:
            await _disconnect_pending(owner_id,'failed','phone_code_expired')
            return {'ok':False,'error':'code_expired'}
        return await _finalize(owner_id,item)
    except Exception as e:
        return {'ok':False,'error':'sign_in_failed','details':str(e)[:300]}


async def submit_password(owner_id: int, password: str) -> dict:
    item=_PENDING.get(int(owner_id))
    if not item:
        return {'ok':False,'error':'login_not_pending'}
    try:
        from telethon.errors import PasswordHashInvalidError
        try:
            await item['client'].sign_in(password=password or '')
        except PasswordHashInvalidError:
            return {'ok':False,'error':'invalid_password'}
        return await _finalize(owner_id,item)
    except Exception as e:
        return {'ok':False,'error':'password_sign_in_failed','details':str(e)[:300]}


async def verify_session(owner_id: int, session_id: int) -> dict:
    row=await get_session(owner_id,session_id)
    if not row:
        return {'ok':False,'authorized':False,'error':'session_not_found'}
    try:
        from telethon import TelegramClient
        client=TelegramClient(row['session_path'],settings.telegram_api_id,settings.telegram_api_hash)
        await asyncio.wait_for(client.connect(),timeout=12)
        try:
            authorized=bool(await asyncio.wait_for(client.is_user_authorized(),timeout=12))
            me=await client.get_me() if authorized else None
        finally:
            await client.disconnect()
        now=now_iso(); db=await connect()
        try:
            await db.execute('UPDATE telegram_sessions SET health=?,last_error=NULL,last_seen_at=?,updated_at=?,telegram_user_id=COALESCE(?,telegram_user_id),username=COALESCE(?,username),phone_hint=COALESCE(?,phone_hint) WHERE id=?',
                             ('authorized' if authorized else 'unauthorized',now,now,int(getattr(me,'id',0) or 0) or None,getattr(me,'username',None),_phone_hint(getattr(me,'phone',None)),session_id))
            await db.commit()
        finally: await db.close()
        return {'ok':True,'authorized':authorized,'session_id':session_id,'username':getattr(me,'username',None) if me else None}
    except Exception as e:
        db=await connect()
        try:
            await db.execute("UPDATE telegram_sessions SET health='error',last_error=?,updated_at=? WHERE id=?",(str(e)[:500],now_iso(),session_id)); await db.commit()
        finally: await db.close()
        return {'ok':False,'authorized':False,'error':'verify_failed','details':str(e)[:300]}


async def resolve_source(owner_id: int, session_id: int, source_ref: str) -> dict:
    row=await get_session(owner_id,session_id)
    if not row or not row['enabled']:
        return {'ok':False,'error':'session_not_available'}
    raw=(source_ref or '').strip()
    if not raw:
        return {'ok':False,'error':'invalid_source'}
    if 't.me/+' in raw or '/joinchat/' in raw:
        return {'ok':False,'error':'private_invite_not_supported','details':'انضم إلى المصدر بالحساب أولًا ثم أرسل @username أو Chat ID.'}
    if 't.me/' in raw:
        raw=raw.split('t.me/',1)[1].split('/',1)[0]
        raw='@'+raw.lstrip('@')
    elif raw.lstrip('-').isdigit():
        raw=int(raw)
    try:
        from telethon import TelegramClient, utils
        client=TelegramClient(row['session_path'],settings.telegram_api_id,settings.telegram_api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {'ok':False,'error':'session_not_authorized'}
            entity=await client.get_entity(raw)
            chat_id=int(utils.get_peer_id(entity))
            return {'ok':True,'chat_id':chat_id,'title':getattr(entity,'title',None),'username':getattr(entity,'username',None)}
        finally:
            await client.disconnect()
    except Exception as e:
        return {'ok':False,'error':'source_not_accessible','details':str(e)[:300]}


async def source_session(owner_id: int, source) -> dict | None:
    sid=int(source['telegram_session_id'] or 0) if 'telegram_session_id' in source.keys() else 0
    db=await connect()
    try:
        row=None
        if sid:
            row=await (await db.execute("SELECT * FROM telegram_sessions WHERE id=? AND owner_id=? AND enabled=1",(sid,owner_id))).fetchone()
        if row is None:
            row=await (await db.execute("SELECT * FROM telegram_sessions WHERE owner_id=? AND enabled=1 AND health IN ('authorized','authorized_unverified') ORDER BY id LIMIT 1",(owner_id,))).fetchone()
        if row:
            return dict(row)
    finally:
        await db.close()
    if session_file(settings.telegram_session_path).exists():
        return {'id':0,'owner_id':owner_id,'label':'legacy','session_path':settings.telegram_session_path,'enabled':1,'health':'authorized_unverified'}
    return None
