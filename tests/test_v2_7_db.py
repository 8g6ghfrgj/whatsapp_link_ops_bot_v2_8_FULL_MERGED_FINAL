import asyncio
import os
import tempfile
from pathlib import Path


TMP=tempfile.TemporaryDirectory(prefix='wa_v27_db_test_')
os.environ['DB_PATH']=str(Path(TMP.name)/'test.db')

from app.db import connect, init_db, now_iso, upsert_link
from app.link_utils import normalize_url
from app.services.file_imports import import_links_text


async def main():
    await init_db()
    db=await connect(); now=now_iso()
    try:
        for url,section in [
            ('https://www.whatsapp.com/channel/ABC?utm_source=one','important'),
            ('https://www.whatsapp.com/channel/ABC?utm_source=two','students'),
        ]:
            cur=await db.execute('''INSERT INTO links(normalized_url,original_url,category,section,first_seen_at,last_seen_at,seen_count)
                VALUES(?,?,?,?,?,?,1)''',(url,url,'whatsapp_channel',section,now,now))
            lid=int(cur.lastrowid)
            await db.execute('INSERT INTO link_sections(link_id,section,first_seen_at) VALUES(?,?,?)',(lid,section,now))
            await db.execute('INSERT INTO occurrences(link_id,source,seen_at) VALUES(?,?,?)',(lid,'test',now))
            await db.execute("INSERT OR IGNORE INTO join_queue(operator_id,link_id,status,created_at,updated_at) VALUES(1,?,'pending',?,?)",(lid,now,now))
        await db.commit()
    finally:
        await db.close()

    await init_db()
    db=await connect()
    try:
        rows=await (await db.execute("SELECT id,normalized_url FROM links WHERE category='whatsapp_channel'")).fetchall()
        assert len(rows)==1 and rows[0]['normalized_url']=='https://www.whatsapp.com/channel/ABC'
        sections=await (await db.execute('SELECT section FROM link_sections WHERE link_id=?',(rows[0]['id'],))).fetchall()
        assert [x['section'] for x in sections]==['channels']
        assert int((await (await db.execute('SELECT COUNT(*) c FROM join_queue')).fetchone())['c'])==0
    finally:
        await db.close()

    new,lid=await upsert_link('https://www.whatsapp.com/channel/ABC?ref=again',normalize_url('https://www.whatsapp.com/channel/ABC?ref=again'),'whatsapp_channel',1,'test',section='students')
    assert not new and lid
    report=await import_links_text(1,'important','https://www.whatsapp.com/channel/XYZ?x=1\nhttps://chat.whatsapp.com/GROUP1','unit-test')
    assert report['smart_channels']==1 and report['new']==2
    expired=await import_links_text(1,'expired','https://chat.whatsapp.com/GROUP1','unit-test')
    assert expired['expired_added']==1 and expired['removed_from_active']==1
    print('V2.7 DB MIGRATION AND IMPORT TEST PASSED')


if __name__=='__main__':
    try: asyncio.run(main())
    finally: TMP.cleanup()
