import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, Browsers } from '@whiskeysockets/baileys'
import pino from 'pino'
import { extractMessageText } from './message_text.mjs'

const HOST = process.env.WA_PROVIDER_HOST || '127.0.0.1'
const PORT = Number(process.env.WA_PROVIDER_PORT || 8765)
const TOKEN = process.env.WA_PROVIDER_TOKEN || ''
const ROOT = process.env.WA_PROVIDER_DATA || path.resolve('data/wa_provider')
const logger = pino({ level: process.env.WA_PROVIDER_LOG_LEVEL || 'warn' })
const BOOT_ID = crypto.randomBytes(8).toString('hex')
fs.mkdirSync(ROOT, { recursive: true })

const sessions = new Map()
const events = new Map()
const eventSeq = new Map()

function safeId(v) { return String(v || '').replace(/[^0-9A-Za-z_.-]/g, '_').slice(0, 120) }
function authDir(id) { return path.join(ROOT, safeId(id)) }
function nextEvent(id, ev) {
  const n = (eventSeq.get(id) || 0) + 1
  eventSeq.set(id, n)
  const arr = events.get(id) || []
  arr.push({ id:n, at:Date.now(), ...ev })
  if (arr.length > 50000) arr.splice(0, arr.length - 50000)
  events.set(id, arr)
}
function msgEvent(m) {
  return {
    type:'message',
    jid:m?.key?.remoteJid || null,
    message_id:m?.key?.id || null,
    from_me:!!m?.key?.fromMe,
    participant:m?.key?.participant || null,
    timestamp:Number(m?.messageTimestamp || 0),
    text:extractMessageText(m)
  }
}

async function startSession(accountId) {
  accountId = safeId(accountId)
  const old = sessions.get(accountId)
  if (old?.sock) return old
  fs.mkdirSync(authDir(accountId), { recursive:true })
  const { state, saveCreds } = await useMultiFileAuthState(authDir(accountId))
  const { version } = await fetchLatestBaileysVersion()
  const rec = { accountId, sock:null, status:'connecting', qr:null, lastError:null, me:null, startedAt:Date.now(), contacts:new Map(), chats:new Map() }
  sessions.set(accountId, rec)
  const sock = makeWASocket({
    version,
    auth: state,
    logger,
    browser: Browsers.ubuntu('Chrome'),
    printQRInTerminal: false,
    syncFullHistory: true,
    markOnlineOnConnect: false,
    generateHighQualityLinkPreview: false
  })
  rec.sock = sock
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', async (u) => {
    if (u.qr) { rec.qr = u.qr; rec.status = 'qr'; nextEvent(accountId,{type:'qr'}) }
    if (u.connection === 'open') {
      rec.status = 'connected'; rec.qr = null; rec.lastError = null; rec.me = sock.user || null
      nextEvent(accountId,{type:'connection',status:'connected',me:rec.me})
    }
    if (u.connection === 'close') {
      const code = u.lastDisconnect?.error?.output?.statusCode || u.lastDisconnect?.error?.statusCode || null
      rec.status = 'disconnected'; rec.lastError = String(u.lastDisconnect?.error?.message || u.lastDisconnect?.error || 'disconnected')
      nextEvent(accountId,{type:'connection',status:'disconnected',code,error:rec.lastError})
      const shouldReconnect = code !== DisconnectReason.loggedOut
      rec.sock = null
      if (shouldReconnect) setTimeout(()=>startSession(accountId).catch(e=>{rec.lastError=String(e)}), 4000)
    }
  })
  sock.ev.on('messages.upsert', ({messages}) => {
    for (const m of messages || []) nextEvent(accountId, msgEvent(m))
  })
  sock.ev.on('contacts.upsert', (items=[]) => { for (const x of items) if (x?.id) rec.contacts.set(x.id,{id:x.id,name:x.name||x.notify||x.verifiedName||'',notify:x.notify||'',verifiedName:x.verifiedName||''}) })
  sock.ev.on('contacts.update', (items=[]) => { for (const x of items) if (x?.id) rec.contacts.set(x.id,{...(rec.contacts.get(x.id)||{}),...x}) })
  sock.ev.on('chats.upsert', (items=[]) => { for (const x of items) if (x?.id) rec.chats.set(x.id,x) })
  sock.ev.on('chats.update', (items=[]) => { for (const x of items) if (x?.id) rec.chats.set(x.id,{...(rec.chats.get(x.id)||{}),...x}) })
  sock.ev.on('messaging-history.set', ({messages,contacts,chats}) => {
    for (const x of contacts || []) if (x?.id) rec.contacts.set(x.id,{id:x.id,name:x.name||x.notify||x.verifiedName||'',notify:x.notify||'',verifiedName:x.verifiedName||''})
    for (const x of chats || []) if (x?.id) rec.chats.set(x.id,x)
    for (const m of messages || []) nextEvent(accountId, {...msgEvent(m), type:'history_message'})
  })
  return rec
}

async function stopSession(id, logout=false) {
  id=safeId(id); const rec=sessions.get(id)
  if (rec?.sock) {
    try { if (logout) await rec.sock.logout(); else rec.sock.end(undefined) } catch {}
  }
  sessions.delete(id)
  if (logout) fs.rmSync(authDir(id), {recursive:true,force:true})
}

function json(res, code, obj) { const b=Buffer.from(JSON.stringify(obj)); res.writeHead(code,{'content-type':'application/json','content-length':b.length}); res.end(b) }
async function body(req){return await new Promise((resolve,reject)=>{let s='';req.on('data',d=>{s+=d;if(s.length>1e6)req.destroy()});req.on('end',()=>{try{resolve(s?JSON.parse(s):{})}catch(e){reject(e)}});req.on('error',reject)})}
function okAuth(req){ if(!TOKEN) return true; return req.headers['authorization'] === `Bearer ${TOKEN}` }
function inviteCode(url){ const m=String(url||'').match(/chat\.whatsapp\.com\/([A-Za-z0-9_-]+)/i); return m?m[1]:null }

const server=http.createServer(async(req,res)=>{
  try {
    if(!okAuth(req)) return json(res,401,{ok:false,error:'unauthorized'})
    const u=new URL(req.url,`http://${HOST}:${PORT}`)
    if(req.method==='GET' && u.pathname==='/health') return json(res,200,{ok:true,sessions:sessions.size,boot_id:BOOT_ID})
    if(req.method==='POST' && u.pathname==='/accounts/start') {
      const b=await body(req); const id=safeId(b.account_id); if(!id) return json(res,400,{ok:false,error:'account_id required'})
      const r=await startSession(id); return json(res,200,{ok:true,status:r.status,qr:r.qr,me:r.me,last_error:r.lastError})
    }
    if(req.method==='GET' && u.pathname==='/accounts/status') {
      const id=safeId(u.searchParams.get('account_id')); let r=sessions.get(id)
      if(!r && fs.existsSync(authDir(id))) r=await startSession(id)
      return json(res,200,{ok:true,status:r?.status||'not_started',qr:r?.qr||null,me:r?.me||null,last_error:r?.lastError||null,boot_id:BOOT_ID})
    }
    if(req.method==='POST' && u.pathname==='/accounts/logout') {
      const b=await body(req); await stopSession(b.account_id,true); return json(res,200,{ok:true})
    }
    if(req.method==='GET' && u.pathname==='/accounts/groups') {
      const id=safeId(u.searchParams.get('account_id')); const r=sessions.get(id); if(!r?.sock) return json(res,409,{ok:false,error:'not_connected'})
      const groups=await r.sock.groupFetchAllParticipating()
      const out=Object.values(groups||{}).map(g=>({jid:g.id,subject:g.subject||'',size:(g.participants||[]).length,announce:!!g.announce,restrict:!!g.restrict}))
      return json(res,200,{ok:true,groups:out})
    }
    if(req.method==='GET' && u.pathname==='/accounts/contacts') {
      const id=safeId(u.searchParams.get('account_id')); const r=sessions.get(id); if(!r?.sock) return json(res,409,{ok:false,error:'not_connected'})
      const out=[...r.contacts.values()].map(x=>({jid:x.id,name:x.name||x.notify||x.verifiedName||'',notify:x.notify||'',verifiedName:x.verifiedName||''}))
      return json(res,200,{ok:true,contacts:out})
    }
    if(req.method==='GET' && u.pathname==='/events') {
      const id=safeId(u.searchParams.get('account_id')); const after=Number(u.searchParams.get('after')||0); const limit=Math.min(5000,Math.max(1,Number(u.searchParams.get('limit')||1000)))
      const arr=(events.get(id)||[]).filter(e=>e.id>after).slice(0,limit)
      return json(res,200,{ok:true,events:arr,last_id:arr.length?arr[arr.length-1].id:after,boot_id:BOOT_ID})
    }
    if(req.method==='POST' && u.pathname==='/groups/invite-info') {
      const b=await body(req); const id=safeId(b.account_id); const r=sessions.get(id); if(!r?.sock) return json(res,409,{ok:false,error:'not_connected'})
      const code=inviteCode(b.url); if(!code) return json(res,400,{ok:false,error:'invalid_invite'})
      const g=await r.sock.groupGetInviteInfo(code)
      return json(res,200,{ok:true,group:{jid:g?.id||null,subject:g?.subject||'',size:g?.size||g?.participants?.length||null}})
    }
    if(req.method==='POST' && u.pathname==='/messages/send') {
      const b=await body(req); const id=safeId(b.account_id); const r=sessions.get(id); if(!r?.sock) return json(res,409,{ok:false,error:'not_connected'})
      const jid=String(b.jid||'').trim(); const text=String(b.text||'')
      if(!jid || !text) return json(res,400,{ok:false,error:'jid_and_text_required'})
      if(!jid.endsWith('@g.us') && !jid.endsWith('@s.whatsapp.net') && !jid.endsWith('@lid')) return json(res,400,{ok:false,error:'unsupported_target_jid'})
      try {
        const out=await r.sock.sendMessage(jid,{text})
        return json(res,200,{ok:true,message_id:out?.key?.id||null,jid})
      } catch(e) {
        const msg=String(e?.message||e||'send_failed'); const low=msg.toLowerCase()
        let status='failed'
        if(low.includes('rate')||low.includes('429')||low.includes('too many')||low.includes('later')||low.includes('temporar')) status='retry_later'
        return json(res,200,{ok:false,status,error:msg})
      }
    }
    if(req.method==='POST' && u.pathname==='/groups/join') {
      const b=await body(req); const id=safeId(b.account_id); const r=sessions.get(id); if(!r?.sock) return json(res,409,{ok:false,error:'not_connected'})
      const code=inviteCode(b.url); if(!code) return json(res,400,{ok:false,error:'invalid_invite'})
      try {
        const jid=await r.sock.groupAcceptInvite(code)
        return json(res,200,{ok:true,status:'joined',jid:jid||null})
      } catch(e) {
        const msg=String(e?.message||e||'join_failed')
        const low=msg.toLowerCase()
        let status='failed'
        if(low.includes('request')||low.includes('approval')||low.includes('pending')) status='pending_or_approval_required'
        else if(low.includes('rate')||low.includes('429')||low.includes('too many')||low.includes('later')) status='retry_later'
        else if(low.includes('already')||low.includes('participant')) status='already_member'
        return json(res,200,{ok:false,status,error:msg})
      }
    }
    return json(res,404,{ok:false,error:'not_found'})
  } catch(e) { return json(res,500,{ok:false,error:String(e?.message||e)}) }
})

server.listen(PORT,HOST,()=>logger.warn(`WhatsApp provider listening on http://${HOST}:${PORT}`))
