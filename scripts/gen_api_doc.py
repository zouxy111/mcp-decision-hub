"""从 FastAPI 运行时 OpenAPI 规范生成前端用的 JSON API 契约文档。

为什么自动生成而不是手写：手写的契约文档会与代码脱节 —— 加了接口忘了
更新文档、或参数名改了文档还留着旧名字。本脚本直接读 ``app.openapi()``，
跑一次就与代码一致。

用法：
    uv run python scripts/gen_api_doc.py > docs/api/JSON-API-契约.md
"""

import json
import re
import sys
import tempfile
from pathlib import Path

# 允许从项目根直接 `uv run python scripts/gen_api_doc.py` 执行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.config import Settings
from hub.main import create_app


def build_spec() -> dict:
    """起一个临时app 拿到 OpenAPI 规范。

    用临时 sqlite 文件而不是内存库 —— 迁移逻辑（``init_db``）在两种库上
    都跑得通，但临时文件更接近真实部署。
    """
    tmp = Path(tempfile.mkdtemp()) / "apidoc.db"
    settings = Settings(
        database_url=f"sqlite:///{tmp}",
        session_secret="apidoc",
        admin_username=None,
        admin_initial_password=None,
    )
    app = create_app(settings, llm=None)
    return app.openapi()


def ref_name(sch) -> str:
    if not isinstance(sch, dict):
        return ""
    if "$ref" in sch:
        return sch["$ref"].split("/")[-1]
    if sch.get("type") == "array":
        inner = ref_name(sch.get("items", {}))
        return f"array<{inner}>" if inner else "array"
    return "object" if sch.get("type") == "object" else ""


def describe_fields(schemas: dict, name: str) -> str:
    """把 schema 的字段列成markdown 列表。"""
    sch = schemas.get(name)
    if not sch:
        return ""
    if "enum" in sch:
        return "取值：" + " | ".join(f"`{x}`" for x in sch["enum"])
    props = sch.get("properties", {})
    if not props:
        return ""
    required = set(sch.get("required", []))
    lines = []
    for key, val in props.items():
        if "$ref" in val:
            typ = val["$ref"].split("/")[-1]
        elif val.get("type") == "array":
            items = val.get("items", {})
            typ = "array<" + (
                items["$ref"].split("/")[-1] if "$ref" in items
                else items.get("type", "any")) + ">"
        elif val.get("anyOf"):
            typ = " | ".join(
                x.get("type") or x.get("$ref", "?").split("/")[-1]
                for x in val["anyOf"])
        else:
            typ = val.get("type", "?")
        if "default" in val:
            typ += f"（默认 `{val['default']}`）"
        req = "**必填**" if key in required else ""
        desc = (val.get("description") or "").strip().split("\n")[0]
        lines.append(f"- `{key}`: {typ} {req} {desc}".rstrip())
    return "\n".join(lines)


GROUPS = [
    ("认证（Cookie 会话）", lambda p: p.startswith("/api/auth")),
    ("事项：留言板 / 立场 / 消息 / 决议 / 看板协作",
     lambda p: p.startswith("/api/items") or p.startswith("/api/questions")),
    ("会议模式", lambda p: p.startswith("/api/meetings")),
    ("邀请链接", lambda p: p.startswith("/api/invitations")),
    ("Agent 通道（Bearer token，前端一般用不到）",
     lambda p: p.startswith("/api/agent")),
]

HEADER = """# mcp-decision-hub JSON API 契约（前端开发依据）

> 由 `scripts/gen_api_doc.py` 从 FastAPI 运行时规范生成，**不要手改**——
> 改了后端就跑一遍重新生成，否则会和代码脱节。
> 后端现有 **38 个** `/api/*` JSON 接口，本文覆盖前端会用到的部分。

## 认证：两条通道，别搞混

| 通道 | 用法| 覆盖的接口 |
|---|---|---|
| **Cookie 会话** | 浏览器自动带 `hub_session`，前端不用管 | `/api/auth/*`、`/api/meetings/*`、`/api/invitations/*` |
| **Bearer token** | `Authorization: Bearer hdt_xxx` | `/api/items/*`、`/api/questions`、`/api/agent/*` |

**为什么前端要同时拿两样**：`/api/items/*` 这批接口原本是给 AI Agent 用的，
统一走 Bearer，且被大量测试锁住行为；改成 Cookie 会牵动 900+ 测试，因此不动它。
前端的做法是登录时调 `/api/auth/token`——一次拿到 Cookie + token：

```ts
const res = await fetch('/api/auth/token', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username, password }),
})
const { access_token } = await res.json()
localStorage.setItem('hub_token', access_token)
// Cookie 浏览器自己管，前端不用碰
```

之后：
- `/api/items/*`、`/api/questions` → 加 `Authorization: Bearer ${token}`
- `/api/auth/*`、`/api/meetings/*`、`/api/invitations/*` → 什么都不用加

**同域部署**（前后端一个域名），不涉及 CORS。

## 错误形状

`/api/*` 的错误统一长这样（`hub/main.py` 的全局 handler）：

```json
{ "error_code": "MATTER_NOT_FOUND", "message": "事项不存在", "details": {} }
```

| 状态码 | 含义 |
|---|---|
| 400 | 参数或状态不对（如会议已结束） |
| 401 | token 无效/被吊销 |
| 403 | 没有权限 |
| 404 | 资源不存在 |
| 409 | 冲突（如本轮已提交立场） |
| 422 | 校验失败，`details.errors` 是逐字段列表 |
| 429 | 触发限流，带 `Retry-After` 响应头 |

⚠ **未登录不是 401**：`get_current_user` 会 **303 跳登录页**。前端要么用
`fetch(..., {redirect: 'manual'})` 拦一下，要么先调 `GET /api/auth/session`
判断登录态（它未登录时返回 200 + `authenticated: false`）。

## 权限要点

- 会议、邀请的创建/列举/撤销：**只有事项发起人**
- 读会议详情、提交立场：**事项发起人或参与人**
- 事项列表：只返回本人可见的（发起人 + 参与人）
"""


def main() -> None:
    spec = build_spec()
    schemas = spec.get("components", {}).get("schemas", {})

    out = [HEADER]

    for title, matcher in GROUPS:
        entries = []
        for path in sorted(spec["paths"]):
            if not path.startswith("/api/") or not matcher(path):
                continue
            for method, op in sorted(spec["paths"][path].items()):
                # 路径里的 {xxx} 不是 query 参数，别混进参数表里
                path_params = set(re.findall(r"\{(\w+)\}", path))
                query = []
                for prm in op.get("parameters", []):
                    if prm["name"] in path_params:
                        continue
                    sch = prm.get("schema", {})
                    typ = (sch["$ref"].split("/")[-1] if "$ref" in sch
                           else sch.get("type", "?"))
                    desc = (prm.get("description") or "").split("\n")[0]
                    query.append({
                        "name": prm["name"], "type": typ,
                        "required": prm.get("required", False),
                        "desc": desc,
                    })
                req_sch = op.get("requestBody", {}).get("content", {}) \
                    .get("application/json", {}).get("schema", {})
                resp_sch = {}
                for code, resp in op.get("responses", {}).items():
                    if code.startswith("2"):
                        resp_sch = resp.get("content", {}) \
                            .get("application/json", {}).get("schema", {})
                        break
                entries.append({
                    "method": method.upper(), "path": path,
                    "summary": op.get("summary", ""),
                    "description": (op.get("description") or "").strip(),
                    "query": query,
                    "req": ref_name(req_sch), "resp": ref_name(resp_sch),
                })

        if not entries:
            continue

        out.append(f"\n---\n\n## {title}\n")
        for e in entries:
            sig = e["path"]
            if e["query"]:
                sig += "?" if any(not q["required"] for q in e["query"]) else ""
            out.append(f"### `{e['method']} {sig}`")
            if e["summary"]:
                out.append(f"\n*{e['summary']}*\n")
            if e["description"]:
                out.append(f"\n{e['description']}\n")
            if e["query"]:
                out.append("**Query 参数**\n")
                for q in e["query"]:
                    mark = "**必填**" if q["required"] else ""
                    out.append(f"- `{q['name']}`: {q['type']} {mark} {q['desc']}".rstrip())
                out.append("")
            for label, name in (("请求体", e["req"]), ("响应", e["resp"])):
                if not name:
                    continue
                out.append(f"**{label}**：`{name}`\n")
                fields = describe_fields(schemas, name)
                if fields:
                    out.append(fields + "\n")
            out.append("---")

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
