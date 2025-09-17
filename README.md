# Melt 8 Pizza Voice Ordering System

A real-time AI voice application that integrates Twilio's voice services with OpenAI's Realtime API to create an interactive pizza ordering assistant for Pakistani customers.

## Features

🍕 **Voice Pizza Ordering in Urdu** - Natural conversation AI that takes pizza orders in Urdu language
📞 **Twilio Voice Integration** - Handle incoming phone calls seamlessly  
🔄 **Real-time AI Responses** - OpenAI Realtime API for instant voice-to-voice conversation
📊 **Chef Dashboard** - Web interface for managing orders with real-time updates
🗃️ **PostgreSQL Database** - Persistent order storage with customer information
🔒 **Production Ready** - Never sleeps, SSL secured, custom domain support

## System Architecture

- **Backend**: FastAPI with async/await for concurrent voice streams
- **Database**: PostgreSQL with order management
- **Voice AI**: OpenAI Realtime API with Urdu language support
- **Telephony**: Twilio Voice API for phone call handling
- **Deployment**: Replit Reserved VM (always-on)

## Order Flow

1. Customer calls Twilio phone number
2. AI greets in Urdu and asks for pizza preferences
3. AI collects: flavor, size, drink, delivery address, customer name
4. Order saved to database with customer phone number
5. Chef sees order in real-time dashboard
6. Chef updates order status (preparing → ready → delivered)

## Setup Requirements

### Environment Variables
```env
OPENAI_API_KEY=your_openai_api_key
DATABASE_URL=postgresql://user:pass@host/db
PUBLIC_BASE_URL=your-domain.com
CHEF_USERNAME=chef
CHEF_PASSWORD=your_secure_password
```

### Dependencies
- Python 3.11+
- FastAPI
- OpenAI SDK
- Twilio SDK
- PostgreSQL (psycopg2)
- WebSockets

## Deployment

1. **Database Setup**: Create PostgreSQL orders table
2. **Environment Variables**: Configure all required secrets
3. **Twilio Configuration**: Set webhook URL to `/incoming-call`
4. **Deploy**: Use Reserved VM deployment (never sleeps)
5. **SSL**: Configure HTTPS for webhook security

## API Endpoints

- `POST /incoming-call` - Twilio voice webhook
- `WS /media-stream` - WebSocket for real-time audio
- `GET /chef-dashboard` - Chef order management interface  
- `GET /api/orders` - Orders API for dashboard
- `PUT /api/orders/{id}/status` - Update order status

## Database Schema

```sql
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    flavour VARCHAR(100) NOT NULL,
    size VARCHAR(20) NOT NULL,
    drink VARCHAR(50),
    address TEXT NOT NULL,
    customer_name VARCHAR(100),
    customer_phone VARCHAR(20),
    order_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) DEFAULT 'new'
);
```

## Production Features

✅ Reserved VM Deployment (never sleeps)  
✅ Custom domain with SSL certificates  
✅ Cross-process phone number reliability  
✅ Concurrent call support  
✅ Real-time order dashboard  
✅ Secure authentication for chef access  

## Business Impact

Perfect for Pakistani pizza shops wanting to:
- Accept orders via phone calls in Urdu
- Reduce staff workload for order taking
- Provide 24/7 ordering availability  
- Maintain accurate order records
- Streamline kitchen operations

---

Built with ❤️ for Melt 8 Pakistani Pizza Shop