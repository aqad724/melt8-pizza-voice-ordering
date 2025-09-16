import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
import uvicorn

load_dotenv()

# ================================
# CONFIG
# ================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
FRAME_SIZE = 640  # 20ms at 16kHz

# ================================
# FASTAPI APP
# ================================
app = FastAPI()

@app.get("/")
async def index():
    return HTMLResponse("<h1>Twilio Voice + OpenAI Realtime is running ✅</h1>")

@app.post("/voice")
async def voice():
    """Generate TwiML response for incoming call"""
    response = VoiceResponse()
    with Connect():
        stream = Stream(url="wss://your-server-domain/ws/twilio")
        response.append(stream)
    return HTMLResponse(str(response), status_code=200, headers={"Content-Type": "text/xml"})

# ================================
# STATE TRACKING
# ================================
class CallSession:
    def __init__(self):
        self.ai_speaking = False
        self.drop_audio = False
        self.openai_ws = None
        self.twilio_ws = None

    async def cancel_ai(self):
        """Cancel any current AI response & flush queues"""
        self.drop_audio = True
        self.ai_speaking = False
        if self.openai_ws:
            try:
                await self.openai_ws.send(json.dumps({"type": "response.cancel"}))
            except Exception as e:
                print("⚠️ Cancel failed:", e)

# ================================
# HELPER: Detect energy for barge-in
# ================================
def detect_speech_energy(b64_audio: str, threshold=500):
    """Simple RMS energy check on PCM16"""
    raw_audio = base64.b64decode(b64_audio)
    if not raw_audio:
        return False
    import array
    audio_arr = array.array("h", raw_audio)  # PCM16
    rms = sum(abs(x) for x in audio_arr) / len(audio_arr)
    return rms > threshold

# ================================
# TWILIO WEBSOCKET ENDPOINT
# ================================
@app.websocket("/ws/twilio")
async def twilio_ws_endpoint(ws: WebSocket):
    await ws.accept()
    print("✅ Twilio connected")

    session = CallSession()
    session.twilio_ws = ws

    async with websockets.connect(
        OPENAI_REALTIME_URL,
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        },
    ) as openai_ws:
        print("✅ Connected to OpenAI Realtime API")
        session.openai_ws = openai_ws

        consumer_task = asyncio.create_task(forward_from_openai(session))
        producer_task = asyncio.create_task(forward_from_twilio(session))

        done, pending = await asyncio.wait(
            [consumer_task, producer_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in pending:
            task.cancel()

# ================================
# FORWARD: From Twilio → OpenAI
# ================================
async def forward_from_twilio(session: CallSession):
    ws = session.twilio_ws
    openai_ws = session.openai_ws
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)

            if data["event"] == "media":
                payload = data["media"]["payload"]

                # 🎤 Detect user speech -> barge-in
                if detect_speech_energy(payload) and session.ai_speaking:
                    print("🚨 User barge-in detected — cancelling AI response")
                    await session.cancel_ai()

                # forward audio to OpenAI
                audio_append = {
                    "type": "input_audio_buffer.append",
                    "audio": payload,
                }
                await openai_ws.send(json.dumps(audio_append))

            elif data["event"] == "mark":
                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                await openai_ws.send(json.dumps({"type": "response.create"}))
    except Exception as e:
        print("❌ forward_from_twilio error:", e)

# ================================
# FORWARD: From OpenAI → Twilio
# ================================
async def forward_from_openai(session: CallSession):
    ws = session.twilio_ws
    openai_ws = session.openai_ws
    try:
        async for message in openai_ws:
            response = json.loads(message)

            if response["type"] == "response.audio.start":
                session.ai_speaking = True
                session.drop_audio = False
                print("🔊 AI started speaking")

            elif response["type"] == "response.audio.delta":
                if not session.drop_audio:
                    audio_data = response["delta"]
                    await ws.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "media": {"payload": audio_data},
                            }
                        )
                    )
                else:
                    continue  # 🚫 discard frames

            elif response["type"] == "response.audio.end":
                print("🔇 AI finished speaking")

            elif response["type"] == "response.done":
                session.ai_speaking = False
                session.drop_audio = False
                print("✅ Response cycle complete")
    except Exception as e:
        print("❌ forward_from_openai error:", e)

# ================================
# MAIN
# ================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
