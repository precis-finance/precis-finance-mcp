# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""In-process Keycloak protocol fake; never opens a socket."""
import json
from urllib.parse import parse_qs

import httpx


class FakeKeycloak:
    def __init__(self):
        self.requests = []
        self.users = {}
        self.tokens = []
        self.responses = []
        self.login_responses = []

    def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/token"):
            self.tokens.append(parse_qs(request.content.decode()))
            if self.login_responses:
                return self.login_responses.pop(0)
            return httpx.Response(200, json={"access_token": "fake-token"})
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if request.method == "GET" and request.url.path.endswith("/users"):
            user = self.users.get(request.url.params.get("username"))
            return httpx.Response(200, json=[user] if user else [])
        if request.method == "POST" and request.url.path.endswith("/users"):
            body = json.loads(request.content)
            if body["username"] in self.users:
                return httpx.Response(409)
            self.users[body["username"]] = {**body, "id": "user-uuid"}
            return httpx.Response(201, headers={"Location": "/users/user-uuid"})
        return httpx.Response(204)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))
