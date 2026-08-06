"""Database engine and session management (SQLAlchemy 2.0 + PostgreSQL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mercury.core.logging import get_logger

logger = get_logger("core.db")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Database:
    """Owns the SQLAlchemy engine and session factory."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        connect_args: dict[str, Any] = {}
        if url.startswith("postgresql"):
            # Fail fast when Postgres is down instead of hanging on a TCP timeout.
            connect_args["connect_timeout"] = 10
        self.engine: Engine = create_engine(
            url, pool_pre_ping=True, echo=echo, connect_args=connect_args
        )
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings) -> Database:
        return cls(settings.database_url)

    @classmethod
    def in_memory(cls) -> Database:
        """Build an in-memory SQLite database for tests."""
        from sqlalchemy.pool import StaticPool

        db = cls.__new__(cls)
        db.url = "sqlite://"
        db.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        db._session_factory = sessionmaker(bind=db.engine, expire_on_commit=False)
        return db

    def create_tables(self) -> None:
        """Create all tables defined on ``Base`` (idempotent)."""
        from mercury.models import orm  # noqa: F401  (register models)

        Base.metadata.create_all(self.engine)
        logger.info("database tables ensured")

    def dispose(self) -> None:
        self.engine.dispose()
        logger.info("database engine disposed")

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Context manager yielding a committed-or-rolled-back session."""
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Context manager that does NOT auto-commit (caller controls)."""
        session: Session = self._session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
