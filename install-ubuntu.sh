#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

apt update
apt install -y python3 python3-venv python3-pip python3-dev build-essential libffi-dev libssl-dev curl ca-certificates gnupg unzip nano sqlite3 git

if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" != "22" ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt install -y nodejs
fi

rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
python -c "import aiogram,aiohttp,aiosqlite,telethon,qrcode; print('PYTHON DEPENDENCIES OK')"

cd provider
rm -rf node_modules
npm cache verify
npm install --omit=dev
node -e "import('@whiskeysockets/baileys').then(()=>console.log('BAILEYS OK')).catch(e=>{console.error(e);process.exit(1)})"
node --check server.mjs
node --check message_text.mjs
cd ..

chmod +x botctl.sh start.sh start-bot.sh start-provider.sh install-ubuntu.sh

if [ ! -f .env ]; then
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
PY
  echo '.env created. Edit BOT_TOKEN and OWNER_ID before starting.'
fi

./botctl.sh versioncheck
printf '\nINSTALLATION COMPLETE\nNext: nano .env\nThen: ./botctl.sh selfcheck && ./botctl.sh dbcheck && ./botctl.sh start\n'
