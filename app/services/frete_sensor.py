"""Sensor de geocode do frete (09/07/2026).

Registra cada evento que PODE barrar/errar uma venda no site — endereço não
localizado (venda travou), frete impreciso (cotou pelo centroide do CEP) ou
Google resgatou — pro dono ver o padrão e saber se está perdendo venda. Só
log: sessão ISOLADA (`Session(db.engine)` — nunca contamina a transação do
checkout) e best-effort (qualquer erro é engolido, nunca quebra a venda).

Kill-switch: `FRETE_SENSOR=0`.
"""
import logging
import threading
import time

from sqlalchemy.orm import Session

from app.extensions import db

logger = logging.getLogger(__name__)

# Dedup leve (o /api/frete é PÚBLICO): o MESMO (origem+desfecho+endereço) não
# grava 2x numa janela curta — barra bot/duplo-clique inflando a tabela.
_DEDUP_SEG = 600
_ultimo = {}
_lock = threading.Lock()


def _pode_gravar(chave):
    agora = time.monotonic()
    with _lock:
        if len(_ultimo) > 512:
            for k in [k for k, t in _ultimo.items() if agora - t >= _DEDUP_SEG]:
                del _ultimo[k]
        if agora - _ultimo.get(chave, 0.0) < _DEDUP_SEG:
            return False
        _ultimo[chave] = agora
        return True

# Custo aproximado de 1 chamada REMOTA ao Google Geocoding (USD) — só pra
# estimar o gasto no painel. Atualizar se a Google mudar a tabela.
CUSTO_GOOGLE_USD = 0.005

# Desfechos que valem registrar (os que indicam risco/uso). Sucesso normal da
# cadeia grátis NÃO entra (seria ruído).
DESFECHOS = ('barrado', 'impreciso', 'resolvido_google', 'lalamove_falhou')


def _ativo():
    from flask import current_app
    return str(current_app.config.get('FRETE_SENSOR', '1')).strip().lower() \
        not in ('0', 'false', 'no', '')


def registrar(origem, desfecho, *, endereco=None, cep=None, fonte=None,
              km=None, valor=None, contato=None):
    """Grava um evento do sensor. Best-effort, isolado. `origem`:
    preview|checkout|lalamove. `desfecho`: ver DESFECHOS."""
    try:
        if not _ativo() or desfecho not in DESFECHOS:
            return
        ident = ' '.join((endereco or '').lower().split())[:120]
        if not _pode_gravar(f'{origem}|{desfecho}|{ident}'):
            return
        from app.models import FreteSensor
        with Session(db.engine) as s:
            s.add(FreteSensor(
                origem=origem, desfecho=desfecho, fonte=fonte,
                endereco=(endereco or '')[:300], cep=(cep or '')[:12],
                km=km, valor=valor, contato=(contato or '')[:160]))
            s.commit()
    except Exception:  # noqa: BLE001 — sensor nunca pode quebrar o frete
        logger.exception('frete_sensor.registrar falhou (ignorado)')


def resumo(dias=7):
    """Agrega os eventos dos últimos `dias` pro painel do dono. Devolve dict
    com contagens por desfecho, chamadas/custo Google e os eventos recentes."""
    from datetime import timedelta

    from app.models import AppConfig, FreteSensor
    from app.utils import agora, hoje

    dias = max(1, min(int(dias or 7), 90))
    desde = agora() - timedelta(days=dias)
    eventos = (FreteSensor.query
               .filter(FreteSensor.criado_em >= desde)
               .order_by(FreteSensor.criado_em.desc())
               .all())
    por_desfecho = {}
    for e in eventos:
        por_desfecho[e.desfecho] = por_desfecho.get(e.desfecho, 0) + 1

    # Chamadas remotas ao Google HOJE (contador do frete) + custo estimado.
    dia_iso = hoje().isoformat()
    dia, _, n = (AppConfig.get('frete_google_dia') or '').partition('|')
    google_hoje = int(n) if n.isdigit() and dia == dia_iso else 0

    return {
        'dias': dias,
        'total': len(eventos),
        'barrado': por_desfecho.get('barrado', 0),
        'impreciso': por_desfecho.get('impreciso', 0),
        'resolvido_google': por_desfecho.get('resolvido_google', 0),
        'lalamove_falhou': por_desfecho.get('lalamove_falhou', 0),
        'google_chamadas_hoje': google_hoje,
        'google_custo_hoje_usd': round(google_hoje * CUSTO_GOOGLE_USD, 2),
        'eventos': eventos[:100],
    }
