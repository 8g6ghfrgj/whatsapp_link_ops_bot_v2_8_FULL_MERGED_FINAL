# تحقق V2.6 FULL MERGED FINAL

- Python syntax/compileall: PASS على Python 3.13.5.
- Node syntax: PASS على v22.16.0.
- Bash syntax: PASS.
- اختبارات الإرث V2.0 وV2.1 وV2.2 وV2.3 وV2.4 وV2.5: PASS.
- اختبار V2.6: PASS.
- Coverage merge: جميع ملفات المصدر غير المؤقتة الموجودة في V2.5 وV2.5.1 وV2.6 موجودة في الإصدار الموحد.
- ملفات __pycache__ و*.pyc مستبعدة عمدًا لأنها Runtime cache وليست مصدرًا.

> ملاحظة: اختبار تثبيت الحزم من الإنترنت ليس جزءًا من هذا التحقق المحلي. استخدم requirements.txt وprovider/package.json ثم ./botctl.sh selfcheck على جهاز التشغيل بعد npm/pip install.
