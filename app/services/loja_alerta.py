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
import re
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
        # Poda entradas expiradas quando o dict cresce — evita crescer sem
        # limite sob fluxo de chaves distintas (ex: alerta de endereco vindo
        # do endpoint publico /loja/api/frete).
        if len(_ultimo_envio) > 256:
            for k in [k for k, t in _ultimo_envio.items()
                      if agora - t >= _DEDUP_SEGUNDOS]:
                del _ultimo_envio[k]
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


# Teto/hora GLOBAL de alertas de endereço — o gatilho é o endpoint PÚBLICO
# /loja/api/frete (rate-limit por IP só), então um scanner com endereços
# distintos furaria o dedup por-string. Bounde o total pra não inundar o
# WhatsApp do dono. In-memory por worker (best-effort).
_ENDFALHO_MAX_HORA = 12
_endfalho_ts = []


def _endfalho_sob_teto():
    """True se ainda cabe alertar de endereço nesta hora (poda a janela)."""
    agora = time.monotonic()
    with _lock:
        while _endfalho_ts and agora - _endfalho_ts[0] > 3600:
            _endfalho_ts.pop(0)
        if len(_endfalho_ts) >= _ENDFALHO_MAX_HORA:
            return False
        _endfalho_ts.append(agora)
        return True


_CEP_RE = re.compile(r'(\d{5})[\s.-]?(\d{3})')


def _cep_e_chave(endereco, cep):
    """CEP normalizado (do param OU extraído do endereço) + chave de dedup
    CANÔNICA. Preview (api_frete) e checkout mandam o endereço diferente (CEP
    separado num, concatenado no outro) — normalizar une a MESMA venda perdida
    numa só chave, sem alerta dobrado."""
    cep_d = re.sub(r'\D', '', cep or '')
    end = endereco or ''
    if not cep_d:
        m = _CEP_RE.search(end)
        if m:
            cep_d = m.group(1) + m.group(2)
    cep_fmt = f'{cep_d[:5]}-{cep_d[5:]}' if len(cep_d) == 8 else (cep or None)
    base = ' '.join(_CEP_RE.sub('', end).lower().split()).strip(' ,')
    return cep_fmt, f'endfalho|{base}|{cep_d}'


def _texto_endereco_falho(endereco, cep, contato, impreciso=False):
    """Mensagem do alerta (pura — testável). `impreciso`: o frete resolveu só
    pelo centroide do CEP (a venda NÃO travou, mas o valor pode estar errado)."""
    end = (endereco or '').strip() or 'endereço não informado'
    if impreciso:
        cabeca = ('📍 FRETE IMPRECISO no site — cotado pelo CENTROIDE do CEP '
                  '(o endereço exato não foi localizado)')
        rodape = ('O cliente CONSEGUE comprar, mas o frete saiu por estimativa '
                  'do CEP e pode estar bem errado. Confira e ajuste com ele.')
    else:
        cabeca = '📍 ERRO DE ENDEREÇO no site — cliente pode ter desistido da compra'
        rodape = ('O site não localizou esse endereço no cálculo de frete. '
                  'Confira se dá pra atender e chame o cliente pra fechar.')
    linhas = [cabeca, f'Endereço: {end}']
    if (cep or '').strip():
        linhas.append(f'CEP: {cep.strip()}')
    if (contato or '').strip():
        linhas.append(f'Contato: {contato.strip()}')
    linhas.append(rodape)
    return '\n'.join(linhas)


def _enviar_direto(app, texto):
    """Worker: envia o texto (dedup/teto já checados no agendamento)."""
    try:
        with app.app_context():
            numero = _numero_destino()
            if not numero:
                logger.info('loja_alerta: sem numero de destino, pulando')
                return
            from app.services import zapi
            zapi.enviar_texto(numero, texto)
    except Exception:  # noqa: BLE001
        logger.exception('loja_alerta: falha ao enviar alerta de endereco')


def alertar_endereco_falho(endereco, cep=None, contato=None, impreciso=False):
    """Dispara (async) o alerta ao dono sobre problema de endereço no frete
    (decisão do dono 09/07/2026: "isso pode barrar vendas"). Dois casos:
    `nao_encontrado` (venda travou) e `impreciso=True` (venda passou, mas o
    frete saiu por centroide do CEP e pode estar errado). Best-effort; dedup
    por (endereço+CEP) canônico + teto/hora anti-flood."""
    try:
        if not _ativo():
            return
        cep_fmt, chave = _cep_e_chave(endereco, cep)
        chave = f'{chave}|{"imp" if impreciso else "err"}'
        if not _deve_enviar(chave):          # mesmo alerta recente
            return
        if not _endfalho_sob_teto():         # teto/hora (endpoint público)
            logger.warning('loja_alerta: teto/hora de alerta de endereco '
                           'atingido — %r suprimido', (endereco or '')[:80])
            return
        app = current_app._get_current_object()
        texto = _texto_endereco_falho(endereco, cep_fmt, contato, impreciso)
        _POOL.submit(_enviar_direto, app, texto)
    except Exception:  # noqa: BLE001
        logger.exception('loja_alerta: falha ao agendar alerta de endereco')
