class BaseExc(Exception):
    def __init__(self, detail: str, code: int):
        self.code = code
        self.detail = detail
        super().__init__(detail)


class GenericException(BaseExc): ...
