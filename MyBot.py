import os
import json
import tempfile
import time
import datetime as dt
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
from yt_dlp.utils import DownloadError
import asyncio
from collections import deque
import random
from urllib.parse import urlparse, parse_qs
import redis.asyncio as aioredis
import db
from embeds import make_now_playing_embed, make_added_to_queue_embed, ok_embed, info_embed, err_embed
from spotify_scraper import SpotifyClient

try:
    import whisper as _whisper
    _whisper_model = _whisper.load_model("base")
except ImportError:
    _whisper_model = None

# Import Configs
from config import TOKEN, GUILD_ID, FFMPEG_EXECUTABLE, FFMPEG_OPTIONS, YDL_OPTIONS, REDIS_URL, SESSION_NOTIFY_CHANNEL_ID


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

spotify_client = SpotifyClient()

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


async def fetch_tracks(query_or_url: str, allow_playlist: bool = False) -> tuple[list[dict], str | None, bool, int, str | None]:
    loop = asyncio.get_running_loop()

    def _extract():
        options = dict(YDL_OPTIONS)
        if allow_playlist:
            options["noplaylist"] = False
            options["ignoreerrors"] = True

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.extract_info(query_or_url, download=False)
        except DownloadError as exc:
            return [], None, False, 0, str(exc)
        except Exception as exc:
            return [], None, False, 0, str(exc)

        if result is None:
            return [], None, False, 0, None

        if "entries" in result:
            tracks = []
            skipped = 0
            for entry in result["entries"] or []:
                track = _to_track(entry, query_or_url)
                if track is None:
                    skipped += 1
                    continue
                tracks.append(track)
            return tracks, result.get("title"), len(tracks) > 1, skipped, None

        single = _to_track(result, query_or_url)
        return ([single] if single else []), None, False, (0 if single else 1), None
        
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
        tracks, _, _, _, _ = await fetch_tracks(track["video_url"])
        if not tracks:
            await play_next(guild) # skip broken track
            return
        refreshed = tracks[0]
        audio_url = refreshed.get("audio_url")
        track["thumbnail"] = refreshed.get("thumbnail")
        if refreshed.get("video_url"):
            track["video_url"] = refreshed["video_url"]

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

# Spotify Handling

def is_spotify_link(url: str) -> bool:
    return "open.spotify.com" in url


def _make_spotify_stub(track_info: dict) -> dict:
    artist = track_info["artists"][0]["name"] if track_info.get("artists") else "Unknown"
    title = f"{artist} - {track_info['name']}"
    return {"title": title, "audio_url": None, "video_url": f"ytsearch1:{title}", "thumbnail": None}


async def _resolve_spotify_url(url: str) -> tuple[list[dict], str | None, bool]:
    """Resolve a Spotify track/playlist/album URL into playable track stubs."""
    loop = asyncio.get_running_loop()

    def _scrape():
        if "/track/" in url:
            info = spotify_client.get_track_info(url)
            return [_make_spotify_stub(info)], None, False

        if "/playlist/" in url:
            info = spotify_client.get_playlist_info(url)
            raw_tracks = info.get("tracks", [])
            # tracks may be a list or {"items": [...], "total": ...}
            if isinstance(raw_tracks, dict):
                raw_tracks = raw_tracks.get("items", [])
            # each entry may be {"track": {...}} or the track dict directly
            stubs = []
            for item in raw_tracks:
                if not item:
                    continue
                t = item.get("track", item) if isinstance(item, dict) and "track" in item else item
                if t:
                    stubs.append(_make_spotify_stub(t))
            return stubs, info.get("name"), True

        if "/album/" in url:
            info = spotify_client.get_album_info(url)
            stubs = [_make_spotify_stub(t) for t in info.get("tracks", []) if t]
            return stubs, info.get("name"), True

        return [], None, False

    try:
        return await loop.run_in_executor(None, _scrape)
    except Exception as exc:
        print(f"[Spotify] resolve error: {exc}")
        return [], None, False


# ── Session notification listener ─────────────────────────────────────────────

def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def session_listener():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    channel = None
    while True:
        try:
            if channel is None:
                channel = bot.get_channel(SESSION_NOTIFY_CHANNEL_ID)
            if channel is None:
                channel = await bot.fetch_channel(SESSION_NOTIFY_CHANNEL_ID)
            result = await r.blpop("session_saved", timeout=30)
            if result is None:
                continue
            _, raw = result
            payload = json.loads(raw)
            user_name = payload.get("user_name") or "Unknown"
            duration = _format_duration(int(payload.get("duration_sec", 0)))
            ended_at = payload.get("ended_at", "")
            description = payload.get("description") or ""
            embed = discord.Embed(
                title="📚 Study Session Saved",
                description=description if description else discord.utils.MISSING,
                color=0x5865F2,
            )
            embed.set_author(name=user_name)
            embed.add_field(name="Duration", value=duration, inline=True)
            if ended_at:
                embed.add_field(name="Ended at", value=ended_at[:19].replace("T", " "), inline=True)
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[session_listener] Discord error: {e}")
        except Exception as e:
            print(f"[session_listener] error: {e}")
            await asyncio.sleep(5)


async def session_start_listener():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    channel = None
    while True:
        try:
            if channel is None:
                channel = bot.get_channel(SESSION_NOTIFY_CHANNEL_ID)
            if channel is None:
                channel = await bot.fetch_channel(SESSION_NOTIFY_CHANNEL_ID)
            result = await r.blpop("session_start", timeout=30)
            if result is None:
                continue
            _, raw = result
            payload = json.loads(raw)
            user_name = payload.get("user_name") or "Unknown"
            description = payload.get("description") or ""
            session_label = f"**{description}**" if description else "a study"
            await channel.send(f"**{user_name}** has started {session_label} session 📖")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[session_start_listener] Discord error: {e}")
        except Exception as e:
            print(f"[session_start_listener] error: {e}")
            await asyncio.sleep(5)


async def exercise_listener():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    channel = None
    while True:
        try:
            if channel is None:
                channel = bot.get_channel(SESSION_NOTIFY_CHANNEL_ID)
            if channel is None:
                channel = await bot.fetch_channel(SESSION_NOTIFY_CHANNEL_ID)
            result = await r.blpop("exercise_saved", timeout=30)
            if result is None:
                continue
            _, raw = result
            payload = json.loads(raw)
            user_name     = payload.get("user_name") or "Unknown"
            duration      = _format_duration(int(payload.get("duration_sec", 0)))
            ended_at      = payload.get("ended_at", "")
            description   = payload.get("description") or ""
            exercise_type = payload.get("exercise_type") or "Workout"
            sets_count    = int(payload.get("sets_count", 0))
            embed = discord.Embed(
                title="💪 Workout Saved",
                description=description if description else discord.utils.MISSING,
                color=0xe07820,
            )
            embed.set_author(name=user_name)
            embed.add_field(name="Type", value=exercise_type, inline=True)
            embed.add_field(name="Duration", value=duration, inline=True)
            if sets_count > 0:
                embed.add_field(name="Sets", value=str(sets_count), inline=True)
            if ended_at:
                embed.add_field(name="Ended at", value=ended_at[:19].replace("T", " "), inline=True)
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[exercise_listener] Discord error: {e}")
        except Exception as e:
            print(f"[exercise_listener] error: {e}")
            await asyncio.sleep(5)


async def exercise_start_listener():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    channel = None
    while True:
        try:
            if channel is None:
                channel = bot.get_channel(SESSION_NOTIFY_CHANNEL_ID)
            if channel is None:
                channel = await bot.fetch_channel(SESSION_NOTIFY_CHANNEL_ID)
            result = await r.blpop("exercise_start", timeout=30)
            if result is None:
                continue
            _, raw = result
            payload       = json.loads(raw)
            user_name     = payload.get("user_name") or "Unknown"
            exercise_type = payload.get("exercise_type") or ""
            type_label    = f"**{exercise_type}**" if exercise_type else "a workout"
            await channel.send(f"**{user_name}** has started {type_label} session 🏋️")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[exercise_start_listener] Discord error: {e}")
        except Exception as e:
            print(f"[exercise_start_listener] error: {e}")
            await asyncio.sleep(5)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    test_guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=test_guild)
    asyncio.create_task(session_listener())
    asyncio.create_task(session_start_listener())
    asyncio.create_task(exercise_listener())
    asyncio.create_task(exercise_start_listener())
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
    
    if is_spotify_link(song_query):
        tracks, playlist_title, is_playlist = await _resolve_spotify_url(song_query)
        skipped_count = 0
        if not tracks:
            await interaction.followup.send(embed=err_embed("Could not resolve Spotify link. Make sure it's a valid track, playlist, or album URL."))
            return
    else:
        is_url = song_query.startswith("http")
        query = f"ytsearch1:{song_query}" if not is_url else song_query
        allow_playlist = is_url and _is_playlist_url(song_query)

        tracks, playlist_title, is_playlist, skipped_count, fetch_error = await fetch_tracks(query, allow_playlist=allow_playlist)
        if not tracks:
            if fetch_error and ("confirm your age" in fetch_error.lower() or "sign in" in fetch_error.lower()):
                await interaction.followup.send(
                    embed=err_embed("That video/playlist contains age-restricted content. I cannot access it without YouTube cookies.")
                )
            else:
                await interaction.followup.send(embed=err_embed("No playable results found."))
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
            extra = f"\nSkipped **{skipped_count}** unavailable/restricted track{'s' if skipped_count != 1 else ''}." if skipped_count else ""
            await interaction.followup.send(
                embed=info_embed(
                    f"Added **{len(tracks)}** tracks from **{title}** to the queue.{extra}",
                    title="Added Playlist to Queue \U0001f3b6",
                )
            )
        else:
            track = tracks[0]
            position = len(queues[guild_id])
            await interaction.followup.send(embed=make_added_to_queue_embed(track, position))
    else:
        await play_next(interaction.guild, send_notification=False)
        current = now_playing.get(guild_id)
        if current:
            await interaction.followup.send(embed=make_now_playing_embed(current))
            if is_playlist:
                title = playlist_title or "Playlist"
                queued_after_now_playing = max(len(tracks) - 1, 0)
                extra = f"\nSkipped **{skipped_count}** unavailable/restricted track{'s' if skipped_count != 1 else ''}." if skipped_count else ""
                await interaction.followup.send(
                    embed=info_embed(
                        f"Queued **{queued_after_now_playing}** more track{'s' if queued_after_now_playing != 1 else ''} from **{title}**.{extra}",
                        title="Playlist Loaded",
                    )
                )
        else:
            await interaction.followup.send(embed=err_embed("Could not start playback."))

@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queue = queues.get(guild_id)
    if not queue or len(queue) < 2:
        await interaction.response.send_message(embed=err_embed("Not enough songs in the queue to shuffle."))
        return
    items = list(queue)
    random.shuffle(items)
    queues[guild_id] = deque(items)
    await interaction.response.send_message(embed=ok_embed(f"Shuffled **{len(items)}** songs in the queue. 🔀"))

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
        video_url = current.get("video_url", "")
        title_link = f"[{current['title']}]({video_url})" if video_url and video_url.startswith("http") else current['title']
        lines.append(f"\U0001f3b5 **Now playing:** {title_link}")
    if queue:
        lines.append(f"\n**Up next ({len(queue)} song{'s' if len(queue) != 1 else ''}):**")
        shown = 0
        for i, track in enumerate(queue, start=1):
            video_url = track.get("video_url", "")
            title_link = f"[{track['title']}]({video_url})" if video_url and video_url.startswith("http") else track['title']
            line = f"`{i}.` {title_link}"
            # Leave room for the "and X more" footer (~50 chars)
            if len("\n".join(lines)) + len(line) + 50 > 4096:
                remaining = len(queue) - shown
                lines.append(f"*... and {remaining} more*")
                break
            lines.append(line)
            shown += 1

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


# ── Voice recording ───────────────────────────────────────────────────────────

# guild_id -> {user_id, start_time, text_channel}
_active_recordings: dict[int, dict] = {}


async def _recording_finished(sink: discord.sinks.WaveSink, guild_id: int):
    meta = _active_recordings.pop(guild_id, None)
    if meta is None:
        return

    user_id: int = meta["user_id"]
    start_time: float = meta["start_time"]
    text_channel: discord.TextChannel = meta["text_channel"]
    duration = time.monotonic() - start_time

    audio_data = sink.audio_data.get(user_id)
    if not audio_data:
        await text_channel.send(embed=err_embed("No audio captured — make sure you were speaking."))
        return

    if _whisper_model is None:
        await text_channel.send(embed=err_embed(
            "Whisper is not installed. Run `pip install openai-whisper` to enable transcription."
        ))
        return

    await text_channel.send(embed=info_embed("Transcribing your audio... ⏳", title="Processing"))

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_data.file.read())
        tmp_path = f.name

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: _whisper_model.transcribe(tmp_path, language="th"))
        transcript = result["text"].strip()
    finally:
        os.unlink(tmp_path)

    db.save_voice_session(user_id, guild_id, duration, transcript)

    word_count = len(transcript.split()) if transcript else 0
    wpm = round((word_count / duration) * 60) if duration > 0 else 0
    preview = f"> {transcript[:400]}" if transcript else "> *(silence or no speech detected)*"

    embed = discord.Embed(title="Recording Saved", color=0x57F287)
    embed.add_field(name="Duration", value=f"{duration:.0f}s", inline=True)
    embed.add_field(name="Words", value=str(word_count), inline=True)
    embed.add_field(name="WPM", value=str(wpm), inline=True)
    embed.add_field(name="Transcript", value=preview, inline=False)
    await text_channel.send(embed=embed)


@bot.tree.command(name="record", description="Start or stop recording your voice for habit tracking")
@app_commands.describe(action="start or stop")
@app_commands.choices(action=[
    app_commands.Choice(name="start", value="start"),
    app_commands.Choice(name="stop", value="stop"),
])
async def record(interaction: discord.Interaction, action: str):
    guild_id = interaction.guild.id

    if action == "start":
        if interaction.user.voice is None:
            await interaction.response.send_message(embed=err_embed("Join a voice channel first."), ephemeral=True)
            return
        if guild_id in _active_recordings:
            await interaction.response.send_message(embed=err_embed("Already recording. Use `/record stop` first."), ephemeral=True)
            return

        vc = interaction.guild.voice_client
        voice_channel = interaction.user.voice.channel
        if vc is None:
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        _active_recordings[guild_id] = {
            "user_id": interaction.user.id,
            "start_time": time.monotonic(),
            "text_channel": interaction.channel,
        }
        vc.start_recording(
            discord.sinks.WaveSink(),
            _recording_finished,
            guild_id,
        )
        await interaction.response.send_message(embed=ok_embed("Recording started. Use `/record stop` when you're done. 🎙️"))

    elif action == "stop":
        if guild_id not in _active_recordings:
            await interaction.response.send_message(embed=err_embed("No active recording. Use `/record start` first."), ephemeral=True)
            return
        vc = interaction.guild.voice_client
        if vc is None:
            _active_recordings.pop(guild_id, None)
            await interaction.response.send_message(embed=err_embed("No voice client found."), ephemeral=True)
            return
        vc.stop_recording()  # triggers _recording_finished
        await interaction.response.send_message(embed=ok_embed("Stopped recording. Processing..."))


@bot.tree.command(name="habits", description="View your voice speaking stats and recent transcripts")
async def habits(interaction: discord.Interaction):
    user_id = interaction.user.id
    guild_id = interaction.guild.id

    stats = db.get_voice_stats(user_id, guild_id)
    recent = db.get_voice_habits(user_id, guild_id, limit=3)

    if stats["sessions"] == 0:
        await interaction.response.send_message(embed=info_embed("No recordings yet. Use `/record start` to begin.", title="Your Speaking Habits"))
        return

    total_min = int(stats["total_sec"] // 60)
    avg_wpm = round((stats["total_words"] / stats["total_sec"]) * 60) if stats["total_sec"] > 0 else 0

    embed = discord.Embed(title="Your Speaking Habits 🎙️", color=0x5865F2)
    embed.add_field(name="Sessions", value=str(stats["sessions"]), inline=True)
    embed.add_field(name="Total Time", value=f"{total_min}m", inline=True)
    embed.add_field(name="Avg WPM", value=str(avg_wpm), inline=True)

    for row in recent:
        transcript_preview = (row["transcript"] or "*(no transcript)*")[:120]
        duration = f"{row['duration_sec']:.0f}s"
        embed.add_field(
            name=f"{row['recorded_at'][:16]}  •  {duration}  •  {row['word_count']} words",
            value=f"> {transcript_preview}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)