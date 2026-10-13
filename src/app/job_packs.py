"""Versioned, selectable interview jobs and their fixed question banks."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.dialects.postgresql import insert

from app.ports import SqlDBClientPort
from domain.interviews import BankQuestion, PackTopic, RolePack
from domain.models.interview import Job
from shared.utils import datetime_utc


@dataclass(frozen=True)
class SampleJob:
    slug: str
    title: str
    description: str
    pack: RolePack


def _topic(
    slug: str,
    topic_id: str,
    category: Literal["behavioral", "technical"],
    weight: int,
    skills: tuple[str, str],
    prompts: tuple[str, str],
) -> PackTopic:
    return PackTopic(
        id=topic_id,
        category=category,
        weight=weight,
        skill_ids=[f"{topic_id}.{skill}" for skill in skills],
        questions=[
            BankQuestion(id=f"{slug}.{topic_id}.{suffix}", text=text)
            for suffix, text in zip(("a", "b"), prompts, strict=True)
        ],
    )


def _job(
    slug: str, title: str, description: str, topics: list[PackTopic]
) -> SampleJob:
    return SampleJob(
        slug=slug,
        title=title,
        description=description,
        pack=RolePack(version=1, seniority="mid", language="en", topics=topics),
    )


SAMPLE_JOBS: tuple[SampleJob, ...] = (
    _job(
        "backend-engineer",
        "Backend Engineer",
        "Build reliable APIs and data-backed services, collaborate on trade-offs, and operate systems in production.",
        [
            _topic("backend-engineer", "ownership", "behavioral", 20, ("tradeoffs", "collaboration"), (
                "How did you handle a trade-off with another team while owning a backend change?",
                "What did you do when a backend delivery you owned was at risk?",
            )),
            _topic("backend-engineer", "api_design", "technical", 30, ("api_contracts", "idempotency"), (
                "How would you design an idempotent order-creation endpoint that a client may retry?",
                "What contract would you use for paginating a frequently changing resource collection?",
            )),
            _topic("backend-engineer", "data_consistency", "technical", 30, ("transactions", "query_reasoning"), (
                "How would you prevent two concurrent requests from reserving the last item in stock?",
                "What would you investigate when a previously fast SQL query starts timing out as data grows?",
            )),
            _topic("backend-engineer", "reliability", "behavioral", 20, ("diagnosis", "prevention"), (
                "How did you diagnose a production incident when the initial symptom pointed to the wrong service?",
                "What did you learn from a backend failure that changed how your team operates the service?",
            )),
        ],
    ),
    _job(
        "frontend-engineer",
        "Frontend Engineer",
        "Build accessible, responsive web interfaces, manage client-side state, and deliver dependable user experiences with product and design teams.",
        [
            _topic("frontend-engineer", "ownership", "behavioral", 20, ("tradeoffs", "collaboration"), (
                "How did you resolve a disagreement with product or design while owning a frontend feature?",
                "What did you do when a frontend delivery you owned was at risk?",
            )),
            _topic("frontend-engineer", "component_state", "technical", 30, ("state_boundaries", "async_consistency"), (
                "What component and state boundaries would you choose for a multi-step form with shared validation?",
                "How would you keep asynchronously loaded search results consistent when requests return out of order?",
            )),
            _topic("frontend-engineer", "web_quality", "technical", 30, ("accessibility", "performance"), (
                "How would you make a custom dialog keyboard-accessible and usable with a screen reader?",
                "What would you change first on a slow client-rendered page after measuring its loading and interaction performance?",
            )),
            _topic("frontend-engineer", "debugging", "behavioral", 20, ("diagnosis", "regression_prevention"), (
                "How did you isolate a frontend bug that appeared only in production?",
                "What did you change in your delivery process after a frontend regression reached users?",
            )),
        ],
    ),
    _job(
        "data-analyst",
        "Data Analyst",
        "Use SQL and analytical reasoning to validate data, investigate business questions, and communicate evidence-backed recommendations.",
        [
            _topic("data-analyst", "ownership", "behavioral", 20, ("definitions", "validation_ownership"), (
                "How did you resolve conflicting metric definitions across teams?",
                "What did you do when an analysis deadline conflicted with the time needed to validate the data?",
            )),
            _topic("data-analyst", "sql_quality", "technical", 30, ("query_grain", "data_validation"), (
                "What checks would you apply before trusting a SQL join used for a revenue dashboard?",
                "How would you handle duplicated events and missing values when calculating daily active users?",
            )),
            _topic("data-analyst", "analysis_reasoning", "technical", 30, ("causal_reasoning", "uncertainty"), (
                "How would you determine whether a conversion-rate increase after a launch was caused by the launch?",
                "What would you investigate when an aggregate business metric improves but key customer segments become worse?",
            )),
            _topic("data-analyst", "decision_impact", "behavioral", 20, ("business_impact", "stakeholder_communication"), (
                "How did an analysis you delivered change a business decision?",
                "What did you do when stakeholders interpreted your analysis more confidently than the data supported?",
            )),
        ],
    ),
)


async def seed_jobs(db: SqlDBClientPort) -> None:
    """Atomically upsert fixed packs without changing job IDs or interview snapshots."""
    async with db.begin_session() as session:
        for sample in SAMPLE_JOBS:
            values = {
                "slug": sample.slug,
                "title": sample.title,
                "description": sample.description,
                "pack_version": sample.pack.version,
                "pack": sample.pack.model_dump(mode="json"),
            }
            statement = insert(Job).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Job.slug],
                    set_={
                        **{key: values[key] for key in ("title", "description", "pack_version", "pack")},
                        "deleted_at": None,
                        "updated_at": datetime_utc(unix=False),
                    },
                )
            )
