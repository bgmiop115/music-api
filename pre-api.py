from flask import Flask, request, jsonify, send_file
import yt_dlp
import sqlite3
import os
import secrets
import json
import uuid
import time
import glob as globmod
import threading
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "api_keys.db")
STREAM_DIR = "/tmp/yt_streams"
os.makedirs(STREAM_DIR, exist_ok=True)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            owner TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            requests_today INTEGER DEFAULT 0,
            daily_limit INTEGER DEFAULT 500,
            is_active INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT,
            endpoint TEXT,
            query TEXT,
            ip TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stream_cache (
            token TEXT PRIMARY KEY,
            video_url TEXT NOT NULL,
            format_str TEXT,
            stream_type TEXT DEFAULT 'vid',
            title TEXT,
            file_path TEXT,
            content_type TEXT DEFAULT 'video/mp4',
            created_at REAL
        )
    """)
    # Clean expired entries and temp files (6 hours)
    c.execute("SELECT file_path FROM stream_cache WHERE created_at < ?", (time.time() - 21600,))
    for row in c.fetchall():
        if row[0] and os.path.exists(row[0]):
            try: os.remove(row[0])
            except: pass
    c.execute("DELETE FROM stream_cache WHERE created_at < ?", (time.time() - 21600,))
    # Default key for devil
    c.execute("SELECT COUNT(*) FROM api_keys WHERE key = 'devil'")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO api_keys (key, owner, daily_limit) VALUES ('devil', 'devil', -1)")
    else:
        c.execute("UPDATE api_keys SET daily_limit = -1 WHERE key = 'devil'")
    conn.commit()
    conn.close()


def validate_key(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT owner, is_active, requests_today, daily_limit FROM api_keys WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False, "Invalid API key"
    if not row[1]:
        return False, "API key is disabled"
    if row[3] != -1 and row[2] >= row[3]:
        return False, "Daily limit exceeded"
    return True, row[0]


def log_request(key, endpoint, query, ip):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO request_logs (api_key, endpoint, query, ip) VALUES (?, ?, ?, ?)",
              (key, endpoint, query, ip))
    c.execute("UPDATE api_keys SET requests_today = requests_today + 1 WHERE key = ?", (key,))
    conn.commit()
    conn.close()


def search_youtube(query, max_results=1):
    """Search YouTube and return video info list."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    entries = result.get("entries", [])
    return entries


def get_stream_info(video_url, stream_type="vid", quality="720"):
    """Extract video info and download file for streaming."""
    if stream_type == "audio":
        fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
    else:
        # bestvideo picks video-only streams, yt-dlp merges with bestaudio via ffmpeg
        video_formats = {
            "360": "18/bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio",
            "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/18",
            "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/22/18",
            "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/22/18",
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        }
        fmt = video_formats.get(quality, video_formats["720"])

    token = uuid.uuid4().hex
    ext = "m4a" if stream_type == "audio" else "mp4"
    file_path = os.path.join(STREAM_DIR, f"{token}.{ext}")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "outtmpl": file_path,
        "merge_output_format": "mp4" if stream_type != "audio" else None,
    }
    # Remove None values
    ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    # yt-dlp may change extension
    if not os.path.exists(file_path):
        for f in globmod.glob(os.path.join(STREAM_DIR, f"{token}.*")):
            file_path = f
            ext = f.rsplit(".", 1)[-1]
            break

    actual_quality = f"{info.get('height', 'N/A')}p" if stream_type != "audio" else f"{info.get('abr', 'N/A')}kbps"
    filesize = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    return {
        "token": token,
        "file_path": file_path,
        "ext": ext,
        "info": {
            "success": True,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "webpage_url": info.get("webpage_url"),
            "quality": actual_quality,
            "filesize_mb": round(filesize / (1024 * 1024), 2) if filesize else None,
            "type": "audio" if stream_type == "audio" else "video",
            "developer": "@DEVIL_KING_9"
        }
    }


# ─── Routes ─────────────────────────────────────────────────────────

@app.route("/")
def home():
    host = request.host_url.rstrip("/")
    return jsonify({
        "status": "online",
        "endpoints": {
            "stream": {
                "url": f"{host}/api/ytstream",
                "params": {
                    "key": "your_api_key",
                    "song": "song name",
                    "type": "vid or audio",
                    "quality": "360 / 480 / 720 / 1080 / best"
                },
                "example": f"{host}/api/ytstream?key=YOUR_KEY&song=kalank&type=vid&quality=720"
            },
            "search": {
                "url": f"{host}/api/ytsearch",
                "params": {
                    "key": "your_api_key",
                    "song": "search query",
                    "limit": "number of results (default 5)"
                },
                "example": f"{host}/api/ytsearch?key=YOUR_KEY&song=arijit+singh&limit=5"
            },
            "stats": {
                "url": f"{host}/api/stats",
                "params": {"key": "your_api_key"},
                "example": f"{host}/api/stats?key=YOUR_KEY"
            }
        },
        "quality_options": ["360", "480", "720 (default)", "1080", "best"],
        "developer": "@DEVIL_KING_9"
    })


@app.route("/api/ytstream")
def ytstream():
    key = request.args.get("key")
    song = request.args.get("song")
    stream_type = request.args.get("type", "vid")
    quality = request.args.get("quality", "720")

    if not key:
        return jsonify({"success": False, "error": "API key required (key=)"}), 401
    if not song:
        return jsonify({"success": False, "error": "Song name required (song=)"}), 400

    valid, msg = validate_key(key)
    if not valid:
        return jsonify({"success": False, "error": msg}), 403

    try:
        entries = search_youtube(song, max_results=1)
        if not entries:
            return jsonify({"success": False, "error": "No results found"}), 404

        video_url = entries[0].get("url") or f"https://www.youtube.com/watch?v={entries[0]['id']}"
        data = get_stream_info(video_url, stream_type, quality)

        # Store in cache
        token = data["token"]
        content_type = "audio/mp4" if stream_type == "audio" else "video/mp4"
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM stream_cache WHERE created_at < ?", (time.time() - 21600,))
        c.execute(
            "INSERT INTO stream_cache (token, video_url, format_str, stream_type, title, file_path, content_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token, video_url, "", stream_type,
             data["info"].get("title", ""), data["file_path"],
             content_type, time.time())
        )
        conn.commit()
        conn.close()

        host = request.host_url.rstrip("/")
        result = data["info"]
        result["stream_url"] = f"{host}/api/play/{token}"

        log_request(key, "/api/ytstream", song, request.remote_addr)
        return jsonify(result)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/ytsearch")
def ytsearch():
    key = request.args.get("key")
    song = request.args.get("song")
    limit = int(request.args.get("limit", 5))

    if not key:
        return jsonify({"success": False, "error": "API key required"}), 401
    if not song:
        return jsonify({"success": False, "error": "Song name required"}), 400

    valid, msg = validate_key(key)
    if not valid:
        return jsonify({"success": False, "error": msg}), 403

    try:
        entries = search_youtube(song, max_results=min(limit, 20))
        results = []
        for e in entries:
            results.append({
                "title": e.get("title"),
                "id": e.get("id"),
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "duration": e.get("duration"),
                "channel": e.get("channel") or e.get("uploader"),
            })
        log_request(key, "/api/ytsearch", song, request.remote_addr)
        return jsonify({"success": True, "results": results, "count": len(results)})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/genkey")
def generate_key():
    owner = request.args.get("owner")
    if not owner:
        return jsonify({"success": False, "error": "Owner name required (owner=)"}), 400

    new_key = secrets.token_hex(8)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO api_keys (key, owner) VALUES (?, ?)", (new_key, owner))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"success": False, "error": "Try again"}), 500
    conn.close()

    return jsonify({
        "success": True,
        "key": new_key,
        "owner": owner,
        "daily_limit": 500,
        "message": "Save your key! Use it as: /api/ytstream?key=YOUR_KEY&song=kalank&type=vid"
    })


@app.route("/api/stats")
def stats():
    key = request.args.get("key")
    if not key:
        return jsonify({"success": False, "error": "API key required"}), 401

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT owner, requests_today, daily_limit, created_at FROM api_keys WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"success": False, "error": "Invalid key"}), 403

    return jsonify({
        "success": True,
        "owner": row[0],
        "requests_today": row[1],
        "daily_limit": row[2],
        "created_at": row[3]
    })


@app.route("/api/play/<token>")
def play_stream(token):
    """Serve downloaded file directly — smooth playback guaranteed."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_path, content_type, title FROM stream_cache WHERE token = ?", (token,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"success": False, "error": "Invalid or expired stream token"}), 404

    file_path, content_type, title = row

    if not file_path or not os.path.exists(file_path):
        return jsonify({"success": False, "error": "Stream file not found"}), 404

    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else "mp4"
    download_name = f"{title or 'stream'}.{ext}"

    return send_file(
        file_path,
        mimetype=content_type or "video/mp4",
        as_attachment=False,
        download_name=download_name
    )


# ─── Main ────────────────────────────────────────────────────────────

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8282, debug=False)
