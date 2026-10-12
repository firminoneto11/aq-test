import pytest

from .types_ import ReverseURL


async def test_resolve_qualified_name(reverse_url: ReverseURL):
    url = reverse_url("v1:liveness_probe")
    assert url == "/api/v1/probes/health"


async def test_resolve_unqualified_name_searches_mounts(reverse_url: ReverseURL):
    url = reverse_url("liveness_probe")
    assert url == "/api/v1/probes/health"


async def test_raises_no_match_found_for_unknown_route(reverse_url: ReverseURL):
    with pytest.raises(ValueError):
        reverse_url("nonexistent_route")
