import sys
from functools import partial

import uvicorn
from typer import Typer
from typer import echo as Echo

from conf import settings

echo = partial(Echo, file=sys.stderr)
app = Typer()


@app.command("prod")
def prod():
    from ._gunicorn_conf import GunicornApp

    app = GunicornApp(
        host=settings.HOST,
        port=str(settings.PORT),
        asgi_app=settings.ASGI_APP,
        echo=echo,
    )
    app.run()


@app.command("dev")
def dev():
    uvicorn.run(
        settings.ASGI_APP,
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.should_reload,
        access_log=False,
    )
