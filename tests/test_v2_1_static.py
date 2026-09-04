from pathlib import Path
from app.link_utils import extract_urls, classify_link
R=Path(__file__).resolve().parents[1]

assert extract_urls('chat.whatsapp.com/ABC')[0]=='https://chat.whatsapp.com/ABC'
assert classify_link('https://chat.whatsapp.com/ABC')=='whatsapp_group'
assert classify_link('https://whatsapp.com/channel/XYZ')=='whatsapp_channel'
assert classify_link('https://x.com/openai')=='x'

bot=(R/'app/bot.py').read_text(encoding='utf-8')
collector=(R/'app/services/collector.py').read_text(encoding='utf-8')
provider=(R/'provider/server.mjs').read_text(encoding='utf-8')
msgtext=(R/'provider/message_text.mjs').read_text(encoding='utf-8')

checks={
 'global cancel button': "callback_data='cancel_action'" in bot,
 'home clears FSM': 'await state.clear()' in bot,
 'collection job creation': ("create_job(c.from_user.id,'collection'" in bot or "create_job(op,'collection'" in bot),
 'background collection': '_spawn(_collect_job' in bot,
 'job cancel': "job_cancel:" in bot and 'request_cancel' in bot,
 'job resume new': "job_resume:" in bot and "payload['period']='new'" in bot,
 'resync history button': 'acct_resync_confirm:' in bot,
 'dashboard text diagnostics': 'رسائل ذات نص قابل للقراءة' in bot,
 'whatsapp virtual category': "requested == 'whatsapp'" in collector,
 'fast bare WA link scan': "chat.whatsapp.com/%" in collector,
 'recursive wrapped parser': 'extractMessageText' in provider and 'ephemeralMessage' not in provider,
 'text parser depth guard': 'depth > 9' in msgtext,
}
for name,ok in checks.items():
    assert ok,name
    print('OK',name)
print('V2.1 STATIC TESTS PASSED')
