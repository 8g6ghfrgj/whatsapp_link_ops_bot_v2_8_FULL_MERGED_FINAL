# الترقية من V2.1 إلى V2.4 FINAL

أوقف V2.1 وخذ نسخة من `.env` و`data/` كاملة، ثم فك V2.4 في مجلد جديد واسترجعهما. لا تحذف `data/wa_provider/` لأنه يحتوي جلسات QR.

بعد استرجاع `.env` عدّل اسم النسخة والقيم الاختيارية:

```bash
sed -i 's|^INSTANCE_NAME=.*|INSTANCE_NAME="WhatsApp Link Ops V2.4 FINAL QR"|' .env
sed -i 's/^BROADCAST_MAX_MESSAGES_PER_TARGET=.*/BROADCAST_MAX_MESSAGES_PER_TARGET=100/' .env
sed -i 's/^BROADCAST_MIN_GROUP_DELAY_SECONDS=.*/BROADCAST_MIN_GROUP_DELAY_SECONDS=0/' .env
sed -i 's/^BROADCAST_MIN_CHAT_DELAY_SECONDS=.*/BROADCAST_MIN_CHAT_DELAY_SECONDS=0/' .env
sed -i 's/^BROADCAST_MIN_BATCH_REST_SECONDS=.*/BROADCAST_MIN_BATCH_REST_SECONDS=0/' .env
sed -i 's/^BROADCAST_MIN_REPEAT_INTERVAL_SECONDS=.*/BROADCAST_MIN_REPEAT_INTERVAL_SECONDS=0/' .env
```

ثم ثبت Python وNode وشغل `./botctl.sh selfcheck`. يجب أن يظهر `BAILEYS OK` ثم `SELF-CHECK PASSED`.
