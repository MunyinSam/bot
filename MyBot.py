import os
import datetime as dt
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque
from urllib.parse import urlparse, parse_qs
import db
from embeds import make_now_playing_embed, ok_embed, info_embed, err_embed

# Import Configs
from config import TOKEN, GUILD_ID, FFMPEG_EXECUTABLE, FFMPEG_OPTIONS, YDL_OPTIONS


# CONSTANTS


db.init_db()
# guild_id -> deque of {"title": str, "audio_url": str|None, "video_url": str, "thumbnail": str|None}
queues: dict[int, deque] = {}
now_playing: dict[int, dict] = {}
guild_text_channels: dict[int, discord.TextChannel] = {}
daily_reminder_tasks: dict[tuple[int, int], asyncio.Task] = {}

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# BOT LOGIC

def _is_playlist_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if "playlist" in parsed.path.lower():
        return True

    query = parse_qs(parsed.query)
    return bool(query.get("list"))


def _to_track(info: dict, fallback_url: str) -> dict | None:
    if not info:
        return None

    title = info.get("title") or "Untitled"
    video_url = info.get("webpage_url") or info.get("original_url") or fallback_url
    raw_audio_url = info.get("url")

    # yt-dlp may return non-http placeholder values for playlist entries.
    audio_url = raw_audio_url if isinstance(raw_audio_url, str) and raw_audio_url.startswith("http") else None

    return {
        "title": title,
        "audio_url": audio_url,
        "video_url": video_url,
        "thumbnail": info.get("thumbnail"),
    }


async def fetch_tracks(query_or_url: str, allow_playlist: bool = False) -> tuple[list[dict], str | None, bool]:
    loop = asyncio.get_running_loop()

    def _extract():
        options = dict(YDL_OPTIONS)
        if allow_playlist:
            options["noplaylist"] = False

        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.extract_info(query_or_url, download=False)
            if result is None:
                return [], None, False

            if "entries" in result:
                tracks = []
                for entry in result["entries"]:
                    track = _to_track(entry, query_or_url)
                    if track is not None:
                        tracks.append(track)
                return tracks, result.get("title"), len(tracks) > 1

            single = _to_track(result, query_or_url)
            return ([single] if single else []), None, False
        
    return await loop.run_in_executor(None, _extract)

async def play_next(guild: discord.Guild, send_notification: bool = True):
    vc = guild.voice_client
    queue = queues.get(guild.id)

    # If nothing is going on
    if not queue or not vc or vc.is_playing():
        now_playing.pop(guild.id, None)
        return
    
    track = queue.popleft()
    audio_url = track.get("audio_url")

    if not audio_url:
        tracks, _, _ = await fetch_tracks(track["video_url"])
        if not tracks:
            await play_next(guild) # skip broken track
            return
        refreshed = tracks[0]
        audio_url = refreshed.get("audio_url")
        track["thumbnail"] = refreshed.get("thumbnail") # for picture

        if not audio_url:
            await play_next(guild)
            return
    
    now_playing[guild.id] = track # store full track dict
    source = discord.FFmpegOpusAudio(
        audio_url, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTIONS
    )

    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        now_playing.pop(guild.id, None)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop) # make new thread calling next

    # Bot Play Music HERE
    vc.play(source, after=after_playing) 

    if send_notification and guild.id in guild_text_channels:
        await guild_text_channels[guild.id].send(embed=make_now_playing_embed(track))


def parse_daily_time(time_text: str) -> tuple[int, int] | None:
    parts = time_text.strip().split(":")
    if len(parts) != 2:
        return None
    if not parts[0].isdigit() or not parts[1].isdigit():
        return None

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None

    return hour, minute


def seconds_until_next_run(hour: int, minute: int) -> float:
    now = dt.datetime.now()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += dt.timedelta(days=1)
    return (next_run - now).total_seconds()


async def daily_reminder_loop(channel: discord.abc.Messageable, user_id: int, reminder_text: str, hour: int, minute: int):
    while True:
        await asyncio.sleep(seconds_until_next_run(hour, minute))
        try:
            await channel.send(embed=info_embed(f"<@{user_id}> {reminder_text}", title="Daily Reminder \u23f0"))
        except (discord.Forbidden, discord.HTTPException):
            # Keep the loop alive; the channel may become available again later.
            continue


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    test_guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=test_guild)
    print(f"{bot.user} is online")

# ── Playback commands ─────────────────────────────────────────────────────────

@bot.tree.command(name="sync_command", description="Sync lastest commands")
async def sync_command(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message(embed=ok_embed("Syncing new commands..."))

@bot.tree.command(name="play", description="Play a song or add it to the queue")
@app_commands.describe(song_query="Song Name or Youtube URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if interaction.user.voice is None:
        await interaction.followup.send(embed=err_embed("You are not in a voice channel."))
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)
    
    is_url = song_query.startswith("http")
    query = f"ytsearch1:{song_query}" if not is_url else song_query
    allow_playlist = is_url and _is_playlist_url(song_query)

    tracks, playlist_title, is_playlist = await fetch_tracks(query, allow_playlist=allow_playlist)
    if not tracks:
        await interaction.followup.send(embed=err_embed("No results found."))
        return

    guild_id = interaction.guild.id
    guild_text_channels[guild_id] = interaction.channel
    if guild_id not in queues:
        queues[guild_id] = deque()

    already_active = voice_client.is_playing() or voice_client.is_paused() or len(queues[guild_id]) > 0
    for track in tracks:
        queues[guild_id].append(track)

    if already_active:
        if is_playlist:
            title = playlist_title or "Playlist"
            await interaction.followup.send(
                embed=info_embed(
                    f"Added **{len(tracks)}** tracks from **{title}** to the queue.",
                    title="Added Playlist to Queue \U0001f3b6",
                )
            )
        else:
            title = tracks[0]["title"]
            position = len(queues[guild_id])
            await interaction.followup.send(
                embed=info_embed(
                    f"Added **{title}** to the queue at position #{position}.",
                    title="Added to Queue \U0001f3b6",
                )
            )
    else:
        await play_next(interaction.guild, send_notification=False)
        current = now_playing.get(guild_id)
        if current:
            await interaction.followup.send(embed=make_now_playing_embed(current))
        else:
            await interaction.followup.send(embed=err_embed("Could not start playback."))

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message(embed=err_embed("Nothing is playing right now."))
        return
    vc.stop()  # triggers after_playing -> play_next
    await interaction.response.send_message(embed=ok_embed("Skipped ⏭️"))

@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queues.pop(guild_id, None)
    now_playing.pop(guild_id, None)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    await interaction.response.send_message(embed=ok_embed("Stopped playback and cleared the queue. ⏹️"))

@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queue = queues.get(guild_id, deque())
    current = now_playing.get(guild_id)

    if not current and not queue:
        await interaction.response.send_message(embed=info_embed("The queue is empty.", title="Queue"))
        return

    lines = []
    if current:
        lines.append(f"\U0001f3b5 **Now playing:** {current['title']}")
    if queue:
        lines.append(f"\n**Up next ({len(queue)} song{'s' if len(queue) != 1 else ''}):**")
        for i, track in enumerate(queue, start=1):
            lines.append(f"`{i}.` {track['title']}")

    await interaction.response.send_message(embed=info_embed("\n".join(lines), title="Queue \U0001f4c4"))


@bot.tree.command(name="reminder", description="Set a daily reminder")
@app_commands.describe(
    reminder="What should I remind you about?",
    time="Daily time in HH:MM (24-hour), for example 09:30",
)
async def reminder(interaction: discord.Interaction, reminder: str, time: str):
    parsed_time = parse_daily_time(time)
    if parsed_time is None:
        await interaction.response.send_message(
            embed=err_embed("Invalid time format. Use HH:MM in 24-hour format, for example 09:30."),
            ephemeral=True,
        )
        return

    if interaction.channel is None or interaction.channel_id is None:
        await interaction.response.send_message(embed=err_embed("I could not find a channel for this reminder."), ephemeral=True)
        return

    hour, minute = parsed_time
    task_key = (interaction.user.id, interaction.channel_id)

    existing_task = daily_reminder_tasks.pop(task_key, None)
    if existing_task:
        existing_task.cancel()

    task = asyncio.create_task(
        daily_reminder_loop(interaction.channel, interaction.user.id, reminder, hour, minute)
    )
    daily_reminder_tasks[task_key] = task

    await interaction.response.send_message(
        embed=ok_embed(f"Daily reminder saved for **{time}**: {reminder}"),
        ephemeral=True,
    )


bot.run(TOKEN)