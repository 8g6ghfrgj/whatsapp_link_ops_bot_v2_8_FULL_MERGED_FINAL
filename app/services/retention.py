from __future__ import annotations

import json
import zlib
from datetime import datetime, timedelta, timezone

from ..config import settings
from ..db import connect, now_iso


def _packed(row) -> tuple[bytes, int]:
    payload = {key: row[key] for key in row.keys()}
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return zlib.compress(raw, level=9), len(raw)


async def archive_status() -> dict:
    db = await connect()
    try:
        active = int((await (await db.execute('SELECT COUNT(*) c FROM wa_messages')).fetchone())['c'])
        archived = await (await db.execute(
            '''SELECT COUNT(*) c,COALESCE(SUM(original_bytes),0) original_bytes,
                      COALESCE(SUM(compressed_bytes),0) compressed_bytes FROM wa_messages_archive'''
        )).fetchone()
        last = await (await db.execute(
            'SELECT * FROM message_archive_runs ORDER BY id DESC LIMIT 1'
        )).fetchone()
        return {
            'active': active, 'archived': int(archived['c']),
            'original_bytes': int(archived['original_bytes']),
            'compressed_bytes': int(archived['compressed_bytes']),
            'last_run': dict(last) if last else None,
        }
    finally:
        await db.close()


async def archive_old_messages(owner_id: int, *, retention_days: int | None = None,
                               batch_limit: int | None = None) -> dict:
    days = max(7, int(retention_days or settings.message_retention_days))
    limit = max(100, min(20000, int(batch_limit or settings.message_archive_batch)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = now_iso()
    db = await connect()
    try:
        cur = await db.execute(
            '''INSERT INTO message_archive_runs(owner_id,status,cutoff_at,created_at)
               VALUES(?,'running',?,?)''', (int(owner_id), cutoff, created)
        )
        run_id = int(cur.lastrowid)
        await db.commit()

        # A row is eligible only after a global collector cursor and every
        # active-watch cursor for that account have safely passed it. Accounts
        # never globally collected are kept
        # untouched. The compressed archive is recoverable and never purged here.
        rows = await (await db.execute(
            '''WITH base AS (
                 SELECT account_slot_id,MIN(last_message_row_id) safe_id
                   FROM collection_cursors WHERE source_jid='' GROUP BY account_slot_id
               ), watch AS (
                 SELECT account_slot_id,MIN(last_message_row_id) safe_id
                   FROM watches WHERE enabled=1 GROUP BY account_slot_id
               ), safe AS (
                 SELECT b.account_slot_id,MIN(b.safe_id,COALESCE(w.safe_id,b.safe_id)) safe_id
                   FROM base b LEFT JOIN watch w ON w.account_slot_id=b.account_slot_id
               )
               SELECT m.* FROM wa_messages m JOIN safe s ON s.account_slot_id=m.account_slot_id
               WHERE m.id<=s.safe_id AND m.inserted_at<? ORDER BY m.id LIMIT ?''',
            (cutoff, limit),
        )).fetchall()
        moved = original_bytes = compressed_bytes = 0
        for row in rows:
            blob, raw_size = _packed(row)
            cur = await db.execute(
                '''INSERT OR IGNORE INTO wa_messages_archive(
                     id,account_slot_id,remote_jid,message_ts,payload_zlib,original_bytes,compressed_bytes,archived_at)
                   VALUES(?,?,?,?,?,?,?,?)''',
                (int(row['id']), int(row['account_slot_id']), row['remote_jid'], row['message_ts'],
                 blob, raw_size, len(blob), now_iso()),
            )
            if cur.rowcount:
                await db.execute('DELETE FROM wa_messages WHERE id=?', (int(row['id']),))
                moved += 1
                original_bytes += raw_size
                compressed_bytes += len(blob)
        details = {
            'retention_days': days, 'batch_limit': limit,
            'recoverable': True, 'purged': 0,
        }
        await db.execute(
            '''UPDATE message_archive_runs SET status='completed',moved_rows=?,original_bytes=?,
               compressed_bytes=?,details_json=?,completed_at=? WHERE id=?''',
            (moved, original_bytes, compressed_bytes, json.dumps(details, ensure_ascii=False), now_iso(), run_id),
        )
        await db.commit()
    except Exception as exc:
        try:
            await db.execute(
                "UPDATE message_archive_runs SET status='failed',details_json=?,completed_at=? WHERE id=?",
                (json.dumps({'error': str(exc)}, ensure_ascii=False), now_iso(), run_id),
            )
            await db.commit()
        except Exception:
            pass
        raise
    finally:
        await db.close()
    # Reclaim free pages after moving text to compressed blobs. Failure here is
    # non-fatal because the archive transaction is already complete.
    if moved:
        maintenance = await connect()
        try:
            await maintenance.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            await maintenance.execute('VACUUM')
        except Exception:
            pass
        finally:
            await maintenance.close()
    ratio = round((compressed_bytes / original_bytes * 100), 1) if original_bytes else 0.0
    return {
        'run_id': run_id, 'moved': moved, 'retention_days': days,
        'original_bytes': original_bytes, 'compressed_bytes': compressed_bytes,
        'compressed_percent': ratio, 'recoverable': True,
    }


async def restore_archived_messages(limit: int = 5000) -> dict:
    db = await connect()
    restored = 0
    try:
        rows = await (await db.execute(
            'SELECT * FROM wa_messages_archive ORDER BY id DESC LIMIT ?',
            (max(1, min(20000, int(limit))),),
        )).fetchall()
        for row in reversed(rows):
            payload = json.loads(zlib.decompress(row['payload_zlib']).decode('utf-8'))
            cur = await db.execute(
                '''INSERT OR IGNORE INTO wa_messages(
                     id,account_slot_id,provider_boot_id,provider_event_id,remote_jid,message_id,
                     participant,message_ts,text,history,inserted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                tuple(payload.get(key) for key in (
                    'id','account_slot_id','provider_boot_id','provider_event_id','remote_jid','message_id',
                    'participant','message_ts','text','history','inserted_at'
                )),
            )
            if cur.rowcount:
                await db.execute('DELETE FROM wa_messages_archive WHERE id=?', (int(row['id']),))
                restored += 1
        await db.commit()
    finally:
        await db.close()
    return {'restored': restored}
