import pytest

from hub.domain.limits import (
    ContentLimitError,
    validate_content_limits,
    validate_request_body_size,
)

KiB = 1024


def test_within_limits_ok():
    answers = [{"question_id": "q1", "content": "a" * 100}]
    validate_content_limits(answers, "note")


def test_single_item_exactly_16kib_ok():
    answers = [{"question_id": "q1", "content": "a" * (16 * KiB)}]
    validate_content_limits(answers, None)


def test_single_item_over_16kib_rejected():
    answers = [{"question_id": "q1", "content": "a" * (16 * KiB + 1)}]
    with pytest.raises(ContentLimitError) as exc:
        validate_content_limits(answers, None)
    assert exc.value.limit_name == "answers[].content"
    assert exc.value.actual == 16 * KiB + 1
    assert exc.value.limit == 16 * KiB


def test_multibyte_counts_utf8_bytes():
    ok = [{"question_id": "q1", "content": "汉" * 5461}]   # 16383 bytes
    over = [{"question_id": "q1", "content": "汉" * 5462}]  # 16386 bytes
    validate_content_limits(ok, None)
    with pytest.raises(ContentLimitError):
        validate_content_limits(over, None)


def test_total_over_64kib_rejected():
    answers = [
        {"question_id": f"q{i}", "content": "a" * (16 * KiB)} for i in range(5)
    ]  # 5 * 16 KiB = 80 KiB total, each item within limit
    with pytest.raises(ContentLimitError) as exc:
        validate_content_limits(answers, None)
    assert exc.value.limit_name == "answers_total"


def test_notes_over_8kib_rejected():
    with pytest.raises(ContentLimitError) as exc:
        validate_content_limits([{"question_id": "q1", "content": "a"}], "n" * (8 * KiB + 1))
    assert exc.value.limit_name == "notes"


def test_request_body_limit():
    validate_request_body_size(96 * KiB)
    with pytest.raises(ContentLimitError) as exc:
        validate_request_body_size(96 * KiB + 1)
    assert exc.value.limit_name == "request_body"
