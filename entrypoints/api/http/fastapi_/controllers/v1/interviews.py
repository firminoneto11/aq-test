from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.exceptions import GenericException
from app.usecases.voice_sessions import ConnectionPending, ConnectionResponse
from domain.interviews import (
    EvaluationResponse,
    InterviewState,
    TranscriptResponse,
)
from entrypoints.api.http.fastapi_.utils.state import (
    InterviewDep,
    UserDep,
    VoiceSessionsDep,
)

router = APIRouter(prefix="/interviews")


class CreateInterviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    request_id: UUID


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID


@router.post("", response_model=InterviewState, status_code=201)
async def create_interview(
    payload: CreateInterviewRequest,
    response: Response,
    user: UserDep,
    interviews: InterviewDep,
) -> InterviewState:
    state, created = await interviews.create(user.id, payload.job_id, payload.request_id)
    if not created:
        response.status_code = 200
    return state


@router.post("/{interview_id}/connect", response_model=ConnectionResponse)
async def connect_interview(
    interview_id: UUID,
    payload: ConnectRequest,
    response: Response,
    user: UserDep,
    voice: VoiceSessionsDep,
) -> ConnectionResponse:
    connection = await voice.connect(user.id, interview_id, payload.request_id)
    if isinstance(connection, ConnectionPending):
        response.status_code = 202
    return connection


@router.post("/{interview_id}/end", response_model=InterviewState)
async def end_interview(
    interview_id: UUID, user: UserDep, voice: VoiceSessionsDep
) -> InterviewState:
    return await voice.end(user.id, interview_id)


@router.get("/{interview_id}", response_model=InterviewState)
async def get_interview(
    interview_id: UUID, user: UserDep, interviews: InterviewDep
) -> InterviewState:
    return await interviews.get_state(user.id, interview_id)


@router.get("/{interview_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    interview_id: UUID, user: UserDep, interviews: InterviewDep
) -> TranscriptResponse:
    return await interviews.get_transcript(user.id, interview_id)


@router.get("/{interview_id}/evaluation", response_model=EvaluationResponse)
async def get_evaluation(
    interview_id: UUID, user: UserDep, interviews: InterviewDep
) -> EvaluationResponse:
    return await interviews.get_evaluation(user.id, interview_id)


@router.post("/{interview_id}/evaluation", response_model=EvaluationResponse)
async def retry_evaluation(
    interview_id: UUID, response: Response, user: UserDep, interviews: InterviewDep
) -> EvaluationResponse:
    state = await interviews.get_state(user.id, interview_id)
    if state.status not in ("evaluating", "evaluation_failed", "completed"):
        raise GenericException("Interview is not ready for evaluation", 409)
    result = await interviews.evaluate(interview_id)
    if result.status == "running":
        response.status_code = 202
    return result
