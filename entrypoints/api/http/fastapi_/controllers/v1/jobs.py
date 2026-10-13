import sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel

from domain.interviews import JobSummary
from domain.models import Job
from entrypoints.api.http.fastapi_.utils.state import DBSessionDep

router = APIRouter(prefix="/jobs")


class JobList(BaseModel):
    jobs: list[JobSummary]


@router.get("", response_model=JobList)
async def list_jobs(session: DBSessionDep) -> JobList:
    jobs = (await session.scalars(sa.select(Job).order_by(Job.slug))).all()
    return JobList(
        jobs=[
            JobSummary(
                id=job.id,
                slug=job.slug,
                title=job.title,
                description=job.description,
                pack_version=job.pack_version,
            )
            for job in jobs
        ]
    )
