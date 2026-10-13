import sys
from functools import partial
from getpass import getpass

import uvicorn
from typer import Exit, Option, Typer
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


@app.command("seed-jobs")
def seed_jobs_command():
    import asyncio

    from app.job_packs import seed_jobs
    from infra.database.adapter import SqlDBClientAdapter

    async def run() -> None:
        db = SqlDBClientAdapter(
            connection_string=settings.DATABASE_URL,
            autocommit=True,
        )
        await db.connect()
        try:
            await seed_jobs(db)
        finally:
            await db.disconnect()

    asyncio.run(run())


@app.command("create-user")
def create_user_command(email: str = Option(..., "--email")):
    import asyncio

    from app.exceptions import GenericException
    from app.usecases.auth import AuthUseCase
    from infra.database.adapter import SqlDBClientAdapter
    from infra.passwords import PasswordAdapter

    password = getpass("Password: ")
    if password != getpass("Confirm password: "):
        echo("Passwords do not match")
        raise Exit(code=1)

    async def run() -> str:
        db = SqlDBClientAdapter(
            connection_string=settings.DATABASE_URL,
            autocommit=True,
        )
        await db.connect()
        try:
            service = AuthUseCase(
                db=db,
                passwords=PasswordAdapter(),
                secret=settings.JWT_SECRET,
                algorithm=settings.JWT_ALGORITHM,
            )
            user = await service.create_user(email=email, password=password)
            return user.email
        finally:
            await db.disconnect()

    try:
        created_email = asyncio.run(run())
    except (GenericException, ValueError) as exc:
        echo(str(exc))
        raise Exit(code=1) from exc
    echo(f"Created user {created_email}")
