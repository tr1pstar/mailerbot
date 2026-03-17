import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8647411347:AAGI21-CS1mb5o7zR7WzxM0LNdpdklf36Uw")

# ── Zoho IMAP ─────────────────────────────────────────────────────────────────
ZOHO_EMAIL:     str = os.getenv("ZOHO_EMAIL",     "test@funmail.run")
ZOHO_PASSWORD:  str = os.getenv("ZOHO_PASSWORD",  "FunMail!")
ZOHO_DOMAIN:    str = os.getenv("ZOHO_DOMAIN",    "funmail.run")
ZOHO_IMAP_HOST: str = os.getenv("ZOHO_IMAP_HOST", "imappro.zoho.eu")
