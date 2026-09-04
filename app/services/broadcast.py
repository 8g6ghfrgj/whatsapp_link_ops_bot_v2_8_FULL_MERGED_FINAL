from __future__ import annotations
import asyncio
from ..db import connect, now_iso
from ..config import settings
from .wa_provider import provider
from .jobs import job_signal

async def _sleep_interruptible(seconds:float, job_id:int|None):
    left=max(0.0,float(seconds))
    while left>0:
        sig=await job_signal(job_id)
        if sig:return sig
        step=min(5.0,left)
        await asyncio.sleep(step); left-=step
    return None

async def create_campaign(owner_id:int, account_slot_id:int, target_type:str, template_id:int,
                          target_limit_per_run:int, messages_per_target:int,
                          target_delay_seconds:int, batch_size:int, batch_rest_seconds:int,
                          repeat_total:int=1, repeat_interval_seconds:int=3600, selected_jids:list[str]|None=None)->int:
    repeat_total=max(1,min(int(repeat_total),settings.broadcast_max_repeat_cycles))
    repeat_interval_seconds=max(0,int(repeat_interval_seconds))
    db=await connect(); now=now_iso()
    try:
        if owner_id==settings.owner_id:
            acct=await (await db.execute("SELECT * FROM account_slots WHERE id=? AND enabled=1",(account_slot_id,))).fetchone()
        else:
            acct=await (await db.execute("SELECT * FROM account_slots WHERE id=? AND operator_id=? AND enabled=1",(account_slot_id,owner_id))).fetchone()
        if not acct: raise ValueError('account_not_available')
        tpl=await (await db.execute("SELECT * FROM message_templates WHERE id=? AND owner_id=? AND enabled=1",(template_id,owner_id))).fetchone()
        if not tpl: raise ValueError('template_not_found')
        cur=await db.execute('''INSERT INTO broadcast_campaigns(owner_id,account_slot_id,target_type,template_id,status,messages_per_target,target_limit_per_run,target_delay_seconds,batch_size,batch_rest_seconds,repeat_total,repeat_completed,repeat_interval_seconds,current_cycle,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(owner_id,account_slot_id,target_type,template_id,'queued',messages_per_target,target_limit_per_run,target_delay_seconds,batch_size,batch_rest_seconds,repeat_total,0,repeat_interval_seconds,1,now,now))
        cid=int(cur.lastrowid); await db.commit()
    finally: await db.close()

    targets=[]
    if target_type=='group':
        data=await provider.groups(acct['provider_account_id'])
        for g in sorted(data.get('groups') or [], key=lambda x:((x.get('subject') or '').casefold(),x.get('jid') or '')):
            jid=g.get('jid')
            if jid: targets.append((jid,g.get('subject') or jid,'group'))
    elif target_type=='chat':
        db=await connect()
        try:
            rows=await (await db.execute('''SELECT remote_jid,MAX(id) last_id FROM wa_messages
              WHERE account_slot_id=? AND remote_jid IS NOT NULL
                AND remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter'
                AND remote_jid NOT IN ('status@broadcast')
              GROUP BY remote_jid ORDER BY last_id DESC''',(account_slot_id,))).fetchall()
        finally: await db.close()
        targets=[(r['remote_jid'],r['remote_jid'],'chat') for r in rows if r['remote_jid']]
    else:
        raise ValueError('unsupported_target_type')

    if selected_jids:
        wanted=set(selected_jids); targets=[t for t in targets if t[0] in wanted]
    db=await connect(); now=now_iso()
    try:
        for jid,name,typ in targets:
            sup=await (await db.execute('SELECT 1 FROM send_suppression WHERE owner_id=? AND target_jid=?',(owner_id,jid))).fetchone()
            if sup: continue
            await db.execute('INSERT OR IGNORE INTO broadcast_targets(campaign_id,target_jid,display_name,target_type,status,updated_at) VALUES(?,?,?,?,?,?)',(cid,jid,name,typ,'pending',now))
        total=(await (await db.execute('SELECT COUNT(*) c FROM broadcast_targets WHERE campaign_id=?',(cid,))).fetchone())['c']
        await db.execute('UPDATE broadcast_campaigns SET total_targets=?,updated_at=? WHERE id=?',(total,now_iso(),cid)); await db.commit()
    finally: await db.close()
    return cid

async def campaign_summary(campaign_id:int, owner_id:int):
    db=await connect()
    try:
        c=await (await db.execute('''SELECT c.*,a.label account_label,t.name template_name FROM broadcast_campaigns c
          JOIN account_slots a ON a.id=c.account_slot_id JOIN message_templates t ON t.id=c.template_id
          WHERE c.id=? AND c.owner_id=?''',(campaign_id,owner_id))).fetchone()
        if not c:return None
        rows=await (await db.execute('SELECT status,COUNT(*) c FROM broadcast_targets WHERE campaign_id=? GROUP BY status',(campaign_id,))).fetchall()
        return dict(c)|{'counts':{r['status']:r['c'] for r in rows}}
    finally: await db.close()

async def _reset_for_next_cycle(campaign_id:int):
    db=await connect()
    try:
        c=await (await db.execute('SELECT repeat_completed,repeat_total,current_cycle FROM broadcast_campaigns WHERE id=?',(campaign_id,))).fetchone()
        completed=int(c['repeat_completed'])+1
        if completed>=int(c['repeat_total']):
            await db.execute("UPDATE broadcast_campaigns SET repeat_completed=?,status='completed',updated_at=? WHERE id=?",(completed,now_iso(),campaign_id))
            await db.commit(); return False,completed
        await db.execute("UPDATE broadcast_targets SET status='pending',messages_sent=0,last_error=NULL,updated_at=? WHERE campaign_id=? AND status IN ('sent','failed','cancelled','retry_later')",(now_iso(),campaign_id))
        await db.execute("UPDATE broadcast_campaigns SET repeat_completed=?,current_cycle=current_cycle+1,status='waiting_repeat',sent_targets=0,failed_targets=0,updated_at=? WHERE id=?",(completed,now_iso(),campaign_id))
        await db.commit(); return True,completed
    finally: await db.close()

async def run_campaign(campaign_id:int, owner_id:int, job_id:int|None=None):
    total_run_processed=total_run_sent=total_run_failed=0
    while True:
        db=await connect()
        try:
            c=await (await db.execute('''SELECT c.*,a.provider_account_id,t.body FROM broadcast_campaigns c
              JOIN account_slots a ON a.id=c.account_slot_id JOIN message_templates t ON t.id=c.template_id
              WHERE c.id=? AND c.owner_id=?''',(campaign_id,owner_id))).fetchone()
            if not c:return {'error':'campaign_not_found'}
            await db.execute("UPDATE broadcast_campaigns SET status='running',updated_at=? WHERE id=?",(now_iso(),campaign_id)); await db.commit()
        finally: await db.close()

        max_this_run=int(c['target_limit_per_run'] or 0)
        processed=sent_targets=failed_targets=0; throttled=False; signal=None
        while True:
            signal=await job_signal(job_id)
            if signal: break
            if max_this_run and processed>=max_this_run: break
            db=await connect()
            try:
                r=await (await db.execute("SELECT * FROM broadcast_targets WHERE campaign_id=? AND status IN ('pending','retry_later') ORDER BY id LIMIT 1",(campaign_id,))).fetchone()
            finally: await db.close()
            if not r: break

            ok=True; err=None; msgsent=0
            for i in range(int(c['messages_per_target'])):
                signal=await job_signal(job_id)
                if signal: ok=False; err=signal; break
                try:
                    out=await provider.send_text(c['provider_account_id'],r['target_jid'],c['body'])
                    if not out.get('ok'):
                        ok=False; err=out.get('error') or out.get('status') or 'send_failed'
                        if out.get('status')=='retry_later': throttled=True
                        break
                    msgsent+=1
                except Exception as e:
                    ok=False; err=str(e)
                    low=err.lower(); throttled=any(x in low for x in ('429','rate','too many','later','temporar'))
                    break
                if i+1<int(c['messages_per_target']):
                    signal=await _sleep_interruptible(0,job_id)
                    if signal: break
            st='sent' if ok and msgsent==int(c['messages_per_target']) else ('retry_later' if throttled else ('pending' if signal=='pause_requested' else ('cancelled' if signal=='cancel_requested' else 'failed')))
            db=await connect()
            try:
                await db.execute('UPDATE broadcast_targets SET status=?,messages_sent=messages_sent+?,last_error=?,updated_at=? WHERE id=?',(st,msgsent,err,now_iso(),r['id']))
                await db.commit()
            finally: await db.close()
            processed+=1
            if st=='sent': sent_targets+=1
            elif st=='failed': failed_targets+=1
            if signal or throttled: break

            if processed % int(c['batch_size']) == 0:
                signal=await _sleep_interruptible(int(c['batch_rest_seconds']),job_id)
            else:
                signal=await _sleep_interruptible(int(c['target_delay_seconds']),job_id)
            if signal: break

        total_run_processed+=processed; total_run_sent+=sent_targets; total_run_failed+=failed_targets
        db=await connect()
        try:
            counts={r['status']:r['c'] for r in await (await db.execute('SELECT status,COUNT(*) c FROM broadcast_targets WHERE campaign_id=? GROUP BY status',(campaign_id,))).fetchall()}
            pending=int(counts.get('pending',0))+int(counts.get('retry_later',0))
            if signal=='pause_requested': status='paused'
            elif signal=='cancel_requested': status='cancelled'
            elif throttled: status='paused_rate_limit'
            elif pending: status='partial'
            else: status='cycle_completed'
            await db.execute('UPDATE broadcast_campaigns SET status=?,sent_targets=?,failed_targets=?,updated_at=? WHERE id=?',(status,int(counts.get('sent',0)),int(counts.get('failed',0)),now_iso(),campaign_id)); await db.commit()
        finally: await db.close()

        if status!='cycle_completed':
            return {'campaign_id':campaign_id,'status':status,'processed_this_run':total_run_processed,'sent_this_run':total_run_sent,'failed_this_run':total_run_failed,'remaining':pending,'counts':counts,'cycle':int(c['current_cycle']),'repeat_total':int(c['repeat_total'])}

        has_next,completed=await _reset_for_next_cycle(campaign_id)
        if not has_next:
            return {'campaign_id':campaign_id,'status':'completed','processed_this_run':total_run_processed,'sent_this_run':total_run_sent,'failed_this_run':total_run_failed,'remaining':0,'counts':counts,'cycle':int(c['current_cycle']),'repeat_total':int(c['repeat_total']),'repeat_completed':completed}

        # Repeat only after a full cycle. A finite cycle count + minimum interval avoids accidental endless sends.
        signal=await _sleep_interruptible(int(c['repeat_interval_seconds']),job_id)
        if signal:
            db=await connect()
            try:
                st='paused' if signal=='pause_requested' else 'cancelled'
                await db.execute('UPDATE broadcast_campaigns SET status=?,updated_at=? WHERE id=?',(st,now_iso(),campaign_id)); await db.commit()
            finally: await db.close()
            return {'campaign_id':campaign_id,'status':st,'processed_this_run':total_run_processed,'sent_this_run':total_run_sent,'failed_this_run':total_run_failed,'remaining':int(c['total_targets']),'cycle':int(c['current_cycle'])+1,'repeat_total':int(c['repeat_total']),'repeat_completed':completed}
