from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path):
    return (ROOT / path).read_text(encoding='utf-8')


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print('OK  ', name)


def main():
    cfg = text('app/config.py'); db = text('app/db.py'); bot = text('app/bot.py')
    join = text('app/services/join_worker.py'); safety = text('app/services/join_safety.py')
    permissions = text('app/services/permissions.py'); alerts = text('app/services/alerts.py')
    retention = text('app/services/retention.py')
    check('v28 version', 'V2.8 FINAL' in cfg and text('VERSION').strip() == '2.8.0-final')
    check('daily account safety', 'JOIN_SAFE_DAILY_LIMIT' in text('.env.example') and 'join_safety_events' in db and 'safety_status' in join)
    check('safe and balanced profiles', all(x in safety for x in ["'very_safe'", "'balanced'", "'custom'", 'jitter_delay']))
    check('rate limit circuit', 'JOIN_RATE_LIMIT_COOLDOWN_SECONDS' in text('.env.example') and 'retry_later_or_rate_limit' in safety)
    check('single worker per account', '_ACCOUNT_LOCKS' in join and 'async with lock' in join)
    check('group jid dedupe', 'whatsapp_group_identities' in db and '_same_group_done' in join and 'duplicate_group' in join)
    check('pending request not retried', "ja.status IN ('pending','retry_later')" in join)
    check('granular permissions', 'supervisor_permissions' in db and 'PERMISSIONS' in permissions and 'sup_perm_toggle:' in bot)
    check('smart alerts', 'alert_rules' in db and 'account_disconnected' in alerts and 'alerts_center' in bot)
    check('compressed recoverable archive', 'wa_messages_archive' in db and 'zlib.compress' in retention and 'restore_archived_messages' in retention)
    check('archive is task-visible', "'message_archive'" in bot and 'archive_old_messages' in bot)
    check('background monitors receive bot', 'worker_forever(bot)' in bot and 'telegram_sync_forever(bot)' in bot)
    print('V2.8 STATIC CHECK PASSED')


if __name__ == '__main__':
    main()
