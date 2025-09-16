import os
import json
import base64
import asyncio
import websockets
import time
import uuid
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# =========================================
# CONFIGURATION
# =========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PROMPT_ID = "pmpt_68bdd42ebbb881948ffca4f752efaec406a110ab981d5f90"
PROMPT_VERSION = "15"
VOICE = "alloy"

LOG_EVENT_TYPES = [
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created"
]

app = FastAPI()
active_connections = 0

API_KEYS_CONFIGURED = bool(OPENAI_API_KEY)
if not API_KEYS_CONFIGURED:
    print("⚠️  Warning: OpenAI API key not configured. Voice features will be limited.")

# =========================================
# ROOT ENDPOINT
# =========================================
@app.get("/")
async def index_page():
    return {"status": "Server running", "info": "Twilio + OpenAI Realtime AI Voice", "active_connections": active_connections}

@app.get("/status")
async def connection_status():
    return {
        "status": "healthy",
        "active_connections": active_connections,
        "concurrent_support": "enabled",
        "api_configured": API_KEYS_CONFIGURED
    }

# =========================================
# TWILIO VOICE WEBHOOK
# =========================================
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()

    if not API_KEYS_CONFIGURED:
        response.say("Webhook is working! However, the AI voice assistant is not fully configured yet. Please add your API keys to enable voice features.")
        return HTMLResponse(content=str(response), media_type="application/xml")

    response.say("Please wait while we connect your call to the AI voice assistant.")
    response.pause(length=1)
    response.say("Okay, you can start talking!")

    host = request.url.hostname
    if not host or host == "127.0.0.1" or host == "localhost":
        host = os.getenv("REPLIT_DEV_DOMAIN", request.url.hostname)

    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# =========================================
# MEDIA STREAM HANDLER
# =========================================
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    global active_connections
    connection_id = f"conn_{str(uuid.uuid4())[:8]}"
    await websocket.accept()

    if not API_KEYS_CONFIGURED:
        print(f"❌ [{connection_id}] API keys not configured - closing WebSocket connection")
        await websocket.close()
        return

    try:
        async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview-2024-12-17",
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        ) as openai_ws:
            try:
                active_connections += 1
                print(f"🔗 [{connection_id}] Connected successfully (Active: {active_connections})")

                await send_session_update(openai_ws)
                stream_sid = None
                drop_audio = False
                ai_speaking = False

                def _ulaw_to_linear(b):
                    out = []
                    for u in b:
                        u = (~u) & 0xFF
                        sign = u & 0x80
                        exp = (u >> 4) & 0x07
                        mant = u & 0x0F
                        sample = ((mant | 0x10) << (exp + 3)) - 132
                        if sign:
                            sample = -sample
                        out.append(sample)
                    return out

                def detect_speech_energy(audio_b64):
                    """Fast energy-based VAD aligned with prefix_padding_ms=0"""
                    try:
                        data = base64.b64decode(audio_b64, validate=False)
                        if not data:
                            return False

                        samples = _ulaw_to_linear(data)
                        abs_vals = [abs(x) for x in samples]

                        peak = max(abs_vals)
                        mean_abs = sum(abs_vals) / len(abs_vals)

                        # Fire instantly on strong peak or steady energy
                        if peak > 2000 or mean_abs > 300:
                            return True
                        return False
                    except Exception:
                        return False

                async def receive_from_twilio():
                    nonlocal stream_sid, drop_audio, ai_speaking
                    try:
                        async for message in websocket.iter_text():
                            data = json.loads(message)
                            if data["event"] == "media":
                                if ai_speaking and detect_speech_energy(data["media"]["payload"]):
                                    print(f"🚀 [{connection_id}] INSTANT interruption detected locally!")
                                    drop_audio = True
                                    ai_speaking = False
                                    try:
                                        await openai_ws.send(json.dumps({"type": "response.cancel"}))
                                    except:
                                        pass

                                audio_append = {
                                    "type": "input_audio_buffer.append",
                                    "audio": data["media"]["payload"]
                                }
                                await openai_ws.send(json.dumps(audio_append))

                            elif data["event"] == "start":
                                stream_sid = data["start"]["streamSid"]
                                print(f"📞 [{connection_id}] Stream started: {stream_sid}")
                    except Exception as e:
                        print(f"❌ [{connection_id}] Error receiving from Twilio: {e}")

                async def send_to_twilio():
                    nonlocal stream_sid, drop_audio, ai_speaking
                    try:
                        async for openai_message in openai_ws:
                            response = json.loads(openai_message)

                            if response["type"] in LOG_EVENT_TYPES:
                                print(f"Event: {response['type']}", response)

                            if response["type"] == "response.audio.start":
                                ai_speaking = True
                                print(f"🤖 [{connection_id}] AI started speaking")

                            elif response["type"] == "input_audio_buffer.speech_started":
                                print(f"🎤 [{connection_id}] User started speaking - server VAD fallback")
                                drop_audio = True
                                ai_speaking = False

                            elif response["type"] == "input_audio_buffer.committed":
                                print(f"🔊 [{connection_id}] User finished speaking - enabling AI audio")
                                drop_audio = False

                            elif response["type"] == "response.done":
                                ai_speaking = False
                                if response.get("response", {}).get("status") == "cancelled":
                                    print(f"❌ [{connection_id}] Response cancelled")
                                else:
                                    print(f"✅ [{connection_id}] Response completed")

                            if response["type"] == "response.audio.delta" and response.get("delta") and not drop_audio:
                                try:
                                    if not ai_speaking:
                                        ai_speaking = True
                                        print(f"🤖 [{connection_id}] AI started speaking (delta)")

                                    audio_data = base64.b64decode(response["delta"])

                                    frame_size = 160
                                    frame_count = 0
                                    for i in range(0, len(audio_data), frame_size):
                                        if drop_audio:
                                            break

                                        frame = audio_data[i:i + frame_size]
                                        if len(frame) == frame_size and stream_sid:
                                            frame_b64 = base64.b64encode(frame).decode("utf-8")

                                            audio_delta = {
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": frame_b64}
                                            }
                                            await websocket.send_json(audio_delta)

                                            frame_count += 1
                                            if frame_count % 2 == 0:
                                                await asyncio.sleep(0)

                                except Exception as e:
                                    print(f"❌ [{connection_id}] Error processing audio delta: {e}")
                    except Exception as e:
                        print(f"❌ [{connection_id}] Error from OpenAI: {e}")

                await asyncio.gather(receive_from_twilio(), send_to_twilio())
            except Exception as e:
                print(f"❌ [{connection_id}] Connection error: {e}")
            finally:
                active_connections -= 1
                print(f"🔌 [{connection_id}] Connection closed (Active: {active_connections})")
    except Exception as e:
        print(f"❌ [{connection_id}] Failed to connect to OpenAI: {e}")
        await websocket.close(code=1011, reason="Upstream connect failed")

# =========================================
# SESSION UPDATE WITH PROMPT ID + VERSION
# =========================================
async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.50,
                "prefix_padding_ms": 0,
                "silence_duration_ms": 500
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "speed": 0.9,
            "prompt": {
                "id": PROMPT_ID,
                "version": PROMPT_VERSION
            }
        }
    }
    print("Sending session update:", json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

# =========================================
# MAIN
# =========================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
