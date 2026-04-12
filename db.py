import sqlite3
import os
from contextlib import contextmanager

_data_dir = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(_data_dir, "bot.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS voice_habits (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                guild_id     INTEGER NOT NULL,
                recorded_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                duration_sec REAL    NOT NULL,
                word_count   INTEGER NOT NULL DEFAULT 0,
                transcript   TEXT
            );

            CREATE TABLE IF NOT EXISTS playlists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                owner_id    INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                UNIQUE(name, guild_id)
            );

            CREATE TABLE IF NOT EXISTS playlist_songs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL,
                title       TEXT    NOT NULL,
                video_url   TEXT    NOT NULL,
                duration    INTEGER,
                UNIQUE(playlist_id, position)
            );
        """)


# ── Playlist helpers ────────────────────────────────────────────────────────────

def create_playlist(name: str, owner_id: int, guild_id: int) -> int | None:
    """Returns the new playlist id, or None if the name is already taken."""
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO playlists (name, owner_id, guild_id) VALUES (?, ?, ?)",
                (name, owner_id, guild_id),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_playlist(name: str, guild_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM playlists WHERE name = ? AND guild_id = ?",
            (name, guild_id),
        ).fetchone()


def list_playlists(guild_id: int):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT p.id, p.name, p.owner_id, COUNT(s.id) AS song_count
            FROM playlists p
            LEFT JOIN playlist_songs s ON s.playlist_id = p.id
            WHERE p.guild_id = ?
            GROUP BY p.id
            ORDER BY p.name
            """,
            (guild_id,),
        ).fetchall()


def delete_playlist(playlist_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


# ── Song helpers ────────────────────────────────────────────────────────────────

def add_song(playlist_id: int, title: str, video_url: str, duration: int | None = None):
    with get_db() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO playlist_songs (playlist_id, position, title, video_url, duration) VALUES (?, ?, ?, ?, ?)",
            (playlist_id, max_pos + 1, title, video_url, duration),
        )


def get_songs(playlist_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM playlist_songs WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()


def remove_song(playlist_id: int, position: int) -> bool:
    """Remove a song by position and re-number remaining songs. Returns True if removed."""
    with get_db() as conn:
        rows = conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND position = ?",
            (playlist_id, position),
        ).rowcount
        if rows:
            conn.execute(
                "UPDATE playlist_songs SET position = position - 1 WHERE playlist_id = ? AND position > ?",
                (playlist_id, position),
            )
        return bool(rows)


# ── Voice habits helpers ─────────────────────────────────────────────────────────

def save_voice_session(user_id: int, guild_id: int, duration_sec: float, transcript: str):
    word_count = len(transcript.split()) if transcript else 0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO voice_habits (user_id, guild_id, duration_sec, word_count, transcript) VALUES (?, ?, ?, ?, ?)",
            (user_id, guild_id, duration_sec, word_count, transcript),
        )


def get_voice_habits(user_id: int, guild_id: int, limit: int = 5):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT recorded_at, duration_sec, word_count, transcript
            FROM voice_habits
            WHERE user_id = ? AND guild_id = ?
            ORDER BY recorded_at DESC
            LIMIT ?
            """,
            (user_id, guild_id, limit),
        ).fetchall()


def get_voice_stats(user_id: int, guild_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as sessions,
                   COALESCE(SUM(duration_sec), 0) as total_sec,
                   COALESCE(SUM(word_count), 0) as total_words
            FROM voice_habits
            WHERE user_id = ? AND guild_id = ?
            """,
            (user_id, guild_id),
        ).fetchone()
        return dict(row) if row else {"sessions": 0, "total_sec": 0, "total_words": 0}
