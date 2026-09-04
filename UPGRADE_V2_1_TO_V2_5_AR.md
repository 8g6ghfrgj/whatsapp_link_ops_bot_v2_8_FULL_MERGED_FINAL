# الترقية المباشرة من V2.1 إلى V2.5 FINAL

هذه الخطوات مخصصة لمسار التشغيل الحالي في Ubuntu/Termux. الهدف هو الاحتفاظ بـ `.env` وقاعدة SQLite وملفات جلسة WhatsApp QR.

## 1. أوقف V2.1 وخذ نسخة احتياطية

```bash
proot-distro login ubuntu
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_1
source .venv/bin/activate
./botctl.sh stop

cp .env /root/wa_v21_env.backup
rm -rf /root/wa_v21_data.backup
cp -a data /root/wa_v21_data.backup
```

لا تحذف `data/wa_provider/`؛ هو الذي يحتوي جلسة الأجهزة المرتبطة.

## 2. فك V2.5

```bash
cd /root/whatsapp-bot
unzip -o /sdcard/Download/whatsapp_link_ops_bot_v2_5_final.zip
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_5_final
```

## 3. استرجع الإعدادات والبيانات

```bash
cp /root/wa_v21_env.backup .env
chmod 600 .env
rm -rf data
cp -a /root/wa_v21_data.backup data
sed -i 's|^INSTANCE_NAME=.*|INSTANCE_NAME="WhatsApp Link Ops V2.5 FINAL QR"|' .env
```

أضف إعدادات V2.5 إن لم تكن موجودة:

```bash
grep -q '^BROADCAST_MAX_MESSAGES_PER_TARGET=' .env || echo 'BROADCAST_MAX_MESSAGES_PER_TARGET=100' >> .env
grep -q '^BROADCAST_MIN_GROUP_DELAY_SECONDS=' .env || echo 'BROADCAST_MIN_GROUP_DELAY_SECONDS=0' >> .env
grep -q '^BROADCAST_MIN_CHAT_DELAY_SECONDS=' .env || echo 'BROADCAST_MIN_CHAT_DELAY_SECONDS=0' >> .env
grep -q '^BROADCAST_MIN_BATCH_REST_SECONDS=' .env || echo 'BROADCAST_MIN_BATCH_REST_SECONDS=0' >> .env
grep -q '^BROADCAST_MAX_REPEAT_CYCLES=' .env || echo 'BROADCAST_MAX_REPEAT_CYCLES=100' >> .env
grep -q '^BROADCAST_MIN_REPEAT_INTERVAL_SECONDS=' .env || echo 'BROADCAST_MIN_REPEAT_INTERVAL_SECONDS=0' >> .env
grep -q '^TELEGRAM_API_ID=' .env || echo 'TELEGRAM_API_ID=0' >> .env
grep -q '^TELEGRAM_API_HASH=' .env || echo 'TELEGRAM_API_HASH=' >> .env
grep -q '^TELEGRAM_SESSION_PATH=' .env || echo 'TELEGRAM_SESSION_PATH=data/telegram_user' >> .env
```

## 4. ثبّت Python

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
```

## 5. ثبّت Node/Baileys

```bash
cd provider
rm -rf node_modules
npm cache verify
npm install --omit=dev
cd ..
```

اختبار Baileys:

```bash
node -e "import('@whiskeysockets/baileys').then(()=>console.log('BAILEYS OK')).catch(e=>{console.error(e);process.exit(1)})"
```

## 6. الفحص والتشغيل

```bash
chmod +x botctl.sh start.sh start-bot.sh start-provider.sh
./botctl.sh selfcheck
./botctl.sh start
./botctl.sh status
```

المفروض يظهر:

```text
provider running PID ...
bot running PID ...
```

## 7. Telethon — اختياري فقط للتاريخ القديم

المراقبة الجديدة عبر Bot API لا تحتاج Telethon. إذا أردت استيراد الرسائل القديمة من قنوات/مجموعات Telegram أضف API ID وAPI Hash إلى `.env` ثم:

```bash
source .venv/bin/activate
python telegram_session_login.py
```

أدخل كود Telegram و2FA في Termux فقط. لا تشارك ملف `data/telegram_user.session` مع أي شخص.

## 8. لا تحذف V2.1 مباشرة
احتفظ بمجلد V2.1 ونسخة `/root/wa_v21_data.backup` إلى أن تتأكد من اتصال الحساب وتشغيل المهام في V2.5.
