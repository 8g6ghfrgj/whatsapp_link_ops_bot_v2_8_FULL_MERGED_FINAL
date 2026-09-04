from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def txt(rel): return (ROOT/rel).read_text(encoding='utf-8')

bot=txt('app/bot.py')
db=txt('app/db.py')
kb=txt('app/keyboards.py')
src=txt('app/services/telegram_sources.py')
hist=txt('app/services/telegram_history.py')
join=txt('app/services/join_worker.py')
exp=txt('app/services/exporter.py')
cfg=txt('app/config.py')
env=txt('.env.example')
ctl=txt('botctl.sh')
login=txt('telegram_session_login.py')

checks={
 'principal supervisor role': "role=='principal'" in bot and "add_supervisor(uid,'principal')" in bot and 'PRINCIPAL_IDS' in bot,
 'owner-only supervisor management': "if not real_owner(c.from_user.id)" in bot and "إدارة المشرفين للمالك فقط" in bot,
 'principal shared owner workspace': 'def scope_uid(uid): return settings.owner_id if owner(uid) else uid' in bot,
 'principal cached fast path': 'uid==settings.owner_id or uid in PRINCIPAL_IDS' in bot,
 'regular supervisor limited menu': 'سجل الروابط المنتهية' in kb and 'سجل الروابط المهمشة' in kb,
 'five semantic sections': all(x in src for x in ["'important'","'students'","'expired'","'ignored'","'channels'"]),
 'multi-section global dedupe': 'CREATE TABLE IF NOT EXISTS link_sections' in db and 'PRIMARY KEY(link_id,section)' in db and 'INSERT OR IGNORE INTO link_sections' in db,
 'telegram source section schema': 'section TEXT NOT NULL DEFAULT \'important\'' in db and 'telegram_sources' in db,
 'channel and group live ingestion': '@dp.channel_post()' in bot and "F.chat.type.in_({'group','supergroup'})" in bot,
 'registered channel middleware': "{'group','supergroup','channel'}" in bot and '_registered_source_chat' in bot,
 'source section picker': all(x in bot for x in ['tgsrc_section:important','tgsrc_section:students','tgsrc_section:expired','tgsrc_section:ignored','tgsrc_section:channels']),
 'expired ignored source exclusion': 'expired_registry' in src and 'ignored_registry' in src and "DELETE FROM join_queue" in src,
 'telethon optional history': 'telegram_api_id' in cfg and 'telegram_api_hash' in cfg and 'TelegramClient' in hist and 'telegram_session_login.py' in bot,
 'telethon local login helper': 'await client.start()' in login and 'TELEGRAM_API_ID' in env and 'TELEGRAM_API_HASH' in env,
 'telethon quick UI status': 'async def history_status(verify:bool=False)' in hist and 'if not verify:' in hist,
 'history background job': "create_job(op,'telegram_history'" in bot and 'import_source_history' in bot,
 'durable telegram history cursor': 'history_cursor_id INTEGER NOT NULL DEFAULT 0' in db and 'min_id=cursor' in hist and 'processed % 250' in hist,
 'join section account flow': 'join_section:' in bot and 'join_account:' in bot and 'join_limit_choice:' in bot,
 'join only actual whatsapp groups': "l.category='whatsapp_group'" in join and 'link_sections' in join,
 'blocked sections not joinable': "JOINABLE_SECTIONS={'important','students','channels'}" in src and 'join_blocked:' in bot,
 'semantic exports': 'link_sections' in exp and 'section_' in exp,
 'v25 lineage / current instance name': any(v in cfg for v in ('V2.5 FINAL','V2.6 FINAL','V2.8 FINAL')) and any(v in env for v in ('V2.5 FINAL','V2.6 FINAL','V2.8 FINAL')),
 'v25 selfcheck hook': 'test_v2_5_static.py' in ctl,
}

bad=[name for name,ok in checks.items() if not ok]
for name,ok in checks.items(): print(('OK   ' if ok else 'FAIL ')+name)
if bad: raise SystemExit('V2.5 static failures: '+', '.join(bad))
print('V2.5 STATIC CHECK PASSED')
