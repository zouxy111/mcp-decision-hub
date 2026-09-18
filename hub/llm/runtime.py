"""LLM 运行时配置：值存 DB、每次调用前解析一次、保存即生效。

为什么需要这一层
----------------
``Settings`` 是 frozen dataclass，``DeepSeekClient`` 在 ``create_app()`` 里只
构造一次并挂到 ``app.state.llm``。也就是说改造前「改配置」只有一条路：改
``.env`` 再重启进程 —— 而这个应用没有任何重启入口，页面上也看不到「需要
重启」。使用者按下保存、看到成功、然后行为没变，是最糟的一种失败：它不报错。

这一层把「生效配置」从「启动时的常量」改成「调用前解析一次的变量」：

* 值落在 ``llm_config`` 单行表（见 :mod:`hub.db.migrations` v11）；
* 页面保存后调用 :meth:`RuntimeLlm.invalidate`，同进程内下一次调用即回读；
* 另留 TTL 兜底，覆盖「另一个进程写了库」的情况（本应用目前单进程，TTL 让
  这层在将来多进程时不会静默失效）。

解析顺序逐字段：DB 有值以 DB 为准，否则回落 ``Settings``（环境变量 / .env）。
**因此空表 = 行为与改造前完全一致** —— 存量部署不需要任何数据迁移。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from hub.config import Settings
from hub.db.models import LlmConfig as LlmConfigRow
from hub.domain.timeutil import utcnow
from hub.llm.client import DeepSeekClient
from hub.llm.client import LLMError  # re-export：调用方只需 import 本模块

logger = logging.getLogger(__name__)

# 官方模型 id（api-docs.deepseek.com，2026-09-19 复核）：
#   deepseek-flash    —— 官方推荐档
#   deepseek-v4-pro   —— 能力档
# 用白名单而非自由文本：写错一个 id 只会换来一个上游 400，且在页面上看不出
# 原因。宁可在这里挡掉，也不让一个错字进库。
DEFAULT_MODEL = "deepseek-flash"
ALLOWED_MODELS: tuple[str, ...] = ("deepseek-flash", "deepseek-v4-pro")
# 2026-07-24 15:59 UTC 下线。留在代码里是为了把「为什么不能用」讲清楚 ——
# 这两个 id 的失败信息应该是「已下线」，而不是「不支持」。
RETIRED_MODELS: tuple[str, ...] = ("deepseek-chat", "deepseek-reasoner")
DEFAULT_BASE_URL = "https://api.deepseek.com"
# 页面出口：让使用者自己去生成 key，而不是让他猜去哪找。
API_KEY_CONSOLE_URL = "https://platform.deepseek.com/api_keys"

SINGLETON_ID = 1

__all__ = [
    "ALLOWED_MODELS", "API_KEY_CONSOLE_URL", "DEFAULT_BASE_URL", "DEFAULT_MODEL",
    "ConfigValidationError", "EffectiveLlm", "LLMError", "RETIRED_MODELS",
    "RuntimeLlm", "mask_secret", "read_config", "resolve_effective", "save_config",
]


class ConfigValidationError(ValueError):
    """模型配置未通过校验；``str(exc)`` 是给使用者看的整句中文。"""


@dataclass(frozen=True)
class EffectiveLlm:
    """一次解析的结论：调用要用的四个值 + 每个值的来源。

    ``*_source`` 存在的理由：页面必须能回答「现在到底在按谁说的跑」。
    只显示模型名而不显示它的来源，会让人以为改了页面就一定生效了。
    """

    api_key: str | None
    base_url: str
    model: str
    timeout_seconds: int
    api_key_source: str    # "database" | "settings" | "none"
    model_source: str      # "database" | "settings"
    base_url_source: str   # "database" | "settings"


def _from_settings(settings: Settings) -> EffectiveLlm:
    return EffectiveLlm(
        api_key=settings.deepseek_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_request_timeout_seconds,
        api_key_source="settings" if settings.deepseek_api_key else "none",
        model_source="settings",
        base_url_source="settings",
    )


def read_config(db: Session) -> LlmConfigRow | None:
    """读单行配置；无行 = 从未在页面上保存过。"""
    return db.get(LlmConfigRow, SINGLETON_ID)


def resolve_effective(db: Session, settings: Settings) -> EffectiveLlm:
    """逐字段解析生效值。纯读，无副作用。"""
    row = read_config(db)
    if row is None:
        return _from_settings(settings)
    api_key = (row.api_key or "").strip()
    model = (row.model or "").strip()
    base_url = (row.base_url or "").strip()
    return EffectiveLlm(
        api_key=api_key or settings.deepseek_api_key,
        base_url=base_url or settings.llm_base_url,
        model=model or settings.llm_model,
        timeout_seconds=settings.llm_request_timeout_seconds,
        api_key_source=("database" if api_key
                        else ("settings" if settings.deepseek_api_key else "none")),
        model_source="database" if model else "settings",
        base_url_source="database" if base_url else "settings",
    )


def mask_secret(value: str | None) -> str:
    """给页面回显用的掩码。绝不返回原文。

    ``sk-`` 之类的固定前缀留 6 位 + 末 4 位：足够让人确认「是不是我那一把」，
    不足以让人复制走。短于 12 位一律全遮，避免掩码本身泄露大部分内容。
    """
    if not value:
        return ""
    if len(value) <= 12:
        return "•" * len(value)
    return f"{value[:6]}{'•' * 8}{value[-4:]}"


def validate_model(model: str) -> str:
    """模型 id 校验。返回规范化后的值，非法则抛 :class:`ConfigValidationError`。"""
    model = (model or "").strip()
    if model in RETIRED_MODELS:
        raise ConfigValidationError(
            f"模型 {model} 已于 2026-07-24 下线（官方已换用 {DEFAULT_MODEL}），不能再选。")
    if model not in ALLOWED_MODELS:
        raise ConfigValidationError(
            f"不支持的模型 id「{model}」。可选：{'、'.join(ALLOWED_MODELS)}。")
    return model


def validate_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip() or DEFAULT_BASE_URL
    if not base_url.startswith(("http://", "https://")):
        raise ConfigValidationError("Base URL 必须以 http:// 或 https:// 开头。")
    if len(base_url) > 255:
        raise ConfigValidationError("Base URL 过长（限 255 字符）。")
    return base_url.rstrip("/")


def save_config(db: Session, *, actor_id: int, model: str, base_url: str,
                api_key: str = "",
                clear_api_key: bool = False) -> tuple[LlmConfigRow, list[str]]:
    """写入单行配置。三态语义（这是本函数唯一容易写错的地方）：

    * ``api_key`` 留空 且 ``clear_api_key=False`` → **保持原样**（页面用掩码
      回显，提交空值代表「我没打算改它」，不是「删掉它」）；
    * ``api_key`` 有新值 → 覆盖；
    * ``clear_api_key=True`` → 置 NULL，回落环境变量。

    不 ``commit``：事务边界留给调用方（与 ``hub.api.accounts`` 一致）。
    """
    model = validate_model(model)
    base_url = validate_base_url(base_url)
    new_key = (api_key or "").strip()

    row = read_config(db)
    created = row is None
    if row is None:
        row = LlmConfigRow(id=SINGLETON_ID, model=model, base_url=base_url)
        db.add(row)

    changed: list[str] = []
    if row.model != model:
        changed.append("model")
    if row.base_url != base_url:
        changed.append("base_url")
    key_changed = False
    if clear_api_key:
        if row.api_key is not None:
            key_changed = True
        row.api_key = None
    elif new_key:
        if row.api_key != new_key:
            key_changed = True
        row.api_key = new_key
    if key_changed:
        changed.append("api_key")

    row.model = model
    row.base_url = base_url
    row.updated_by = actor_id
    row.updated_at = utcnow()
    db.flush()

    if created:
        changed.append("created")
    return row, changed


class RuntimeLlm:
    """按调用时解析配置的 LLM 门面。接口与 :class:`DeepSeekClient` 一致。

    刻意不改调用方：``app.state.llm`` 与后台 worker 拿到的仍是同一个对象，
    只是这个对象从「一个固定 client」变成「一个会看配置的 client 工厂」。
    """

    CACHE_TTL_SECONDS = 5.0

    def __init__(self, session_factory: sessionmaker[Session] | None,
                 settings: Settings, *, ttl_seconds: float | None = None):
        self._session_factory = session_factory
        self._settings = settings
        self._ttl = self.CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._lock = threading.Lock()
        self._cached: EffectiveLlm | None = None
        self._expires_at = 0.0
        self._generation = 0
        self._cached_generation = -1
        self._clients: dict[tuple, DeepSeekClient] = {}

    # ---- 配置解析 -------------------------------------------------------
    def current(self) -> EffectiveLlm:
        now = time.monotonic()
        with self._lock:
            hit = (self._cached is not None
                   and self._cached_generation == self._generation
                   and now < self._expires_at)
            if hit:
                return self._cached
        resolved = self._read()
        with self._lock:
            self._cached = resolved
            self._cached_generation = self._generation
            self._expires_at = time.monotonic() + self._ttl
        return resolved

    def invalidate(self) -> None:
        """配置写入后调用：下一次 :meth:`current` 必回读 DB。同进程内立即生效。"""
        with self._lock:
            self._generation += 1

    def _read(self) -> EffectiveLlm:
        if self._session_factory is None:
            return _from_settings(self._settings)
        try:
            with self._session_factory() as session:
                return resolve_effective(session, self._settings)
        except Exception:  # noqa: BLE001
            # 读配置失败不能把 LLM 调用一起拖死：退回落 env 的行为，
            # 但必须留下日志 —— 否则「页面改了没生效」将无从排查。
            logger.warning("llm_config 读取失败，回落 env 配置", exc_info=True)
            return _from_settings(self._settings)

    def _client(self, cfg: EffectiveLlm) -> DeepSeekClient:
        key = (cfg.api_key, cfg.base_url, cfg.model, cfg.timeout_seconds)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = DeepSeekClient(
                    api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model,
                    timeout_seconds=cfg.timeout_seconds,
                )
                # 只留当前一个：旧 client 的 httpx 连接池不该跟着配置一起长存。
                self._clients = {key: client}
            return client

    # ---- DeepSeekClient 同形接口 ---------------------------------------
    def complete_json(self, system_prompt: str, user_prompt: str, *,
                      schema_name: str) -> dict:
        return self._client(self.current()).complete_json(
            system_prompt, user_prompt, schema_name=schema_name)

    def probe(self) -> float:
        return self._client(self.current()).probe()
