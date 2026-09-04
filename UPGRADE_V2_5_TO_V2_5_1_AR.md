# الترقية من V2.5 إلى V2.5.1

هذه النسخة تعالج تعطل صفحة مصادر Telegram عند استخدام قاعدة بيانات انتقلت من V2.4، وتعرض مستلمي الدردشات الحالية بالأسماء/الأرقام، وتصلح selfcheck لـ Baileys.

1. أوقف V2.5 وخذ نسخة احتياطية من `.env` و`data`.
2. فك V2.5.1 في مجلد جديد.
3. انسخ `.env` و`data` من V2.5 إلى المجلد الجديد.
4. أنشئ `.venv` وثبت `requirements.txt` وثبت provider بـ `npm install --omit=dev`.
5. شغل `./botctl.sh selfcheck` ثم `./botctl.sh start`.

عند أول تشغيل، `init_db()` يكتشف جدول `telegram_sources` القديم تلقائيًا، وينقله إلى مخطط V2.5.1، ويترك نسخة أمان داخل قاعدة البيانات باسم `telegram_sources_v24_legacy`.
