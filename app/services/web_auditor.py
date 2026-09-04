from __future__ import annotations
import asyncio, html, re
from dataclasses import dataclass
import aiohttp
from ..config import settings
from ..link_utils import classify_link

TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I)

@dataclass
class AuditResult:
    status: str
    display_name: str | None = None
    details: str | None = None


def _extract(regex, text):
    m = regex.search(text or "")
    return html.unescape(m.group(1)).strip() if m else None


def classify_preview(url: str, status_code: int, body: str) -> AuditResult:
    cat = classify_link(url)
    if status_code in {404,410}:
        return AuditResult("expired", details=f"HTTP {status_code}")
    if status_code == 429:
        return AuditResult("retry_later", details="HTTP 429")
    if status_code >= 500:
        return AuditResult("retry_later", details=f"HTTP {status_code}")
    title = _extract(TITLE_RE, body)
    desc = _extract(DESC_RE, body)
    low = (body or "").lower()
    if cat == "whatsapp_channel":
        if status_code == 200 and title and title.lower() not in {"whatsapp", "whatsapp.com"}:
            return AuditResult("channel_active", title, desc)
        return AuditResult("needs_manual_check", title, desc)
    if cat == "whatsapp_group":
        # WhatsApp changes its HTML frequently; only mark active when public metadata is present.
        if status_code == 200 and title and title.lower() not in {"whatsapp", "whatsapp.com", "invite to group via whatsapp"}:
            return AuditResult("group_active", title, desc)
        if "invite to group via whatsapp" in low and not title:
            return AuditResult("needs_manual_check", None, "Generic invite page without enough metadata")
        return AuditResult("needs_manual_check", title, desc)
    if cat == "whatsapp_contact":
        return AuditResult("contact_link", title, desc)
    return AuditResult("unsupported", title, desc)

async def inspect_one(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> AuditResult:
    async with sem:
        try:
            async with session.get(url, allow_redirects=True) as r:
                body = await r.text(errors="ignore")
                return classify_preview(url, r.status, body)
        except asyncio.TimeoutError:
            return AuditResult("retry_later", details="timeout")
        except aiohttp.ClientError as e:
            return AuditResult("retry_later", details=type(e).__name__)

async def inspect_many(urls: list[str]) -> list[AuditResult]:
    timeout = aiohttp.ClientTimeout(total=settings.web_audit_timeout_seconds)
    headers = {"User-Agent":"Mozilla/5.0 (compatible; WhatsAppLinkOps/1.0; +local-operator-tool)"}
    sem = asyncio.Semaphore(settings.web_audit_workers)
    connector = aiohttp.TCPConnector(limit=settings.web_audit_workers)
    async with aiohttp.ClientSession(timeout=timeout,headers=headers,connector=connector) as s:
        return await asyncio.gather(*(inspect_one(s,u,sem) for u in urls))
