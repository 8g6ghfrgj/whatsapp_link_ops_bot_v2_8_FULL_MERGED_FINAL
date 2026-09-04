from __future__ import annotations
import json
import time
from ..db import connect, now_iso
from ..config import settings
from ..link_utils import extract_urls, normalize_url, classify_link, canonical_section

PERIOD_SECONDS = {'24h':86400,'3d':259200,'7d':604800,'14d':1209600,'30d':2592000}


def _matches_category(requested: str, actual: str) -> bool:
    if requested == 'all':
        return True
    if requested == 'whatsapp':
        return actual.startswith('whatsapp_')
    return requested == actual


async def _stop_signal(job_id: int | None) -> str | None:
    if not job_id:
        return None
    db = await connect()
    try:
        row = await (await db.execute('SELECT status FROM jobs WHERE id=?', (job_id,))).fetchone()
        if not row: return None
        st=row['status']
        return st if st in {'cancel_requested','pause_requested'} else None
    finally:
        await db.close()


async def _job_progress(job_id: int | None, report: dict) -> None:
    if not job_id:
        return
    db = await connect()
    try:
        await db.execute(
            'UPDATE jobs SET report_json=?,updated_at=? WHERE id=?',
            (json.dumps(report, ensure_ascii=False), now_iso(), job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _upsert_on_connection(db, url: str, normalized: str, category: str, operator_id: int, source: str, section: str='important'):
    """Fast link upsert using the caller transaction; returns new/dup/blocked."""
    blocked = await (await db.execute(
        'SELECT 1 FROM expired_registry WHERE normalized_url=? UNION SELECT 1 FROM ignored_registry WHERE normalized_url=?', (normalized, normalized)
    )).fetchone()
    if blocked:
        return 'blocked'
    now = now_iso(); section=canonical_section(section,category)
    row = await (await db.execute('SELECT id FROM links WHERE normalized_url=?', (normalized,))).fetchone()
    if row:
        lid = int(row['id'])
        await db.execute(
            "UPDATE links SET last_seen_at=?,seen_count=seen_count+1,category=?,section=CASE WHEN ?='whatsapp_channel' THEN 'channels' ELSE section END WHERE id=?",
            (now,category,category,lid),
        )
        result = 'duplicate'
    else:
        cur = await db.execute(
            '''INSERT INTO links(normalized_url,original_url,category,section,first_seen_at,last_seen_at,first_operator_id,source)
               VALUES(?,?,?,?,?,?,?,?)''',
            (normalized,url,category,section,now,now,operator_id,source),
        )
        lid = int(cur.lastrowid)
        result = 'new'
    if category=='whatsapp_channel':
        await db.execute("DELETE FROM link_sections WHERE link_id=? AND section<>'channels'",(lid,))
        await db.execute('DELETE FROM join_queue WHERE link_id=?',(lid,))
    await db.execute('INSERT OR IGNORE INTO link_sections(link_id,section,first_seen_at) VALUES(?,?,?)',(lid,section,now))
    await db.execute(
        'INSERT INTO occurrences(link_id,operator_id,source,seen_at) VALUES(?,?,?,?)',
        (lid, operator_id, source, now),
    )
    return result


async def collect_from_accounts(operator_id:int, category:str, mode='fast', period='new', source_jid='', job_id:int|None=None):
    db = await connect()
    try:
        if operator_id == settings.owner_id:
            accounts = await (await db.execute('SELECT * FROM account_slots WHERE enabled=1 ORDER BY operator_id,id')).fetchall()
        else:
            accounts = await (await db.execute('SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 ORDER BY id',(operator_id,))).fetchall()
    finally:
        await db.close()

    report=[]
    totals = {
        'new':0, 'duplicates':0, 'blocked':0, 'messages':0,
        'text_messages':0, 'url_messages':0, 'urls_found':0, 'matching_urls':0,
        'accounts':report, 'cancelled':False, 'paused':False, 'smart_channels':0,
    }
    cutoff = 0 if period in {'all','new'} else int(time.time()) - PERIOD_SECONDS.get(period,0)

    for a in accounts:
        sig=await _stop_signal(job_id)
        if sig:
            totals['paused'] = sig=='pause_requested'
            totals['cancelled'] = sig=='cancel_requested'
            break
        slot = int(a['id'])
        db = await connect()
        try:
            cursor = await (await db.execute(
                '''SELECT last_message_row_id FROM collection_cursors
                   WHERE operator_id=? AND account_slot_id=? AND category=? AND source_jid=?''',
                (operator_id, slot, category, source_jid),
            )).fetchone()
            start = int(cursor['last_message_row_id']) if cursor and period == 'new' else 0

            where = ['account_slot_id=?', 'id>?']
            args = [slot, start]
            if source_jid:
                where.append('remote_jid=?'); args.append(source_jid)
            if cutoff:
                where.append('message_ts>=?'); args.append(cutoff)
            where_sql = ' AND '.join(where)

            summary = await (await db.execute(
                f'''SELECT COUNT(*) total,
                           SUM(CASE WHEN length(trim(text))>0 THEN 1 ELSE 0 END) text_count,
                           SUM(CASE WHEN text LIKE '%http://%' OR text LIKE '%https://%' OR text LIKE '%www.%' OR text LIKE '%chat.whatsapp.com/%' OR text LIKE '%wa.me/%' OR text LIKE '%whatsapp.com/channel/%' THEN 1 ELSE 0 END) url_count,
                           COALESCE(MAX(id),?) max_id
                    FROM wa_messages WHERE {where_sql}''',
                [start] + args,
            )).fetchone()
            eligible_total = int(summary['total'] or 0)
            text_count = int(summary['text_count'] or 0)
            url_msg_count = int(summary['url_count'] or 0)
            high_water = int(summary['max_id'] or start)

            query = f'SELECT id,remote_jid,text,message_ts FROM wa_messages WHERE {where_sql}'
            qargs = list(args)
            if mode == 'fast':
                query += " AND (text LIKE '%http://%' OR text LIKE '%https://%' OR text LIKE '%www.%' OR text LIKE '%chat.whatsapp.com/%' OR text LIKE '%wa.me/%' OR text LIKE '%whatsapp.com/channel/%')"
            query += ' ORDER BY id'
            cur = await db.execute(query, qargs)

            new=dup=blocked=processed=url_hits=urls_found=matching=smart_channels=0
            last_processed=start
            cancelled=False
            since_commit=0
            while True:
                rows = await cur.fetchmany(1000)
                if not rows:
                    break
                sig=await _stop_signal(job_id)
                if sig:
                    cancelled=True
                    totals['paused'] = sig=='pause_requested'
                    totals['cancelled'] = sig=='cancel_requested'
                    break
                for r in rows:
                    processed += 1
                    last_processed=int(r['id'])
                    urls = extract_urls(r['text'] or '')
                    if urls:
                        url_hits += 1
                    urls_found += len(urls)
                    for u in urls:
                        n = normalize_url(u)
                        cat = classify_link(u)
                        if not n or not _matches_category(category, cat):
                            continue
                        matching += 1
                        if cat=='whatsapp_channel':
                            smart_channels+=1
                        result = await _upsert_on_connection(
                            db, u, n, cat, operator_id, f"wa:{slot}:{r['remote_jid']}"
                        )
                        if result == 'new': new += 1
                        elif result == 'duplicate': dup += 1
                        else: blocked += 1
                        since_commit += 1
                        if since_commit >= settings.collector_db_batch:
                            await db.commit(); since_commit = 0
                await _job_progress(job_id, {
                    **{k:v for k,v in totals.items() if k != 'accounts'},
                    'current_account': a['label'], 'current_processed': processed,
                })
            await db.commit()

            # Advance the high-water to all eligible messages only if this account was not cancelled.
            # This avoids rescanning text-only rows forever in fast mode.
            cursor_target = last_processed if cancelled else high_water
            if cursor_target > start:
                await db.execute(
                    '''INSERT INTO collection_cursors(operator_id,account_slot_id,category,source_jid,last_message_row_id,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(operator_id,account_slot_id,category,source_jid)
                       DO UPDATE SET last_message_row_id=MAX(collection_cursors.last_message_row_id,excluded.last_message_row_id),updated_at=excluded.updated_at''',
                    (operator_id, slot, category, source_jid, cursor_target, now_iso()),
                )
                await db.commit()

            item = {
                'slot_id':slot, 'label':a['label'], 'messages':processed,
                'eligible_messages':eligible_total, 'text_messages':text_count,
                'url_messages':url_msg_count, 'urls_found':urls_found,
                'matching_urls':matching, 'new':new, 'duplicates':dup,
                'blocked':blocked, 'completed':not cancelled,
                'smart_channels':smart_channels,
            }
            report.append(item)
            totals['messages'] += processed
            totals['text_messages'] += text_count
            totals['url_messages'] += url_msg_count
            totals['urls_found'] += urls_found
            totals['matching_urls'] += matching
            totals['new'] += new
            totals['duplicates'] += dup
            totals['blocked'] += blocked
            totals['smart_channels'] += smart_channels
            if cancelled:
                if not totals.get('paused'): totals['cancelled'] = True
                break
        finally:
            await db.close()

    await _job_progress(job_id, totals)
    return totals
