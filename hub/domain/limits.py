"""Content size limits (PRD 9.1). All sizes are UTF-8 encoded byte counts."""


class ContentLimitError(ValueError):
    def __init__(self, limit_name: str, actual: int, limit: int):
        self.limit_name = limit_name
        self.actual = actual
        self.limit = limit
        super().__init__(f"{limit_name} 超限: {actual} 字节 > 上限 {limit} 字节")


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def validate_content_limits(
    answers: list[dict[str, str]],
    notes: str | None,
    *,
    item_limit: int = 16 * 1024,
    total_limit: int = 64 * 1024,
    notes_limit: int = 8 * 1024,
) -> None:
    total = 0
    for item in answers:
        size = _utf8_len(item["content"])
        if size > item_limit:
            raise ContentLimitError("answers[].content", size, item_limit)
        total += size
    if total > total_limit:
        raise ContentLimitError("answers_total", total, total_limit)
    if notes is not None and _utf8_len(notes) > notes_limit:
        raise ContentLimitError("notes", _utf8_len(notes), notes_limit)


def validate_request_body_size(size: int, *, body_limit: int = 96 * 1024) -> None:
    if size > body_limit:
        raise ContentLimitError("request_body", size, body_limit)
