import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque
import db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_EXECUTABLE", "ffmpeg")

db.init_db()

# guild_id -> deque of {"title": str, "audio_url": str|None, "video_url": str}
queues: dict[int, deque] = {}
# guild_id -> title of the currently playing track
now_playing: dict[int, str] = {}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -c:a libopus -b:a 96k",
}

YDL_OPTIONS = {
    "format": "bestaudio[abr<=96]/bestaudio",
    "noplaylist": True,
    "youtube_include_dash_manifest": False,
    "youtube_include_hit_manifest": False,
}


async def fetch_track(query_or_url: str) -> dict | None:
    """Resolve a search query or YouTube URL to a track info dict via yt-dlp."""
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            result = ydl.extract_info(query_or_url, download=False)
            if result is None:
                return None
            if "entries" in result:
                return result["entries"][0] if result["entries"] else None
            return result

    return await loop.run_in_executor(None, _extract)


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def play_next(guild: discord.Guild):
    """Play the next track in the guild's queue, or stop if empty."""
    vc = guild.voice_client
    queue = queues.get(guild.id)

    if not queue or not vc or vc.is_playing():
        now_playing.pop(guild.id, None)
        return

    track = queue.popleft()
    audio_url = track.get("audio_url")

    if not audio_url:
        # Resolve stored YouTube URL to a fresh audio stream
        info = await fetch_track(track["video_url"])
        if info is None:
            await play_next(guild)  # skip unresolvable track
            return
        audio_url = info["url"]

    now_playing[guild.id] = track["title"]
    source = discord.FFmpegOpusAudio(
        audio_url, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTIONS
    )

    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        now_playing.pop(guild.id, None)
        asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)

    vc.play(source, after=after_playing)


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    test_guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=test_guild)
    print(f"{bot.user} is online")

# @bot.event
# async def on_message(msg):
#     if msg.author.id != bot.user.id:
#         await msg.channel.send(f"Message from {msg.author.mention}")


# ── Playback commands ─────────────────────────────────────────────────────────

@bot.tree.command(name="sync_command", description="Sync latest commands")
async def sync_command(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message("Syncing New Commands")


@bot.tree.command(name="play", description="Play a song or add it to the queue")
@app_commands.describe(song_query="Song name or YouTube URL")
async def play(interaction: discord.Interaction, song_query: str):
    await interaction.response.defer()

    if interaction.user.voice is None:
        await interaction.followup.send("You are not in a voice channel.")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    query = f"ytsearch1:{song_query}" if not song_query.startswith("http") else song_query
    info = await fetch_track(query)
    if info is None:
        await interaction.followup.send("No results found.")
        return

    title = info.get("title", "Untitled")
    track = {
        "title": title,
        "audio_url": info["url"],
        "video_url": info.get("webpage_url", song_query),
    }

    guild_id = interaction.guild.id
    if guild_id not in queues:
        queues[guild_id] = deque()

    queues[guild_id].append(track)
    already_active = voice_client.is_playing() or voice_client.is_paused() or len(queues[guild_id]) > 1

    if already_active:
        await interaction.followup.send(f"Added to queue (#{len(queues[guild_id])}): **{title}**")
    else:
        await play_next(interaction.guild)
        await interaction.followup.send(f"Now playing: **{title}**")


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing right now.")
        return
    vc.stop()  # triggers after_playing -> play_next
    await interaction.response.send_message("Skipped.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queues.pop(guild_id, None)
    now_playing.pop(guild_id, None)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    queue = queues.get(guild_id, deque())
    current = now_playing.get(guild_id)

    if not current and not queue:
        await interaction.response.send_message("The queue is empty.")
        return

    lines = []
    if current:
        lines.append(f"Now playing: **{current}**")
    if queue:
        lines.append(f"\nUp next ({len(queue)} song{'s' if len(queue) != 1 else ''}):")
        for i, track in enumerate(queue, start=1):
            lines.append(f"{i}. {track['title']}")

    await interaction.response.send_message("\n".join(lines))


# ── Playlist commands ─────────────────────────────────────────────────────────

playlist_group = app_commands.Group(name="playlist", description="Manage saved playlists")


@playlist_group.command(name="create", description="Create a new playlist")
@app_commands.describe(name="Playlist name")
async def playlist_create(interaction: discord.Interaction, name: str):
    playlist_id = db.create_playlist(name, interaction.user.id, interaction.guild.id)
    if playlist_id is None:
        await interaction.response.send_message(f"A playlist named **{name}** already exists in this server.")
    else:
        await interaction.response.send_message(f"Playlist **{name}** created.")


@playlist_group.command(name="add", description="Add a song to a playlist")
@app_commands.describe(name="Playlist name", song_query="Song name or YouTube URL")
async def playlist_add(interaction: discord.Interaction, name: str, song_query: str):
    await interaction.response.defer()
    playlist = db.get_playlist(name, interaction.guild.id)
    if playlist is None:
        await interaction.followup.send(f"Playlist **{name}** not found.")
        return

    query = f"ytsearch1:{song_query}" if not song_query.startswith("http") else song_query
    info = await fetch_track(query)
    if info is None:
        await interaction.followup.send("Could not find that song.")
        return

    db.add_song(
        playlist["id"],
        info.get("title", "Untitled"),
        info.get("webpage_url", song_query),
        info.get("duration"),
    )
    await interaction.followup.send(f"Added **{info.get('title', 'Untitled')}** to playlist **{name}**.")


@playlist_group.command(name="play", description="Load a playlist into the queue")
@app_commands.describe(name="Playlist name")
async def playlist_play(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    if interaction.user.voice is None:
        await interaction.followup.send("You are not in a voice channel.")
        return

    playlist = db.get_playlist(name, interaction.guild.id)
    if playlist is None:
        await interaction.followup.send(f"Playlist **{name}** not found.")
        return

    songs = db.get_songs(playlist["id"])
    if not songs:
        await interaction.followup.send(f"Playlist **{name}** is empty.")
        return

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    guild_id = interaction.guild.id
    if guild_id not in queues:
        queues[guild_id] = deque()

    for song in songs:
        queues[guild_id].append({"title": song["title"], "audio_url": None, "video_url": song["video_url"]})

    if not voice_client.is_playing():
        await play_next(interaction.guild)

    await interaction.followup.send(f"Loaded **{len(songs)}** songs from **{name}** into the queue.")


@playlist_group.command(name="list", description="List all playlists in this server")
async def playlist_list(interaction: discord.Interaction):
    playlists = db.list_playlists(interaction.guild.id)
    if not playlists:
        await interaction.response.send_message("No playlists found in this server.")
        return
    lines = ["**Playlists:**"]
    for p in playlists:
        lines.append(f"- **{p['name']}** — {p['song_count']} song{'s' if p['song_count'] != 1 else ''}")
    await interaction.response.send_message("\n".join(lines))


@playlist_group.command(name="view", description="View songs in a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_view(interaction: discord.Interaction, name: str):
    playlist = db.get_playlist(name, interaction.guild.id)
    if playlist is None:
        await interaction.response.send_message(f"Playlist **{name}** not found.")
        return
    songs = db.get_songs(playlist["id"])
    if not songs:
        await interaction.response.send_message(f"Playlist **{name}** is empty.")
        return
    lines = [f"**{name}** ({len(songs)} songs):"]
    for s in songs:
        if s["duration"]:
            dur = f"{s['duration'] // 60}:{s['duration'] % 60:02d}"
        else:
            dur = "?:??"
        lines.append(f"{s['position']}. {s['title']} [{dur}]")
    await interaction.response.send_message("\n".join(lines))


@playlist_group.command(name="remove", description="Remove a song from a playlist by its position number")
@app_commands.describe(name="Playlist name", position="Position number shown in /playlist view")
async def playlist_remove(interaction: discord.Interaction, name: str, position: int):
    playlist = db.get_playlist(name, interaction.guild.id)
    if playlist is None:
        await interaction.response.send_message(f"Playlist **{name}** not found.")
        return
    removed = db.remove_song(playlist["id"], position)
    if removed:
        await interaction.response.send_message(f"Removed song at position {position} from **{name}**.")
    else:
        await interaction.response.send_message(f"No song found at position {position}.")


@playlist_group.command(name="delete", description="Delete a playlist you own")
@app_commands.describe(name="Playlist name")
async def playlist_delete(interaction: discord.Interaction, name: str):
    playlist = db.get_playlist(name, interaction.guild.id)
    if playlist is None:
        await interaction.response.send_message(f"Playlist **{name}** not found.")
        return
    if playlist["owner_id"] != interaction.user.id:
        await interaction.response.send_message("You can only delete playlists you created.")
        return
    db.delete_playlist(playlist["id"])
    await interaction.response.send_message(f"Deleted playlist **{name}**.")


bot.tree.add_command(playlist_group)
bot.run(TOKEN)