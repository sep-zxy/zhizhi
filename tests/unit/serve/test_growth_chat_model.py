"""A chat model reply is approved once, stored before upload, and never regenerated on retry."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.testclient import TestClient

from ahadiff.growth import chat_model
from ahadiff.growth.chat_model import ChatModelPreview, ChatReplyOutput
from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.sync import GrowthSyncStore
from ahadiff.llm.schemas import ProviderResponse
from ahadiff.serve import ServeState, create_app
from ahadiff.serve import routes_growth_chat as chat_routes

if TYPE_CHECKING:
    from pathlib import Path


def test_chat_preview_redacts_and_requires_latest_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        base_url="https://model.example/v1", model_name="test-model",
        thinking_level=None,
        model_dump=lambda **_kwargs: {"base_url": "https://model.example/v1",
                                      "model_name": "test-model"},
    )
    monkeypatch.setattr(chat_model, "_provider", lambda *_args, **_kwargs:
                        (config, None, None))
    secret = "ghp_" + "a" * 36
    chat = {"session": {"session_id": str(uuid.uuid4())},
            "messages": [{"role": "user", "content_text":
                          f"请说明身份与权限。令牌是 {secret}"}]}
    preview = chat_model.prepare_chat_preview(tmp_path, "fixture", chat)
    assert secret not in preview.payload_text
    assert "身份与权限" in preview.payload_text
    assert len(preview.approval_hash) == 64
    chat["messages"].append({"role": "assistant", "content_text": "已回复"})
    with pytest.raises(ValueError, match="最新消息必须来自用户"):
        chat_model.prepare_chat_preview(tmp_path, "fixture", chat)


def test_chat_preview_labels_historical_answer_and_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        base_url="https://model.example/v1", model_name="test-model",
        thinking_level=None,
        model_dump=lambda **_kwargs: {"base_url": "https://model.example/v1",
                                      "model_name": "test-model"},
    )
    monkeypatch.setattr(chat_model, "_provider", lambda *_args, **_kwargs:
                        (config, None, None))
    chat = {"session": {"session_id": str(uuid.uuid4())},
            "messages": [{"role": "user", "content_text": "我以前怎么理解 join？"}]}
    memory = {"memory_status": "current", "notes": [], "attempts": [{
        "attempt_id": str(uuid.uuid4()), "parent_attempt_id": None,
        "question": "join 会等待吗？", "answer_text": "join 不会等待",
        "feedback": {"corrections": ["join 会等待结果"]}, "source_refs": [],
    }]}
    preview = chat_model.prepare_chat_preview(tmp_path, "fixture", chat, memory)
    assert '"historical":true' in preview.payload_text
    assert "join 不会等待" in preview.payload_text
    assert "join 会等待结果" in preview.payload_text
    assert "历史回答不能直接作为当前事实" in preview.payload_text


def test_chat_preview_keeps_latest_message_when_memory_is_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        base_url="https://model.example/v1", model_name="test-model",
        thinking_level=None,
        model_dump=lambda **_kwargs: {"base_url": "https://model.example/v1",
                                      "model_name": "test-model"},
    )
    monkeypatch.setattr(chat_model, "_provider", lambda *_args, **_kwargs:
                        (config, None, None))
    chat = {"session": {"session_id": str(uuid.uuid4())},
            "messages": [{"role": "user", "content_text": "旧对话" + "甲" * 4500}]
            * 19 + [{"role": "user", "content_text": "最新问题：join 会等待吗？"}]}
    memory = {"memory_status": "current", "topics": [],
              "notes": [{"note_id": str(uuid.uuid4()),
                         "content_text": "学习笔记" + "乙" * 100000}],
              "attempts": []}
    preview = chat_model.prepare_chat_preview(tmp_path, "fixture", chat, memory)
    assert len(preview.payload_text) <= 48000
    assert "最新问题：join 会等待吗？" in preview.payload_text
    assert '"memory_truncated":true' in preview.payload_text
    assert '"conversation_truncated":true' in preview.payload_text


def test_chat_generation_uses_approved_payload_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        base_url="https://model.example/v1", model_name="test-model",
        thinking_level=None,
        model_dump=lambda **_kwargs: {"base_url": "https://model.example/v1",
                                      "model_name": "test-model"},
    )
    monkeypatch.setattr(chat_model, "_provider", lambda *_args, **_kwargs:
                        (config, "test-key", None))
    chat = {"session": {"session_id": str(uuid.uuid4())},
            "messages": [{"role": "user", "content_text": "身份和权限有何区别？"}]}
    preview = chat_model.prepare_chat_preview(tmp_path, "fixture", chat)
    seen: list[Any] = []

    class FakeProvider:
        def __enter__(self) -> FakeProvider:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generate(self, request: Any) -> ProviderResponse:
            seen.append(request)
            return ProviderResponse(
                content='{"reply":"身份先识别主体，权限再控制操作。","suggestion":null}',
                model_id="test-model", input_tokens=10, output_tokens=20,
                request_id="provider-123",
            )

    monkeypatch.setattr(chat_model, "make_provider", lambda *_args, **_kwargs:
                        FakeProvider())
    reply, response = chat_model.generate_chat_reply(tmp_path, preview)
    assert reply.reply == "身份先识别主体，权限再控制操作。"
    assert response.request_id == "provider-123"
    assert len(seen) == 1
    assert seen[0].payload_text == preview.payload_text
    assert seen[0].redacted_payload_text == preview.payload_text
    assert seen[0].source_ref == chat["session"]["session_id"]
    assert '"suggestion":null' in seen[0].payload_text
    assert '"title"' in seen[0].payload_text
    assert '"reason"' in seen[0].payload_text
    assert "作用域不足或资源归属不符属于授权" in seen[0].payload_text


def test_chat_generation_rejects_provider_json_with_wrong_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        base_url="https://model.example/v1", model_name="test-model",
        thinking_level=None,
        model_dump=lambda **_kwargs: {"base_url": "https://model.example/v1",
                                      "model_name": "test-model"},
    )
    monkeypatch.setattr(chat_model, "_provider", lambda *_args, **_kwargs:
                        (config, "test-key", None))
    chat = {"session": {"session_id": str(uuid.uuid4())},
            "messages": [{"role": "user", "content_text": "我想学习认证和授权"}]}
    preview = chat_model.prepare_chat_preview(tmp_path, "fixture", chat)

    class FakeProvider:
        def __enter__(self) -> FakeProvider:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def generate(self, _request: Any) -> ProviderResponse:
            return ProviderResponse(
                content='{"reply":"继续学习","suggestion":{"topic":"权限"}}',
                model_id="test-model", input_tokens=10, output_tokens=20,
            )

    monkeypatch.setattr(chat_model, "make_provider", lambda *_args, **_kwargs:
                        FakeProvider())
    with pytest.raises(chat_model.ChatOutputValidationError):
        chat_model.generate_chat_reply(tmp_path, preview)


def test_chat_model_approval_and_durable_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, device_id, session_id = (uuid.uuid4() for _ in range(3))
    state = ServeState(state_dir=tmp_path / ".ahadiff", token="local-token")
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        GrowthSyncStore(ledger, account_id, device_id,
                        cloud_origin="http://127.0.0.1:9001")
        ledger.connection.execute(
            "UPDATE sync_accounts SET bootstrapped=1 WHERE account_id=?",
            (str(account_id),),
        )
        ledger.connection.commit()

    async def fake_preview(_request: Any, _body: Any) -> ChatModelPreview:
        return ChatModelPreview(
            session_id=str(session_id), project_id=None,
            payload_text="approved user text", approval_hash="a" * 64,
            provider_name="fixture", provider_config_hash="b" * 64,
            provider_host="https://model.example", model_name="fixture-model",
        )

    calls = 0

    def fake_generate(_workspace: Path, _preview: ChatModelPreview
                      ) -> tuple[ChatReplyOutput, ProviderResponse]:
        nonlocal calls
        calls += 1
        content = json.dumps({"reply": "继续分析权限边界", "suggestion": {
            "title": "权限边界", "reason": "连续两条消息涉及权限设计",
        }}, ensure_ascii=False)
        return ChatReplyOutput.model_validate_json(content), ProviderResponse(
            content=content, model_id="fixture-model", input_tokens=24,
            output_tokens=16, request_id="upstream-1",
        )

    sync_calls = 0

    async def fake_sync(ledger: GrowthLocalRepository, _client: httpx.AsyncClient,
                        _token: str) -> dict[str, str]:
        nonlocal sync_calls
        sync_calls += 1
        pending = ledger.connection.execute(
            "SELECT operation_id FROM sync_outbox WHERE account_id=? AND state='pending'",
            (str(account_id),),
        ).fetchone()
        assert pending is not None
        if sync_calls == 1:
            raise httpx.ConnectError("offline")
        ledger.connection.execute(
            "UPDATE sync_outbox SET state='acked' WHERE account_id=?",
            (str(account_id),),
        )
        ledger.connection.commit()
        return {"account_id": str(account_id)}

    monkeypatch.setattr(chat_routes, "_preview", fake_preview)
    monkeypatch.setattr(chat_routes, "generate_chat_reply", fake_generate)
    monkeypatch.setattr(chat_routes, "connect_and_sync", fake_sync)

    client = TestClient(create_app(state), base_url="http://localhost:8765")
    headers = {"Origin": "http://localhost:8765", "X-AhaDiff-Token": "local-token"}
    path = f"/api/growth/local/cloud/chats/{session_id}/generate"
    body = {
        "cloud_url": "http://127.0.0.1:9001", "access_token": "cloud-token",
        "account_id": str(account_id), "provider_name": "fixture",
        "request_id": str(uuid.uuid4()), "message_id": str(uuid.uuid4()),
        "approved_payload_hash": "a" * 64, "approved": True,
    }
    assert client.post(path, headers=headers, json=body | {"approved": False}).status_code == 422
    assert client.post(path, headers=headers, json=body | {
        "approved_payload_hash": "c" * 64,
    }).status_code == 409
    first = client.post(path, headers=headers, json=body)
    assert first.status_code == 202
    assert first.json()["sync_state"] == "pending"
    assert calls == 1
    with GrowthLocalRepository(state.state_dir / "growth.sqlite") as ledger:
        row = ledger.connection.execute(
            "SELECT status, response_json FROM local_chat_model_requests "
            "WHERE account_id=? AND request_id=?",
            (str(account_id), body["request_id"]),
        ).fetchone()
        assert row["status"] == "ready"
        assert json.loads(row["response_json"])["suggestion"]["title"] == "权限边界"
    retry = client.post(path, headers=headers, json=body)
    assert retry.status_code == 200 and retry.json()["sync_state"] == "acked"
    assert calls == 1
    assert client.post(path, headers=headers, json=body | {
        "message_id": str(uuid.uuid4()),
    }).status_code == 409
    assert calls == 1
