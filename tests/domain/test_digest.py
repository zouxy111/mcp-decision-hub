import hashlib

from hub.domain.digest import compute_content_digest


def test_digest_matches_reference_computation():
    answers = [
        {"question_id": "q2", "content": "B"},
        {"question_id": "q1", "content": "A"},
    ]
    expected = hashlib.sha256("q1\nA\nq2\nB\n".encode("utf-8")).hexdigest()
    assert compute_content_digest(answers, None) == expected


def test_digest_is_order_independent():
    a = [{"question_id": "q1", "content": "x"}, {"question_id": "q2", "content": "y"}]
    b = list(reversed(a))
    assert compute_content_digest(a, None) == compute_content_digest(b, None)


def test_notes_appended_without_separator():
    answers = [{"question_id": "q1", "content": "A"}]
    expected = hashlib.sha256("q1\nA\nNOTE".encode("utf-8")).hexdigest()
    assert compute_content_digest(answers, "NOTE") == expected


def test_none_notes_equals_empty_string():
    answers = [{"question_id": "q1", "content": "A"}]
    assert compute_content_digest(answers, None) == compute_content_digest(answers, "")


def test_utf8_multibyte_content():
    answers = [{"question_id": "q1", "content": "中文内容"}]
    expected = hashlib.sha256("q1\n中文内容\n".encode("utf-8")).hexdigest()
    assert compute_content_digest(answers, None) == expected
