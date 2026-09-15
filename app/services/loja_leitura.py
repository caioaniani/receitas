"""Leituras em lote durante a exibição de kits, descartadas ao terminar o render.

Não envolve criação de pedidos, reserva, pagamento ou operações do owner.
Cada abertura busca preço, publicação, saldo e horários atuais no banco.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from app.utils import agora

_leitura = ContextVar('loja_leitura', default=None)


def atual():
    return _leitura.get()


@contextmanager
def catalogo_em_lote(*, dias=31, base=None):
    from app.services import loja_catalogo, loja_checkout, loja_data_especial, loja_plano_dia

    base = base or agora()
    inicio = base.date()
    fim = inicio + timedelta(days=dias + loja_checkout.ENCOMENDA_LEAD_DIAS)
    datas = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    produtos = loja_catalogo.produtos_publicados()
    dados = {
        'catalogo': {(p['kind'], p['id']): p for p in produtos},
        'saldos': loja_plano_dia.saldos_no_periodo(inicio, fim),
        'regras': loja_data_especial.regras_do_periodo(datas),
        'inicio': inicio, 'fim': fim,
    }
    token = _leitura.set(dados)
    try:
        yield
    finally:
        _leitura.reset(token)
