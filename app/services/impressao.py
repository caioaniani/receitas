"""Impressão de comandas por setor em térmicas ESC/POS (Jetway e similares).

Quando a venda do caixa vira 'paga', os itens são agrupados pelo setor de
produção (chapa, café, cozinha, viagem...) e cada setor com impressora
cadastrada (model Impressora) recebe sua comanda. O setor especial 'caixa'
recebe um cupom de conferência da venda inteira (não fiscal — a NFC-e
continua saindo pela Seru/Clover).

Transporte: TCP raw na porta 9100 (padrão das térmicas de rede). Impressora
USB pode ser usada compartilhando-a como impressora de rede RAW no
computador em que está ligada (print server).

Texto é normalizado pra ASCII (sem acento) — evita lixo por diferença de
codepage entre modelos de térmica.
"""
import socket
import unicodedata
from datetime import timezone, timedelta

BRT = timezone(timedelta(hours=-3))

# Comandos ESC/POS
_INIT = b'\x1b@'
_BOLD_ON = b'\x1bE\x01'
_BOLD_OFF = b'\x1bE\x00'
_GRANDE_ON = b'\x1d!\x11'    # 2x largura e altura
_GRANDE_OFF = b'\x1d!\x00'
_ALTO_ON = b'\x1d!\x01'      # 2x altura (itens da comanda)
_ALTO_OFF = b'\x1d!\x00'
_CENTRO = b'\x1ba\x01'
_ESQUERDA = b'\x1ba\x00'
_CORTE = b'\n\n\n\x1dV\x42\x00'  # feed + corte parcial

TIMEOUT = 5


def _t(s):
    """Normaliza texto pra bytes ASCII (sem acento)."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    return s.encode('ascii', 'ignore')


def _qtd(q):
    q = q or 0
    return str(int(q)) if float(q).is_integer() else f'{q:g}'


def _hora_brt(dt_utc):
    """datetime UTC naive (como gravamos no banco) -> 'HH:MM' BRT."""
    if not dt_utc:
        return ''
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(BRT).strftime('%H:%M')


def enviar(host, porta, dados, timeout=TIMEOUT):
    """Manda bytes pra impressora (TCP raw). Levanta OSError se falhar."""
    with socket.create_connection((host, int(porta or 9100)), timeout=timeout) as s:
        s.sendall(dados)


def comanda_setor(venda, setor, itens, largura=48):
    """Comanda de produção de um setor: número da venda bem grande,
    hora e itens em letra alta pra leitura rápida na bancada."""
    linha = _t('-' * largura) + b'\n'
    b = bytearray(_INIT)
    b += _CENTRO + _GRANDE_ON + _BOLD_ON + _t(setor.upper()) + b'\n' + _BOLD_OFF + _GRANDE_OFF
    b += _t(f'{venda.code}   {_hora_brt(venda.criado_em)}') + b'\n'
    b += _ESQUERDA + linha
    for i in itens:
        b += _ALTO_ON + _BOLD_ON + _t(f'{_qtd(i.quantidade)} x {i.descricao}') + b'\n'
        b += _BOLD_OFF + _ALTO_OFF
    if venda.observacao:
        b += linha + _t(f'Obs: {venda.observacao}') + b'\n'
    b += linha + _CORTE
    return bytes(b)


def cupom_conferencia(venda, largura=48):
    """Cupom da venda inteira pro caixa — conferência, não é documento fiscal."""
    linha = _t('-' * largura) + b'\n'
    b = bytearray(_INIT)
    b += _CENTRO + _BOLD_ON + _t('PADARIA OPAO') + b'\n' + _BOLD_OFF
    b += _t(f'{venda.code}   {_hora_brt(venda.criado_em)}') + b'\n'
    b += _t('*** NAO E DOCUMENTO FISCAL ***') + b'\n'
    b += _ESQUERDA + linha
    for i in venda.itens:
        esq = f'{_qtd(i.quantidade)} x {i.descricao}'
        dirta = f'{i.subtotal:.2f}'
        espaco = max(largura - len(esq) - len(dirta), 1)
        b += _t(esq + ' ' * espaco + dirta) + b'\n'
    b += linha
    if venda.desconto:
        b += _t(f'Desconto: {venda.desconto:.2f}') + b'\n'
    b += _BOLD_ON + _t(f'TOTAL: R$ {venda.total:.2f}') + b'\n' + _BOLD_OFF
    for p in venda.pagamentos:
        if p.status != 'aprovado':
            continue
        b += _t(f'{p.metodo}: {p.valor:.2f}') + b'\n'
        if p.troco:
            b += _t(f'troco: {p.troco:.2f}') + b'\n'
    b += _CORTE
    return bytes(b)


def teste(impressora):
    """Página de teste pro botão 'testar' da configuração."""
    b = bytearray(_INIT)
    b += _CENTRO + _GRANDE_ON + _t('TESTE OK') + b'\n' + _GRANDE_OFF
    b += _t(f'Setor: {impressora.setor}') + b'\n'
    b += _t(f'{impressora.nome} ({impressora.host}:{impressora.porta})') + b'\n'
    b += _t('AaEeIiOoUuCc 0123456789') + b'\n'
    b += _CORTE
    return bytes(b)
