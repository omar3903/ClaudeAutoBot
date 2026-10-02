from __future__ import annotations

import logging
import logging.handlers
import re
import sys

from ..config import LOG_DIR
from ..secrets_store import mask

_CONFIGURED = False

#: an IBKR account id: U1234567 is a live account, DU1234567 a paper one (F/DF an advisor's, I/DI a broker's)
_ACCOUNT_ID = re.compile(r"\bD?[UFI]\d{5,9}\b")


class _MaskedFormatter(logging.Formatter):
    """Writes every IBKR account id in a line masked (…4567), its traceback included. ib_async's own
    warnings print whole orders and fills - the account with them - when IBKR rejects or cancels an order,
    and the log is a plain file that ends up pasted into an issue or a chat."""

    def format(self, record: logging.LogRecord) -> str:
        return _ACCOUNT_ID.sub(lambda m: mask(m.group()), super().format(record))


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        logging.getLogger().setLevel(level)
        return

    # logs/ unless ATB_LOG_DIR says otherwise - the tests point it at their own folder
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = _MaskedFormatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    # Windows consoles default to cp1252 and choke on stray unicode in logs.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except Exception:  # noqa: BLE001
            pass

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    fileh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "autotradebot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    # third-party noise - ib_async logs every position, execution and order status at INFO
    for noisy in ("httpx", "urllib3", "asyncio", "ib_async"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
