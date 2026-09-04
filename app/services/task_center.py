from __future__ import annotations

import asyncio, json
from datetime import datetime, timedelta, timezone
from aiogram.types import FSInputFile
from ..config import settings
from ..db import connect, now_iso, set_account_status
from .wa_provider import provider
from .system_tools import create_safe_backup_zip, write_diagnostics_file
from .admin_tools import log_admin_event, log_system_error
from .retention import archive_old_messages


def _dt(value:str|None):
    if not value:return None
    try:return datetime.fromisoformat(value.replace('Z','+00:00'))
    except Exception:return None


async def create_scheduled_task(owner_id:int,action:str,title:str,delay_minutes:int,payload:dict|None=None,recurrence_minutes:int=0,priority:str='normal') -> int:
    allowed={'reminder','backup','diagnostic','health_check','message_archive'}
    if action not in allowed:raise ValueError('unsupported scheduled action')
    now=datetime.now(timezone.utc); run_at=now+timedelta(minutes=max(0,int(delay_minutes)))
    db=await connect()
    try:
        cur=await db.execute("""INSERT INTO scheduled_tasks(owner_id,action,title,payload_json,status,priority,run_at,recurrence_minutes,created_at,updated_at)
            VALUES(?,?,?,?, 'scheduled', ?, ?, ?, ?, ?)""",
            (int(owner_id),action,title[:120],json.dumps(payload or {},ensure_ascii=False),priority,run_at.isoformat(),max(0,int(recurrence_minutes)),now.isoformat(),now.isoformat()))
        await db.commit(); tid=int(cur.lastrowid)
    finally:await db.close()
    await log_admin_event(owner_id,'scheduled_task_created','scheduled_task',tid,{'action':action,'run_at':run_at.isoformat(),'recurrence_minutes':recurrence_minutes})
    return tid


async def list_scheduled_tasks(owner_id:int,limit:int=30):
    db=await connect()
    try:return await (await db.execute("SELECT * FROM scheduled_tasks WHERE owner_id=? ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'scheduled' THEN 1 ELSE 2 END, run_at,id DESC LIMIT ?",(int(owner_id),max(1,min(100,int(limit)))))).fetchall()
    finally:await db.close()


async def cancel_scheduled_task(owner_id:int,task_id:int)->bool:
    db=await connect()
    try:
        cur=await db.execute("UPDATE scheduled_tasks SET status='cancelled',updated_at=? WHERE id=? AND owner_id=? AND status IN ('scheduled','failed')",(now_iso(),int(task_id),int(owner_id)))
        await db.commit(); ok=cur.rowcount>0
    finally: await db.close()
    if ok:await log_admin_event(owner_id,'scheduled_task_cancelled','scheduled_task',task_id)
    return ok


async def check_account_health(owner_id:int|None=None):
    db=await connect()
    try:
        if owner_id is None:
            rows=await (await db.execute('SELECT * FROM account_slots WHERE enabled=1 ORDER BY id')).fetchall()
        else:
            rows=await (await db.execute('SELECT * FROM account_slots WHERE enabled=1 AND operator_id=? ORDER BY id',(int(owner_id),))).fetchall()
    finally:await db.close()
    results=[]
    for r in rows:
        try:
            st=await asyncio.wait_for(provider.status(r['provider_account_id']),timeout=settings.health_check_timeout_seconds)
            health=st.get('status','unknown'); me=st.get('me') or {}
            await set_account_status(int(r['id']),int(r['operator_id']),health,st.get('last_error'),str(me.get('id') or '') or None)
            base={'connected':100,'open':100,'connecting':70,'qr':40,'not_linked':20,'closed':10,'provider_error':0}.get(health,50)
            if st.get('last_error'):base=max(0,base-20)
            results.append({'id':int(r['id']),'label':r['label'],'health':health,'score':base,'error':st.get('last_error')})
        except Exception as e:
            await set_account_status(int(r['id']),int(r['operator_id']),'provider_error',str(e))
            await log_system_error('account_health',str(e),f"account_slot={r['id']}")
            results.append({'id':int(r['id']),'label':r['label'],'health':'provider_error','score':0,'error':str(e)})
    return results


async def _claim_due():
    now=now_iso(); db=await connect()
    try:
        row=await (await db.execute("SELECT * FROM scheduled_tasks WHERE status='scheduled' AND run_at<=? ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, run_at,id LIMIT 1",(now,))).fetchone()
        if not row:return None
        cur=await db.execute("UPDATE scheduled_tasks SET status='running',updated_at=? WHERE id=? AND status='scheduled'",(now,int(row['id'])))
        await db.commit()
        return row if cur.rowcount else None
    finally:await db.close()


async def _finish(row,status:str,error:str|None=None):
    recurrence=max(0,int(row['recurrence_minutes'] or 0)); now=datetime.now(timezone.utc)
    db=await connect()
    try:
        if recurrence>0:
            nxt=now+timedelta(minutes=recurrence)
            await db.execute("UPDATE scheduled_tasks SET status='scheduled',last_run_at=?,run_at=?,next_run_at=?,run_count=run_count+1,last_error=?,updated_at=? WHERE id=?",
                (now.isoformat(),nxt.isoformat(),nxt.isoformat(),error,now.isoformat(),int(row['id'])))
        else:
            await db.execute("UPDATE scheduled_tasks SET status=?,last_run_at=?,run_count=run_count+1,last_error=?,updated_at=? WHERE id=?",
                (status,now.isoformat(),error,now.isoformat(),int(row['id'])))
        await db.commit()
    finally:await db.close()


async def _execute(bot,row):
    payload=json.loads(row['payload_json'] or '{}'); owner_id=int(row['owner_id']); action=row['action']
    if action=='reminder':
        await bot.send_message(owner_id,'⏰ تذكير مجدول\n\n'+str(payload.get('text') or row['title']))
    elif action=='backup':
        path=await create_safe_backup_zip()
        await bot.send_document(owner_id,FSInputFile(path),caption='💾 نسخة احتياطية مجدولة آمنة — لا تحتوي الجلسات أو .env')
    elif action=='diagnostic':
        path=await write_diagnostics_file()
        await bot.send_document(owner_id,FSInputFile(path),caption='🧰 تقرير تشخيص مجدول')
    elif action=='health_check':
        rows=await check_account_health(None)
        online=sum(1 for x in rows if x['score']>=90)
        await bot.send_message(owner_id,f'🩺 فحص صحة الحسابات\nالمتصلة/السليمة: {online}/{len(rows)}\n'+('\n'.join(f"#{x['id']} {x['label']} — {x['health']} — {x['score']}%" for x in rows[:25]) or 'لا توجد حسابات.'))
    elif action=='message_archive':
        report=await archive_old_messages(owner_id)
        await bot.send_message(owner_id,f"🗜 أرشفة الرسائل القديمة\nنُقلت: {report['moved']}\nنسبة الحجم المضغوط: {report['compressed_percent']}%\nالأرشيف قابل للاستعادة ولم يُحذف نهائيًا.")
    await log_admin_event(owner_id,'scheduled_task_run','scheduled_task',int(row['id']),{'action':action})


async def scheduler_forever(bot):
    while True:
        try:
            row=await _claim_due()
            if not row:
                await asyncio.sleep(settings.scheduler_interval_seconds); continue
            try:
                await _execute(bot,row); await _finish(row,'completed')
            except asyncio.CancelledError:raise
            except Exception as e:
                await _finish(row,'failed',str(e)); await log_system_error('scheduler',str(e),f"task_id={row['id']} action={row['action']}")
        except asyncio.CancelledError:raise
        except Exception as e:
            await log_system_error('scheduler_loop',str(e)); await asyncio.sleep(settings.scheduler_interval_seconds)
