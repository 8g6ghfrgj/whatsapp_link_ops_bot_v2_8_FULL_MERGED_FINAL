import { extractMessageText } from './message_text.mjs'
const cases = [
  [{message:{conversation:'https://chat.whatsapp.com/ABC'}}, 'chat.whatsapp.com/ABC'],
  [{message:{ephemeralMessage:{message:{extendedTextMessage:{text:'hello https://chat.whatsapp.com/DEF'}}}}}, 'chat.whatsapp.com/DEF'],
  [{message:{viewOnceMessageV2:{message:{imageMessage:{caption:'https://wa.me/967700000000'}}}}}, 'wa.me/967700000000'],
  [{message:{documentWithCaptionMessage:{message:{documentMessage:{caption:'https://whatsapp.com/channel/XYZ'}}}}}, 'whatsapp.com/channel/XYZ'],
]
for (const [msg,want] of cases) {
  const got=extractMessageText(msg)
  if (!got.includes(want)) throw new Error(`missing ${want}: ${got}`)
}
console.log('V2.1 MESSAGE TEXT TESTS PASSED')
