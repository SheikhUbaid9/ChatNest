"""
gmail_login.py — One-time Gmail OAuth2 login.
Run this from Terminal:  python gmail_login.py
"""
import sys
import webbrowser
sys.path.insert(0, '.')

from google_auth_oauthlib.flow import InstalledAppFlow
from config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

s = get_settings()

flow = InstalledAppFlow.from_client_secrets_file(
    str(s.gmail_credentials_path),
    SCOPES,
    redirect_uri="urn:ietf:wg:oauth:2.0:oob",
)

auth_url, _ = flow.authorization_url(
    prompt="consent",
    login_hint="hasaanranaahmad@gmail.com",
    access_type="offline",
)

print("\n📧  Gmail OAuth2 Login")
print("=" * 50)
print("\nOpening browser...")
webbrowser.open(auth_url)
print("\nIf the browser didn't open, visit this URL manually:")
print(f"\n  {auth_url}\n")
print("=" * 50)

code = input("Paste the authorization code here: ").strip()

flow.fetch_token(code=code)
s.gmail_token_path.write_text(flow.credentials.to_json())

print(f"\n✅  Done! Token saved to: {s.gmail_token_path}")
print("   You can now start the server: python -m uvicorn ui.server:app --port 8000\n")
