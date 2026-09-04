from __future__ import annotations
import re
from urllib.parse import urlparse, urlunparse

URL_RE = re.compile(
    r"(?:(?:https?://|www\.)[^\s<>'\"\]\[(){}]+|(?:chat\.whatsapp\.com|wa\.me|api\.whatsapp\.com|(?:www\.)?whatsapp\.com/channel)/[^\s<>'\"\]\[(){}]+)",
    re.I,
)


def extract_urls(text: str) -> list[str]:
    out=[]; seen=set()
    for raw in URL_RE.findall(text or ''):
        u=raw.rstrip('.,;:!؟،)]}>')
        if not re.match(r'^https?://',u,re.I):
            u='https://'+u
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def normalize_url(url: str) -> str:
    url=(url or '').strip()
    if not url:return ''
    p=urlparse(url if '://' in url else 'https://'+url)
    scheme='https'; host=p.netloc.lower().split(':')[0]
    if not host:return ''
    path=re.sub(r'/+','/',p.path or '/')
    if path!='/':path=path.rstrip('/')
    # Tracking parameters create false duplicates for WhatsApp group/channel
    # invitations.  Their stable identity is fully contained in the path.
    query=p.query
    if host=='chat.whatsapp.com' or (host in {'whatsapp.com','www.whatsapp.com'} and path.lower().startswith('/channel/')):
        query=''
    return urlunparse((scheme,host,path,'',query,''))


def classify_link(url: str) -> str:
    p=urlparse(normalize_url(url)); host=p.netloc.lower(); path=p.path.lower()
    if host=='chat.whatsapp.com':return 'whatsapp_group'
    if host in {'whatsapp.com','www.whatsapp.com'} and path.startswith('/channel/'):return 'whatsapp_channel'
    if host in {'wa.me','api.whatsapp.com'}:return 'whatsapp_contact'
    if host in {'t.me','telegram.me','www.t.me','www.telegram.me'}:return 'telegram'
    if host in {'x.com','twitter.com','www.x.com','www.twitter.com'}:return 'x'
    return 'other'


def is_whatsapp_invite(url: str) -> bool:
    return classify_link(url) in {'whatsapp_group','whatsapp_channel'}


POSITIVE_SECTIONS={'important','students','channels'}


def canonical_section(section: str, category: str) -> str:
    """Return the one canonical positive section for a link.

    WhatsApp channel links always belong to ``channels`` regardless of the
    source section.  Blocking registries (expired/ignored) are handled before
    this helper and therefore remain authoritative.
    """
    section=(section or 'important').strip().lower()
    if category=='whatsapp_channel':
        return 'channels'
    return section if section in POSITIVE_SECTIONS else 'important'
