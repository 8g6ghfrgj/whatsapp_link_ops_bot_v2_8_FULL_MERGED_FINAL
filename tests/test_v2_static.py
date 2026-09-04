from pathlib import Path
R=Path(__file__).resolve().parents[1]
checks={
 'QR provider': ('provider/server.mjs','useMultiFileAuthState'),
 'multi account isolation': ('app/db.py','provider_account_id'),
 'real group listing': ('provider/server.mjs','groupFetchAllParticipating'),
 'join provider': ('provider/server.mjs','groupAcceptInvite'),
 'message history sync': ('provider/server.mjs','messaging-history.set'),
 'message live sync': ('provider/server.mjs','messages.upsert'),
 'high water collection': ('app/db.py','collection_cursors'),
 'global dedupe': ('app/db.py','normalized_url TEXT NOT NULL UNIQUE'),
 'expired registry': ('app/db.py','expired_registry'),
 '10 then hour defaults': ('.env.example','JOIN_BATCH_REST_SECONDS=3600'),
 'permission protected export': ('app/services/permissions.py',"'export_'"),
 'web audit': ('app/services/web_auditor.py','inspect_many'),
}
for name,(f,s) in checks.items():
    t=(R/f).read_text(encoding='utf-8')
    assert s in t, name
    print('OK',name)
print('V2 STATIC TESTS PASSED')
