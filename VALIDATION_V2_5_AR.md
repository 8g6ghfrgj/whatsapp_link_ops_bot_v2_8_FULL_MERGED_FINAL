# تحقق V2.5 FINAL

تم التحقق قبل التغليف من:

- Python compile لكل `app/` والاختبارات وسكربت تسجيل جلسة Telegram.
- Node syntax لـ `provider/server.mjs` و`provider/message_text.mjs`.
- اختبار قارئ نصوص رسائل WhatsApp المتداخلة: ناجح.
- اختبارات التوافق V2.0 وV2.1 وV2.2 وV2.3 وV2.4: ناجحة.
- اختبارات V2.5: المشرف الرئيسي، صلاحيات المالك، الأقسام الخمسة، `link_sections`، استقبال channel/group، Telethon الاختياري، cursor التاريخ، والانضمام حسب القسم والحساب: ناجحة.
- إنشاء مخطط SQLite V2.5 واختبار أعمدة `telegram_sources` و`link_sections`: ناجح.
- `botctl.sh selfcheck` يتضمن أيضًا Runtime import لـ Baileys بعد تنفيذ `npm install`; لا يتم تضمين `node_modules` داخل ZIP عمدًا.

الحزمة النهائية لا تتضمن `.env` حقيقيًا أو SQLite DB أو WhatsApp QR sessions أو Telegram `.session` أو `.venv` أو `node_modules`.
