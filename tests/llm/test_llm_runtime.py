"""LLM 运行时配置（``hub/llm/runtime.py``）单测。

2026-09-19 新增。/admin/model 的三个已定裁定 —— 「存库」「立即生效」「仅管理员」
—— 里前两条的答案全在这一层，所以这一层单独测，页面上只测它有没有被用对。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from hub.config import Settings
from hub.db.models import LlmConfig
from hub.llm.runtime import (
    ALLOWED_MODELS,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ConfigValidationError,
    RuntimeLlm,
    mask_secret,
    read_config,
    resolve_effective,
    save_config,
)
from tests.conftest import make_user


def _settings(**over) -> Settings:
    base = dict(database_url="sqlite:///x.db", session_secret="s",
                admin_username=None, admin_initial_password=None)
    base.update(over)
    return Settings(**base)


def _actor(db_session):
    user = make_user(db_session, "op", is_admin=True)
    db_session.commit()
    return user


# --------------------------------------------------------------- 解析与回落 #


def test_默认模型是deepseek官方flash档():
    assert DEFAULT_MODEL == "deepseek-flash"
    assert DEFAULT_MODEL in ALLOWED_MODELS
    # 已下线的两个 id 必须不在白名单里，否则「拒绝保存」形同虚设
    assert "deepseek-chat" not in ALLOWED_MODELS
    assert "deepseek-reasoner" not in ALLOWED_MODELS


def test_空表时逐字段回落环境变量(db_session):
    settings = _settings(deepseek_api_key="env-key", llm_model="deepseek-v4-pro",
                         llm_base_url="https://env.test", llm_request_timeout_seconds=30)
    assert read_config(db_session) is None

    eff = resolve_effective(db_session, settings)

    assert eff.api_key == "env-key"
    assert eff.model == "deepseek-v4-pro"
    assert eff.base_url == "https://env.test"
    assert eff.timeout_seconds == 30
    assert (eff.api_key_source, eff.model_source, eff.base_url_source) == (
        "settings", "settings", "settings")


def test_没有key时来源标为none(db_session):
    eff = resolve_effective(db_session, _settings())
    assert eff.api_key is None
    assert eff.api_key_source == "none"


def test_库里有值时以库为准并标来源为database(db_session):
    actor = _actor(db_session)
    save_config(db_session, actor_id=actor.id, model="deepseek-v4-pro",
                base_url="https://self.test", api_key="db-key")
    db_session.commit()

    eff = resolve_effective(db_session, _settings(deepseek_api_key="env-key",
                                                  llm_model="deepseek-flash"))
    assert eff.model == "deepseek-v4-pro"
    assert eff.base_url == "https://self.test"
    assert eff.api_key == "db-key"
    assert (eff.api_key_source, eff.model_source, eff.base_url_source) == (
        "database", "database", "database")
    # 超时不暴露在页面上，永远来自 env
    assert eff.timeout_seconds == 120


def test_清掉key后回落环境变量且来源回到settings(db_session):
    actor = _actor(db_session)
    save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL, api_key="db-key")
    save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL, clear_api_key=True)
    db_session.commit()

    eff = resolve_effective(db_session, _settings(deepseek_api_key="env-key"))
    assert eff.api_key == "env-key"
    assert eff.api_key_source == "settings"


# ------------------------------------------------------------------ 保存 #


def test_保存是幂等的且如实报告改了哪几项(db_session):
    actor = _actor(db_session)

    _, changed_first = save_config(db_session, actor_id=actor.id,
                                   model="deepseek-v4-pro",
                                   base_url="https://a.test", api_key="k1")
    assert "created" in changed_first
    assert "api_key" in changed_first

    _, changed_again = save_config(db_session, actor_id=actor.id,
                                   model="deepseek-v4-pro",
                                   base_url="https://a.test", api_key="k1")
    # 同值重存不产生「变更」：审计里不该出现无事发生的假记录
    assert changed_again == []

    _, changed_key_only = save_config(db_session, actor_id=actor.id,
                                      model="deepseek-v4-pro",
                                      base_url="https://a.test", api_key="k2")
    assert changed_key_only == ["api_key"]


def test_api_key留空表示保持不变(db_session):
    actor = _actor(db_session)
    save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL, api_key="k1")
    save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL, api_key="   ")
    assert read_config(db_session).api_key == "k1"


def test_拒绝已下线的模型并说清原因(db_session):
    actor = _actor(db_session)
    with pytest.raises(ConfigValidationError) as exc:
        save_config(db_session, actor_id=actor.id, model="deepseek-chat",
                    base_url=DEFAULT_BASE_URL)
    assert "下线" in str(exc.value)
    # 校验在写库之前失败：不能留下半成品行
    assert read_config(db_session) is None


def test_拒绝白名单外的模型(db_session):
    actor = _actor(db_session)
    with pytest.raises(ConfigValidationError) as exc:
        save_config(db_session, actor_id=actor.id, model="gpt-4o",
                    base_url=DEFAULT_BASE_URL)
    assert "不支持" in str(exc.value)
    assert read_config(db_session) is None


def test_base_url必须带协议且空值回落默认(db_session):
    actor = _actor(db_session)
    with pytest.raises(ConfigValidationError):
        save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                    base_url="api.deepseek.com")
    row, _ = save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                         base_url="   ")
    assert row.base_url == DEFAULT_BASE_URL


def test_单行约束挡住第二行(db_session):
    actor = _actor(db_session)
    save_config(db_session, actor_id=actor.id, model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL)
    db_session.add(LlmConfig(id=2, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL))
    with pytest.raises(IntegrityError):
        db_session.flush()


# ------------------------------------------------------------------ 掩码 #


def test_掩码既认得出也拿不走():
    assert mask_secret(None) == ""
    assert mask_secret("") == ""
    assert mask_secret("short") == "•••••"
    masked = mask_secret("sk-1234567890abcdef")
    assert masked.startswith("sk-123")
    assert masked.endswith("cdef")
    assert "4567890abc" not in masked


# ------------------------------------------------------- RuntimeLlm 行为 #


def test_空表时门面按环境变量构造客户端(session_factory):
    llm = RuntimeLlm(session_factory, _settings(llm_model="deepseek-flash"))
    assert llm.current().model == "deepseek-flash"


def test_保存后invalidate立刻生效(session_factory, db_session):
    """「存库 + 立即生效」的核心断言：不重建 app、不重启进程。"""
    actor = _actor(db_session)
    llm = RuntimeLlm(session_factory, _settings(llm_model="deepseek-flash"))
    assert llm.current().model == "deepseek-flash"

    save_config(db_session, actor_id=actor.id, model="deepseek-v4-pro",
                base_url=DEFAULT_BASE_URL, api_key="k1")
    db_session.commit()

    # 未失效前仍是旧值 —— 证明缓存是真的，因而 invalidate 不是装饰
    assert llm.current().model == "deepseek-flash"

    llm.invalidate()
    eff = llm.current()
    assert eff.model == "deepseek-v4-pro"
    assert eff.api_key == "k1"


def test_缓存过期后自行回读(session_factory, db_session):
    actor = _actor(db_session)
    llm = RuntimeLlm(session_factory, _settings(llm_model="deepseek-flash"),
                     ttl_seconds=0.0)
    assert llm.current().model == "deepseek-flash"
    save_config(db_session, actor_id=actor.id, model="deepseek-v4-pro",
                base_url=DEFAULT_BASE_URL)
    db_session.commit()
    # ttl=0 → 下一次 current() 必回读，覆盖「另一个进程写了库」的场景
    assert llm.current().model == "deepseek-v4-pro"


def test_读库失败时回落到环境变量而不是把调用打死():
    def _boom():
        raise RuntimeError("db down")

    settings = _settings(llm_model="deepseek-flash", deepseek_api_key="env-key")
    llm = RuntimeLlm(_boom, settings)
    eff = llm.current()
    assert eff.model == "deepseek-flash"
    assert eff.api_key == "env-key"


def test_没有session_factory时也能工作():
    llm = RuntimeLlm(None, _settings(llm_model="deepseek-flash"))
    assert llm.current().model == "deepseek-flash"
