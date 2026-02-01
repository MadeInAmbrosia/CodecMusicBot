import discord
from discord.ext import commands
import json
import os
import asyncio
import time
import socket
import traceback
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Codec is online as {bot.user}!")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash sync failed: {e}")

    # Attempt to restore VC connections for music after restart
    try:
        from cogs.music import vc_connections, Music
        for gid, queue in Music.queues.items():
            guild = bot.get_guild(int(gid))
            if not guild:
                continue
            # reconnect if there's a queue or currently playing song
            if gid in Music.current_song or queue:
                # pick first channel from the guild's voice channels with members (simple heuristic)
                for vc_channel in guild.voice_channels:
                    if vc_channel.members:
                        vc = await vc_channel.connect()
                        vc_connections[gid] = vc
                        print(f"Restored VC connection in guild {guild.name} ({gid})")
                        break
    except Exception as e:
        print(f"Failed to restore music VC connections: {e}")
        print(f"Ignore this at startup, if it's the first time you launched the bot.")

@bot.event
async def setup_hook():
    from cogs.music import Music
    await bot.add_cog(Music(bot))

try:
    bot.run(TOKEN)
except Exception:
    traceback.print_exc()
    input("Press Enter to exit...")
