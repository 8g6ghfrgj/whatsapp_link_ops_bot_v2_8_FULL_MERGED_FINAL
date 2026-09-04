from __future__ import annotations

import os, platform, shutil, sqlite3, subprocess, sys, zipfile
from datetime import datetime
from pathlib import Path
from ..config import settings
from ..db import backup_db, connect


def _stamp(): return datetime.now().strftime('%Y%m%d_%H%M%S')


def _safe_version(cmd):
    try:return subprocess.check_output(cmd,stderr=subprocess.STDOUT,text=True,timeout=5).strip()
    except Exception as e:return f'unavailable ({e.__class__.__name__})'


async def database_health():
    db=await connect()
    try:
        integrity=(await (await db.execute('PRAGMA integrity_check')).fetchone())[0]
        page_count=(await (await db.execute('PRAGMA page_count')).fetchone())[0]
        page_size=(await (await db.execute('PRAGMA page_size')).fetchone())[0]
        tables=(await (await db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'" )).fetchone())[0]
        return {'integrity':integrity,'tables':int(tables),'bytes':int(page_count)*int(page_size)}
    finally: await db.close()


async def create_safe_backup_zip():
    """Create a share-safe backup. Session credentials and .env are deliberately excluded."""
    os.makedirs(settings.backup_dir,exist_ok=True)
    db_copy=await backup_db()
    dst=os.path.join(settings.backup_dir,f'wa_ops_safe_{_stamp()}.zip')
    with zipfile.ZipFile(dst,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy,'data/whatsapp_ops.db')
        for rel in ['README_AR.md','FEATURES_V2_7_AR.md','FINAL_FEATURES_V2_5_AR.md','FIXES_V2_5_1_AR.md']:
            if os.path.exists(rel):z.write(rel,rel)
        z.writestr('BACKUP_NOTICE_AR.txt','هذه نسخة مشاركة آمنة: لا تحتوي .env ولا جلسات WhatsApp أو Telethon.\n')
    try: os.remove(db_copy)
    except OSError: pass
    # Rotation applies only to safe archives, never to user-created local full backups.
    files=sorted(Path(settings.backup_dir).glob('wa_ops_safe_*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)
    for old in files[settings.backup_keep_count:]:
        try:old.unlink()
        except OSError:pass
    return dst


async def create_local_full_backup():
    """Local disaster-recovery archive. Never auto-sent by the Telegram bot."""
    os.makedirs(settings.backup_dir,exist_ok=True)
    db_copy=await backup_db()
    dst=os.path.join(settings.backup_dir,f'wa_ops_local_full_{_stamp()}.zip')
    exclude_dirs={'.venv','node_modules','__pycache__','logs','backups','exports'}
    with zipfile.ZipFile(dst,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy,'data/whatsapp_ops.db')
        for base in ['data/wa_provider','data/telethon']:
            p=Path(base)
            if p.exists():
                for f in p.rglob('*'):
                    if f.is_file():z.write(f,str(f))
        legacy=Path(settings.telegram_session_path)
        legacy_candidates=[Path(str(legacy)+suffix) for suffix in ('.session','.session-journal','.session-wal','.session-shm')]
        if legacy.suffix=='.session': legacy_candidates[0]=legacy
        for f in legacy_candidates:
            if f.is_file():z.write(f,str(f))
        for root,dirs,files in os.walk('.'):
            dirs[:]=[d for d in dirs if d not in exclude_dirs and not d.startswith('.git')]
            for fn in files:
                p=Path(root)/fn
                rel=str(p).lstrip('./')
                if rel=='.env' or rel.startswith('data/'):continue
                if p.suffix in {'.pyc'}:continue
                z.write(p,rel)
        z.writestr('LOCAL_BACKUP_NOTICE_AR.txt','نسخة استعادة محلية قد تحتوي جلسات حساسة. لا ترسل هذا الملف لأي شخص. لا تحتوي ملف .env.\n')
    try: os.remove(db_copy)
    except OSError:pass
    return dst


async def diagnostics_text():
    h=await database_health()
    disk=shutil.disk_usage('.')
    lines=[
        'WhatsApp Link Ops V2.8 diagnostics',
        f'Python: {sys.version.split()[0]}',
        f'Node: {_safe_version(["node","--version"])}',
        f'npm: {_safe_version(["npm","--version"])}',
        f'Platform: {platform.platform()}',
        f'DB integrity: {h["integrity"]}',
        f'DB tables: {h["tables"]}',
        f'DB logical size: {h["bytes"]}',
        f'Disk free: {disk.free}',
        f'Disk total: {disk.total}',
        f'DB path: {settings.db_path}',
        f'Provider URL: {settings.wa_provider_url}',
        'Secrets: intentionally omitted',
    ]
    return '\n'.join(lines)+'\n'


async def write_diagnostics_file():
    os.makedirs('data/diagnostics',exist_ok=True)
    p=f'data/diagnostics/diagnostics_{_stamp()}.txt'
    Path(p).write_text(await diagnostics_text(),encoding='utf-8')
    return p


def restore_db_file(path:str):
    """Offline helper used by botctl. It validates SQLite before replacing the DB."""
    src=Path(path).expanduser().resolve()
    if not src.is_file(): raise FileNotFoundError(src)
    con=sqlite3.connect(str(src))
    try:
        result=con.execute('PRAGMA integrity_check').fetchone()[0]
        if result!='ok':raise RuntimeError(f'backup integrity check failed: {result}')
        required={'links','jobs','account_slots'}
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):raise RuntimeError('not a WhatsApp Link Ops database')
    finally: con.close()
    dst=Path(settings.db_path)
    dst.parent.mkdir(parents=True,exist_ok=True)
    if dst.exists():shutil.copy2(dst,dst.with_suffix(dst.suffix+f'.pre_restore_{_stamp()}'))
    shutil.copy2(src,dst)
    return str(dst)
