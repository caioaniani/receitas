"""Alerta de saldo baixo da carteira Lalamove (decisao do dono 15/06/2026).

Acionado pelo webhook `POST /lalamove/webhook` toda vez que a Lalamove manda
o evento `WALLET_BALANCE_CHANGED`. A persistencia do saldo ja era feita; aqui
ficou so a parte do alarme:

  1. Le `LALAMOVE_SALDO_MIN_REAIS` (default R$ 200) — abaixo disso dispara.
  2. Anti-spam via `AppConfig`: NAO realerta se ja avisou nas ultimas X horas
     E o saldo nao caiu mais que Y reais desde o ultimo alerta (cobre o caso
     de queda rapida — alertou em R$180, caiu pra R$80, vale realertar).
  3. Sobe via `zapi.enviar_texto` pro `ZAPI_BOT_DONO_NUMERO` (fallback:
     `ZAPI_NUMERO_DESTINO`).
  4. Desligavel por env: `LALAMOVE_SALDO_ALERTA=0`.

Best-effort: falha aqui NUNCA quebra o webhook (que continua persistindo o
saldo). A logica fica isolada num try/except no caller (routes.py).
"""
import logging
import os
from decimal import Decimal, InvalidOperation

from flask import current_app

from app.constants import (
    LALAMOVE_SALDO_ALERTA_DEDUPE_DELTA_REAIS,
    LALAMOVE_SALDO_ALERTA_DEDUPE_HORAS,
    LALAMOVE_SALDO_MIN_REAIS,
)

logger = logging.getLogger(__name__)

_CHAVE_ULTIMO_EM = 'lalamove_saldo_alerta_ultimo_em'
_CHAVE_ULTIMO_VALOR = 'lalamove_saldo_alerta_ultimo_valor'


def _ligado():
    """Default: ligado. `LALAMOVE_SALDO_ALERTA=0` desliga."""
    return os.environ.get('LALAMOVE_SALDO_ALERTA', '1') != '0'


def _destino():
    cfg = current_app.config
    return ((cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
            or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())


def _para_decimal(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _formatar_reais(valor):
    """1234.5 -> 'R$ 1.234,50'. Pra mensagem do WhatsApp."""
    try:
        s = f'{Decimal(str(valor)):.2f}'
    except (InvalidOperation, ValueError):
        return f'R$ {valor}'
    inteiro, dec = s.split('.')
    # separador de milhar
    inteiro_fmt = f'{int(inteiro):,}'.replace(',', '.')
    return f'R$ {inteiro_fmt},{dec}'


def _dedupe_permite(valor_atual):
    """True se PODE alertar agora. Bloqueia se ja alertou nas ultimas
    LALAMOVE_SALDO_ALERTA_DEDUPE_HORAS *E* o saldo nao caiu mais que
    LALAMOVE_SALDO_ALERTA_DEDUPE_DELTA_REAIS desde o ultimo alerta."""
    from datetime import timedelta

    from app.models import AppConfig
    from app.utils import agora

    ts_str = AppConfig.get(_CHAVE_ULTIMO_EM)
    if not ts_str:
        return True  # nunca alertou
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return True
    janela = timedelta(hours=LALAMOVE_SALDO_ALERTA_DEDUPE_HORAS)
    if agora() - ts >= janela:
        return True  # passou da janela, pode realertar
    # Dentro da janela — so realerta se caiu MUITO desde o ultimo aviso
    ultimo_valor = _para_decimal(AppConfig.get(_CHAVE_ULTIMO_VALOR))
    if ultimo_valor is None:
        return True
    delta = ultimo_valor - _para_decimal(valor_atual)
    return delta >= Decimal(str(LALAMOVE_SALDO_ALERTA_DEDUPE_DELTA_REAIS))


def _registrar_alerta(valor):
    """Persiste timestamp + valor do alerta que acabou de sair."""
    from app.extensions import db
    from app.models import AppConfig
    from app.utils import agora

    AppConfig.set(_CHAVE_ULTIMO_EM, agora().isoformat())
    AppConfig.set(_CHAVE_ULTIMO_VALOR, str(valor))
    db.session.commit()


def _montar_mensagem(valor, moeda):
    """Texto que vai pro WhatsApp."""
    return (
        '⚠️ *Lalamove — saldo baixo*\n'
        '\n'
        f'Carteira: *{_formatar_reais(valor)}* ({moeda or "BRL"})\n'
        f'Limite de alerta: {_formatar_reais(LALAMOVE_SALDO_MIN_REAIS)}\n'
        '\n'
        'Recarregue pra não atrapalhar as entregas:\n'
        'https://www.lalamove.com/business'
    )


def avaliar_e_alertar(valor_novo, moeda='BRL'):
    """Decide se manda o alerta. Retorna dict com o que aconteceu:
      {'alertado': True,  'valor': X}
      {'alertado': False, 'motivo': '...'}

    NUNCA propaga exception — caller (webhook) nao deve quebrar por isso.
    """
    try:
        if not _ligado():
            return {'alertado': False, 'motivo': 'LALAMOVE_SALDO_ALERTA=0'}

        valor = _para_decimal(valor_novo)
        if valor is None:
            return {'alertado': False, 'motivo': 'valor invalido'}

        if valor >= Decimal(str(LALAMOVE_SALDO_MIN_REAIS)):
            return {'alertado': False,
                    'motivo': f'saldo {valor} >= limite {LALAMOVE_SALDO_MIN_REAIS}'}

        if not _dedupe_permite(valor):
            return {'alertado': False, 'motivo': 'dedupe — ja alertou recente'}

        numero = _destino()
        if not numero:
            logger.warning('lalamove_saldo: saldo baixo (%s) mas sem '
                           'ZAPI_BOT_DONO_NUMERO/ZAPI_NUMERO_DESTINO',
                           valor)
            return {'alertado': False, 'motivo': 'sem destino Z-API'}

        from app.services import zapi
        envio = zapi.enviar_texto(numero, _montar_mensagem(valor, moeda))
        if not envio.get('ok'):
            logger.warning('lalamove_saldo: envio Z-API falhou: %s', envio)
            return {'alertado': False, 'motivo': f'zapi: {envio}'}

        _registrar_alerta(valor)
        logger.info('lalamove_saldo: alerta enviado (saldo=%s, destino=%s)',
                    valor, numero)
        return {'alertado': True, 'valor': str(valor)}
    except Exception as exc:  # noqa: BLE001
        logger.exception('lalamove_saldo: avaliar_e_alertar falhou')
        return {'alertado': False, 'motivo': f'erro: {exc}'}
