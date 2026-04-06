"""
Minimal Spotify scraper — no API key required.
Scrapes the public embed page JSON to get track/playlist/album info.
Matches the spotify_scraper.SpotifyClient interface used in MyBot.py.
"""

import re
import json
import requests


_EMBED_URL = "https://open.spotify.com/embed/{type}/{id}"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def _extract_id(url: str) -> tuple[str, str]:
    """Return (type, id) from a Spotify URL."""
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?([a-z]+)/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError(f"Cannot parse Spotify URL: {url}")
    return match.group(1), match.group(2)


def _fetch_embed_json(spotify_type: str, spotify_id: str) -> dict:
    embed_url = _EMBED_URL.format(type=spotify_type, id=spotify_id)
    resp = requests.get(embed_url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)
    if not match:
        raise RuntimeError("Could not find embed JSON in Spotify page")
    return json.loads(match.group(1))


class SpotifyClient:
    def get_track_info(self, url: str) -> dict:
        _, spotify_id = _extract_id(url)
        data = _fetch_embed_json("track", spotify_id)
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        return {
            "name": entity["name"],
            "artists": [{"name": a["name"]} for a in entity.get("artists", [])],
        }

    def get_playlist_info(self, url: str) -> dict:
        _, spotify_id = _extract_id(url)
        data = _fetch_embed_json("playlist", spotify_id)
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        tracks = [
            {
                "name": item["name"],
                "artists": [{"name": a["name"]} for a in item.get("artists", [])],
            }
            for item in entity.get("trackList", [])
            if item
        ]
        return {"name": entity.get("name", ""), "tracks": tracks}

    def get_album_info(self, url: str) -> dict:
        _, spotify_id = _extract_id(url)
        data = _fetch_embed_json("album", spotify_id)
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        album_artists = entity.get("artists", [])
        tracks = [
            {
                "name": item["name"],
                "artists": [{"name": a["name"]} for a in item.get("artists", [])] or album_artists,
            }
            for item in entity.get("trackList", [])
            if item
        ]
        return {"name": entity.get("name", ""), "tracks": tracks}
