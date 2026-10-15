"""Run this ON YOUR PC (not on Render) with the Telegram PREMIUM account you want as the 4 GB helper.
   pip install pyrogram tgcrypto
   python gen_session.py   -> log in with phone + code, copy the long text it prints into SESSION_STRING."""
from pyrogram import Client

api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ").strip()
with Client("gen", api_id=api_id, api_hash=api_hash, in_memory=True) as app:
    print("\nSESSION_STRING (keep it secret, it is full access to your account):\n")
    print(app.export_session_string())