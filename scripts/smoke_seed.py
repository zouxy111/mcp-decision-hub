# scripts/smoke_seed.py
"""Prepare smoke users and agent tokens against the dev database.

Usage: uv run python scripts/smoke_seed.py
Idempotent: skips users that already exist, prints fresh tokens on every run
(tokens are one-time visible; old ones stay valid until revoked).
"""

from sqlalchemy import select

from hub.api.tokens import issue_token
from hub.config import load_settings
from hub.db.models import User
from hub.db.session import init_db, make_engine, make_session_factory

USERS = ["smoke_init", "smoke_alice", "smoke_bob"]


def main() -> None:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    from argon2 import PasswordHasher

    with factory() as session:
        hasher = PasswordHasher()
        for name in USERS:
            exists = session.scalar(select(User).where(User.username == name))
            if exists is None:
                session.add(
                    User(username=name, email=f"{name}@example.com",
                         password_hash=hasher.hash("smoke-pw-123"),
                         is_admin=False, is_active=True,
                         must_change_password=False)
                )
        session.commit()
        ids = {
            name: session.scalar(select(User).where(User.username == name)).id
            for name in USERS
        }
        _, token_a = issue_token(session, user=session.get(User, ids["smoke_alice"]),
                                 name="smoke-a")
        _, token_b = issue_token(session, user=session.get(User, ids["smoke_bob"]),
                                 name="smoke-b")
        session.commit()
        print("users ready:", ids)
        print("SMOKE_TOKEN_ALICE=" + token_a)
        print("SMOKE_TOKEN_BOB=" + token_b)


if __name__ == "__main__":
    main()
