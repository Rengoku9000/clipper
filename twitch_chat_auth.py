"""Authorize the local clipper to read Twitch chat using a public Twitch app."""

import argparse
import os
import re
import time
from pathlib import Path

import requests

from credential_store import CredentialStore


SCOPE = "user:read:chat"
OAUTH = "https://id.twitch.tv/oauth2"
STORE = CredentialStore(Path(__file__).resolve().parent / ".dashboard" / "twitch-chat.dpapi")


def saved_credentials():
    if os.name == "nt":
        try:
            return STORE.load()
        except (ValueError, OSError):
            pass
    return {}


def store_token(client_id, token):
    if SCOPE not in token.get("scope", []) or not token.get("refresh_token"):
        raise ValueError("Twitch did not return renewable read-only chat access.")
    credentials = {"client_id": client_id, "access_token": token["access_token"],
                   "refresh_token": token["refresh_token"], "expires_at": time.time() + token["expires_in"]}
    STORE.path.parent.mkdir(exist_ok=True)
    STORE.save(credentials)
    return credentials


def configured():
    return bool((os.getenv("TWITCH_CLIENT_ID") and os.getenv("TWITCH_USER_TOKEN")) or saved_credentials())


def capture_credentials():
    client_id, token = os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_USER_TOKEN")
    if client_id and token:
        return client_id, token
    saved = saved_credentials()
    if not saved:
        raise ValueError("Connect Twitch chat before enabling chat detection.")
    if time.time() >= saved["expires_at"] - 90:
        response = requests.post(f"{OAUTH}/token", data={"client_id": saved["client_id"],
                                 "grant_type": "refresh_token", "refresh_token": saved["refresh_token"]}, timeout=10)
        if not response.ok:
            raise ValueError("Twitch chat authorization needs to be renewed. Reconnect Twitch chat.")
        saved = store_token(saved["client_id"], response.json())
    return saved["client_id"], saved["access_token"]


def authorize(client_id):
    response = requests.post(f"{OAUTH}/device", data={"client_id": client_id, "scopes": SCOPE}, timeout=10)
    if not response.ok:
        raise ValueError("Twitch rejected the chat authorization request. Check the public app Client ID.")
    device = response.json()
    print(f"Open https://www.twitch.tv/activate and enter {device['user_code']}.", flush=True)
    interval = device["interval"]
    deadline = time.monotonic() + device["expires_in"]
    while time.monotonic() + interval < deadline:
        time.sleep(interval)
        response = requests.post(f"{OAUTH}/token", data={"client_id": client_id, "scopes": SCOPE,
                                 "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                 "device_code": device["device_code"]}, timeout=10)
        token = response.json()
        if response.ok:
            identity = requests.get(f"{OAUTH}/validate", headers={"Authorization": f"OAuth {token['access_token']}"}, timeout=10)
            identity.raise_for_status()
            if identity.json().get("client_id") != client_id:
                raise ValueError("Twitch returned a token for a different app.")
            store_token(client_id, token)
            print("Twitch chat is connected.", flush=True)
            return
        if token.get("message") == "slow_down":
            interval += 5
        elif token.get("message") != "authorization_pending":
            raise ValueError("Twitch did not approve the chat authorization.")
    raise ValueError("The Twitch authorization code expired. Start again for a new code.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_id")
    args = parser.parse_args()
    if os.name != "nt" or not re.fullmatch(r"[A-Za-z0-9]{20,64}", args.client_id):
        parser.error("Enter a Twitch public app's Client ID on Windows.")
    try:
        authorize(args.client_id)
    except (OSError, ValueError, KeyError, requests.RequestException) as error:
        parser.exit(1, f"Twitch chat connection failed: {error}\n")


if __name__ == "__main__":
    main()
