# All discord Embeds
import discord
import asyncio

def make_now_playing_embed(track: dict) -> discord.Embed:
    embed = discord.Embed(title=track["title"],
                            url=track.get("video_url"),
                            color=discord.Color.blurple())
    embed.set_author(name="Now Playing \U0001f3b5")
    if track.get("thumbnail"):
        embed.set_image(url=trackk["thumbnail"])
    return embed

def ok_embed(description: str) -> discord.Embed:
    return discord.Embed(description=description, color=discord.Color.green())

def info_embed(description: str, title: str = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.blurple())

def err_embed(description: str, title: str = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())