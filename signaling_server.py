import asyncio
import websockets
import json
import secrets

rooms = {}  # { room_id: [peer1, peer2] }

async def handler(ws):
    try:
        async for message in ws:
            data = json.loads(message)
            msg_type = data.get("type")
            room = data.get("room")

            # Create random room
            if msg_type == "create":
                room_id = secrets.token_hex(3).upper()  # e.g., A1B2C3
                rooms[room_id] = [ws]
                await ws.send(json.dumps({"type": "room_created", "room": room_id}))
                print(f"🟢 Room created: {room_id}")
                continue

            # Join existing room
            if msg_type == "join":
                if room in rooms:
                    # 1. Notify the new peer that they successfully joined
                    await ws.send(json.dumps({"type": "room_joined", "room": room}))
                    print(f"🟢 Peer joined room: {room}")

                    # 2. Notify ALL EXISTING peers (not including the new one) that a peer joined
                    for peer in rooms[room]:
                        await peer.send(json.dumps({"type": "peer_joined", "room": room}))
                    
                    # 3. NOW add the new peer to the room (after notifications)
                    rooms[room].append(ws)
                else:
                    await ws.send(json.dumps({"type": "error", "message": "Room not found"}))
                continue

            # Relay signaling or chat messages
            if room in rooms:
                # Log what we are about to relay
                print(f"➡️  Relaying '{msg_type}' in room {room}") 
                
                for peer in rooms[room]:
                    if peer != ws:
                        await peer.send(message)
            else:
                # Log if we get a message for a room that doesn't exist
                print(f"⚠️  Got message for unknown room: {room}")

    except websockets.exceptions.ConnectionClosed:
        print("🔴 Connection closed")

    finally:
        # Cleanup disconnected users
        for room_id, peers in list(rooms.items()):
            if ws in peers:
                peers.remove(ws)
                # Notify remaining peers that someone left
                for peer in peers:
                    await peer.send(json.dumps({"type": "peer_left", "room": room_id}))
                
                if not peers:
                    del rooms[room_id]
                    print(f"🧹 Room {room_id} removed (no peers left)")
                else:
                    print(f"👋 Peer left room {room_id} ({len(peers)} remaining)")
                break


async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        print(f"✅ Signaling server running on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

asyncio.run(main())