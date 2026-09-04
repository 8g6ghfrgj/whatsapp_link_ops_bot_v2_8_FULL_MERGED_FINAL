from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..db import connect, now_iso


PROFILE_LABELS = {
    'very_safe': 'آمن جدًا (موصى به)',
    'balanced': 'متوازن — إعدادك السابق',
    'custom': 'مخصص مع قاطع حماية',
}


def profile_settings(profile: str, *, item_delay: int | None = None,
                     batch_size: int | None = None, batch_rest: int | None = None) -> dict:
    profile = profile if profile in PROFILE_LABELS else 'very_safe'
    if profile == 'very_safe':
        low = settings.join_safe_min_delay_seconds
        high = max(low, settings.join_safe_max_delay_seconds)
        return {
            'profile': profile, 'daily_limit': settings.join_safe_daily_limit,
            'min_delay': low, 'max_delay': high,
            'batch_size': settings.join_safe_batch_size,
            'batch_rest': settings.join_safe_batch_rest_seconds,
        }
    if profile == 'balanced':
        return {
            'profile': profile, 'daily_limit': settings.join_balanced_daily_limit,
            'min_delay': 30, 'max_delay': 60,
            'batch_size': 10, 'batch_rest': 3600,
        }
    # Custom settings remain bounded by the same per-account daily circuit
    # breaker and never permit zero-delay rapid joining.
    low = max(15, int(item_delay if item_delay is not None else settings.join_item_delay_seconds))
    return {
        'profile': profile, 'daily_limit': settings.join_balanced_daily_limit,
        'min_delay': low, 'max_delay': max(low, min(86400, low + max(5, low // 2))),
        'batch_size': max(1, min(20, int(batch_size if batch_size is not None else settings.join_batch_per_account))),
        'batch_rest': max(300, int(batch_rest if batch_rest is not None else settings.join_batch_rest_seconds)),
    }


def jitter_delay(profile: dict) -> float:
    return random.uniform(float(profile['min_delay']), float(profile['max_delay']))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None


async def safety_status(account_slot_id: int, daily_limit: int) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    db = await connect()
    try:
        used = int((await (await db.execute(
            'SELECT COUNT(*) c FROM join_safety_events WHERE account_slot_id=? AND created_at>=?',
            (int(account_slot_id), since),
        )).fetchone())['c'])
        state = await (await db.execute(
            'SELECT * FROM join_safety_state WHERE account_slot_id=?', (int(account_slot_id),)
        )).fetchone()
    finally:
        await db.close()
    cooldown_until = _parse_dt(state['cooldown_until']) if state else None
    if cooldown_until and cooldown_until > now:
        return {
            'allowed': False, 'reason': 'cooldown', 'used_24h': used,
            'remaining_24h': max(0, int(daily_limit) - used),
            'cooldown_until': cooldown_until.isoformat(),
            'details': state['reason'] or 'safety_circuit',
        }
    if used >= int(daily_limit):
        return {
            'allowed': False, 'reason': 'daily_limit', 'used_24h': used,
            'remaining_24h': 0, 'cooldown_until': None,
        }
    return {
        'allowed': True, 'reason': None, 'used_24h': used,
        'remaining_24h': max(0, int(daily_limit) - used), 'cooldown_until': None,
    }


async def record_result(operator_id: int, account_slot_id: int, link_id: int,
                        profile: str, status: str, details: dict | None = None) -> dict:
    details = details or {}
    now = datetime.now(timezone.utc)
    failure = status in {'failed', 'retry_later'}
    rate_limited = status == 'retry_later'
    db = await connect()
    try:
        await db.execute(
            '''INSERT INTO join_safety_events(operator_id,account_slot_id,link_id,profile,status,details_json,created_at)
               VALUES(?,?,?,?,?,?,?)''',
            (int(operator_id), int(account_slot_id), int(link_id), profile, status,
             json.dumps(details, ensure_ascii=False)[:4000], now.isoformat()),
        )
        old = await (await db.execute(
            'SELECT consecutive_failures FROM join_safety_state WHERE account_slot_id=?',
            (int(account_slot_id),),
        )).fetchone()
        failures = int(old['consecutive_failures'] or 0) if old else 0
        failures = failures + 1 if failure else 0
        cooldown_until = None
        reason = None
        if rate_limited:
            cooldown_until = now + timedelta(seconds=settings.join_rate_limit_cooldown_seconds)
            reason = 'retry_later_or_rate_limit'
        elif failures >= settings.join_failure_circuit_threshold:
            cooldown_until = now + timedelta(hours=12)
            reason = 'repeated_join_failures'
        await db.execute(
            '''INSERT INTO join_safety_state(account_slot_id,cooldown_until,reason,consecutive_failures,last_attempt_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(account_slot_id) DO UPDATE SET cooldown_until=excluded.cooldown_until,
                 reason=excluded.reason,consecutive_failures=excluded.consecutive_failures,
                 last_attempt_at=excluded.last_attempt_at,updated_at=excluded.updated_at''',
            (int(account_slot_id), cooldown_until.isoformat() if cooldown_until else None,
             reason, failures, now.isoformat(), now.isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    return {
        'circuit_open': bool(cooldown_until),
        'cooldown_until': cooldown_until.isoformat() if cooldown_until else None,
        'reason': reason, 'consecutive_failures': failures,
    }


async def clear_expired_cooldown(account_slot_id: int) -> None:
    now = now_iso()
    db = await connect()
    try:
        await db.execute(
            '''UPDATE join_safety_state SET cooldown_until=NULL,reason=NULL,updated_at=?
               WHERE account_slot_id=? AND cooldown_until IS NOT NULL AND cooldown_until<=?''',
            (now, int(account_slot_id), now),
        )
        await db.commit()
    finally:
        await db.close()
