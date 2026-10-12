import json as jsonlib
import os
import signal
import typing

import psutil
from gunicorn.app.base import BaseApplication
from gunicorn.util import import_app

if typing.TYPE_CHECKING:
    from uvicorn_worker import UvicornWorker


class GunicornApp(BaseApplication):
    def __init__(
        self,
        host: str,
        port: str,
        asgi_app: str,
        echo: typing.Callable[[str], None],
        **kwargs: typing.Any,
    ):
        self._host = host
        self._port = port
        self._asgi_app = asgi_app
        self._echo = echo
        super().__init__(**kwargs)

    def load(self):
        return import_app(self._asgi_app)

    def load_config(self):
        gunicorn_workers, physical_cores, logical_cores = self._workers_formula()

        opts = {
            "bind": f"{self._host}:{self._port}",
            "workers": gunicorn_workers,
            "keepalive": 120,
            "worker_class": "uvicorn_worker.UvicornWorker",
            "worker_int": _worker_int,
        }

        for key, val in opts.items():
            self.cfg.set(key, val)

        keys_to_skip = ("worker_int",)

        self._echo(
            jsonlib.dumps(
                {
                    **{k: v for k, v in opts.items() if k not in keys_to_skip},
                    "cpu_physical_cores": physical_cores,
                    "cpu_logical_cores": logical_cores,
                },
                indent=4,
            )
        )

    def _workers_formula(self):
        physical_cores = psutil.cpu_count(logical=False)
        logical_cores = psutil.cpu_count()

        gunicorn_workers = physical_cores or logical_cores

        if gunicorn_workers is None:
            self._echo("Could not determine CPU core count, defaulting to 1")
            gunicorn_workers = 1

        return gunicorn_workers, physical_cores, logical_cores


def _worker_int(worker: UvicornWorker):
    os.kill(int(worker.pid), signal.SIGINT)
