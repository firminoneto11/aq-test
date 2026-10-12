from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ports import ErrorTrackingPort, SqlDBClientPort


_bound = False

_db_global: SqlDBClientPort | None = None
_error_tracker_global: ErrorTrackingPort | None = None

_db_cv: ContextVar[SqlDBClientPort | None] = ContextVar("db", default=None)
_error_tracker_cv: ContextVar[ErrorTrackingPort | None] = ContextVar(
    "error_tracker", default=None
)


def bind(*, db: SqlDBClientPort, error_tracker: ErrorTrackingPort):
    global _bound, _db_global, _error_tracker_global
    if _bound:
        raise RuntimeError("context already bound — bind() may only be called once")
    _db_global = db
    _error_tracker_global = error_tracker
    _db_cv.set(db)
    _error_tracker_cv.set(error_tracker)
    _bound = True


def get_db():
    v = _db_cv.get()
    if v is not None:
        return v
    if _db_global is not None:
        return _db_global
    raise RuntimeError("db not bound to context")


def get_error_tracker():
    v = _error_tracker_cv.get()
    if v is not None:
        return v
    if _error_tracker_global is not None:
        return _error_tracker_global
    raise RuntimeError("error_tracker not bound to context")
