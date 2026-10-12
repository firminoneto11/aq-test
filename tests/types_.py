from typing import Any, Protocol


class ReverseURL(Protocol):
    def __call__(self, view_name: str, **path_params: Any) -> str: ...
