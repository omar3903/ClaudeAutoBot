"""Engine / session management.

Primary target is MySQL via PyMySQL (``mysql+pymysql://``). If MySQL is
unreachable and ``DB_ALLOW_SQLITE_FALLBACK`` is set, we transparently fall
back to a local SQLite file so the app still runs for development.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator, Optional

from sqlalchemy import create_engine, event, literal
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models_orm import Base

log = logging.getLogger(__name__)

# SQLite lets one connection write at a time; another that wants to write meanwhile waits this long for it before
# giving up with "database is locked" - sqlite3's own wait is 5 s, short enough for another writer's long transaction
# to turn a fill's booking away. The journal mode is left as it is: the database can sit in a synced folder
# (OneDrive), where WAL's shared-memory file isn't safe
SQLITE_BUSY_S = 30


def _connect_args(url: str) -> dict:
    """What a new connection to ``url`` is opened with: SQLite's wait for another writer, nothing for MySQL."""
    return {"timeout": SQLITE_BUSY_S} if make_url(url).get_backend_name() == "sqlite" else {}


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
                                future=True, connect_args=_connect_args(want))
            with eng.connect() as c:  # force a real connection
                c.exec_driver_sql("SELECT 1")
            self.engine = eng
            self.url = want
        except Exception as e:  # noqa: BLE001
            if url is None and s.secrets.db_allow_sqlite_fallback:
                fb = s.secrets.sqlite_fallback_url()
                log.warning("MySQL unavailable (%s) - falling back to %s", e, fb)
                self.engine = create_engine(fb, echo=echo, future=True, connect_args=_connect_args(fb))
                self.url = fb
            else:
                raise

        self.dialect = self.engine.dialect.name
        if self.dialect == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _fk(dbapi_con, _):  # noqa: ANN001
                dbapi_con.execute("PRAGMA foreign_keys=ON")
                # the same wait as the connect argument, set as SQLite's own setting on each new connection too,
                # whatever the connection was opened with
                dbapi_con.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_S * 1000}")

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False,
                                         class_=Session, future=True)
        return self.engine

    def create_all(self) -> None:
        if self.engine is None:
            self.init()
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        log.info("schema ensured on %s", self.dialect or self.url)

    def _add_missing_columns(self) -> None:
        """Lightweight forward-only migration: ADD COLUMN for any mapped column
        not yet in the live table. Never drops or alters. Covers SQLite + MySQL
        so an existing dev database picks up new fields without a rebuild."""
        from sqlalchemy import inspect as _inspect

        insp = _inspect(self.engine)
        try:
            existing_tables = set(insp.get_table_names())
        except Exception:  # noqa: BLE001
            return
        dialect = self.engine.dialect
        # quotes a name only when it needs it (an SQL keyword such as "by"), the database's own way
        q = dialect.identifier_preparer.quote
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                try:
                    coltype = col.type.compile(dialect=dialect)
                    default = ""
                    if col.default is not None and getattr(col.default, "is_scalar", False):
                        # SQLAlchemy writes the value the database's way ('it''s', 1/0 or true/false),
                        # which Python's repr() doesn't; one it can't write is logged below, not fatal
                        lit = literal(col.default.arg).compile(dialect=dialect,
                                                               compile_kwargs={"literal_binds": True})
                        default = f" DEFAULT {lit}"
                    ddl = f"ALTER TABLE {q(table.name)} ADD COLUMN {q(col.name)} {coltype}{default}"
                    with self.engine.begin() as conn:
                        conn.exec_driver_sql(ddl)
                    log.info("migrated: %s", ddl)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not add column %s.%s: %s", table.name, col.name, e)

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
