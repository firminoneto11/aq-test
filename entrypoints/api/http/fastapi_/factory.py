import asyncio as aio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI

from conf import settings
from infra.database import SqlDBClientAdapter
from infra.error_tracking import ErrorTrackingAdapter
from shared import context, logging

from . import bg_tasks, handlers, middleware, routers, types_

if TYPE_CHECKING:
    from starlette.middleware.base import BaseHTTPMiddleware


class ASGIFactory:
    @classmethod
    def new(cls, base_model_to_migrate: Any | None = None):
        config = cls(base_model_to_migrate)
        return config, config.application

    @property
    def apps(self):
        if self._is_router_setup:
            return (
                self.application,
                *[mount.app for mount in self.application.state.mounted_applications],
            )
        raise ValueError(
            "This property can only be called after the `configure_routers` method is called"
        )

    def __init__(self, base_model_to_migrate: Any | None):
        self._is_router_setup = False
        self.base_model_to_migrate = base_model_to_migrate

        self.application = cast(
            types_.AsgiApp,
            FastAPI(
                **settings.get_asgi_settings(main_mount=True),  # type:ignore
                lifespan=self.lifespan,  # type:ignore
            ),
        )

        self.configure_logging()
        self.configure_state()
        self.configure_routers()

        self.configure_middleware()
        self.configure_exception_handlers()

    def configure_logging(self):
        service_name = settings.get_service_name()
        logging.configure_logging(
            log_level=settings.LOG_LEVEL,
            use_simple_logging=settings.is_local,
            service_name=service_name,
        )
        if settings.DEBUG:
            logging.get_logger().warning("Debug is enabled")

    def configure_state(self):
        self.application.state.db = SqlDBClientAdapter(
            connection_string=settings.DATABASE_URL,
            autocommit=settings.AUTOCOMMIT,
        )
        self.application.state.error_tracker = ErrorTrackingAdapter(
            settings.ERROR_TRACKER_BACKEND, **settings.error_tracker_kwargs
        )
        self.application.state.mounted_applications = []

    def configure_routers(self):
        for mount in routers.get_mounts():
            self._update_sub_mount_state(sub_mount=mount.app)
            self.application.mount(path=mount.path, app=mount.app, name=mount.name)
            self.application.state.mounted_applications.append(mount)
        self._is_router_setup = True

    def configure_middleware(self):
        middlewares: list[dict[str, Any] | type[BaseHTTPMiddleware]] = [
            middleware.CorrelationIdMiddleware,
            middleware.cors_middleware_configuration,
            middleware.HttpLoggerMiddleware,
        ]
        middlewares.reverse()  # NOTE: Middleware runs from bottom to top (Need to reverse to preserver order)

        for mdl in middlewares:
            for app in self.apps:
                if isinstance(mdl, dict):
                    app.add_middleware(**mdl)
                else:
                    app.add_middleware(mdl)

    def configure_exception_handlers(self):
        for key, value in handlers.HANDLERS_MAP.items():
            for app in self.apps:
                app.add_exception_handler(key, value)

    def _update_sub_mount_state(self, sub_mount: types_.AsgiApp):
        state_items = ["db", "error_tracker"]
        for item in state_items:
            main_value = getattr(self.application.state, item)
            setattr(sub_mount.state, item, main_value)
        sub_mount.state.mounted_applications = []

    @asynccontextmanager
    async def lifespan(self, app: types_.AsgiApp):
        tasks = await self._on_startup(app=app)
        try:
            yield
        finally:
            await self._on_shutdown(app=app, tasks=tasks)

    async def _on_startup(self, app: types_.AsgiApp):
        await self._connect_to_services(app=app)
        await self._create_db_tables(app=app)
        return bg_tasks.get_background_tasks(app=app)

    async def _connect_to_services(self, app: types_.AsgiApp):
        if settings.CONNECT_TO_SERVICES_ON_STARTUP:
            app.state.error_tracker.init()
            async with aio.TaskGroup() as group:
                group.create_task(app.state.db.connect())
            self._bind_state_to_context(app=app)

    async def _create_db_tables(self, app: types_.AsgiApp):
        logger = logging.get_logger()
        if (
            self.base_model_to_migrate
            and settings.CONNECT_TO_SERVICES_ON_STARTUP
            and settings.is_local
        ):
            await logger.awarning(
                "DB tables are being created automatically. Make sure this is a "
                "testing/local environment"
            )
            await app.state.db.migrate(base_model=self.base_model_to_migrate)

    def _bind_state_to_context(self, app: types_.AsgiApp):
        context.bind(db=app.state.db, error_tracker=app.state.error_tracker)

    async def _on_shutdown(self, app: types_.AsgiApp, tasks: list[aio.Task[None]]):
        logger = logging.get_logger()

        [task.cancel() for task in tasks if task]
        results = await aio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, aio.CancelledError
            ):
                await logger.aerror(
                    "Background task failed during shutdown", exc_info=result
                )
                app.state.error_tracker.capture_exception(
                    result, area="api.shutdown.background_task"
                )

        await self._disconnect_from_services(app=app)

    async def _disconnect_from_services(self, app: types_.AsgiApp):
        if settings.CONNECT_TO_SERVICES_ON_STARTUP:
            logger = logging.get_logger()
            results = await aio.gather(
                app.state.db.disconnect(),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    await logger.aerror("Service disconnection failed", exc_info=result)
