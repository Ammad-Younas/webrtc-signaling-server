# main.py
import asyncio
import json
import logging
import uuid
from typing import Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="UnderByte Relay Server")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("relay")

# Very permissive CORS for dev; tighten in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory rooms structure:
# rooms = { room_id: { user_id: WebSocket, ... }, ... }
rooms: Dict[str, Dict[str, WebSocket]] = {}
rooms_lock = asyncio.Lock()

MAX_BINARY_SIZE = 5 * 1024 * 1024  # 5 MB per binary frame (tune as needed)
MAX_PARTICIPANTS_PER_ROOM = 10      # safety limit

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/create_room")
async def create_room():
    """Create a new ephemeral room id (REST)."""
    room_id = uuid.uuid4().hex[:12]
    async with rooms_lock:
        rooms.setdefault(room_id, {})
    return JSONResponse({"room_id": room_id})

@app.get("/rooms/{room_id}")
async def get_room_info(room_id: str):
    """Return list of participants (debug)."""
    async with rooms_lock:
        if room_id not in rooms:
            raise HTTPException(status_code=404, detail="Room not found")
        return {"room_id": room_id, "participants": list(rooms[room_id].keys())}

@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, user_id: str):
    """
    WebSocket endpoint for a user in a room.

    Protocol (text frames -> JSON):
      {
        "type": "message" | "file_init" | "file_done" | "control",
        "to": "<userId>" | "all",            # target
        "payload": { ... }                   # message body
      }

    Binary frames:
      - Treated as opaque file chunk bytes and forwarded to target(s).
      - Sender SHOULD send a preceding `file_init` JSON announcing filename/size/mime,
        and a trailing `file_done` JSON when finished.
    """
    await websocket.accept()
    logger.info(f"WS connect request: room={room_id} user={user_id}")

    async with rooms_lock:
        # create room if not exists
        if room_id not in rooms:
            rooms[room_id] = {}
        # safety: participant limit
        if len(rooms[room_id]) >= MAX_PARTICIPANTS_PER_ROOM:
            await websocket.close(code=1008, reason="Room full")
            return
        rooms[room_id][user_id] = websocket
        logger.info(f"User {user_id} joined room {room_id} (participants={len(rooms[room_id])})")

    try:
        while True:
            msg = await websocket.receive()

            # Text (JSON) frame
            if "text" in msg and msg["text"] is not None:
                raw = msg["text"]
                try:
                    data = json.loads(raw)
                except Exception:
                    # invalid JSON: ignore or optionally send error
                    logger.warning(f"Invalid JSON from {user_id} in {room_id}: {raw}")
                    continue

                # attach sender
                data.setdefault("from", user_id)
                mtype = data.get("type", "message")
                to = data.get("to", "all")

                logger.debug(f"Text from {user_id} in {room_id}: type={mtype} to={to}")

                await forward_text(room_id, user_id, to, data)

            # Binary frame
            elif "bytes" in msg and msg["bytes"] is not None:
                bdata = msg["bytes"]
                # Safety: limit binary frame size
                if len(bdata) > MAX_BINARY_SIZE:
                    logger.warning(f"Binary frame too large from {user_id}: {len(bdata)} bytes")
                    # Optionally notify sender before dropping
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "payload": {"message": "binary frame too large"}
                        }))
                    except Exception:
                        pass
                    continue

                # Forward binary to peers in same room
                await forward_binary(room_id, user_id, bdata)

            else:
                # ping/pong or close events handled by WebSocket implementation
                await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        logger.info(f"WS disconnect: {user_id} from room {room_id}")
    except Exception as e:
        logger.exception(f"Exception in websocket loop for {user_id}@{room_id}: {e}")
    finally:
        # cleanup
        async with rooms_lock:
            if room_id in rooms and user_id in rooms[room_id]:
                try:
                    del rooms[room_id][user_id]
                except Exception:
                    pass
            # if room empty remove it
            if room_id in rooms and not rooms[room_id]:
                logger.info(f"Removing empty room {room_id}")
                del rooms[room_id]
        try:
            await websocket.close()
        except Exception:
            pass

async def forward_text(room_id: str, from_user: str, to: str, data: Dict[str, Any]):
    """Forward JSON text message to the correct recipients."""
    async with rooms_lock:
        if room_id not in rooms:
            logger.debug(f"Room {room_id} not found while forwarding text")
            return
        recipients = []

        if to == "all":
            recipients = [ws for uid, ws in rooms[room_id].items() if uid != from_user]
        else:
            ws = rooms[room_id].get(to)
            if ws:
                recipients = [ws]
            else:
                # optional: inform sender target offline
                logger.debug(f"Target {to} not in room {room_id}")
                sender_ws = rooms[room_id].get(from_user)
                if sender_ws:
                    try:
                        await sender_ws.send_text(json.dumps({
                            "type": "error",
                            "payload": {"message": f"target {to} not found"}
                        }))
                    except Exception:
                        pass
                return

    # send without holding lock
    for ws in recipients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception as e:
            logger.warning(f"Error forwarding text to a peer: {e}")

async def forward_binary(room_id: str, from_user: str, binary_data: bytes):
    """Forward binary frame to all peers in the room except the sender."""
    async with rooms_lock:
        if room_id not in rooms:
            return
        recipients = [ws for uid, ws in rooms[room_id].items() if uid != from_user]

    for ws in recipients:
        try:
            await ws.send_bytes(binary_data)
        except Exception as e:
            logger.warning(f"Error forwarding binary to a peer: {e}")
