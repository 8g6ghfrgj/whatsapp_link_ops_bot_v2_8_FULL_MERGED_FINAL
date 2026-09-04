from __future__ import annotations
import os, json
from datetime import datetime, timezone
import aiosqlite
from .config import settings
from .link_utils import canonical_section

SCHEMA='''
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
CREATE TABLE IF NOT EXISTS supervisors(user_id INTEGER PRIMARY KEY,role TEXT NOT NULL DEFAULT 'full',enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supervisor_permissions(
 user_id INTEGER NOT NULL,permission TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,permission));
CREATE TABLE IF NOT EXISTS account_slots(
 id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,label TEXT NOT NULL,provider_account_id TEXT,
 phone_hint TEXT,enabled INTEGER NOT NULL DEFAULT 1,health TEXT NOT NULL DEFAULT 'not_linked',last_error TEXT,
 last_seen_at TEXT,created_at TEXT NOT NULL,UNIQUE(operator_id,label),UNIQUE(provider_account_id));
CREATE INDEX IF NOT EXISTS idx_accounts_operator ON account_slots(operator_id,enabled,id);
CREATE TABLE IF NOT EXISTS links(
 id INTEGER PRIMARY KEY AUTOINCREMENT,normalized_url TEXT NOT NULL UNIQUE,original_url TEXT NOT NULL,category TEXT NOT NULL,
 display_name TEXT,section TEXT NOT NULL DEFAULT 'important',first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,seen_count INTEGER NOT NULL DEFAULT 1,
 first_operator_id INTEGER,source TEXT);
CREATE INDEX IF NOT EXISTS idx_links_category_id ON links(category,id);
CREATE TABLE IF NOT EXISTS link_sections(link_id INTEGER NOT NULL,section TEXT NOT NULL,first_seen_at TEXT NOT NULL,PRIMARY KEY(link_id,section));
CREATE INDEX IF NOT EXISTS idx_link_sections_section_link ON link_sections(section,link_id);
CREATE TABLE IF NOT EXISTS occurrences(id INTEGER PRIMARY KEY AUTOINCREMENT,link_id INTEGER NOT NULL,operator_id INTEGER,source TEXT,seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS expired_registry(normalized_url TEXT PRIMARY KEY,reason TEXT,source TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ignored_registry(normalized_url TEXT PRIMARY KEY,reason TEXT,source TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wa_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,account_slot_id INTEGER NOT NULL,provider_boot_id TEXT,provider_event_id INTEGER,
 remote_jid TEXT,message_id TEXT,participant TEXT,message_ts INTEGER,text TEXT NOT NULL DEFAULT '',history INTEGER NOT NULL DEFAULT 0,
 inserted_at TEXT NOT NULL,UNIQUE(account_slot_id,message_id,remote_jid));
CREATE INDEX IF NOT EXISTS idx_wa_messages_account_id ON wa_messages(account_slot_id,id);
CREATE INDEX IF NOT EXISTS idx_wa_messages_jid_id ON wa_messages(remote_jid,id);
CREATE TABLE IF NOT EXISTS wa_messages_archive(
 id INTEGER PRIMARY KEY,account_slot_id INTEGER NOT NULL,remote_jid TEXT,message_ts INTEGER,
 payload_zlib BLOB NOT NULL,original_bytes INTEGER NOT NULL DEFAULT 0,compressed_bytes INTEGER NOT NULL DEFAULT 0,
 archived_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_wa_messages_archive_account ON wa_messages_archive(account_slot_id,id);
CREATE TABLE IF NOT EXISTS message_archive_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,status TEXT NOT NULL,moved_rows INTEGER NOT NULL DEFAULT 0,
 original_bytes INTEGER NOT NULL DEFAULT 0,compressed_bytes INTEGER NOT NULL DEFAULT 0,cutoff_at TEXT,
 details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS wa_sync_cursors(account_slot_id INTEGER PRIMARY KEY,provider_boot_id TEXT,last_event_id INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_cursors(operator_id INTEGER NOT NULL,account_slot_id INTEGER NOT NULL,category TEXT NOT NULL,source_jid TEXT NOT NULL DEFAULT '',last_message_row_id INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(operator_id,account_slot_id,category,source_jid));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,report_json TEXT,hidden INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,name TEXT NOT NULL,mode TEXT NOT NULL DEFAULT 'web',status TEXT NOT NULL DEFAULT 'queued',high_water_link_id INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS audit_results(id INTEGER PRIMARY KEY AUTOINCREMENT,audit_id INTEGER NOT NULL,link_id INTEGER,normalized_url TEXT NOT NULL,status TEXT NOT NULL,display_name TEXT,details TEXT,checked_at TEXT NOT NULL,UNIQUE(audit_id,normalized_url));
CREATE INDEX IF NOT EXISTS idx_audit_results_audit_status ON audit_results(audit_id,status);
CREATE TABLE IF NOT EXISTS audit_inputs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,audit_id INTEGER NOT NULL,link_id INTEGER,normalized_url TEXT NOT NULL,ordinal INTEGER NOT NULL,
 UNIQUE(audit_id,normalized_url));
CREATE INDEX IF NOT EXISTS idx_audit_inputs_audit_ordinal ON audit_inputs(audit_id,ordinal);

CREATE TABLE IF NOT EXISTS join_queue(
 id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,link_id INTEGER NOT NULL,account_slot_id INTEGER,
 status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,requested_at TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(operator_id,link_id));
CREATE INDEX IF NOT EXISTS idx_join_queue_operator_status ON join_queue(operator_id,status,id);
CREATE TABLE IF NOT EXISTS join_attempts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,link_id INTEGER NOT NULL,account_slot_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',group_jid TEXT,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,requested_at TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(operator_id,link_id,account_slot_id));
CREATE INDEX IF NOT EXISTS idx_join_attempts_op_account_status ON join_attempts(operator_id,account_slot_id,status,id);
CREATE TABLE IF NOT EXISTS whatsapp_group_identities(
 link_id INTEGER PRIMARY KEY,group_jid TEXT NOT NULL,subject TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_group_identities_jid ON whatsapp_group_identities(group_jid,link_id);
CREATE TABLE IF NOT EXISTS join_safety_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,account_slot_id INTEGER NOT NULL,link_id INTEGER,
 profile TEXT NOT NULL,status TEXT NOT NULL,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_join_safety_account_time ON join_safety_events(account_slot_id,created_at,id);
CREATE TABLE IF NOT EXISTS join_safety_state(
 account_slot_id INTEGER PRIMARY KEY,cooldown_until TEXT,reason TEXT,consecutive_failures INTEGER NOT NULL DEFAULT 0,
 last_attempt_at TEXT,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches(id INTEGER PRIMARY KEY AUTOINCREMENT,operator_id INTEGER NOT NULL,account_slot_id INTEGER,remote_jid TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'whatsapp',enabled INTEGER NOT NULL DEFAULT 1,last_message_row_id INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(operator_id,account_slot_id,remote_jid,category));

CREATE TABLE IF NOT EXISTS telegram_sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,label TEXT NOT NULL,session_path TEXT NOT NULL,
 phone_hint TEXT,telegram_user_id INTEGER,username TEXT,enabled INTEGER NOT NULL DEFAULT 1,
 health TEXT NOT NULL DEFAULT 'pending',last_error TEXT,last_seen_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(owner_id,label),UNIQUE(session_path));
CREATE INDEX IF NOT EXISTS idx_telegram_sessions_owner ON telegram_sessions(owner_id,enabled,id);

CREATE TABLE IF NOT EXISTS telegram_sources(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,title TEXT,username TEXT,section TEXT NOT NULL DEFAULT 'important',telegram_session_id INTEGER,
 enabled INTEGER NOT NULL DEFAULT 1,auto_join_queue INTEGER NOT NULL DEFAULT 1,collected_links INTEGER NOT NULL DEFAULT 0,
 history_cursor_id INTEGER NOT NULL DEFAULT 0,history_complete INTEGER NOT NULL DEFAULT 0,last_sync_at TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_id,chat_id));
CREATE INDEX IF NOT EXISTS idx_telegram_sources_chat ON telegram_sources(chat_id,enabled);

CREATE TABLE IF NOT EXISTS message_templates(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,UNIQUE(owner_id,name));
CREATE TABLE IF NOT EXISTS broadcast_campaigns(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,account_slot_id INTEGER NOT NULL,target_type TEXT NOT NULL,
 template_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',messages_per_target INTEGER NOT NULL DEFAULT 1,
 target_limit_per_run INTEGER NOT NULL DEFAULT 0,target_delay_seconds INTEGER NOT NULL DEFAULT 30,batch_size INTEGER NOT NULL DEFAULT 10,
 batch_rest_seconds INTEGER NOT NULL DEFAULT 3600,total_targets INTEGER NOT NULL DEFAULT 0,sent_targets INTEGER NOT NULL DEFAULT 0,
 failed_targets INTEGER NOT NULL DEFAULT 0,repeat_total INTEGER NOT NULL DEFAULT 1,repeat_completed INTEGER NOT NULL DEFAULT 0,repeat_interval_seconds INTEGER NOT NULL DEFAULT 3600,current_cycle INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broadcast_targets(
 id INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id INTEGER NOT NULL,target_jid TEXT NOT NULL,display_name TEXT,target_type TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',messages_sent INTEGER NOT NULL DEFAULT 0,last_error TEXT,updated_at TEXT NOT NULL,
 UNIQUE(campaign_id,target_jid));
CREATE INDEX IF NOT EXISTS idx_broadcast_targets_campaign_status ON broadcast_targets(campaign_id,status,id);
CREATE TABLE IF NOT EXISTS send_suppression(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,target_jid TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL,UNIQUE(owner_id,target_jid));

-- V2.6 administration and workflow tables. These are additive and do not alter
-- the meaning of any V2.5/V2.5.1 data.
CREATE TABLE IF NOT EXISTS scheduled_tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,action TEXT NOT NULL,title TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'scheduled',priority TEXT NOT NULL DEFAULT 'normal',
 run_at TEXT NOT NULL,recurrence_minutes INTEGER NOT NULL DEFAULT 0,last_run_at TEXT,next_run_at TEXT,
 run_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(status,run_at,id);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_owner ON scheduled_tasks(owner_id,status,id);

CREATE TABLE IF NOT EXISTS admin_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id INTEGER NOT NULL,event_type TEXT NOT NULL,entity_type TEXT,entity_id TEXT,
 details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_admin_events_created ON admin_events(id DESC);

CREATE TABLE IF NOT EXISTS system_errors(
 id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,severity TEXT NOT NULL DEFAULT 'error',message TEXT NOT NULL,
 details TEXT,created_at TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_system_errors_open ON system_errors(resolved,id DESC);
CREATE TABLE IF NOT EXISTS alert_rules(
 owner_id INTEGER NOT NULL,event_type TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
 cooldown_minutes INTEGER NOT NULL DEFAULT 60,updated_at TEXT NOT NULL,PRIMARY KEY(owner_id,event_type));
CREATE TABLE IF NOT EXISTS alerts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,event_type TEXT NOT NULL,severity TEXT NOT NULL DEFAULT 'warning',
 title TEXT NOT NULL,details TEXT,dedupe_key TEXT,delivered INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_alerts_owner_created ON alerts(owner_id,id DESC);

CREATE TABLE IF NOT EXISTS chat_metadata(
 owner_id INTEGER NOT NULL,account_slot_id INTEGER NOT NULL,remote_jid TEXT NOT NULL,display_name TEXT,note TEXT,
 status TEXT NOT NULL DEFAULT 'new',follow_up_at TEXT,updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,account_slot_id,remote_jid));
CREATE INDEX IF NOT EXISTS idx_chat_metadata_status ON chat_metadata(owner_id,status,updated_at);

CREATE TABLE IF NOT EXISTS entity_tags(
 owner_id INTEGER NOT NULL,entity_type TEXT NOT NULL,entity_key TEXT NOT NULL,tag TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,entity_type,entity_key,tag));
CREATE INDEX IF NOT EXISTS idx_entity_tags_tag ON entity_tags(owner_id,tag,entity_type);
'''

def now_iso(): return datetime.now(timezone.utc).isoformat()
async def connect():
    os.makedirs(os.path.dirname(settings.db_path) or '.',exist_ok=True)
    db=await aiosqlite.connect(settings.db_path); db.row_factory=aiosqlite.Row
    await db.execute('PRAGMA busy_timeout=10000'); return db
async def init_db():
    db=await connect()
    try:
        await db.executescript(SCHEMA)
        # Forward-compatible migrations for V2.3 databases upgraded from V2.2.
        job_cols={r['name'] for r in await (await db.execute("PRAGMA table_info(jobs)")).fetchall()}
        if 'hidden' not in job_cols:
            await db.execute('ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
        for name,ddl in [
            ('priority',"TEXT NOT NULL DEFAULT 'normal'"),
            ('progress_current','INTEGER NOT NULL DEFAULT 0'),
            ('progress_total','INTEGER NOT NULL DEFAULT 0'),
            ('last_error','TEXT'),
            ('retry_count','INTEGER NOT NULL DEFAULT 0'),
        ]:
            if name not in job_cols:
                await db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {ddl}')
        link_cols={r['name'] for r in await (await db.execute("PRAGMA table_info(links)")).fetchall()}
        if 'section' not in link_cols:
            await db.execute("ALTER TABLE links ADD COLUMN section TEXT NOT NULL DEFAULT 'important'")
        src_cols={r['name'] for r in await (await db.execute("PRAGMA table_info(telegram_sources)")).fetchall()}
        # V2.4 used a different telegram_sources schema.  Rebuild it once, while
        # keeping the legacy table as a local safety copy and preserving source
        # sections/cursors collected by V2.4.
        if 'owner_id' not in src_cols and 'source_ref' in src_cols:
            await db.execute('DROP INDEX IF EXISTS idx_telegram_sources_chat')
            await db.execute('DROP INDEX IF EXISTS idx_tg_sources_section')
            await db.execute('ALTER TABLE telegram_sources RENAME TO telegram_sources_v24_legacy')
            await db.execute("""CREATE TABLE telegram_sources(
              id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,title TEXT,username TEXT,section TEXT NOT NULL DEFAULT 'important',telegram_session_id INTEGER,
              enabled INTEGER NOT NULL DEFAULT 1,auto_join_queue INTEGER NOT NULL DEFAULT 1,collected_links INTEGER NOT NULL DEFAULT 0,
              history_cursor_id INTEGER NOT NULL DEFAULT 0,history_complete INTEGER NOT NULL DEFAULT 0,last_sync_at TEXT,
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(owner_id,chat_id))""")
            await db.execute('CREATE INDEX IF NOT EXISTS idx_telegram_sources_chat ON telegram_sources(chat_id,enabled)')
            legacy_rows=await (await db.execute('SELECT * FROM telegram_sources_v24_legacy ORDER BY id')).fetchall()
            has_section_links=bool(await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_section_links'")).fetchone())
            for r in legacy_rows:
                ref=(r['source_ref'] or '').strip() if 'source_ref' in r.keys() else ''
                username=None
                if ref.startswith('@'):
                    username=ref[1:]
                elif 't.me/' in ref and '/+' not in ref and '/joinchat/' not in ref:
                    username=ref.split('t.me/',1)[1].split('/',1)[0].strip() or None
                collected=0
                if has_section_links:
                    collected=int((await (await db.execute('SELECT COUNT(*) c FROM telegram_section_links WHERE source_id=?',(r['id'],))).fetchone())['c'] or 0)
                legacy_owner=int(settings.owner_id)
                await db.execute("""INSERT INTO telegram_sources(id,owner_id,chat_id,title,username,section,enabled,auto_join_queue,collected_links,history_cursor_id,history_complete,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,1,?,?,0,?,?)
                  ON CONFLICT(owner_id,chat_id) DO UPDATE SET title=excluded.title,username=COALESCE(excluded.username,telegram_sources.username),section=excluded.section,enabled=excluded.enabled,collected_links=MAX(telegram_sources.collected_links,excluded.collected_links),history_cursor_id=MAX(telegram_sources.history_cursor_id,excluded.history_cursor_id),updated_at=excluded.updated_at""",
                  (r['id'],legacy_owner,int(r['chat_id']),r['title'],username,r['section'] or 'important',int(r['enabled'] or 0),collected,int(r['last_message_id'] or 0),r['created_at'],r['updated_at']))
            if has_section_links:
                await db.execute("""INSERT OR IGNORE INTO link_sections(link_id,section,first_seen_at)
                  SELECT tsl.link_id,tsl.section,COALESCE(tsl.first_seen_at,l.first_seen_at)
                  FROM telegram_section_links tsl JOIN links l ON l.id=tsl.link_id
                  WHERE tsl.link_id IS NOT NULL AND tsl.section NOT IN ('expired','ignored')""")
            src_cols={r['name'] for r in await (await db.execute("PRAGMA table_info(telegram_sources)")).fetchall()}
        # Normal additive migrations for V2.5-era databases.
        if 'section' not in src_cols:
            await db.execute("ALTER TABLE telegram_sources ADD COLUMN section TEXT NOT NULL DEFAULT 'important'")
        for name,ddl in [
            ('username','TEXT'),
            ('telegram_session_id','INTEGER'),
            ('auto_join_queue','INTEGER NOT NULL DEFAULT 1'),
            ('collected_links','INTEGER NOT NULL DEFAULT 0'),
            ('history_cursor_id','INTEGER NOT NULL DEFAULT 0'),
            ('history_complete','INTEGER NOT NULL DEFAULT 0'),
            ('last_sync_at','TEXT'),
        ]:
            if name not in src_cols:
                await db.execute(f'ALTER TABLE telegram_sources ADD COLUMN {name} {ddl}')
        await db.execute("INSERT OR IGNORE INTO link_sections(link_id,section,first_seen_at) SELECT id,COALESCE(section,'important'),first_seen_at FROM links")
        # Smart canonical routing: a WhatsApp channel is stored once globally
        # and belongs only to the channels section, even if it was originally
        # discovered in an important/students source.
        await db.execute("UPDATE links SET section='channels' WHERE category='whatsapp_channel'")
        await db.execute("DELETE FROM link_sections WHERE link_id IN (SELECT id FROM links WHERE category='whatsapp_channel') AND section<>'channels'")
        await db.execute("INSERT OR IGNORE INTO link_sections(link_id,section,first_seen_at) SELECT id,'channels',first_seen_at FROM links WHERE category='whatsapp_channel'")
        await db.execute("DELETE FROM join_queue WHERE link_id IN (SELECT id FROM links WHERE category='whatsapp_channel')")
        # V2.7 normalization drops tracking queries from channel invitations.
        # Merge pre-existing query variants before changing the UNIQUE key.
        channel_rows=await (await db.execute("SELECT * FROM links WHERE category='whatsapp_channel' ORDER BY id")).fetchall()
        keepers={}
        for r in channel_rows:
            canonical=(r['normalized_url'] or '').split('?',1)[0]
            keeper=keepers.get(canonical)
            if keeper is None:
                collision=await (await db.execute('SELECT id FROM links WHERE normalized_url=?',(canonical,))).fetchone()
                if collision and int(collision['id'])!=int(r['id']):
                    keeper=int(collision['id'])
                else:
                    keeper=int(r['id'])
                    if canonical and canonical!=r['normalized_url']:
                        await db.execute('UPDATE links SET normalized_url=?,original_url=? WHERE id=?',(canonical,canonical,keeper))
                keepers[canonical]=keeper
            if int(r['id'])!=keeper:
                await db.execute('UPDATE occurrences SET link_id=? WHERE link_id=?',(keeper,r['id']))
                await db.execute('UPDATE audit_inputs SET link_id=? WHERE link_id=?',(keeper,r['id']))
                await db.execute('UPDATE audit_results SET link_id=? WHERE link_id=?',(keeper,r['id']))
                await db.execute('UPDATE links SET seen_count=seen_count+?,last_seen_at=MAX(last_seen_at,?),first_seen_at=MIN(first_seen_at,?) WHERE id=?',
                                 (int(r['seen_count'] or 0),r['last_seen_at'],r['first_seen_at'],keeper))
                await db.execute('DELETE FROM link_sections WHERE link_id=?',(r['id'],))
                await db.execute('DELETE FROM links WHERE id=?',(r['id'],))
        cols={r['name'] for r in await (await db.execute("PRAGMA table_info(broadcast_campaigns)")).fetchall()}
        for name,ddl in [
            ('repeat_total','INTEGER NOT NULL DEFAULT 1'),
            ('repeat_completed','INTEGER NOT NULL DEFAULT 0'),
            ('repeat_interval_seconds','INTEGER NOT NULL DEFAULT 3600'),
            ('current_cycle','INTEGER NOT NULL DEFAULT 1'),
        ]:
            if name not in cols:
                await db.execute(f'ALTER TABLE broadcast_campaigns ADD COLUMN {name} {ddl}')
        await db.commit()
    finally: await db.close()
async def is_supervisor(uid:int)->bool:
    if uid==settings.owner_id:return True
    db=await connect()
    try:
        r=await (await db.execute('SELECT enabled FROM supervisors WHERE user_id=?',(uid,))).fetchone(); return bool(r and r['enabled'])
    finally: await db.close()
async def supervisor_role(uid:int)->str|None:
    if uid==settings.owner_id:return 'owner'
    db=await connect()
    try:
        r=await (await db.execute('SELECT role,enabled FROM supervisors WHERE user_id=?',(uid,))).fetchone()
        return (r['role'] if r and r['enabled'] else None)
    finally: await db.close()

async def add_supervisor(uid:int,role='full'):
    db=await connect()
    try:
        await db.execute("INSERT INTO supervisors(user_id,role,enabled,created_at) VALUES(?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,enabled=1",(uid,role,now_iso()))
        if role=='registry': await db.execute('DELETE FROM supervisor_permissions WHERE user_id=?',(uid,))
        await db.commit()
    finally: await db.close()
async def remove_supervisor(uid:int):
    db=await connect()
    try:
        await db.execute('UPDATE supervisors SET enabled=0 WHERE user_id=?',(uid,)); await db.execute('UPDATE account_slots SET enabled=0 WHERE operator_id=?',(uid,)); await db.commit()
    finally: await db.close()
async def add_account_slot(operator_id:int,label:str):
    db=await connect()
    try:
        cur=await db.execute('INSERT INTO account_slots(operator_id,label,created_at) VALUES(?,?,?)',(operator_id,label.strip(),now_iso())); aid=cur.lastrowid
        pid=f'{operator_id}_{aid}'
        await db.execute('UPDATE account_slots SET provider_account_id=? WHERE id=?',(pid,aid)); await db.commit(); return int(aid),pid
    finally: await db.close()
async def set_account_status(slot_id:int,operator_id:int,health:str,last_error=None,phone_hint=None):
    db=await connect()
    try:
        await db.execute('UPDATE account_slots SET health=?,last_error=?,phone_hint=COALESCE(?,phone_hint),last_seen_at=? WHERE id=? AND operator_id=?',(health,last_error,phone_hint,now_iso(),slot_id,operator_id)); await db.commit()
    finally: await db.close()
async def upsert_link(url,normalized,category,operator_id,source,display_name=None,section='important'):
    db=await connect()
    try:
        if await (await db.execute('SELECT 1 FROM expired_registry WHERE normalized_url=?',(normalized,))).fetchone(): return False,None
        if await (await db.execute('SELECT 1 FROM ignored_registry WHERE normalized_url=?',(normalized,))).fetchone(): return False,None
        now=now_iso(); section=canonical_section(section,category)
        row=await (await db.execute('SELECT id FROM links WHERE normalized_url=?',(normalized,))).fetchone()
        if row:
            lid=row['id']; await db.execute("UPDATE links SET last_seen_at=?,seen_count=seen_count+1,display_name=COALESCE(display_name,?),category=?,section=CASE WHEN ?='whatsapp_channel' THEN 'channels' ELSE section END WHERE id=?",(now,display_name,category,category,lid)); is_new=False
        else:
            cur=await db.execute('INSERT INTO links(normalized_url,original_url,category,display_name,section,first_seen_at,last_seen_at,first_operator_id,source) VALUES(?,?,?,?,?,?,?,?,?)',(normalized,url,category,display_name,section,now,now,operator_id,source)); lid=cur.lastrowid; is_new=True
        if category=='whatsapp_channel':
            await db.execute("DELETE FROM link_sections WHERE link_id=? AND section<>'channels'",(lid,))
            await db.execute('DELETE FROM join_queue WHERE link_id=?',(lid,))
        await db.execute('INSERT OR IGNORE INTO link_sections(link_id,section,first_seen_at) VALUES(?,?,?)',(lid,section or 'important',now))
        await db.execute('INSERT INTO occurrences(link_id,operator_id,source,seen_at) VALUES(?,?,?,?)',(lid,operator_id,source,now)); await db.commit(); return is_new,int(lid)
    finally: await db.close()
async def stats():
    db=await connect()
    try:
        total=(await (await db.execute('SELECT COUNT(*) c FROM links')).fetchone())['c']; exp=(await (await db.execute('SELECT COUNT(*) c FROM expired_registry')).fetchone())['c']; ign=(await (await db.execute('SELECT COUNT(*) c FROM ignored_registry')).fetchone())['c']
        rows=await (await db.execute('SELECT category,COUNT(*) c FROM links GROUP BY category')).fetchall()
        srows=await (await db.execute('SELECT section,COUNT(*) c FROM link_sections GROUP BY section')).fetchall()
        return {'total':total,'expired':exp,'ignored':ign,'by_category':{r['category']:r['c'] for r in rows},'by_section':{r['section']:r['c'] for r in srows}}
    finally: await db.close()
async def backup_db():
    os.makedirs(settings.backup_dir,exist_ok=True); stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); dst=os.path.join(settings.backup_dir,f'whatsapp_ops_{stamp}.db')
    src=await connect(); out=await aiosqlite.connect(dst)
    try: await src.backup(out)
    finally: await out.close(); await src.close()
    return dst
async def factory_reset(keep_accounts=True):
    db=await connect()
    try:
        for t in ['link_sections','links','occurrences','expired_registry','ignored_registry','wa_messages','wa_messages_archive','message_archive_runs','wa_sync_cursors','collection_cursors','jobs','audits','audit_results','audit_inputs','join_queue','join_attempts','whatsapp_group_identities','join_safety_events','join_safety_state','watches','telegram_sources','message_templates','broadcast_campaigns','broadcast_targets','send_suppression','scheduled_tasks','chat_metadata','entity_tags','alerts','alert_rules']: await db.execute(f'DELETE FROM {t}')
        if not keep_accounts:
            await db.execute('DELETE FROM account_slots'); await db.execute('DELETE FROM supervisors'); await db.execute('DELETE FROM supervisor_permissions'); await db.execute('DELETE FROM telegram_sessions')
        await db.commit()
    finally: await db.close()
