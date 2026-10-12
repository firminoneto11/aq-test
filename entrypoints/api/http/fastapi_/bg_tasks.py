from asyncio import Task
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types_ import AsgiApp


def get_background_tasks(app: AsgiApp) -> list[Task[None]]:
    return []
