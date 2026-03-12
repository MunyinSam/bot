import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque
import db
from embed import make_now_playing_embed, ok_embed, info_embed, err_embed

# Import Configs
from config import TOKEN, GUILD_ID, FFMPEG_EXECUTABLE, FFMPEG_OPTIONS, YDL_OPTIONS


db.init_db()
# guild_id -> deque of {"title": str, "audio_url": str|None, "video_url": str, "thumbnail": str|None}
queues: dict[int, deque] = {}
now_playing: dict[int, dict] = {}
guild_text_channels: dict[int, discord.TextChannel] = {}


# BOT LOGIC

async def fetch_tract(query_or_url: str) -> dict | None:
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