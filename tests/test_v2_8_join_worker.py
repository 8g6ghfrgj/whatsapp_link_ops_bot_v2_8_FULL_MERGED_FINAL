import asyncio
import os
import tempfile
from pathlib import Path


TMP = tempfile.TemporaryDirectory(prefix='wa_v28_join_test_')
os.environ['DB_PATH'] = str(Path(TMP.name) / 'test.db')
os.environ['OWNER_ID'] = '1'

from app.db import connect, init_db, now_iso
import app.services.join_worker as worker


async def main():
    await init_db(); now = now_iso()
    db = await connect()
    try:
        cur = await db.execute(
            "INSERT INTO account_slots(operator_id,label,provider_account_id,health,created_at) VALUES(1,'A','1_A','connected',?)",
            (now,),
        )
        account_id = int(cur.lastrowid)
        link_ids = []
        for idx in range(2):
            url = f'https://chat.whatsapp.com/ROTATED{idx}'
            cur = await db.execute(
                '''INSERT INTO links(normalized_url,original_url,category,section,first_seen_at,last_seen_at)
                   VALUES(?,?,'whatsapp_group','important',?,?)''', (url, url, now, now)
            )
            link_id = int(cur.lastrowid); link_ids.append(link_id)
            await db.execute(
                'INSERT INTO link_sections(link_id,section,first_seen_at) VALUES(?,?,?)',
                (link_id, 'important', now),
            )
            await db.execute(
                "INSERT INTO join_queue(operator_id,link_id,status,created_at,updated_at) VALUES(1,?,'pending',?,?)",
                (link_id, now, now),
            )
        await db.commit()
    finally:
        await db.close()

    calls = {'join': 0}

    async def groups(_account_id):
        return {'groups': []}

    async def invite_info(_account_id, _url):
        return {'group': {'jid': 'same-group@g.us', 'subject': 'Rotated invite'}}

    async def join(_account_id, _url):
        calls['join'] += 1
        return {'ok': True, 'status': 'joined'}

    async def no_sleep(_seconds, _job_id):
        return None

    worker.provider.groups = groups
    worker.provider.invite_info = invite_info
    worker.provider.join = join
    worker._sleep_interruptible = no_sleep
    report = await worker.process_operator(
        1, 2, account_slot_id=account_id, section='important', safety_profile='very_safe'
    )
    assert report['totals']['processed'] == 2
    assert report['totals']['joined'] == 1
    assert report['totals']['duplicate_group'] == 1
    assert calls['join'] == 1
    db = await connect()
    try:
        events = int((await (await db.execute('SELECT COUNT(*) c FROM join_safety_events')).fetchone())['c'])
        aliases = int((await (await db.execute("SELECT COUNT(*) c FROM whatsapp_group_identities WHERE group_jid='same-group@g.us'" )).fetchone())['c'])
    finally:
        await db.close()
    assert events == 1 and aliases == 2
    print('V2.8 JOIN WORKER ROTATED-LINK DEDUPE TEST PASSED')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        TMP.cleanup()
