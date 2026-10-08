"""Chat suggestion decisions keep one idempotent payload across retries."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx

from ahadiff.serve import routes_growth_chat as chat_routes


def test_chat_confirmation_retry_uses_same_topic_after_page_restart(
    monkeypatch: Any,
) -> None:
    account_id = uuid.uuid4()
    session_id = uuid.uuid4()
    suggestion_id = uuid.uuid4()
    posted: list[dict[str, str | None]] = []
    body = chat_routes._ChatDecision(
        cloud_url="http://127.0.0.1:18781",
        access_token="test-token",
        account_id=account_id,
        decision="confirm",
    )

    async def parse(_request: Any, _model: Any) -> Any:
        return body

    class Client:
        async def post(self, path: str, *, headers: dict[str, str],
                       json: dict[str, str | None]) -> httpx.Response:
            assert path.endswith(f"/{suggestion_id}/decision")
            posted.append(json.copy())
            return httpx.Response(200, json={"status": "confirmed",
                                              "topic_id": json["topic_id"]})

    @asynccontextmanager
    async def client(_body: Any, _state: Any) -> Any:
        yield Client(), {}

    monkeypatch.setattr(chat_routes, "_parse", parse)
    monkeypatch.setattr(chat_routes, "_client", client)
    monkeypatch.setattr(chat_routes, "require_write_token", lambda _request: None)
    monkeypatch.setattr(chat_routes, "serve_state", lambda _request: None)
    request = SimpleNamespace(path_params={
        "session_id": str(session_id), "suggestion_id": str(suggestion_id),
    })
    first = asyncio.run(chat_routes.growth_chat_decision(request))
    second = asyncio.run(chat_routes.growth_chat_decision(request))

    assert first.status_code == second.status_code == 200
    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert posted[0]["topic_id"] is not None
    assert posted[0]["operation_id"] is not None

    another = chat_routes._chat_decision_payload(account_id, uuid.uuid4(), "confirm")
    rejected = chat_routes._chat_decision_payload(account_id, suggestion_id, "reject")
    assert another["topic_id"] != posted[0]["topic_id"]
    assert rejected["topic_id"] is None
    assert rejected["operation_id"] != posted[0]["operation_id"]
