# Wireline — Chat, File Sharing, Voice & Video Calls

A self-hosted, real-time chat web app built with **Python (Flask + Flask-SocketIO)**
on the backend and **WebRTC** for peer-to-peer voice/video calls.

## Features

- **Real-time chat** — instant messaging per "channel" (room), powered by Socket.IO.
- **File sharing** — drag/pick a file, it uploads to the server and a download
  link is broadcast to everyone in the channel.
- **Voice calls** — one click, browser-to-browser audio using WebRTC.
- **Video calls** — same flow, with camera video. Multiple people in a channel
  can be in the same call (mesh topology — each browser connects directly to
  every other participant).
- **Presence list** — see who's in the channel and who's currently in a call.

The Python server only handles chat relay, file storage, and WebRTC
*signaling* (passing small connection-setup messages between browsers).
Once a call is established, audio/video flows directly peer-to-peer, not
through the server.

## Requirements

- Python 3.9+
- A modern browser (Chrome, Edge, Firefox, Safari) — needs camera/mic
  permissions for calls.
- **HTTPS or localhost**: browsers only allow camera/microphone access on
  `localhost` or over HTTPS. Running on `localhost` (as below) works out of
  the box. If you deploy this to a server, put it behind HTTPS (e.g. via
  nginx + Let's Encrypt, or a tunnel like ngrok/Cloudflare Tunnel) or calls
  won't be able to request camera/mic access.

## Setup

```bash
# 1. Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate      # on Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the server
python app.py
```

Then open **http://localhost:5000** in two (or more) browser tabs/devices to
try it out — pick the same channel name in each, use different callsigns.

## How it works

```
chatapp/
├── app.py              # Flask + Flask-SocketIO server
│                        #   - serves the page
│                        #   - /upload and /download routes for files
│                        #   - Socket.IO events for chat + WebRTC signaling
├── requirements.txt
├── templates/
│   └── index.html       # Join screen + main chat/call UI
└── static/
    ├── style.css         # "Wireline" visual identity
    ├── main.js           # Socket.IO client, file upload, WebRTC mesh logic
    └── uploads/          # Uploaded files land here, organized by channel
```

### Chat
The client sends `chat_message` events over Socket.IO; the server relays them
to everyone else in the same room (`room` = Socket.IO room = your channel
name).

### File sharing
Files are **not** sent over the WebSocket. The browser POSTs the file to
`/upload` (a normal HTTP multipart upload) using `XMLHttpRequest`, so you get
a progress bar for large files. The server stores it under
`static/uploads/<channel>/` and returns a JSON payload (filename, size,
download URL). The client then emits `file_shared` with that payload so
everyone in the channel sees a download link appear in the chat.

### Voice / video calls
1. Clicking **Voice** or **Video** calls `getUserMedia()` to grab your
   mic/camera, then emits `join_call`.
2. The server tells you who else is already "in the call" in that channel.
3. Your browser creates one `RTCPeerConnection` **per existing participant**
   and sends a WebRTC **offer** through the server (`webrtc_offer`).
4. Each peer replies with an **answer** (`webrtc_answer`), and both sides
   exchange **ICE candidates** (`webrtc_ice_candidate`) until a direct
   peer-to-peer connection is established.
5. From then on, audio/video streams directly between browsers — the
   Python server is no longer in the media path at all.

This is a **mesh** call model: fine for small groups (2–6 people). For large
group calls you'd typically switch to an SFU (e.g. mediasoup, LiveKit,
Janus), which is a bigger architectural change.

## Configuration notes

- `app.config["SECRET_KEY"]` in `app.py` — change this before deploying
  anywhere real.
- `MAX_CONTENT_LENGTH` in `app.py` — max upload size (default 100 MB).
- `ICE_SERVERS` in `static/main.js` — currently just public Google STUN
  servers. If users are behind restrictive NATs/firewalls (common on
  corporate networks or mobile carriers), calls may fail to connect — in
  that case you'll also need a **TURN server** (e.g. coturn, or a hosted
  TURN provider) and add it to `ICE_SERVERS`.

## Running with production settings

`python app.py` runs Flask-SocketIO's built-in dev server. For real
deployments, run with eventlet/gunicorn, e.g.:

```bash
pip install gunicorn
gunicorn -k eventlet -w 1 app:app
```

(Only 1 worker is supported per process unless you add a message queue like
Redis for Socket.IO to coordinate across workers — see the
[Flask-SocketIO docs](https://flask-socketio.readthedocs.io/en/latest/deployment.html)
for multi-worker/multi-server setups.)
