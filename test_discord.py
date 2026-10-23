

import logging
import os

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing from .env")

intents = discord.Intents.default()

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"CONNECTED: {client.user}")
    print(f"Servers: {[g.name for g in client.guilds]}")

client.run(TOKEN, log_level=logging.DEBUG)
