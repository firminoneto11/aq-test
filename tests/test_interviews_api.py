from uuid import uuid4

import sqlalchemy as sa
from httpx import AsyncClient

from app.job_packs import seed_jobs
from app.ports import SqlDBClientPort
from app.usecases.auth import AuthUseCase
from conf import settings
from domain.models import User
from entrypoints.api.http.fastapi_.factory import ASGIFactory
from entrypoints.api.http.fastapi_.types_ import AsgiApp

from .test_interviews import remove_interview


async def test_job_selection_create_retry_and_private_results(
    http_client: AsyncClient,
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    await seed_jobs(db)
    jobs_response = await http_client.get("/api/v1/jobs")
    assert jobs_response.status_code == 200
    jobs = jobs_response.json()["jobs"]
    assert [job["slug"] for job in jobs] == [
        "backend-engineer",
        "data-analyst",
        "frontend-engineer",
    ]
    assert all(job["title"] and job["description"] for job in jobs)
    unauthorized = await http_client.post(
        "/api/v1/interviews", json={"job_id": jobs[0]["id"], "request_id": str(uuid4())}
    )
    assert unauthorized.status_code == 401

    _, app = asgi_config
    auth = AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    )
    password = "correct-horse-battery-staple"
    owner_email = f"owner-{uuid4()}@example.com"
    stranger_email = f"stranger-{uuid4()}@example.com"
    owner = await auth.create_user(owner_email, password)
    stranger = await auth.create_user(stranger_email, password)
    interview_id = None
    try:

        async def headers(email: str) -> dict[str, str]:
            response = await http_client.post(
                "/api/v1/auth/token", data={"username": email, "password": password}
            )
            assert response.status_code == 200
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        owner_headers = await headers(owner_email)
        stranger_headers = await headers(stranger_email)
        path = "/api/v1/interviews"
        payload = {"job_id": jobs[0]["id"], "request_id": str(uuid4())}
        created = await http_client.post(path, json=payload, headers=owner_headers)
        assert created.status_code == 201
        state = created.json()
        interview_id = state["id"]
        assert state["job"]["slug"] == "backend-engineer"
        assert state["answered_count"] == 0
        assert state["current_question"] is None
        assert len(state["rubric"]) == 4

        retry = await http_client.post(path, json=payload, headers=owner_headers)
        assert retry.status_code == 200
        assert retry.json()["id"] == interview_id
        changed_job = await http_client.post(
            path, json={**payload, "job_id": jobs[1]["id"]}, headers=owner_headers
        )
        assert changed_job.status_code == 409
        unknown_job = await http_client.post(
            path,
            json={"job_id": str(uuid4()), "request_id": str(uuid4())},
            headers=owner_headers,
        )
        assert unknown_job.status_code == 404

        for suffix in ("", "/transcript", "/evaluation"):
            denied = await http_client.get(
                f"{path}/{interview_id}{suffix}", headers=stranger_headers
            )
            assert denied.status_code == 404
        transcript = await http_client.get(
            f"{path}/{interview_id}/transcript", headers=owner_headers
        )
        assert transcript.status_code == 200
        assert transcript.json()["questions"] == []
        assert transcript.json()["messages"] == []
        evaluation = await http_client.get(
            f"{path}/{interview_id}/evaluation", headers=owner_headers
        )
        assert evaluation.status_code == 200
        assert evaluation.json() == {
            "status": "not_ready",
            "result": None,
            "error_code": None,
            "can_retry": False,
        }
        premature = await http_client.post(
            f"{path}/{interview_id}/evaluation", headers=owner_headers
        )
        assert premature.status_code == 409
    finally:
        if interview_id is not None:
            from uuid import UUID

            await remove_interview(db, UUID(interview_id), owner.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == owner.id))
        async with db.begin_session() as session:
            await session.execute(sa.delete(User).where(User.id == stranger.id))
