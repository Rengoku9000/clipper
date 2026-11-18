"""OAuth connections and video publishing for the local dashboard."""

import base64
import hashlib
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlparse

import requests


PLATFORMS = ("youtube", "instagram", "facebook")
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/youtube/v3"
META_VERSION = "v27.0"
META_API = f"https://graph.facebook.com/{META_VERSION}"
META_PAGE_FIELDS = "id,name,access_token,instagram_business_account{id,username}"
META_CONTENT_TASKS = {
    "CREATE_CONTENT", "MANAGE", "PROFILE_PLUS_CREATE_CONTENT",
    "PROFILE_PLUS_FULL_CONTROL", "PROFILE_PLUS_MANAGE", "PROFILE_PLUS_FACEBOOK_ACCESS",
}


def call(method, url, *, token=None, oauth=False, headers=None, timeout=25, **kwargs):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"{'OAuth' if oauth else 'Bearer'} {token}"
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout,
                                    allow_redirects=False, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError("Platform request could not be completed.") from error
    if not response.ok or response.is_redirect:
        # Keep request URLs, authorization headers and provider payloads out of logs.
        raise RuntimeError(f"Platform returned HTTP {response.status_code}. Check the app permissions and account access.")
    return response


def graph(method, path, token=None, **kwargs):
    return call(method, f"{META_API}/{path}", token=token, **kwargs).json()


class Accounts:
    def __init__(self, local_url):
        self.local_url = local_url
        self.lock = threading.RLock()
        self.states = {}
        self.destinations = {}
        self.notice = ""
        self.meta_credentials = None

    def settings(self, provider):
        prefix = "CLIPPER_GOOGLE" if provider == "youtube" else "CLIPPER_META"
        identifier = os.getenv(f"{prefix}_CLIENT_ID")
        secret = os.getenv(f"{prefix}_CLIENT_SECRET")
        redirect = f"{self.local_url}/oauth/{provider}"
        if provider == "meta":
            redirect = os.getenv("CLIPPER_META_REDIRECT_URI", redirect)
            if self.meta_credentials:
                identifier, secret, _ = self.meta_credentials
        return identifier, secret, redirect

    def meta_config_id(self):
        return self.meta_credentials[2] if self.meta_credentials else os.getenv("CLIPPER_META_CONFIG_ID")

    def configure_meta(self, identifier, secret, config_id):
        if not all(isinstance(value, str) for value in (identifier, secret, config_id)):
            raise ValueError("Enter the Meta App ID, App Secret and Configuration ID.")
        identifier, secret, config_id = identifier.strip(), secret.strip(), config_id.strip()
        if not all(re.fullmatch(r"[0-9]{5,30}", value) for value in (identifier, config_id)):
            raise ValueError("Enter the numeric Meta App ID and Configuration ID.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", secret):
            raise ValueError("Enter the App Secret shown in Meta’s Basic app settings.")
        with self.lock:
            if self.destinations.get("instagram") or self.destinations.get("facebook"):
                raise ValueError("Disconnect Facebook and Instagram before changing the Meta app.")
            self.meta_credentials = (identifier, secret, config_id)
            self.states = {key: value for key, value in self.states.items() if value[0] != "meta"}
            self.notice = "Meta credentials loaded for this session. Connect Instagram to authorize publishing."

    def summary(self):
        with self.lock:
            return {
                "configured": {
                    provider: bool(all(self.settings(provider)[:2]) and
                                   (provider != "meta" or self.meta_config_id()))
                    for provider in ("youtube", "meta")
                },
                "meta_app": {"identifier": self.settings("meta")[0] or "",
                             "config_id": self.meta_config_id() or ""},
                "redirects": {provider: self.settings(provider)[2] for provider in ("youtube", "meta")},
                "destinations": {
                    platform: [{key: account[key] for key in ("id", "name")}
                               for account in accounts.values()]
                    for platform, accounts in self.destinations.items()
                },
                "notice": self.notice,
            }

    def begin(self, provider):
        if provider not in ("youtube", "meta") or not self.summary()["configured"][provider]:
            raise ValueError("Configure the provider credentials on the dashboard server first.")
        identifier, _, redirect = self.settings(provider)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        with self.lock:
            now = time.monotonic()
            self.states = {key: item for key, item in self.states.items() if item[2] > now}
            self.states[state] = (provider, verifier, now + 600)
        query = {"client_id": identifier, "redirect_uri": redirect, "state": state, "response_type": "code"}
        if provider == "youtube":
            endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            query.update({
                "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
                "code_challenge_method": "S256", "code_challenge": challenge.decode().rstrip("="),
                "access_type": "offline", "prompt": "consent",
            })
        else:
            endpoint = f"https://www.facebook.com/{META_VERSION}/dialog/oauth"
            query["config_id"] = self.meta_config_id()
        return f"{endpoint}?{urlencode(query)}"

    def finish(self, provider, state, code):
        with self.lock:
            request = self.states.pop(state, None)
        if not request or request[0] != provider or request[2] < time.monotonic():
            raise ValueError("The connection request expired. Start a new connection.")
        if not code:
            raise ValueError("Authorization was declined or cancelled.")
        identifier, secret, redirect = self.settings(provider)
        params = {"client_id": identifier, "client_secret": secret, "redirect_uri": redirect, "code": code}
        if provider == "youtube":
            params.update(grant_type="authorization_code", code_verifier=request[1])
            token = call("POST", GOOGLE_TOKEN, data=params).json()
            result = call("GET", f"{GOOGLE_API}/channels", token=token["access_token"],
                          params={"part": "snippet", "mine": "true"}).json()
            destinations = {"youtube": {
                item["id"]: {
                    "id": item["id"], "name": item["snippet"]["title"],
                    "token": token["access_token"], "refresh": token.get("refresh_token"),
                    "expires": time.time() + token.get("expires_in", 3600),
                }
                for item in result.get("items", [])
            }}
        else:
            token = graph("GET", "oauth/access_token", params=params)["access_token"]
            page_id = os.getenv("CLIPPER_META_PAGE_ID", "").strip()
            destinations = self.meta_destinations(token, page_id)
        with self.lock:
            self.destinations.update(destinations)
            count = sum(map(len, destinations.values()))
            self.notice = f"Connected {count} publishing destination{'s' if count != 1 else ''}."
            if not count:
                self.notice = "No eligible destination was returned. Check that you granted account and publishing access."
            elif provider == "meta" and not destinations["instagram"]:
                self.notice = "Facebook Page connected. Meta did not report a professional Instagram account linked to it."

    @staticmethod
    def meta_destinations(token, page_id=""):
        destinations = {"facebook": {}, "instagram": {}}
        if page_id:
            if not re.fullmatch(r"[0-9]{5,30}", page_id):
                raise ValueError("The configured Meta Page ID is invalid.")
            try:
                page = graph("GET", page_id, token, params={"fields": META_PAGE_FIELDS})
            except RuntimeError as error:
                raise RuntimeError(f"The selected Facebook Page could not be read. {error}") from error
            if not page.get("access_token"):
                raise ValueError("Meta returned the selected Page without a Page access token. Check that your Facebook profile has Page access and accepted the publishing permissions.")
            Accounts.add_meta_page(destinations, page)
            return destinations

        params = {"fields": f"{META_PAGE_FIELDS},tasks", "limit": 100}
        while True:
            result = graph("GET", "me/accounts", token, params=params)
            for page in result.get("data", []):
                if not page.get("access_token") or not set(page.get("tasks", [])) & META_CONTENT_TASKS:
                    continue
                Accounts.add_meta_page(destinations, page)
            paging = result.get("paging", {})
            if not paging.get("next"):
                return destinations
            params["after"] = paging["cursors"]["after"]

    @staticmethod
    def add_meta_page(destinations, page):
        destinations["facebook"][page["id"]] = {
            "id": page["id"], "name": page["name"], "token": page["access_token"],
        }
        instagram = page.get("instagram_business_account")
        if instagram:
            destinations["instagram"][instagram["id"]] = {
                "id": instagram["id"], "name": "@" + (instagram.get("username") or instagram["id"]),
                "token": page["access_token"],
            }

    def disconnect(self, platform):
        if platform not in PLATFORMS:
            raise ValueError("Unknown platform.")
        with self.lock:
            self.destinations.pop(platform, None)
            self.notice = "Destination disconnected from this dashboard."

    def get(self, platform, identifier):
        with self.lock:
            account = self.destinations.get(platform, {}).get(identifier)
            if not account:
                raise ValueError("Connect and select a publishing destination first.")
            if platform == "youtube" and time.time() >= account["expires"] - 60:
                if not account["refresh"]:
                    raise ValueError("Reconnect your YouTube account to refresh access.")
                client_id, secret, _ = self.settings("youtube")
                token = call("POST", GOOGLE_TOKEN, data={
                    "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
                    "refresh_token": account["refresh"],
                }).json()
                account.update(token=token["access_token"], expires=time.time() + token["expires_in"])
            return account.copy()


def upload_binary(method, url, path, token, *, meta=False):
    expected_host = "rupload.facebook.com" if meta else "www.googleapis.com"
    target = urlparse(url)
    if target.scheme != "https" or target.hostname != expected_host:
        raise RuntimeError("The upload endpoint returned by the platform was unexpected.")
    size = str(path.stat().st_size)
    headers = {"Content-Type": "video/mp4", "Content-Length": size}
    if meta:
        headers.update(offset="0", file_size=size)
    with path.open("rb") as stream:
        return call(method, url, token=token, oauth=meta, headers=headers,
                    data=stream, timeout=180).json()


def publish(platform, account, path, title, description, visibility, made_for_kids, record):
    token = account["token"]
    identifier = account["id"]
    if platform == "youtube":
        session = call(
            "POST", "https://www.googleapis.com/upload/youtube/v3/videos", token=token,
            params={"uploadType": "resumable", "part": "snippet,status", "notifySubscribers": "false"},
            headers={"X-Upload-Content-Length": str(path.stat().st_size), "X-Upload-Content-Type": "video/mp4"},
            json={"snippet": {"title": title, "description": description},
                  "status": {"privacyStatus": visibility, "selfDeclaredMadeForKids": made_for_kids}},
        )
        record("Submitting video", None)
        remote = upload_binary("PUT", session.headers["Location"], path, token)["id"]
        return remote, f"https://www.youtube.com/shorts/{remote}"
    if platform == "facebook":
        endpoint = f"{identifier}/video_reels"
        session = graph("POST", endpoint, token, data={"upload_phase": "start"})
        remote = session["video_id"]
        record("Uploading video", remote)
        upload_binary("POST", session["upload_url"], path, token, meta=True)
        record("Submitting Reel", remote)
        result = graph("POST", endpoint, token, data={
            "video_id": remote, "upload_phase": "finish", "video_state": "PUBLISHED",
            "title": title, "description": description,
        })
        if not result.get("success"):
            raise RuntimeError("Facebook did not acknowledge the publish request.")
        return remote, f"https://www.facebook.com/reel/{remote}"

    container = graph("POST", f"{identifier}/media", token, data={
        "media_type": "REELS", "upload_type": "resumable",
        "caption": f"{title}\n\n{description}".strip(),
    })["id"]
    record("Uploading video", container)
    upload_binary("POST", f"https://rupload.facebook.com/ig-api-upload/{META_VERSION}/{container}",
                  path, token, meta=True)
    record("Processing video", container)
    for _ in range(30):
        status = graph("GET", container, token, params={"fields": "status_code"})["status_code"]
        if status == "FINISHED":
            break
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram container status: {status.lower()}.")
        time.sleep(4)
    else:
        raise RuntimeError("Instagram is still processing the video.")
    record("Submitting Reel", container)
    remote = graph("POST", f"{identifier}/media_publish", token, data={"creation_id": container})["id"]
    try:
        link = graph("GET", remote, token, params={"fields": "permalink"}).get("permalink")
    except RuntimeError:
        link = None
    return remote, link
