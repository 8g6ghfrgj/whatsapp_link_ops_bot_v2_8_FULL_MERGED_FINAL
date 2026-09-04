# ترقية V2.2 إلى V2.3 FINAL

أوقف V2.2 وخذ نسخة من `.env` و`data/` بالكامل، ثم فك V2.3 في مجلد جديد وانسخ `.env` و`data/` إليه. عند أول تشغيل `init_db()` يضيف أعمدة دورات الإرسال وجدول `audit_inputs` تلقائيًا. لا تحذف `data/wa_provider/` لأنه يحتوي جلسات QR المرتبطة.

بعد النقل شغّل:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -r requirements.txt
cd provider && npm install && cd ..
./botctl.sh selfcheck
./botctl.sh start
./botctl.sh status
```
