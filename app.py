"""
PyChat - A real-time chat web app with file sharing, voice calls, and video calls.

Backend responsibilities:
1. Serve the web UI.
2. Relay real-time chat messages between users in the same room (Socket.IO).
3. Accept file uploads over HTTP and broadcast a shareable download link.
4. Act as a WebRTC "signaling server" - it never touches actual audio/video/
   file bytes for calls, it just passes small JSON messages (offer/answer/
   ICE candidates) between browsers so they can set up a direct peer-to-peer
   media connection.

Run with:  python app.py
Then open: http://localhost:5000
"""

import os
import uuid
import time
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 100 * 1024 * 1024  # 100 MB max upload size

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret-in-production"
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=MAX_CONTENT_LENGTH)

# In-memory state (fine for a demo / single-process app).
# rooms = { room_name: { sid: {"username": str, "in_call": bool} } }
rooms = {}


def room_user_list(room):
    return [
        {"sid": sid, "username": info["username"], "in_call": info["in_call"]}
        for sid, info in rooms.get(room, {}).items()
    ]


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_file():
    """Accepts a file upload and stores it under a per-room folder.
    Returns JSON metadata the client will broadcast over the socket."""
    room = request.form.get("room", "default")
    username = request.form.get("username", "anonymous")

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
        "username": username,
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
    pass


@socketio.on("join")
def on_join(data):
    room = data.get("room", "default")
    username = data.get("username", "anonymous")

    join_room(room)
    rooms.setdefault(room, {})[request.sid] = {"username": username, "in_call": False}

    emit("system_message", {
        "text": f"{username} joined the room.",
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }, room=room)

    emit("user_list", room_user_list(room), room=room)


@socketio.on("chat_message")
def on_chat_message(data):
    room = data.get("room", "default")
    username = data.get("username", "anonymous")
    text = data.get("text", "")

    emit("chat_message", {
        "username": username,
        "text": text,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }, room=room, include_self=False)


@socketio.on("file_shared")
def on_file_shared(data):
    """Broadcast file metadata (already uploaded via /upload) to the room."""
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
# The server only relays messages between two specific peers (by sid).
# It never sees the actual audio/video stream - that flows directly
# between browsers once the connection is negotiated.

@socketio.on("join_call")
def on_join_call(data):
    room = data.get("room", "default")
    call_type = data.get("call_type", "video")  # "video" or "audio"

    if room in rooms and request.sid in rooms[room]:
        rooms[room][request.sid]["in_call"] = True

    existing_peers = [
        sid for sid, info in rooms.get(room, {}).items()
        if info["in_call"] and sid != request.sid
    ]

    # Tell the newcomer who is already in the call, so it can create offers.
    emit("existing_call_peers", {"peers": existing_peers, "call_type": call_type})

    # Tell existing participants someone new is available (for UI/state only).
    emit("peer_joined_call", {"sid": request.sid}, room=room, include_self=False)


@socketio.on("leave_call")
def on_leave_call(data):
    room = data.get("room", "default")
    if room in rooms and request.sid in rooms[room]:
        rooms[room][request.sid]["in_call"] = False
    emit("peer_left_call", {"sid": request.sid}, room=room, include_self=False)


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
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
