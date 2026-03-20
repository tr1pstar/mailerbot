import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ── Zoho IMAP ─────────────────────────────────────────────────────────────────
ZOHO_EMAIL:     str = os.getenv("ZOHO_EMAIL",     "test@funmail.run")
ZOHO_PASSWORD:  str = os.getenv("ZOHO_PASSWORD",  "FunMail!")
ZOHO_DOMAIN:    str = os.getenv("ZOHO_DOMAIN",    "funmail.run")
ZOHO_IMAP_HOST: str = os.getenv("ZOHO_IMAP_HOST", "imappro.zoho.eu")

# Твой Telegram user_id — получи у @userinfobot
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
