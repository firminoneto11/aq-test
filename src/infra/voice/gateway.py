"""LiveKit room, dispatch and audio-only candidate access."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from livekit import api

from app.ports import VoiceGatewayPort
from conf import settings


class LiveKitGateway(VoiceGatewayPort):
    """One long-lived server client; close it with ``aclose`` at shutdown."""

    def __init__(self) -> None:
        self.server_url = settings.LIVEKIT_URL
        self._api_key = settings.LIVEKIT_API_KEY.get_secret_value()
        self._api_secret = settings.LIVEKIT_API_SECRET.get_secret_value()
        if not self.server_url or not self._api_key or not self._api_secret:
            raise ValueError("LiveKit URL, API key and API secret are required")
        self._client: api.LiveKitAPI | None = None

    def _get_client(self) -> api.LiveKitAPI:
        # Called from async operations, so aiohttp's session is created on the
        # owning event loop rather than at HTTP application import time.
        if self._client is None:
            self._client = api.LiveKitAPI(
                url=self.server_url,
                api_key=self._api_key,
                api_secret=self._api_secret,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "LiveKitGateway":
        self._get_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def create_room(self, room_name: str) -> None:
        try:
            await self._get_client().room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=60,
                    max_participants=2,
                )
            )
        except api.ServerError as exc:
            if exc.code != api.ServerErrorCode.ALREADY_EXISTS:
                raise

    async def dispatch(self, room_name: str, interview_id: UUID, run_id: UUID) -> str:
        dispatched = await self._get_client().agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name="interviewer",
                metadata=json.dumps(
                    {"interview_id": str(interview_id), "run_id": str(run_id)},
                    separators=(",", ":"),
                ),
            )
        )
        return dispatched.id

    def issue_token(
        self, room_name: str, participant_identity: str, expires_at: datetime
    ) -> str:
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("Grant expiry must be timezone-aware")
        # AccessToken accepts a relative TTL and computes now again when signed.
        # Subtract one second to ensure that rounding and the second clock read
        # cannot extend this token beyond the persisted absolute grant expiry.
        ttl = expires_at.astimezone(timezone.utc) - datetime.now(timezone.utc)
        if ttl <= timedelta(seconds=1):
            raise ValueError("Grant expired")
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(participant_identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                    can_publish_sources=["microphone"],
                )
            )
            .with_ttl(ttl - timedelta(seconds=1))
            .to_jwt()
        )

    async def close_room(self, room_name: str) -> None:
        try:
            await self._get_client().room.delete_room(api.DeleteRoomRequest(room=room_name))
        except api.ServerError as exc:
            if exc.code != api.ServerErrorCode.NOT_FOUND:
                raise
