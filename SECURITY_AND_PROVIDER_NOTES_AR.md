# ملاحظات الأمان والموصل

- لا تشارك `BOT_TOKEN` أو `WA_PROVIDER_TOKEN` أو `.env`.
- لا تشارك QR الخاص بربط WhatsApp أو محتويات `data/wa_provider/`.
- إذا استخدمت Telethon، ملف `data/telegram_user.session` يعطي جلسة Telegram حساسة؛ لا ترفعه ولا ترسله لأي شخص.
- أدخل كود تسجيل Telegram وكلمة مرور 2FA محليًا في Termux عبر `telegram_session_login.py`، وليس في محادثة البوت.
- Baileys يعتمد WhatsApp Web/Multi-Device وليس WhatsApp Cloud API الرسمية.
- مقدار تاريخ WhatsApp الذي تتم مزامنته يحدده WhatsApp؛ `syncFullHistory` لا يضمن تاريخًا غير محدود.
- Telegram Bot API يلتقط المنشورات/الرسائل الجديدة التي تصل للبوت ولا يوفر تاريخ القناة القديم بشكل عام؛ Telethon الاختياري هو مسار الاستيراد القديم.
- عند Rate Limit أو Try Later لا يحاول المشروع تجاوز تقييد WhatsApp.
