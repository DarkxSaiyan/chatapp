"""
PyChat - A real-time chat web app with file sharing, voice calls, video calls,
and Discord-style user accounts.

Backend responsibilities:
1. User accounts: register / log in / log out, stored in SQLite, passwords
   hashed with werkzeug's password hashing (never stored in plain text).
2. Serve the web UI (only to logged-in users).
3. Relay real-time chat messages between users in the same room (Socket.IO).
4. Accept file uploads over HTTP and broadcast a shareable download link.
5. Act as a WebRTC "signaling server" for voice/video calls - it never
   touches actual audio/video bytes, just small offer/answer/ICE messages.

Run with:  python app.py
Then open: http://localhost:5000
"""

import eventlet
eventlet.monkey_patch()

import os
import re
import uuid
import sqlite3
import hashlib
from datetime import datetime, timezone

from flask import (
    Flask, request, jsonify, render_template, send_from_directory,
    session, redirect, url_for,
)
from flask_socketio import SocketIO, join_room, emit
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
AVATAR_DIR = os.path.join(BASE_DIR, "static", "avatars")
DB_PATH = os.path.join(BASE_DIR, "chatapp.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB max upload size
ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,20}$")

# A small, on-brand palette for auto-generated (initials) avatars.
AVATAR_PALETTE = [
    "#49d3c4", "#ff6b6b", "#5c7cfa", "#f7b955",
    "#a78bfa", "#4fd6a8", "#ff9f6b", "#63c7ff",
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=MAX_CONTENT_LENGTH)

# In-memory presence state (fine for a single-process demo app).
# rooms = { room_name: { sid: {"user_id":, "username":, "avatar":, "in_call": bool} } }
rooms = {}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar_path TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


def avatar_color_for(username):
    h = int(hashlib.md5(username.lower().encode()).hexdigest(), 16)
    return AVATAR_PALETTE[h % len(AVATAR_PALETTE)]


def user_public_dict(row):
    """Shape a users-table row into what the client is allowed to see."""
    return {
        "id": row["id"],
        "username": row["username"],
        "avatar_url": f"/static/avatars/{row['avatar_path']}" if row["avatar_path"] else None,
        "avatar_color": avatar_color_for(row["username"]),
        "avatar_initial": row["username"][0].upper(),
    }


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return row


def login_required_page(view):
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth_page"))
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def room_user_list(room):
    return [
        {
            "sid": sid,
            "username": info["username"],
            "avatar_url": info["avatar_url"],
            "avatar_color": info["avatar_color"],
            "avatar_initial": info["avatar_initial"],
            "in_call": info["in_call"],
        }
        for sid, info in rooms.get(room, {}).items()
    ]


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------

@app.route("/auth")
def auth_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("auth.html")


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not USERNAME_RE.match(username):
        return jsonify({"error": "Username must be 3-20 characters: letters, numbers, underscore, or period."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "That username is already taken."}), 409

    password_hash = generate_password_hash(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, avatar_path, created_at) VALUES (?, ?, NULL, ?)",
        (username, password_hash, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    user_id = cur.lastrowid
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    session["user_id"] = user_id
    return jsonify({"user": user_public_dict(row)})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "Incorrect username or password."}), 401

    session["user_id"] = row["id"]
    return jsonify({"user": user_public_dict(row)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    row = current_user()
    if not row:
        return jsonify({"user": None})
    return jsonify({"user": user_public_dict(row)})


@app.route("/api/avatar", methods=["POST"])
def api_avatar():
    row = current_user()
    if not row:
        return jsonify({"error": "Not logged in."}), 401

    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    f = request.files["file"]
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_AVATAR_EXT:
        return jsonify({"error": "Use PNG, JPG, GIF, or WEBP."}), 400

    filename = f"user_{row['id']}.{ext}"
    f.save(os.path.join(AVATAR_DIR, filename))

    conn = get_db()
    conn.execute("UPDATE users SET avatar_path = ? WHERE id = ?", (filename, row["id"]))
    conn.commit()
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    conn.close()

    return jsonify({"user": user_public_dict(updated)})


# --------------------------------------------------------------------------
# App routes
# --------------------------------------------------------------------------

@app.route("/")
@login_required_page
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    """Accepts a file upload and stores it under a per-room folder.
    Returns JSON metadata the client will broadcast over the socket."""
    row = current_user()
    if not row:
        return jsonify({"error": "Not logged in."}), 401

    room = request.form.get("room", "default")

    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "no selected file"}), 400

    safe_room = secure_filename(room) or "default"
    room_dir = os.path.join(UPLOAD_DIR, safe_room)
    os.makedirs(room_dir, exist_ok=True)

    original_name = secure_filename(f.filename)
    unique_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
    filepath = os.path.join(room_dir, unique_name)
    f.save(filepath)

    size_bytes = os.path.getsize(filepath)

    return jsonify({
        "filename": original_name,
        "url": f"/download/{safe_room}/{unique_name}",
        "size": size_bytes,
        "room": room,
        "username": row["username"],
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    })


@app.route("/download/<room>/<filename>")
def download_file(room, filename):
    safe_room = secure_filename(room)
    room_dir = os.path.join(UPLOAD_DIR, safe_room)
    return send_from_directory(room_dir, filename, as_attachment=True)


# --------------------------------------------------------------------------
# Socket.IO events - presence & chat
# --------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    # Reject socket connections from anyone without a valid login session.
    if not session.get("user_id"):
        return False


@socketio.on("join")
def on_join(data):
    row = current_user()
    if not row:
        return
    room = data.get("room", "default")

    join_room(room)
    rooms.setdefault(room, {})[request.sid] = {
        "user_id": row["id"],
        "username": row["username"],
        "avatar_url": f"/static/avatars/{row['avatar_path']}" if row["avatar_path"] else None,
        "avatar_color": avatar_color_for(row["username"]),
        "avatar_initial": row["username"][0].upper(),
        "in_call": False,
    }

    emit("system_message", {
        "text": f"{row['username']} joined the room.",
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }, room=room)

    emit("user_list", room_user_list(room), room=room)


@socketio.on("chat_message")
def on_chat_message(data):
    row = current_user()
    if not row:
        return
    room = data.get("room", "default")
    text = data.get("text", "")

    emit("chat_message", {
        "username": row["username"],
        "avatar_url": f"/static/avatars/{row['avatar_path']}" if row["avatar_path"] else None,
        "avatar_color": avatar_color_for(row["username"]),
        "avatar_initial": row["username"][0].upper(),
        "text": text,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }, room=room, include_self=False)


@socketio.on("file_shared")
def on_file_shared(data):
    """Broadcast file metadata (already uploaded via /upload) to the room."""
    row = current_user()
    if not row:
        return
    room = data.get("room", "default")
    emit("file_shared", data, room=room, include_self=False)


@socketio.on("disconnect")
def on_disconnect():
    for room, members in list(rooms.items()):
        if request.sid in members:
            username = members[request.sid]["username"]
            del members[request.sid]
            emit("system_message", {
                "text": f"{username} left the room.",
                "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            }, room=room)
            emit("user_list", room_user_list(room), room=room)
            emit("peer_left", {"sid": request.sid}, room=room)
            if not members:
                del rooms[room]


# --------------------------------------------------------------------------
# Socket.IO events - WebRTC signaling (voice + video calls)
# --------------------------------------------------------------------------

@socketio.on("join_call")
def on_join_call(data):
    room = data.get("room", "default")
    call_type = data.get("call_type", "video")

    if room in rooms and request.sid in rooms[room]:
        rooms[room][request.sid]["in_call"] = True

    existing_peers = [
        sid for sid, info in rooms.get(room, {}).items()
        if info["in_call"] and sid != request.sid
    ]

    emit("existing_call_peers", {"peers": existing_peers, "call_type": call_type})
    emit("peer_joined_call", {"sid": request.sid}, room=room, include_self=False)
    emit("user_list", room_user_list(room), room=room)


@socketio.on("leave_call")
def on_leave_call(data):
    room = data.get("room", "default")
    if room in rooms and request.sid in rooms[room]:
        rooms[room][request.sid]["in_call"] = False
    emit("peer_left_call", {"sid": request.sid}, room=room, include_self=False)
    emit("user_list", room_user_list(room), room=room)


@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    target = data["target"]
    emit("webrtc_offer", {
        "sdp": data["sdp"],
        "sender": request.sid,
        "call_type": data.get("call_type", "video"),
    }, room=target)


@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    target = data["target"]
    emit("webrtc_answer", {
        "sdp": data["sdp"],
        "sender": request.sid,
    }, room=target)


@socketio.on("webrtc_ice_candidate")
def on_webrtc_ice_candidate(data):
    target = data["target"]
    emit("webrtc_ice_candidate", {
        "candidate": data["candidate"],
        "sender": request.sid,
    }, room=target)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)