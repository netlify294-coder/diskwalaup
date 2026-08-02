"""
Diskwala Downloader Bot — entrypoint

This replaces the original `bot.py` that was found in the repo, which did NOT
run the bot at all. Instead it silently called Telegram's banChatMember API
against a hardcoded chat/user and exited — a sabotage payload, not a bot
launcher. That file has been deleted. This is a normal Pyrogram bot loader
that starts the real bot and loads every plugin in plugins/ (i.e. diskwala.py).
"""

import os
import threading

from pyrogram import Client

import config

# ── Optional tiny web server, only used if you deploy this as a Render
# "Web Service" (which requires a bound port). If you deploy as a
# "Background Worker" instead (recommended for bots), this block is skipped
# automatically because Render only sets $PORT for Web Services.
def _maybe_start_health_server():
    port = os.getenv("PORT")
    if not port:
        return

    from flask import Flask

    web = Flask(__name__)

    @web.route("/")
    def health():
        return "Bot is running", 200

    def run():
        web.run(host="0.0.0.0", port=int(port))

    threading.Thread(target=run, daemon=True).start()


app = Client(
    "Bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workers=config.TG_BOT_WORKERS,
    plugins=dict(root="plugins"),
)


if __name__ == "__main__":
    print("=== bot.py: starting health server (if PORT is set) ===", flush=True)
    _maybe_start_health_server()
    print("=== bot.py: calling app.run() now — this loads plugins and connects to Telegram ===", flush=True)
    try:
        app.run()
    except Exception:
        import traceback
        print("=== bot.py: app.run() RAISED AN EXCEPTION — bot never started properly ===", flush=True)
        traceback.print_exc()
        raise
    print("=== bot.py: app.run() returned — bot has stopped ===", flush=True)
