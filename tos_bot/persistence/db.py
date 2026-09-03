"""Engine / session management.

Primary target is MySQL via PyMySQL (``mysql+pymysql://``). If MySQL is
unreachable and ``DB_ALLOW_SQLITE_FALLBACK`` is set, we transparently fall
back to a local SQLite file so the app still runs for development.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models_orm import Base

log = logging.getLogger(__name__)


class _DB:
    def __init__(self) -> None:
        self.engine: Optional[Engine] = None
        self.SessionLocal: Optional[sessionmaker] = None
        self.dialect: str = ""
        self.url: str = ""

    # ------------------------------------------------------------------ #
    def init(self, url: Optional[str] = None, echo: Optional[bool] = None) -> Engine:
        s = get_settings()
        want = url or s.secrets.resolved_database_url()
        echo = s.config.database.echo_sql if echo is None else echo

        try:
            eng = create_engine(want, echo=echo, pool_pre_ping=True,
                                pool_size=s.config.database.pool_size, max_overflow=10,
                                future=True)
            with eng.connect() as c:  # force a real connection
                c.exec_driver_sql("SELECT 1")
            self.engine = eng
            self.url = want
        except Exception as e:  # noqa: BLE001
            if url is None and s.secrets.db_allow_sqlite_fallback:
                fb = s.secrets.sqlite_fallback_url()
                log.warning("MySQL unavailable (%s) - falling back to %s", e, fb)
                self.engine = create_engine(fb, echo=echo, future=True)
                self.url = fb
            else:
                raise

        self.dialect = self.engine.dialect.name
        if self.dialect == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _fk(dbapi_con, _):  # noqa: ANN001
                dbapi_con.execute("PRAGMA foreign_keys=ON")

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False,
                                         class_=Session, future=True)
        return self.engine

    def create_all(self) -> None:
        if self.engine is None:
            self.init()
        Base.metadata.create_all(self.engine)
        log.info("schema ensured on %s", self.dialect or self.url)

    def session(self) -> Session:
        if self.SessionLocal is None:
            self.init()
        return self.SessionLocal()  # type: ignore[misc]


DB = _DB()


def get_engine() -> Engine:
    if DB.engine is None:
        DB.init()
    return DB.engine  # type: ignore[return-value]


def init_db() -> None:
    DB.init()
    DB.create_all()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    sess = DB.session()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
