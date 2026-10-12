from typing import Any

from starlette.routing import NoMatchFound

from entrypoints.api.http.fastapi_.types_ import AsgiApp


def reverse_url(view_name: str, *, app: AsgiApp, **path_params: Any):
    """Resolve a URL path by route name, similar to Django's ``reverse()``.

    Args:
        view_name: The route name to resolve.  For routes inside a mounted
            sub-application, use the ``"mount:route"`` format
            (e.g. ``"v1:health_check"``).  If a plain name is given and
            isn't found on the root app, every mounted sub-app is searched
            automatically so you can omit the mount prefix.
        app: The FastAPI application instance to resolve against.
        **path_params: Path parameters to interpolate into the URL
            (e.g. ``reverse_url("get_user", user_id=42)``).

    Returns:
        The resolved URL path as a string.

    Raises:
        NoMatchFound: If no route matches *view_name*.

    Note:
        The automatic sub-app search only looks one level deep into the
        mounted applications of the provided *app*. Deeply nested mounts
        are not searched. For those cases, use a qualified name instead
        (e.g. ``"mount:sub_mount:route"``).
    """

    # 1) Try an exact lookup on the root application (handles both plain
    #    names on the root and "mount:route" qualified names).
    try:
        return str(app.url_path_for(view_name, **path_params))
    except NoMatchFound:
        pass

    # 2) If the name wasn't qualified, search each mounted sub-app so the
    #    caller doesn't have to remember mount prefixes.
    if ":" not in view_name:
        for mount in getattr(app.state, "mounted_applications", []):
            try:
                url = str(mount.app.url_path_for(view_name, **path_params))
                # url_path_for on the sub-app returns a path relative to
                # its own root – prepend the mount path.
                return f"{mount.path}{url}"
            except NoMatchFound:
                continue

    raise ValueError(view_name, path_params)
