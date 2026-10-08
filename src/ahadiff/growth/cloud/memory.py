"""Recoverable, version-gated learning source projection into private ReMe."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

from .db import connect, enqueue_projection

if TYPE_CHECKING:
    import psycopg


_WORKER_LOCK = 782136410198


def reme_note_path(account_id: uuid.UUID, note_id: uuid.UUID) -> str:
    return f"digest/growth/{account_id}/{note_id}.md"


def reme_attempt_path(account_id: uuid.UUID, attempt_id: uuid.UUID) -> str:
    return f"digest/growth/{account_id}/attempts/{attempt_id}.md"


def _reme_path(job: ProjectionJob) -> str:
    if job.source_type == "note":
        return reme_note_path(job.account_id, job.source_id)
    if job.source_type == "attempt":
        return reme_attempt_path(job.account_id, job.source_id)
    raise ValueError(f"不支持的记忆来源: {job.source_type}")


@dataclass(frozen=True)
class ProjectionJob:
    account_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    source_version: int
    projection_key: str
    action: str
    attempt_count: int


class ReMeAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def write_note(
        self, job: ProjectionJob, content: str, source_refs: list[str]
    ) -> dict[str, Any]:
        return await self.write_source(job, content, source_refs)

    async def write_source(
        self, job: ProjectionJob, content: str, source_refs: list[str]
    ) -> dict[str, Any]:
        path = _reme_path(job)
        is_attempt = job.source_type == "attempt"
        response = await self.client.post("/write", json={
            "path": path,
            "name": f"{'历史学习回答' if is_attempt else '成长笔记'} {job.source_id}",
            "description": (
                "历史回答及反馈；错误回答仅供回顾，业务主库版本为权威来源"
                if is_attempt else "用户亲写的工程学习笔记；业务主库版本为权威来源"
            ),
            "content": content,
            "metadata": {
                "growth_account_id": str(job.account_id),
                "growth_source_type": job.source_type,
                "growth_source_id": str(job.source_id),
                "growth_source_version": job.source_version,
                "growth_projection_key": job.projection_key,
                "growth_dependencies": [
                    {"source_id": str(job.source_id), "version": job.source_version}
                ],
                "growth_source_refs": source_refs,
            },
        })
        response.raise_for_status()
        return _job_result(response, "write")

    async def delete_note(self, job: ProjectionJob) -> dict[str, Any]:
        path = _reme_path(job)
        response = await self.client.post(
            "/delete", json={"path": path}
        )
        # ReMe reports a missing *file* with HTTP 200 and metadata.error.
        # HTTP 404 means the delete Job endpoint is not configured at all;
        # treating that as success would leave the real file and index behind.
        response.raise_for_status()
        result = response.json()
        if isinstance(result, dict) and result.get("success") is False:
            metadata = result.get("metadata")
            if (isinstance(metadata, dict) and metadata.get("error") == "not found"
                    and metadata.get("path") == path):
                return {"already_absent": True}
        return _job_result(response, "delete")

    async def search_paths(self, query: str, *, limit: int = 10) -> list[str]:
        response = await self.client.post("/search", json={"query": query, "limit": limit})
        response.raise_for_status()
        result = _job_result(response, "search")
        metadata_raw: object = result.get("metadata")
        if not isinstance(metadata_raw, dict):
            raise RuntimeError("ReMe search 缺少结构化结果")
        metadata = cast("dict[str, Any]", metadata_raw)
        results_raw: object = metadata.get("results")
        if not isinstance(results_raw, list):
            raise RuntimeError("ReMe search 缺少结构化结果")
        paths: list[str] = []
        for item_raw in cast("list[object]", results_raw):
            if isinstance(item_raw, dict):
                item = cast("dict[str, Any]", item_raw)
                path: object = item.get("path")
                if isinstance(path, str):
                    paths.append(path)
        return paths


def _job_result(response: httpx.Response, action: str) -> dict[str, Any]:
    raw: object = response.json()
    if not isinstance(raw, dict):
        return {"result": raw}
    result = cast("dict[str, Any]", raw)
    if result.get("success") is False or result.get("error"):
        raise RuntimeError(f"ReMe {action} 返回业务错误")
    return result


def bootstrap_note_jobs(dsn: str) -> int:
    """Enqueue pre-existing current notes after a migration or worker outage."""
    count = 0
    with connect(dsn) as conn:
        for note in conn.execute(
            "SELECT account_id, note_id, revision, deleted_at FROM growth_notes"
        ).fetchall():
            enqueue_projection(
                conn, account_id=note["account_id"], source_type="note",
                source_id=note["note_id"], source_version=int(note["revision"]),
                action="delete" if note["deleted_at"] else "upsert",
            )
            count += 1
    return count


def bootstrap_attempt_jobs(dsn: str) -> int:
    """Enqueue attempts created before the attempt projector was enabled."""
    count = 0
    with connect(dsn) as conn:
        for attempt in conn.execute(
            "SELECT account_id, attempt_id, status FROM growth_learning_attempts"
        ).fetchall():
            enqueue_projection(
                conn, account_id=attempt["account_id"], source_type="attempt",
                source_id=attempt["attempt_id"],
                source_version=2 if attempt["status"] == "feedback_ready" else 1,
                action="upsert",
            )
            count += 1
    return count


def current_notes_for_paths(
    dsn: str, account_id: uuid.UUID, paths: list[str]
) -> list[dict[str, Any]]:
    """Ignore foreign, deleted, and superseded ReMe hits before returning text."""
    notes: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    prefix = f"digest/growth/{account_id}/"
    with connect(dsn) as conn:
        for path in paths:
            if not path.startswith(prefix) or not path.endswith(".md"):
                continue
            try:
                note_id = uuid.UUID(path[len(prefix):-3])
            except ValueError:
                continue
            if note_id in seen or path != reme_note_path(account_id, note_id):
                continue
            seen.add(note_id)
            row = conn.execute(
                "SELECT n.note_id, n.revision, n.task_id, n.topic_id, n.content_text, "
                "n.content_hash, n.source_refs FROM growth_notes AS n "
                "JOIN growth_memory_current AS m ON m.account_id=n.account_id "
                "AND m.source_type='note' AND m.source_id=n.note_id "
                "AND m.source_version=n.revision AND m.deleted_at IS NULL "
                "WHERE n.account_id=%s AND n.note_id=%s AND n.deleted_at IS NULL",
                (account_id, note_id),
            ).fetchone()
            if row is not None:
                notes.append({key: str(value) if isinstance(value, uuid.UUID) else value
                              for key, value in row.items()})
    return notes


def current_attempts_for_paths(
    dsn: str, account_id: uuid.UUID, paths: list[str]
) -> list[dict[str, Any]]:
    """Return current, account-owned attempts with explicit historical labels."""
    attempts: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    prefix = f"digest/growth/{account_id}/attempts/"
    with connect(dsn) as conn:
        for path in paths:
            if not path.startswith(prefix) or not path.endswith(".md"):
                continue
            try:
                attempt_id = uuid.UUID(path[len(prefix):-3])
            except ValueError:
                continue
            if attempt_id in seen or path != reme_attempt_path(account_id, attempt_id):
                continue
            seen.add(attempt_id)
            row = conn.execute(
                "SELECT a.attempt_id, a.task_id, t.topic_id, a.parent_attempt_id, "
                "t.question, a.answer_text, a.answer_hash, a.status, a.feedback, "
                "a.feedback_origin, t.source_refs FROM growth_learning_attempts a "
                "JOIN growth_tasks t ON t.account_id=a.account_id AND t.task_id=a.task_id "
                "JOIN growth_memory_current m ON m.account_id=a.account_id "
                "AND m.source_type='attempt' AND m.source_id=a.attempt_id "
                "AND m.source_version=CASE WHEN a.status='feedback_ready' THEN 2 ELSE 1 END "
                "AND m.deleted_at IS NULL "
                "WHERE a.account_id=%s AND a.attempt_id=%s AND t.deleted_at IS NULL",
                (account_id, attempt_id),
            ).fetchone()
            if row is not None:
                item = {key: str(value) if isinstance(value, uuid.UUID) else value
                        for key, value in row.items()}
                item["historical"] = True
                attempts.append(item)
    return attempts


def linked_objective_answers_for_notes(
    dsn: str, account_id: uuid.UUID, notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach historical card choices to a retrieved, current note's task."""
    task_ids = {note.get("task_id") for note in notes if note.get("task_id")}
    if not task_ids:
        return []
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    with connect(dsn) as conn:
        for task_id in sorted(task_ids):
            row = conn.execute(
                "SELECT bundle FROM growth_knowledge_bundles "
                "WHERE account_id=%s AND card_id=%s",
                (account_id, uuid.UUID(str(task_id))),
            ).fetchone()
            if row is None:
                continue
            tables = row["bundle"].get("tables", {})
            questions = {
                item["question_id"]: item
                for item in tables.get("card_objective_questions", [])
            }
            read_explanations = {
                (item["question_id"], item["question_version"])
                for item in tables.get("card_explanation_reads", [])
            }
            for answer in tables.get("card_objective_answers", []):
                question = questions.get(answer.get("question_id"))
                if (question is None or answer.get("question_version") != question.get("version")):
                    continue
                if (answer["question_id"], answer["question_version"]) not in read_explanations:
                    continue
                options = json.loads(question["options_json"])
                chosen = next((option for option in options
                               if option.get("id") == answer["option_id"]), None)
                if chosen is None:
                    continue
                explanations = json.loads(question["explanations_json"])
                correct = bool(answer["correct"])
                ranked.append((int(question.get("card_version", 0)),
                               str(answer.get("created_at", "")), {
                    "attempt_id": answer["answer_id"],
                    "task_id": str(task_id),
                    "parent_attempt_id": None,
                    "question": question["stem"],
                    "answer_text": f"{answer['option_id']}. {chosen['text']}",
                    "feedback": {
                        "summary": "当时客观作答正确" if correct else "当时客观作答错误",
                        "corrections": [
                            explanations.get(answer["option_id"], ""),
                            question["reasoning"],
                        ],
                    },
                    "source_refs": json.loads(question["source_refs_json"]),
                    "source_type": "objective_answer",
                    "retrieval_basis": "retrieved_note_same_task",
                    "historical": True,
                }))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def _claim(conn: psycopg.Connection[Any]) -> ProjectionJob | None:
    with conn.transaction():
        row = conn.execute(
            "SELECT account_id, source_type, source_id, source_version, projection_key, "
            "action, attempt_count FROM growth_memory_jobs "
            "WHERE (state IN ('pending', 'failed') AND next_retry_at<=now()) "
            "OR (state='running' AND lease_until<now()) "
            "ORDER BY next_retry_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE growth_memory_jobs SET state='running', attempt_count=attempt_count+1, "
            "lease_until=now()+interval '90 seconds', updated_at=now() "
            "WHERE account_id=%s AND source_type=%s AND source_id=%s AND source_version=%s",
            (row["account_id"], row["source_type"], row["source_id"],
             row["source_version"]),
        )
        return ProjectionJob(
            account_id=row["account_id"], source_type=row["source_type"],
            source_id=row["source_id"], source_version=int(row["source_version"]),
            projection_key=row["projection_key"].strip(), action=row["action"],
            attempt_count=int(row["attempt_count"]) + 1,
        )


def _current_source(conn: psycopg.Connection[Any], job: ProjectionJob) -> dict[str, Any] | None:
    if job.source_type == "note":
        return conn.execute(
            "SELECT revision AS version, content_text, source_refs, deleted_at "
            "FROM growth_notes WHERE account_id=%s AND note_id=%s",
            (job.account_id, job.source_id),
        ).fetchone()
    if job.source_type == "attempt":
        row = conn.execute(
            "SELECT a.answer_text, a.parent_attempt_id, a.status, a.feedback, "
            "t.question, t.source_refs, t.deleted_at FROM growth_learning_attempts a "
            "JOIN growth_tasks t ON t.account_id=a.account_id AND t.task_id=a.task_id "
            "WHERE a.account_id=%s AND a.attempt_id=%s",
            (job.account_id, job.source_id),
        ).fetchone()
        if row is None:
            return None
        feedback = row["feedback"] if isinstance(row["feedback"], dict) else {}
        lines = [
            "历史学习回答（仅用于回顾，不代表当前认识；错误回答不能当作事实）",
            f"问题：{row['question']}",
            f"当时回答：{row['answer_text']}",
            f"反馈状态：{'已反馈' if row['status'] == 'feedback_ready' else '待反馈'}",
        ]
        if row["parent_attempt_id"]:
            lines.append(f"修正自历史回答：{row['parent_attempt_id']}")
        if feedback:
            lines.append(f"反馈摘要：{feedback.get('summary', '')}")
            for correction in feedback.get("corrections", []):
                lines.append(f"纠错：{correction}")
        return {
            "version": 2 if row["status"] == "feedback_ready" else 1,
            "content_text": "\n".join(lines), "source_refs": row["source_refs"],
            "deleted_at": row["deleted_at"],
        }
    raise ValueError(f"不支持的记忆来源: {job.source_type}")


def _finish(conn: psycopg.Connection[Any], job: ProjectionJob, state: str,
            *, error: str | None = None) -> None:
    with conn.transaction():
        if state == "succeeded":
            current = _current_source(conn, job)
            if (
                current is None
                or int(current["version"]) != job.source_version
                or (job.action == "upsert" and current["deleted_at"] is not None)
                or (job.action == "delete" and current["deleted_at"] is None)
            ):
                state = "skipped"
            else:
                conn.execute(
                    "INSERT INTO growth_memory_current "
                    "(account_id, source_type, source_id, source_version, "
                    "projection_key, reme_path, deleted_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(account_id, source_type, source_id) DO UPDATE SET "
                    "source_version=excluded.source_version, "
                    "projection_key=excluded.projection_key, "
                    "reme_path=excluded.reme_path, deleted_at=excluded.deleted_at, "
                    "updated_at=now()",
                    (job.account_id, job.source_type, job.source_id,
                     job.source_version, job.projection_key,
                     _reme_path(job),
                     current["deleted_at"] if job.action == "delete" else None),
                )
        retry_seconds = min(2 ** min(job.attempt_count, 10), 3600)
        conn.execute(
            "UPDATE growth_memory_jobs SET state=%s, lease_until=NULL, "
            "next_retry_at=CASE WHEN %s='failed' THEN now()+(%s * interval '1 second') "
            "ELSE next_retry_at END, last_error=%s, updated_at=now() "
            "WHERE account_id=%s AND source_type=%s AND source_id=%s AND source_version=%s",
            (state, state, retry_seconds, error[:500] if error else None,
             job.account_id, job.source_type, job.source_id, job.source_version),
        )


async def run_once(dsn: str, adapter: ReMeAdapter) -> str | None:
    """Project one job; keep the business write independent of ReMe availability."""
    with connect(dsn) as guard:
        guard.autocommit = True
        lock_row = guard.execute("SELECT pg_try_advisory_lock(%s)", (_WORKER_LOCK,)).fetchone()
        if lock_row is None or not next(iter(lock_row.values())):
            return None
        try:
            with connect(dsn) as conn:
                conn.autocommit = True
                job = _claim(conn)
                if job is None:
                    return None
                source = _current_source(conn, job)
                if source is None or int(source["version"]) != job.source_version:
                    _finish(conn, job, "skipped")
                    return "skipped"
                if (job.action == "upsert") == (source["deleted_at"] is not None):
                    _finish(conn, job, "skipped")
                    return "skipped"
                try:
                    if job.action == "upsert":
                        await adapter.write_source(
                            job, source["content_text"], list(source["source_refs"])
                        )
                    else:
                        await adapter.delete_note(job)
                except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                    _finish(conn, job, "failed", error=f"{type(exc).__name__}: {exc}")
                    return "failed"
                _finish(conn, job, "succeeded")
                return "succeeded"
        finally:
            guard.execute("SELECT pg_advisory_unlock(%s)", (_WORKER_LOCK,))


async def run_forever(dsn: str, reme_url: str) -> None:
    bootstrap_note_jobs(dsn)
    bootstrap_attempt_jobs(dsn)
    async with httpx.AsyncClient(base_url=reme_url, timeout=20) as client:
        adapter = ReMeAdapter(client)
        while True:
            outcome = await run_once(dsn, adapter)
            if outcome is None:
                await asyncio.sleep(2)


def main() -> None:
    asyncio.run(run_forever(os.environ["GROWTH_DATABASE_URL"], os.environ["GROWTH_REME_URL"]))


if __name__ == "__main__":
    main()
