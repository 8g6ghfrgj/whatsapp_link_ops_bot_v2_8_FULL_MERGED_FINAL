import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


TMP = tempfile.TemporaryDirectory(prefix='wa_v28_db_test_')
os.environ['DB_PATH'] = str(Path(TMP.name) / 'test.db')
os.environ['OWNER_ID'] = '1'

from app.db import add_supervisor, connect, init_db, now_iso
from app.services.alerts import emit_alert, recent_alerts, toggle_alert_rule
from app.services.join_safety import record_result, safety_status
from app.services.join_worker import _same_group_done, _save_attempt, _save_identity
from app.services.permissions import effective_permissions, set_permission
from app.services.retention import archive_old_messages, archive_status, restore_archived_messages


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, user_id, text):
        self.messages.append((user_id, text))


async def main():
    await init_db()

    await add_supervisor(22, 'registry')
    assert await effective_permissions(22) == {'accounts', 'expired', 'ignored'}
    await set_permission(22, 'join', True)
    permissions = await effective_permissions(22)
    assert {'accounts', 'expired', 'ignored', 'join'} <= permissions

    db = await connect(); now = now_iso()
    try:
        cur = await db.execute(
            "INSERT INTO account_slots(operator_id,label,provider_account_id,health,created_at) VALUES(1,'A','1_A','connected',?)",
            (now,),
        )
        account_id = int(cur.lastrowid)
        links = []
        for idx in range(2):
            url = f'https://chat.whatsapp.com/ALIAS{idx}'
            cur = await db.execute(
                '''INSERT INTO links(normalized_url,original_url,category,section,first_seen_at,last_seen_at)
                   VALUES(?,?,'whatsapp_group','important',?,?)''', (url, url, now, now)
            )
            links.append(int(cur.lastrowid))
        await db.commit()
    finally:
        await db.close()

    await _save_identity(links[0], '12345@g.us', 'Same group')
    await _save_attempt(1, links[0], account_id, 'joined', '12345@g.us')
    await _save_identity(links[1], '12345@g.us', 'Same group new link')
    duplicate = await _same_group_done(1, account_id, links[1], '12345@g.us')
    assert duplicate and int(duplicate['link_id']) == links[0]

    before = await safety_status(account_id, 10)
    assert before['allowed'] and before['used_24h'] == 0
    circuit = await record_result(1, account_id, links[0], 'very_safe', 'retry_later', {'error': 'rate limited'})
    assert circuit['circuit_open']
    after = await safety_status(account_id, 10)
    assert not after['allowed'] and after['reason'] == 'cooldown' and after['used_24h'] == 1

    fake = FakeBot()
    assert await emit_alert(fake, 1, 'account_disconnected', 'Disconnected', 'account A', dedupe_key='A')
    assert len(fake.messages) == 1 and len(await recent_alerts(1)) == 1
    assert not await emit_alert(fake, 1, 'account_disconnected', 'Disconnected', 'account A', dedupe_key='A')
    enabled = await toggle_alert_rule(1, 'account_disconnected')
    assert enabled is False

    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    db = await connect()
    try:
        cur = await db.execute(
            '''INSERT INTO wa_messages(account_slot_id,remote_jid,message_id,message_ts,text,history,inserted_at)
               VALUES(?,?,?,?,?,0,?)''', (account_id, 'group@g.us', 'm1', 1, 'A' * 8000, old)
        )
        message_id = int(cur.lastrowid)
        await db.execute(
            '''INSERT INTO collection_cursors(operator_id,account_slot_id,category,source_jid,last_message_row_id,updated_at)
               VALUES(1,?,'all','',?,?)''', (account_id, message_id, now_iso())
        )
        await db.commit()
    finally:
        await db.close()
    report = await archive_old_messages(1, retention_days=90, batch_limit=100)
    assert report['moved'] == 1 and report['compressed_bytes'] < report['original_bytes']
    status = await archive_status()
    assert status['active'] == 0 and status['archived'] == 1
    restored = await restore_archived_messages(100)
    assert restored['restored'] == 1
    status = await archive_status()
    assert status['active'] == 1 and status['archived'] == 0
    print('V2.8 DB/SAFETY/PERMISSIONS/ALERTS/ARCHIVE TEST PASSED')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        TMP.cleanup()
