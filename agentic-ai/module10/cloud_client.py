"""Authenticated client for the cloud demo. No account or Region defaults."""
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from uuid import uuid4

import boto3

from .models import ReviewResponse
from .urls import open_url


class CloudClient:
    def __init__(self, outputs: Path, credentials: Path):
        self.outputs = json.loads(outputs.read_text())
        self.credentials = json.loads(credentials.read_text())
        self.base_url = self.outputs["apiUrl"].rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ValueError("Cloud endpoint must use HTTPS")
        self._tokens = {}
        self.last_events = []

    def token(self, team):
        cached = self._tokens.get(team)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        user = self.credentials["users"][team]
        response = boto3.client("cognito-idp").initiate_auth(
            ClientId=self.credentials["client_id"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": user["username"], "PASSWORD": user["password"]},
        )["AuthenticationResult"]
        self._tokens[team] = (response["AccessToken"], time.time() + response["ExpiresIn"])
        return response["AccessToken"]

    def call(self, path, *, team="platform", body=None, headers=None, on_event=None):
        headers = {"Content-Type": "application/json", **(headers or {})}
        if team is not None:
            headers["Authorization"] = "Bearer " + self.token(team)
        request = urllib.request.Request(self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        start = time.monotonic()
        events = []
        self.last_events = events
        try:
            with open_url(request, timeout=215) as response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    return ReviewResponse(response.status, json.load(response))
                event_name = None
                result = None
                for line in response:
                    line = line.decode().rstrip("\r\n")
                    if line.startswith("event: "):
                        event_name = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        event = {"event": event_name, "seconds": time.monotonic() - start, "data": data}
                        events.append(event)
                        if on_event:
                            on_event(event)
                        if event_name in {"result", "error"}:
                            result = ReviewResponse(200 if event_name == "result" else 502, data)
                if result is None:
                    raise RuntimeError("The stream ended without a terminal result")
                self.last_events = events
                return result
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read())
            except (ValueError, UnicodeDecodeError):
                body = {"error": f"HTTP {error.code}"}
            return ReviewResponse(error.code, body)

    def invoke(self, team, summary, request_id=None, headers=None, on_event=None):
        return self.call("/invoke", team=team, headers=headers, on_event=on_event, body={
            "change_summary": summary, "request_id": request_id or uuid4().hex,
        })

    def configure(self, session, section, backend, **extra):
        for attempt in range(4):
            response = self.call("/control", body={"session": session, "section": section, "backend": backend, **extra})
            if response.status_code != 429 or attempt == 3:
                break
            # The preceding zero-model probe can exhaust the operator's burst.
            time.sleep(attempt + 1)
        if response.status_code != 200:
            raise RuntimeError(f"Cloud demo configuration failed ({response.status_code})")
        return response.body
