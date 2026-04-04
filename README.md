# Discord Music Bot

A Discord music bot that plays YouTube audio, manages a queue, and lets you save playlists — backed by SQLite.

---

## Setup

1. **Clone the repo** onto your machine or Raspberry Pi.

2. **Create a `.env` file** in the project root:
   ```
   DISCORD_TOKEN=your_discord_bot_token_here
   GUILD_ID=your_server_id_here
   ```

3. **Run with Docker:**
   ```bash
   docker compose up -d --build
   ```
   The bot will start automatically and restart if it crashes. The database is saved in `./data/bot.db`.

4. **Run locally (Windows)** without Docker:
   ```bash
   dc_env\Scripts\activate
   pip install -r requirements.txt
   python MyBot.py
   ```
   Make sure `bin\ffmpeg\ffmpeg.exe` exists, or add `FFMPEG_EXECUTABLE=path\to\ffmpeg.exe` to your `.env`.

---

## Commands

### Playback

| Command | What it does |
|---|---|
| `/play <song>` | Search YouTube and play a song. If something is already playing, it gets added to the queue instead. |
| `/skip` | Skip the current song and play the next one in the queue. |
| `/stop` | Stop playback, clear the queue, and disconnect the bot from voice. |
| `/queue` | Show what's currently playing and what's coming up next. |

### Playlists

Playlists are saved permanently in the database and shared across the whole server.

| Command | What it does |
|---|---|
| `/playlist create <name>` | Create a new empty playlist. |
| `/playlist add <name> <song>` | Search for a song and add it to the playlist. |
| `/playlist play <name>` | Load all songs from a playlist into the queue and start playing. |
| `/playlist list` | Show all playlists in this server. |
| `/playlist view <name>` | Show all songs in a playlist with their position numbers and durations. |
| `/playlist remove <name> <position>` | Remove a song from a playlist by its position number (use `/playlist view` to find it). |
| `/playlist delete <name>` | Permanently delete a playlist. Only the person who created it can do this. |

dc_env\Scripts\activate

https://mermaid.ai/app/projects/a1eda080-c87c-4942-bd73-292b20e25d1f/diagrams/60d03854-83cd-4b17-9976-0b7d03a6c130/share/invite/eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJkb2N1bWVudElEIjoiNjBkMDM4NTQtODNjZC00YjE3LTk5NzYtMGI3ZDAzYTZjMTMwIiwiYWNjZXNzIjoiVmlldyIsImlhdCI6MTc3NTI4MjQzM30.Wb9OilRR8jP1DnKDEYB53dhC-4o6B2isda1UxCiM8TA