"""Unified error shape {error_code, message, details?} (PRD 9.5).

M1 extensions beyond PRD 9.5 (both 422, for malformed client payloads the PRD
table does not cover): VALIDATION_FAILED, CURSOR_INVALID. Do NOT add others.
"""


class ApiError(Exception):
    def __init__(self, status_code: int, error_code: str, message: str,
                 details: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details
        # PRD 9.1 限流要回 Retry-After；其余错误不设置即为空。
        self.headers = headers or {}
        super().__init__(message)


def error_payload(err: ApiError) -> dict:
    payload = {"error_code": err.error_code, "message": err.message}
    if err.details:
        payload["details"] = err.details
    return payload
