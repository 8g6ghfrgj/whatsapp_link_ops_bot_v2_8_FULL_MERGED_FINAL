from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def t(p): return (ROOT/p).read_text(encoding='utf-8')
def ck(name,cond):
    if not cond: raise AssertionError(name)
    print('OK  ',name)

def main():
    bot=t('app/bot.py'); db=t('app/db.py'); cfg=t('app/config.py'); ctl=t('botctl.sh')
    tasks=t('app/services/task_center.py'); tools=t('app/services/system_tools.py'); admin=t('app/services/admin_tools.py')
    pkg=t('provider/package.json'); env=t('.env.example')
    ck('v26 lineage', 'V2.8 FINAL' in cfg and 'V2.8 FINAL' in env and '"version": "2.6.0"' in pkg)
    ck('supported runtime pin', '(3,12),(3,13),(3,14)' in ctl and 'Node 22 LTS' in ctl and 'GIT OK' in ctl)
    ck('scheduled task schema', 'CREATE TABLE IF NOT EXISTS scheduled_tasks' in db and 'recurrence_minutes' in db)
    ck('scheduler worker', 'scheduler_forever' in tasks and 'create_scheduled_task' in tasks and 'scheduler_task=asyncio.create_task' in bot)
    ck('safe scheduled actions', "{'reminder','backup','diagnostic','health_check','message_archive'}" in tasks)
    ck('account health', 'check_account_health' in tasks and "F.data=='account_health_all'" in bot)
    ck('error center', 'CREATE TABLE IF NOT EXISTS system_errors' in db and "F.data=='error_center'" in bot and 'log_system_error' in admin)
    ck('admin audit', 'CREATE TABLE IF NOT EXISTS admin_events' in db and "F.data=='admin_audit'" in bot)
    ck('chat followup metadata', 'CREATE TABLE IF NOT EXISTS chat_metadata' in db and "F.data=='inbox'" in bot and 'inbox_follow:' in bot)
    ck('entity tags', 'CREATE TABLE IF NOT EXISTS entity_tags' in db and 'inbox_tag:' in bot)
    ck('global search', 'البحث الشامل V2.6' in bot and 'telegram_sources' in bot and 'wa_messages' in bot)
    ck('safe backup excludes sessions', 'create_safe_backup_zip' in tools and 'Session credentials and .env are deliberately excluded' in tools)
    ck('local full backup not auto sent', 'create_local_full_backup' in tools and 'لم يرسلها البوت عبر Telegram' in bot)
    ck('diagnostics omit secrets', 'Secrets: intentionally omitted' in tools and "F.data=='diagnostics'" in bot)
    ck('db health and restore', 'database_health' in tools and 'restore_db_file' in tools and 'restore-db' in ctl)
    ck('v26 selfcheck hook', 'test_v2_6_static.py' in ctl)
    print('V2.6 STATIC CHECK PASSED')

if __name__=='__main__': main()
