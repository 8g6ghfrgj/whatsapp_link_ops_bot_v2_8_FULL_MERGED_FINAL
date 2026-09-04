# تثبيت V2.6 FULL MERGED FINAL - INSTALL FIXED على Termux

هذه التعليمات مبنية على البيئة التي تم التحقق منها عمليًا:

- Python 3.12 أو 3.13 أو 3.14 (تم التحقق عمليًا من Python 3.14.4)
- Node.js 22 LTS (تم التحقق من v22.23.2)
- npm 10.x (تم التحقق من 10.9.8)
- Git مطلوب لتثبيت بعض تبعيات Baileys

## 1) داخل Termux

```bash
pkg update -y
pkg upgrade -y
pkg install -y proot-distro tmux
termux-setup-storage
proot-distro install ubuntu
proot-distro login ubuntu
```

## 2) داخل Ubuntu

```bash
apt update
apt upgrade -y
apt install -y python3 python3-venv python3-pip python3-dev build-essential libffi-dev libssl-dev curl ca-certificates gnupg unzip nano sqlite3 git
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
```

تحقق:

```bash
python3 --version
node --version
npm --version
git --version
```

## 3) فك النسخة

ضع ملف ZIP في Downloads ثم:

```bash
mkdir -p /root/whatsapp-bot
cd /root/whatsapp-bot
ZIP="$(find /sdcard/Download -maxdepth 1 -type f -name '*FULL_MERGED_FINAL_INSTALL_FIXED*.zip' -print -quit)"
test -n "$ZIP" || { echo 'BOT ZIP NOT FOUND'; exit 1; }
unzip -o "$ZIP" -d /root/whatsapp-bot/
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_6_FULL_MERGED_FINAL
```


## تثبيت تلقائي اختياري داخل Ubuntu

بعد فك ZIP والدخول إلى مجلد البوت يمكنك بدل الخطوات 4 و5 تنفيذ:

```bash
chmod +x install-ubuntu.sh
./install-ubuntu.sh
```

ثم عدّل `.env` وشغّل `selfcheck` و`dbcheck` قبل البدء.

## 4) Python

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
python -c "import aiogram,aiohttp,aiosqlite,telethon,qrcode; print('PYTHON DEPENDENCIES OK')"
```

## 5) Node / Baileys

```bash
cd provider
rm -rf node_modules
npm cache verify
npm install --omit=dev
node -e "import('@whiskeysockets/baileys').then(()=>console.log('BAILEYS OK')).catch(e=>{console.error(e);process.exit(1)})"
node --check server.mjs
node --check message_text.mjs
cd ..
```

## 6) الإعدادات

```bash
cp .env.example .env
chmod 600 .env
python - <<'PY'
from pathlib import Path
import secrets,re
p=Path('.env'); s=p.read_text(); token=secrets.token_hex(32)
if re.search(r'^WA_PROVIDER_TOKEN=',s,flags=re.M):
    s=re.sub(r'^WA_PROVIDER_TOKEN=.*$',f'WA_PROVIDER_TOKEN={token}',s,flags=re.M)
else:
    s += f'\nWA_PROVIDER_TOKEN={token}\n'
p.write_text(s)
print('WA_PROVIDER_TOKEN GENERATED')
PY
nano .env
```

أدخل على الأقل `BOT_TOKEN` و`OWNER_ID`. لا تشارك `.env` أو التوكنات.

## 7) الفحص

```bash
chmod +x botctl.sh start.sh start-bot.sh start-provider.sh
./botctl.sh versioncheck
./botctl.sh selfcheck
./botctl.sh dbcheck
```

المطلوب في الفحص: `SELF-CHECK PASSED`، وسلامة قاعدة البيانات `ok`.

## 8) التشغيل في الخلفية

```bash
./botctl.sh start
./botctl.sh status
```

`botctl.sh start` يشغل Provider والبوت باستخدام `nohup` في الخلفية.

## 9) ثبات أفضل على Android

اخرج من Ubuntu:

```bash
exit
termux-wake-lock
```

ويمكن استخدام tmux:

```bash
tmux new -s whatsapp
proot-distro login ubuntu
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_6_FULL_MERGED_FINAL
source .venv/bin/activate
./botctl.sh status
```

لفصل tmux بدون إيقافه: `Ctrl+B` ثم `D`.
