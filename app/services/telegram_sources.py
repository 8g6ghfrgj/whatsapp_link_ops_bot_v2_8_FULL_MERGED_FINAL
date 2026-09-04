from __future__ import annotations
from ..db import connect, now_iso, upsert_link
from ..link_utils import extract_urls, normalize_url, classify_link

SECTIONS={
    'important':'⭐ الروابط المهمة',
    'students':'🎓 روابط الطلبة',
    'expired':'⛔ الروابط المنتهية',
    'ignored':'🗑 الروابط المهمشة',
    'channels':'📢 روابط القنوات',
}
JOINABLE_SECTIONS={'important','students','channels'}

async def ingest_source_text(text:str, src, actor_id:int, source_tag:str, display_name:str|None=None):
    """Ingest WhatsApp links according to a Telegram source's semantic section.

    Expired/ignored sources populate their blocking registries and remove any existing
    important-link/join-queue entries. Positive sections use links.section while the
    technical link type remains in links.category.
    """
    section=(src['section'] if 'section' in src.keys() else 'important') or 'important'
    auto_join=bool(src['auto_join_queue']) if 'auto_join_queue' in src.keys() else True
    urls=[]
    for raw in extract_urls(text or ''):
        n=normalize_url(raw)
        if n and classify_link(raw).startswith('whatsapp_'):
            urls.append((raw,n,classify_link(raw)))
    if not urls:
        return {'found':0,'new':0,'duplicates':0,'queued':0,'blocked':0,'smart_channels':0}
    new=dup=queued=blocked=smart_channels=0
    if section in {'expired','ignored'}:
        table='expired_registry' if section=='expired' else 'ignored_registry'
        reason='telegram_source_expired' if section=='expired' else 'telegram_source_ignored'
        db=await connect()
        try:
            for raw,n,cat in urls:
                cur=await db.execute(f'INSERT OR IGNORE INTO {table}(normalized_url,reason,source,created_at) VALUES(?,?,?,?)',(n,reason,source_tag,now_iso()))
                if cur.rowcount and cur.rowcount>0: new+=1
                else: dup+=1
                await db.execute('DELETE FROM join_queue WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(n,))
                await db.execute('DELETE FROM link_sections WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(n,))
                await db.execute('DELETE FROM links WHERE normalized_url=?',(n,))
                blocked+=1
            await db.commit()
        finally: await db.close()
        return {'found':len(urls),'new':new,'duplicates':dup,'queued':0,'blocked':blocked,'smart_channels':0}

    for raw,n,cat in urls:
        if cat=='whatsapp_channel' and section!='channels':
            smart_channels+=1
        is_new,lid=await upsert_link(raw,n,cat,actor_id,source_tag,display_name,section=section)
        if lid is None:
            blocked+=1; continue
        if is_new:new+=1
        else:dup+=1
        if auto_join and section in JOINABLE_SECTIONS and cat=='whatsapp_group':
            db=await connect()
            try:
                cur=await db.execute("INSERT OR IGNORE INTO join_queue(operator_id,link_id,status,created_at,updated_at) VALUES(?,?, 'pending',?,?)",(actor_id,lid,now_iso(),now_iso()))
                queued+=max(0,cur.rowcount or 0); await db.commit()
            finally: await db.close()
    return {'found':len(urls),'new':new,'duplicates':dup,'queued':queued,'blocked':blocked,'smart_channels':smart_channels}
