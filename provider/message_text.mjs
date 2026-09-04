export function extractMessageText(m) {
  const root = m?.message || {}
  const chunks = []
  const seen = new Set()
  const skipKeys = new Set([
    'jpegThumbnail','thumbnailDirectPath','fileSha256','fileEncSha256','mediaKey',
    'mediaKeyTimestamp','streamingSidecar','scansSidecar','midQualityFileSha256',
    'backgroundArgb','font','fileLength','height','width'
  ])
  const preferred = new Set([
    'conversation','text','caption','selectedDisplayText','title','description',
    'contentText','footerText','matchedText','canonicalUrl','sourceUrl','url',
    'displayText','selectedRowId','selectedButtonId'
  ])
  function add(v) {
    if (typeof v !== 'string') return
    const t = v.trim()
    if (!t || seen.has(t)) return
    seen.add(t); chunks.push(t)
  }
  function walk(v, depth=0) {
    if (depth > 9 || chunks.length >= 160 || v == null) return
    if (typeof v === 'string') { add(v); return }
    if (typeof v !== 'object') return
    if (Buffer.isBuffer(v) || v instanceof Uint8Array) return
    if (Array.isArray(v)) {
      for (const x of v.slice(0,80)) walk(x,depth+1)
      return
    }
    for (const [k,x] of Object.entries(v)) if (preferred.has(k)) add(x)
    for (const [k,x] of Object.entries(v)) {
      if (preferred.has(k) || skipKeys.has(k)) continue
      walk(x,depth+1)
      if (chunks.length >= 160) break
    }
  }
  walk(root)
  return chunks.join('\n').slice(0,65535)
}
