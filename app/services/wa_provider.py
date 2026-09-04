from __future__ import annotations
import aiohttp, asyncio, time
from ..config import settings

class ProviderError(RuntimeError): pass

class WhatsAppProvider:
    def __init__(self):
        self.base=settings.wa_provider_url.rstrip('/')
        self.headers={"Authorization":f"Bearer {settings.wa_provider_token}"} if settings.wa_provider_token else {}
        self._session=None
        self._group_cache={}
        self._contact_cache={}
    async def _request(self,method,path,*,params=None,json=None):
        if self._session is None or self._session.closed:
            timeout=aiohttp.ClientTimeout(total=settings.wa_provider_timeout)
            self._session=aiohttp.ClientSession(timeout=timeout,headers=self.headers,connector=aiohttp.TCPConnector(limit=100,ttl_dns_cache=300))
        async with self._session.request(method,self.base+path,params=params,json=json) as r:
            data=await r.json(content_type=None)
            if r.status>=400: raise ProviderError(data.get('error') or f'HTTP {r.status}')
            return data
    async def health(self): return await self._request('GET','/health')
    async def start(self,account_id): return await self._request('POST','/accounts/start',json={'account_id':str(account_id)})
    async def status(self,account_id): return await self._request('GET','/accounts/status',params={'account_id':str(account_id)})
    async def logout(self,account_id): return await self._request('POST','/accounts/logout',json={'account_id':str(account_id)})
    async def groups(self,account_id,refresh=False):
        k=str(account_id); now=time.monotonic(); hit=self._group_cache.get(k)
        if hit and not refresh and now-hit[0]<15:return hit[1]
        data=await self._request('GET','/accounts/groups',params={'account_id':k}); self._group_cache[k]=(now,data); return data
    async def contacts(self,account_id,refresh=False):
        k=str(account_id); now=time.monotonic(); hit=self._contact_cache.get(k)
        if hit and not refresh and now-hit[0]<30:return hit[1]
        data=await self._request('GET','/accounts/contacts',params={'account_id':k}); self._contact_cache[k]=(now,data); return data
    async def events(self,account_id,after=0,limit=1000): return await self._request('GET','/events',params={'account_id':str(account_id),'after':after,'limit':limit})
    async def invite_info(self,account_id,url): return await self._request('POST','/groups/invite-info',json={'account_id':str(account_id),'url':url})
    async def join(self,account_id,url): return await self._request('POST','/groups/join',json={'account_id':str(account_id),'url':url})
    async def send_text(self,account_id,jid,text): return await self._request('POST','/messages/send',json={'account_id':str(account_id),'jid':str(jid),'text':str(text)})
    async def close(self):
        if self._session and not self._session.closed: await self._session.close()
provider=WhatsAppProvider()
