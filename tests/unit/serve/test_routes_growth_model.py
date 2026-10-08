"""Review the exact selected payload and persist a provider-backed opportunity run."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any

from starlette.testclient import TestClient

from ahadiff.growth.local import GrowthLocalRepository
from ahadiff.growth.model_feedback import GrowthFeedbackService, prepare_feedback_preview
from ahadiff.llm.schemas import ProviderRequest, ProviderResponse
from ahadiff.serve.app import create_app
from ahadiff.serve.state import ServeState

if TYPE_CHECKING:
    from pathlib import Path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _terminal_task(client: TestClient, task_id: str, headers: dict[str, str]) -> str:
    for _ in range(100):
        response = client.get(f"/api/tasks/{task_id}", headers=headers)
        assert response.status_code == 200
        status = response.json()["status"]
        if status in {"completed", "failed", "cancelled"}:
            return str(status)
        time.sleep(0.1)
    raise AssertionError("analysis task did not finish")


def test_model_preview_and_live_opportunity_scope(tmp_path: Path, monkeypatch: Any) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "growth@example.invalid")
    _git(repo, "config", "user.name", "Growth Acceptance")
    (repo / "main.py").write_text("answer = 1\n", encoding="utf-8")
    (repo / "private.py").write_text("private = 1\n", encoding="utf-8")
    (repo / "removed.py").write_text("old = True\n", encoding="utf-8")
    _git(repo, "add", "main.py", "private.py", "removed.py")
    _git(repo, "commit", "-m", "初始代码")
    (repo / "main.py").write_text(
        'answer = 2\nAPI_KEY = "sk-1234567890abcdefghijklmnop"\n', encoding="utf-8"
    )
    (repo / "private.py").write_text("private = 2\n", encoding="utf-8")
    (repo / "removed.py").unlink()
    config_dir = repo / ".ahadiff"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[providers.local]\nprovider_class = "ollama"\n'
        'model_name = "fake-model"\nbase_url = "http://127.0.0.1:11434"\n'
        'api_key_env = ""\n',
        encoding="utf-8",
    )

    captured_requests: list[ProviderRequest] = []
    response_source_id = str(uuid.uuid4())
    feedback_source_id = str(uuid.uuid4())

    class FakeProvider:
        def __enter__(self) -> FakeProvider:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def generate(self, request: ProviderRequest) -> ProviderResponse:
            captured_requests.append(request)
            if request.prompt_name == "growth_feedback":
                output = {
                    "summary": "需要区分线程与等待",
                    "user_claims": ["用户认为执行器自动等待"],
                    "code_facts": ["代码显式等待结果"],
                    "general_principles": [],
                    "inferences": [],
                    "corrections": ["执行器不自动等待"],
                    "source_refs": [feedback_source_id],
                }
            else:
                output = {"opportunities": [{
                    "title": "理解执行器",
                    "reason": "代码改变了任务的执行方式",
                    "learning_goal": "解释执行器与等待",
                    "source_refs": [response_source_id],
                    "estimated_minutes": 5,
                    "topic_suggestions": ["异步执行"],
                    "uncertainties": [],
                }]}
            return ProviderResponse(
                content=json.dumps(output, ensure_ascii=False),
                model_id="fake-model", input_tokens=50, output_tokens=30,
                request_id=f"fake-{len(captured_requests)}",
            )

    monkeypatch.setattr(
        "ahadiff.growth.model_opportunities.make_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        "ahadiff.growth.model_feedback.make_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    state_dir = tmp_path / "workspace" / ".ahadiff"
    headers = {"origin": "http://localhost:8765", "X-AhaDiff-Token": "test-token"}
    with TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    ) as client:
        project = client.post(
            "/api/growth/local/projects", headers=headers, json={"name": "模型范围"}
        ).json()
        binding = client.post(
            f"/api/growth/local/projects/{project['project_id']}/bindings",
            headers=headers, json={"path": str(repo)},
        ).json()
        feature = client.post(
            f"/api/growth/local/projects/{project['project_id']}/features",
            headers=headers,
            json={"binding_id": binding["binding_id"], "label": "修正计算",
                  "base_ref": "HEAD"},
        ).json()
        snapshot = client.post(
            f"/api/growth/local/features/{feature['feature_id']}/snapshots",
            headers=headers,
            json={"binding_id": binding["binding_id"]},
        ).json()
        source_list = client.get(
            f"/api/growth/local/snapshots/{snapshot['snapshot_id']}/model-sources",
            headers=headers, params={"binding_id": binding["binding_id"]},
        )
        assert source_list.status_code == 200
        assert source_list.json()["provider_names"] == ["local"]
        sources = {
            item["relative_path"]: item["source_ref_id"]
            for item in source_list.json()["sources"]
        }
        assert set(sources) == {"main.py", "private.py", "removed.py"}
        removed_preview = client.post(
            f"/api/growth/local/snapshots/{snapshot['snapshot_id']}/model-preview",
            headers=headers,
            json={"binding_id": binding["binding_id"],
                  "provider_name": "local", "source_ref_ids": [sources["removed.py"]]},
        )
        assert removed_preview.status_code == 200
        assert "/dev/null" in removed_preview.json()["payload_text"]
        preview = client.post(
            f"/api/growth/local/snapshots/{snapshot['snapshot_id']}/model-preview",
            headers=headers,
            json={"binding_id": binding["binding_id"],
                  "provider_name": "local", "source_ref_ids": [sources["main.py"]]},
        )
        assert preview.status_code == 200
        reviewed = preview.json()
        assert "main.py" in reviewed["payload_text"]
        assert "private.py" not in reviewed["payload_text"]
        assert "sk-1234567890abcdefghijklmnop" not in reviewed["payload_text"]
        assert reviewed["provider_host"] == "http://127.0.0.1:11434"
        assert reviewed["source_ref_ids"] == [sources["main.py"]]

        path = f"/api/growth/local/snapshots/{snapshot['snapshot_id']}/analysis"
        body = {
            "binding_id": binding["binding_id"],
            "request_id": str(uuid.uuid4()),
            "mode": "live", "provider_name": "local",
            "source_ref_ids": [sources["main.py"]],
            "approved_payload_hash": reviewed["approval_hash"],
        }
        assert client.post(path, headers=headers, json=body).status_code == 422
        assert client.post(
            path, headers=headers, json=body | {
                "approved": True, "approved_payload_hash": "0" * 64,
            }
        ).status_code == 409
        assert client.post(
            path, headers=headers, json=body | {
                "approved": True, "source_ref_ids": [sources["private.py"]],
            }
        ).status_code == 409
        assert captured_requests == []

        blocked = client.post(path, headers=headers, json=body | {"approved": True})
        assert blocked.status_code == 409
        assert captured_requests == []
        policy = client.put(
            f"/api/growth/local/projects/{project['project_id']}/policy",
            headers=headers,
            json={"local_processing": True, "model_allowed": True,
                  "cloud_allowed": False},
        )
        assert policy.status_code == 200
        assert policy.json()["model_allowed"] is True
        assert policy.json()["cloud_allowed"] is False

        submitted = client.post(path, headers=headers, json=body | {"approved": True})
        assert submitted.status_code == 202
        assert submitted.json()["analysis_id"]
        with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
            queued = ledger.connection.execute(
                "SELECT status FROM analysis_runs WHERE analysis_id=?",
                (submitted.json()["analysis_id"],),
            ).fetchone()
            assert queued is not None
        assert _terminal_task(client, submitted.json()["task_id"], headers) == "failed"
        saved = client.get("/api/growth/local", headers=headers).json()
        failed = next(item for item in saved["analysis_runs"] if item["mode"] == "live")
        assert failed["status"] == "failed"
        assert saved["opportunities"] == []
        assert len(captured_requests) == 1
        assert captured_requests[0].effective_payload() == reviewed["payload_text"]

        response_source_id = sources["main.py"]
        retried = client.post(
            f"/api/growth/local/analyses/{failed['analysis_id']}/retry",
            headers=headers, json={"binding_id": binding["binding_id"]},
        )
        assert retried.status_code == 202
        assert _terminal_task(client, retried.json()["task_id"], headers) == "completed"
        after = client.get("/api/growth/local", headers=headers).json()
        succeeded = next(
            item for item in after["analysis_runs"]
            if item["analysis_id"] == failed["analysis_id"]
        )
        assert succeeded["status"] == "succeeded"
        assert succeeded["upstream_run_id"]
        assert len(after["opportunities"]) == 1
        assert after["opportunities"][0]["title"] == "理解执行器"
        with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
            attempts = ledger.connection.execute(
                "SELECT status FROM analysis_attempts WHERE analysis_id=? ORDER BY attempt_no",
                (failed["analysis_id"],),
            ).fetchall()
            assert [item["status"] for item in attempts] == ["failed", "succeeded"]
            batch = ledger.connection.execute(
                "SELECT origin, provider_request_id FROM opportunity_batches WHERE analysis_id=?",
                (failed["analysis_id"],),
            ).fetchone()
            assert batch["origin"] == "live"
            assert batch["provider_request_id"] == "fake-2"
            model_response = ledger.connection.execute(
                "SELECT input_tokens, output_tokens, response_text "
                "FROM analysis_model_responses WHERE analysis_id=?",
                (failed["analysis_id"],),
            ).fetchone()
            assert model_response["input_tokens"] == 50
            assert model_response["output_tokens"] == 30
            assert "理解执行器" in model_response["response_text"]

        reused = client.post(
            path, headers=headers,
            json=body | {"approved": True, "request_id": str(uuid.uuid4())},
        )
        assert reused.status_code == 200
        assert reused.json()["reused"] is True
        assert reused.json()["analysis_id"] == failed["analysis_id"]
        assert len(captured_requests) == 2

        reanalysis = client.post(
            path, headers=headers,
            json=body | {"approved": True, "request_id": str(uuid.uuid4()),
                         "reanalysis": True},
        )
        assert reanalysis.status_code == 202
        assert reanalysis.json()["analysis_id"] != failed["analysis_id"]
        assert _terminal_task(client, reanalysis.json()["task_id"], headers) == "completed"
        versions = client.get("/api/growth/local", headers=headers).json()
        new_run = next(
            item for item in versions["analysis_runs"]
            if item["analysis_id"] == reanalysis.json()["analysis_id"]
        )
        assert new_run["supersedes_analysis_id"] == failed["analysis_id"]
        assert len(captured_requests) == 3

    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        queued_id, created = ledger.queue_live_opportunities(
            snapshot["snapshot_id"], binding["binding_id"], str(uuid.uuid4()),
            provider_name="local", source_ref_ids=[sources["main.py"]],
            approved_payload_hash=reviewed["approval_hash"],
            trace_id=str(uuid.uuid4()), reanalysis=True,
        )
        assert created
    with TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    ) as recovered_client:
        recovered = recovered_client.get("/api/growth/local", headers=headers).json()
        interrupted = next(
            item for item in recovered["analysis_runs"]
            if item["analysis_id"] == queued_id
        )
        assert interrupted["status"] == "failed"
        retried_after_restart = recovered_client.post(
            f"/api/growth/local/analyses/{queued_id}/retry",
            headers=headers, json={"binding_id": binding["binding_id"]},
        )
        assert retried_after_restart.status_code == 202
        assert _terminal_task(
            recovered_client, retried_after_restart.json()["task_id"], headers,
        ) == "completed"
        assert len(captured_requests) == 4

        local = recovered_client.get("/api/growth/local", headers=headers).json()
        opportunity = next(
            item for item in local["opportunities"]
            if item["analysis_id"] == failed["analysis_id"]
        )
        proposal = next(
            item for item in local["topic_proposals"]
            if item["opportunity_id"] == opportunity["opportunity_id"]
        )
        task_path = f"/api/growth/local/opportunities/{opportunity['opportunity_id']}/tasks"
        assert recovered_client.post(
            task_path, headers=headers, json={"topic_id": str(uuid.uuid4())},
        ).status_code == 409
        decision = recovered_client.post(
            f"/api/growth/local/topic-proposals/{proposal['proposal_id']}/decision",
            headers=headers, json={"decision": "confirm"},
        )
        assert decision.status_code == 200
        assert decision.json()["topic_id"]
        assert recovered_client.post(
            f"/api/growth/local/topic-proposals/{proposal['proposal_id']}/decision",
            headers=headers, json={"decision": "confirm"},
        ).json()["topic_id"] == decision.json()["topic_id"]
        task = recovered_client.post(
            task_path, headers=headers, json={"topic_id": decision.json()["topic_id"]},
        )
        assert task.status_code == 201
        task_id = task.json()["task_id"]
        chain_path = f"/api/growth/local/tasks/{task_id}"
        before_answer = recovered_client.get(chain_path, headers=headers).json()
        assert "解释执行器与等待" in before_answer["task"]["question"]
        assert before_answer["notes"] == []
        hint_request = {"request_id": str(uuid.uuid4())}
        hint = recovered_client.post(
            f"{chain_path}/hints", headers=headers, json=hint_request,
        )
        assert hint.status_code == 201
        assert hint.json()["level"] == 1
        assert "main.py" in hint.json()["hint_text"]
        assert recovered_client.post(
            f"{chain_path}/hints", headers=headers, json=hint_request,
        ).json()["hint_id"] == hint.json()["hint_id"]
        assert recovered_client.post(
            f"{chain_path}/answers", headers=headers,
            json={"answer_text": "不能隐去已查看的提示"},
        ).status_code == 409
        a1 = recovered_client.post(
            f"{chain_path}/answers", headers=headers,
            json={"answer_text": "第一次理解有误", "hint_level": 1},
        )
        assert a1.status_code == 201
        feedback_path = f"/api/growth/local/attempts/{a1.json()['attempt_id']}"
        feedback_preview = recovered_client.post(
            f"{feedback_path}/feedback-preview", headers=headers,
            json={"binding_id": binding["binding_id"]},
        )
        assert feedback_preview.status_code == 200
        feedback_reviewed = feedback_preview.json()
        assert "第一次理解有误" in feedback_reviewed["payload_text"]
        assert "sk-1234567890abcdefghijklmnop" not in feedback_reviewed["payload_text"]
        feedback_body = {
            "binding_id": binding["binding_id"],
            "request_id": str(uuid.uuid4()),
            "approved_payload_hash": feedback_reviewed["approval_hash"],
            "approved": True,
        }
        assert recovered_client.post(
            f"{feedback_path}/feedback", headers=headers,
            json=feedback_body | {"approved": False},
        ).status_code == 422
        first_feedback = recovered_client.post(
            f"{feedback_path}/feedback", headers=headers, json=feedback_body,
        )
        assert first_feedback.status_code == 202
        assert _terminal_task(
            recovered_client, first_feedback.json()["task_id"], headers,
        ) == "failed"
        failed_chain = recovered_client.get(chain_path, headers=headers).json()
        assert failed_chain["attempts"][0]["answer_text"] == "第一次理解有误"
        assert failed_chain["attempts"][0]["status"] == "feedback_failed"
        feedback_source_id = sources["main.py"]
        retried_feedback = recovered_client.post(
            f"{feedback_path}/feedback", headers=headers,
            json=feedback_body | {"request_id": str(uuid.uuid4()), "retry": True},
        )
        assert retried_feedback.status_code == 202
        assert _terminal_task(
            recovered_client, retried_feedback.json()["task_id"], headers,
        ) == "completed"
        a2 = recovered_client.post(
            f"{chain_path}/answers", headers=headers,
            json={"answer_text": "修正后理解", "parent_attempt_id": a1.json()["attempt_id"],
                  "hint_level": 1},
        )
        assert a2.status_code == 201
        note = recovered_client.post(
            f"{chain_path}/notes", headers=headers,
            json={"content_text": "我亲手写的笔记"},
        )
        assert note.status_code == 201
        assert recovered_client.post(
            f"{chain_path}/progress", headers=headers,
            json={"target": "completed"},
        ).status_code == 200
        chain = recovered_client.get(chain_path, headers=headers).json()
        assert len(chain["hints"]) == 1
        assert [item["hint_level"] for item in chain["attempts"]] == [1, 1]
        assert [item["answer_text"] for item in chain["attempts"]] == [
            "第一次理解有误", "修正后理解",
        ]
        assert chain["attempts"][1]["parent_attempt_id"] == a1.json()["attempt_id"]
        assert chain["notes"][0]["content_text"] == "我亲手写的笔记"
        assert chain["notes"][0]["author"] == "user"
        assert chain["task"]["progress"] == "completed"
        assert chain["attempts"][0]["status"] == "feedback_ready"
        assert chain["attempts"][0]["feedback_origin"] == "live"
        with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
            model_attempts = ledger.connection.execute(
                "SELECT status FROM feedback_model_attempts WHERE attempt_id=? "
                "ORDER BY attempt_no", (a1.json()["attempt_id"],),
            ).fetchall()
            assert [item["status"] for item in model_attempts] == ["failed", "succeeded"]

    with GrowthLocalRepository(state_dir / "growth.sqlite") as ledger:
        pending_preview = prepare_feedback_preview(
            ledger, a2.json()["attempt_id"], binding["binding_id"],
        )
        created, status = GrowthFeedbackService(ledger).queue(
            a2.json()["attempt_id"], binding["binding_id"], str(uuid.uuid4()),
            pending_preview.approval_hash, retry=False, trace_id=str(uuid.uuid4()),
        )
        assert created and status == "queued"
    with TestClient(
        create_app(ServeState(state_dir=state_dir, token="test-token")),
        base_url="http://localhost:8765",
    ) as recovered_feedback_client:
        interrupted_chain = recovered_feedback_client.get(chain_path, headers=headers).json()
        assert interrupted_chain["attempts"][1]["status"] == "feedback_failed"
        resumed = recovered_feedback_client.post(
            f"/api/growth/local/attempts/{a2.json()['attempt_id']}/feedback",
            headers=headers,
            json={"binding_id": binding["binding_id"],
                  "request_id": str(uuid.uuid4()),
                  "approved_payload_hash": pending_preview.approval_hash,
                  "approved": True, "retry": True},
        )
        assert resumed.status_code == 202
        assert _terminal_task(
            recovered_feedback_client, resumed.json()["task_id"], headers,
        ) == "completed"
        final_chain = recovered_feedback_client.get(chain_path, headers=headers).json()
        assert final_chain["attempts"][1]["answer_text"] == "修正后理解"
        assert final_chain["attempts"][1]["status"] == "feedback_ready"
