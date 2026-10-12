from abc import ABCMeta, abstractmethod
from typing import Any


class BaseBackend(metaclass=ABCMeta):
    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def capture_exception(self, exc: BaseException, /, **context: Any) -> None: ...
