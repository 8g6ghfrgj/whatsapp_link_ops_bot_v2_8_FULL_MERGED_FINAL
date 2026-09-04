"""Create/refresh the optional Telethon user session used only for history import."""
import asyncio
from telethon import TelegramClient
from app.config import settings

async def main():
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise SystemExit('Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.')
    client=TelegramClient(settings.telegram_session_path,settings.telegram_api_id,settings.telegram_api_hash)
    print('This login creates a local Telegram user session for importing old source history.')
    print('The session stays in:', settings.telegram_session_path + '.session')
    await client.start()
    me=await client.get_me()
    print('AUTHORIZED:', getattr(me,'id',None), getattr(me,'username',None) or '')
    await client.disconnect()

if __name__=='__main__': asyncio.run(main())
