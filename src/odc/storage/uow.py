# SPDX-License-Identifier: GPL-3.0-only
"""Unit of work simples: sessão curta, commit/rollback e escrita serializada no processo."""

import threading
from types import TracebackType

from sqlalchemy import Engine
from sqlalchemy.orm import Session

# Um escritor por vez no processo: evita `database is locked` além do busy_timeout.
_WRITE_LOCK = threading.Lock()


class UnitOfWork:
    """Context manager que entrega uma `Session` e faz commit (ou rollback em exceção).

    Escritas (`write=True`) adquirem o lock global de escrita; leituras não bloqueiam (WAL).
    Não faça I/O de rede dentro do bloco: a transação deve ser curta.
    """

    def __init__(self, engine: Engine, *, write: bool = True) -> None:
        self._engine = engine
        self._write = write
        self._session: Session | None = None

    def __enter__(self) -> Session:
        if self._write:
            _WRITE_LOCK.acquire()
        try:
            self._session = Session(self._engine, expire_on_commit=False)
        except BaseException:
            self._release()
            raise
        return self._session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self._session
        self._session = None
        try:
            if session is not None:
                try:
                    if exc_type is None and self._write:
                        session.commit()
                    else:
                        session.rollback()
                finally:
                    session.close()
        finally:
            self._release()

    def _release(self) -> None:
        if self._write:
            _WRITE_LOCK.release()
