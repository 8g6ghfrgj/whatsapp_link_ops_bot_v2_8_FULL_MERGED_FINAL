from __future__ import annotations
import json
from ..db import connect, now_iso

async def create_job(operator_id:int, kind:str, payload:dict|None=None) -> int:
    db=await connect(); now=now_iso()
    try:
        cur=await db.execute(
            "INSERT INTO jobs(operator_id,kind,status,payload_json,created_at,updated_at) VALUES(?,?, 'queued', ?, ?, ?)",
            (operator_id,kind,json.dumps(payload or {},ensure_ascii=False),now,now),
        )
        await db.commit(); return int(cur.lastrowid)
    finally: await db.close()

async def set_job(job_id:int,status:str,report:dict|None=None):
    db=await connect()
    try:
        if report is None:
            await db.execute('UPDATE jobs SET status=?,updated_at=? WHERE id=?',(status,now_iso(),job_id))
        else:
            await db.execute('UPDATE jobs SET status=?,report_json=?,updated_at=? WHERE id=?',(
                status,json.dumps(report,ensure_ascii=False),now_iso(),job_id))
        await db.commit()
    finally: await db.close()


async def record_completed_job(operator_id:int,kind:str,payload:dict|None,report:dict,status:str='completed')->int:
    """Record a short synchronous action in the same task center as workers."""
    job_id=await create_job(operator_id,kind,payload)
    await set_job(job_id,status,report)
    return job_id

async def job_signal(job_id:int|None)->str|None:
    if not job_id:return None
    db=await connect()
    try:
        r=await (await db.execute('SELECT status FROM jobs WHERE id=?',(job_id,))).fetchone()
        if not r:return None
        st=r['status']
        return st if st in {'cancel_requested','pause_requested'} else None
    finally: await db.close()

async def is_cancel_requested(job_id:int|None)->bool:
    return (await job_signal(job_id))=='cancel_requested'

async def is_pause_requested(job_id:int|None)->bool:
    return (await job_signal(job_id))=='pause_requested'

async def request_cancel(operator_id:int,job_id:int)->bool:
    db=await connect()
    try:
        cur=await db.execute(
            "UPDATE jobs SET status='cancel_requested',updated_at=? WHERE id=? AND operator_id=? AND status IN ('queued','running','pause_requested')",
            (now_iso(),job_id,operator_id),
        )
        await db.commit(); return cur.rowcount>0
    finally: await db.close()

async def request_pause(operator_id:int,job_id:int)->bool:
    db=await connect()
    try:
        cur=await db.execute(
            "UPDATE jobs SET status='pause_requested',updated_at=? WHERE id=? AND operator_id=? AND status IN ('queued','running')",
            (now_iso(),job_id,operator_id),
        )
        await db.commit(); return cur.rowcount>0
    finally: await db.close()

async def recover_interrupted_jobs():
    db=await connect()
    try:
        await db.execute("UPDATE jobs SET status='interrupted',updated_at=? WHERE status IN ('queued','running','cancel_requested','pause_requested')",(now_iso(),))
        await db.commit()
    finally: await db.close()


async def hide_job(operator_id:int, job_id:int)->bool:
    db=await connect()
    try:
        r=await (await db.execute('SELECT status FROM jobs WHERE id=? AND operator_id=?',(job_id,operator_id))).fetchone()
        if not r:return False
        if r['status'] in {'queued','running','pause_requested','cancel_requested'}:
            await db.execute("UPDATE jobs SET status='cancel_requested',hidden=1,updated_at=? WHERE id=? AND operator_id=?",(now_iso(),job_id,operator_id))
        else:
            await db.execute('UPDATE jobs SET hidden=1,updated_at=? WHERE id=? AND operator_id=?',(now_iso(),job_id,operator_id))
        await db.commit(); return True
    finally: await db.close()
