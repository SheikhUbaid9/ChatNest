ChatNest (v1.0)
Unified messaging platform that aggregates Gmail, Slack, and Telegram into a single inbox with a web-based dashboard.
Architecture
api/ — Core API layer (FastAPI, routing, request handling)
clients/ — Platform integration clients (Gmail, Slack, Telegram)
tools/ — Shared utility and helper modules
ui/ — Frontend dashboard (HTML, CSS, JavaScript)
main.py — Application entry point
database.py — Database models and connection logic
config.py — Environment and app configuration
gmail_login.py — Gmail OAuth2 authentication flow
telethon_login.py — Telegram authentication via Telethon
Patterns implemented:
Unified inbox / adapter pattern
OAuth2 authentication (Gmail)
Session-based auth (Telegram via Telethon)
Docker-based containerized deployment
Vercel frontend deployment
Prerequisites
Docker Desktop / Docker Engine
Docker Compose
Python 3.10+ (only if running locally outside Docker)
1) Environment Setup
Copy the example env file:
cp .env.example .env
Fill in your credentials in .env:
GMAIL_CLIENT_ID=your_gmail_client_id
GMAIL_CLIENT_SECRET=your_gmail_client_secret
SLACK_BOT_TOKEN=your_slack_bot_token
TELEGRAM_API_ID=your_telegram_api_id
TELEGRAM_API_HASH=your_telegram_api_hash
2) Start the Full Stack
docker compose up --build
Or run locally:
pip install -r requirements.txt
python main.py
3) Gmail Login
Authenticate your Gmail account:
python gmail_login.py
Follow the OAuth2 browser prompt. Credentials will be saved locally.
4) Telegram Login
Authenticate your Telegram account:
python telethon_login.py
Enter your phone number and the OTP sent to your Telegram app.
5) Frontend Dashboard
Once the server is running, open:
http://localhost:8000
Pages:
/ — Unified inbox (Gmail + Slack + Telegram messages)
/compose — Send a new message
/settings — Manage connected accounts
Features included:
Unified inbox view across all platforms
Platform badges (Gmail / Slack / Telegram)
Message threading
Compose and reply workflow
Connected accounts management
6) API Overview
Base URL: http://localhost:8000
Get all messages:
curl http://localhost:8000/api/messages
Get messages by platform:
curl "http://localhost:8000/api/messages?platform=gmail"
curl "http://localhost:8000/api/messages?platform=slack"
curl "http://localhost:8000/api/messages?platform=telegram"
Send a message:
curl -X POST http://localhost:8000/api/send \
  -H "Content-Type: application/json" \
  -d '{"platform":"gmail","to":"user@example.com","content":"Hello!"}'
7) Local Dev Without Docker
Install dependencies:
pip install -r requirements.txt
Run the app:
python main.py
Frontend (if developing UI separately):
Open ui/index.html directly in your browser, or serve it:
cd ui
python -m http.server 3000
Deployment
Frontend is deployable to Vercel via vercel.json. Backend can be containerized via the provided Dockerfile.
docker build -t chatnest .
docker run -p 8000:8000 chatnest
Notes
Slack and Telegram integrations may require approved API credentials from their developer portals.
Gmail OAuth requires a Google Cloud project with the Gmail API enabled.
All credentials are loaded from .env — never commit your .env file.
