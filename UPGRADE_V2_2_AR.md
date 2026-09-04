# ترقية V2.1 إلى V2.2 على Termux

داخل Ubuntu:
```bash
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_1
source .venv/bin/activate
./botctl.sh stop
cp .env /root/wa_v21_env.backup
rm -rf /root/wa_v21_data.backup
cp -a data /root/wa_v21_data.backup

cd /root/whatsapp-bot
unzip -o /sdcard/Download/whatsapp_link_ops_bot_v2_2_message_manager.zip
cd /root/whatsapp-bot/whatsapp_link_ops_bot_v2_2
cp /root/wa_v21_env.backup .env
rm -rf data
cp -a /root/wa_v21_data.backup data
sed -i 's/^INSTANCE_NAME=.*/INSTANCE_NAME=WhatsApp Link Ops V2.2 QR/' .env

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
cd provider
npm install
cd ..
./botctl.sh selfcheck
./botctl.sh start
./botctl.sh status
```

إذا ظهر أي `FAIL` في selfcheck لا تشغل المهام قبل مراجعة الخطأ.
