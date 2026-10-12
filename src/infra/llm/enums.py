from typing import Literal, cast

ALLOWED_PROVIDERS = Literal["google", "anthropic", "openrouter"]

ALLOWED_PROVIDERS_TPL = cast(tuple[str, ...], ALLOWED_PROVIDERS.__args__)  # type: ignore
