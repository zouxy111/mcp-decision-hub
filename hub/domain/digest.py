"""content_digest computation, exact PRD 9.3 formula."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def compute_content_digest(answers: list[dict[str, str]], notes: str | None) -> str:
    parts: list[str] = []
    for item in sorted(answers, key=lambda a: a["question_id"]):
        parts.append(item["question_id"] + "\n" + item["content"] + "\n")
    parts.append(notes if notes is not None else "")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def compute_stance_content_hash(fields: Mapping[str, Any]) -> str:
    """立场正文摘要（B3 口径冻结）。

    调用方传 ``payload.model_dump(mode="json")``；本函数自行剔除
    ``content_hash`` 自身，再对剩余字段做规范化 JSON 后取 sha256：

        body = {k: v for k, v in fields.items() if k != "content_hash"}
        hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()

    sort_keys 保证字段顺序无关；ensure_ascii=False + 紧凑分隔符保证与
    客户端按同一文本编码计算时结果一致。服务端重算并比对，客户端传什么
    不再等于存什么。
    """
    body = {k: v for k, v in fields.items() if k != "content_hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
