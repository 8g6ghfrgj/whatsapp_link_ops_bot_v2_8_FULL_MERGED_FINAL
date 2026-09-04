from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from ..config import settings
from ..db import connect, now_iso
from .jobs import job_signal
from .join_safety import profile_settings, jitter_delay, safety_status, record_result, clear_expired_cooldown
from .wa_provider import provider


TERMINAL = {'joined', 'already_member', 'pending_approval', 'admins_not_accepting', 'duplicate_group', 'failed'}
_ACCOUNT_LOCKS: dict[int, asyncio.Lock] = {}


async def _sleep_interruptible(seconds: float, job_id: int | None):
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        sig = await job_signal(job_id)
        if sig:
            return sig
        step = min(5.0, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return None


async def _save_identity(link_id: int, group_jid: str | None, subject: str | None = None) -> None:
    if not group_jid:
        return
    db = await connect()
    try:
        await db.execute(
            '''INSERT INTO whatsapp_group_identities(link_id,group_jid,subject,first_seen_at,last_seen_at)
               VALUES(?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET group_jid=excluded.group_jid,
               subject=COALESCE(excluded.subject,whatsapp_group_identities.subject),last_seen_at=excluded.last_seen_at''',
            (int(link_id), str(group_jid), (subject or '')[:240] or None, now_iso(), now_iso()),
        )
        await db.commit()
    finally:
        await db.close()


async def _same_group_done(operator_id: int, account_slot_id: int, link_id: int, group_jid: str | None):
    if not group_jid:
        return None
    db = await connect()
    try:
        return await (await db.execute(
            '''SELECT ja.id,ja.link_id,ja.status,l.original_url
               FROM join_attempts ja
               JOIN whatsapp_group_identities gi ON gi.link_id=ja.link_id
               JOIN links l ON l.id=ja.link_id
               WHERE ja.operator_id=? AND ja.account_slot_id=? AND gi.group_jid=? AND ja.link_id<>?
                 AND ja.status IN ('joined','already_member','pending_approval','duplicate_group')
               ORDER BY ja.id LIMIT 1''',
            (int(operator_id), int(account_slot_id), str(group_jid), int(link_id)),
        )).fetchone()
    finally:
        await db.close()


async def _save_attempt(operator_id: int, link_id: int, account_slot_id: int,
                        status: str, group_jid: str | None, error: str | None = None) -> None:
    now = now_iso()
    db = await connect()
    try:
        await db.execute(
            '''INSERT INTO join_attempts(operator_id,link_id,account_slot_id,status,group_jid,attempts,last_error,requested_at,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?,?,?)
               ON CONFLICT(operator_id,link_id,account_slot_id) DO UPDATE SET
                 status=excluded.status,group_jid=COALESCE(excluded.group_jid,join_attempts.group_jid),
                 attempts=join_attempts.attempts+1,last_error=excluded.last_error,
                 requested_at=CASE WHEN excluded.status='pending_approval'
                   THEN COALESCE(join_attempts.requested_at,excluded.requested_at) ELSE join_attempts.requested_at END,
                 updated_at=excluded.updated_at''',
            (int(operator_id), int(link_id), int(account_slot_id), status, group_jid,
             error, now if status == 'pending_approval' else None, now, now),
        )
        await db.commit()
    finally:
        await db.close()


async def _account_worker(operator_id, account, limit, job_id=None, item_delay=None,
                          batch_size=None, batch_rest=None, section='important',
                          safety_profile='very_safe'):
    policy = profile_settings(
        safety_profile, item_delay=item_delay, batch_size=batch_size, batch_rest=batch_rest
    )
    processed = success = pending = failed = joined = already_member = requests_sent = retry_later = 0
    duplicate_group = 0
    cancelled = paused = daily_limit_reached = safety_cooldown = False
    cooldown_until = None
    lock = _ACCOUNT_LOCKS.setdefault(int(account['id']), asyncio.Lock())
    async with lock:
        await clear_expired_cooldown(int(account['id']))
        while processed < limit:
            sig = await job_signal(job_id)
            if sig:
                cancelled = sig == 'cancel_requested'
                paused = sig == 'pause_requested'
                break
            guard = await safety_status(int(account['id']), int(policy['daily_limit']))
            if not guard['allowed']:
                daily_limit_reached = guard['reason'] == 'daily_limit'
                safety_cooldown = guard['reason'] == 'cooldown'
                cooldown_until = guard.get('cooldown_until')
                break
            db = await connect()
            try:
                fetch_limit = min(int(policy['batch_size']), limit - processed, int(guard['remaining_24h']))
                rows = await (await db.execute(
                    '''SELECT q.link_id,l.original_url,ja.status old_status
                       FROM join_queue q JOIN links l ON l.id=q.link_id
                       LEFT JOIN join_attempts ja ON ja.operator_id=q.operator_id AND ja.link_id=q.link_id AND ja.account_slot_id=?
                       WHERE q.operator_id=? AND l.category='whatsapp_group'
                         AND (?='all' OR EXISTS (SELECT 1 FROM link_sections ls WHERE ls.link_id=l.id AND ls.section=?))
                         AND NOT EXISTS (SELECT 1 FROM expired_registry er WHERE er.normalized_url=l.normalized_url)
                         AND NOT EXISTS (SELECT 1 FROM ignored_registry ir WHERE ir.normalized_url=l.normalized_url)
                         AND (ja.id IS NULL OR ja.status IN ('pending','retry_later'))
                       ORDER BY q.id LIMIT ?''',
                    (account['id'], operator_id, section, section, fetch_limit),
                )).fetchall()
            finally:
                await db.close()
            if not rows:
                if int(guard['remaining_24h']) <= 0:
                    daily_limit_reached = True
                break
            batch_provider_attempts = 0
            for row in rows:
                sig = await job_signal(job_id)
                if sig:
                    cancelled = sig == 'cancel_requested'
                    paused = sig == 'pause_requested'
                    break
                guard = await safety_status(int(account['id']), int(policy['daily_limit']))
                if not guard['allowed']:
                    daily_limit_reached = guard['reason'] == 'daily_limit'
                    safety_cooldown = guard['reason'] == 'cooldown'
                    cooldown_until = guard.get('cooldown_until')
                    break
                url = row['original_url']
                group_jid = subject = None
                try:
                    info = await provider.invite_info(account['provider_account_id'], url)
                    group = info.get('group') or {}
                    group_jid = group.get('jid')
                    subject = group.get('subject') or group.get('name')
                except Exception:
                    pass
                await _save_identity(int(row['link_id']), group_jid, subject)
                previous = await _same_group_done(
                    operator_id, int(account['id']), int(row['link_id']), group_jid
                )
                if previous:
                    await _save_attempt(
                        operator_id, int(row['link_id']), int(account['id']), 'duplicate_group',
                        group_jid, f"same_group_as_link_id={previous['link_id']} status={previous['status']}",
                    )
                    processed += 1
                    duplicate_group += 1
                    success += 1
                    continue
                try:
                    out = await provider.join(account['provider_account_id'], url)
                except Exception as exc:
                    out = {'ok': False, 'status': 'retry_later', 'error': str(exc)}
                batch_provider_attempts += 1
                if not group_jid and out.get('jid'):
                    group_jid = out.get('jid')
                    await _save_identity(int(row['link_id']), group_jid, subject)
                raw_status = out.get('status') or ('joined' if out.get('ok') else 'failed')
                mapped = {
                    'joined': 'joined', 'already_member': 'already_member',
                    'pending_or_approval_required': 'pending_approval',
                    'retry_later': 'retry_later',
                }.get(raw_status, 'failed')
                await _save_attempt(
                    operator_id, int(row['link_id']), int(account['id']), mapped,
                    group_jid, out.get('error'),
                )
                circuit = await record_result(
                    operator_id, int(account['id']), int(row['link_id']), policy['profile'], mapped,
                    {'provider_status': raw_status, 'error': out.get('error')},
                )
                processed += 1
                if mapped == 'joined':
                    joined += 1; success += 1
                elif mapped == 'already_member':
                    already_member += 1; success += 1
                elif mapped == 'pending_approval':
                    pending += 1; requests_sent += 1
                elif mapped == 'retry_later':
                    retry_later += 1; failed += 1
                else:
                    failed += 1
                if circuit['circuit_open']:
                    safety_cooldown = True
                    cooldown_until = circuit['cooldown_until']
                    break
                sig = await _sleep_interruptible(jitter_delay(policy), job_id)
                if sig:
                    cancelled = sig == 'cancel_requested'
                    paused = sig == 'pause_requested'
                    break
            if cancelled or paused or daily_limit_reached or safety_cooldown:
                break
            if len(rows) < fetch_limit:
                break
            if processed < limit and batch_provider_attempts:
                sig = await _sleep_interruptible(int(policy['batch_rest']), job_id)
                if sig:
                    cancelled = sig == 'cancel_requested'
                    paused = sig == 'pause_requested'
                    break
    final_guard = await safety_status(int(account['id']), int(policy['daily_limit']))
    return {
        'account': account['label'], 'account_slot_id': int(account['id']),
        'processed': processed, 'success': success, 'joined': joined,
        'already_member': already_member, 'duplicate_group': duplicate_group,
        'pending': pending, 'requests_sent': requests_sent,
        'retry_later': retry_later, 'failed': failed,
        'cancelled': cancelled, 'paused': paused,
        'daily_limit_reached': daily_limit_reached,
        'safety_cooldown': safety_cooldown,
        'cooldown_until': cooldown_until,
        'used_24h': final_guard['used_24h'],
        'remaining_24h': final_guard['remaining_24h'],
        'safety': policy,
    }


async def recheck_pending(operator_id: int):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    db = await connect()
    try:
        if operator_id == settings.owner_id:
            accounts = await (await db.execute(
                "SELECT * FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id"
            )).fetchall()
        else:
            accounts = await (await db.execute(
                "SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected'",
                (operator_id,),
            )).fetchall()
    finally:
        await db.close()
    changed = 0
    for account in accounts:
        try:
            groups = (await provider.groups(account['provider_account_id'])).get('groups') or []
        except Exception:
            continue
        joined_groups = {g.get('jid') for g in groups}
        db = await connect()
        try:
            rows = await (await db.execute(
                "SELECT * FROM join_attempts WHERE operator_id=? AND account_slot_id=? AND status='pending_approval'",
                (operator_id, account['id']),
            )).fetchall()
            for row in rows:
                if row['group_jid'] and row['group_jid'] in joined_groups:
                    await db.execute(
                        "UPDATE join_attempts SET status='joined',updated_at=? WHERE id=?",
                        (now_iso(), row['id']),
                    )
                    changed += 1
                elif row['requested_at'] and row['requested_at'] <= cutoff:
                    await db.execute(
                        "UPDATE join_attempts SET status='admins_not_accepting',updated_at=? WHERE id=?",
                        (now_iso(), row['id']),
                    )
                    changed += 1
            await db.commit()
        finally:
            await db.close()
    return changed


async def process_operator(operator_id: int, per_account_limit: int | None = None,
                           job_id: int | None = None, item_delay: int | None = None,
                           batch_size: int | None = None, batch_rest: int | None = None,
                           account_slot_id: int | None = None, section: str = 'important',
                           safety_profile: str = 'very_safe'):
    await recheck_pending(operator_id)
    db = await connect()
    try:
        if account_slot_id:
            if operator_id == settings.owner_id:
                accounts = await (await db.execute(
                    "SELECT * FROM account_slots WHERE id=? AND enabled=1 AND health='connected'", (account_slot_id,)
                )).fetchall()
            else:
                accounts = await (await db.execute(
                    "SELECT * FROM account_slots WHERE id=? AND operator_id=? AND enabled=1 AND health='connected'",
                    (account_slot_id, operator_id),
                )).fetchall()
        elif operator_id == settings.owner_id:
            accounts = await (await db.execute(
                "SELECT * FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id"
            )).fetchall()
        else:
            accounts = await (await db.execute(
                "SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected' ORDER BY id",
                (operator_id,),
            )).fetchall()
    finally:
        await db.close()
    if not accounts:
        return {'error': 'no_connected_accounts', 'cancelled': False, 'paused': False, 'section': section}
    db = await connect()
    try:
        total = int((await (await db.execute(
            '''SELECT COUNT(*) c FROM join_queue q JOIN links l ON l.id=q.link_id
               WHERE q.operator_id=? AND l.category='whatsapp_group'
                 AND (?='all' OR EXISTS (SELECT 1 FROM link_sections ls WHERE ls.link_id=l.id AND ls.section=?))''',
            (operator_id, section, section),
        )).fetchone())['c'])
    finally:
        await db.close()
    limit = int(per_account_limit or total)
    results = await asyncio.gather(*(
        _account_worker(operator_id, account, limit, job_id, item_delay, batch_size,
                        batch_rest, section, safety_profile)
        for account in accounts
    ), return_exceptions=True)
    output = []
    cancelled = paused = rate_limited = daily_limited = False
    for account, result in zip(accounts, results):
        if isinstance(result, Exception):
            output.append({'account': account['label'], 'error': str(result)})
        else:
            output.append(result)
            cancelled = cancelled or bool(result.get('cancelled'))
            paused = paused or bool(result.get('paused'))
            rate_limited = rate_limited or bool(result.get('safety_cooldown'))
            daily_limited = daily_limited or bool(result.get('daily_limit_reached'))
    totals = {key: sum(int(r.get(key, 0)) for r in output if isinstance(r, dict)) for key in (
        'processed', 'joined', 'already_member', 'duplicate_group', 'requests_sent',
        'pending', 'retry_later', 'failed',
    )}
    return {
        'section': section, 'account_slot_id': account_slot_id,
        'per_account_limit': limit, 'safety_profile': safety_profile,
        'accounts': output, 'totals': totals,
        'cancelled': cancelled, 'paused': paused,
        'rate_limited': rate_limited, 'daily_limit_reached': daily_limited,
    }
