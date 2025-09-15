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
PROMPT_VERSION = "11"

VOICE = "alloy"

LOG_EVENT_TYPES = [
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "conversation.interrupted",
    "response.canceled"
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
        "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await send_session_update(openai_ws)

        stream_sid = None
        
        # Interruption handling state
        current_item_id = None
        is_ai_speaking = False
        audio_samples_played = 0
        suppress_playback = False

        async def interrupt_ai_response():
            """Send interruption commands to OpenAI"""
            nonlocal is_ai_speaking, current_item_id, audio_samples_played, suppress_playback
            
            try:
                # Suppress further audio playback immediately
                suppress_playback = True
                
                # Cancel the ongoing response
                cancel_event = {"type": "response.cancel"}
                await openai_ws.send(json.dumps(cancel_event))
                print("🛑 Sent response.cancel")
                
                # Only send truncate if we have a valid item ID
                if current_item_id and is_ai_speaking:
                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": current_item_id,
                        "content_index": 0,
                        "audio_end_ms": int(audio_samples_played * 1000 / 8000)  # Convert samples to milliseconds (8kHz for g711_ulaw)
                    }
                    await openai_ws.send(json.dumps(truncate_event))
                    print(f"✂️ Truncated item {current_item_id} at {audio_samples_played} samples")
                else:
                    print("⚠️ No truncation needed - sent cancel only")
                
                # Reset state
                is_ai_speaking = False
                current_item_id = None
                audio_samples_played = 0
                
            except Exception as e:
                print(f"Error during interruption: {e}")

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

        async def send_to_twilio():
            nonlocal stream_sid, current_item_id, is_ai_speaking, audio_samples_played, suppress_playback
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response["type"] in LOG_EVENT_TYPES:
                        print(f"Event: {response['type']}", response)
                    
                    # Handle speech detection - always interrupt on user speech
                    if response["type"] == "input_audio_buffer.speech_started":
                        print("🎤 User speech detected - interrupting AI")
                        await interrupt_ai_response()
                    
                    # Track AI response creation to get item ID
                    elif response["type"] == "response.created":
                        # Look for audio output items but don't reset suppression yet
                        for output in response.get("response", {}).get("output", []):
                            if output.get("type") == "output_audio":
                                new_item_id = output.get("id")
                                print(f"🎵 Audio response created (item: {new_item_id})")
                                break
                    
                    # Also handle response.output_item.added events
                    elif response["type"] == "response.output_item.added":
                        item = response.get("item", {})
                        if item.get("type") == "output_audio":
                            # Reset suppression only when new audio item is actually added
                            current_item_id = item.get("id")
                            audio_samples_played = 0
                            suppress_playback = False
                            print(f"🎵 Audio output item added (item: {current_item_id})")
                    
                    # Track when AI starts sending audio
                    elif response["type"] == "response.audio.delta" and response.get("delta"):
                        # Check if playback is suppressed (after interruption)
                        if suppress_playback:
                            print("🔇 Suppressing audio playback after interruption")
                            continue
                        
                        # Drop all deltas when no current item (prevents late frame leaks)
                        if not current_item_id:
                            print("🚫 Dropping audio delta (no current item)")
                            continue
                        
                        # Validate item_id matches current item before forwarding
                        delta_item_id = response.get("item_id")
                        if delta_item_id and delta_item_id != current_item_id:
                            print(f"🚫 Ignoring audio delta from different item: {delta_item_id} != {current_item_id}")
                            continue
                        
                        if not is_ai_speaking:
                            is_ai_speaking = True
                            print(f"🤖 AI started speaking")
                        
                        # Track audio samples and forward to Twilio
                        try:
                            decoded_audio = base64.b64decode(response["delta"])
                            audio_samples_played += len(decoded_audio)
                            
                            audio_payload = base64.b64encode(decoded_audio).decode("utf-8")
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload}
                            }
                            await websocket.send_json(audio_delta)
                        except Exception as e:
                            print(f"Error sending audio to Twilio: {e}")
                    
                    # Reset state when AI finishes or gets interrupted
                    elif response["type"] == "response.audio.done":
                        is_ai_speaking = False
                        current_item_id = None
                        audio_samples_played = 0
                        # Keep suppress_playback until new item is added
                        print(f"🤖 AI finished speaking")
                    
                    elif response["type"] == "response.canceled":
                        is_ai_speaking = False
                        current_item_id = None
                        audio_samples_played = 0
                        # Keep suppress_playback until new item is added
                        print("🚫 Response canceled")
                    
                    elif response["type"] == "conversation.interrupted":
                        is_ai_speaking = False
                        # Don't reset current_item_id or suppress_playback here to prevent early reset
                        print("🚫 OpenAI detected conversation interruption")
                        
            except Exception as e:
                print(f"Error from OpenAI: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


# =========================================
# SESSION UPDATE WITH PROMPT ID + VERSION
# =========================================
async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
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