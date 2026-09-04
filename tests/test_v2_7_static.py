from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def t(path): return (ROOT/path).read_text(encoding='utf-8')
def ck(name,condition):
    if not condition: raise AssertionError(name)
    print('OK  ',name)


def main():
    bot=t('app/bot.py'); db=t('app/db.py'); cfg=t('app/config.py')
    sessions=t('app/services/telegram_sessions.py'); history=t('app/services/telegram_history.py')
    imports=t('app/services/file_imports.py'); links=t('app/link_utils.py'); collector=t('app/services/collector.py')
    join=t('app/services/join_worker.py'); jobs=t('app/services/jobs.py'); exporter=t('app/services/exporter.py')
    ck('v27 lineage','V2.8 FINAL' in cfg and t('VERSION').strip()=='2.8.0-final')
    ck('multi telethon schema','CREATE TABLE IF NOT EXISTS telegram_sessions' in db and 'telegram_session_id INTEGER' in db)
    ck('in-bot telethon login',all(x in bot for x in ['tg_session_add','tg_session_phone','tg_session_code','tg_session_password']))
    ck('sensitive login cleanup','await m.delete()' in bot and 'cancel_telegram_login' in bot)
    ck('session login service',all(x in sessions for x in ['send_code_request','submit_code','submit_password','SessionPasswordNeededError']))
    ck('old and new telegram sync','telegram_sync_forever' in history and 'min_id=cursor' in history and 'telegram_task=asyncio.create_task' in bot)
    ck('session-backed source resolution','resolve_telegram_source' in bot and 'tgsrc_session:' in bot and 'telegram_session_id=excluded.telegram_session_id' in bot)
    ck('txt file UI','txt_import' in bot and 'txt_section:' in bot and 'decode_text_file' in bot)
    ck('txt import service','IMPORT_SECTIONS' in imports and 'expired_added' in imports and 'ignored_added' in imports)
    ck('smart channel canonicalization','canonical_section' in links and "category=='whatsapp_channel'" in links)
    ck('collector writes sections','INSERT OR IGNORE INTO link_sections' in collector and 'smart_channels' in collector)
    ck('migration moves channels',"UPDATE links SET section='channels'" in db and "section<>'channels'" in db)
    ck('detailed join counters',all(x in join for x in ['joined','already_member','requests_sent','retry_later']))
    ck('short actions recorded','record_completed_job' in jobs and all(x in bot for x in ['manual_import','expired_import','ignored_import','file_import']))
    ck('downloadable task report','export_job_report' in exporter and 'job_export:' in bot)
    ck('audit expired counter','expired_added' in bot and 'status_counts' in bot)
    ck('v27 selfcheck hook','test_v2_7_static.py' in t('botctl.sh'))
    print('V2.7 STATIC CHECK PASSED')


if __name__=='__main__': main()
