"""One error type with a short code, so every caller shows errors the same way."""


class BlastError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
