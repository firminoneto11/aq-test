import json as jsonlib


class BaseExc(Exception):
    def __init__(
        self,
        detail: str,
        status_code: int | None = None,
        response_text: str | None = None,
    ):
        self.detail = detail
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(self.error_message)

    @property
    def error_message(self):
        return jsonlib.dumps(
            {
                "detail": self.detail,
                "status_code": self.status_code,
                "response_text": self.response_text,
            }
        )


class InvalidHTTPStatusError(BaseExc): ...


class HTTPError(BaseExc): ...
