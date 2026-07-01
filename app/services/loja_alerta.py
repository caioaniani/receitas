"""Alerta IMEDIATO ao dono quando um cliente e BARRADO no checkout do site.

Hoje o unico "trava" que impede a compra e o item ESGOTADO no plano-do-dia pra
a data escolhida (`loja_checkout.py` gate de disponibilidade) — a reserva
fisica nao bloqueia mais (regra do dono 01/07/2026). Quando isso acontece o
cliente ia comprar e nao conseguiu: transformamos essa venda perdida num
WhatsApp na hora, COM o contato do cliente (nome/telefone/email do formulario,
mesmo sem pedido criado) pra o dono chamar e fechar a venda.

Garantias:
- ASSINCRONO: o envio roda fora do request do cliente (ThreadPoolExecutor), o
  checkout nao espera o Z-API.
- BEST-EFFORT: qualquer erro e engolido (log) — NUNCA quebra nem atrasa a venda.
- IMEDIATO: dispara a cada cliente barrado (decisao do dono — "preciso saber
  urgente"). So uma trava leve anti-duplo-clique: o MESMO (cliente+itens) nao
  repete numa janela curta (`_DEDUP_SEGUNDOS`).

Config: `LOJA_ALERTA_TRAVA=0` desliga; destino `LOJA_ALERTA_NUMERO` ou
`ZAPI_NUMERO_DESTINO`.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

logger = logging.getLogger(__name__)

# Envio e fire-and-forget, fora do request do cliente.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='loja-alerta')

# Trava leve anti-duplo-clique (NAO e agrupamento — o dono quer cada cliente na
# hora). So evita que o mesmo cliente reenviando o form em segundos pingue 2x.
# In-memory por worker; sobreviver a deploy nao importa numa janela de minutos.
_DEDUP_SEGUNDOS = 600
_ultimo_envio = {}          # chave -> time.monotonic()
_lock = threading.Lock()


def _ativo():
    return str(current_app.config.get('LOJA_ALERTA_TRAVA', '1')).strip().lower() \
        not in ('0', 'false', 'no', '')


def _numero_destino():
    cfg = current_app.config
    return ((cfg.get('LOJA_ALERTA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())


def _texto_esgotado(nome, telefone, email, itens, data_entrega):
    """Mensagem do alerta (pura — testavel sem Z-API)."""
    nome = (nome or '').strip() or 'Cliente'
    quais = ', '.join(i for i in (itens or []) if i) or 'itens do carrinho'
    data = data_entrega.strftime('%d/%m/%Y') if data_entrega else '—'
    contato = (telefone or '').strip() or 'sem telefone'
    if (email or '').strip():
        contato = f'{contato} · {email.strip()}'
    return (
        '🔔 VENDA BARRADA no site — cliente NÃO conseguiu comprar\n'
        f'Cliente: {nome}\n'
        f'Contato: {contato}\n'
        f'Queria: {quais}\n'
        f'Entrega: {data}\n'
        'Motivo: esgotado no plano-do-dia. Ajuste em '
        '/admin/loja-online/plano-do-dia e chame o cliente pra fechar.'
    )


def _deve_enviar(chave):
    """Dedup leve, thread-safe. True se pode enviar (e marca o envio)."""
    agora = time.monotonic()
    with _lock:
        ult = _ultimo_envio.get(chave, 0.0)
        if agora - ult < _DEDUP_SEGUNDOS:
            return False
        _ultimo_envio[chave] = agora
        return True


def _enviar(app, texto, chave):
    """Worker no pool — best-effort, nunca levanta."""
    try:
        with app.app_context():
            numero = _numero_destino()
            if not numero:
                logger.info('loja_alerta: sem numero de destino, pulando')
                return
            if not _deve_enviar(chave):
                return
            from app.services import zapi
            zapi.enviar_texto(numero, texto)
    except Exception:  # noqa: BLE001
        logger.exception('loja_alerta: falha ao enviar alerta de trava')


def alertar_esgotado(nome, telefone, email, itens, data_entrega):
    """Dispara (async) o alerta de cliente barrado por esgotado. Best-effort:
    engole QUALQUER erro pra nunca afetar o checkout. Chamado de dentro do
    `criar_pedido` no gate de disponibilidade."""
    try:
        if not _ativo():
            return
        app = current_app._get_current_object()
        texto = _texto_esgotado(nome, telefone, email, itens, data_entrega)
        ident = (email or telefone or nome or '').strip().lower()
        chave = f'esgotado|{ident}|{",".join(sorted(i for i in (itens or []) if i))}'
        _POOL.submit(_enviar, app, texto, chave)
    except Exception:  # noqa: BLE001
        logger.exception('loja_alerta: falha ao agendar alerta')
