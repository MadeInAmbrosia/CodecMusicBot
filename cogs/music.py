import discord
from discord.ext import commands, tasks
from discord import app_commands
import yt_dlp
import asyncio
import random
import os
import shutil
import json
import sys
import subprocess
import datetime

FFMPEG_PATH_FILE = "ffmpeg_path.txt"
MUSIC_STATE_FILE = "data/music_state.json"
ffmpeg_path = None

# ffmpeg loader on startup
def load_ffmpeg_path():
    global ffmpeg_path
    if os.path.exists(FFMPEG_PATH_FILE):
        with open(FFMPEG_PATH_FILE, "r", encoding="utf-8") as f:
            saved = f.read().strip()
            if os.path.isfile(saved):
                ffmpeg_path = saved
                return

    auto = shutil.which("ffmpeg")
    if auto:
        ffmpeg_path = auto
        with open(FFMPEG_PATH_FILE, "w", encoding="utf-8") as f:
            f.write(auto)
        return

    print("\n========== FFMPEG REQUIRED ==========")
    user_path = input("Enter path to ffmpeg: ").strip()
    if not os.path.isfile(user_path):
        raise FileNotFoundError("FFmpeg not found at provided location.")
    ffmpeg_path = user_path
    with open(FFMPEG_PATH_FILE, "w", encoding="utf-8") as f:
        f.write(ffmpeg_path)

load_ffmpeg_path()
vc_connections = {}
queues = {}
repeat_one = {}
repeat_all = {}
current_song = {}

# state persistance
def save_state():
    os.makedirs("data", exist_ok=True)
    state = {}
    for gid in set(list(queues.keys()) + list(current_song.keys())):
        state[gid] = {
            "queue": queues.get(gid, []),
            "current": current_song.get(gid),
            "repeat_one": repeat_one.get(gid, False),
            "repeat_all": repeat_all.get(gid, False),
            "vc_channel": vc_connections.get(gid).channel.id if gid in vc_connections else None
        }
    with open(MUSIC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def load_state():
    if not os.path.exists(MUSIC_STATE_FILE):
        return {}
    with open(MUSIC_STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.watchdog.start()
        self.restore_state_task = self.bot.loop.create_task(self.restore_state())
        self.restart_task = self.bot.loop.create_task(self.auto_restart_task())

    # play_next, self explanitory
    async def play_next(self, guild_id, interaction_channel=None):
        if not queues.get(guild_id):
            if guild_id in vc_connections:
                await vc_connections[guild_id].disconnect()
                vc_connections.pop(guild_id, None)
            current_song.pop(guild_id, None)
            save_state()
            return

        title, url = queues[guild_id].pop(0)
        vc = vc_connections[guild_id]
        current_song[guild_id] = (title, url)
        save_state()

        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android']
                }
            }
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)
            audio_url = info.get('url') or info.get('formats', [{}])[0].get('url')

            def after_play(e):
                if repeat_one.get(guild_id):
                    queues[guild_id].insert(0, (title, url))
                elif repeat_all.get(guild_id):
                    queues[guild_id].append((title, url))
                current_song.pop(guild_id, None)
                save_state()
                asyncio.run_coroutine_threadsafe(self.play_next(guild_id, interaction_channel), self.bot.loop)

            if interaction_channel:
                asyncio.run_coroutine_threadsafe(interaction_channel.send(f"Now playing: **{title}**"), self.bot.loop)

            vc.play(discord.FFmpegPCMAudio(
                audio_url,
                executable=ffmpeg_path,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn -loglevel error"
            ), after=after_play)

        except Exception as e:
            if interaction_channel:
                await interaction_channel.send(f"Failed to play: `{e}`")
            await vc.disconnect()
            vc_connections.pop(guild_id, None)
            current_song.pop(guild_id, None)
            save_state()

    # Watchdog implemented.
    @tasks.loop(seconds=10)
    async def watchdog(self):
        for gid, vc in vc_connections.items():
            if not vc.is_playing() and queues.get(gid) and gid not in current_song:
                try:
                    channel = vc.channel
                    text_channel = await self.bot.fetch_channel(channel.id)
                    await self.play_next(gid, text_channel)
                except Exception:
                    pass

    @watchdog.before_loop
    async def before_watchdog(self):
        await self.bot.wait_until_ready()

    # This restores the bot back to it's original state.
    async def restore_state(self):
        await self.bot.wait_until_ready()
        state = load_state()
        for gid_str, guild_state in state.items():
            gid = int(gid_str)
            guild = self.bot.get_guild(gid)
            if not guild:
                continue

            vc_id = guild_state.get("vc_channel")
            if vc_id:
                channel = guild.get_channel(vc_id)
                if channel and isinstance(channel, discord.VoiceChannel):
                    try:
                        vc = await channel.connect()
                        vc_connections[gid] = vc
                        print(f"Restored VC in {guild.name}")
                    except Exception:
                        pass

            queues[gid] = guild_state.get("queue", [])
            current_song[gid] = tuple(guild_state["current"]) if guild_state.get("current") else None
            repeat_one[gid] = guild_state.get("repeat_one", False)
            repeat_all[gid] = guild_state.get("repeat_all", False)

            # Auto-resume current song if possible:
            if current_song[gid] and gid in vc_connections:
                asyncio.create_task(self.play_next(gid))

    # Restart function
    async def auto_restart_task(self):
        await self.bot.wait_until_ready()
        while True:
            # wait 2 hours
            await asyncio.sleep(2 * 60 * 60)
            print("Auto-restart triggered.")
            for gid, vc in vc_connections.items():
                if vc.is_connected():
                    try:
                        text_channel = await self.bot.fetch_channel(vc.channel.id)
                        await text_channel.send("Bot will restart in 10 seconds, saving current queue...")
                    except Exception:
                        pass
            save_state()
            # relaunch main.py 
            python = sys.executable
            subprocess.Popen([python, "main.py"])
            await self.bot.close()
            return

    # Commands
    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You're not in a voice channel.")
            return
        vc = await interaction.user.voice.channel.connect()
        vc_connections[interaction.guild.id] = vc
        queues[interaction.guild.id] = []
        await interaction.response.send_message(f"Codec connected to **{interaction.user.voice.channel.name}**")

    @app_commands.command(name="leave", description="Leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        vc = vc_connections.get(gid)
        if vc:
            await vc.disconnect()
        for d in (vc_connections, queues, current_song, repeat_one, repeat_all):
            d.pop(gid, None)
        await interaction.response.send_message("Codec disconnected and cleared session.")

    @app_commands.command(name="play", description="Plays audio from YouTube or search query")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You must be in a VC to use this command.")
            return
        guild_id = interaction.guild.id
        voice_channel = interaction.user.voice.channel
        if guild_id not in vc_connections:
            vc = await voice_channel.connect()
            vc_connections[guild_id] = vc
            queues[guild_id] = []
        if not interaction.response.is_done():
            await interaction.response.defer()
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extractor_args": {"youtube": {"player_client": ["android"], "js_runtime": "node"}}
        }
        try:
            search_term = query if query.startswith("http") else f"ytsearch:{query}"
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, search_term, download=False)
            entries = info.get('entries', [info])
            added = 0
            current_urls = {url for _, url in queues[guild_id]}
            for entry in entries:
                url = entry.get('webpage_url') or entry.get('url')
                title = entry.get('title', 'Unknown Title')
                if not url or url in current_urls:
                    continue
                queues[guild_id].append((title, url))
                added += 1
                current_urls.add(url)
            if added == 0:
                await interaction.followup.send("No new songs added.")
            else:
                await interaction.followup.send(f"Added {added} song(s) to the queue.")
            if not vc_connections[guild_id].is_playing():
                await self.play_next(guild_id, interaction.channel)
        except Exception as e:
            await interaction.followup.send(f"Failed to play: `{str(e)}`")

    @app_commands.command(name="queue", description="View the current song queue")
    async def queue(self, interaction: discord.Interaction):
        queue = queues.get(interaction.guild.id, [])
        if not queue:
            await interaction.response.send_message("Queue is empty.")
            return
        pages = [queue[i:i+10] for i in range(0, len(queue), 10)]
        index = 0
        async def send_page(i):
            page = pages[i]
            desc = "\n".join(f"{idx+1+i*10}. [{t}]({u})" for idx, (t, u) in enumerate(page))
            return discord.Embed(title=f"Queue (Page {i+1}/{len(pages)})", description=desc)
        class QueueView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
            @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
            async def prev(self, i2, _):
                nonlocal index
                index = max(index-1, 0)
                await i2.response.edit_message(embed=await send_page(index), view=self)
            @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
            async def next(self, i2, _):
                nonlocal index
                index = min(index+1, len(pages)-1)
                await i2.response.edit_message(embed=await send_page(index), view=self)
        await interaction.response.send_message(embed=await send_page(index), view=QueueView())

    @app_commands.command(name="skip", description="Skips the currently playing song")
    async def skip(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        vc = vc_connections.get(gid)
        if vc and vc.is_playing():
            current_song.pop(gid, None)
            vc.stop()
            await interaction.response.send_message("Skipped to the next song.")
        else:
            await interaction.response.send_message("No song is currently playing.")

    @app_commands.command(name="stop", description="Stops playback and clears the queue")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild.id
        vc = vc_connections.get(gid)
        try:
            if vc:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
        except Exception:
            pass
        for d in (vc_connections, queues, current_song, repeat_one, repeat_all):
            d.pop(gid, None)
        await interaction.followup.send("Stopped playback and cleared the queue.")

    @app_commands.command(name="shuffle", description="Shuffles the current music queue")
    async def shuffle(self, interaction: discord.Interaction):
        queue = queues.get(interaction.guild.id, [])
        if len(queue) < 2:
            await interaction.response.send_message("Not enough songs to shuffle.")
        else:
            random.shuffle(queue)
            await interaction.response.send_message("Queue shuffled!")

    @app_commands.command(name="clearqueue", description="Clears the queue")
    async def clearqueue(self, interaction: discord.Interaction):
        queues[interaction.guild.id] = []
        await interaction.response.send_message("Queue cleared.")

    @app_commands.command(name="remove", description="Removes a song from the queue by position")
    async def remove(self, interaction: discord.Interaction, position: int):
        queue = queues.get(interaction.guild.id, [])
        if 0 < position <= len(queue):
            removed = queue.pop(position-1)
            await interaction.response.send_message(f"Removed: {removed}")
        else:
            await interaction.response.send_message("Invalid position.")

    @app_commands.command(name="raudio", description="Toggle repeat current song")
    async def raudio(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        current = repeat_one.get(gid, False)
        repeat_one[gid] = not current
        if repeat_one[gid]:
            repeat_all[gid] = False
        await interaction.response.send_message(f"Repeat-one is now {'enabled' if repeat_one[gid] else 'disabled'}.")

    @app_commands.command(name="rqueue", description="Toggle repeat queue")
    async def rqueue(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        current = repeat_all.get(gid, False)
        repeat_all[gid] = not current
        if repeat_all[gid]:
            repeat_one[gid] = False
        await interaction.response.send_message(f"Repeat-queue is now {'enabled' if repeat_all[gid] else 'disabled'}.")

async def setup(bot):
    await bot.add_cog(Music(bot))
