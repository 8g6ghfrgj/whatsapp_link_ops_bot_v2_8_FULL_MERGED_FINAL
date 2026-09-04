from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu(owner=False, supervisor=False, principal=False):
    # Registry supervisors intentionally receive only the three maintenance areas.
    if supervisor and not owner and not principal:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='📱 حساباتي WhatsApp',callback_data='accounts')],
            [InlineKeyboardButton(text='⛔ سجل الروابط المنتهية',callback_data='expired_import')],
            [InlineKeyboardButton(text='🗑 سجل الروابط المهمشة',callback_data='ignored_import')],
        ])
    rows=[
      [InlineKeyboardButton(text='📱 الحسابات',callback_data='accounts'), InlineKeyboardButton(text='🚀 لوحة التحكم',callback_data='dashboard')],
      [InlineKeyboardButton(text='🔗 الروابط والتجميع',callback_data='links_hub')],
      [InlineKeyboardButton(text='🔍 الفحص والانضمام',callback_data='audit_join_hub')],
      [InlineKeyboardButton(text='📨 إدارة الرسائل',callback_data='messages')],
      [InlineKeyboardButton(text='📋 المهام والتقارير',callback_data='tasks_hub')],
      [InlineKeyboardButton(text='👁 المراقبة المباشرة',callback_data='watches')],
      [InlineKeyboardButton(text='⚙️ الإدارة والنظام',callback_data='system_hub')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⬅️ الرئيسية',callback_data='home')]])
