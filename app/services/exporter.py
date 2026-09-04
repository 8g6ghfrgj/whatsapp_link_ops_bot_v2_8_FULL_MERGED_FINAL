from __future__ import annotations
import os, zipfile, json
from datetime import datetime
from ..config import settings
from ..db import connect

async def export_links_zip() -> str:
    os.makedirs(settings.export_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(settings.export_dir, f"links_{stamp}")
    os.makedirs(folder, exist_ok=True)
    db = await connect()
    try:
        cur = await db.execute("SELECT category,display_name,original_url FROM links ORDER BY category,id")
        handles = {}
        try:
            async for r in cur:
                cat = r["category"] or "other"
                if cat not in handles:
                    handles[cat] = open(os.path.join(folder, f"{cat}.txt"), "w", encoding="utf-8")
                h = handles[cat]
                if r["display_name"]:
                    h.write(f"الاسم: {r['display_name']}\n")
                h.write(f"الرابط: {r['original_url']}\n\n")
        finally:
            for h in handles.values(): h.close()
        # V2.5 semantic sections keep global link dedupe while allowing one link in several sections.
        cur2 = await db.execute('''SELECT ls.section,l.display_name,l.original_url FROM link_sections ls JOIN links l ON l.id=ls.link_id ORDER BY ls.section,l.id''')
        sh={}
        try:
            async for r in cur2:
                sec=r['section'] or 'important'
                if sec not in sh: sh[sec]=open(os.path.join(folder,f'section_{sec}.txt'),'w',encoding='utf-8')
                h=sh[sec]
                if r['display_name']: h.write(f"الاسم: {r['display_name']}\n")
                h.write(f"الرابط: {r['original_url']}\n\n")
        finally:
            for h in sh.values(): h.close()
        for table,name in [('expired_registry','section_expired.txt'),('ignored_registry','section_ignored.txt')]:
            rows=await (await db.execute(f'SELECT normalized_url,reason,source FROM {table} ORDER BY created_at')).fetchall()
            with open(os.path.join(folder,name),'w',encoding='utf-8') as h:
                for r in rows:
                    h.write(f"الرابط: {r['normalized_url']}\n")
                    if r['reason']: h.write(f"السبب: {r['reason']}\n")
                    if r['source']: h.write(f"المصدر: {r['source']}\n")
                    h.write('\n')
    finally:
        await db.close()
    zpath = os.path.join(settings.export_dir, f"links_{stamp}.zip")
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(folder):
            z.write(os.path.join(folder,name), arcname=name)
    return zpath

async def export_audit_zip(audit_id: int) -> str:
    os.makedirs(settings.export_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(settings.export_dir, f"audit_{audit_id}_{stamp}")
    os.makedirs(folder, exist_ok=True)
    db = await connect()
    try:
        cur = await db.execute("SELECT status,display_name,normalized_url,details FROM audit_results WHERE audit_id=? ORDER BY status,id", (audit_id,))
        handles = {}
        try:
            async for r in cur:
                st = r["status"]
                if st not in handles:
                    handles[st] = open(os.path.join(folder, f"{st}.txt"), "w", encoding="utf-8")
                h = handles[st]
                if r["display_name"]:
                    h.write(f"الاسم: {r['display_name']}\n")
                h.write(f"الرابط: {r['normalized_url']}\n")
                if r["details"]:
                    h.write(f"معلومات: {r['details']}\n")
                h.write("\n")
        finally:
            for h in handles.values(): h.close()
    finally:
        await db.close()
    zpath = os.path.join(settings.export_dir, f"audit_{audit_id}_{stamp}.zip")
    with zipfile.ZipFile(zpath,"w",zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(folder):
            z.write(os.path.join(folder,name), arcname=name)
    return zpath

async def export_join_zip(operator_id: int) -> str:
    os.makedirs(settings.export_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    folder = os.path.join(settings.export_dir, f'join_{operator_id}_{stamp}')
    os.makedirs(folder, exist_ok=True)
    db = await connect()
    try:
        cur = await db.execute('''SELECT ja.status,a.label account_label,l.display_name,l.original_url,ja.last_error
          FROM join_attempts ja JOIN links l ON l.id=ja.link_id JOIN account_slots a ON a.id=ja.account_slot_id
          WHERE ja.operator_id=? ORDER BY ja.status,a.id,ja.id''',(operator_id,))
        handles={}
        try:
            async for r in cur:
                st=r['status'] or 'unknown'
                if st not in handles: handles[st]=open(os.path.join(folder,f'{st}.txt'),'w',encoding='utf-8')
                h=handles[st]
                h.write(f"الحساب: {r['account_label']}\n")
                if r['display_name']: h.write(f"الاسم: {r['display_name']}\n")
                h.write(f"الرابط: {r['original_url']}\n")
                if r['last_error']: h.write(f"معلومة: {r['last_error']}\n")
                h.write('\n')
        finally:
            for h in handles.values(): h.close()
    finally: await db.close()
    zpath=os.path.join(settings.export_dir,f'join_{operator_id}_{stamp}.zip')
    with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(folder): z.write(os.path.join(folder,name),arcname=name)
    return zpath


async def export_job_report(job_id: int, operator_id: int) -> str | None:
    """Create a complete UTF-8 task report that can be downloaded from the bot."""
    os.makedirs(settings.export_dir,exist_ok=True)
    db=await connect()
    try:
        row=await (await db.execute('SELECT * FROM jobs WHERE id=? AND operator_id=?',(job_id,operator_id))).fetchone()
    finally:
        await db.close()
    if not row:
        return None
    try: payload=json.loads(row['payload_json'] or '{}')
    except Exception: payload={}
    try: report=json.loads(row['report_json'] or '{}')
    except Exception: report={}
    path=os.path.join(settings.export_dir,f'job_{job_id}_report.txt')
    with open(path,'w',encoding='utf-8') as handle:
        handle.write(f"تقرير المهمة #{job_id}\n")
        handle.write(f"النوع: {row['kind']}\nالحالة: {row['status']}\n")
        handle.write(f"وقت الإنشاء: {row['created_at']}\nآخر تحديث: {row['updated_at']}\n\n")
        handle.write('إعدادات المهمة:\n')
        handle.write(json.dumps(payload,ensure_ascii=False,indent=2))
        handle.write('\n\nالنتيجة الكاملة:\n')
        handle.write(json.dumps(report,ensure_ascii=False,indent=2))
        handle.write('\n')
    return path
