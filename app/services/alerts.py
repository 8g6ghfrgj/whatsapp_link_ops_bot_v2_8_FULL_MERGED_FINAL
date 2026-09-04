from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..db import connect, now_iso


ALERT_TYPES = {
    'account_disconnected': 'انقطاع حساب WhatsApp',
    'telegram_source_failed': 'توقف/فشل مصدر Telegram',
    'join_safety_circuit': 'توقف الانضمام للحماية',
    'high_failure_rate': 'ارتفاع نسبة فشل مهمة',
}


async def ensure_alert_rules(owner_id: int) -> None:
    db = await connect()
    try:
        for event_type in ALERT_TYPES:
            await db.execute(
                '''INSERT OR IGNORE INTO alert_rules(owner_id,event_type,enabled,cooldown_minutes,updated_at)
                   VALUES(?,?,1,60,?)''',
                (int(owner_id), event_type, now_iso()),
            )
        await db.commit()
    finally:
        await db.close()


async def toggle_alert_rule(owner_id: int, event_type: str) -> bool:
    if event_type not in ALERT_TYPES:
        raise ValueError('unknown alert type')
    await ensure_alert_rules(owner_id)
    db = await connect()
    try:
        row = await (await db.execute(
            'SELECT enabled FROM alert_rules WHERE owner_id=? AND event_type=?',
            (int(owner_id), event_type),
        )).fetchone()
        enabled = not bool(row['enabled'])
        await db.execute(
            'UPDATE alert_rules SET enabled=?,updated_at=? WHERE owner_id=? AND event_type=?',
            (1 if enabled else 0, now_iso(), int(owner_id), event_type),
        )
        await db.commit()
        return enabled
    finally:
        await db.close()


async def alert_rules(owner_id: int):
    await ensure_alert_rules(owner_id)
    db = await connect()
    try:
        return await (await db.execute(
            'SELECT * FROM alert_rules WHERE owner_id=? ORDER BY event_type', (int(owner_id),)
        )).fetchall()
    finally:
        await db.close()


async def recent_alerts(owner_id: int, limit: int = 20):
    db = await connect()
    try:
        return await (await db.execute(
            'SELECT * FROM alerts WHERE owner_id=? ORDER BY id DESC LIMIT ?',
            (int(owner_id), max(1, min(100, int(limit)))),
        )).fetchall()
    finally:
        await db.close()


async def emit_alert(bot, owner_id: int, event_type: str, title: str, details: str = '',
                     *, severity: str = 'warning', dedupe_key: str | None = None,
                     cooldown_minutes: int | None = None) -> bool:
    if event_type not in ALERT_TYPES:
        return False
    await ensure_alert_rules(owner_id)
    db = await connect()
    try:
        rule = await (await db.execute(
            'SELECT * FROM alert_rules WHERE owner_id=? AND event_type=?',
            (int(owner_id), event_type),
        )).fetchone()
        if not rule or not rule['enabled']:
            return False
        cooldown = int(cooldown_minutes if cooldown_minutes is not None else rule['cooldown_minutes'])
        if dedupe_key:
            since = (datetime.now(timezone.utc) - timedelta(minutes=max(0, cooldown))).isoformat()
            duplicate = await (await db.execute(
                '''SELECT 1 FROM alerts WHERE owner_id=? AND event_type=? AND dedupe_key=? AND created_at>=?
                   ORDER BY id DESC LIMIT 1''',
                (int(owner_id), event_type, dedupe_key, since),
            )).fetchone()
            if duplicate:
                return False
        cur = await db.execute(
            '''INSERT INTO alerts(owner_id,event_type,severity,title,details,dedupe_key,delivered,created_at)
               VALUES(?,?,?,?,?,?,0,?)''',
            (int(owner_id), event_type, severity, title[:240], details[:3500] or None,
             (dedupe_key or '')[:240] or None, now_iso()),
        )
        alert_id = int(cur.lastrowid)
        await db.commit()
    finally:
        await db.close()
    icon = '🔴' if severity == 'critical' else ('🟠' if severity == 'warning' else '🔔')
    delivered = False
    try:
        await bot.send_message(int(owner_id), f'{icon} {title}\n\n{details}'[:4000])
        delivered = True
    except Exception:
        delivered = False
    if delivered:
        db = await connect()
        try:
            await db.execute('UPDATE alerts SET delivered=1 WHERE id=?', (alert_id,))
            await db.commit()
        finally:
            await db.close()
    return delivered
