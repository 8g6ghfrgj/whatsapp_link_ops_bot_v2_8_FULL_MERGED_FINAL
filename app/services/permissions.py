from __future__ import annotations

from ..config import settings
from ..db import connect, now_iso


PERMISSIONS = {
    'accounts': '📱 الحسابات',
    'links': '🔗 قاعدة الروابط والاستيراد',
    'collect': '⚡ تجميع روابط WhatsApp',
    'telegram_sources': '📡 جلسات ومصادر Telegram',
    'audit': '🧪 فحص الروابط',
    'join': '➕ الانضمام إلى المجموعات',
    'messages': '📨 الرسائل والحملات',
    'tasks': '📋 المهام والتقارير',
    'watches': '👁 المراقبة المباشرة',
    'expired': '⛔ الروابط المنتهية',
    'ignored': '🗑 الروابط المهمشة',
    'system': '⚙️ الإدارة والنظام',
}

REGISTRY_DEFAULTS = {'accounts', 'expired', 'ignored'}


async def effective_permissions(user_id: int) -> set[str]:
    user_id = int(user_id)
    if user_id == settings.owner_id:
        return set(PERMISSIONS)
    db = await connect()
    try:
        row = await (await db.execute(
            'SELECT role,enabled FROM supervisors WHERE user_id=?', (user_id,)
        )).fetchone()
        if not row or not row['enabled']:
            return set()
        if row['role'] == 'principal':
            return set(PERMISSIONS)
        explicit = await (await db.execute(
            'SELECT permission,enabled FROM supervisor_permissions WHERE user_id=?', (user_id,)
        )).fetchall()
    finally:
        await db.close()
    if not explicit:
        return set(REGISTRY_DEFAULTS)
    return {r['permission'] for r in explicit if r['permission'] in PERMISSIONS and r['enabled']}


async def has_permission(user_id: int, permission: str) -> bool:
    return permission in await effective_permissions(user_id)


async def has_any_permission(user_id: int, permissions: set[str]) -> bool:
    return bool((await effective_permissions(user_id)) & set(permissions))


async def set_permission(user_id: int, permission: str, enabled: bool) -> None:
    if permission not in PERMISSIONS:
        raise ValueError('unknown permission')
    db = await connect()
    try:
        row = await (await db.execute(
            'SELECT role FROM supervisors WHERE user_id=? AND enabled=1', (int(user_id),)
        )).fetchone()
        if not row:
            raise ValueError('supervisor not found')
        if row['role'] == 'principal':
            raise ValueError('principal permissions are fixed')
        # Materialize the old registry defaults once, then apply the requested
        # toggle. This keeps upgrades backward compatible and makes each future
        # permission explicit and auditable.
        count = int((await (await db.execute(
            'SELECT COUNT(*) c FROM supervisor_permissions WHERE user_id=?', (int(user_id),)
        )).fetchone())['c'])
        if count == 0:
            for code in PERMISSIONS:
                await db.execute(
                    'INSERT INTO supervisor_permissions(user_id,permission,enabled,updated_at) VALUES(?,?,?,?)',
                    (int(user_id), code, 1 if code in REGISTRY_DEFAULTS else 0, now_iso()),
                )
        await db.execute(
            '''INSERT INTO supervisor_permissions(user_id,permission,enabled,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(user_id,permission) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at''',
            (int(user_id), permission, 1 if enabled else 0, now_iso()),
        )
        await db.execute("UPDATE supervisors SET role='custom' WHERE user_id=?", (int(user_id),))
        await db.commit()
    finally:
        await db.close()


def permissions_for_callback(data: str) -> set[str]:
    data = data or ''
    if data in {'home', 'cancel_action', 'noop'}:
        return set()
    routes = (
        (('accounts', 'account_add'), ('acct:', 'acct_'), {'accounts'}),
        (('expired_import',), ('expired',), {'expired'}),
        (('ignored_import',), ('ignored',), {'ignored'}),
        (('links_hub',), (), {'links', 'collect', 'telegram_sources'}),
        (('collect',), ('collect_',), {'collect'}),
        (('tg_sources',), ('tg_', 'tgsrc_', 'tgsource_', 'telegram_'), {'telegram_sources'}),
        (('manual', 'txt_import', 'database', 'search', 'export'), ('txt_', 'manual_', 'db_', 'export_'), {'links'}),
        (('audit_join_hub',), (), {'audit', 'join'}),
        (('audit',), ('audit_',), {'audit'}),
        (('joinq', 'join_add', 'join_recheck', 'join_export'), ('join_',), {'join'}),
        (('messages', 'inbox'), ('msg_', 'template_', 'broadcast_', 'campaign_', 'inbox_', 'inboxmsg:'), {'messages'}),
        (('tasks_hub', 'jobs', 'scheduled_tasks', 'scheduled_add', 'account_health_all', 'accounts_report'), ('job_', 'sched_'), {'tasks'}),
        (('watches',), ('watch_',), {'watches'}),
        (('dashboard',), ('dashboard_',), {'tasks'}),
        (('system_hub', 'backup', 'diagnostics', 'error_center', 'admin_audit', 'db_health', 'local_full_backup', 'reset', 'alerts_center', 'message_archive'), ('error_', 'reset_', 'alert_', 'archive_'), {'system'}),
        (('supervisors', 'sup_add', 'sup_add_principal', 'sup_del'), ('sup_',), {'__owner_only__'}),
    )
    for exact, prefixes, required in routes:
        if data in exact or data.startswith(prefixes):
            return required
    return {'__unmapped__'}


STATE_PERMISSIONS = {
    'account_label': 'accounts',
    'expired': 'expired', 'ignored': 'ignored',
    'manual': 'links', 'txt_import': 'links', 'search': 'links',
    'collect_source': 'collect',
    'telegram_source': 'telegram_sources', 'tg_session_label': 'telegram_sources',
    'tg_session_phone': 'telegram_sources', 'tg_session_code': 'telegram_sources',
    'tg_session_password': 'telegram_sources',
    'audit_paste': 'audit',
    'join_paste': 'join', 'join_limit': 'join', 'join_delay': 'join',
    'join_batch_size': 'join', 'join_batch_rest': 'join',
    'template_name': 'messages', 'template_body': 'messages',
    'broadcast_count': 'messages', 'broadcast_delay': 'messages',
    'broadcast_batch_size': 'messages', 'broadcast_batch_rest': 'messages',
    'broadcast_repeat_count': 'messages', 'broadcast_repeat_interval': 'messages',
    'broadcast_mpt_input': 'messages', 'suppression_add': 'messages',
    'scheduled_reminder': 'tasks',
    'watch_jid': 'watches', 'inbox_note': 'messages', 'inbox_tag': 'messages',
    'add_sup': '__owner_only__', 'add_principal': '__owner_only__', 'del_sup': '__owner_only__',
}


def permission_for_state(state_name: str | None) -> str | None:
    if not state_name:
        return None
    return STATE_PERMISSIONS.get(state_name.rsplit(':', 1)[-1], '__unmapped__')
