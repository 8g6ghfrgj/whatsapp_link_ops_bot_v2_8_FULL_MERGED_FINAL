from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def text(p): return (ROOT/p).read_text(encoding='utf-8')

def check(name, cond):
    if not cond: raise AssertionError(name)
    print('OK  ',name)

def main():
    db=text('app/db.py'); col=text('app/services/collector.py'); bot=text('app/bot.py'); kb=text('app/keyboards.py'); provider=text('provider/server.mjs'); broadcast=text('app/services/broadcast.py')
    check('ignored registry schema', 'ignored_registry' in db)
    check('ignored registry blocks collector', 'ignored_registry' in col)
    check('ignored registry UI', "F.data=='ignored_import'" in bot)
    check('restricted supervisor menu', 'supervisor and not owner' in kb)
    check('permission-aware supervisor middleware', 'AccessMiddleware' in bot and 'لا تملك صلاحية هذه الوظيفة' in bot)
    check('message manager button', "F.data=='messages'" in bot)
    check('campaign tables', 'broadcast_campaigns' in db and 'broadcast_targets' in db)
    check('campaign continuation', 'استمرار الإرسال في بقية الأهداف' in bot)
    check('provider send endpoint', '/messages/send' in provider and 'sendMessage(jid,{text})' in provider)
    check('rate-limit pause', 'paused_rate_limit' in broadcast and 'retry_later' in broadcast)
    check('chat group channel directory', "F.data=='msg_directory'" in bot and '@newsletter' in bot and '@g.us' in bot)
    check('suppression list', 'send_suppression' in db and "F.data=='msg_suppression'" in bot)
    check('owner global account pool', 'operator_id == settings.owner_id' in col and 'owner_id==settings.owner_id' in broadcast)
    print('V2.2 STATIC CHECK PASSED')
if __name__=='__main__': main()
