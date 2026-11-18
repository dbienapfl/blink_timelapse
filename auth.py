import asyncio
from aiohttp import ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

CREDS_FILE = "/app/creds/blink_creds.json"


async def main():
  async with ClientSession() as session:
    blink = Blink(session=session)
    username = input("Blink Account Email: ").strip()
    password = input("Blink Account Password: ").strip()

    blink.auth = Auth(
        {"username": username, "password": password}, no_prompt=True
    )
    try:
      await blink.start()
    except BlinkTwoFARequiredError:
      pin = input("Enter 6-digit 2FA PIN sent to your email/phone: ").strip()
      await blink.send_2fa_code(pin)
      await blink.setup_post_verify()

    await blink.save(CREDS_FILE)
    print(f"\nSuccess! Credentials saved to {CREDS_FILE}")
    print("Detected cameras:", list(blink.cameras.keys()))


if __name__ == "__main__":
  asyncio.run(main())
