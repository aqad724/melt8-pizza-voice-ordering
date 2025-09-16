import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Say
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# =========================================
# CONFIGURATION
# =========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Replace with your Prompt ID + Version
PROMPT_ID = "pmpt_68bdd42ebbb881948ffca4f752efaec406a110ab981d5f90"
PROMPT_VERSION = "7"

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

# Allow app to start without API key for webhook testing
API_KEYS_CONFIGURED = bool(OPENAI_API_KEY)
if not API_KEYS_CONFIGURED:
    print("⚠️  Warning: OpenAI API key not configured. Voice features will be limited.")


# =========================================
# ROOT ENDPOINT
# =========================================
@app.get("/")
async def index_page():
    return {"status": "Server running", "info": "Twilio + OpenAI Realtime AI Voice"}


# =========================================
# TWILIO VOICE WEBHOOK
# =========================================
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle Twilio webhook and respond with TwiML to connect audio stream."""

    response = VoiceResponse()
    
    if not API_KEYS_CONFIGURED:
        response.say("Webhook is working! However, the AI voice assistant is not fully configured yet. Please add your API keys to enable voice features.")
        return HTMLResponse(content=str(response), media_type="application/xml")
    
    response.say("Please wait while we connect your call to the AI voice assistant.")
    response.pause(length=1)
    response.say("Okay, you can start talking!")

    # Get the host from the request or environment
    host = request.url.hostname
    if not host or host == "127.0.0.1" or host == "localhost":
        # Use environment domain for Replit
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
    print("Twilio connected")
    await websocket.accept()
    
    if not API_KEYS_CONFIGURED:
        print("API keys not configured - closing WebSocket connection")
        await websocket.close()
        return

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview-2024-12-17",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await send_session_update(openai_ws)

        stream_sid = None
        audio_queue = asyncio.Queue(maxsize=50)  # ~1 second buffer
        drop_audio = False

        async def receive_from_twilio():
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data["event"] == "media":
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"]
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        print(f"Stream started: {stream_sid}")
            except Exception as e:
                print(f"Error receiving from Twilio: {e}")

        async def audio_playback():
            """Send audio frames to Twilio at 20ms intervals"""
            nonlocal stream_sid, drop_audio
            while True:
                try:
                    # Wait for audio frame with timeout
                    audio_frame = await asyncio.wait_for(audio_queue.get(), timeout=0.02)
                    
                    if not drop_audio and stream_sid:
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_frame}
                        }
                        await websocket.send_json(audio_delta)
                    
                    audio_queue.task_done()
                    
                except asyncio.TimeoutError:
                    # No audio available, continue the 20ms loop
                    continue
                except Exception as e:
                    print(f"Error in audio playback: {e}")
                    break

        async def send_to_twilio():
            nonlocal stream_sid, drop_audio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response["type"] in LOG_EVENT_TYPES:
                        print(f"Event: {response['type']}", response)

                    # Flush audio queue on interruption
                    if response["type"] == "input_audio_buffer.speech_started":
                        print("🎤 User started speaking - flushing audio queue")
                        drop_audio = True
                        # Clear the queue
                        while not audio_queue.empty():
                            try:
                                audio_queue.get_nowait()
                                audio_queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                    
                    # Reset drop flag when user finishes speaking and AI can respond
                    elif response["type"] == "input_audio_buffer.committed":
                        print("🔊 User finished speaking - enabling AI audio")
                        drop_audio = False
                    
                    # Handle cancelled responses
                    elif response["type"] == "response.done":
                        if response.get("response", {}).get("status") == "cancelled":
                            print("❌ Response cancelled - flushing remaining audio")
                            # Clear the queue but keep drop_audio as is
                            while not audio_queue.empty():
                                try:
                                    audio_queue.get_nowait()
                                    audio_queue.task_done()
                                except asyncio.QueueEmpty:
                                    break
                        else:
                            print("✅ Response completed")

                    # Process audio deltas
                    if response["type"] == "response.audio.delta" and response.get("delta") and not drop_audio:
                        try:
                            # Decode audio data
                            audio_data = base64.b64decode(response["delta"])
                            
                            # Split into 20ms frames (160 bytes for G.711 µ-law at 8kHz)
                            frame_size = 160
                            for i in range(0, len(audio_data), frame_size):
                                frame = audio_data[i:i + frame_size]
                                if len(frame) == frame_size:  # Only send complete frames
                                    frame_b64 = base64.b64encode(frame).decode("utf-8")
                                    
                                    # Add to queue (non-blocking, drop if full)
                                    try:
                                        audio_queue.put_nowait(frame_b64)
                                    except asyncio.QueueFull:
                                        print("⚠️ Audio queue full, dropping frame")
                                        
                        except Exception as e:
                            print(f"Error processing audio delta: {e}")
            except Exception as e:
                print(f"Error from OpenAI: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio(), audio_playback())


# =========================================
# SESSION UPDATE WITH PROMPT ID + VERSION
# =========================================
async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
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