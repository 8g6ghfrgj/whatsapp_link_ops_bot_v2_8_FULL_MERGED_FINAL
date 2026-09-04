from __future__ import annotations
import asyncio, os, tempfile, json, io
import qrcode
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from .config import settings
from .db import *
from .link_utils import extract_urls, normalize_url, classify_link, is_whatsapp_invite, canonical_section
from .keyboards import main_menu, back
from .services.wa_provider import provider
from .services.collector import collect_from_accounts
from .services.web_auditor import inspect_many
from .services.exporter import export_links_zip, export_audit_zip, export_join_zip, export_job_report
from .services.join_worker import process_operator
from .services.wa_sync import worker_forever
from .services.jobs import create_job, set_job, request_cancel, request_pause, recover_interrupted_jobs, is_cancel_requested, job_signal, hide_job, record_completed_job
from .services.broadcast import create_campaign, run_campaign, campaign_summary
from .services.telegram_sources import SECTIONS as TG_SECTIONS, JOINABLE_SECTIONS, ingest_source_text
from .services.telegram_history import history_status, import_source_history, telegram_sync_forever
from .services.telegram_sessions import list_sessions as list_telegram_sessions, begin_login as begin_telegram_login, submit_code as submit_telegram_code, submit_password as submit_telegram_password, cancel_login as cancel_telegram_login, verify_session as verify_telegram_session, resolve_source as resolve_telegram_source
from .services.file_imports import decode_text_file, import_links_text
from .services.task_center import create_scheduled_task, list_scheduled_tasks, cancel_scheduled_task, check_account_health, scheduler_forever
from .services.admin_tools import log_admin_event, log_system_error, recent_errors, resolve_error, recent_admin_events, upsert_chat_meta, add_tag, list_tags
from .services.system_tools import create_safe_backup_zip, create_local_full_backup, write_diagnostics_file, database_health
from .services.permissions import PERMISSIONS, effective_permissions, has_any_permission, permissions_for_callback, permission_for_state, set_permission
from .services.alerts import ALERT_TYPES, ensure_alert_rules, alert_rules, recent_alerts, toggle_alert_rule, emit_alert
from .services.retention import archive_status, archive_old_messages, restore_archived_messages
from .services.join_safety import PROFILE_LABELS, profile_settings

# telegram_session_login.py remains an offline compatibility fallback; V2.8
# performs the normal Telethon login flow from the private bot conversation.

class S(StatesGroup):
    account_label=State(); manual=State(); collect_source=State(); expired=State(); ignored=State(); audit_paste=State(); join_paste=State(); search=State(); add_sup=State(); add_principal=State(); del_sup=State(); watch_jid=State()
    template_name=State(); template_body=State(); broadcast_count=State(); broadcast_delay=State(); broadcast_batch_size=State(); broadcast_batch_rest=State(); broadcast_repeat_count=State(); broadcast_repeat_interval=State(); broadcast_mpt_input=State(); suppression_add=State(); telegram_source=State(); join_limit=State(); join_delay=State(); join_batch_size=State(); join_batch_rest=State()
    scheduled_reminder=State(); inbox_note=State(); inbox_tag=State(); txt_import=State()
    tg_session_label=State(); tg_session_phone=State(); tg_session_code=State(); tg_session_password=State()

bot=Bot(settings.bot_token); dp=Dispatcher(storage=MemoryStorage())
PRINCIPAL_IDS=set()
CUSTOM_IDS=set()
def real_owner(uid): return uid==settings.owner_id
def owner(uid): return real_owner(uid) or uid in PRINCIPAL_IDS
def scope_uid(uid): return settings.owner_id if owner(uid) else uid
async def allowed(uid): return await is_supervisor(uid)
def menu(uid): return main_menu(real_owner(uid), supervisor=not owner(uid) and uid not in CUSTOM_IDS, principal=(uid in PRINCIPAL_IDS))

async def refresh_principals():
    db=await connect()
    try:
        rows=await (await db.execute("SELECT user_id,role FROM supervisors WHERE role IN ('principal','custom') AND enabled=1")).fetchall()
    finally: await db.close()
    PRINCIPAL_IDS.clear(); PRINCIPAL_IDS.update(int(r['user_id']) for r in rows if r['role']=='principal')
    CUSTOM_IDS.clear(); CUSTOM_IDS.update(int(r['user_id']) for r in rows if r['role']=='custom')

def ik(rows): return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb():
    return ik([[InlineKeyboardButton(text='❌ إلغاء العملية', callback_data='cancel_action')]])

async def _registered_source_chat(chat_id:int)->bool:
    db=await connect()
    try:
        r=await (await db.execute('SELECT 1 FROM telegram_sources WHERE chat_id=? AND enabled=1 LIMIT 1',(int(chat_id),))).fetchone(); return bool(r)
    finally: await db.close()

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid=getattr(getattr(event,'from_user',None),'id',0)
        if isinstance(event,Message) and getattr(getattr(event,'chat',None),'type',None) in {'group','supergroup','channel'} and await _registered_source_chat(event.chat.id):
            return await handler(event,data)
        if uid==settings.owner_id or uid in PRINCIPAL_IDS:
            return await handler(event,data)
        role=await supervisor_role(uid)
        if role=='principal':
            PRINCIPAL_IDS.add(int(uid))
            return await handler(event,data)
        if not role:
            if isinstance(event,CallbackQuery):
                await event.answer('غير مصرح لك باستخدام هذا البوت.',show_alert=True)
            elif isinstance(event,Message):
                await event.answer('غير مصرح لك باستخدام هذا البوت.')
            return
        if role=='custom': CUSTOM_IDS.add(int(uid))
        if isinstance(event,CallbackQuery):
            required=permissions_for_callback(event.data or '')
            if required and not await has_any_permission(uid,required):
                await event.answer('لا تملك صلاحية هذه الوظيفة. يمكن للمالك تعديل صلاحياتك بشكل مستقل.',show_alert=True); return
        elif isinstance(event,Message):
            text=(event.text or '').strip()
            state=data.get('state'); cur=await state.get_state() if state else None
            required=permission_for_state(cur)
            if text.startswith('/start'):
                return await handler(event,data)
            if required and await has_any_permission(uid,{required}):
                return await handler(event,data)
            await event.answer('لا تملك صلاحية هذه العملية. يمكن للمالك منح كل صلاحية أو سحبها بشكل مستقل.'); return
        return await handler(event,data)

dp.callback_query.outer_middleware(AccessMiddleware())
dp.message.outer_middleware(AccessMiddleware())

def _spawn(coro):
    task=asyncio.create_task(coro)
    def _done(t):
        if t.cancelled(): return
        try: exc=t.exception()
        except Exception as e: exc=e
        if exc:
            try: asyncio.create_task(log_system_error('background_task',str(exc),repr(exc)))
            except Exception: pass
    task.add_done_callback(_done)
    return task

CATEGORY_LABELS={
    'whatsapp':'روابط WhatsApp', 'whatsapp_group':'مجموعات WhatsApp',
    'whatsapp_channel':'قنوات WhatsApp', 'whatsapp_contact':'حسابات WhatsApp',
    'telegram':'Telegram', 'x':'X/Twitter', 'all':'كل الروابط'
}
STATUS_LABELS={
    'queued':'بالانتظار', 'running':'يعمل الآن', 'cancel_requested':'جاري الإلغاء', 'pause_requested':'جاري الإيقاف', 'paused':'متوقفة مؤقتًا',
    'cancelled':'ملغاة', 'completed':'مكتملة', 'failed':'فشلت', 'interrupted':'توقفت بسبب إعادة تشغيل البوت', 'partial':'جزئية', 'paused_rate_limit':'متوقفة بسبب تقييد'
}

async def ingest_text(text,op,source):
    urls=extract_urls(text or ''); new=dup=blocked=smart_channels=0
    for u in urls:
        n=normalize_url(u); cat=classify_link(u)
        if not n: continue
        if cat=='whatsapp_channel': smart_channels+=1
        is_new,lid=await upsert_link(u,n,cat,op,source)
        if lid is None: blocked+=1
        elif is_new:new+=1
        else:dup+=1
    return {'found':len(urls),'new':new,'duplicates':dup,'blocked':blocked,'smart_channels':smart_channels}

@dp.message(CommandStart())
async def start(m:Message,state:FSMContext):
    if not await allowed(m.from_user.id): return await m.answer('غير مصرح لك باستخدام هذا البوت.')
    await cancel_telegram_login(scope_uid(m.from_user.id))
    await state.clear()
    await m.answer(settings.instance_name+'\n\nحسابات WhatsApp الحقيقية تُربط عبر QR متعدد الأجهزة. لكل مشرف حساباته الخاصة، والروابط في قاعدة مشتركة دون تكرار عالمي.',reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data=='home')
async def home(c:CallbackQuery,state:FSMContext):
    if not await allowed(c.from_user.id): return
    await cancel_telegram_login(scope_uid(c.from_user.id))
    await state.clear()
    await c.message.edit_text(settings.instance_name,reply_markup=menu(c.from_user.id))

@dp.callback_query(F.data=='cancel_action')
async def cancel_action(c:CallbackQuery,state:FSMContext):
    await cancel_telegram_login(scope_uid(c.from_user.id))
    await state.clear()
    if await allowed(c.from_user.id):
        try:
            await c.message.edit_text('تم إلغاء العملية. لم يتم تنفيذ أي تغيير.',reply_markup=menu(c.from_user.id))
        except Exception:
            await c.message.answer('تم إلغاء العملية. لم يتم تنفيذ أي تغيير.',reply_markup=menu(c.from_user.id))
    await c.answer('تم الإلغاء')

@dp.callback_query(F.data=='links_hub')
async def links_hub(c:CallbackQuery):
    await c.message.edit_text('🔗 الروابط والتجميع',reply_markup=ik([
        [InlineKeyboardButton(text='⚡ تجميع من WhatsApp',callback_data='collect')],
        [InlineKeyboardButton(text='✍️ استقبال روابط يدويًا',callback_data='manual')],
        [InlineKeyboardButton(text='📄 استيراد روابط من ملف TXT',callback_data='txt_import')],
        [InlineKeyboardButton(text='📡 مصادر Telegram للروابط',callback_data='tg_sources')],
        [InlineKeyboardButton(text='🗃 قاعدة الروابط',callback_data='database'),InlineKeyboardButton(text='🔎 البحث',callback_data='search')],
        [InlineKeyboardButton(text='⛔ المنتهية',callback_data='expired_import'),InlineKeyboardButton(text='🗑 المهمشة',callback_data='ignored_import')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]))

@dp.callback_query(F.data=='audit_join_hub')
async def audit_join_hub(c:CallbackQuery):
    await c.message.edit_text('🔍 الفحص والانضمام',reply_markup=ik([
        [InlineKeyboardButton(text='🧪 فحص روابط WhatsApp',callback_data='audit')],
        [InlineKeyboardButton(text='➕ إدارة طابور الانضمام',callback_data='joinq')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]))

@dp.callback_query(F.data=='tasks_hub')
async def tasks_hub(c:CallbackQuery):
    await c.message.edit_text('📋 المهام والتقارير',reply_markup=ik([
        [InlineKeyboardButton(text='📋 مهام التشغيل',callback_data='jobs'),InlineKeyboardButton(text='⏰ المهام المجدولة',callback_data='scheduled_tasks')],
        [InlineKeyboardButton(text='🩺 صحة الحسابات',callback_data='account_health_all'),InlineKeyboardButton(text='📊 تقرير الحسابات',callback_data='accounts_report')],
        [InlineKeyboardButton(text='📨 حملات الإرسال',callback_data='msg_campaigns')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]))

@dp.callback_query(F.data=='system_hub')
async def system_hub(c:CallbackQuery):
    rows=[]
    if real_owner(c.from_user.id):
        rows.append([InlineKeyboardButton(text='👥 إدارة المشرفين',callback_data='supervisors')])
    rows += [
        [InlineKeyboardButton(text='💾 نسخة آمنة',callback_data='backup'),InlineKeyboardButton(text='🧰 التشخيص',callback_data='diagnostics')],
        [InlineKeyboardButton(text='🔔 التنبيهات الذكية',callback_data='alerts_center'),InlineKeyboardButton(text='🗜 أرشفة الرسائل',callback_data='message_archive')],
        [InlineKeyboardButton(text='⚠️ مركز الأخطاء',callback_data='error_center'),InlineKeyboardButton(text='🧾 سجل الإدارة',callback_data='admin_audit')],
        [InlineKeyboardButton(text='🗄 فحص قاعدة البيانات',callback_data='db_health'),InlineKeyboardButton(text='📦 نسخة محلية كاملة',callback_data='local_full_backup')],
        [InlineKeyboardButton(text='⚠️ إعادة الضبط',callback_data='reset')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]
    await c.message.edit_text('⚙️ الإدارة والنظام',reply_markup=ik(rows))

@dp.callback_query(F.data=='accounts')
async def accounts(c:CallbackQuery):
    if not await allowed(c.from_user.id): return
    db=await connect()
    try:
        if owner(c.from_user.id): rows=await (await db.execute('SELECT * FROM account_slots ORDER BY operator_id,id')).fetchall()
        else: rows=await (await db.execute('SELECT * FROM account_slots WHERE operator_id=? ORDER BY id',(c.from_user.id,))).fetchall()
    finally: await db.close()
    lines=['جميع حسابات WhatsApp المرتبطة:' if owner(c.from_user.id) else 'حسابات WhatsApp الخاصة بك:']
    kb=[]
    for r in rows:
        owner_tag=f" — المشرف {r['operator_id']}" if owner(c.from_user.id) and int(r['operator_id'])!=c.from_user.id else ''
        lines.append(f"#{r['id']} {r['label']} — {'فعال' if r['enabled'] else 'متوقف'} — {r['health']}"+(f" — {r['phone_hint']}" if r['phone_hint'] else '')+owner_tag)
        kb.append([InlineKeyboardButton(text=f"📱 #{r['id']} {r['label']}",callback_data=f"acct:{r['id']}")])
    if not rows: lines.append('لا توجد حسابات.')
    kb += [[InlineKeyboardButton(text='➕ ربط حساب جديد QR',callback_data='account_add')],[InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]]
    await c.message.edit_text('\n'.join(lines),reply_markup=ik(kb))

@dp.callback_query(F.data=='account_add')
async def account_add(c:CallbackQuery,state:FSMContext):
    await state.set_state(S.account_label); await c.message.answer('أرسل اسمًا لهذا الحساب، مثل: حساب اليمن 1',reply_markup=cancel_kb())

@dp.message(S.account_label)
async def account_add_msg(m:Message,state:FSMContext):
    if not await allowed(m.from_user.id): return
    label=(m.text or '').strip()[:80]
    if not label:return await m.answer('أرسل اسمًا صالحًا.')
    try: slot,pid=await add_account_slot(m.from_user.id,label)
    except Exception:return await m.answer('هذا الاسم مستخدم لديك مسبقًا.')
    await state.clear()
    try: await provider.start(pid)
    except Exception as e:
        await set_account_status(slot,m.from_user.id,'provider_error',str(e)); return await m.answer(f'تم إنشاء الحساب #{slot} لكن موصل WhatsApp غير متاح: {e}')
    await m.answer(f'تم إنشاء الحساب #{slot}. اضغط عرض QR ثم امسحه من WhatsApp > الأجهزة المرتبطة.',reply_markup=ik([[InlineKeyboardButton(text='🔳 عرض QR',callback_data=f'acct_qr:{slot}')],[InlineKeyboardButton(text='🔄 تحديث الحالة',callback_data=f'acct_test:{slot}')],[InlineKeyboardButton(text='⬅️ الحسابات',callback_data='accounts')]]))

async def get_slot(uid,slot):
    db=await connect()
    try:
        if owner(uid): return await (await db.execute('SELECT * FROM account_slots WHERE id=?',(slot,))).fetchone()
        return await (await db.execute('SELECT * FROM account_slots WHERE id=? AND operator_id=?',(slot,uid))).fetchone()
    finally:await db.close()

@dp.callback_query(F.data.startswith('acct:'))
async def acct(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return await c.answer('غير موجود',show_alert=True)
    kb=[[InlineKeyboardButton(text='🔳 عرض QR/إعادة الربط',callback_data=f'acct_qr:{slot}'),InlineKeyboardButton(text='🧪 اختبار',callback_data=f'acct_test:{slot}')],
        [InlineKeyboardButton(text='👥 عرض المجموعات',callback_data=f'acct_groups:{slot}'),InlineKeyboardButton(text='⏯ تفعيل/إيقاف',callback_data=f'acct_toggle:{slot}')],
        [InlineKeyboardButton(text='♻️ إعادة مزامنة السجل بالـQR',callback_data=f'acct_resync:{slot}')],
        [InlineKeyboardButton(text='🗑 حذف وربط الجهاز',callback_data=f'acct_delete:{slot}')],[InlineKeyboardButton(text='⬅️ الحسابات',callback_data='accounts')]]
    await c.message.edit_text(f"الحساب #{slot}\nالاسم: {r['label']}\nالحالة: {r['health']}\nآخر خطأ: {r['last_error'] or '-'}",reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('acct_qr:'))
async def acct_qr(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return
    try:
        await provider.start(r['provider_account_id']); st=await provider.status(r['provider_account_id'])
    except Exception as e:return await c.answer(str(e),show_alert=True)
    if st.get('status')=='connected':
        me=st.get('me') or {}; await set_account_status(slot,int(r['operator_id']),'connected',None,str(me.get('id') or '')); return await c.answer('الحساب متصل بالفعل.',show_alert=True)
    qr=st.get('qr')
    if not qr:return await c.answer('QR لم يصل بعد. انتظر ثوانٍ واضغط مرة أخرى.',show_alert=True)
    path=f'data/qr_{c.from_user.id}_{slot}.png'; os.makedirs('data',exist_ok=True); qrcode.make(qr).save(path)
    try: await c.message.answer_photo(FSInputFile(path),caption='امسح هذا الرمز من WhatsApp > الإعدادات > الأجهزة المرتبطة > ربط جهاز. لا ترسل QR لأي شخص.')
    finally:
        try: os.remove(path)
        except: pass

@dp.callback_query(F.data.startswith('acct_test:'))
async def acct_test(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return
    try: st=await provider.status(r['provider_account_id'])
    except Exception as e: await set_account_status(slot,int(r['operator_id']),'provider_error',str(e)); return await c.answer(str(e),show_alert=True)
    health=st.get('status','unknown'); me=st.get('me') or {}; await set_account_status(slot,int(r['operator_id']),health,st.get('last_error'),str(me.get('id') or '') or None)
    await c.answer(f'الحالة: {health}',show_alert=True)

@dp.callback_query(F.data.startswith('acct_groups:'))
async def acct_groups(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return
    try:data=await provider.groups(r['provider_account_id'])
    except Exception as e:return await c.answer(str(e),show_alert=True)
    gs=data.get('groups') or []; text='المجموعات المتاحة للحساب:\n'+('\n'.join(f"{g['subject']}\n{g['jid']} — {g.get('size') or '?'} عضو" for g in gs[:100]) or 'لا توجد مجموعات/الحساب غير متصل.')
    if len(gs)>100:text+=f'\n\n… وإجمالي {len(gs)} مجموعة.'
    await c.message.answer(text[:3900])

@dp.callback_query(F.data.startswith('acct_toggle:'))
async def acct_toggle(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return
    db=await connect()
    try: await db.execute('UPDATE account_slots SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND operator_id=?',(slot,int(r['operator_id']))); await db.commit()
    finally: await db.close()
    await accounts(c)

@dp.callback_query(F.data.startswith('acct_resync:'))
async def acct_resync(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return await c.answer('الحساب غير موجود',show_alert=True)
    await c.message.edit_text(
        f"إعادة مزامنة سجل الحساب #{slot} — {r['label']}\n\nسيتم حذف النسخة المحلية للرسائل ونقاط التجميع لهذا الحساب فقط، ثم إلغاء ربط جلسة WhatsApp لهذا الجهاز لتربطه من جديد بالـQR. قاعدة الروابط العالمية لن تُحذف.",
        reply_markup=ik([[InlineKeyboardButton(text='✅ نعم، إعادة المزامنة',callback_data=f'acct_resync_confirm:{slot}')],[InlineKeyboardButton(text='❌ إلغاء',callback_data=f'acct:{slot}')]])
    )

@dp.callback_query(F.data.startswith('acct_resync_confirm:'))
async def acct_resync_confirm(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return await c.answer('الحساب غير موجود',show_alert=True)
    try: await provider.logout(r['provider_account_id'])
    except Exception: pass
    db=await connect()
    try:
        await db.execute('DELETE FROM wa_messages WHERE account_slot_id=?',(slot,))
        await db.execute('DELETE FROM wa_sync_cursors WHERE account_slot_id=?',(slot,))
        await db.execute('DELETE FROM collection_cursors WHERE account_slot_id=?',(slot,))
        await db.execute('UPDATE watches SET last_message_row_id=0 WHERE account_slot_id=?',(slot,))
        await db.execute("UPDATE account_slots SET health='not_linked',last_error=NULL WHERE id=? AND operator_id=?",(slot,int(r['operator_id'])))
        await db.commit()
    finally: await db.close()
    try: await provider.start(r['provider_account_id'])
    except Exception as e: return await c.message.answer(f'تم تنظيف السجل المحلي، لكن تعذر بدء جلسة QR: {e}')
    await c.message.answer('تم تنظيف السجل المحلي ونقاط التجميع لهذا الحساب. انتظر ثوانٍ ثم اضغط «عرض QR» واربط الحساب من جديد؛ سيُخزن السجل الذي يرسله WhatsApp باستخدام قارئ النصوص الجديد.',reply_markup=ik([[InlineKeyboardButton(text='🔳 عرض QR',callback_data=f'acct_qr:{slot}')],[InlineKeyboardButton(text='⬅️ الحساب',callback_data=f'acct:{slot}')]]))

@dp.callback_query(F.data.startswith('acct_delete:'))
async def acct_delete(c:CallbackQuery):
    slot=int(c.data.split(':')[1]); r=await get_slot(c.from_user.id,slot)
    if not r:return
    try: await provider.logout(r['provider_account_id'])
    except: pass
    db=await connect()
    try: await db.execute('DELETE FROM account_slots WHERE id=? AND operator_id=?',(slot,int(r['operator_id']))); await db.commit()
    finally: await db.close()
    await c.answer('تم حذف الحساب وإلغاء ربط الجلسة.',show_alert=True); await accounts(c)

@dp.callback_query(F.data=='manual')
async def manual(c:CallbackQuery,state:FSMContext): await state.set_state(S.manual) or await c.message.answer('أرسل الروابط في رسالة واحدة أو عدة أسطر.',reply_markup=cancel_kb())
@dp.message(S.manual)
async def manual_msg(m:Message,state:FSMContext):
    op=scope_uid(m.from_user.id); rep=await ingest_text(m.text or '',op,'manual')
    job_id=await record_completed_job(op,'manual_import',{'source':'message'},rep); await state.clear()
    await m.answer(f"تمت المعالجة في المهمة #{job_id}\nالمكتشفة: {rep['found']}\nالجديدة: {rep['new']}\nالمكررة: {rep['duplicates']}\nمحظورة سابقًا: {rep['blocked']}\nنُقلت ذكيًا إلى القنوات: {rep['smart_channels']}",reply_markup=back())

@dp.callback_query(F.data=='txt_import')
async def txt_import(c:CallbackQuery,state:FSMContext):
    await state.clear()
    await c.message.edit_text('اختر القسم الذي سيُستورد إليه ملف TXT. روابط قنوات WhatsApp تُنقل تلقائيًا إلى قسم القنوات حتى عند اختيار المهمة أو الطلبة.',reply_markup=ik([
        [InlineKeyboardButton(text='⭐ الروابط المهمة',callback_data='txt_section:important')],
        [InlineKeyboardButton(text='🎓 روابط الطلبة',callback_data='txt_section:students')],
        [InlineKeyboardButton(text='📢 روابط القنوات',callback_data='txt_section:channels')],
        [InlineKeyboardButton(text='⛔ الروابط المنتهية',callback_data='txt_section:expired')],
        [InlineKeyboardButton(text='🗑 الروابط المهمشة',callback_data='txt_section:ignored')],
        [InlineKeyboardButton(text='❌ إلغاء',callback_data='cancel_action')],
    ]))

@dp.callback_query(F.data.startswith('txt_section:'))
async def txt_section(c:CallbackQuery,state:FSMContext):
    section=c.data.split(':',1)[1]
    if section not in TG_SECTIONS:return await c.answer('قسم غير صالح',show_alert=True)
    await state.update_data(txt_import_section=section); await state.set_state(S.txt_import)
    await c.message.answer(f"القسم: {TG_SECTIONS.get(section,section)}\nأرسل الآن ملفًا بامتداد .txt وحجم لا يتجاوز {settings.text_import_max_mb} MB.",reply_markup=cancel_kb())

@dp.message(S.txt_import)
async def txt_import_file(m:Message,state:FSMContext):
    doc=m.document
    if not doc:return await m.answer('أرسل ملف TXT كمستند.',reply_markup=cancel_kb())
    if not (doc.file_name or '').lower().endswith('.txt'):
        return await m.answer('الملف يجب أن يكون بامتداد .txt فقط.',reply_markup=cancel_kb())
    max_bytes=settings.text_import_max_mb*1024*1024
    if int(doc.file_size or 0)>max_bytes:return await m.answer(f'حجم الملف أكبر من {settings.text_import_max_mb} MB.',reply_markup=cancel_kb())
    data=io.BytesIO()
    try:
        file=await bot.get_file(doc.file_id); await bot.download_file(file.file_path,destination=data)
        raw=data.getvalue()
        if len(raw)>max_bytes:return await m.answer('حجم الملف يتجاوز الحد المسموح.',reply_markup=cancel_kb())
        text=decode_text_file(raw)
    except ValueError:
        return await m.answer('تعذر قراءة ترميز الملف. احفظه UTF-8 ثم أرسله مجددًا.',reply_markup=cancel_kb())
    except Exception as e:
        return await m.answer(f'تعذر تنزيل الملف: {str(e)[:200]}',reply_markup=cancel_kb())
    d=await state.get_data(); section=d.get('txt_import_section','important'); op=scope_uid(m.from_user.id)
    job_id=await create_job(op,'file_import',{'file_name':(doc.file_name or '')[:180],'section':section,'bytes':len(raw)})
    await set_job(job_id,'running')
    try:
        rep=await import_links_text(op,section,text,f'txt:{doc.file_unique_id}',job_id)
        status='paused' if rep.get('paused') else ('cancelled' if rep.get('cancelled') else ('failed' if rep.get('error') else 'completed'))
        await set_job(job_id,status,rep)
    except Exception as e:
        rep={'error':str(e)}; status='failed'; await set_job(job_id,status,rep)
    await state.clear()
    await m.answer(f"تقرير استيراد TXT — المهمة #{job_id}\nالقسم: {TG_SECTIONS.get(section,section)}\nURL مكتشفة: {rep.get('urls_detected',0)}\nروابط WhatsApp: {rep.get('whatsapp_found',0)}\nجديدة: {rep.get('new',0)}\nمكررة: {rep.get('duplicates',0)}\nمنتهية مضافة: {rep.get('expired_added',0)}\nمهمشة مضافة: {rep.get('ignored_added',0)}\nنُقلت ذكيًا إلى القنوات: {rep.get('smart_channels',0)}\nمرفوضة لاختلاف قسم القنوات: {rep.get('wrong_section',0)}\nالحالة: {STATUS_LABELS.get(status,status)}",reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data=='collect')
async def collect(c:CallbackQuery,state:FSMContext):
    rows=[
        [InlineKeyboardButton(text='WhatsApp — جميع روابط واتساب',callback_data='collect_cat:whatsapp')],
        [InlineKeyboardButton(text='Telegram',callback_data='collect_cat:telegram')],
        [InlineKeyboardButton(text='X/Twitter',callback_data='collect_cat:x')],
        [InlineKeyboardButton(text='كل الروابط',callback_data='collect_cat:all')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]
    ]
    await c.message.edit_text('اختر القسم الذي تريد استخراجه من رسائل حسابات WhatsApp:',reply_markup=ik(rows))

@dp.callback_query(F.data.startswith('collect_cat:'))
async def collect_cat(c:CallbackQuery,state:FSMContext):
    await state.update_data(category=c.data.split(':',1)[1])
    await c.message.edit_text('اختر المصدر:',reply_markup=ik([
        [InlineKeyboardButton(text='كل الحسابات/كل المجموعات',callback_data='collect_src:all')],
        [InlineKeyboardButton(text='مجموعة محددة',callback_data='collect_src:specific')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]
    ]))

@dp.callback_query(F.data=='collect_src:specific')
async def collect_specific(c:CallbackQuery,state:FSMContext):
    await state.set_state(S.collect_source)
    await c.message.answer('أرسل رابط دعوة مجموعة WhatsApp الموجودة في أحد حساباتك، أو JID الظاهر من زر عرض المجموعات.',reply_markup=cancel_kb())

@dp.message(S.collect_source)
async def collect_source_msg(m:Message,state:FSMContext):
    src=(m.text or '').strip(); jid=src
    if 'chat.whatsapp.com/' in src:
        db=await connect()
        try: a=await (await db.execute("SELECT * FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id LIMIT 1")).fetchone() if owner(m.from_user.id) else await (await db.execute("SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected' ORDER BY id LIMIT 1",(m.from_user.id,))).fetchone()
        finally: await db.close()
        if not a:return await m.answer('لا يوجد حساب WhatsApp متصل لحل رابط الدعوة.',reply_markup=cancel_kb())
        try: info=await provider.invite_info(a['provider_account_id'],src); jid=(info.get('group') or {}).get('jid') or ''
        except Exception as e:return await m.answer(f'تعذر معرفة المجموعة: {e}',reply_markup=cancel_kb())
    if not jid:return await m.answer('تعذر تحديد المجموعة.',reply_markup=cancel_kb())
    await state.update_data(source_jid=jid); await state.set_state(None); await show_collect_mode(m,state)

@dp.callback_query(F.data=='collect_src:all')
async def collect_all(c:CallbackQuery,state:FSMContext):
    await state.update_data(source_jid='')
    await show_collect_mode(c.message,state)

async def show_collect_mode(msg,state):
    await msg.answer('اختر طريقة القراءة:',reply_markup=ik([
        [InlineKeyboardButton(text='⚡ سريع — رسائل تحتوي روابط فقط',callback_data='collect_mode:fast')],
        [InlineKeyboardButton(text='🔎 عميق — فحص كل النصوص المتزامنة',callback_data='collect_mode:deep')],
        [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')]
    ]))

@dp.callback_query(F.data.startswith('collect_mode:'))
async def collect_mode(c:CallbackQuery,state:FSMContext):
    await state.update_data(mode=c.data.split(':')[1])
    await c.message.edit_text('اختر الفترة:',reply_markup=ik([
        [InlineKeyboardButton(text='🆕 الجديد فقط',callback_data='collect_period:new')],
        [InlineKeyboardButton(text='الكل المتزامن',callback_data='collect_period:all')],
        [InlineKeyboardButton(text='30 يوم',callback_data='collect_period:30d'),InlineKeyboardButton(text='14 يوم',callback_data='collect_period:14d')],
        [InlineKeyboardButton(text='7 أيام',callback_data='collect_period:7d'),InlineKeyboardButton(text='3 أيام',callback_data='collect_period:3d')],
        [InlineKeyboardButton(text='24 ساعة',callback_data='collect_period:24h')],
        [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')]
    ]))

async def _collect_job(job_id:int,uid:int,chat_id:int,payload:dict):
    pre=await job_signal(job_id)
    if pre:
        await set_job(job_id,'paused' if pre=='pause_requested' else 'cancelled',{})
        return
    await set_job(job_id,'running')
    try:
        rep=await collect_from_accounts(
            uid,payload.get('category','all'),payload.get('mode','fast'),payload.get('period','new'),payload.get('source_jid',''),job_id=job_id
        )
        status='paused' if rep.get('paused') else ('cancelled' if rep.get('cancelled') else 'completed')
        await set_job(job_id,status,rep)
        lines=[
            f"{'تم إيقاف' if status=='paused' else ('تم إلغاء' if status=='cancelled' else 'اكتملت')} المهمة #{job_id} — تجميع {CATEGORY_LABELS.get(payload.get('category'),payload.get('category'))}",
            f"الرسائل التي تمت قراءتها: {rep.get('messages',0)}",
            f"رسائل ذات نص: {rep.get('text_messages',0)}",
            f"رسائل تحتوي URL: {rep.get('url_messages',0)}",
            f"إجمالي URL المستخرجة: {rep.get('urls_found',0)}",
            f"روابط مطابقة للقسم: {rep.get('matching_urls',0)}",
            f"روابط جديدة: {rep.get('new',0)}",
            f"مكررة: {rep.get('duplicates',0)}",
            f"محظورة كسابقًا منتهية: {rep.get('blocked',0)}",
            f"روابط قنوات صُنفت ذكيًا: {rep.get('smart_channels',0)}",
            '', 'تقرير الحسابات:'
        ]
        for a in rep.get('accounts',[]):
            mark='✅' if a.get('completed') else '⛔'
            lines.append(
                f"• {a.get('label')} {mark} | متاح {a.get('eligible_messages',0)} | نص {a.get('text_messages',0)} | URL-msg {a.get('url_messages',0)} | مطابق {a.get('matching_urls',0)} | جديد {a.get('new',0)} | مكرر {a.get('duplicates',0)} | قنوات {a.get('smart_channels',0)}"
            )
        if rep.get('messages',0)>0 and rep.get('text_messages',0)==0:
            lines += ['', '⚠️ تشخيص: توجد رسائل متزامنة لكن نصوصها فارغة. افتح الحساب واضغط «♻️ إعادة مزامنة السجل بالـQR» ثم اربطه من جديد.']
        elif rep.get('text_messages',0)>0 and rep.get('url_messages',0)==0:
            lines += ['', 'ℹ️ لم يجد القارئ أي URL داخل النصوص التي وصلت من WhatsApp ضمن هذه الفترة/المصدر.']
        await bot.send_message(chat_id,'\n'.join(lines)[:3900],reply_markup=menu(uid))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)})
        await bot.send_message(chat_id,f'فشلت المهمة #{job_id}: {e}',reply_markup=menu(uid))

@dp.callback_query(F.data.startswith('collect_period:'))
async def collect_period(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.clear(); period=c.data.split(':')[1]
    payload={
        'category':d.get('category','all'),'mode':d.get('mode','fast'),'period':period,'source_jid':d.get('source_jid','')
    }
    op=scope_uid(c.from_user.id); job_id=await create_job(op,'collection',payload)
    label=CATEGORY_LABELS.get(payload['category'],payload['category'])
    await c.message.answer(
        f'✅ تم إنشاء المهمة #{job_id}: تجميع {label}\nطريقة القراءة: {payload["mode"]}\nالفترة: {period}\n\nسيتم تنفيذها في الخلفية ويمكنك متابعة حالتها من «9 - المهام».',
        reply_markup=menu(c.from_user.id)
    )
    _spawn(_collect_job(job_id,op,c.message.chat.id,payload))

async def run_audit(op,urls,name,job_id=None,audit_id=None):
    db=await connect(); now=now_iso()
    try:
        if audit_id is None:
            cur=await db.execute("INSERT INTO audits(operator_id,name,mode,status,created_at) VALUES(?,?,?,'running',?)",(op,name,'web',now)); audit_id=int(cur.lastrowid)
            ordinal=0
            for lid,u in urls:
                blocked=await (await db.execute('SELECT 1 FROM expired_registry WHERE normalized_url=? UNION SELECT 1 FROM ignored_registry WHERE normalized_url=?',(u,u))).fetchone()
                if blocked: continue
                ordinal+=1; await db.execute('INSERT OR IGNORE INTO audit_inputs(audit_id,link_id,normalized_url,ordinal) VALUES(?,?,?,?)',(audit_id,lid,u,ordinal))
        else:
            await db.execute("UPDATE audits SET status='running',completed_at=NULL WHERE id=? AND operator_id=?",(audit_id,op))
        await db.commit()
    finally: await db.close()
    db=await connect()
    try:
        total=int((await (await db.execute('SELECT COUNT(*) c FROM audit_inputs WHERE audit_id=?',(audit_id,))).fetchone())['c'] or 0)
        checked=int((await (await db.execute('SELECT COUNT(*) c FROM audit_results WHERE audit_id=?',(audit_id,))).fetchone())['c'] or 0)
    finally: await db.close()
    if job_id: await set_job(job_id,'running',{'audit_id':audit_id,'checked':checked,'total':total})
    cancelled=paused=False; expired_added=0
    while True:
        sig=await job_signal(job_id)
        if sig: cancelled=sig=='cancel_requested'; paused=sig=='pause_requested'; break
        db=await connect()
        try:
            sql=("SELECT i.link_id,i.normalized_url FROM audit_inputs i LEFT JOIN audit_results r ON r.audit_id=i.audit_id AND r.normalized_url=i.normalized_url WHERE i.audit_id=? AND r.id IS NULL ORDER BY i.ordinal LIMIT 1000")
            batch=await (await db.execute(sql,(audit_id,))).fetchall()
        finally: await db.close()
        if not batch: break
        results=await inspect_many([r['normalized_url'] for r in batch]); db=await connect()
        try:
            for row,out in zip(batch,results):
                await db.execute('INSERT OR REPLACE INTO audit_results(audit_id,link_id,normalized_url,status,display_name,details,checked_at) VALUES(?,?,?,?,?,?,?)',(audit_id,row['link_id'],row['normalized_url'],out.status,out.display_name,out.details,now_iso()))
                if out.status=='expired':
                    cur=await db.execute('INSERT OR IGNORE INTO expired_registry(normalized_url,reason,source,created_at) VALUES(?,?,?,?)',(row['normalized_url'],'web_audit','audit',now_iso()))
                    expired_added+=max(0,int(cur.rowcount or 0))
                    await db.execute('DELETE FROM join_queue WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(row['normalized_url'],))
                    await db.execute('DELETE FROM link_sections WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(row['normalized_url'],))
                    await db.execute('DELETE FROM links WHERE normalized_url=?',(row['normalized_url'],))
            await db.commit(); checked+=len(batch)
        finally: await db.close()
        if job_id: await set_job(job_id,'running',{'audit_id':audit_id,'checked':checked,'total':total,'expired_added':expired_added})
    st='paused' if paused else ('cancelled' if cancelled else 'completed'); db=await connect()
    try:
        await db.execute('UPDATE audits SET status=?,completed_at=? WHERE id=?',(st,now_iso() if st=='completed' else None,audit_id)); await db.commit()
    finally: await db.close()
    db=await connect()
    try:
        rows=await (await db.execute('SELECT status,COUNT(*) c FROM audit_results WHERE audit_id=? GROUP BY status',(audit_id,))).fetchall()
    finally: await db.close()
    return {'audit_id':audit_id,'cancelled':cancelled,'paused':paused,'checked':checked,'total':total,
            'expired_added':expired_added,'status_counts':{r['status']:int(r['c']) for r in rows}}

async def _audit_job(job_id:int,uid:int,chat_id:int,urls,name,audit_id=None):
    pre=await job_signal(job_id)
    if pre:
        await set_job(job_id,'paused' if pre=='pause_requested' else 'cancelled',{})
        return
    await set_job(job_id,'running')
    try:
        rep=await run_audit(uid,urls,name,job_id,audit_id)
        status='paused' if rep.get('paused') else ('cancelled' if rep.get('cancelled') else 'completed')
        await set_job(job_id,status,rep)
        aid=rep['audit_id']
        kb=[[InlineKeyboardButton(text='➕ إضافة المجموعات الصالحة لطابور الانضمام',callback_data=f'audit_enqueue:{aid}')],[InlineKeyboardButton(text='📦 تصدير التقرير',callback_data=f'audit_export:{aid}')],[InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]]
        await bot.send_message(chat_id,f"{'تم إيقاف' if status=='paused' else ('تم إلغاء' if status=='cancelled' else 'اكتمل')} الفحص — المهمة #{job_id}\nتقرير الفحص #{aid}\nتم فحص: {rep.get('checked',0)} من {rep.get('total',0)}\nأضيف إلى قسم الروابط المنتهية: {rep.get('expired_added',0)}\nتفصيل الحالات: {json.dumps(rep.get('status_counts',{}),ensure_ascii=False)}",reply_markup=ik(kb))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)})
        await bot.send_message(chat_id,f'فشلت مهمة الفحص #{job_id}: {e}',reply_markup=menu(uid))

@dp.callback_query(F.data=='audit')
async def audit(c:CallbackQuery,state:FSMContext):
    await c.message.edit_text('فحص WhatsApp عبر صفحات الدعوة العامة — بدون استخدام الحسابات للروابط التي يمكن تصنيفها بالويب.',reply_markup=ik([[InlineKeyboardButton(text='فحص روابط قاعدة البيانات',callback_data='audit_db')],[InlineKeyboardButton(text='لصق روابط للفحص',callback_data='audit_paste')],[InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]]))

@dp.callback_query(F.data=='audit_db')
async def audit_db(c:CallbackQuery):
    db=await connect()
    try: rows=await (await db.execute("SELECT id,normalized_url FROM links WHERE category='whatsapp_group' ORDER BY id")).fetchall()
    finally: await db.close()
    urls=[(r['id'],r['normalized_url']) for r in rows]
    op=scope_uid(c.from_user.id); job_id=await create_job(op,'audit',{'source':'database','count':len(urls)})
    await c.message.answer(f'✅ تم إنشاء مهمة الفحص #{job_id} لعدد {len(urls)} رابط. ستعمل في الخلفية ويمكن إلغاؤها من «9 - المهام».',reply_markup=menu(c.from_user.id))
    _spawn(_audit_job(job_id,op,c.message.chat.id,urls,'Database WhatsApp audit'))

@dp.callback_query(F.data=='audit_paste')
async def audit_paste(c:CallbackQuery,state:FSMContext):
    await state.set_state(S.audit_paste)
    await c.message.answer('ألصق روابط مجموعات WhatsApp المراد فحصها.',reply_markup=cancel_kb())

@dp.message(S.audit_paste)
async def audit_paste_msg(m:Message,state:FSMContext):
    urls=[]
    for u in extract_urls(m.text or ''):
        n=normalize_url(u)
        if classify_link(n)=='whatsapp_group':urls.append((None,n))
    await state.clear()
    if not urls:return await m.answer('لم أجد روابط دعوات WhatsApp.',reply_markup=back())
    op=scope_uid(m.from_user.id); job_id=await create_job(op,'audit',{'source':'paste','count':len(urls)})
    await m.answer(f'✅ تم إنشاء مهمة الفحص #{job_id} لعدد {len(urls)} رابط. ستعمل في الخلفية.',reply_markup=menu(m.from_user.id))
    _spawn(_audit_job(job_id,op,m.chat.id,urls,'Pasted WhatsApp audit'))

@dp.callback_query(F.data.startswith('audit_export:'))
async def audit_export(c:CallbackQuery):
    aid=int(c.data.split(':')[1]); db=await connect()
    try: row=await (await db.execute('SELECT 1 FROM audits WHERE id=? AND operator_id=?',(aid,scope_uid(c.from_user.id)))).fetchone()
    finally: await db.close()
    if not row:return await c.answer('الفحص غير موجود ضمن نطاقك.',show_alert=True)
    path=await export_audit_zip(aid); await c.message.answer_document(FSInputFile(path),caption=f'تقرير الفحص #{aid}')

@dp.callback_query(F.data.startswith('audit_enqueue:'))
async def audit_enqueue(c:CallbackQuery):
    aid=int(c.data.split(':')[1]); db=await connect()
    try:
        audit=await (await db.execute('SELECT 1 FROM audits WHERE id=? AND operator_id=?',(aid,scope_uid(c.from_user.id)))).fetchone()
        rows=await (await db.execute("SELECT DISTINCT link_id,normalized_url,display_name FROM audit_results WHERE audit_id=? AND status='group_active'",(aid,))).fetchall() if audit else []
    finally: await db.close()
    if not audit:return await c.answer('الفحص غير موجود ضمن نطاقك.',show_alert=True)
    added=0
    for r in rows:
        lid=r['link_id']
        if not lid:
            _,lid=await upsert_link(r['normalized_url'],r['normalized_url'],'whatsapp_group',scope_uid(c.from_user.id),f'audit:{aid}',r['display_name'],section='important')
        if not lid: continue
        db=await connect()
        try:
            cur=await db.execute("INSERT OR IGNORE INTO join_queue(operator_id,link_id,status,created_at,updated_at) VALUES(?,?, 'pending',?,?)",(scope_uid(c.from_user.id),lid,now_iso(),now_iso())); added+=max(0,cur.rowcount); await db.commit()
        finally: await db.close()
    await c.answer(f'تمت إضافة {added} مجموعة صالحة إلى قاعدة الروابط المهمة وطابور الانضمام.',show_alert=True)

@dp.callback_query(F.data=='expired_import')
async def expired_import(c:CallbackQuery,state:FSMContext):
    if not await allowed(c.from_user.id):return
    await state.set_state(S.expired); await c.message.answer('ألصق روابط WhatsApp المنتهية القديمة. سيتم منعها عالميًا من الإدخال والفحص لاحقًا.',reply_markup=cancel_kb())
@dp.message(S.expired)
async def expired_msg(m:Message,state:FSMContext):
    if not await allowed(m.from_user.id):return
    urls={normalize_url(u) for u in extract_urls(m.text or '') if is_whatsapp_invite(u)}; db=await connect(); added=removed=0
    try:
        for u in urls:
            cur=await db.execute('INSERT OR IGNORE INTO expired_registry(normalized_url,reason,source,created_at) VALUES(?,?,?,?)',(u,'imported_expired','manual',now_iso())); added+=max(0,int(cur.rowcount or 0))
            active=await (await db.execute('SELECT id FROM links WHERE normalized_url=?',(u,))).fetchone(); removed+=1 if active else 0
            await db.execute('DELETE FROM join_queue WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(u,)); await db.execute('DELETE FROM link_sections WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(u,)); await db.execute('DELETE FROM links WHERE normalized_url=?',(u,))
        await db.commit()
    finally:await db.close()
    rep={'urls_detected':len(urls),'expired_added':added,'duplicates':len(urls)-added,'removed_from_active':removed}
    job_id=await record_completed_job(scope_uid(m.from_user.id),'expired_import',{'source':'message'},rep)
    await state.clear(); await m.answer(f'تمت معالجة المهمة #{job_id}\nأضيف إلى المنتهية: {added}\nمكرر: {len(urls)-added}\nأزيل من الروابط النشطة: {removed}',reply_markup=back())

@dp.callback_query(F.data=='ignored_import')
async def ignored_import(c:CallbackQuery,state:FSMContext):
    if not await allowed(c.from_user.id):return
    await state.set_state(S.ignored)
    await c.message.answer('ألصق روابط WhatsApp المهمشة التي لا تريد اعتبارها مهمة. سيتم منعها عالميًا من الإدخال والفحص والانضمام لاحقًا.',reply_markup=cancel_kb())

@dp.message(S.ignored)
async def ignored_msg(m:Message,state:FSMContext):
    if not await allowed(m.from_user.id):return
    urls={normalize_url(u) for u in extract_urls(m.text or '') if normalize_url(u) and classify_link(u).startswith('whatsapp_')}
    db=await connect(); added=removed=0
    try:
        for u in urls:
            cur=await db.execute('INSERT OR IGNORE INTO ignored_registry(normalized_url,reason,source,created_at) VALUES(?,?,?,?)',(u,'operator_ignored',f'operator:{m.from_user.id}',now_iso())); added+=max(0,int(cur.rowcount or 0))
            active=await (await db.execute('SELECT id FROM links WHERE normalized_url=?',(u,))).fetchone(); removed+=1 if active else 0
            await db.execute('DELETE FROM join_queue WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(u,))
            await db.execute('DELETE FROM link_sections WHERE link_id IN (SELECT id FROM links WHERE normalized_url=?)',(u,))
            await db.execute('DELETE FROM links WHERE normalized_url=?',(u,))
        await db.commit()
    finally: await db.close()
    rep={'urls_detected':len(urls),'ignored_added':added,'duplicates':len(urls)-added,'removed_from_active':removed}
    job_id=await record_completed_job(scope_uid(m.from_user.id),'ignored_import',{'source':'message'},rep)
    await state.clear(); await m.answer(f'تمت معالجة المهمة #{job_id}\nأضيف إلى المهمشة: {added}\nمكرر: {len(urls)-added}\nأزيل من الروابط النشطة: {removed}\nلن يعود إلى الفحص أو الانضمام.',reply_markup=back())

@dp.callback_query(F.data=='tg_sources')
async def tg_sources(c:CallbackQuery,state:FSMContext):
    op=scope_uid(c.from_user.id)
    db=await connect()
    try:
        rows=await (await db.execute('SELECT * FROM telegram_sources WHERE owner_id=? ORDER BY id DESC',(op,))).fetchall()
    except Exception as e:
        await db.close()
        return await c.answer(f'تعذر قراءة مصادر Telegram: {str(e)[:140]}',show_alert=True)
    finally:
        try: await db.close()
        except Exception: pass
    hs=await history_status(owner_id=op)
    history_label=(f"✅ جلسات جاهزة: {hs.get('authorized_count',0)}" if hs.get('authorized') else ('⚠️ لا توجد جلسة Telethon مسجلة' if hs.get('configured') else 'ℹ️ بيانات Telethon غير مكتملة'))
    lines=['📡 مصادر Telegram لروابط WhatsApp','',
           'جلسات Telethon المسجلة من البوت تستخرج التاريخ القديم ثم تزامن المنشورات الجديدة تلقائيًا. ويمكن أيضًا الالتقاط عبر Bot API عندما يكون البوت داخل المصدر.',
           history_label]
    kb=[]
    for r in rows:
        sec=TG_SECTIONS.get(r['section'] or 'important',r['section'] or 'important')
        lines.append(f"#{r['id']} {r['title'] or r['username'] or r['chat_id']} — {sec} — {'فعال' if r['enabled'] else 'متوقف'} — جُمِع {r['collected_links']} — آخر مزامنة {r['last_sync_at'] or '-'}")
        row=[InlineKeyboardButton(text=f"{'⏸' if r['enabled'] else '▶️'} #{r['id']}",callback_data=f"tgsrc_toggle:{r['id']}"),InlineKeyboardButton(text='🔄 الحالة',callback_data=f"tgsrc_refresh:{r['id']}")]
        if hs.get('authorized'): row.append(InlineKeyboardButton(text='🕘 استيراد القديم',callback_data=f"tgsrc_history:{r['id']}"))
        kb.append(row)
        kb.append([InlineKeyboardButton(text='🗑 حذف المصدر',callback_data=f"tgsrc_delete:{r['id']}")])
    kb += [[InlineKeyboardButton(text='➕ إضافة مصدر Telegram',callback_data='tgsrc_add')],
           [InlineKeyboardButton(text='🔐 جلسات Telegram واستخراج جلسة',callback_data='tg_session_info')],
           [InlineKeyboardButton(text='⬅️ الروابط والتجميع',callback_data='links_hub')]]
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik(kb))

@dp.callback_query(F.data=='tg_session_info')
async def tg_session_info(c:CallbackQuery):
    op=scope_uid(c.from_user.id); rows=await list_telegram_sessions(op); hs=await history_status(owner_id=op)
    lines=['🔐 جلسات Telegram (Telethon)','',
           'إنشاء الجلسة يتم كاملًا من داخل البوت. بعد ربط المصدر، تُقرأ المنشورات القديمة مرة واحدة ثم الجديدة تلقائيًا.',
           '⚠️ كود الدخول وكلمة مرور التحقق بخطوتين بيانات شديدة الحساسية. لا تستخدم هذه الشاشة إلا في محادثتك الخاصة مع البوت.']
    kb=[]
    if not hs.get('configured'):
        lines += ['', 'يلزم وجود TELEGRAM_API_ID وTELEGRAM_API_HASH في .env مرة واحدة. لن يطلبهما البوت داخل المحادثة ولن يعرضهما.']
    for r in rows:
        lines.append(f"#{r['id']} {r['label']} — {r['health']} — {r['phone_hint'] or '-'} — @{r['username'] or '-'}")
        kb.append([InlineKeyboardButton(text=f"🧪 فحص الجلسة #{r['id']}",callback_data=f"tg_session_verify:{r['id']}")])
    if not rows: lines.append('\nلا توجد جلسات مسجلة حتى الآن.')
    if hs.get('configured'):
        kb.append([InlineKeyboardButton(text='➕ استخراج/إنشاء جلسة Telethon من البوت',callback_data='tg_session_add')])
    kb.append([InlineKeyboardButton(text='⬅️ مصادر Telegram',callback_data='tg_sources')])
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik(kb))

def _tg_login_error(rep:dict)->str:
    labels={
        'api_credentials_missing':'بيانات TELEGRAM_API_ID وTELEGRAM_API_HASH غير موجودة في .env.',
        'telethon_not_installed':'مكتبة Telethon غير مثبتة.',
        'invalid_phone':'رقم الهاتف غير صالح. أرسله مع مفتاح الدولة، مثال +9677XXXXXXXX.',
        'invalid_label':'اسم الجلسة غير صالح.','duplicate_label':'اسم الجلسة مستخدم مسبقًا.',
        'login_not_pending':'انتهت محاولة الدخول. ابدأ استخراج جلسة جديدة.',
        'invalid_code_format':'صيغة الكود غير صحيحة. أرسل الأرقام فقط أو افصل بينها بمسافات.',
        'invalid_code':'كود الدخول غير صحيح. حاول مجددًا.','code_expired':'انتهت صلاحية الكود. ابدأ جلسة جديدة.',
        'invalid_password':'كلمة مرور التحقق بخطوتين غير صحيحة. حاول مجددًا.',
    }
    return labels.get(rep.get('error'),f"تعذر تسجيل الجلسة: {rep.get('details') or rep.get('error') or 'خطأ غير معروف'}")

@dp.callback_query(F.data=='tg_session_add')
async def tg_session_add(c:CallbackQuery,state:FSMContext):
    if c.message.chat.type!='private':return await c.answer('استخراج الجلسة مسموح في المحادثة الخاصة مع البوت فقط.',show_alert=True)
    await cancel_telegram_login(scope_uid(c.from_user.id)); await state.clear(); await state.set_state(S.tg_session_label)
    await c.message.answer('أرسل اسمًا للجلسة، مثال: حساب مصادر اليمن.',reply_markup=cancel_kb())

@dp.message(S.tg_session_label)
async def tg_session_label_msg(m:Message,state:FSMContext):
    if m.chat.type!='private':return await m.answer('أكمل تسجيل الجلسة في المحادثة الخاصة مع البوت فقط.')
    label=(m.text or '').strip()[:80]
    if not label:return await m.answer('أرسل اسمًا صالحًا.',reply_markup=cancel_kb())
    await state.update_data(tg_session_label=label); await state.set_state(S.tg_session_phone)
    await m.answer('أرسل رقم حساب Telegram مع مفتاح الدولة، مثال: +9677XXXXXXXX. سأحاول حذف رسالة الرقم بعد قراءتها.',reply_markup=cancel_kb())

@dp.message(S.tg_session_phone)
async def tg_session_phone_msg(m:Message,state:FSMContext):
    if m.chat.type!='private':return await m.answer('أكمل تسجيل الجلسة في المحادثة الخاصة مع البوت فقط.')
    phone=(m.text or '').strip(); d=await state.get_data()
    try: await m.delete()
    except Exception: pass
    rep=await begin_telegram_login(scope_uid(m.from_user.id),d.get('tg_session_label',''),phone)
    if not rep.get('ok'):
        return await m.answer(_tg_login_error(rep),reply_markup=cancel_kb())
    await state.set_state(S.tg_session_code)
    await m.answer(f"أرسل كود الدخول الذي وصلك إلى حساب Telegram المرتبط بـ{rep.get('phone_hint','')}. يمكنك كتابته هكذا: 1 2 3 4 5. سأحاول حذف رسالة الكود فور قراءتها.",reply_markup=cancel_kb())

@dp.message(S.tg_session_code)
async def tg_session_code_msg(m:Message,state:FSMContext):
    if m.chat.type!='private':return await m.answer('أكمل تسجيل الجلسة في المحادثة الخاصة مع البوت فقط.')
    code=m.text or ''
    try: await m.delete()
    except Exception: pass
    rep=await submit_telegram_code(scope_uid(m.from_user.id),code)
    if not rep.get('ok'):
        return await m.answer(_tg_login_error(rep),reply_markup=cancel_kb())
    if rep.get('password_required'):
        await state.set_state(S.tg_session_password)
        return await m.answer('الحساب محمي بالتحقق بخطوتين. أرسل كلمة مرور Telegram الآن، وسأحاول حذف رسالتها فور قراءتها.',reply_markup=cancel_kb())
    await state.clear()
    job_id=await record_completed_job(scope_uid(m.from_user.id),'telegram_session_login',{'session_id':rep['session_id']},{'authorized':True,'session_id':rep['session_id'],'username':rep.get('username')})
    await m.answer(f"✅ تم استخراج الجلسة #{rep['session_id']} وإضافتها تلقائيًا للبوت.\nالحساب: @{rep.get('username') or '-'}\nسُجلت العملية في المهمة #{job_id}.\nيمكنك الآن إضافتها إلى مصادر Telegram؛ وسيتم استخراج القديم والجديد تلقائيًا.",reply_markup=menu(m.from_user.id))

@dp.message(S.tg_session_password)
async def tg_session_password_msg(m:Message,state:FSMContext):
    if m.chat.type!='private':return await m.answer('أكمل تسجيل الجلسة في المحادثة الخاصة مع البوت فقط.')
    password=m.text or ''
    try: await m.delete()
    except Exception: pass
    rep=await submit_telegram_password(scope_uid(m.from_user.id),password)
    if not rep.get('ok'):
        return await m.answer(_tg_login_error(rep),reply_markup=cancel_kb())
    await state.clear()
    job_id=await record_completed_job(scope_uid(m.from_user.id),'telegram_session_login',{'session_id':rep['session_id']},{'authorized':True,'session_id':rep['session_id'],'username':rep.get('username')})
    await m.answer(f"✅ تم استخراج الجلسة #{rep['session_id']} وإضافتها تلقائيًا للبوت.\nالحساب: @{rep.get('username') or '-'}\nسُجلت العملية في المهمة #{job_id}.\nأضف مصادر القنوات الآن وسيقرأ القديم والجديد تلقائيًا.",reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data.startswith('tg_session_verify:'))
async def tg_session_verify(c:CallbackQuery):
    sid=int(c.data.split(':')[1]); rep=await verify_telegram_session(scope_uid(c.from_user.id),sid)
    await c.answer('✅ الجلسة صالحة ومصرح بها.' if rep.get('authorized') else _tg_login_error(rep),show_alert=True)

@dp.callback_query(F.data=='tgsrc_add')
async def tgsrc_add(c:CallbackQuery,state:FSMContext):
    await state.clear()
    await c.message.edit_text('اختر القسم الذي ستُحفظ فيه الروابط القادمة من هذا المصدر:',reply_markup=ik([
        [InlineKeyboardButton(text='⭐ الروابط المهمة',callback_data='tgsrc_section:important')],
        [InlineKeyboardButton(text='🎓 روابط الطلبة',callback_data='tgsrc_section:students')],
        [InlineKeyboardButton(text='⛔ الروابط المنتهية',callback_data='tgsrc_section:expired')],
        [InlineKeyboardButton(text='🗑 الروابط المهمشة',callback_data='tgsrc_section:ignored')],
        [InlineKeyboardButton(text='📢 روابط القنوات',callback_data='tgsrc_section:channels')],
        [InlineKeyboardButton(text='❌ إلغاء',callback_data='cancel_action')],
    ]))

@dp.callback_query(F.data.startswith('tgsrc_section:'))
async def tgsrc_section(c:CallbackQuery,state:FSMContext):
    section=c.data.split(':',1)[1]
    if section not in TG_SECTIONS:return await c.answer('قسم غير صالح',show_alert=True)
    await state.update_data(telegram_source_section=section)
    sessions=await list_telegram_sessions(scope_uid(c.from_user.id)); kb=[]
    for row in sessions:
        if row['enabled'] and row['health'] in {'authorized','authorized_unverified'}:
            kb.append([InlineKeyboardButton(text=f"📲 {row['label']} — {row['phone_hint'] or '#'+str(row['id'])}",callback_data=f"tgsrc_session:{row['id']}")])
    kb.append([InlineKeyboardButton(text='🤖 وصول البوت مباشرة (بدون جلسة)',callback_data='tgsrc_session:0')])
    kb.append([InlineKeyboardButton(text='❌ إلغاء',callback_data='cancel_action')])
    await c.message.edit_text(f"القسم: {TG_SECTIONS[section]}\nاختر جلسة Telegram التي تستطيع الوصول إلى المصدر. الجلسة ستقرأ القديم والجديد تلقائيًا:",reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('tgsrc_session:'))
async def tgsrc_session(c:CallbackQuery,state:FSMContext):
    sid=int(c.data.split(':',1)[1]); d=await state.get_data()
    if d.get('telegram_source_section') not in TG_SECTIONS:return await c.answer('ابدأ من إضافة المصدر.',show_alert=True)
    if sid:
        sessions=await list_telegram_sessions(scope_uid(c.from_user.id))
        row=next((x for x in sessions if int(x['id'])==sid and x['enabled']),None)
        if not row:return await c.answer('الجلسة غير متاحة.',show_alert=True)
    await state.update_data(telegram_source_session=sid); await state.set_state(S.telegram_source)
    note='سيتم التحقق والوصول بواسطة جلسة المستخدم المختارة.' if sid else 'يجب أن يكون البوت عضوًا/مشرفًا في المصدر.'
    await c.message.answer(f"أرسل @username أو رابط https://t.me/username أو Chat ID.\n{note}",reply_markup=cancel_kb())

@dp.message(S.telegram_source)
async def tgsrc_add_msg(m:Message,state:FSMContext):
    raw=(m.text or '').strip(); d=await state.get_data(); section=d.get('telegram_source_section','important'); op=scope_uid(m.from_user.id); sid=int(d.get('telegram_source_session',0) or 0)
    if sid:
        resolved=await resolve_telegram_source(op,sid,raw)
        if not resolved.get('ok'):return await m.answer(f"تعذر وصول الجلسة إلى المصدر: {resolved.get('details') or resolved.get('error')}\nتأكد أن الحساب داخل القناة ثم أرسل @username أو Chat ID.",reply_markup=cancel_kb())
        chat_id=int(resolved['chat_id']); title=resolved.get('title'); username=resolved.get('username')
    else:
        ident=raw
        if raw.startswith('https://t.me/'):
            tail=raw.split('https://t.me/',1)[1].split('/',1)[0]
            if tail and not tail.startswith('+'): ident='@'+tail
        elif raw.lstrip('-').isdigit(): ident=int(raw)
        try: chat=await bot.get_chat(ident)
        except Exception as e:return await m.answer(f'تعذر وصول البوت إلى المصدر: {e}\nأضف البوت إلى القناة/المجموعة أو اختر جلسة Telethon.',reply_markup=cancel_kb())
        chat_id=int(chat.id); title=getattr(chat,'title',None); username=getattr(chat,'username',None)
    db=await connect(); now=now_iso()
    try:
        await db.execute("""INSERT INTO telegram_sources(owner_id,chat_id,title,username,section,telegram_session_id,enabled,auto_join_queue,created_at,updated_at)
          VALUES(?,?,?,?,?,?,1,1,?,?) ON CONFLICT(owner_id,chat_id) DO UPDATE SET title=excluded.title,username=excluded.username,section=excluded.section,telegram_session_id=excluded.telegram_session_id,enabled=1,updated_at=excluded.updated_at""",
          (op,chat_id,title,username,section,sid or None,now,now)); await db.commit()
        src=await (await db.execute('SELECT id FROM telegram_sources WHERE owner_id=? AND chat_id=?',(op,chat_id))).fetchone(); source_id=int(src['id'])
    finally: await db.close()
    await state.clear(); extra=''
    if sid:
        job_id=await create_job(op,'telegram_history',{'source_id':source_id,'initial_sync':True,'session_id':sid})
        _spawn(_telegram_history_job(job_id,op,m.chat.id,source_id,m.from_user.id)); extra=f'\nبدأ استخراج كل التاريخ القديم في المهمة #{job_id}، وبعدها تستمر مزامنة الجديد تلقائيًا.'
    await m.answer(f'✅ تم تسجيل المصدر: {title or chat_id}\nالقسم: {TG_SECTIONS.get(section,section)}\nسيتم التقاط روابط WhatsApp الجديدة تلقائيًا.{extra}',reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data.startswith('tgsrc_toggle:'))
async def tgsrc_toggle(c:CallbackQuery,state:FSMContext):
    sid=int(c.data.split(':')[1]); op=scope_uid(c.from_user.id); db=await connect()
    try: await db.execute('UPDATE telegram_sources SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,updated_at=? WHERE id=? AND owner_id=?',(now_iso(),sid,op)); await db.commit()
    finally: await db.close()
    await tg_sources(c,state)

@dp.callback_query(F.data.startswith('tgsrc_refresh:'))
async def tgsrc_refresh(c:CallbackQuery,state:FSMContext):
    sid=int(c.data.split(':')[1]); op=scope_uid(c.from_user.id); db=await connect()
    try:r=await (await db.execute('SELECT * FROM telegram_sources WHERE id=? AND owner_id=?',(sid,op))).fetchone()
    finally: await db.close()
    if not r:return await c.answer('المصدر غير موجود.',show_alert=True)
    await c.answer(f"{TG_SECTIONS.get(r['section'],r['section'])} — {'فعال' if r['enabled'] else 'متوقف'} — جُمِع {r['collected_links']} رابط — آخر مزامنة: {r['last_sync_at'] or '-'}.",show_alert=True)

@dp.callback_query(F.data.startswith('tgsrc_delete:'))
async def tgsrc_delete(c:CallbackQuery,state:FSMContext):
    sid=int(c.data.split(':')[1]); op=scope_uid(c.from_user.id); db=await connect()
    try: await db.execute('DELETE FROM telegram_sources WHERE id=? AND owner_id=?',(sid,op)); await db.commit()
    finally: await db.close()
    await tg_sources(c,state)

@dp.callback_query(F.data.startswith('tgsrc_history:'))
async def tgsrc_history(c:CallbackQuery):
    sid=int(c.data.split(':')[1]); op=scope_uid(c.from_user.id); hs=await history_status(verify=True,owner_id=op)
    if not hs.get('authorized'):
        return await c.answer('جلسة Telethon غير جاهزة. افتح «جلسات Telegram» ثم اضغط استخراج جلسة من البوت.',show_alert=True)
    job_id=await create_job(op,'telegram_history',{'source_id':sid})
    await c.answer(f'بدأ استيراد التاريخ في المهمة #{job_id}.',show_alert=True)
    _spawn(_telegram_history_job(job_id,op,c.message.chat.id,sid,c.from_user.id))

async def _telegram_history_job(job_id:int,op:int,chat_id:int,sid:int,actor_uid:int):
    await set_job(job_id,'running')
    try:
        rep=await import_source_history(sid,op,job_id)
        if rep.get('paused'):st='paused'
        elif rep.get('cancelled'):st='cancelled'
        elif rep.get('error'):st='failed'
        else:st='completed'
        await set_job(job_id,st,rep)
        await bot.send_message(chat_id,f"تقرير استيراد تاريخ Telegram #{job_id}\n"+json.dumps(rep,ensure_ascii=False,indent=2)[:3300],reply_markup=menu(actor_uid))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)}); await bot.send_message(chat_id,f'فشل استيراد تاريخ Telegram #{job_id}: {e}',reply_markup=menu(actor_uid))

async def _ingest_telegram_source_message(msg:Message):
    db=await connect()
    try: src=await (await db.execute('SELECT * FROM telegram_sources WHERE chat_id=? AND enabled=1 ORDER BY id LIMIT 1',(int(msg.chat.id),))).fetchone()
    finally: await db.close()
    if not src:return
    rep=await ingest_source_text(msg.text or msg.caption or '',src,int(src['owner_id']),f'telegram_source:{msg.chat.id}',getattr(msg.chat,'title',None))
    if not rep.get('found'):return
    db=await connect()
    try: await db.execute('UPDATE telegram_sources SET collected_links=collected_links+?,updated_at=? WHERE id=?',(int(rep.get('new',0)),now_iso(),src['id'])); await db.commit()
    finally: await db.close()

@dp.message(F.chat.type.in_({'group','supergroup'}))
async def telegram_source_group_message(m:Message): await _ingest_telegram_source_message(m)

@dp.channel_post()
async def telegram_source_channel_post(m:Message): await _ingest_telegram_source_message(m)

@dp.callback_query(F.data=='joinq')
async def joinq(c:CallbackQuery,state:FSMContext):
    await state.clear()
    op=scope_uid(c.from_user.id)
    db=await connect()
    try:
        rows=await (await db.execute("SELECT ls.section,COUNT(DISTINCT q.link_id) c FROM join_queue q JOIN links l ON l.id=q.link_id JOIN link_sections ls ON ls.link_id=l.id WHERE q.operator_id=? AND l.category='whatsapp_group' GROUP BY ls.section",(op,))).fetchall()
    finally: await db.close()
    counts={r['section']:int(r['c']) for r in rows}
    await c.message.edit_text('➕ إدارة الانضمام إلى مجموعات WhatsApp\n\nاختر القسم، الحساب، العدد، ثم وضع الحماية. الوضع الآمن جدًا هو الافتراضي، لكن لا يوجد معدل آلي تضمنه WhatsApp. المنتهية والمهمشة تظهران للسجل فقط ولا يسمح بالانضمام منهما. وفي قسم القنوات، الانضمام يطبق فقط على روابط مجموعات WhatsApp؛ روابط قنوات WhatsApp تبقى محفوظة في القاعدة دون تمريرها لمحرك انضمام المجموعات.',reply_markup=ik([
        [InlineKeyboardButton(text=f"⭐ المهمة ({counts.get('important',0)})",callback_data='join_section:important')],
        [InlineKeyboardButton(text=f"🎓 الطلبة ({counts.get('students',0)})",callback_data='join_section:students')],
        [InlineKeyboardButton(text=f"📢 القنوات ({counts.get('channels',0)})",callback_data='join_section:channels')],
        [InlineKeyboardButton(text='⛔ المنتهية — مستبعدة',callback_data='join_blocked:expired'),InlineKeyboardButton(text='🗑 المهمشة — مستبعدة',callback_data='join_blocked:ignored')],
        [InlineKeyboardButton(text='➕ إضافة روابط يدويًا للمهمة',callback_data='join_add')],
        [InlineKeyboardButton(text='📡 مصادر Telegram',callback_data='tg_sources')],
        [InlineKeyboardButton(text='🔄 تحديث طلبات 48 ساعة',callback_data='join_recheck'),InlineKeyboardButton(text='📦 تقرير الانضمام',callback_data='join_export')],
        [InlineKeyboardButton(text='⬅️ الفحص والانضمام',callback_data='audit_join_hub')]
    ]))

@dp.callback_query(F.data.startswith('join_blocked:'))
async def join_blocked(c:CallbackQuery):
    sec=c.data.split(':',1)[1]
    label=TG_SECTIONS.get(sec,sec)
    await c.answer(f'{label} سجل استبعاد؛ لا يتم الانضمام إلى روابطه.',show_alert=True)

@dp.callback_query(F.data.startswith('join_section:'))
async def join_section(c:CallbackQuery,state:FSMContext):
    section=c.data.split(':',1)[1]
    if section not in JOINABLE_SECTIONS:return await c.answer('هذا القسم غير قابل للانضمام.',show_alert=True)
    db=await connect()
    try:
        rows=await (await db.execute("SELECT * FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id")).fetchall() if owner(c.from_user.id) else await (await db.execute("SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected' ORDER BY id",(c.from_user.id,))).fetchall()
    finally: await db.close()
    if not rows:return await c.answer('لا توجد حسابات WhatsApp متصلة.',show_alert=True)
    await state.update_data(join_section=section)
    kb=[]
    for a in rows:
        suffix=f" — {a['phone_hint']}" if a['phone_hint'] else ''
        kb.append([InlineKeyboardButton(text=f"📱 #{a['id']} {a['label']}{suffix}",callback_data=f"join_account:{a['id']}")])
    kb.append([InlineKeyboardButton(text='🌐 جميع الحسابات المتصلة',callback_data='join_account:0')])
    kb.append([InlineKeyboardButton(text='⬅️ الانضمام',callback_data='joinq')])
    await c.message.edit_text(f"القسم: {TG_SECTIONS.get(section,section)}\nاختر الحساب الذي سينفذ الانضمام:",reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('join_account:'))
async def join_account(c:CallbackQuery,state:FSMContext):
    slot=int(c.data.split(':',1)[1]); d=await state.get_data(); section=d.get('join_section')
    if section not in JOINABLE_SECTIONS:return await c.answer('ابدأ باختيار القسم.',show_alert=True)
    if slot:
        r=await get_slot(c.from_user.id,slot)
        if not r or r['health']!='connected':return await c.answer('الحساب غير متصل أو غير متاح.',show_alert=True)
    await state.update_data(join_account_slot=slot)
    await c.message.edit_text(f"القسم: {TG_SECTIONS.get(section,section)}\nالحساب: {'جميع الحسابات' if slot==0 else '#'+str(slot)}\n\nحدد عدد الروابط:",reply_markup=ik([
        [InlineKeyboardButton(text='▶️ كل روابط القسم',callback_data='join_limit_choice:0')],
        [InlineKeyboardButton(text='200',callback_data='join_limit_choice:200'),InlineKeyboardButton(text='500',callback_data='join_limit_choice:500')],
        [InlineKeyboardButton(text='1000',callback_data='join_limit_choice:1000'),InlineKeyboardButton(text='🔢 عدد آخر',callback_data='join_limit_choice:custom')],
        [InlineKeyboardButton(text='⬅️ اختيار القسم',callback_data=f'join_section:{section}')]
    ]))

@dp.callback_query(F.data.startswith('join_limit_choice:'))
async def join_limit_choice(c:CallbackQuery,state:FSMContext):
    v=c.data.split(':',1)[1]
    if v=='custom':
        await state.set_state(S.join_limit); return await c.message.answer('أرسل عدد الروابط المطلوب لهذا الحساب/لكل حساب.',reply_markup=cancel_kb())
    await state.update_data(join_limit=int(v)); await _join_profile_menu(c.message,state)

async def _join_profile_menu(message:Message,state:FSMContext):
    await state.set_state(None)
    await message.answer(
        'اختر مستوى أمان الانضمام. لا يوجد معدل آلي تضمنه WhatsApp؛ هذه حدود محافظة داخل البوت:\n\n'
        '🛡 آمن جدًا: 10 محاولات/24س لكل حساب، 60–120ث عشوائيًا، 5 روابط ثم 90د راحة.\n'
        '⚖️ متوازن: إعدادك السابق كأساس — 30–60ث، 10 روابط ثم 60د، وحد 20/24س.\n'
        '⚙️ مخصص: تختار القيم، مع حد يومي وقاطع تقييد إجباريين.',
        reply_markup=ik([
            [InlineKeyboardButton(text='🛡 آمن جدًا — موصى به',callback_data='join_profile:very_safe')],
            [InlineKeyboardButton(text='⚖️ متوازن — إعدادك السابق',callback_data='join_profile:balanced')],
            [InlineKeyboardButton(text='⚙️ مخصص مع حماية',callback_data='join_profile:custom')],
            [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')],
        ]),
    )

@dp.callback_query(F.data.startswith('join_profile:'))
async def join_profile_choice(c:CallbackQuery,state:FSMContext):
    profile=c.data.split(':',1)[1]
    if profile not in PROFILE_LABELS:return await c.answer('وضع غير معروف.',show_alert=True)
    await state.update_data(join_safety_profile=profile)
    if profile=='custom':
        await state.set_state(S.join_delay)
        return await c.message.answer('أرسل الفاصل الأساسي بالثواني (الحد الأدنى الإجباري 15 ثانية).',reply_markup=cancel_kb())
    await _create_join_task(c.message,state,c.from_user.id,profile)

@dp.callback_query(F.data=='join_add')
async def join_add(c:CallbackQuery,state:FSMContext):
    await state.update_data(join_manual_section='important'); await state.set_state(S.join_paste)
    await c.message.answer('ألصق روابط مجموعات WhatsApp. ستُضاف إلى قسم الروابط المهمة.',reply_markup=cancel_kb())

@dp.message(S.join_paste)
async def join_paste(m:Message,state:FSMContext):
    op=scope_uid(m.from_user.id); added=0
    for u in extract_urls(m.text or ''):
        n=normalize_url(u)
        if classify_link(n)!='whatsapp_group':continue
        _,lid=await upsert_link(u,n,'whatsapp_group',op,'join_queue',section='important')
        if lid:
            db=await connect()
            try:
                cur=await db.execute("INSERT OR IGNORE INTO join_queue(operator_id,link_id,status,created_at,updated_at) VALUES(?,?, 'pending',?,?)",(op,lid,now_iso(),now_iso())); added+=max(0,cur.rowcount or 0); await db.commit()
            finally:await db.close()
    await state.clear(); await m.answer(f'تمت إضافة {added} رابط جديد إلى قسم المهمة وطابور الانضمام.',reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data=='join_recheck')
async def join_recheck(c:CallbackQuery):
    from .services.join_worker import recheck_pending
    n=await recheck_pending(scope_uid(c.from_user.id)); await c.answer(f'تم تحديث {n} حالة.',show_alert=True)

@dp.callback_query(F.data=='join_export')
async def join_export(c:CallbackQuery):
    await c.message.answer_document(FSInputFile(await export_join_zip(scope_uid(c.from_user.id))),caption='تقرير الانضمام حسب الحساب والحالة')

@dp.message(S.join_limit)
async def join_limit_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1:return await m.answer('العدد يجب أن يكون 1 أو أكثر.',reply_markup=cancel_kb())
    await state.update_data(join_limit=n); await _join_profile_menu(m,state)

@dp.message(S.join_delay)
async def join_delay_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل عدد الثواني كرقم.',reply_markup=cancel_kb())
    if n<15:return await m.answer('وضع الحماية المخصص لا يسمح بأقل من 15 ثانية.',reply_markup=cancel_kb())
    await state.update_data(join_item_delay=n); await state.set_state(S.join_batch_size)
    await m.answer('بعد كم رابط تريد اعتبار الدفعة مكتملة؟ مثال: 10.',reply_markup=cancel_kb())

@dp.message(S.join_batch_size)
async def join_batch_size_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1 or n>20:return await m.answer('حجم الدفعة يجب أن يكون بين 1 و20.',reply_markup=cancel_kb())
    await state.update_data(join_batch_size=n); await state.set_state(S.join_batch_rest)
    await m.answer('أرسل مدة الراحة بين الدفعات بالدقائق. 0 مسموح.',reply_markup=cancel_kb())

@dp.message(S.join_batch_rest)
async def join_batch_rest_msg(m:Message,state:FSMContext):
    try:minutes=int((m.text or '').strip())
    except:return await m.answer('أرسل عدد الدقائق كرقم.',reply_markup=cancel_kb())
    if minutes<5:return await m.answer('وضع الحماية المخصص يتطلب راحة لا تقل عن 5 دقائق.',reply_markup=cancel_kb())
    await state.update_data(join_batch_rest=minutes*60)
    await _create_join_task(m,state,m.from_user.id,'custom')

async def _create_join_task(message:Message,state:FSMContext,actor_uid:int,profile:str):
    d=await state.get_data(); await state.clear(); op=scope_uid(actor_uid)
    policy=profile_settings(profile,item_delay=d.get('join_item_delay'),batch_size=d.get('join_batch_size'),batch_rest=d.get('join_batch_rest'))
    payload={'section':d.get('join_section','important'),'account_slot_id':int(d.get('join_account_slot',0) or 0),'per_account_limit':int(d.get('join_limit',0)),'safety_profile':profile,'item_delay':policy['min_delay'],'max_delay':policy['max_delay'],'batch_size':policy['batch_size'],'batch_rest':policy['batch_rest'],'daily_limit':policy['daily_limit']}
    job_id=await create_job(op,'join',payload)
    lim=payload['per_account_limit']; label='كل روابط القسم' if lim==0 else str(lim); acct='كل الحسابات' if not payload['account_slot_id'] else f"#{payload['account_slot_id']}"
    await message.answer(f"✅ تم إنشاء مهمة الانضمام #{job_id}\nالقسم: {TG_SECTIONS.get(payload['section'],payload['section'])}\nالحساب: {acct}\nالعدد المطلوب: {label}\nالوضع: {PROFILE_LABELS[profile]}\nالحد اليومي/حساب: {policy['daily_limit']}\nالفاصل العشوائي: {policy['min_delay']}–{policy['max_delay']}ث\nالدفعة: {policy['batch_size']}\nالراحة: {policy['batch_rest']//60}د",reply_markup=menu(actor_uid))
    _spawn(_join_notify(op,message.chat.id,payload,job_id,actor_uid))

async def _join_notify(uid,chat,payload,job_id,actor_uid=None):
    actor_uid=actor_uid or uid
    pre=await job_signal(job_id)
    if pre:
        await set_job(job_id,'paused' if pre=='pause_requested' else 'cancelled',{}); return
    await set_job(job_id,'running')
    try:
        lim=int(payload.get('per_account_limit') or 0)
        rep=await process_operator(uid,None if lim==0 else lim,job_id=job_id,item_delay=payload.get('item_delay'),batch_size=payload.get('batch_size'),batch_rest=payload.get('batch_rest'),account_slot_id=int(payload.get('account_slot_id') or 0) or None,section=payload.get('section','important'),safety_profile=payload.get('safety_profile','very_safe'))
        status='paused' if rep.get('paused') else ('cancelled' if rep.get('cancelled') else ('failed' if rep.get('error') else ('paused_rate_limit' if rep.get('rate_limited') or rep.get('daily_limit_reached') else 'completed')))
        await set_job(job_id,status,rep)
        if rep.get('rate_limited') or rep.get('daily_limit_reached'):
            await emit_alert(bot,settings.owner_id,'join_safety_circuit',f'توقفت مهمة الانضمام #{job_id} للحماية',json.dumps({'rate_limited':rep.get('rate_limited'),'daily_limit_reached':rep.get('daily_limit_reached'),'accounts':rep.get('accounts')},ensure_ascii=False)[:3000],severity='critical' if rep.get('rate_limited') else 'warning',dedupe_key=f'join_job:{job_id}')
        totals=rep.get('totals') or {}; processed=int(totals.get('processed',0)); failed=int(totals.get('failed',0))
        if processed>=3 and failed/processed>=0.5:
            await emit_alert(bot,settings.owner_id,'high_failure_rate',f'ارتفاع فشل مهمة الانضمام #{job_id}',f'الفشل: {failed}/{processed}',severity='warning',dedupe_key=f'join_fail:{job_id}')
        verb='تم إيقافها للحماية' if status=='paused_rate_limit' else ('تم إيقافها' if status=='paused' else ('تم إلغاؤها' if status=='cancelled' else ('فشلت' if status=='failed' else 'اكتملت')))
        await bot.send_message(chat,f"{verb} مهمة الانضمام #{job_id}:\n"+json.dumps(rep,ensure_ascii=False,indent=2)[:3300],reply_markup=menu(actor_uid))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)}); await bot.send_message(chat,f'فشلت مهمة الانضمام #{job_id}: {e}',reply_markup=menu(actor_uid))

@dp.callback_query(F.data=='database')
async def database(c:CallbackQuery):
    s=await stats(); rows=[]
    if await has_any_permission(c.from_user.id,{'links'}): rows.append([InlineKeyboardButton(text='📦 تصدير ZIP',callback_data='export_all')])
    rows.append([InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')])
    sec_lines='\n'.join(f"{TG_SECTIONS.get(k,k)}: {v}" for k,v in s.get('by_section',{}).items()) or '-'
    type_lines='\n'.join(f'{k}: {v}' for k,v in s['by_category'].items()) or '-'
    await c.message.edit_text(f"إجمالي الروابط الفريدة: {s['total']}\nالمنتهية المحظورة: {s['expired']}\nالمهمشة المحظورة: {s['ignored']}\n\n📂 الأقسام:\n{sec_lines}\n\n🔗 أنواع الروابط:\n{type_lines}",reply_markup=ik(rows))
@dp.callback_query(F.data=='export_all')
async def export_all(c):
    await c.message.answer_document(FSInputFile(await export_links_zip()),caption='تصدير قاعدة الروابط')

@dp.callback_query(F.data=='dashboard')
async def dashboard(c):
    s=await stats(); db=await connect()
    try:
        a=(await (await db.execute('SELECT COUNT(*) c FROM account_slots' if owner(c.from_user.id) else 'SELECT COUNT(*) c FROM account_slots WHERE operator_id=?',() if owner(c.from_user.id) else (c.from_user.id,))).fetchone())['c']
        con=(await (await db.execute("SELECT COUNT(*) c FROM account_slots WHERE health='connected'" if owner(c.from_user.id) else "SELECT COUNT(*) c FROM account_slots WHERE operator_id=? AND health='connected'",() if owner(c.from_user.id) else (c.from_user.id,))).fetchone())['c']
        row=await (await db.execute("""SELECT COUNT(*) msgs,
            SUM(CASE WHEN length(trim(text))>0 THEN 1 ELSE 0 END) text_msgs,
            SUM(CASE WHEN text LIKE '%http://%' OR text LIKE '%https://%' OR text LIKE '%www.%' OR text LIKE '%chat.whatsapp.com/%' OR text LIKE '%wa.me/%' OR text LIKE '%whatsapp.com/channel/%' THEN 1 ELSE 0 END) url_msgs
            FROM wa_messages WHERE account_slot_id IN (SELECT id FROM account_slots WHERE operator_id=? OR ?=1)""",(c.from_user.id,1 if owner(c.from_user.id) else 0))).fetchone()
        active=(await (await db.execute("SELECT COUNT(*) c FROM jobs WHERE operator_id=? AND status IN ('queued','running','cancel_requested','pause_requested')",(scope_uid(c.from_user.id),))).fetchone())['c']
        scheduled=(await (await db.execute("SELECT COUNT(*) c FROM scheduled_tasks WHERE owner_id=? AND status='scheduled'",(scope_uid(c.from_user.id),))).fetchone())['c']
        open_errors=(await (await db.execute("SELECT COUNT(*) c FROM system_errors WHERE resolved=0")).fetchone())['c']
        followups=(await (await db.execute("SELECT COUNT(*) c FROM chat_metadata WHERE owner_id=? AND status IN ('followup','important')",(scope_uid(c.from_user.id),))).fetchone())['c']
    finally:await db.close()
    msgs=int(row['msgs'] or 0); text_msgs=int(row['text_msgs'] or 0); url_msgs=int(row['url_msgs'] or 0)
    lines=[
        '🚀 لوحة التحكم V2.8',f'الحسابات: {a}',f'المتصلة: {con}',
        f'رسائل WhatsApp المتزامنة محليًا: {msgs}',f'رسائل ذات نص قابل للقراءة: {text_msgs}',
        f'رسائل تحتوي URL: {url_msgs}',f'مهام التشغيل النشطة: {active}',f'المهام المجدولة: {scheduled}',
        f'محادثات تحتاج متابعة: {followups}',f'أخطاء مفتوحة: {open_errors}',
        f'إجمالي الروابط العالمية: {s["total"]}',f'المنتهية: {s["expired"]}',f'المهمشة: {s["ignored"]}'
    ]
    if msgs>0 and text_msgs==0:
        lines += ['', '⚠️ السجل يحتوي رسائل لكن نصوصها فارغة. استخدم من الحساب: ♻️ إعادة مزامنة السجل بالـQR.']
    await c.message.edit_text('\n'.join(lines),reply_markup=ik([
        [InlineKeyboardButton(text='🩺 صحة الحسابات',callback_data='account_health_all'),InlineKeyboardButton(text='📥 المتابعات',callback_data='inbox')],
        [InlineKeyboardButton(text='⏰ المجدولة',callback_data='scheduled_tasks'),InlineKeyboardButton(text='⚠️ الأخطاء',callback_data='error_center')],
        [InlineKeyboardButton(text='📊 تقرير جميع الحسابات',callback_data='accounts_report')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]))

# ---------------- V2.6 Task Center / health / system administration ----------------

def _scheduled_options(prefix:str):
    return ik([
        [InlineKeyboardButton(text='بعد 5 دقائق',callback_data=f'{prefix}:5:0'),InlineKeyboardButton(text='بعد 30 دقيقة',callback_data=f'{prefix}:30:0')],
        [InlineKeyboardButton(text='بعد ساعة',callback_data=f'{prefix}:60:0'),InlineKeyboardButton(text='بعد 24 ساعة',callback_data=f'{prefix}:1440:0')],
        [InlineKeyboardButton(text='يوميًا بدءًا بعد 24 ساعة',callback_data=f'{prefix}:1440:1440')],
        [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')],
    ])

@dp.callback_query(F.data=='scheduled_tasks')
async def scheduled_tasks(c:CallbackQuery):
    rows=await list_scheduled_tasks(scope_uid(c.from_user.id),30)
    labels={'reminder':'تذكير','backup':'نسخة احتياطية آمنة','diagnostic':'تشخيص','health_check':'فحص صحة الحسابات','message_archive':'أرشفة الرسائل القديمة'}
    lines=['⏰ المهام المجدولة — آخر 30 مهمة:']; kb=[]
    if not rows: lines.append('لا توجد مهام مجدولة.')
    for r in rows:
        repeat=f" | كل {r['recurrence_minutes']} دقيقة" if int(r['recurrence_minutes'] or 0)>0 else ''
        lines.append(f"#{r['id']} {labels.get(r['action'],r['action'])} — {r['status']} — {r['run_at']}{repeat}")
        if r['status'] in {'scheduled','failed'}:
            kb.append([InlineKeyboardButton(text=f"⛔ إلغاء #{r['id']}",callback_data=f"sched_cancel:{r['id']}")])
    kb += [[InlineKeyboardButton(text='➕ مهمة مجدولة',callback_data='scheduled_add')],[InlineKeyboardButton(text='🔄 تحديث',callback_data='scheduled_tasks'),InlineKeyboardButton(text='⬅️ المهام',callback_data='tasks_hub')]]
    await c.message.edit_text('\n'.join(lines)[:3600],reply_markup=ik(kb))

@dp.callback_query(F.data=='scheduled_add')
async def scheduled_add(c:CallbackQuery,state:FSMContext):
    await state.clear()
    await c.message.edit_text('اختر نوع المهمة المجدولة. الجدولة الإدارية لا تتجاوز قيود WhatsApp ولا تبدأ حملات تلقائية.',reply_markup=ik([
        [InlineKeyboardButton(text='⏰ تذكير',callback_data='sched_action:reminder'),InlineKeyboardButton(text='💾 نسخة آمنة',callback_data='sched_action:backup')],
        [InlineKeyboardButton(text='🩺 صحة الحسابات',callback_data='sched_action:health_check'),InlineKeyboardButton(text='🧰 تشخيص',callback_data='sched_action:diagnostic')],
        [InlineKeyboardButton(text='🗜 أرشفة الرسائل القديمة',callback_data='sched_action:message_archive')],
        [InlineKeyboardButton(text='⬅️ المهام المجدولة',callback_data='scheduled_tasks')],
    ]))

@dp.callback_query(F.data.startswith('sched_action:'))
async def sched_action(c:CallbackQuery,state:FSMContext):
    action=c.data.split(':',1)[1]
    if action=='reminder':
        await state.set_state(S.scheduled_reminder)
        return await c.message.answer('أرسل نص التذكير.',reply_markup=cancel_kb())
    titles={'backup':'نسخة احتياطية آمنة','diagnostic':'تقرير تشخيص','health_check':'فحص صحة الحسابات','message_archive':'أرشفة الرسائل القديمة'}
    if action not in titles:return await c.answer('نوع غير مدعوم.',show_alert=True)
    await c.message.edit_text(f"حدد موعد {titles[action]}:",reply_markup=_scheduled_options(f'sched_create:{action}'))

@dp.message(S.scheduled_reminder)
async def scheduled_reminder_text(m:Message,state:FSMContext):
    text=(m.text or '').strip()
    if not text:return await m.answer('أرسل نصًا صالحًا.',reply_markup=cancel_kb())
    await state.update_data(scheduled_reminder_text=text[:1000]); await state.set_state(None)
    await m.answer('حدد موعد التذكير:',reply_markup=_scheduled_options('sched_rem_create'))

@dp.callback_query(F.data.startswith('sched_rem_create:'))
async def sched_rem_create(c:CallbackQuery,state:FSMContext):
    _,delay_s,repeat_s=c.data.split(':'); d=await state.get_data(); text=d.get('scheduled_reminder_text')
    if not text:return await c.answer('نص التذكير غير موجود. ابدأ من جديد.',show_alert=True)
    tid=await create_scheduled_task(scope_uid(c.from_user.id),'reminder','تذكير إداري',int(delay_s),{'text':text},int(repeat_s))
    await state.clear(); await c.answer(f'تم إنشاء المهمة #{tid}.',show_alert=True); await scheduled_tasks(c)

@dp.callback_query(F.data.startswith('sched_create:'))
async def sched_create(c:CallbackQuery):
    _,action,delay_s,repeat_s=c.data.split(':')
    titles={'backup':'نسخة احتياطية آمنة','diagnostic':'تقرير تشخيص','health_check':'فحص صحة الحسابات','message_archive':'أرشفة الرسائل القديمة'}
    if action not in titles:return await c.answer('نوع غير مدعوم.',show_alert=True)
    tid=await create_scheduled_task(scope_uid(c.from_user.id),action,titles[action],int(delay_s),{},int(repeat_s))
    await c.answer(f'تم إنشاء المهمة #{tid}.',show_alert=True); await scheduled_tasks(c)

@dp.callback_query(F.data.startswith('sched_cancel:'))
async def sched_cancel(c:CallbackQuery):
    tid=int(c.data.split(':')[1]); ok=await cancel_scheduled_task(scope_uid(c.from_user.id),tid)
    await c.answer('تم إلغاء المهمة.' if ok else 'لا يمكن إلغاء هذه المهمة.',show_alert=True); await scheduled_tasks(c)

@dp.callback_query(F.data=='account_health_all')
async def account_health_all(c:CallbackQuery):
    await c.answer('جارٍ فحص الحسابات…')
    rows=await check_account_health(None if owner(c.from_user.id) else c.from_user.id)
    lines=['🩺 صحة حسابات WhatsApp']
    if not rows:lines.append('لا توجد حسابات مفعلة.')
    for r in rows[:50]:
        icon='🟢' if r['score']>=90 else ('🟡' if r['score']>=50 else '🔴')
        lines.append(f"{icon} #{r['id']} {r['label']} — {r['health']} — {r['score']}%")
    await log_admin_event(c.from_user.id,'account_health_check','accounts',None,{'count':len(rows)})
    await c.message.edit_text('\n'.join(lines)[:3900],reply_markup=ik([[InlineKeyboardButton(text='🔄 إعادة الفحص',callback_data='account_health_all')],[InlineKeyboardButton(text='⬅️ المهام',callback_data='tasks_hub')]]))

@dp.callback_query(F.data=='diagnostics')
async def diagnostics(c:CallbackQuery):
    path=await write_diagnostics_file(); await log_admin_event(c.from_user.id,'diagnostics_created','system')
    await c.message.answer_document(FSInputFile(path),caption='🧰 تقرير تشخيص — الأسرار مستبعدة عمدًا')

@dp.callback_query(F.data=='db_health')
async def db_health(c:CallbackQuery):
    h=await database_health(); mb=h['bytes']/1024/1024
    await c.message.edit_text(f"🗄 فحص قاعدة البيانات\nIntegrity: {h['integrity']}\nالجداول: {h['tables']}\nالحجم المنطقي: {mb:.2f} MB",reply_markup=ik([[InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')]]))

@dp.callback_query(F.data=='alerts_center')
async def alerts_center(c:CallbackQuery):
    rules=await alert_rules(settings.owner_id); recent=await recent_alerts(settings.owner_id,10)
    lines=['🔔 التنبيهات الذكية','', 'يمنع التكرار خلال فترة التهدئة ويرسل التنبيه إلى المالك.']
    kb=[]
    for rule in rules:
        mark='✅' if rule['enabled'] else '⛔'
        lines.append(f"{mark} {ALERT_TYPES.get(rule['event_type'],rule['event_type'])} — تهدئة {rule['cooldown_minutes']}د")
        kb.append([InlineKeyboardButton(text=f"{mark} تبديل: {ALERT_TYPES.get(rule['event_type'],rule['event_type'])}",callback_data=f"alert_toggle:{rule['event_type']}")])
    lines.append('\nآخر التنبيهات:')
    if not recent: lines.append('لا توجد تنبيهات بعد.')
    for row in recent:
        lines.append(f"#{row['id']} {row['title']} — {row['created_at']}")
    kb.append([InlineKeyboardButton(text='🔄 تحديث',callback_data='alerts_center'),InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')])
    await c.message.edit_text('\n'.join(lines)[:3900],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('alert_toggle:'))
async def alert_toggle(c:CallbackQuery):
    event_type=c.data.split(':',1)[1]
    try: enabled=await toggle_alert_rule(settings.owner_id,event_type)
    except ValueError: return await c.answer('نوع تنبيه غير معروف.',show_alert=True)
    await log_admin_event(c.from_user.id,'alert_rule_toggled','alert_rule',event_type,{'enabled':enabled})
    await c.answer('تم تفعيل التنبيه.' if enabled else 'تم إيقاف التنبيه.',show_alert=True)
    await alerts_center(c)

@dp.callback_query(F.data=='message_archive')
async def message_archive(c:CallbackQuery):
    status=await archive_status(); original=status['original_bytes']; compressed=status['compressed_bytes']
    saved=max(0,original-compressed); ratio=(compressed/original*100) if original else 0
    last=status.get('last_run') or {}
    text=(f"🗜 أرشفة الرسائل القديمة\n\nالرسائل النشطة: {status['active']}\nالمؤرشفة القابلة للاستعادة: {status['archived']}\n"
          f"الحجم قبل الضغط: {original/1024/1024:.2f} MB\nالحجم المضغوط: {compressed/1024/1024:.2f} MB ({ratio:.1f}%)\n"
          f"المساحة المنطقية الموفرّة: {saved/1024/1024:.2f} MB\nآخر تشغيل: {last.get('completed_at') or 'لم تعمل بعد'}\n\n"
          f"تُنقل فقط الرسائل الأقدم من {settings.message_retention_days} يومًا والتي تجاوزها مؤشر تجميع أو مراقبة. لا يوجد حذف نهائي.")
    await c.message.edit_text(text,reply_markup=ik([
        [InlineKeyboardButton(text='▶️ أرشفة دفعة الآن',callback_data='archive_run')],
        [InlineKeyboardButton(text='↩️ استعادة آخر 5000 رسالة مؤرشفة',callback_data='archive_restore')],
        [InlineKeyboardButton(text='⏰ جدولة أرشفة يومية',callback_data='sched_create:message_archive:1440:1440')],
        [InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')],
    ]))

@dp.callback_query(F.data=='archive_run')
async def archive_run(c:CallbackQuery):
    payload={'retention_days':settings.message_retention_days,'batch_limit':settings.message_archive_batch}
    job_id=await create_job(scope_uid(c.from_user.id),'message_archive',payload)
    await c.answer(f'بدأت مهمة الأرشفة #{job_id}.',show_alert=True)
    _spawn(_archive_notify(job_id,c.message.chat.id,c.from_user.id))

async def _archive_notify(job_id:int,chat_id:int,actor_uid:int):
    await set_job(job_id,'running')
    try:
        report=await archive_old_messages(scope_uid(actor_uid))
        await set_job(job_id,'completed',report)
        await bot.send_message(chat_id,f"✅ اكتملت أرشفة الرسائل #{job_id}\nنُقلت: {report['moved']}\nالحجم المضغوط: {report['compressed_percent']}% من الأصل\nالأرشيف قابل للاستعادة ولم يُحذف نهائيًا.",reply_markup=menu(actor_uid))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)})
        await bot.send_message(chat_id,f'فشلت مهمة الأرشفة #{job_id}: {e}',reply_markup=menu(actor_uid))

@dp.callback_query(F.data=='archive_restore')
async def archive_restore(c:CallbackQuery):
    job_id=await create_job(scope_uid(c.from_user.id),'message_restore',{'limit':5000})
    await c.answer(f'بدأت مهمة الاستعادة #{job_id}.',show_alert=True)
    _spawn(_archive_restore_notify(job_id,c.message.chat.id,c.from_user.id))

async def _archive_restore_notify(job_id:int,chat_id:int,actor_uid:int):
    await set_job(job_id,'running')
    try:
        report=await restore_archived_messages(5000)
        await set_job(job_id,'completed',report)
        await bot.send_message(chat_id,f"✅ اكتملت استعادة الأرشيف #{job_id}\nأُعيدت إلى جدول الرسائل النشط: {report['restored']}",reply_markup=menu(actor_uid))
    except Exception as e:
        await set_job(job_id,'failed',{'error':str(e)})
        await bot.send_message(chat_id,f'فشلت استعادة الأرشيف #{job_id}: {e}',reply_markup=menu(actor_uid))

@dp.callback_query(F.data=='local_full_backup')
async def local_full_backup(c:CallbackQuery):
    path=await create_local_full_backup(); await log_admin_event(c.from_user.id,'local_full_backup_created','system')
    await c.message.answer(f"📦 تم إنشاء نسخة استعادة محلية كاملة:\n{path}\n\n⚠️ قد تحتوي هذه النسخة جلسات حساسة. لا ترسلها لأي شخص. لم يرسلها البوت عبر Telegram.",reply_markup=ik([[InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')]]))

@dp.callback_query(F.data=='error_center')
async def error_center(c:CallbackQuery):
    rows=await recent_errors(20,False); lines=['⚠️ مركز الأخطاء — آخر 20:']; kb=[]
    if not rows:lines.append('لا توجد أخطاء مسجلة.')
    for r in rows:
        mark='✅' if r['resolved'] else '🔴'
        lines.append(f"{mark} #{r['id']} [{r['source']}] {r['message'][:120]} — {r['created_at']}")
        if not r['resolved']:kb.append([InlineKeyboardButton(text=f"✅ إغلاق الخطأ #{r['id']}",callback_data=f"error_resolve:{r['id']}")])
    kb.append([InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')])
    await c.message.edit_text('\n'.join(lines)[:3600],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('error_resolve:'))
async def error_resolve(c:CallbackQuery):
    eid=int(c.data.split(':')[1]); await resolve_error(eid); await log_admin_event(c.from_user.id,'error_resolved','system_error',eid)
    await c.answer('تم إغلاق الخطأ.'); await error_center(c)

@dp.callback_query(F.data=='admin_audit')
async def admin_audit(c:CallbackQuery):
    rows=await recent_admin_events(30); lines=['🧾 سجل الإدارة — آخر 30 حدثًا:']
    if not rows:lines.append('لا توجد أحداث مسجلة بعد.')
    for r in rows:
        entity=(f" {r['entity_type']}#{r['entity_id']}" if r['entity_type'] else '')
        lines.append(f"#{r['id']} user={r['actor_id']} — {r['event_type']}{entity} — {r['created_at']}")
    await c.message.edit_text('\n'.join(lines)[:3900],reply_markup=ik([[InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')]]))

@dp.callback_query(F.data=='supervisors')
async def supervisors(c):
    if not real_owner(c.from_user.id): return await c.answer('إدارة المشرفين للمالك فقط.',show_alert=True)
    db=await connect()
    try: rows=await (await db.execute('SELECT user_id,role,enabled,created_at FROM supervisors ORDER BY created_at DESC')).fetchall()
    finally: await db.close()
    lines=['👥 إدارة المشرفين','', '⭐ المشرف الرئيسي: تحكم كامل بالبوت عدا إدارة المشرفين.', '👤 المشرف العادي: الحسابات + المنتهية + المهمشة افتراضيًا.', '🛡 المشرف المخصص: تمنح أو تسحب كل صلاحية منفردة.']
    perm_buttons=[]
    for r in rows[:30]:
        role='مشرف رئيسي' if r['role']=='principal' else ('مشرف مخصص' if r['role']=='custom' else 'مشرف عادي')
        lines.append(f"{r['user_id']} — {role} — {'فعال' if r['enabled'] else 'متوقف'}")
        if r['enabled'] and r['role']!='principal':
            perm_buttons.append([InlineKeyboardButton(text=f"🛡 صلاحيات {r['user_id']}",callback_data=f"sup_perm_menu:{r['user_id']}")])
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik([
        [InlineKeyboardButton(text='➕ إضافة مشرف عادي',callback_data='sup_add')],
        [InlineKeyboardButton(text='⭐ إضافة مشرف رئيسي',callback_data='sup_add_principal')],
        *perm_buttons,
        [InlineKeyboardButton(text='➖ حذف/تعطيل مشرف',callback_data='sup_del')],
        [InlineKeyboardButton(text='⬅️ الإدارة والنظام',callback_data='system_hub')],
    ]))
@dp.callback_query(F.data=='sup_add')
async def sup_add(c,state):
    if not real_owner(c.from_user.id): return await c.answer('للمالك فقط',show_alert=True)
    await state.set_state(S.add_sup); await c.message.answer('أرسل Telegram User ID للمشرف العادي.',reply_markup=cancel_kb())
@dp.message(S.add_sup)
async def sup_add_msg(m,state):
    if not real_owner(m.from_user.id):return
    try:uid=int((m.text or '').strip())
    except:return await m.answer('ID غير صالح')
    await add_supervisor(uid,'registry'); PRINCIPAL_IDS.discard(uid); CUSTOM_IDS.discard(uid); await state.clear(); await m.answer('تمت إضافة المشرف العادي. يمكنك الآن فتح إدارة المشرفين وتخصيص صلاحياته واحدةً واحدة.',reply_markup=back())
@dp.callback_query(F.data=='sup_add_principal')
async def sup_add_principal(c,state):
    if not real_owner(c.from_user.id): return await c.answer('للمالك فقط',show_alert=True)
    await state.set_state(S.add_principal); await c.message.answer('أرسل Telegram User ID للمشرف الرئيسي. سيملك صلاحيات البوت كاملة عدا إدارة المشرفين.',reply_markup=cancel_kb())
@dp.message(S.add_principal)
async def sup_add_principal_msg(m,state):
    if not real_owner(m.from_user.id):return
    try:uid=int((m.text or '').strip())
    except:return await m.answer('ID غير صالح')
    if uid==settings.owner_id:return await m.answer('هذا هو حساب المالك أصلًا.',reply_markup=back())
    await add_supervisor(uid,'principal'); PRINCIPAL_IDS.add(uid); CUSTOM_IDS.discard(uid); await state.clear(); await m.answer('⭐ تم تعيين المشرف الرئيسي. لديه تحكم كامل بالبوت عدا قسم إدارة المشرفين.',reply_markup=back())
@dp.callback_query(F.data=='sup_del')
async def sup_del(c,state):
    if not real_owner(c.from_user.id): return await c.answer('للمالك فقط',show_alert=True)
    await state.set_state(S.del_sup); await c.message.answer('أرسل Telegram User ID المراد تعطيله.',reply_markup=cancel_kb())
@dp.message(S.del_sup)
async def sup_del_msg(m,state):
    if not real_owner(m.from_user.id):return
    try:uid=int((m.text or '').strip())
    except:return await m.answer('ID غير صالح')
    await remove_supervisor(uid); PRINCIPAL_IDS.discard(uid); CUSTOM_IDS.discard(uid); await state.clear(); await m.answer('تم تعطيل المشرف وحساباته.',reply_markup=back())

@dp.callback_query(F.data.startswith('sup_perm_menu:'))
async def sup_perm_menu(c:CallbackQuery):
    if not real_owner(c.from_user.id):return await c.answer('للمالك فقط',show_alert=True)
    uid=int(c.data.split(':',1)[1]); perms=await effective_permissions(uid)
    db=await connect()
    try: row=await (await db.execute('SELECT role,enabled FROM supervisors WHERE user_id=?',(uid,))).fetchone()
    finally: await db.close()
    if not row or not row['enabled']:return await c.answer('المشرف غير موجود أو متوقف.',show_alert=True)
    if row['role']=='principal':return await c.answer('صلاحيات المشرف الرئيسي ثابتة وكاملة.',show_alert=True)
    lines=[f'🛡 صلاحيات المشرف {uid}','اضغط على أي صلاحية لتفعيلها أو سحبها.']
    kb=[]
    for code,label in PERMISSIONS.items():
        mark='✅' if code in perms else '⛔'; lines.append(f'{mark} {label}')
        kb.append([InlineKeyboardButton(text=f'{mark} {label}',callback_data=f'sup_perm_toggle:{uid}:{code}')])
    kb.append([InlineKeyboardButton(text='⬅️ إدارة المشرفين',callback_data='supervisors')])
    await c.message.edit_text('\n'.join(lines),reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('sup_perm_toggle:'))
async def sup_perm_toggle(c:CallbackQuery):
    if not real_owner(c.from_user.id):return await c.answer('للمالك فقط',show_alert=True)
    _,uid_s,code=c.data.split(':',2); uid=int(uid_s)
    current=await effective_permissions(uid)
    try: await set_permission(uid,code,code not in current)
    except ValueError as e:return await c.answer(str(e),show_alert=True)
    CUSTOM_IDS.add(uid); PRINCIPAL_IDS.discard(uid)
    await log_admin_event(c.from_user.id,'supervisor_permission_changed','supervisor',uid,{'permission':code,'enabled':code not in current})
    await c.answer('تم تحديث الصلاحية.',show_alert=True)
    await sup_perm_menu(c)

@dp.callback_query(F.data=='search')
async def search(c,state):
    await state.set_state(S.search)
    await c.message.answer('🔎 البحث الشامل V2.6\n\nأرسل رابطًا أو كلمة أو JID أو اسم مصدر أو جزءًا من رسالة. سيبحث البوت في الروابط والمحادثات المحلية والمهام ومصادر Telegram.',reply_markup=cancel_kb())

@dp.message(S.search)
async def search_msg(m,state):
    raw=(m.text or '').strip()
    if not raw:return await m.answer('أرسل قيمة للبحث.',reply_markup=cancel_kb())
    op=scope_uid(m.from_user.id); like=f"%{raw[:160]}%"; n=normalize_url(raw)
    db=await connect(); sections=[]
    try:
        if n:
            lr=await (await db.execute('SELECT * FROM links WHERE normalized_url=?',(n,))).fetchone()
            if lr:
                secs=await (await db.execute('SELECT section FROM link_sections WHERE link_id=? ORDER BY section',(lr['id'],))).fetchall()
                sec_text='، '.join(TG_SECTIONS.get(x['section'],x['section']) for x in secs) or '-'
                sections.append('🔗 رابط مطابق:\n'+f"#{lr['id']} {lr['original_url']}\nالأقسام: {sec_text}\nالاسم: {lr['display_name'] or '-'}\nالرصد: {lr['seen_count']}")
        lrows=await (await db.execute("SELECT id,original_url,display_name,category FROM links WHERE original_url LIKE ? OR COALESCE(display_name,'') LIKE ? ORDER BY id DESC LIMIT 5",(like,like))).fetchall()
        if lrows and not n:
            sections.append('🔗 الروابط:\n'+'\n'.join(f"#{r['id']} {r['display_name'] or r['category']} — {r['original_url'][:120]}" for r in lrows))
        crows=await (await db.execute("""SELECT m.id,m.account_slot_id,m.remote_jid,m.text,m.inserted_at,a.label
            FROM wa_messages m JOIN account_slots a ON a.id=m.account_slot_id
            WHERE (a.operator_id=? OR ?=1) AND (m.remote_jid LIKE ? OR m.text LIKE ?)
            ORDER BY m.id DESC LIMIT 5""",(m.from_user.id,1 if owner(m.from_user.id) else 0,like,like))).fetchall()
        if crows:sections.append('💬 محادثات/رسائل WhatsApp:\n'+'\n'.join(f"#{r['id']} {r['label']} | {r['remote_jid']} | {(r['text'] or '')[:90]}" for r in crows))
        jrows=await (await db.execute("SELECT id,kind,status,created_at FROM jobs WHERE operator_id=? AND (kind LIKE ? OR status LIKE ? OR CAST(id AS TEXT)=?) ORDER BY id DESC LIMIT 5",(op,like,like,raw))).fetchall()
        if jrows:sections.append('📋 المهام:\n'+'\n'.join(f"#{r['id']} {r['kind']} — {r['status']}" for r in jrows))
        trows=await (await db.execute("SELECT id,title,username,section FROM telegram_sources WHERE owner_id=? AND (COALESCE(title,'') LIKE ? OR COALESCE(username,'') LIKE ?) ORDER BY id DESC LIMIT 5",(op,like,like))).fetchall()
        if trows:sections.append('📡 مصادر Telegram:\n'+'\n'.join(f"#{r['id']} {r['title'] or '-'} @{r['username'] or '-'} — {r['section']}" for r in trows))
        mrows=await (await db.execute("SELECT account_slot_id,remote_jid,status,note FROM chat_metadata WHERE owner_id=? AND (remote_jid LIKE ? OR COALESCE(note,'') LIKE ?) ORDER BY updated_at DESC LIMIT 5",(op,like,like))).fetchall()
        if mrows:sections.append('📝 متابعات WhatsApp:\n'+'\n'.join(f"{r['remote_jid']} — {r['status']} — {(r['note'] or '')[:100]}" for r in mrows))
    finally:await db.close()
    await state.clear()
    await m.answer(('\n\n'.join(sections) if sections else '❌ لم أجد نتائج مطابقة.')[:3900],reply_markup=back())

@dp.callback_query(F.data=='backup')
async def backup(c):
    path=await create_safe_backup_zip(); await log_admin_event(c.from_user.id,'safe_backup_created','system')
    await c.message.answer_document(FSInputFile(path),caption='💾 نسخة احتياطية آمنة — لا تحتوي .env أو جلسات WhatsApp/Telethon')
@dp.callback_query(F.data=='reset')
async def reset(c):
    rows=[[InlineKeyboardButton(text='حذف الروابط والمهام فقط',callback_data='reset_soft')]]
    if real_owner(c.from_user.id):
        rows.append([InlineKeyboardButton(text='إعادة ضبط كاملة',callback_data='reset_factory')])
    rows.append([InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')])
    await c.message.edit_text('سيتم أخذ نسخة احتياطية أولًا. إعادة الضبط الكاملة التي قد تمس قائمة المشرفين متاحة للمالك فقط.',reply_markup=ik(rows))
@dp.callback_query(F.data.in_({'reset_soft','reset_factory'}))
async def reset_do(c):
    if c.data=='reset_factory' and not real_owner(c.from_user.id):return await c.answer('إعادة الضبط الكاملة للمالك فقط.',show_alert=True)
    await backup_db(); await factory_reset(c.data=='reset_soft'); await refresh_principals(); await ensure_alert_rules(settings.owner_id); await log_admin_event(c.from_user.id,'factory_reset' if c.data=='reset_factory' else 'soft_reset','system'); await c.message.answer('تمت إعادة الضبط.',reply_markup=back())

@dp.callback_query(F.data=='jobs')
async def jobs(c):
    db=await connect()
    try: rows=await (await db.execute('SELECT * FROM jobs WHERE operator_id=? AND COALESCE(hidden,0)=0 ORDER BY id DESC LIMIT 20',(scope_uid(c.from_user.id),))).fetchall()
    finally: await db.close()
    lines=['المهام — آخر 20 مهمة:']; kb=[]
    if not rows: lines.append('لا توجد مهام مسجلة.')
    for r in rows:
        try: payload=json.loads(r['payload_json'] or '{}')
        except Exception: payload={}
        kind={'collection':'تجميع','audit':'فحص','join':'انضمام','broadcast':'إرسال رسائل','telegram_history':'استيراد تاريخ Telegram','telegram_auto_sync':'مزامنة Telegram التلقائية','telegram_session_login':'استخراج جلسة Telegram','file_import':'استيراد TXT','manual_import':'إضافة روابط يدوية','expired_import':'إضافة روابط منتهية','ignored_import':'إضافة روابط مهمشة','message_archive':'أرشفة الرسائل','message_restore':'استعادة الرسائل المؤرشفة'}.get(r['kind'],r['kind'])
        detail=(' '+CATEGORY_LABELS.get(payload.get('category'),payload.get('category',''))) if r['kind']=='collection' else ''
        lines.append(f"#{r['id']} {kind}{detail} — {STATUS_LABELS.get(r['status'],r['status'])}")
        row=[InlineKeyboardButton(text=f"📋 تقرير #{r['id']}",callback_data=f"job_report:{r['id']}"),InlineKeyboardButton(text='🗑 حذف',callback_data=f"job_delete:{r['id']}")]
        if r['status'] in {'queued','running'} and r['kind']!='telegram_auto_sync':
            row += [InlineKeyboardButton(text='⏸ إيقاف',callback_data=f"job_pause:{r['id']}"),InlineKeyboardButton(text='⛔ إلغاء',callback_data=f"job_cancel:{r['id']}")]
        elif r['status'] in {'paused','partial','paused_rate_limit','failed','cancelled','interrupted'} or (r['kind']=='collection' and r['status']=='completed'):
            row += [InlineKeyboardButton(text='▶️ استكمال',callback_data=f"job_resume:{r['id']}")]
        kb.append(row)
    kb.append([InlineKeyboardButton(text='📊 تقرير جميع الحسابات',callback_data='accounts_report')])
    kb.append([InlineKeyboardButton(text='🔄 تحديث',callback_data='jobs'),InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')])
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('job_delete:'))
async def job_delete(c:CallbackQuery):
    job_id=int(c.data.split(':')[1]); ok=await hide_job(scope_uid(c.from_user.id),job_id)
    await c.answer('تم حذف المهمة من القائمة.' if ok else 'المهمة غير موجودة.',show_alert=True)
    await jobs(c)

@dp.callback_query(F.data.startswith('job_pause:'))
async def job_pause(c:CallbackQuery):
    job_id=int(c.data.split(':')[1]); ok=await request_pause(scope_uid(c.from_user.id),job_id)
    await c.answer('تم طلب الإيقاف المؤقت. ستتوقف المهمة عند أقرب نقطة آمنة وتحفظ تقدمها.' if ok else 'المهمة منتهية أو لا يمكن إيقافها الآن.',show_alert=True)
    await jobs(c)

@dp.callback_query(F.data.startswith('job_cancel:'))
async def job_cancel(c:CallbackQuery):
    job_id=int(c.data.split(':')[1]); ok=await request_cancel(scope_uid(c.from_user.id),job_id)
    await c.answer('تم إرسال طلب الإلغاء.' if ok else 'المهمة منتهية أو لا تملك صلاحية إلغائها.',show_alert=True)
    await jobs(c)

@dp.callback_query(F.data.startswith('job_report:'))
async def job_report(c:CallbackQuery):
    job_id=int(c.data.split(':')[1]); db=await connect()
    try: r=await (await db.execute('SELECT * FROM jobs WHERE id=? AND operator_id=?',(job_id,scope_uid(c.from_user.id)))).fetchone()
    finally: await db.close()
    if not r:return await c.answer('المهمة غير موجودة.',show_alert=True)
    try: payload=json.loads(r['payload_json'] or '{}')
    except Exception: payload={}
    try: rep=json.loads(r['report_json'] or '{}')
    except Exception: rep={}
    kind={'collection':'تجميع','audit':'فحص','join':'انضمام','broadcast':'إرسال رسائل','telegram_history':'استيراد تاريخ Telegram','telegram_auto_sync':'مزامنة Telegram التلقائية','telegram_session_login':'استخراج جلسة Telegram','file_import':'استيراد TXT','manual_import':'إضافة روابط يدوية','expired_import':'إضافة روابط منتهية','ignored_import':'إضافة روابط مهمشة','message_archive':'أرشفة الرسائل','message_restore':'استعادة الرسائل المؤرشفة'}.get(r['kind'],r['kind'])
    lines=[f'📋 تقرير المهمة #{job_id}',f'النوع: {kind}',f'الحالة: {STATUS_LABELS.get(r["status"],r["status"])}',f'أُنشئت: {r["created_at"]}',f'آخر تحديث: {r["updated_at"]}']
    if r['kind']=='collection':
        lines += [f"القسم: {CATEGORY_LABELS.get(payload.get('category'),payload.get('category','-'))}",f"النمط: {payload.get('mode','-')} | الفترة: {payload.get('period','-')}",f"الرسائل: {rep.get('messages',0)} | النصوص: {rep.get('text_messages',0)} | رسائل URL: {rep.get('url_messages',0)}",f"الجديدة: {rep.get('new',0)} | المكررة: {rep.get('duplicates',0)} | المحظورة: {rep.get('blocked',0)} | القنوات الذكية: {rep.get('smart_channels',0)}"]
        for a in rep.get('accounts',[])[:25]: lines.append(f"• {a.get('label','حساب')} | رسائل {a.get('eligible_messages',0)} | مطابق {a.get('matching_urls',0)} | جديد {a.get('new',0)} | مكرر {a.get('duplicates',0)} | قنوات {a.get('smart_channels',0)}")
    elif r['kind']=='audit': lines += [f"تقرير الفحص: #{rep.get('audit_id','-')}",f"تم فحص: {rep.get('checked',0)} من {rep.get('total',payload.get('count',0))}",f"أضيف إلى المنتهية: {rep.get('expired_added',0)}",'تفصيل الحالات: '+json.dumps(rep.get('status_counts',{}),ensure_ascii=False)]
    elif r['kind']=='join':
        profile=rep.get('safety_profile',payload.get('safety_profile','very_safe'))
        lines += [f"حد المهمة لكل حساب: {rep.get('per_account_limit',payload.get('per_account_limit','الكل'))}",f"وضع الحماية: {PROFILE_LABELS.get(profile,profile)} | الحد اليومي: {payload.get('daily_limit','-')}"]
        for a in rep.get('accounts',[])[:25]: lines.append(f"• {a.get('account','حساب')} | عالج {a.get('processed',0)} | انضم {a.get('joined',0)} | عضو سابق {a.get('already_member',0)} | مكرر JID {a.get('duplicate_group',0)} | طلبات {a.get('requests_sent',a.get('pending',0))} | مؤجل {a.get('retry_later',0)} | فشل {a.get('failed',0)} | مستخدم 24س {a.get('used_24h',0)}")
    elif r['kind']=='broadcast': lines += [f"الحملة: #{rep.get('campaign_id',payload.get('campaign_id','-'))}",f"أُرسل: {rep.get('sent_this_run',0)} | فشل: {rep.get('failed_this_run',0)} | المتبقي: {rep.get('remaining',0)}",f"الدورة: {rep.get('cycle','-')} من {rep.get('repeat_total','-')}"]
    elif r['kind'] in {'telegram_history','telegram_auto_sync'}: lines += [f"المصدر: #{payload.get('source_id','-')}",f"الرسائل المقروءة: {rep.get('processed_messages',0)} | الروابط: {rep.get('found',0)}",f"الجديدة: {rep.get('new',0)} | المكررة: {rep.get('duplicates',0)} | للطابور: {rep.get('queued',0)} | محظورة: {rep.get('blocked',0)} | قنوات ذكية: {rep.get('smart_channels',0)}"]
    elif r['kind'] in {'file_import','manual_import','expired_import','ignored_import'}:
        lines += [f"القسم: {TG_SECTIONS.get(rep.get('section') or payload.get('section'),rep.get('section') or payload.get('section','-'))}",f"URL مكتشفة: {rep.get('urls_detected',rep.get('found',0))} | WhatsApp: {rep.get('whatsapp_found','-')}",f"جديدة: {rep.get('new',0)} | مكررة: {rep.get('duplicates',0)} | محظورة: {rep.get('blocked',0)}",f"منتهية مضافة: {rep.get('expired_added',0)} | مهمشة مضافة: {rep.get('ignored_added',0)} | أزيلت من النشطة: {rep.get('removed_from_active',0)} | قنوات ذكية: {rep.get('smart_channels',0)}"]
    elif r['kind']=='telegram_session_login': lines += [f"الجلسة: #{rep.get('session_id',payload.get('session_id','-'))}",f"الحساب: @{rep.get('username') or '-'}",f"التفويض: {'ناجح' if rep.get('authorized') else 'غير مكتمل'}"]
    elif r['kind']=='message_archive': lines += [f"نُقلت إلى الأرشيف: {rep.get('moved',0)}",f"قبل الضغط: {rep.get('original_bytes',0)} بايت | بعد الضغط: {rep.get('compressed_bytes',0)} بايت ({rep.get('compressed_percent',0)}%)",f"قابل للاستعادة: {'نعم' if rep.get('recoverable') else 'لا'}"]
    elif r['kind']=='message_restore': lines += [f"أُعيدت إلى الرسائل النشطة: {rep.get('restored',0)}"]
    if rep.get('error'): lines.append('الخطأ: '+str(rep['error'])[:700])
    kb=[]
    if r['status'] in {'queued','running'} and r['kind']!='telegram_auto_sync': kb.append([InlineKeyboardButton(text='⏸ إيقاف',callback_data=f'job_pause:{job_id}'),InlineKeyboardButton(text='⛔ إلغاء',callback_data=f'job_cancel:{job_id}')])
    if r['status'] in {'paused','partial','paused_rate_limit','failed','cancelled','interrupted'} or (r['kind']=='collection' and r['status']=='completed'): kb.append([InlineKeyboardButton(text='▶️ استكمال',callback_data=f'job_resume:{job_id}')])
    kb.append([InlineKeyboardButton(text='📄 تحميل التقرير الكامل TXT',callback_data=f'job_export:{job_id}')])
    kb.append([InlineKeyboardButton(text='⬅️ المهام',callback_data='jobs')])
    await c.message.edit_text('\n'.join(lines)[:3900],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('job_export:'))
async def job_export(c:CallbackQuery):
    job_id=int(c.data.split(':')[1]); path=await export_job_report(job_id,scope_uid(c.from_user.id))
    if not path:return await c.answer('المهمة غير موجودة.',show_alert=True)
    await c.message.answer_document(FSInputFile(path),caption=f'التقرير الكامل للمهمة #{job_id}')

@dp.callback_query(F.data.startswith('job_resume:'))
async def job_resume(c:CallbackQuery):
    old_id=int(c.data.split(':')[1]); db=await connect()
    try:r=await (await db.execute('SELECT * FROM jobs WHERE id=? AND operator_id=?',(old_id,scope_uid(c.from_user.id)))).fetchone()
    finally: await db.close()
    if not r:return await c.answer('المهمة غير موجودة.',show_alert=True)
    try: payload=json.loads(r['payload_json'] or '{}')
    except Exception: payload={}
    if r['kind']=='collection':
        op=scope_uid(c.from_user.id); payload['period']='new'; new_id=await create_job(op,'collection',payload); _spawn(_collect_job(new_id,op,c.message.chat.id,payload))
    elif r['kind']=='join':
        op=scope_uid(c.from_user.id); new_id=await create_job(op,'join',payload); _spawn(_join_notify(op,c.message.chat.id,payload,new_id,c.from_user.id))
    elif r['kind']=='broadcast':
        cid=int(payload.get('campaign_id') or 0)
        if not cid:return await c.answer('معرف حملة الإرسال غير موجود.',show_alert=True)
        op=scope_uid(c.from_user.id); new_id=await create_job(op,'broadcast',{'campaign_id':cid}); _spawn(_broadcast_job(new_id,op,c.message.chat.id,cid))
    elif r['kind']=='audit':
        try: rep=json.loads(r['report_json'] or '{}')
        except Exception: rep={}
        aid=int(rep.get('audit_id') or payload.get('audit_id') or 0)
        if not aid:return await c.answer('لا توجد نقطة استكمال محفوظة لهذا الفحص.',show_alert=True)
        op=scope_uid(c.from_user.id); new_id=await create_job(op,'audit',{'source':'resume','audit_id':aid}); _spawn(_audit_job(new_id,op,c.message.chat.id,[],f'Resume audit #{aid}',aid))
    elif r['kind']=='telegram_history':
        sid=int(payload.get('source_id') or 0)
        if not sid:return await c.answer('معرف مصدر Telegram غير موجود.',show_alert=True)
        op=scope_uid(c.from_user.id); new_id=await create_job(op,'telegram_history',{'source_id':sid}); _spawn(_telegram_history_job(new_id,op,c.message.chat.id,sid,c.from_user.id))
    else:return await c.answer('الاستكمال غير متاح لهذا النوع.',show_alert=True)
    await c.answer(f'تم إنشاء مهمة الاستكمال #{new_id}.',show_alert=True); await jobs(c)

@dp.callback_query(F.data=='accounts_report')
async def accounts_report(c:CallbackQuery):
    db=await connect()
    try: accts=await (await db.execute('SELECT * FROM account_slots ORDER BY operator_id,id')).fetchall() if owner(c.from_user.id) else await (await db.execute('SELECT * FROM account_slots WHERE operator_id=? ORDER BY id',(c.from_user.id,))).fetchall()
    finally: await db.close()
    lines=['📊 التقرير الشامل لجميع الحسابات',f'إجمالي الحسابات: {len(accts)}']
    for a in accts:
        db=await connect()
        try:
            m=await (await db.execute("SELECT COUNT(*) msgs,SUM(CASE WHEN length(trim(text))>0 THEN 1 ELSE 0 END) texts,COUNT(DISTINCT CASE WHEN remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter' AND remote_jid NOT LIKE '%@broadcast' THEN remote_jid END) chats,COUNT(DISTINCT CASE WHEN remote_jid LIKE '%@newsletter' THEN remote_jid END) channels FROM wa_messages WHERE account_slot_id=?",(a['id'],))).fetchone()
            j=await (await db.execute("""SELECT COUNT(*) c,
                SUM(CASE WHEN status='joined' THEN 1 ELSE 0 END) joined,
                SUM(CASE WHEN status='already_member' THEN 1 ELSE 0 END) already_member,
                SUM(CASE WHEN status='duplicate_group' THEN 1 ELSE 0 END) duplicate_group,
                SUM(CASE WHEN status='pending_approval' THEN 1 ELSE 0 END) requests_sent,
                SUM(CASE WHEN status='admins_not_accepting' THEN 1 ELSE 0 END) admins_not_accepting,
                SUM(CASE WHEN status='retry_later' THEN 1 ELSE 0 END) retry_later,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
                FROM join_attempts WHERE account_slot_id=?""",(a['id'],))).fetchone()
            bc=await (await db.execute('SELECT COUNT(*) c,COALESCE(SUM(sent_targets),0) sent FROM broadcast_campaigns WHERE account_slot_id=?',(a['id'],))).fetchone()
        finally: await db.close()
        try:gcount=len((await provider.groups(a['provider_account_id'])).get('groups') or []) if a['provider_account_id'] else 0
        except:gcount=0
        lines += ['',f"📱 #{a['id']} {a['label']} — المشرف {a['operator_id']}",f"الحالة: {a['health']} | {'فعال' if a['enabled'] else 'متوقف'} | آخر ظهور: {a['last_seen_at'] or '-'}",f"رسائل: {int(m['msgs'] or 0)} | نصوص: {int(m['texts'] or 0)} | مجموعات حالية: {gcount} | دردشات: {int(m['chats'] or 0)} | قنوات: {int(m['channels'] or 0)}",f"محاولات الانضمام: {int(j['c'] or 0)} | انضم فعليًا: {int(j['joined'] or 0)} | عضو سابق: {int(j['already_member'] or 0)} | مجموعة مكررة JID: {int(j['duplicate_group'] or 0)}",f"طلبات انضمام مرسلة: {int(j['requests_sent'] or 0)} | رفض/لم يقبل المشرفون: {int(j['admins_not_accepting'] or 0)} | مؤجل: {int(j['retry_later'] or 0)} | فشل: {int(j['failed'] or 0)}",f"حملات: {int(bc['c'] or 0)} | أهداف إرسال ناجحة: {int(bc['sent'] or 0)}",f"آخر خطأ: {a['last_error'] or '-'}"]
    text='\n'.join(lines)
    if len(text)<=3900: await c.message.edit_text(text,reply_markup=ik([[InlineKeyboardButton(text='⬅️ المهام',callback_data='jobs')]]))
    else:
        os.makedirs(settings.export_dir,exist_ok=True); path=os.path.join(settings.export_dir,'accounts_full_report.txt'); open(path,'w',encoding='utf-8').write(text)
        await c.message.answer_document(FSInputFile(path),caption='التقرير الشامل لجميع حسابات WhatsApp')
@dp.callback_query(F.data=='watches')
async def watches(c,state):
    db=await connect()
    try:rows=await (await db.execute('SELECT * FROM watches WHERE operator_id=? ORDER BY id',(scope_uid(c.from_user.id),))).fetchall()
    finally:await db.close()
    txt='المراقبات المباشرة:\n'+('\n'.join(f"#{r['id']} {r['remote_jid']} — {'فعال' if r['enabled'] else 'متوقف'}" for r in rows) or 'لا توجد.')
    kb=[]
    for r in rows:
        kb.append([InlineKeyboardButton(text=f"{'⏸ إيقاف' if r['enabled'] else '▶️ تشغيل'} #{r['id']}",callback_data=f"watch_toggle:{r['id']}"),InlineKeyboardButton(text=f"🗑 حذف #{r['id']}",callback_data=f"watch_delete:{r['id']}")])
    kb += [[InlineKeyboardButton(text='➕ إضافة مراقبة',callback_data='watch_add')],[InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]]
    await c.message.edit_text(txt,reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('watch_toggle:'))
async def watch_toggle(c:CallbackQuery,state:FSMContext):
    wid=int(c.data.split(':')[1]); db=await connect()
    try:
        await db.execute('UPDATE watches SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND operator_id=?',(wid,scope_uid(c.from_user.id))); await db.commit()
    finally: await db.close()
    await c.answer('تم تحديث حالة المراقبة.'); await watches(c,state)

@dp.callback_query(F.data.startswith('watch_delete:'))
async def watch_delete(c:CallbackQuery,state:FSMContext):
    wid=int(c.data.split(':')[1]); db=await connect()
    try:
        await db.execute('DELETE FROM watches WHERE id=? AND operator_id=?',(wid,scope_uid(c.from_user.id))); await db.commit()
    finally: await db.close()
    await c.answer('تم حذف المراقبة.'); await watches(c,state)

@dp.callback_query(F.data=='watch_add')
async def watch_add(c,state): await state.set_state(S.watch_jid) or await c.message.answer('أرسل JID للمجموعة من زر عرض المجموعات. سيتم جمع روابط WhatsApp الجديدة منها مباشرة.',reply_markup=cancel_kb())
@dp.message(S.watch_jid)
async def watch_add_msg(m,state):
    jid=(m.text or '').strip(); db=await connect()
    try:
        a=await (await db.execute("SELECT id FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id LIMIT 1")).fetchone() if owner(m.from_user.id) else await (await db.execute("SELECT id FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected' ORDER BY id LIMIT 1",(m.from_user.id,))).fetchone()
        if not a:return await m.answer('أضف حسابًا متصلًا أولًا.')
        last=(await (await db.execute('SELECT COALESCE(MAX(id),0) m FROM wa_messages WHERE account_slot_id=? AND remote_jid=?',(a['id'],jid))).fetchone())['m']
        await db.execute('INSERT OR IGNORE INTO watches(operator_id,account_slot_id,remote_jid,category,last_message_row_id,created_at) VALUES(?,?,?,?,?,?)',(scope_uid(m.from_user.id),a['id'],jid,'whatsapp_group',last,now_iso())); await db.commit()
    finally:await db.close()
    await state.clear(); await m.answer('تم إنشاء المراقبة من الرسائل الجديدة فقط.',reply_markup=back())


# ---------------- V2.6 WhatsApp Inbox / notes / tags ----------------
async def _inbox_message_row(mid:int,uid:int):
    db=await connect()
    try:
        if owner(uid):
            return await (await db.execute("""SELECT m.*,a.label account_label,a.operator_id FROM wa_messages m
                JOIN account_slots a ON a.id=m.account_slot_id WHERE m.id=?""",(int(mid),))).fetchone()
        return await (await db.execute("""SELECT m.*,a.label account_label,a.operator_id FROM wa_messages m
            JOIN account_slots a ON a.id=m.account_slot_id WHERE m.id=? AND a.operator_id=?""",(int(mid),int(uid)))).fetchone()
    finally:await db.close()

@dp.callback_query(F.data=='inbox')
async def inbox(c:CallbackQuery):
    db=await connect()
    try:
        rows=await (await db.execute("""SELECT m.id,m.account_slot_id,m.remote_jid,m.text,m.inserted_at,a.label,
            COALESCE(cm.status,'new') meta_status,cm.note
            FROM wa_messages m
            JOIN (SELECT account_slot_id,remote_jid,MAX(id) mid FROM wa_messages
                  WHERE remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter' AND remote_jid NOT LIKE '%@broadcast'
                  GROUP BY account_slot_id,remote_jid) x ON x.mid=m.id
            JOIN account_slots a ON a.id=m.account_slot_id
            LEFT JOIN chat_metadata cm ON cm.owner_id=? AND cm.account_slot_id=m.account_slot_id AND cm.remote_jid=m.remote_jid
            WHERE (a.operator_id=? OR ?=1)
            ORDER BY m.id DESC LIMIT 20""",(scope_uid(c.from_user.id),c.from_user.id,1 if owner(c.from_user.id) else 0))).fetchall()
    finally:await db.close()
    lines=['📥 صندوق متابعة WhatsApp — أحدث 20 محادثة:']; kb=[]
    status_icon={'new':'🔵','followup':'🟡','important':'⭐','completed':'✅','ignored':'⚪'}
    if not rows:lines.append('لا توجد محادثات شخصية متزامنة بعد.')
    for r in rows:
        preview=' '.join((r['text'] or '').split())[:70] or '(بدون نص)'
        lines.append(f"{status_icon.get(r['meta_status'],'🔵')} #{r['id']} {r['label']} | {r['remote_jid']}\n{preview}")
        kb.append([InlineKeyboardButton(text=f"💬 #{r['id']} {r['remote_jid'][:24]}",callback_data=f"inboxmsg:{r['id']}")])
    kb.append([InlineKeyboardButton(text='🔄 تحديث',callback_data='inbox'),InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')])
    await c.message.edit_text('\n'.join(lines)[:3600],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('inboxmsg:'))
async def inbox_detail(c:CallbackQuery):
    mid=int(c.data.split(':')[1]); r=await _inbox_message_row(mid,c.from_user.id)
    if not r:return await c.answer('المحادثة غير موجودة.',show_alert=True)
    db=await connect()
    try:
        cm=await (await db.execute('SELECT * FROM chat_metadata WHERE owner_id=? AND account_slot_id=? AND remote_jid=?',(scope_uid(c.from_user.id),r['account_slot_id'],r['remote_jid']))).fetchone()
    finally:await db.close()
    tags=await list_tags(scope_uid(c.from_user.id),'chat',f"{r['account_slot_id']}:{r['remote_jid']}")
    status=(cm['status'] if cm else 'new'); note=(cm['note'] if cm else None)
    text=f"💬 متابعة المحادثة\nالحساب: {r['account_label']}\nJID: {r['remote_jid']}\nالحالة: {status}\nآخر رسالة: {(r['text'] or '')[:1200] or '(بدون نص)'}\nالملاحظة: {note or '-'}\nTags: {', '.join(tags) or '-'}"
    kb=[
        [InlineKeyboardButton(text='⭐ مهم',callback_data=f'inbox_status:{mid}:important'),InlineKeyboardButton(text='🟡 متابعة',callback_data=f'inbox_status:{mid}:followup'),InlineKeyboardButton(text='✅ مكتمل',callback_data=f'inbox_status:{mid}:completed')],
        [InlineKeyboardButton(text='📝 ملاحظة',callback_data=f'inbox_note:{mid}'),InlineKeyboardButton(text='🏷 Tag',callback_data=f'inbox_tag:{mid}')],
        [InlineKeyboardButton(text='⏰ تذكير غدًا',callback_data=f'inbox_follow:{mid}:1440'),InlineKeyboardButton(text='⏰ بعد أسبوع',callback_data=f'inbox_follow:{mid}:10080')],
        [InlineKeyboardButton(text='⚪ تجاهل',callback_data=f'inbox_status:{mid}:ignored')],
        [InlineKeyboardButton(text='⬅️ صندوق المتابعة',callback_data='inbox')],
    ]
    await c.message.edit_text(text[:3600],reply_markup=ik(kb))

@dp.callback_query(F.data.startswith('inbox_status:'))
async def inbox_status(c:CallbackQuery):
    _,mid_s,status=c.data.split(':'); mid=int(mid_s); r=await _inbox_message_row(mid,c.from_user.id)
    if not r:return await c.answer('غير موجود.',show_alert=True)
    if status not in {'new','followup','important','completed','ignored'}:return await c.answer('حالة غير صالحة.',show_alert=True)
    await upsert_chat_meta(scope_uid(c.from_user.id),int(r['account_slot_id']),r['remote_jid'],status=status)
    await log_admin_event(c.from_user.id,'chat_status_changed','chat',f"{r['account_slot_id']}:{r['remote_jid']}",{'status':status})
    await c.answer('تم تحديث الحالة.'); await inbox_detail(c)

@dp.callback_query(F.data.startswith('inbox_note:'))
async def inbox_note(c:CallbackQuery,state:FSMContext):
    mid=int(c.data.split(':')[1]); r=await _inbox_message_row(mid,c.from_user.id)
    if not r:return await c.answer('غير موجود.',show_alert=True)
    await state.update_data(inbox_mid=mid); await state.set_state(S.inbox_note)
    await c.message.answer('أرسل الملاحظة التي تريد حفظها لهذه المحادثة.',reply_markup=cancel_kb())

@dp.message(S.inbox_note)
async def inbox_note_msg(m:Message,state:FSMContext):
    d=await state.get_data(); mid=int(d.get('inbox_mid') or 0); r=await _inbox_message_row(mid,m.from_user.id)
    if not r:return await state.clear()
    note=(m.text or '').strip()[:1500]
    await upsert_chat_meta(scope_uid(m.from_user.id),int(r['account_slot_id']),r['remote_jid'],note=note)
    await log_admin_event(m.from_user.id,'chat_note_updated','chat',f"{r['account_slot_id']}:{r['remote_jid']}")
    await state.clear(); await m.answer('✅ تم حفظ الملاحظة.',reply_markup=ik([[InlineKeyboardButton(text='📥 صندوق المتابعة',callback_data='inbox')]]))

@dp.callback_query(F.data.startswith('inbox_tag:'))
async def inbox_tag(c:CallbackQuery,state:FSMContext):
    mid=int(c.data.split(':')[1]); r=await _inbox_message_row(mid,c.from_user.id)
    if not r:return await c.answer('غير موجود.',show_alert=True)
    await state.update_data(inbox_mid=mid); await state.set_state(S.inbox_tag)
    await c.message.answer('أرسل Tag مختصرًا مثل: عميل أو طالب أو متابعة.',reply_markup=cancel_kb())

@dp.message(S.inbox_tag)
async def inbox_tag_msg(m:Message,state:FSMContext):
    d=await state.get_data(); mid=int(d.get('inbox_mid') or 0); r=await _inbox_message_row(mid,m.from_user.id)
    if not r:return await state.clear()
    tag=(m.text or '').strip()[:50]
    await add_tag(scope_uid(m.from_user.id),'chat',f"{r['account_slot_id']}:{r['remote_jid']}",tag)
    await log_admin_event(m.from_user.id,'chat_tag_added','chat',f"{r['account_slot_id']}:{r['remote_jid']}",{'tag':tag})
    await state.clear(); await m.answer('✅ تم حفظ الـTag.',reply_markup=ik([[InlineKeyboardButton(text='📥 صندوق المتابعة',callback_data='inbox')]]))

@dp.callback_query(F.data.startswith('inbox_follow:'))
async def inbox_follow(c:CallbackQuery):
    _,mid_s,delay_s=c.data.split(':'); mid=int(mid_s); r=await _inbox_message_row(mid,c.from_user.id)
    if not r:return await c.answer('غير موجود.',show_alert=True)
    preview=' '.join((r['text'] or '').split())[:220]
    payload={'text':f"متابعة محادثة WhatsApp\nالحساب: {r['account_label']}\nJID: {r['remote_jid']}\nآخر رسالة: {preview}"}
    tid=await create_scheduled_task(scope_uid(c.from_user.id),'reminder',f"متابعة {r['remote_jid']}",int(delay_s),payload,0,'high')
    await upsert_chat_meta(scope_uid(c.from_user.id),int(r['account_slot_id']),r['remote_jid'],status='followup')
    await c.answer(f'تم إنشاء تذكير المتابعة #{tid}.',show_alert=True)

async def _templates_keyboard(uid:int, prefix:str='broadcast_tpl'):
    uid=scope_uid(uid)
    db=await connect()
    try: rows=await (await db.execute('SELECT id,name FROM message_templates WHERE owner_id=? AND enabled=1 ORDER BY id DESC LIMIT 30',(uid,))).fetchall()
    finally: await db.close()
    kb=[[InlineKeyboardButton(text=f"📝 {r['name']}",callback_data=f"{prefix}:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton(text='➕ إضافة رسالة/قالب',callback_data='msg_tpl_add')])
    kb.append([InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')])
    return ik(kb),rows

@dp.callback_query(F.data=='messages')
async def messages_menu(c:CallbackQuery,state:FSMContext):
    await state.clear()
    await c.message.edit_text('📨 إدارة إرسال الرسائل\n\nالإرسال مخصص للمجموعات أو المحادثات الموجودة فعلًا في الحساب المرتبط. كل حملة تحفظ تقدمها وتستطيع استكمال البقية بدون تكرار من تم الإرسال لهم.',reply_markup=ik([
        [InlineKeyboardButton(text='📥 صندوق المتابعة',callback_data='inbox')],
        [InlineKeyboardButton(text='📝 إدارة الرسائل/القوالب',callback_data='msg_templates')],
        [InlineKeyboardButton(text='👥 إرسال إلى المجموعات',callback_data='msg_send_groups')],
        [InlineKeyboardButton(text='👤 إرسال إلى الدردشات الحالية',callback_data='msg_send_chats')],
        [InlineKeyboardButton(text='🗂️ الدليل: دردشات / مجموعات / قنوات',callback_data='msg_directory')],
        [InlineKeyboardButton(text='📊 حملات الإرسال',callback_data='msg_campaigns')],
        [InlineKeyboardButton(text='🚫 قائمة عدم الإرسال',callback_data='msg_suppression')],
        [InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')],
    ]))

@dp.callback_query(F.data=='msg_templates')
async def msg_templates(c:CallbackQuery):
    db=await connect()
    try: rows=await (await db.execute('SELECT id,name,body FROM message_templates WHERE owner_id=? ORDER BY id DESC LIMIT 30',(scope_uid(c.from_user.id),))).fetchall()
    finally: await db.close()
    lines=['📝 الرسائل المحفوظة:']; kb=[]
    for r in rows:
        preview=(r['body'] or '').replace('\n',' ')[:70]
        lines.append(f"#{r['id']} {r['name']} — {preview}")
        kb.append([InlineKeyboardButton(text=f"🗑 حذف #{r['id']} {r['name']}",callback_data=f"msg_tpl_del:{r['id']}")])
    if not rows: lines.append('لا توجد رسائل محفوظة.')
    kb += [[InlineKeyboardButton(text='➕ إضافة رسالة جديدة',callback_data='msg_tpl_add')],[InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')]]
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik(kb))

@dp.callback_query(F.data=='msg_tpl_add')
async def msg_tpl_add(c:CallbackQuery,state:FSMContext):
    await state.set_state(S.template_name)
    await c.message.answer('أرسل اسمًا مختصرًا للرسالة، مثل: إعلان المساء',reply_markup=cancel_kb())

@dp.message(S.template_name)
async def msg_tpl_name(m:Message,state:FSMContext):
    name=(m.text or '').strip()[:80]
    if not name:return await m.answer('أرسل اسمًا صالحًا.',reply_markup=cancel_kb())
    await state.update_data(template_name=name); await state.set_state(S.template_body)
    await m.answer('الآن أرسل نص الرسالة كما تريد أن تصل للمجموعة/الدردشة. الحد 3500 حرف.',reply_markup=cancel_kb())

@dp.message(S.template_body)
async def msg_tpl_body(m:Message,state:FSMContext):
    body=(m.text or '').strip()
    if not body:return await m.answer('الرسالة فارغة.',reply_markup=cancel_kb())
    if len(body)>3500:return await m.answer('الرسالة أطول من 3500 حرف. اختصرها.',reply_markup=cancel_kb())
    d=await state.get_data(); db=await connect()
    try:
        await db.execute('INSERT INTO message_templates(owner_id,name,body,created_at) VALUES(?,?,?,?)',(scope_uid(m.from_user.id),d['template_name'],body,now_iso())); await db.commit()
    except Exception:
        await db.close(); return await m.answer('اسم الرسالة مستخدم مسبقًا. اختر اسمًا آخر.',reply_markup=cancel_kb())
    finally:
        try: await db.close()
        except: pass
    await state.clear(); await m.answer('✅ تم حفظ الرسالة.',reply_markup=menu(m.from_user.id))

@dp.callback_query(F.data.startswith('msg_tpl_del:'))
async def msg_tpl_del(c:CallbackQuery):
    tid=int(c.data.split(':')[1]); db=await connect()
    try:
        await db.execute('UPDATE message_templates SET enabled=0 WHERE id=? AND owner_id=?',(tid,scope_uid(c.from_user.id))); await db.commit()
    finally: await db.close()
    await c.answer('تم تعطيل الرسالة.'); await msg_templates(c)

async def _show_broadcast_accounts(c:CallbackQuery,target_type:str):
    db=await connect()
    try:
        if owner(c.from_user.id): rows=await (await db.execute("SELECT * FROM account_slots WHERE enabled=1 AND health='connected' ORDER BY operator_id,id")).fetchall()
        else: rows=await (await db.execute("SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 AND health='connected' ORDER BY id",(c.from_user.id,))).fetchall()
    finally: await db.close()
    kb=[[InlineKeyboardButton(text=f"📱 #{r['id']} {r['label']}",callback_data=f"broadcast_account:{r['id']}:{target_type}")] for r in rows]
    kb.append([InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')])
    await c.message.edit_text((f'الحسابات المرتبطة والمتصلة: {len(rows)}\n\nاختر حساب WhatsApp الذي سيتم الإرسال منه:' if rows else 'لا يوجد حساب WhatsApp متصل.'),reply_markup=ik(kb))

@dp.callback_query(F.data=='msg_send_groups')
async def msg_send_groups(c:CallbackQuery): await _show_broadcast_accounts(c,'group')

@dp.callback_query(F.data=='msg_send_chats')
async def msg_send_chats(c:CallbackQuery): await _show_broadcast_accounts(c,'chat')

@dp.callback_query(F.data.startswith('broadcast_account:'))
async def broadcast_account(c:CallbackQuery,state:FSMContext):
    _,slot_s,target_type=c.data.split(':'); slot=int(slot_s); a=await get_slot(c.from_user.id,slot)
    if not a:return await c.answer('الحساب غير موجود.',show_alert=True)
    if target_type=='group':
        try: available=len((await provider.groups(a['provider_account_id'])).get('groups') or [])
        except Exception as e:return await c.answer(f'تعذر قراءة المجموعات: {e}',show_alert=True)
        label='المجموعات'
    else:
        db=await connect()
        try:
            available=(await (await db.execute("SELECT COUNT(DISTINCT remote_jid) c FROM wa_messages WHERE account_slot_id=? AND remote_jid IS NOT NULL AND remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter' AND remote_jid NOT LIKE '%@broadcast'",(slot,))).fetchone())['c']
        finally: await db.close()
        label='الدردشات الشخصية الحالية'
    await state.update_data(broadcast_slot=slot,broadcast_target_type=target_type,broadcast_available=int(available))
    preview=''
    if target_type=='chat' and available:
        try:
            items=await _load_broadcast_select_targets(state)
            available=len(items)
            await state.update_data(broadcast_available=int(available))
            rows=[]
            for i,x in enumerate(items[:12],1):
                jid=x.get('jid') or ''; number=jid.split('@',1)[0]; name=(x.get('name') or '').strip()
                shown=name if name and name!=number else 'بدون اسم محفوظ'
                rows.append(f'{i}. {shown} — {number}')
            preview='\n\nأول المستلمين حسب آخر نشاط:\n'+'\n'.join(rows)
            if len(items)>12: preview+=f'\n… و {len(items)-12} هدف آخر. استخدم «عرض/اختيار المستلمين» لرؤية الجميع.'
        except Exception:
            preview='\n\nتعذر تحميل أسماء جهات الاتصال الآن؛ يمكن عرض الأهداف من زر الاختيار.'
    await c.message.edit_text((f"الحساب: {a['label']}\nعدد {label}: {available}"+preview+"\n\nاختر كل المتاح أو حدد عددًا من البداية. بعد انتهاء هذا العدد ستستطيع متابعة بقية القائمة من نفس الحملة دون تكرار.")[:3500],reply_markup=ik([
        [InlineKeyboardButton(text=f'✅ كل {available}',callback_data='broadcast_scope:all'),InlineKeyboardButton(text='🔢 تحديد عدد',callback_data='broadcast_scope:count')],
        [InlineKeyboardButton(text='📋 عرض/اختيار المستلمين',callback_data='broadcast_scope:select')],
        [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')],
    ]))

async def _load_broadcast_select_targets(state:FSMContext):
    d=await state.get_data(); slot=int(d['broadcast_slot']); typ=d['broadcast_target_type']; a=await get_slot(settings.owner_id,slot)
    items=[]
    if typ=='group':
        data=await provider.groups(a['provider_account_id'])
        items=[{'jid':g.get('jid'),'name':g.get('subject') or g.get('jid')} for g in (data.get('groups') or []) if g.get('jid')]
        items.sort(key=lambda x:(x['name'] or '').casefold())
    else:
        try:
            contacts=(await provider.contacts(a['provider_account_id'])).get('contacts') or []
            names={x.get('jid'):x.get('name') or x.get('notify') or x.get('verifiedName') for x in contacts if x.get('jid')}
        except Exception: names={}
        db=await connect()
        try:
            rows=await (await db.execute("""SELECT remote_jid,MAX(id) last_id FROM wa_messages WHERE account_slot_id=? AND remote_jid IS NOT NULL
              AND remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter' AND remote_jid NOT LIKE '%@broadcast'
              GROUP BY remote_jid ORDER BY last_id DESC""",(slot,))).fetchall()
        finally: await db.close()
        for r in rows:
            jid=r['remote_jid']; fallback=jid.split('@',1)[0]; items.append({'jid':jid,'name':names.get(jid) or fallback})
    await state.update_data(broadcast_target_items=items,broadcast_selected=[])
    return items

async def _render_broadcast_selection(message,state:FSMContext,page:int=0):
    d=await state.get_data(); items=d.get('broadcast_target_items') or []; selected=set(d.get('broadcast_selected') or [])
    per=8; pages=max(1,(len(items)+per-1)//per); page=max(0,min(page,pages-1)); kb=[]
    for idx in range(page*per,min(len(items),(page+1)*per)):
        x=items[idx]; mark='✅' if x['jid'] in selected else '⬜'; jid=x.get('jid') or ''; number=jid.split('@',1)[0]; name=(x.get('name') or '').strip()
        base=name if name and name!=number else number
        label=(f'{idx+1}. {base}' + (f' — {number}' if name and name!=number else ''))[:52]
        kb.append([InlineKeyboardButton(text=f'{mark} {label}',callback_data=f'broadcast_pick:{idx}:{page}')])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text='◀️',callback_data=f'broadcast_page:{page-1}'))
    nav.append(InlineKeyboardButton(text=f'{page+1}/{pages}',callback_data='noop'))
    if page+1<pages: nav.append(InlineKeyboardButton(text='▶️',callback_data=f'broadcast_page:{page+1}'))
    kb.append(nav); kb.append([InlineKeyboardButton(text=f'✅ اعتماد المحدد ({len(selected)})',callback_data='broadcast_selection_done')]); kb.append([InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')])
    await message.edit_text(f'اختر الأهداف يدويًا. المحدد: {len(selected)} من {len(items)}',reply_markup=ik(kb))

@dp.callback_query(F.data=='noop')
async def noop(c:CallbackQuery): await c.answer()

@dp.callback_query(F.data=='broadcast_scope:select')
async def broadcast_scope_select(c:CallbackQuery,state:FSMContext):
    items=await _load_broadcast_select_targets(state)
    if not items:return await c.answer('لا توجد أهداف متاحة.',show_alert=True)
    await _render_broadcast_selection(c.message,state,0)

@dp.callback_query(F.data.startswith('broadcast_pick:'))
async def broadcast_pick(c:CallbackQuery,state:FSMContext):
    _,idx_s,page_s=c.data.split(':'); idx=int(idx_s); page=int(page_s); d=await state.get_data(); items=d.get('broadcast_target_items') or []
    if idx>=len(items):return await c.answer('العنصر لم يعد متاحًا.',show_alert=True)
    sel=set(d.get('broadcast_selected') or []); jid=items[idx]['jid']; sel.remove(jid) if jid in sel else sel.add(jid)
    await state.update_data(broadcast_selected=list(sel)); await _render_broadcast_selection(c.message,state,page)

@dp.callback_query(F.data.startswith('broadcast_page:'))
async def broadcast_page(c:CallbackQuery,state:FSMContext): await _render_broadcast_selection(c.message,state,int(c.data.split(':')[1]))

@dp.callback_query(F.data=='broadcast_selection_done')
async def broadcast_selection_done(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); sel=d.get('broadcast_selected') or []
    if not sel:return await c.answer('اختر هدفًا واحدًا على الأقل.',show_alert=True)
    await state.update_data(broadcast_limit=0); kb,rows=await _templates_keyboard(c.from_user.id)
    await c.message.edit_text(f'تم اختيار {len(sel)} هدف. اختر الرسالة:' if rows else 'لا توجد رسالة محفوظة. أضف رسالة أولًا.',reply_markup=kb)

@dp.callback_query(F.data=='broadcast_scope:all')
async def broadcast_scope_all(c:CallbackQuery,state:FSMContext):
    await state.update_data(broadcast_limit=0); kb,rows=await _templates_keyboard(c.from_user.id)
    await c.message.edit_text('اختر الرسالة التي سيتم إرسالها:' if rows else 'لا توجد رسالة محفوظة. أضف رسالة أولًا.',reply_markup=kb)

@dp.callback_query(F.data=='broadcast_scope:count')
async def broadcast_scope_count(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); await state.set_state(S.broadcast_count)
    await c.message.answer(f"أرسل عدد {('المجموعات' if d.get('broadcast_target_type')=='group' else 'الدردشات')} المطلوب لهذه الدفعة. المتاح: {d.get('broadcast_available',0)}",reply_markup=cancel_kb())

@dp.message(S.broadcast_count)
async def broadcast_count_msg(m:Message,state:FSMContext):
    d=await state.get_data()
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1 or n>int(d.get('broadcast_available',0)):return await m.answer(f"اختر رقمًا بين 1 و {d.get('broadcast_available',0)}.",reply_markup=cancel_kb())
    await state.update_data(broadcast_limit=n); await state.set_state(None); kb,rows=await _templates_keyboard(m.from_user.id)
    await m.answer('اختر الرسالة:' if rows else 'لا توجد رسالة محفوظة. أضف رسالة أولًا.',reply_markup=kb)

@dp.callback_query(F.data.startswith('broadcast_tpl:'))
async def broadcast_tpl(c:CallbackQuery,state:FSMContext):
    tid=int(c.data.split(':')[1]); await state.update_data(broadcast_template=tid)
    await c.message.edit_text('كم رسالة تريد إرسالها داخل كل مجموعة/دردشة؟',reply_markup=ik([
        [InlineKeyboardButton(text='1',callback_data='broadcast_mpt:1'),InlineKeyboardButton(text='2',callback_data='broadcast_mpt:2'),InlineKeyboardButton(text='3',callback_data='broadcast_mpt:3'),InlineKeyboardButton(text='5',callback_data='broadcast_mpt:5')],
        [InlineKeyboardButton(text='🔢 عدد آخر',callback_data='broadcast_mpt:custom')],
        [InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')]
    ]))

@dp.callback_query(F.data.startswith('broadcast_mpt:'))
async def broadcast_mpt(c:CallbackQuery,state:FSMContext):
    v=c.data.split(':')[1]
    if v=='custom':
        await state.set_state(S.broadcast_mpt_input); return await c.message.answer('أرسل عدد الرسائل لكل هدف كرقم صحيح.',reply_markup=cancel_kb())
    n=int(v); await state.update_data(broadcast_mpt=n); await state.set_state(S.broadcast_delay)
    await c.message.answer('أرسل الفاصل بالثواني بين المجموعة/الدردشة والتي بعدها. يمكنك اختيار 0 أو أي قيمة تريدها؛ وإذا فرض WhatsApp تقييدًا يتوقف البوت تلقائيًا.',reply_markup=cancel_kb())

@dp.message(S.broadcast_mpt_input)
async def broadcast_mpt_input_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1 or n>settings.broadcast_max_messages_per_target:return await m.answer(f'اختر عددًا من 1 إلى {settings.broadcast_max_messages_per_target}.',reply_markup=cancel_kb())
    await state.update_data(broadcast_mpt=n); await state.set_state(S.broadcast_delay)
    await m.answer('أرسل الفاصل بالثواني بين كل هدف والذي بعده. 0 مسموح.',reply_markup=cancel_kb())

@dp.message(S.broadcast_delay)
async def broadcast_delay_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل عدد الثواني كرقم صحيح.',reply_markup=cancel_kb())
    if n<0:return await m.answer('الفاصل لا يمكن أن يكون سالبًا.',reply_markup=cancel_kb())
    await state.update_data(broadcast_delay=n); await state.set_state(S.broadcast_batch_size)
    await m.answer('بعد كم هدف تريد أخذ راحة طويلة؟ مثال: 10 يعني بعد كل 10 مجموعات/دردشات.',reply_markup=cancel_kb())

@dp.message(S.broadcast_batch_size)
async def broadcast_batch_size_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1 or n>100000:return await m.answer('اختر حجم دفعة من 1 إلى 100000.',reply_markup=cancel_kb())
    await state.update_data(broadcast_batch_size=n); await state.set_state(S.broadcast_batch_rest)
    await m.answer('أرسل مدة الراحة بين الدفعات بالدقائق. 0 مسموح.',reply_markup=cancel_kb())

@dp.message(S.broadcast_batch_rest)
async def broadcast_batch_rest_msg(m:Message,state:FSMContext):
    try: minutes=int((m.text or '').strip())
    except:return await m.answer('أرسل عدد الدقائق كرقم صحيح.',reply_markup=cancel_kb())
    seconds=minutes*60
    if seconds<0:return await m.answer('المدة لا يمكن أن تكون سالبة.',reply_markup=cancel_kb())
    await state.update_data(broadcast_batch_rest=seconds)
    await m.answer('🔁 هل تريد تكرار دورة الإرسال بعد اكتمال جميع الأهداف؟\nاختر عدد الدورات الإجمالي. التكرار محدود ولا يعمل بلا نهاية.',reply_markup=ik([
        [InlineKeyboardButton(text='مرة واحدة فقط',callback_data='broadcast_repeat:1'),InlineKeyboardButton(text='دورتان',callback_data='broadcast_repeat:2')],
        [InlineKeyboardButton(text='3 دورات',callback_data='broadcast_repeat:3'),InlineKeyboardButton(text='5 دورات',callback_data='broadcast_repeat:5')],
        [InlineKeyboardButton(text='🔢 عدد آخر',callback_data='broadcast_repeat:custom')],[InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')]
    ]))

@dp.callback_query(F.data.startswith('broadcast_repeat:'))
async def broadcast_repeat_pick(c:CallbackQuery,state:FSMContext):
    v=c.data.split(':')[1]
    if v=='custom':
        await state.set_state(S.broadcast_repeat_count); return await c.message.answer(f'أرسل عدد الدورات من 1 إلى {settings.broadcast_max_repeat_cycles}.',reply_markup=cancel_kb())
    n=int(v); await state.update_data(broadcast_repeat_count=n)
    if n<=1:
        await state.update_data(broadcast_repeat_interval=0); return await _broadcast_review(c.message,state,c.from_user.id)
    await state.set_state(S.broadcast_repeat_interval)
    await c.message.answer('أرسل الفاصل بين نهاية الدورة وبداية الدورة التالية بالدقائق. 0 مسموح.',reply_markup=cancel_kb())

@dp.message(S.broadcast_repeat_count)
async def broadcast_repeat_count_msg(m:Message,state:FSMContext):
    try:n=int((m.text or '').strip())
    except:return await m.answer('أرسل رقمًا صحيحًا.',reply_markup=cancel_kb())
    if n<1 or n>settings.broadcast_max_repeat_cycles:return await m.answer(f'اختر من 1 إلى {settings.broadcast_max_repeat_cycles}.',reply_markup=cancel_kb())
    await state.update_data(broadcast_repeat_count=n)
    if n==1:
        await state.update_data(broadcast_repeat_interval=0); await state.set_state(None); return await _broadcast_review(m,state,m.from_user.id)
    await state.set_state(S.broadcast_repeat_interval); await m.answer('أرسل الفاصل بين الدورات بالدقائق. 0 مسموح.',reply_markup=cancel_kb())

@dp.message(S.broadcast_repeat_interval)
async def broadcast_repeat_interval_msg(m:Message,state:FSMContext):
    try: minutes=int((m.text or '').strip())
    except:return await m.answer('أرسل عدد الدقائق كرقم صحيح.',reply_markup=cancel_kb())
    sec=minutes*60
    if sec<0:return await m.answer('المدة لا يمكن أن تكون سالبة.',reply_markup=cancel_kb())
    await state.update_data(broadcast_repeat_interval=sec); await state.set_state(None); await _broadcast_review(m,state,m.from_user.id)

async def _broadcast_review(message,state,uid):
    uid=scope_uid(uid)
    d=await state.get_data(); db=await connect()
    try:
        a=await (await db.execute('SELECT label FROM account_slots WHERE id=?',(d['broadcast_slot'],))).fetchone()
        t=await (await db.execute('SELECT name FROM message_templates WHERE id=? AND owner_id=?',(d['broadcast_template'],uid))).fetchone()
    finally: await db.close()
    target_label='المجموعات' if d['broadcast_target_type']=='group' else 'الدردشات الشخصية الحالية'; count_text='الكل' if int(d['broadcast_limit'])==0 else str(d['broadcast_limit'])
    repeat_n=int(d.get('broadcast_repeat_count',1)); repeat_min=int(d.get('broadcast_repeat_interval',0))//60
    text=f"📋 مراجعة الحملة\nالحساب: {a['label']}\nالنوع: {target_label}\nالعدد لهذه الجولة: {count_text}\nالرسالة: {t['name']}\nعدد الرسائل لكل هدف: {d['broadcast_mpt']}\nالفاصل: {d['broadcast_delay']} ثانية\nحجم الدفعة: {d['broadcast_batch_size']}\nراحة الدفعة: {int(d['broadcast_batch_rest'])//60} دقيقة\nعدد الدورات: {repeat_n}\nالفاصل بين الدورات: {repeat_min} دقيقة\n\nكل دورة تبدأ فقط بعد اكتمال الدورة السابقة، ولا يعاد الهدف داخل الدورة نفسها."
    await message.answer(text,reply_markup=ik([[InlineKeyboardButton(text='✅ إنشاء وبدء الحملة',callback_data='broadcast_confirm')],[InlineKeyboardButton(text='❌ إلغاء العملية',callback_data='cancel_action')]]))

@dp.callback_query(F.data=='broadcast_confirm')
async def broadcast_confirm(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); required={'broadcast_slot','broadcast_target_type','broadcast_template','broadcast_limit','broadcast_mpt','broadcast_delay','broadcast_batch_size','broadcast_batch_rest','broadcast_repeat_count','broadcast_repeat_interval'}
    if not required.issubset(d):return await c.answer('بيانات الحملة غير مكتملة. ابدأ من إدارة الرسائل.',show_alert=True)
    try:
        op=scope_uid(c.from_user.id); cid=await create_campaign(op,int(d['broadcast_slot']),d['broadcast_target_type'],int(d['broadcast_template']),int(d['broadcast_limit']),int(d['broadcast_mpt']),int(d['broadcast_delay']),int(d['broadcast_batch_size']),int(d['broadcast_batch_rest']),int(d['broadcast_repeat_count']),int(d['broadcast_repeat_interval']),d.get('broadcast_selected') or None)
    except Exception as e:return await c.answer(f'تعذر إنشاء الحملة: {e}',show_alert=True)
    await state.clear(); job_id=await create_job(op,'broadcast',{'campaign_id':cid})
    await c.message.answer(f'✅ تم إنشاء حملة الإرسال #{cid} والمهمة #{job_id}. تعمل بالخلفية ويمكن إلغاؤها من «9 - المهام».',reply_markup=menu(c.from_user.id))
    _spawn(_broadcast_job(job_id,op,c.message.chat.id,cid))

async def _broadcast_job(job_id:int,uid:int,chat_id:int,cid:int):
    pre=await job_signal(job_id)
    if pre:
        await set_job(job_id,'paused' if pre=='pause_requested' else 'cancelled',{})
        return
    await set_job(job_id,'running')
    try:
        rep=await run_campaign(cid,uid,job_id)
        js='paused' if rep.get('status') in {'paused','paused_rate_limit','partial'} else ('cancelled' if rep.get('status')=='cancelled' else ('failed' if rep.get('error') else 'completed'))
        await set_job(job_id,js,rep)
        kb=[]
        if int(rep.get('remaining',0))>0: kb.append([InlineKeyboardButton(text='▶️ استمرار الإرسال في بقية الأهداف',callback_data=f'campaign_continue:{cid}')])
        kb.append([InlineKeyboardButton(text='📊 حملات الإرسال',callback_data='msg_campaigns'),InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')])
        note='\n⚠️ أوقف البوت الجولة بسبب إشارة تقييد/حاول لاحقًا. لا يحاول تجاوزها؛ استكمل لاحقًا.' if rep.get('status')=='paused_rate_limit' else ''
        await bot.send_message(chat_id,f"تحديث الحملة #{cid}\nتم الإرسال في هذا التشغيل: {rep.get('sent_this_run',0)}\nفشل: {rep.get('failed_this_run',0)}\nالمتبقي في الدورة: {rep.get('remaining',0)}\nالدورة: {rep.get('cycle','-')} من {rep.get('repeat_total','-')}\nالحالة: {rep.get('status')}"+note,reply_markup=ik(kb))
    except Exception as e:
        await set_job(job_id,'failed',{'campaign_id':cid,'error':str(e)}); await bot.send_message(chat_id,f'فشلت حملة الإرسال #{cid}: {e}',reply_markup=menu(uid))

@dp.callback_query(F.data.startswith('campaign_continue:'))
async def campaign_continue(c:CallbackQuery):
    cid=int(c.data.split(':')[1]); op=scope_uid(c.from_user.id); summary=await campaign_summary(cid,op)
    if not summary:return await c.answer('الحملة غير موجودة.',show_alert=True)
    remaining=int(summary.get('counts',{}).get('pending',0))+int(summary.get('counts',{}).get('retry_later',0))
    if remaining<=0:return await c.answer('لا توجد أهداف متبقية.',show_alert=True)
    job_id=await create_job(op,'broadcast',{'campaign_id':cid}); _spawn(_broadcast_job(job_id,op,c.message.chat.id,cid))
    await c.answer(f'تم إنشاء مهمة الاستكمال #{job_id}. المتبقي قبل البدء: {remaining}.',show_alert=True)

@dp.callback_query(F.data=='msg_campaigns')
async def msg_campaigns(c:CallbackQuery):
    db=await connect()
    try: rows=await (await db.execute("SELECT c.*,a.label account_label,t.name template_name FROM broadcast_campaigns c JOIN account_slots a ON a.id=c.account_slot_id JOIN message_templates t ON t.id=c.template_id WHERE c.owner_id=? ORDER BY c.id DESC LIMIT 20",(scope_uid(c.from_user.id),))).fetchall()
    finally: await db.close()
    lines=['📊 آخر حملات الإرسال:']; kb=[]
    if not rows: lines.append('لا توجد حملات.')
    for r in rows:
        lines.append(f"#{r['id']} {r['account_label']} | {'مجموعات' if r['target_type']=='group' else 'دردشات'} | {r['template_name']} | {r['status']} | الدورة {r['current_cycle']}/{r['repeat_total']} | أُرسل {r['sent_targets']}/{r['total_targets']}")
        if int(r['sent_targets'])<int(r['total_targets']): kb.append([InlineKeyboardButton(text=f"▶️ استمرار الحملة #{r['id']}",callback_data=f"campaign_continue:{r['id']}")])
    kb.append([InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')])
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik(kb))

@dp.callback_query(F.data=='msg_directory')
async def msg_directory(c:CallbackQuery):
    db=await connect()
    try: accts=await (await db.execute("SELECT * FROM account_slots WHERE enabled=1 ORDER BY operator_id,id")).fetchall() if owner(c.from_user.id) else await (await db.execute("SELECT * FROM account_slots WHERE operator_id=? AND enabled=1 ORDER BY id",(c.from_user.id,))).fetchall()
    finally: await db.close()
    lines=['🗂️ دليل WhatsApp المحلي:']
    for a in accts:
        try:gcount=len((await provider.groups(a['provider_account_id'])).get('groups') or [])
        except Exception:gcount=0
        db=await connect()
        try:
            r=await (await db.execute("SELECT COUNT(DISTINCT CASE WHEN remote_jid LIKE '%@newsletter' THEN remote_jid END) channels, COUNT(DISTINCT CASE WHEN remote_jid NOT LIKE '%@g.us' AND remote_jid NOT LIKE '%@newsletter' AND remote_jid NOT LIKE '%@broadcast' THEN remote_jid END) chats FROM wa_messages WHERE account_slot_id=?",(a['id'],))).fetchone()
        finally: await db.close()
        lines.append(f"\n📱 {a['label']}\n👤 الدردشات: {int(r['chats'] or 0)}\n👥 المجموعات: {gcount}\n📢 القنوات المتزامنة محليًا: {int(r['channels'] or 0)}")
    await c.message.edit_text('\n'.join(lines)[:3500],reply_markup=ik([[InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')]]))

@dp.callback_query(F.data=='msg_suppression')
async def msg_suppression(c:CallbackQuery,state:FSMContext):
    db=await connect()
    try:
        count=(await (await db.execute('SELECT COUNT(*) c FROM send_suppression WHERE owner_id=?',(scope_uid(c.from_user.id),))).fetchone())['c']; rows=await (await db.execute('SELECT target_jid,reason FROM send_suppression WHERE owner_id=? ORDER BY id DESC LIMIT 20',(scope_uid(c.from_user.id),))).fetchall()
    finally: await db.close()
    text=f'🚫 قائمة عدم الإرسال — {count} هدف\n'+('\n'.join(f"{r['target_jid']} — {r['reason'] or '-'}" for r in rows) or 'لا توجد عناصر.')
    await c.message.edit_text(text[:3500],reply_markup=ik([[InlineKeyboardButton(text='➕ إضافة JID إلى عدم الإرسال',callback_data='msg_suppression_add')],[InlineKeyboardButton(text='⬅️ إدارة الرسائل',callback_data='messages')]]))

@dp.callback_query(F.data=='msg_suppression_add')
async def msg_suppression_add(c:CallbackQuery,state:FSMContext):
    await state.set_state(S.suppression_add); await c.message.answer('أرسل JID للمجموعة أو الدردشة التي لا تريد أن تدخل في حملات الإرسال. يمكنك أخذ JID المجموعة من «عرض المجموعات».',reply_markup=cancel_kb())

@dp.message(S.suppression_add)
async def msg_suppression_add_msg(m:Message,state:FSMContext):
    jid=(m.text or '').strip()
    if '@' not in jid:return await m.answer('JID غير صالح.',reply_markup=cancel_kb())
    db=await connect()
    try: await db.execute('INSERT OR IGNORE INTO send_suppression(owner_id,target_jid,reason,created_at) VALUES(?,?,?,?)',(scope_uid(m.from_user.id),jid,'manual_exclusion',now_iso())); await db.commit()
    finally: await db.close()
    await state.clear(); await m.answer('تمت إضافته إلى قائمة عدم الإرسال.',reply_markup=menu(m.from_user.id))

async def main():
    if not settings.bot_token or not settings.owner_id: raise SystemExit('BOT_TOKEN and OWNER_ID are required')
    await init_db()
    await refresh_principals()
    await ensure_alert_rules(settings.owner_id)
    await list_telegram_sessions(settings.owner_id)
    await recover_interrupted_jobs()
    sync_task=asyncio.create_task(worker_forever(bot))
    telegram_task=asyncio.create_task(telegram_sync_forever(bot))
    scheduler_task=asyncio.create_task(scheduler_forever(bot))
    try: await dp.start_polling(bot)
    finally:
        sync_task.cancel(); telegram_task.cancel(); scheduler_task.cancel()
        try: await provider.close()
        except Exception: pass
