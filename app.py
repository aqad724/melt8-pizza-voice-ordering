import os
import json
import base64
import asyncio
import websockets
import time
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, WebSocket, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from twilio.twiml.voice_response import VoiceResponse, Connect, Say
from dotenv import load_dotenv
import uvicorn
import secrets
load_dotenv()
# =========================================
# CONFIGURATION
# =========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Replace with your Prompt ID + Version
PROMPT_ID = "pmpt_68bdd42ebbb881948ffca4f752efaec406a110ab981d5f90"
PROMPT_VERSION = ""

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL")

# Deployment configuration
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "your-deployment.replit.dev")
PORT = int(os.getenv("PORT", "5000"))

# Chef Dashboard Security Configuration
CHEF_USERNAME = os.getenv("CHEF_USERNAME", "chef")
CHEF_PASSWORD = os.getenv("CHEF_PASSWORD", "pizza123")
security = HTTPBasic()

print(f"🔐 Chef dashboard authentication configured for user: {CHEF_USERNAME}")

# Function definition for OpenAI
SAVE_ORDER_FUNCTION = {
    "name": "save_order",
    "description": "Save a completed pizza order to the backend file storage (orders.json).",
    "parameters": {
        "type": "object",
        "properties": {
            "flavour": {
                "type": "string",
                "description": "Pizza flavour chosen by the customer (e.g., Pepperoni, Veggie)."
            },
            "size": {
                "type": "string",
                "description": "Pizza size.",
                "enum": ["Small", "Medium", "Large"]
            },
            "drink": {
                "type": "string",
                "description": "Optional drink choice. If none, send an empty string or omit."
            },
            "address": {
                "type": "string",
                "description": "Delivery address (street, area, city)."
            },
            "customer_name": {
                "type": "string",
                "description": "Optional customer name."
            }
        },
        "additionalProperties": False,
        "required": ["flavour", "size", "address"]
    }
}
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

# Connection tracking for concurrent calls
active_connections = 0
phone_registry = {}  # Store phone numbers by call session

# Allow app to start without API key for webhook testing
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
# DATABASE FUNCTIONS
# =========================================
def get_db_connection():
    """Get database connection"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

async def save_order_to_db(flavour, size, drink, address, customer_name, customer_phone=None):
    """Save order to database"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO orders (flavour, size, drink, address, customer_name, customer_phone)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, order_time
        """, (flavour, size, drink or '', address, customer_name or '', customer_phone))
        
        result = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        
        if result:
            order_id = dict(result).get('id', 'Unknown')
            print(f"✅ Order saved: ID {order_id} - {size} {flavour} for {customer_name or 'Unknown'}")
            return dict(result)
        else:
            print(f"❌ Error: No result returned when saving order")
            return None
    except Exception as e:
        print(f"❌ Error saving order: {e}")
        return None

# =========================================
# AUTHENTICATION
# =========================================
def authenticate_chef(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Authenticate chef dashboard access using HTTP Basic Authentication.
    Returns True if credentials are valid, otherwise raises HTTPException.
    """
    # Use secrets.compare_digest for constant-time string comparison to prevent timing attacks
    is_correct_username = secrets.compare_digest(credentials.username, CHEF_USERNAME)
    is_correct_password = secrets.compare_digest(credentials.password, CHEF_PASSWORD)
    
    if not (is_correct_username and is_correct_password):
        print(f"❌ Unauthorized chef dashboard access attempt: {credentials.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials for chef dashboard access",
            headers={"WWW-Authenticate": "Basic"}
        )
    
    print(f"✅ Chef dashboard access granted to: {credentials.username}")
    return True

# =========================================
# FUNCTION CALL HANDLER
# =========================================
async def handle_function_call(connection_id, customer_phone, call_id, function_name, arguments, openai_ws):
    """
    Enhanced function call handler with proper error handling and response formatting
    """
    print(f"🔧 [{connection_id}] Executing function: {function_name} with args: {arguments}")
    
    try:
        if function_name == "save_order":
            # Use the customer phone number captured from Twilio
            print(f"💾 [{connection_id}] Saving order for customer: {customer_phone}")
            
            # Validate required arguments
            required_fields = ['flavour', 'size', 'address']
            missing_fields = [field for field in required_fields if not arguments.get(field)]
            
            if missing_fields:
                print(f"❌ [{connection_id}] Missing required fields: {missing_fields}")
                function_result = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "success": False,
                            "message": f"Missing required information: {', '.join(missing_fields)}",
                            "missing_fields": missing_fields
                        })
                    }
                }
            else:
                # Save order to database
                result = await save_order_to_db(
                    flavour=arguments.get("flavour"),
                    size=arguments.get("size"),
                    drink=arguments.get("drink", ""),
                    address=arguments.get("address"),
                    customer_name=arguments.get("customer_name", ""),
                    customer_phone=customer_phone
                )
                
                # Create function result based on database operation
                if result:
                    function_result = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps({
                                "success": True,
                                "order_id": result.get("id"),
                                "order_time": str(result.get("order_time", "")),
                                "message": f"Order #{result.get('id')} saved successfully! Your {arguments.get('size')} {arguments.get('flavour')} pizza will be prepared shortly."
                            })
                        }
                    }
                    print(f"✅ [{connection_id}] Function call successful - Order ID: {result.get('id')}")
                else:
                    function_result = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps({
                                "success": False,
                                "message": "Sorry, there was a technical issue saving your order. Please try again.",
                                "error_type": "database_error"
                            })
                        }
                    }
                    print(f"❌ [{connection_id}] Function call failed - Database error")
        else:
            # Handle unknown function calls
            print(f"⚠️ [{connection_id}] Unknown function: {function_name}")
            function_result = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "success": False,
                        "message": f"Unknown function: {function_name}",
                        "error_type": "unknown_function"
                    })
                }
            }
        
        # Send function result back to OpenAI
        print(f"📤 [{connection_id}] Sending function result to OpenAI")
        await openai_ws.send(json.dumps(function_result))
        
        # Request AI to continue/respond
        await openai_ws.send(json.dumps({"type": "response.create"}))
        print(f"✅ [{connection_id}] Function call handling completed")
        
    except Exception as e:
        print(f"❌ [{connection_id}] Critical error in function call handler: {e}")
        import traceback
        traceback.print_exc()
        
        # Send error response to OpenAI
        try:
            error_result = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "success": False,
                        "message": "Internal error occurred while processing your request.",
                        "error_type": "internal_error"
                    })
                }
            }
            await openai_ws.send(json.dumps(error_result))
            await openai_ws.send(json.dumps({"type": "response.create"}))
        except Exception as send_error:
            print(f"❌ [{connection_id}] Failed to send error response: {send_error}")

# =========================================
# CHEF DASHBOARD ROUTES  
# =========================================
@app.get("/chef-dashboard")
async def chef_dashboard(authenticated: bool = Depends(authenticate_chef)):
    """Serve chef dashboard HTML"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Melt 8 - Chef Dashboard</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: #2c3e50; color: white; padding: 20px; border-radius: 8px; margin-bottom: 20px; }
            .order-card { background: white; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .order-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
            .order-id { font-weight: bold; color: #e74c3c; }
            .order-time { color: #7f8c8d; font-size: 0.9em; }
            .status-new { background: #e74c3c; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
            .status-preparing { background: #f39c12; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
            .status-ready { background: #27ae60; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }
            .order-details { margin: 10px 0; }
            .customer-info { background: #ecf0f1; padding: 10px; border-radius: 4px; margin: 10px 0; }
            .btn { padding: 8px 12px; margin: 2px; border: none; border-radius: 4px; cursor: pointer; }
            .btn-warning { background: #f39c12; color: white; }
            .btn-success { background: #27ae60; color: white; }
            .btn-info { background: #3498db; color: white; }
            .refresh-btn { position: fixed; top: 20px; right: 20px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🍕 Melt 8 - Chef Dashboard</h1>
            <p>Real-time pizza orders from voice calls</p>
        </div>
        
        <button class="btn btn-info refresh-btn" onclick="location.reload()">🔄 Refresh</button>
        
        <div id="orders-container">
            <p>Loading orders...</p>
        </div>
        
        <script>
            async function loadOrders() {
                try {
                    const response = await fetch('/api/orders');
                    const orders = await response.json();
                    
                    const container = document.getElementById('orders-container');
                    if (orders.length === 0) {
                        container.innerHTML = '<p>No orders yet. Waiting for customers to call...</p>';
                        return;
                    }
                    
                    container.innerHTML = orders.map(order => `
                        <div class="order-card">
                            <div class="order-header">
                                <span class="order-id">Order #${order.id}</span>
                                <span class="status-${order.status}">${order.status.toUpperCase()}</span>
                                <span class="order-time">${new Date(order.order_time).toLocaleString()}</span>
                            </div>
                            <div class="order-details">
                                <strong>🍕 ${order.size} ${order.flavour} Pizza</strong>
                                ${order.drink ? `<br>🥤 ${order.drink}` : ''}
                            </div>
                            <div class="customer-info">
                                <strong>Customer:</strong> ${order.customer_name || 'Unknown'}<br>
                                <strong>Phone:</strong> ${order.customer_phone || 'N/A'}<br>
                                <strong>Address:</strong> ${order.address}
                            </div>
                            <div style="margin-top: 10px;">
                                ${order.status === 'new' ? `<button class="btn btn-warning" onclick="updateStatus(${order.id}, 'preparing')">Start Preparing</button>` : ''}
                                ${order.status === 'preparing' ? `<button class="btn btn-success" onclick="updateStatus(${order.id}, 'ready')">Mark Ready</button>` : ''}
                                ${order.status === 'ready' ? `<button class="btn btn-info" onclick="updateStatus(${order.id}, 'delivered')">Mark Delivered</button>` : ''}
                            </div>
                        </div>
                    `).join('');
                } catch (error) {
                    document.getElementById('orders-container').innerHTML = '<p>Error loading orders. Please refresh.</p>';
                }
            }
            
            async function updateStatus(orderId, status) {
                try {
                    await fetch(`/api/orders/${orderId}/status`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status })
                    });
                    loadOrders(); // Reload orders
                } catch (error) {
                    alert('Error updating order status');
                }
            }
            
            // Load orders on page load
            loadOrders();
            
            // Auto-refresh every 10 seconds
            setInterval(loadOrders, 10000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/orders")
async def get_orders(authenticated: bool = Depends(authenticate_chef)):
    """Get all orders for chef dashboard"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM orders 
            ORDER BY order_time DESC
        """)
        
        orders = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return [dict(order) for order in orders]
    except Exception as e:
        print(f"❌ Error fetching orders: {e}")
        return []

@app.put("/api/orders/{order_id}/status")
async def update_order_status(order_id: int, status_data: dict, authenticated: bool = Depends(authenticate_chef)):
    """Update order status"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE orders SET status = %s WHERE id = %s
        """, (status_data['status'], order_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return {"success": True}
    except Exception as e:
        print(f"❌ Error updating order status: {e}")
        return {"success": False, "error": str(e)}
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

    # Extract customer phone number from Twilio webhook data
    form_data = None
    call_sid = "unknown"
    
    try:
        if request.method == "POST":
            form_data = await request.form()
            caller_phone_raw = form_data.get("From", "Unknown")
            call_sid = form_data.get("CallSid", "unknown")
            # Ensure we have a string, not UploadFile
            caller_phone = str(caller_phone_raw) if caller_phone_raw else "Unknown"
        else:
            # For GET requests (testing), use query parameter
            caller_phone = request.query_params.get("From", "Unknown")
            call_sid = request.query_params.get("CallSid", "unknown")
        
        # Clean up phone number format (handle WhatsApp format and regular formats)
        if isinstance(caller_phone, str) and caller_phone != "Unknown":
            # Handle WhatsApp format: whatsapp:+923270737372
            if caller_phone.startswith("whatsapp:"):
                caller_phone = caller_phone[9:]  # Remove "whatsapp:" prefix
                print(f"🟢 WhatsApp call detected, extracted number: {caller_phone}")
            
            # Now handle regular phone number prefixes
            if caller_phone.startswith("+1") and len(caller_phone) == 12:
                caller_phone = caller_phone[2:]  # Remove +1 prefix
            elif caller_phone.startswith("+"):
                caller_phone = caller_phone[1:]  # Remove + prefix
            
        print(f"📞 Incoming call from: {caller_phone}")
        
        # Store phone number for this call session
        phone_registry[call_sid] = caller_phone
        print(f"📝 Stored phone {caller_phone} for call session {call_sid}")
        
    except Exception as e:
        print(f"❌ Error extracting phone number: {e}")
        caller_phone = "Unknown"

    response.say("Please wait while we connect your call to the AI voice assistant.")
    response.pause(length=1)
    response.say("Okay, you can start talking!")
    
    # Use fixed deployment URL for WebSocket (not workflow preview URL)
    websocket_url = f"wss://{PUBLIC_BASE_URL}/media-stream?call_sid={call_sid}"
    print(f"🔗 Generated WebSocket URL: {websocket_url}")
    
    connect = Connect()
    connect.stream(url=websocket_url)
    response.append(connect)
    
    # Debug: Show the final TwiML response
    twiml_response = str(response)
    print(f"📋 TwiML Response: {twiml_response}")
    
    return HTMLResponse(content=twiml_response, media_type="application/xml")
# =========================================
# MEDIA STREAM HANDLER
# =========================================
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    global active_connections
    # Generate unique connection ID for tracking
    connection_id = f"conn_{str(uuid.uuid4())[:8]}"
    
    # Extract call session ID and get phone number from registry
    customer_phone = "Unknown"
    call_sid = "unknown"
    
    try:
        # Extract call_sid from WebSocket query parameters
        if hasattr(websocket, 'query_params'):
            call_sid = websocket.query_params.get('call_sid', 'unknown')
            print(f"📞 [{connection_id}] Call SID: {call_sid}")
            
            # Get phone number from registry
            if call_sid in phone_registry:
                customer_phone = phone_registry[call_sid]
                print(f"✅ [{connection_id}] Phone retrieved from registry: {customer_phone}")
            else:
                print(f"❌ [{connection_id}] No phone found in registry for call SID: {call_sid}")
                print(f"🔍 [{connection_id}] Current registry: {phone_registry}")
        else:
            print(f"❌ [{connection_id}] No query_params available")
            
    except Exception as e:
        print(f"❌ [{connection_id}] Error in phone extraction: {e}")
    
    print(f"📱 [{connection_id}] FINAL customer phone: {customer_phone}")
    
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
                # Only increment counter after successful connections
                active_connections += 1
                print(f"🔗 [{connection_id}] Connected successfully (Active: {active_connections})")
                
                # CRITICAL FIX: Do not send session update immediately - wait for session.created first
                session_configured = False
                print(f"⏳ [{connection_id}] Waiting for session.created before configuring...")
                stream_sid = None
                drop_audio = False
                ai_speaking = False
                
                def _ulaw_to_linear(b):
                    """Convert G.711 µ-law to linear PCM"""
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
                    """Proper energy-based VAD for G.711 µ-law"""
                    try:
                        data = base64.b64decode(audio_b64, validate=False)
                        if not data or len(data) < 80:  # <10ms too short
                            return False
                        s = _ulaw_to_linear(data)
                        N = len(s)
                        abs_vals = [abs(x) for x in s]
                        mean_abs = sum(abs_vals) / N
                        loud_ratio = sum(1 for v in abs_vals if v > 900) / N
                        peak = max(abs_vals)
                        result = (peak > 2500 and loud_ratio > 0.01) or (mean_abs > 400 and loud_ratio > 0.02)
                        return result
                    except Exception:
                        return False
                
                def detect_strong_user_speech(audio_b64):
                    """CRITICAL FIX: Much more restrictive VAD to prevent AI voice false positives"""
                    try:
                        data = base64.b64decode(audio_b64, validate=False)
                        if not data or len(data) < 160:  # Require longer audio for confidence
                            return False
                        s = _ulaw_to_linear(data)
                        N = len(s)
                        abs_vals = [abs(x) for x in s]
                        mean_abs = sum(abs_vals) / N
                        loud_ratio = sum(1 for v in abs_vals if v > 1200) / N  # Higher threshold
                        peak = max(abs_vals)
                        
                        # CRITICAL: Much stricter criteria to avoid false positives from AI voice
                        strong_signal = (peak > 4000 and loud_ratio > 0.05) or (mean_abs > 800 and loud_ratio > 0.08)
                        
                        if strong_signal:
                            print(f"🔍 [{connection_id}] Strong speech detected - peak: {peak}, mean: {mean_abs:.1f}, loud_ratio: {loud_ratio:.3f}")
                        
                        return strong_signal
                    except Exception as e:
                        print(f"❌ [{connection_id}] Error in strong speech detection: {e}")
                        return False
                        
                async def receive_from_twilio():
                    nonlocal stream_sid, drop_audio, ai_speaking, customer_phone
                    try:
                        async for message in websocket.iter_text():
                            data = json.loads(message)
                            if data["event"] == "media":
                                # CRITICAL FIX: Skip processing audio during AI speech to prevent feedback loop
                                if ai_speaking:
                                    # Check for STRONG user interruption signal only
                                    if detect_strong_user_speech(data["media"]["payload"]):
                                        print(f"🎤 [{connection_id}] STRONG user interruption detected during AI speech!")
                                        drop_audio = True
                                        ai_speaking = False
                                        # Send cancel to OpenAI to stop generation
                                        try:
                                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                                        except:
                                            pass
                                    else:
                                        # CRITICAL: Drop audio during AI speech to prevent feedback
                                        continue
                                
                                # CRITICAL FIX: Only send audio when AI is NOT speaking
                                if not ai_speaking:
                                    audio_append = {
                                        "type": "input_audio_buffer.append",
                                        "audio": data["media"]["payload"]
                                    }
                                    await openai_ws.send(json.dumps(audio_append))
                            elif data["event"] == "start":
                                stream_sid = data["start"]["streamSid"]
                                print(f"📞 [{connection_id}] Stream started: {stream_sid}")
                                
                                # CRITICAL FIX: Extract CallSid from Twilio start event
                                twilio_call_sid = data["start"].get("callSid")
                                if twilio_call_sid:
                                    print(f"📞 [{connection_id}] CallSid from start event: {twilio_call_sid}")
                                    
                                    # Look up phone number in registry using the CallSid
                                    if twilio_call_sid in phone_registry:
                                        customer_phone = phone_registry[twilio_call_sid]
                                        print(f"✅ [{connection_id}] Phone resolved from start event: {customer_phone}")
                                    else:
                                        print(f"❌ [{connection_id}] CallSid not found in phone registry: {twilio_call_sid}")
                                        print(f"🔍 [{connection_id}] Registry contents: {phone_registry}")
                                else:
                                    print(f"❌ [{connection_id}] No CallSid in start event data")
                    except Exception as e:
                        print(f"❌ [{connection_id}] Error receiving from Twilio: {e}")
                async def send_to_twilio():
                    nonlocal stream_sid, drop_audio, ai_speaking, session_configured
                    try:
                        async for openai_message in openai_ws:
                            response = json.loads(openai_message)
                            if response["type"] in LOG_EVENT_TYPES:
                                print(f"Event: {response['type']}", response)
                            
                            # CRITICAL FIX: Send session update after receiving session.created
                            if response["type"] == "session.created" and not session_configured:
                                print(f"✅ [{connection_id}] session.created received, now sending our configuration...")
                                try:
                                    await send_session_update(openai_ws)
                                    session_configured = True
                                    print(f"📤 [{connection_id}] Session update sent successfully, waiting for session.updated...")
                                except Exception as e:
                                    print(f"❌ [{connection_id}] Failed to send session update: {e}")
                                continue  # Skip validation this time, wait for session.updated
                            
                            # Handle session.updated confirmation
                            elif response["type"] == "session.updated":
                                print(f"🎯 [{connection_id}] session.updated received - validating configuration...")
                                
                            # Validate session configuration was accepted (for both session.created and session.updated)
                            if response["type"] in ["session.created", "session.updated"]:
                                session_data = response.get("session", {})
                                
                                # Check if our instructions were applied
                                instructions = session_data.get("instructions", "")
                                if "Melt 8" in instructions and "اردو" in instructions:
                                    print(f"✅ [{connection_id}] Urdu pizza prompt applied successfully!")
                                else:
                                    print(f"❌ [{connection_id}] CRITICAL: Urdu prompt NOT applied!")
                                    print(f"🔍 [{connection_id}] Received instructions: {instructions[:100]}...")
                                
                                # Check if save_order tool was registered
                                tools = session_data.get("tools", [])
                                save_order_found = any(tool.get("name") == "save_order" for tool in tools)
                                if save_order_found:
                                    print(f"✅ [{connection_id}] save_order function registered successfully!")
                                else:
                                    print(f"❌ [{connection_id}] CRITICAL: save_order function NOT registered!")
                                    print(f"🔍 [{connection_id}] Received tools: {[t.get('name', 'unnamed') for t in tools]}")
                                
                                # Overall session configuration status
                                if "Melt 8" in instructions and save_order_found:
                                    print(f"🎉 [{connection_id}] Session configured perfectly - Ready for Urdu pizza orders!")
                                else:
                                    print(f"⚠️ [{connection_id}] Session configuration FAILED - Check above errors")
                            
                            # CRITICAL FIX: Enhanced AI speech state management
                            if response["type"] == "response.audio.start":
                                ai_speaking = True
                                drop_audio = False  # Enable AI audio output
                                print(f"🤖 [{connection_id}] AI started speaking - blocking user audio input")

                            elif response["type"] == "response.audio.done":
                                ai_speaking = False
                                print(f"🤖 [{connection_id}] AI finished speaking - enabling user audio input")

                            # CRITICAL: More conservative interruption handling
                            elif response["type"] == "input_audio_buffer.speech_started":
                                if not ai_speaking:  # Only handle if AI wasn't speaking
                                    print(f"🎤 [{connection_id}] User started speaking (server VAD)")
                                else:
                                    print(f"⚠️ [{connection_id}] Server VAD triggered during AI speech - potential feedback loop!")
                                    # Don't immediately stop AI - let strong user speech detection handle it
                                
                            elif response["type"] == "input_audio_buffer.speech_stopped":
                                if not ai_speaking:
                                    print(f"🔇 [{connection_id}] User stopped speaking (server VAD)")

                            # Reset drop flag when user finishes speaking and AI can respond
                            elif response["type"] == "input_audio_buffer.committed":
                                print(f"🔊 [{connection_id}] User audio committed - AI can respond")
                                drop_audio = False
                                # Only set ai_speaking to false if we're not currently generating
                                if ai_speaking:
                                    print(f"⚠️ [{connection_id}] Audio committed during AI speech - possible interruption")

                            # Handle function calls from OpenAI - Enhanced with better debugging and multiple event support
                            elif response["type"] == "response.function_call_arguments.delta":
                                # Function call in progress, log with details
                                delta_content = response.get('delta', '')
                                print(f"🔧 [{connection_id}] Function call streaming delta: {delta_content[:100]}...")
                            
                            elif response["type"] == "response.function_call_arguments.done":
                                # Function call completed via arguments.done event
                                print(f"🔧 [{connection_id}] Function call arguments.done event received")
                                print(f"🔍 [{connection_id}] Full event structure: {json.dumps(response, indent=2)}")
                                
                                try:
                                    call_id = response.get("call_id")
                                    function_name = response.get("name") 
                                    arguments_str = response.get("arguments", "{}")
                                    
                                    print(f"🔍 [{connection_id}] Extracted - call_id: {call_id}, name: {function_name}, args: {arguments_str}")
                                    
                                    if not call_id:
                                        print(f"❌ [{connection_id}] Missing call_id in function call event")
                                        continue
                                    
                                    if not function_name:
                                        print(f"❌ [{connection_id}] Missing function name in function call event")
                                        continue
                                    
                                    # Parse arguments safely
                                    try:
                                        arguments = json.loads(arguments_str) if arguments_str else {}
                                    except json.JSONDecodeError as e:
                                        print(f"❌ [{connection_id}] Failed to parse function arguments: {e}")
                                        arguments = {}
                                    
                                    # Use the enhanced function call handler
                                    await handle_function_call(connection_id, customer_phone, call_id, function_name, arguments, openai_ws)
                                        
                                except Exception as e:
                                    print(f"❌ [{connection_id}] Error processing function_call_arguments.done: {e}")
                                    import traceback
                                    traceback.print_exc()

                            # Handle response completion and check for function calls
                            elif response["type"] == "response.done":
                                ai_speaking = False
                                if response.get("response", {}).get("status") == "cancelled":
                                    print(f"❌ [{connection_id}] Response cancelled")
                                else:
                                    print(f"✅ [{connection_id}] Response completed")
                                    
                                    # Alternative function call handling via response.done event
                                    # Some implementations provide function calls in the output field
                                    try:
                                        output = response.get("output", [])
                                        if output:
                                            print(f"🔍 [{connection_id}] Checking response.done output for function calls: {len(output)} items")
                                            
                                        for item in output:
                                            content = item.get("content", [])
                                            for content_item in content:
                                                if content_item.get("type") == "function_call":
                                                    call_id = content_item.get("call_id")
                                                    function_name = content_item.get("name")
                                                    arguments_str = content_item.get("arguments", "{}")
                                                    
                                                    print(f"🔧 [{connection_id}] Function call via response.done: {function_name}")
                                                    print(f"🔍 [{connection_id}] call_id: {call_id}, args: {arguments_str}")
                                                    
                                                    if call_id and function_name:
                                                        try:
                                                            arguments = json.loads(arguments_str) if arguments_str else {}
                                                        except json.JSONDecodeError as e:
                                                            print(f"❌ [{connection_id}] Failed to parse function arguments from response.done: {e}")
                                                            arguments = {}
                                                        
                                                        # Use the enhanced function call handler
                                                        await handle_function_call(connection_id, customer_phone, call_id, function_name, arguments, openai_ws)
                                    except Exception as e:
                                        print(f"❌ [{connection_id}] Error processing function calls from response.done: {e}")
                            # Process audio deltas with responsive yielding
                            if response["type"] == "response.audio.delta" and response.get("delta") and not drop_audio:
                                try:
                                    # Mark AI as speaking on first audio delta
                                    if not ai_speaking:
                                        ai_speaking = True
                                        print(f"🤖 [{connection_id}] AI started speaking (delta)")

                                    # Decode audio data
                                    audio_data = base64.b64decode(response["delta"])

                                    # Split into 20ms frames (160 bytes for G.711 µ-law at 8kHz)
                                    frame_size = 160
                                    frame_count = 0
                                    for i in range(0, len(audio_data), frame_size):
                                        # Check if interrupted while processing
                                        if drop_audio:
                                            break

                                        frame = audio_data[i:i + frame_size]
                                        if len(frame) == frame_size and stream_sid:  # Only send complete frames
                                            frame_b64 = base64.b64encode(frame).decode("utf-8")

                                            # Send frame directly to Twilio (no buffering)
                                            try:
                                                audio_delta = {
                                                    "event": "media",
                                                    "streamSid": stream_sid,
                                                    "media": {"payload": frame_b64}
                                                }
                                                await websocket.send_json(audio_delta)
                                            except Exception as e:
                                                print(f"❌ [{connection_id}] Error sending audio frame: {e}")

                                            # Yield every 2 frames for ultra-responsive interruption
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
    # Urdu pizza ordering prompt
    urdu_prompt = """
آپ Melt 8 پیزا شاپ کے سیلز ایجنٹ ہیں۔ ہمیشہ اردو میں بات کریں۔

🚨 CRITICAL RULES - NEVER BREAK THESE:
1. یہ 4 چیزیں مانگے بغیر آرڈر کنفرم کریں: FLAVOR + SIZE + ADDRESS + NAME
2. ALWAYS ask "آپ کا پتہ کیا ہے؟" - NEVER skip address!  
3. NEVER say "آرڈر کنفرم" until save_order function succeeds
4. If ANY information missing, ask again immediately

MANDATORY ORDER (NO EXCEPTIONS):
1. Greet: "السلام علیکم، ویلکم ٹو Melt 8"
2. Ask FLAVOR: "کون سا پیزا چاہیے؟" (Pepperoni, Veggie, Margherita, BBQ Chicken, Hawaiian)
3. Ask SIZE: "کس سائز میں؟ Small, Medium یا Large?"
4. Ask DRINK: "کوئی ڈرنک؟" (optional - Pepsi, Coke, Seven Up)
5. 🚨 MUST ASK ADDRESS: "آپ کا ڈیلیوری پتہ کیا ہے؟" (area, street, city)
6. 🚨 MUST ASK NAME: "آپ کا نام؟"  
7. 🚨 CALL save_order function immediately with all info
8. 🚨 ONLY after function success say "آپ کا آرڈر کنفرم ہو گیا"

⛔ FORBIDDEN:
- Confirming order before collecting address
- Saying "آرڈر کنفرم" without save_order function success
- Skipping address question
- Long conversations - collect info fast

EXAMPLE CORRECT FLOW:
User: "Pizza chahiye"  
You: "کون سا فلیور؟ Veggie, BBQ Chicken?"
User: "Veggie"
You: "سائز؟ Small, Medium یا Large?"
User: "Medium" 
You: "کوئی ڈرنک؟"
User: "Coke"
You: "🚨آپ کا ڈیلیوری پتہ کیا ہے؟"
User: gives address
You: "آپ کا نام؟"
User: gives name
You: [CALLS save_order function]
You: "آرڈر کنفرم! 30 منٹ میں آئے گا"

Remember: ADDRESS IS MANDATORY! Never skip it!"""

    # CRITICAL FIX: Proper OpenAI Realtime API session configuration
    session_update = {
        "type": "session.update",
        "session": {
            "modalities": ["text", "audio"],
            "instructions": urdu_prompt,
            "voice": VOICE,
            "input_audio_format": "g711_ulaw",  # CRITICAL: Set correct audio format for Twilio
            "output_audio_format": "g711_ulaw", # CRITICAL: Match Twilio's expected format
            "input_audio_transcription": {
                "model": "whisper-1"
            },
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.7,  # CRITICAL FIX: Higher threshold to prevent AI voice triggering
                "prefix_padding_ms": 500,  # More padding to avoid cutting user speech
                "silence_duration_ms": 800  # Longer silence required to prevent false triggers from AI voice
            },
            "tools": [
                {
                    "type": "function",
                    "name": "save_order",
                    "description": "Save a completed pizza order to the database.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "flavour": {
                                "type": "string",
                                "description": "Pizza flavour chosen by the customer (e.g., Pepperoni, Veggie)."
                            },
                            "size": {
                                "type": "string",
                                "description": "Pizza size.",
                                "enum": ["Small", "Medium", "Large"]
                            },
                            "drink": {
                                "type": "string",
                                "description": "Optional drink choice. If none, send an empty string."
                            },
                            "address": {
                                "type": "string",
                                "description": "Delivery address (street, area, city)."
                            },
                            "customer_name": {
                                "type": "string",
                                "description": "Customer name."
                            }
                        },
                        "required": ["flavour", "size", "address"]
                    }
                }
            ],
            "tool_choice": "auto",
            "temperature": 0.8,
            "max_response_output_tokens": 4096
        }
    }
    
    print(f"🔧 CRITICAL FIX: Sending corrected session update for Twilio audio compatibility")
    print("Session config summary:")
    print(f"- Instructions: {len(urdu_prompt)} chars (Urdu pizza prompt)")
    print(f"- Tools: {len(session_update['session']['tools'])} function(s)")
    print(f"- Voice: {VOICE}")
    print(f"- AUDIO FORMAT: {session_update['session']['input_audio_format']} -> {session_update['session']['output_audio_format']}")
    print(f"- Tool choice: {session_update['session']['tool_choice']}")
    
    try:
        await openai_ws.send(json.dumps(session_update))
        print("✅ Session update sent successfully")
        
        # Wait briefly for session confirmation
        await asyncio.sleep(0.1)
        
    except Exception as e:
        print(f"❌ Failed to send session update: {e}")
        import traceback
        traceback.print_exc()
        raise e
# =========================================
# MAIN
# =========================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)