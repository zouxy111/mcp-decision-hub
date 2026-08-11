"""content_digest computation, exact PRD 9.3 formula."""

import hashlib


def compute_content_digest(answers: list[dict[str, str]], notes: str | None) -> str:
    parts: list[str] = []
    for item in sorted(answers, key=lambda a: a["question_id"]):
        parts.append(item["question_id"] + "\n" + item["content"] + "\n")
    parts.append(notes if notes is not None else "")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
