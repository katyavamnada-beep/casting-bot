from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import os

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def main():
    creds = None
    if os.path.exists("token_drive.json"):
        creds = Credentials.from_authorized_user_file("token_drive.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
            creds = flow.run_local_server(host="localhost", port=0)

        with open("token_drive.json", "w") as f:
            f.write(creds.to_json())

    print("OK: token_drive.json created")

if __name__ == "__main__":
    main()
