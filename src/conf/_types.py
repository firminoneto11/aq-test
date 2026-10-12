from typing import Literal

type ALLOWED_ENVIRONMENTS = Literal[
    "development", "testing", "staging", "production", "ci"
]

type HTTPBackendFrameworkTypes = Literal["fastapi"]
