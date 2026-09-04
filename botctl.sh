#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/logs
BPID=data/bot.pid
WPID=data/provider.pid
BLOG=data/logs/bot.log
WLOG=data/logs/provider.log
alive(){ [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
versioncheck(){
  python - <<'PY'
import sys
v=sys.version_info[:2]
if v not in {(3,12),(3,13),(3,14)}:
    raise SystemExit(f"Unsupported Python {sys.version.split()[0]}; use Python 3.12, 3.13 or 3.14")
print("PYTHON OK",sys.version.split()[0])
PY
  node - <<'JS'
const major=Number(process.versions.node.split('.')[0]);
if(major!==22){console.error(`Unsupported Node ${process.version}; use Node 22 LTS`);process.exit(1)}
console.log('NODE OK',process.version)
JS
  echo "NPM $(npm --version)"
  command -v git >/dev/null 2>&1 || { echo 'GIT MISSING: install with apt install -y git'; return 1; }
  echo "GIT OK $(git --version)"
}
case "${1:-}" in
 start)
   versioncheck
   if ! alive "$WPID"; then nohup ./start-provider.sh >>"$WLOG" 2>&1 & echo $! >"$WPID"; fi
   sleep 2
   if ! alive "$WPID"; then echo "WhatsApp provider failed; see $WLOG"; exit 1; fi
   if ! alive "$BPID"; then nohup ./start-bot.sh >>"$BLOG" 2>&1 & echo $! >"$BPID"; fi
   sleep 2
   if alive "$BPID"; then echo "provider PID $(cat "$WPID")"; echo "bot PID $(cat "$BPID")"; else echo "bot failed; see $BLOG"; exit 1; fi;;
 stop)
   if alive "$BPID"; then kill "$(cat "$BPID")" 2>/dev/null || true; fi
   if alive "$WPID"; then kill "$(cat "$WPID")" 2>/dev/null || true; fi
   rm -f "$BPID" "$WPID"; echo stopped;;
 restart) "$0" stop; sleep 1; "$0" start;;
 status)
   if alive "$WPID"; then echo "provider running PID $(cat "$WPID")"; else echo "provider stopped"; fi
   if alive "$BPID"; then echo "bot running PID $(cat "$BPID")"; else echo "bot stopped"; fi;;
 logs) tail -f "$WLOG" "$BLOG";;
 versioncheck) versioncheck;;
 selfcheck)
   versioncheck
   python -m compileall -q app tests
   node --check provider/server.mjs
   node --check provider/message_text.mjs
   (cd provider && node -e "import('@whiskeysockets/baileys').then(()=>console.log('BAILEYS OK')).catch(e=>{console.error(e);process.exit(1)})")
   node provider/test_message_text.mjs
   PYTHONPATH=. python tests/test_static.py
   PYTHONPATH=. python tests/test_v2_static.py
   PYTHONPATH=. python tests/test_v2_1_static.py
   PYTHONPATH=. python tests/test_v2_2_static.py
   PYTHONPATH=. python tests/test_v2_3_static.py
   PYTHONPATH=. python tests/test_v2_4_static.py
   PYTHONPATH=. python tests/test_v2_5_static.py
   PYTHONPATH=. python tests/test_v2_6_static.py
   PYTHONPATH=. python tests/test_v2_7_static.py
   PYTHONPATH=. python tests/test_v2_7_db.py
   PYTHONPATH=. python tests/test_v2_8_static.py
   PYTHONPATH=. python tests/test_v2_8_db.py
   PYTHONPATH=. python tests/test_v2_8_join_worker.py
   echo "SELF-CHECK PASSED";;
 backup)
   python - <<'PY'
import asyncio
from app.db import init_db
from app.services.system_tools import create_safe_backup_zip
async def x():
 await init_db(); print(await create_safe_backup_zip())
asyncio.run(x())
PY
   ;;
 fullbackup)
   python - <<'PY'
import asyncio
from app.db import init_db
from app.services.system_tools import create_local_full_backup
async def x():
 await init_db(); print(await create_local_full_backup())
asyncio.run(x())
PY
   ;;
 diag)
   python - <<'PY'
import asyncio
from app.db import init_db
from app.services.system_tools import write_diagnostics_file
async def x():
 await init_db(); print(await write_diagnostics_file())
asyncio.run(x())
PY
   ;;
 dbcheck)
   python - <<'PY'
import asyncio,json
from app.db import init_db
from app.services.system_tools import database_health
async def x():
 await init_db(); print(json.dumps(await database_health(),ensure_ascii=False,indent=2))
asyncio.run(x())
PY
   ;;
 restore-db)
   if alive "$BPID" || alive "$WPID"; then echo "Stop the bot first: ./botctl.sh stop"; exit 1; fi
   [ -n "${2:-}" ] || { echo "usage: $0 restore-db /path/to/whatsapp_ops.db"; exit 2; }
   python - "$2" <<'PY'
import sys
from app.services.system_tools import restore_db_file
print(restore_db_file(sys.argv[1]))
PY
   ;;
 *) echo "usage: $0 {start|stop|restart|status|logs|versioncheck|selfcheck|backup|fullbackup|diag|dbcheck|restore-db PATH}"; exit 2;;
esac
