from __future__ import annotations

from ..db import connect, now_iso, upsert_link
from ..link_utils import canonical_section, classify_link, extract_urls, normalize_url
from .jobs import job_signal, set_job


IMPORT_SECTIONS={'important','students','channels','expired','ignored'}


def decode_text_file(data: bytes) -> str:
    """Decode common TXT encodings without ever executing or interpreting data."""
    for encoding in ('utf-8-sig','utf-16','utf-16-le','utf-16-be','cp1256'):
        try:
            text=data.decode(encoding)
            if '\x00' not in text:
                return text
        except (UnicodeDecodeError,LookupError):
            continue
    raise ValueError('unsupported_text_encoding')


async def import_links_text(owner_id: int, section: str, text: str, source: str, job_id: int | None=None) -> dict:
    section=(section or '').strip().lower()
    if section not in IMPORT_SECTIONS:
        return {'error':'invalid_section'}
    raw_urls=extract_urls(text or '')
    report={'section':section,'urls_detected':len(raw_urls),'whatsapp_found':0,'new':0,'duplicates':0,
            'blocked':0,'smart_channels':0,'expired_added':0,'ignored_added':0,
            'removed_from_active':0,'wrong_section':0,'cancelled':False,'paused':False}
    seen=set()
    for index,raw in enumerate(raw_urls,1):
        sig=await job_signal(job_id)
        if sig:
            report['paused']=sig=='pause_requested'; report['cancelled']=sig=='cancel_requested'; break
        normalized=normalize_url(raw); category=classify_link(raw)
        if not normalized or not category.startswith('whatsapp_'):
            continue
        if normalized in seen:
            report['duplicates']+=1; continue
        seen.add(normalized); report['whatsapp_found']+=1

        if section in {'expired','ignored'}:
            table='expired_registry' if section=='expired' else 'ignored_registry'
            reason='txt_import_expired' if section=='expired' else 'txt_import_ignored'
            db=await connect()
            try:
                cur=await db.execute(f'INSERT OR IGNORE INTO {table}(normalized_url,reason,source,created_at) VALUES(?,?,?,?)',
                                     (normalized,reason,source,now_iso()))
                added=max(0,int(cur.rowcount or 0))
                if section=='expired': report['expired_added']+=added
                else: report['ignored_added']+=added
                if not added: report['duplicates']+=1
                active=await (await db.execute('SELECT id FROM links WHERE normalized_url=?',(normalized,))).fetchone()
                if active:
                    await db.execute('DELETE FROM join_queue WHERE link_id=?',(active['id'],))
                    await db.execute('DELETE FROM link_sections WHERE link_id=?',(active['id'],))
                    await db.execute('DELETE FROM links WHERE id=?',(active['id'],))
                    report['removed_from_active']+=1
                await db.commit()
            finally:
                await db.close()
        else:
            if section=='channels' and category!='whatsapp_channel':
                report['wrong_section']+=1; continue
            effective=canonical_section(section,category)
            if effective=='channels' and section!='channels':
                report['smart_channels']+=1
            is_new,lid=await upsert_link(raw,normalized,category,owner_id,source,section=effective)
            if lid is None: report['blocked']+=1
            elif is_new: report['new']+=1
            else: report['duplicates']+=1
        if job_id and index % 500==0:
            await set_job(job_id,'running',report)
    return report
