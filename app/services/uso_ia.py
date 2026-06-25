"""Registro e agregacao de uso/custo das chamadas de IA (Anthropic) por funcao.

Cada chamada de modelo chama `registrar(funcao, modelo, response.usage)` e isso
persiste tokens + custo em USD na tabela `UsoIA`. Objetivo: o dono ver quanto
cada funcao (vigia, auditor, bot, OCR, copilot...) gasta por periodo — algo que
ANTES de 25/06/2026 nao era registrado em lugar nenhum.

Dois cuidados deliberados no `registrar`:
- **Sessao isolada** (`Session(db.engine)`): NUNCA contamina a transacao de
  negocio do chamador. Varias dessas funcoes rodam no meio de fluxo de
  estoque/pedido; um `db.session.commit()` solto ali poderia commitar estado
  parcial. A sessao propria commita so a linha de UsoIA.
- **Best-effort**: qualquer erro e logado e engolido. Medir custo jamais pode
  derrubar o vigia, o bot ou o OCR.

Precos em USD por 1M tokens (skill claude-api, jun/2026). Cache read = 0.1x do
input; cache write (5min) = 1.25x do input — formula oficial da Anthropic.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import UsoIA
from app.utils import agora

logger = logging.getLogger(__name__)

# (input, output) em USD por 1M tokens. Casa por PREFIXO do model id pra
# tolerar sufixos (ex: 'claude-haiku-4-5-20251001').
_PRECOS = {
    'claude-opus-4-8': (Decimal('5'), Decimal('25')),
    'claude-opus-4-7': (Decimal('5'), Decimal('25')),
    'claude-opus-4-6': (Decimal('5'), Decimal('25')),
    'claude-sonnet-4-6': (Decimal('3'), Decimal('15')),
    'claude-sonnet-4-5': (Decimal('3'), Decimal('15')),
    'claude-haiku-4-5': (Decimal('1'), Decimal('5')),
}
_MILHAO = Decimal('1000000')


def _precos(modelo):
    m = (modelo or '').strip()
    for prefixo, precos in _PRECOS.items():
        if m.startswith(prefixo):
            return precos
    return None  # modelo desconhecido — nao da pra precificar com confianca


def calcular_custo(modelo, input_t, output_t, cache_read=0, cache_create=0):
    """Custo em USD (Decimal) de UMA chamada. None se o modelo for desconhecido
    (assim o relatorio sinaliza 'sem preco' em vez de inventar um numero)."""
    pr = _precos(modelo)
    if not pr:
        return None
    p_in, p_out = pr
    return (
        Decimal(int(input_t or 0)) * p_in
        + Decimal(int(output_t or 0)) * p_out
        + Decimal(int(cache_read or 0)) * p_in / 10            # cache read 0.1x
        + Decimal(int(cache_create or 0)) * p_in * Decimal('1.25')  # write 1.25x
    ) / _MILHAO


def registrar(funcao, modelo, usage, *, canal=None):
    """Persiste UMA chamada de IA. `usage` = `response.usage` da SDK Anthropic.

    Best-effort + sessao isolada — silencioso em erro, nunca quebra o fluxo
    chamador nem mexe na transacao de negocio dele.
    """
    try:
        input_t = int(getattr(usage, 'input_tokens', 0) or 0)
        output_t = int(getattr(usage, 'output_tokens', 0) or 0)
        cache_read = int(getattr(usage, 'cache_read_input_tokens', 0) or 0)
        cache_create = int(getattr(usage, 'cache_creation_input_tokens', 0) or 0)
        custo = calcular_custo(modelo, input_t, output_t, cache_read, cache_create)
        with Session(db.engine) as s:
            s.add(UsoIA(
                funcao=funcao,
                modelo=(modelo or '')[:60],
                canal=canal,
                input_tokens=input_t,
                output_tokens=output_t,
                cache_read_tokens=cache_read,
                cache_create_tokens=cache_create,
                custo_usd=custo,
            ))
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception('uso_ia.registrar falhou (funcao=%s modelo=%s)',
                         funcao, modelo)


def resumo(dias=7):
    """Agrega custo + tokens por funcao nos ultimos `dias`. Lista de dicts
    ordenada por custo desc. Usa a sessao normal (leitura, sem efeito)."""
    corte = agora() - timedelta(days=dias)
    rows = (db.session.query(
                UsoIA.funcao,
                func.count(UsoIA.id),
                func.coalesce(func.sum(UsoIA.input_tokens), 0),
                func.coalesce(func.sum(UsoIA.output_tokens), 0),
                func.coalesce(func.sum(UsoIA.cache_read_tokens), 0),
                func.coalesce(func.sum(UsoIA.custo_usd), 0))
            .filter(UsoIA.criado_em >= corte)
            .group_by(UsoIA.funcao)
            .all())
    out = [{
        'funcao': funcao,
        'chamadas': int(n or 0),
        'input_tokens': int(in_t or 0),
        'output_tokens': int(out_t or 0),
        'cache_read_tokens': int(cr or 0),
        'custo_usd': Decimal(custo or 0),
    } for funcao, n, in_t, out_t, cr, custo in rows]
    out.sort(key=lambda d: d['custo_usd'], reverse=True)
    return out


def total_periodo(dias=7):
    """Custo total em USD (Decimal) no periodo — soma de todas as funcoes."""
    return sum((d['custo_usd'] for d in resumo(dias)), Decimal(0))
