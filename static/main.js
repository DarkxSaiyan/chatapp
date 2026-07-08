/* ==========================================================================
 * Wireline client
 * - Real-time chat over Socket.IO
 * - File sharing (HTTP upload + socket broadcast of metadata)
 * - Voice / video calling via WebRTC (mesh topology), signaled over Socket.IO
 * ========================================================================== */

const ICE_SERVERS = [
  { urls: "stun:stun.l.google.com:19302" },
  { urls: "stun:stun1.l.google.com:19302" },
];

let socket = null;
let username = "";
let room = "";
let mySid = null;

// call state
let localStream = null;
let currentCallType = null; // 'audio' | 'video' | null
let inCall = false;
const peerConnections = {}; // sid -> RTCPeerConnection
const remoteStreams = {};   // sid -> MediaStream

// ---------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------
const el = (id) => document.getElementById(id);

const joinScreen = el("join-screen");
const appScreen = el("app-screen");
const usernameInput = el("username-input");
const roomInput = el("room-input");
const joinBtn = el("join-btn");
const joinError = el("join-error");

const roomNameLabel = el("room-name-label");
const callBarRoom = el("call-bar-room");
const meLabel = el("me-label");
const userListEl = el("user-list");
const leaveBtn = el("leave-btn");

const messagesEl = el("messages");
const composer = el("composer");
const messageInput = el("message-input");
const attachBtn = el("attach-btn");
const fileInput = el("file-input");
const uploadProgress = el("upload-progress");
const uploadProgressBar = el("upload-progress-bar");
const uploadProgressLabel = el("upload-progress-label");

const voiceCallBtn = el("voice-call-btn");
const videoCallBtn = el("video-call-btn");
const hangupBtn = el("hangup-btn");
const callStage = el("call-stage");
const videoGrid = el("video-grid");
const toggleMicBtn = el("toggle-mic-btn");
const toggleCamBtn = el("toggle-cam-btn");

// ---------------------------------------------------------------------
// Join flow
// ---------------------------------------------------------------------
joinBtn.addEventListener("click", doJoin);
usernameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin(); });
roomInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin(); });

function doJoin() {
  const u = usernameInput.value.trim();
  const r = roomInput.value.trim();
  if (!u || !r) {
    joinError.textContent = "Enter both a callsign and a channel name.";
    return;
  }
  username = u;
  room = r;

  socket = io();
  wireSocketEvents();

  socket.on("connect", () => {
    mySid = socket.id;
    socket.emit("join", { username, room });
    joinScreen.classList.add("hidden");
    appScreen.classList.remove("hidden");
    roomNameLabel.textContent = "#" + room;
    callBarRoom.textContent = "#" + room;
    meLabel.textContent = username;
  });
}

leaveBtn.addEventListener("click", () => window.location.reload());

// ---------------------------------------------------------------------
// Socket event wiring (chat / presence / signaling)
// ---------------------------------------------------------------------
function wireSocketEvents() {
  socket.on("system_message", (data) => addSystemMessage(data.text));

  socket.on("user_list", (users) => renderUserList(users));

  socket.on("chat_message", (data) => {
    addChatMessage(data.username, data.text, data.timestamp, false);
  });

  socket.on("file_shared", (data) => {
    addFileMessage(data.username, data.filename, data.url, data.size, data.timestamp, false);
  });

  socket.on("peer_left", (data) => {
    closePeerConnection(data.sid);
  });

  // ---- WebRTC signaling ----
  socket.on("existing_call_peers", async (data) => {
    // I just joined the call; create an offer to each existing participant.
    currentCallType = data.call_type;
    for (const sid of data.peers) {
      await createPeerConnection(sid, true);
    }
  });

  socket.on("webrtc_offer", async (data) => {
    currentCallType = data.call_type || currentCallType || "video";
    if (!inCall) {
      // Someone is calling us and we haven't joined the call UI yet.
      const accept = confirm(`Incoming ${currentCallType} call. Join?`);
      if (!accept) return;
      await startLocalMedia(currentCallType);
      showCallStage();
      inCall = true;
      socket.emit("join_call", { room, call_type: currentCallType });
    }
    const pc = await createPeerConnection(data.sender, false);
    await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    socket.emit("webrtc_answer", { target: data.sender, sdp: answer });
  });

  socket.on("webrtc_answer", async (data) => {
    const pc = peerConnections[data.sender];
    if (pc) await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
  });

  socket.on("webrtc_ice_candidate", async (data) => {
    const pc = peerConnections[data.sender];
    if (pc && data.candidate) {
      try { await pc.addIceCandidate(new RTCIceCandidate(data.candidate)); }
      catch (err) { console.warn("ICE add error", err); }
    }
  });

  socket.on("peer_left_call", (data) => closePeerConnection(data.sid));
}

// ---------------------------------------------------------------------
// Chat rendering
// ---------------------------------------------------------------------
function addSystemMessage(text) {
  const div = document.createElement("div");
  div.className = "msg system";
  div.innerHTML = `<div class="msg-bubble">${escapeHtml(text)}</div>`;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function addChatMessage(user, text, timestamp, mine) {
  const div = document.createElement("div");
  div.className = "msg" + (mine ? " mine" : "");
  div.innerHTML = `
    <div class="msg-meta">${escapeHtml(user)} · ${timestamp}</div>
    <div class="msg-bubble">${escapeHtml(text)}</div>
  `;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function addFileMessage(user, filename, url, size, timestamp, mine) {
  const div = document.createElement("div");
  div.className = "msg" + (mine ? " mine" : "");
  div.innerHTML = `
    <div class="msg-meta">${escapeHtml(user)} · ${timestamp}</div>
    <div class="msg-bubble file-card">
      <span class="file-ico">&#128206;</span>
      <div>
        <div><a href="${url}" target="_blank" rel="noopener">${escapeHtml(filename)}</a></div>
        <div class="file-meta">${formatBytes(size)}</div>
      </div>
    </div>
  `;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function renderUserList(users) {
  userListEl.innerHTML = "";
  users.forEach((u) => {
    const li = document.createElement("li");
    const dotClass = u.in_call ? "signal-dot in-call" : "signal-dot on";
    li.innerHTML = `<span class="${dotClass}"></span> ${escapeHtml(u.username)}${u.sid === mySid ? " (you)" : ""} <span class="tag">${u.in_call ? "in call" : ""}</span>`;
    userListEl.appendChild(li);
  });
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

// ---------------------------------------------------------------------
// Sending chat messages
// ---------------------------------------------------------------------
composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = messageInput.value.trim();
  if (!text) return;
  const timestamp = new Date().toTimeString().slice(0, 8);
  socket.emit("chat_message", { room, username, text });
  addChatMessage(username, text, timestamp, true);
  messageInput.value = "";
});

// ---------------------------------------------------------------------
// File sharing
// ---------------------------------------------------------------------
attachBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  await uploadFile(file);
  fileInput.value = "";
});

async function uploadFile(file) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("room", room);
  formData.append("username", username);

  uploadProgress.classList.remove("hidden");
  uploadProgressBar.style.width = "0%";
  uploadProgressLabel.textContent = `Uploading ${file.name}…`;

  try {
    const data = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/upload");
      xhr.upload.onprogress = (evt) => {
        if (evt.lengthComputable) {
          const pct = Math.round((evt.loaded / evt.total) * 100);
          uploadProgressBar.style.width = pct + "%";
        }
      };
      xhr.onload = () => {
        if (xhr.status === 200) resolve(JSON.parse(xhr.responseText));
        else reject(new Error("Upload failed: " + xhr.status));
      };
      xhr.onerror = () => reject(new Error("Upload failed"));
      xhr.send(formData);
    });

    socket.emit("file_shared", data);
    addFileMessage(username, data.filename, data.url, data.size, data.timestamp, true);
  } catch (err) {
    alert("File upload failed: " + err.message);
  } finally {
    uploadProgress.classList.add("hidden");
  }
}

// ---------------------------------------------------------------------
// Voice / Video calling (WebRTC mesh)
// ---------------------------------------------------------------------
voiceCallBtn.addEventListener("click", () => startCall("audio"));
videoCallBtn.addEventListener("click", () => startCall("video"));
hangupBtn.addEventListener("click", hangUp);
toggleMicBtn.addEventListener("click", toggleMic);
toggleCamBtn.addEventListener("click", toggleCam);

async function startCall(callType) {
  if (inCall) return;
  currentCallType = callType;
  try {
    await startLocalMedia(callType);
  } catch (err) {
    alert("Could not access microphone/camera: " + err.message);
    return;
  }
  inCall = true;
  showCallStage();
  socket.emit("join_call", { room, call_type: callType });
}

async function startLocalMedia(callType) {
  const constraints = callType === "video"
    ? { audio: true, video: { width: 320, height: 240 } }
    : { audio: true, video: false };
  localStream = await navigator.mediaDevices.getUserMedia(constraints);
  addOrUpdateTile("local", "You", localStream, callType === "video");
}

function showCallStage() {
  callStage.classList.remove("hidden");
  voiceCallBtn.classList.add("hidden");
  videoCallBtn.classList.add("hidden");
  hangupBtn.classList.remove("hidden");
}

function hideCallStage() {
  callStage.classList.add("hidden");
  videoGrid.innerHTML = "";
  voiceCallBtn.classList.remove("hidden");
  videoCallBtn.classList.remove("hidden");
  hangupBtn.classList.add("hidden");
}

async function createPeerConnection(peerSid, isInitiator) {
  if (peerConnections[peerSid]) return peerConnections[peerSid];

  const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS });
  peerConnections[peerSid] = pc;

  if (localStream) {
    localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));
  }

  pc.onicecandidate = (event) => {
    if (event.candidate) {
      socket.emit("webrtc_ice_candidate", { target: peerSid, candidate: event.candidate });
    }
  };

  pc.ontrack = (event) => {
    remoteStreams[peerSid] = event.streams[0];
    addOrUpdateTile(peerSid, "Peer " + peerSid.slice(0, 5), event.streams[0], currentCallType === "video");
  };

  pc.onconnectionstatechange = () => {
    if (["disconnected", "failed", "closed"].includes(pc.connectionState)) {
      closePeerConnection(peerSid);
    }
  };

  if (isInitiator) {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    socket.emit("webrtc_offer", { target: peerSid, sdp: offer, call_type: currentCallType });
  }

  return pc;
}

function closePeerConnection(sid) {
  const pc = peerConnections[sid];
  if (pc) {
    pc.close();
    delete peerConnections[sid];
  }
  delete remoteStreams[sid];
  const tile = el("tile-" + sid);
  if (tile) tile.remove();
}

function addOrUpdateTile(key, label, stream, showVideo) {
  let tile = el("tile-" + key);
  if (!tile) {
    tile = document.createElement("div");
    tile.id = "tile-" + key;
    tile.className = "video-tile" + (showVideo ? "" : " audio-only");
    if (showVideo) {
      const video = document.createElement("video");
      video.autoplay = true;
      video.playsInline = true;
      if (key === "local") video.muted = true;
      tile.appendChild(video);
    } else {
      const ring = document.createElement("div");
      ring.className = "avatar-ring";
      ring.textContent = label.slice(0, 2).toUpperCase();
      tile.appendChild(ring);
    }
    const tag = document.createElement("div");
    tag.className = "tile-label";
    tag.textContent = label;
    tile.appendChild(tag);
    videoGrid.appendChild(tile);
  }
  const videoEl = tile.querySelector("video");
  if (videoEl) videoEl.srcObject = stream;
}

function hangUp() {
  Object.keys(peerConnections).forEach(closePeerConnection);
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  socket.emit("leave_call", { room });
  inCall = false;
  currentCallType = null;
  hideCallStage();
}

function toggleMic() {
  if (!localStream) return;
  const track = localStream.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  toggleMicBtn.textContent = track.enabled ? "Mute mic" : "Unmute mic";
}

function toggleCam() {
  if (!localStream) return;
  const track = localStream.getVideoTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  toggleCamBtn.textContent = track.enabled ? "Turn camera off" : "Turn camera on";
}

window.addEventListener("beforeunload", () => {
  if (inCall) hangUp();
});
