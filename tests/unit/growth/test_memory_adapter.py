"""ReMe delete responses must distinguish a missing file from a missing Job."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from ahadiff.growth.cloud.memory import ProjectionJob, ReMeAdapter, reme_note_path


def _job() -> ProjectionJob:
    return ProjectionJob(
        account_id=uuid.uuid4(), source_type="note", source_id=uuid.uuid4(),
        source_version=1, projection_key="test", action="delete", attempt_count=1,
    )


def test_delete_job_missing_is_retryable_http_failure() -> None:
    job = _job()

    async def execute() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(404, json={"detail": "Not Found"}),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://reme.test") as client:
            with pytest.raises(httpx.HTTPStatusError):
                await ReMeAdapter(client).delete_note(job)

    asyncio.run(execute())


def test_delete_job_reports_same_file_already_absent() -> None:
    job = _job()
    path = reme_note_path(job.account_id, job.source_id)

    async def execute() -> dict[str, object]:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "success": False, "metadata": {"path": path, "error": "not found"},
            }),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://reme.test") as client:
            return await ReMeAdapter(client).delete_note(job)

    assert asyncio.run(execute()) == {"already_absent": True}
