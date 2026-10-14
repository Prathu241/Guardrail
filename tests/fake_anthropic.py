"""A local stand-in for the Anthropic Messages API, for tests that drive the *real* SDK clients
(anthropic, langchain-anthropic) over HTTP: response parsing, error mapping, timeouts and
retries are the SDKs' own, only the server is scripted.

Responses are queued per route: "agent" for requests that carry tools, otherwise the model name.
A queued entry is a Reply (status, body, delay). An empty queue falls back to ``default``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    body: Any
    status: int = 200
    delay: float = 0.0
    raw: bool = False  # send ``body`` as-is (str) instead of JSON-encoding it


def message(content: list[dict], stop_reason: str = "end_turn", model: str = "claude-test") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}}


def text(t: str, stop_reason: str = "end_turn") -> Reply:
    return Reply(message([{"type": "text", "text": t}], stop_reason))


def review(grounded: Any, confidence: Any, concerns: Any = (), evidence: Any = (), **extra: Any) -> Reply:
    payload = {"grounded": grounded, "confidence": confidence, "concerns": list(concerns),
               "evidence": list(evidence), **extra}
    return text(json.dumps(payload))


_tool_ids = iter(range(1, 10_000))


def tool_use(name: str, **args: Any) -> Reply:
    block = {"type": "tool_use", "id": f"toolu_{next(_tool_ids):04d}", "name": name, "input": args}
    return Reply(message([{"type": "text", "text": f"Calling {name}."}, block], "tool_use"))


def error(status: int, kind: str) -> Reply:
    return Reply({"type": "error", "error": {"type": kind, "message": f"simulated {kind}"}}, status)


@dataclass
class FakeAnthropic:
    default: Reply = field(default_factory=lambda: error(500, "api_error"))
    queues: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    requests: list[dict] = field(default_factory=list)

    def script(self, route: str, *replies: Reply) -> None:
        self.queues[route].extend(replies)

    def calls(self, route: str) -> list[dict]:
        return [r for r in self.requests if r["route"] == route]

    # ---- server ---------------------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                route = "agent" if body.get("tools") else body.get("model", "")
                fake.requests.append({"route": route, "path": self.path, "body": body,
                                      "headers": dict(self.headers)})
                queue = fake.queues[route]
                reply = queue.popleft() if queue else fake.default
                if reply.delay:
                    time.sleep(reply.delay)
                data = (reply.body if reply.raw else json.dumps(reply.body)).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    self.send_header("request-id", "req_test")
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up (timeout) - that is the point of the test

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
