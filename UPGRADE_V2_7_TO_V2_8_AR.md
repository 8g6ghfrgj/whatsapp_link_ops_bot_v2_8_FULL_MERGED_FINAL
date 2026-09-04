# الترقية من V2.7 إلى V2.8

1. أوقف البوت وخذ نسخة احتياطية من `.env` و`data/`.
2. استبدل ملفات البرنامج بملفات V2.8، مع إبقاء `.env` و`data/wa_provider/` و`data/telethon/` و`data/whatsapp_ops.db` والجلسة القديمة.
3. راجع قيم `JOIN_SAFE_*` و`MESSAGE_RETENTION_DAYS` في `.env.example`. عدم إضافتها إلى `.env` يستخدم القيم الآمنة الافتراضية.
4. شغّل `./install-ubuntu.sh` عند الحاجة ثم `./botctl.sh selfcheck`.
5. شغّل البوت. `init_db()` يضيف الجداول الجديدة دون حذف الجداول القديمة.

لا تنسخ `.env.example` فوق `.env` الحقيقي.
