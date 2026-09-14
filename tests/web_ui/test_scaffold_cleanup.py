"""rSLMWf · 双前端口径清理验收（web-ui 演示脚手架清理）。

口径（docs/web-ui.md）：web-ui 是未接入后端的 shadcn-admin 脚手架，登录为模板自带
mock 认证；本项目自建认证（FR-01–FR-04）且 PRD 2.2 不做 OAuth2.1/Keycloak——
Clerk 演示路由与依赖不属本项目交付面，必须移除。
"""

import json
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[2] / "web-ui" / "src"


def test_演示认证路由已从路由树移除():
    tree = WEBUI / "routeTree.gen.ts"
    assert tree.exists()
    assert "clerk" not in tree.read_text(encoding="utf-8"), (
        "routeTree.gen.ts 仍引用 clerk 路由（演示脚手架残留）"
    )
    assert not (WEBUI / "routes" / "clerk").exists()


def test_clerk依赖已从清单移除():
    pkg = json.loads((WEBUI.parent / "package.json").read_text(encoding="utf-8"))
    all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert not any("clerk" in name.lower() for name in all_deps), (
        "package.json 仍带 @clerk/* 依赖（PRD 2.2 不做第三方认证服务）"
    )
