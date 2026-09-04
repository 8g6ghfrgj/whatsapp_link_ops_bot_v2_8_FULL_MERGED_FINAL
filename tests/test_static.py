from app.link_utils import normalize_url,classify_link,extract_urls
from app.services.web_auditor import classify_preview

def ok(cond,msg):
    if not cond: raise AssertionError(msg)

ok(classify_link('https://chat.whatsapp.com/ABC')=='whatsapp_group','group classify')
ok(classify_link('https://www.whatsapp.com/channel/ABC')=='whatsapp_channel','channel classify')
ok(classify_link('https://wa.me/967700000000')=='whatsapp_contact','contact classify')
ok(normalize_url('http://CHAT.WHATSAPP.COM/ABC/')=='https://chat.whatsapp.com/ABC','normalize')
ok(normalize_url('https://chat.whatsapp.com/ABC?ref=one')=='https://chat.whatsapp.com/ABC','group tracking dedupe')
ok(normalize_url('https://www.whatsapp.com/channel/ABC?utm_source=x')=='https://www.whatsapp.com/channel/ABC','channel tracking dedupe')
ok(classify_preview('https://chat.whatsapp.com/ABC',404,'').status=='expired','404 expired')
html='<meta property="og:title" content="My Group"><meta property="og:description" content="Join">'
r=classify_preview('https://chat.whatsapp.com/ABC',200,html)
ok(r.status=='group_active' and r.display_name=='My Group','metadata group')
print('SELF-CHECK PASSED')
