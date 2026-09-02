"""Locks entre workers também para ações manuais; sobrevivem a commits internos.

Postgres: advisory lock na MESMA conexão até o fim. SQLite: flock local,
inclusive entre processos. Não usa timeout longo dentro de requisições web.
"""
import hashlib
import os
import tempfile
from contextlib import contextmanager

from sqlalchemy import text

from app.extensions import db


class OperacaoEmAndamento(ValueError):
    pass


@contextmanager
def trava(chave):
    chave = 'cobrancas-b2b-v1:' + chave
    numero = int.from_bytes(hashlib.sha256(chave.encode()).digest()[:8], 'big', signed=True)
    if db.engine.dialect.name == 'postgresql':
        with db.engine.connect() as conn:
            conseguiu = conn.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': numero}).scalar()
            if not conseguiu:
                raise OperacaoEmAndamento('Esta operação está em andamento. Aguarde e atualize a tela.')
            try:
                yield
            finally:
                try:
                    conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': numero})
                except Exception:
                    conn.invalidate()  # desconectar libera o lock, nunca devolvê-lo preso ao pool
                    raise
    else:
        import fcntl
        banco = hashlib.sha256(str(db.engine.url).encode()).hexdigest()[:16]
        caminho = os.path.join(tempfile.gettempdir(), f'padaria-{banco}-{numero}.lock')
        fd = os.open(caminho, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperacaoEmAndamento('Esta operação está em andamento. Aguarde e atualize a tela.') from None
            yield
        finally:
            os.close(fd)


def chave_documento(documento):
    from app.models import FaturaB2B
    return f'{"fatura" if isinstance(documento, FaturaB2B) else "venda"}:{documento.id}'
