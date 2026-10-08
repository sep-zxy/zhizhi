"""The approved source payload is the only user message sent to a local provider."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest

from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.model_opportunities import (
    generate_opportunities,
    prepare_model_preview,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("model_name", "thinking_level"),
    [("local-test-model", None), ("qwen3:8b", "none")],
)
def test_approved_payload_uses_real_provider_adapter(
    tmp_path: Path, model_name: str, thinking_level: str | None,
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler interface
            assert self.path == "/api/chat"
            size = int(self.headers["content-length"])
            requests.append(json.loads(self.rfile.read(size)))
            body = json.dumps({
                "model": model_name,
                "message": {"content": '{"opportunities":[]}'},
                "prompt_eval_count": 42,
                "eval_count": 7,
                "done_reason": "stop",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", "local-http-1")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo = tmp_path / "source"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "growth@example.invalid")
        _git(repo, "config", "user.name", "Growth Acceptance")
        (repo / "main.py").write_text("answer = 1\n", encoding="utf-8")
        _git(repo, "add", "main.py")
        _git(repo, "commit", "-m", "初始代码")
        (repo / "main.py").write_text("answer = 2\n", encoding="utf-8")
        config_dir = repo / ".ahadiff"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text(
            '[providers.local]\nprovider_class = "ollama"\n'
            f'model_name = "{model_name}"\nbase_url = "http://127.0.0.1:{server.server_port}"\n'
            'api_key_env = ""\n'
            + (f'thinking_level = "{thinking_level}"\n' if thinking_level else ''),
            encoding="utf-8",
        )
        with GrowthLocalRepository(tmp_path / "ledger.sqlite") as ledger:
            project_id = ledger.create_project("示例", trace_id=str(uuid.uuid4()))
            binding_id = ledger.bind_repo(project_id, repo, trace_id=str(uuid.uuid4()))
            feature_id = ledger.start_feature(
                project_id, binding_id, "计算变更", "HEAD", trace_id=str(uuid.uuid4())
            )
            snapshot_id = ledger.capture_snapshot(
                feature_id, binding_id, trace_id=str(uuid.uuid4())
            )
            account_id = str(uuid.uuid4())
            other_account_id = str(uuid.uuid4())
            for item in (account_id, other_account_id):
                ledger.connection.execute(
                    "INSERT INTO sync_accounts(account_id, device_id) VALUES (?, ?)",
                    (item, str(uuid.uuid4())),
                )
            ledger.grant_cloud_sync(project_id, account_id, "http://127.0.0.1:12345")
            for item, title in ((account_id, "Web 认证与授权"),
                                (other_account_id, "其他账号的私有主题")):
                topic_id = str(uuid.uuid4())
                ledger.connection.execute(
                    "INSERT INTO sync_entities(account_id, entity_type, entity_id, "
                    "revision, payload_json) VALUES (?, 'topic', ?, 1, ?)",
                    (item, topic_id, json.dumps({"topic_id": topic_id,
                                                  "title": title, "status": "active"})),
                )
            source_id = str(ledger.connection.execute(
                "SELECT source_ref_id FROM source_refs WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0])
            preview = prepare_model_preview(
                ledger, snapshot_id, binding_id, "local", [source_id]
            )
            assert "Web 认证与授权" in preview.payload_text
            assert "其他账号的私有主题" not in preview.payload_text
            batch, response = generate_opportunities(repo, preview)
            assert batch.opportunities == []
            assert response.request_id == "local-http-1"
            assert response.input_tokens == 42 and response.output_tokens == 7
            assert len(requests) == 1
            assert requests[0]["model"] == model_name
            if thinking_level == "none":
                assert requests[0]["think"] is False
            assert requests[0]["messages"] == [
                {"role": "user", "content": preview.payload_text}
            ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
