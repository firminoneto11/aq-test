import sys
import traceback
from typing import Any

from .base import BaseBackend


class ConsoleBackend(BaseBackend):
    def __init__(self, **_: Any):
        pass

    def init(self) -> None:
        pass

    def capture_exception(self, exc: BaseException, /, **context: Any) -> None:
        print("Captured exception", file=sys.stderr)
        if context:
            print(f"Context: {context}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
