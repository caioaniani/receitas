"""Cobrança Sicredi (banco 748) — CNAB 400 + boleto híbrido (04/07/2026).

Fonte: manuais oficiais do Sicredi (COBManualCNAB400 + COBBoletoHibrido)
entregues na homologação. Convenções do layout:
- Linha de EXATAMENTE 400 caracteres + CRLF.
- Numérico: alinhado à direita, zeros à esquerda. Alfanumérico: alinhado à
  esquerda, espaços à direita, SEM acentos/caracteres especiais.
- Nosso número (emissão pelo beneficiário): AA B NNNNN D — ano(2) + byte(1,
  2-9; '1' é reservado ao banco) + sequencial(5) + DV módulo 11 calculado
  sobre agência(4)+posto(2)+beneficiário(5)+ano(2)+byte(1)+sequencial(5)
  (manual §4.4/4.5). Fixture real: boleto-modelo do banco tem ag 0101,
  posto 19, benef 00207, nosso nº 21/103527-5 → DV 5 (travado em teste).
- Config por env (Railway): SICREDI_AGENCIA, SICREDI_POSTO,
  SICREDI_BENEFICIARIO, SICREDI_CNPJ, SICREDI_BYTE (default '2').

Retorno: ocorrência 02=registrada, 06/15/17=liquidação (dá baixa na
cobrança E na parcela B2B), 09/10=baixada, 03=rejeitada (motivo salvo).
Registro tipo 8 (híbrido) traz TXID/URL/copia-e-cola do QR Pix — leitura
OBRIGATÓRIA quando a impressão é do beneficiário (nosso caso).
"""
import logging
import os
import unicodedata
from datetime import date
from decimal import Decimal

from app.extensions import db
from app.models import Cobranca, CobrancaRemessa
from app.utils import hoje

logger = logging.getLogger(__name__)

OCORR_REGISTRADA = {'02'}
OCORR_LIQUIDACAO = {'06', '15', '17'}
OCORR_BAIXA = {'09', '10'}
OCORR_REJEITADA = {'03', '24'}


def _cfg():
    return {
        'agencia': os.environ.get('SICREDI_AGENCIA', '0726'),
        'posto': os.environ.get('SICREDI_POSTO', '61'),
        'beneficiario': os.environ.get('SICREDI_BENEFICIARIO', '34325'),
        'cnpj': ''.join(c for c in os.environ.get(
            'SICREDI_CNPJ', '40646899000139') if c.isdigit()),
        'byte': os.environ.get('SICREDI_BYTE', '2'),
    }


def _ascii(s, maxlen):
    """Sem acento, maiúsculo, só o que o CNAB aceita; corta em maxlen."""
    s = unicodedata.normalize('NFD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c)).upper()
    s = ''.join(c if c.isalnum() or c in ' ./-,' else ' ' for c in s)
    return s[:maxlen]


def _num(v, tam):
    return str(int(v)).rjust(tam, '0')[:tam]


def _alfa(v, tam):
    return _ascii(v, tam).ljust(tam)


def _centavos(valor):
    return int(round(Decimal(str(valor)) * 100))


def dv_nosso_numero(agencia, posto, beneficiario, ano2, byte, seq5):
    """Módulo 11 (manual §4.5): pesos 2..9 da direita pra esquerda sobre
    aaaa+pp+ccccc+yy+b+nnnnn. Posto alfanumérico entra como '00'.
    Resto 10 ou 11 → DV 0."""
    posto = posto if str(posto).isdigit() else '00'
    base = (_num(agencia, 4) + _num(posto, 2) + _num(beneficiario, 5)
            + _num(ano2, 2) + str(byte) + _num(seq5, 5))
    soma, peso = 0, 2
    for ch in reversed(base):
        soma += int(ch) * peso
        peso = 2 if peso == 9 else peso + 1
    dv = 11 - (soma % 11)
    return 0 if dv in (10, 11) else dv


def proximo_nosso_numero(ano=None):
    """Gera o próximo nosso número (9 dígitos com DV) pro ano corrente.
    Sequencial nunca repete: max(existentes do ano+byte) + 1."""
    c = _cfg()
    ano2 = f'{(ano or hoje().year) % 100:02d}'
    prefixo = ano2 + c['byte']
    ultimo = (db.session.query(db.func.max(Cobranca.nosso_numero))
              .filter(Cobranca.nosso_numero.like(prefixo + '%'))
              .scalar())
    seq = int(ultimo[3:8]) + 1 if ultimo else 1
    if seq > 99999:
        raise ValueError('Sequencial do nosso número esgotado no ano.')
    dv = dv_nosso_numero(c['agencia'], c['posto'], c['beneficiario'],
                         ano2, c['byte'], seq)
    return f'{prefixo}{seq:05d}{dv}'


def _linha(campos, tam=400):
    """Monta a linha a partir de [(inicio, fim, valor_str)] (posições 1-based
    do manual). Confere sobreposição e comprimento — CNAB não perdoa."""
    buf = [' '] * tam
    for ini, fim, valor in campos:
        assert len(valor) == fim - ini + 1, \
            f'campo {ini}-{fim}: {len(valor)} != {fim - ini + 1} ({valor!r})'
        buf[ini - 1:fim] = list(valor)
    linha = ''.join(buf)
    assert len(linha) == tam
    return linha


def _header_remessa(numero_remessa, seq):
    c = _cfg()
    return _linha([
        (1, 1, '0'), (2, 2, '1'), (3, 9, 'REMESSA'), (10, 11, '01'),
        (12, 19, 'COBRANCA'), (27, 31, _num(c['beneficiario'], 5)),
        (32, 45, _num(c['cnpj'], 14)), (77, 79, '748'),
        (80, 94, _alfa('SICREDI', 15)),
        (95, 102, hoje().strftime('%Y%m%d')),
        (111, 117, _num(numero_remessa, 7)), (391, 394, '2.00'),
        (395, 400, _num(seq, 6)),
    ])


def _detalhe_titulo(cob, seq):
    """Registro tipo 1 — cadastro de título (instrução 01), boleto HÍBRIDO,
    impressão pelo beneficiário (B) com postagem própria (N)."""
    tipo_insc = '2' if len(''.join(
        ch for ch in cob.pagador_cnpj_cpf if ch.isdigit())) == 14 else '1'
    doc = ''.join(ch for ch in cob.pagador_cnpj_cpf if ch.isdigit())
    cep = ''.join(ch for ch in (cob.pagador_cep or '') if ch.isdigit())
    return _linha([
        (1, 1, '1'), (2, 2, 'A'), (3, 3, 'A'), (4, 4, 'A'), (6, 6, 'H'),
        (17, 17, 'A'), (18, 18, 'A'), (19, 19, 'A'),
        (48, 56, cob.nosso_numero),
        (63, 70, hoje().strftime('%Y%m%d')),
        (72, 72, 'N'), (74, 74, 'B'),
        (75, 76, '00'), (77, 78, '00'),
        (83, 92, _num(0, 10)),          # desconto antecipação
        (93, 96, _num(0, 4)),           # multa %
        (109, 110, '01'),               # instrução: cadastro de título
        (111, 120, _alfa(cob.seu_numero, 10)),
        (121, 126, cob.vencimento.strftime('%d%m%y')),
        (127, 139, _num(_centavos(cob.valor), 13)),
        (149, 149, 'A'),                # duplicata mercantil por indicação
        (150, 150, 'N'),
        (151, 156, cob.emissao.strftime('%d%m%y')),
        (157, 158, '00'), (159, 160, '00'),
        (161, 173, _num(0, 13)),        # juros/dia
        (174, 179, _num(0, 6)),
        (180, 192, _num(0, 13)),        # desconto
        (193, 194, '00'), (195, 196, '00'),
        (197, 205, _num(0, 9)),
        (206, 218, _num(0, 13)),        # abatimento
        (219, 219, tipo_insc), (220, 220, '0'),
        (221, 234, _num(doc, 14)),
        (235, 274, _alfa(cob.pagador_nome, 40)),
        (275, 314, _alfa(cob.pagador_endereco or '', 40)),
        (315, 319, _num(0, 5)), (320, 325, _num(0, 6)),
        (327, 334, _num(cep or 0, 8)),
        (335, 339, _num(0, 5)),
        # 340-353: CNPJ/CPF do BENEFICIÁRIO FINAL (sacador avalista).
        # Não usamos beneficiário final — e a homologação (07/07/2026,
        # Luiz Henrique/Sicredi) devolveu o arquivo porque o campo ia em
        # BRANCO: sem beneficiário final ele deve ir "00000000000000".
        # O nome (354-394) segue em branco.
        (340, 353, _num(0, 14)),
        (395, 400, _num(seq, 6)),
    ])


def _trailer_remessa(seq):
    c = _cfg()
    return _linha([
        (1, 1, '9'), (2, 2, '1'), (3, 5, '748'),
        (6, 10, _num(c['beneficiario'], 5)),
        (395, 400, _num(seq, 6)),
    ])


def validar_para_remessa(cob):
    """O que o banco recusa, a gente recusa ANTES (homologação limpa)."""
    erros = []
    if cob.parcela and cob.parcela.venda.sem_cobranca:
        erros.append(f'#{cob.id}: divulgação sem cobrança — não pode gerar remessa.')
    doc = ''.join(ch for ch in (cob.pagador_cnpj_cpf or '') if ch.isdigit())
    if len(doc) not in (11, 14):
        erros.append(f'#{cob.id} {cob.pagador_nome}: CPF/CNPJ inválido.')
    # Relatório da homologação (06/07/2026): enderecoPagador (275-314 do
    # detalhe) é OBRIGATÓRIO — o banco devolveu o arquivo por ele ir vazio.
    if not _ascii(cob.pagador_endereco, 40).strip():
        erros.append(f'#{cob.id} {cob.pagador_nome}: endereço do pagador '
                     'obrigatório (o Sicredi rejeita sem ele) — edite a '
                     'cobrança.')
    cep = ''.join(ch for ch in (cob.pagador_cep or '') if ch.isdigit())
    if len(cep) != 8:
        erros.append(f'#{cob.id} {cob.pagador_nome}: CEP obrigatório '
                     '(8 dígitos) — edite a cobrança.')
    if (cob.vencimento - cob.emissao).days < 7:
        erros.append(f'#{cob.id} {cob.pagador_nome}: o Sicredi exige '
                     'vencimento no mínimo 7 dias após a emissão.')
    if not cob.valor or cob.valor <= 0:
        erros.append(f'#{cob.id} {cob.pagador_nome}: valor inválido.')
    return erros


def gerar_remessa(cobrancas, user_id=None):
    """Gera o arquivo de remessa (cadastro, instrução 01) das cobranças
    `pendente`. Atribui nosso número a quem não tem, valida tudo, grava a
    CobrancaRemessa (sequencial + conteúdo) e move as cobranças pra status
    'remessa'. Retorna (remessa, erros) — com erros, NADA é gravado."""
    # Mesma ordem de trava da dispensa: venda antes dos títulos. Uma tela
    # aberta antes da classificação não pode mandar a divulgação ao banco.
    from app.models import VendaB2B
    vendas_ids = sorted({c.parcela.venda_id for c in cobrancas if c.parcela})
    if vendas_ids:
        (VendaB2B.query.filter(VendaB2B.id.in_(vendas_ids)).order_by(VendaB2B.id)
         .populate_existing().with_for_update().all())
    for cob in sorted(cobrancas, key=lambda c: c.id):
        db.session.refresh(cob, with_for_update=True)
    alvo = [c for c in cobrancas if c.status == 'pendente']
    if not alvo:
        return None, ['Nenhuma cobrança pendente selecionada.']
    erros = []
    for cob in alvo:
        erros.extend(validar_para_remessa(cob))
    if erros:
        db.session.rollback()
        return None, erros

    for cob in alvo:
        if not cob.nosso_numero:
            cob.nosso_numero = proximo_nosso_numero()
            db.session.flush()          # reserva o sequencial p/ o próximo

    numero = (db.session.query(
        db.func.coalesce(db.func.max(CobrancaRemessa.numero), 0))
        .scalar()) + 1
    linhas = [_header_remessa(numero, 1)]
    for i, cob in enumerate(alvo, start=2):
        linhas.append(_detalhe_titulo(cob, i))
    linhas.append(_trailer_remessa(len(alvo) + 2))
    conteudo = '\r\n'.join(linhas) + '\r\n'

    rem = CobrancaRemessa(numero=numero, n_titulos=len(alvo),
                          conteudo=conteudo, gerado_por_id=user_id)
    db.session.add(rem)
    db.session.flush()
    for cob in alvo:
        cob.status = 'remessa'
        cob.remessa_id = rem.id
    db.session.commit()
    return rem, []


def _achar_cobranca(nosso15):
    """Retorno traz o nosso número em 15 posições 'sem edição' — casa pelos
    9 dígitos finais (zeros à esquerda fora)."""
    n = (nosso15 or '').strip().lstrip('0')
    if not n:
        return None
    n = n[-9:].rjust(9, '0') if len(n) >= 9 else n.rjust(9, '0')
    return Cobranca.query.filter_by(nosso_numero=n).first()


def processar_retorno(texto, user_id=None):
    """Processa um arquivo de retorno CNAB400 do Sicredi. Idempotente por
    estado (liquidar 2x não re-quita). Retorna resumo dict."""
    res = {'registradas': 0, 'pagas': 0, 'baixadas': 0, 'rejeitadas': 0,
           'qrcode': 0, 'ignoradas': 0, 'nao_encontradas': 0, 'detalhes': []}
    for linha in texto.splitlines():
        if not linha.strip():
            continue
        tipo = linha[0]
        if tipo == '1':
            cob = _achar_cobranca(linha[47:62])
            if cob is None:
                res['nao_encontradas'] += 1
                continue
            ocorr = linha[108:110]
            if ocorr in OCORR_LIQUIDACAO:
                if cob.status != 'paga':
                    valor_pago = Decimal(linha[253:266] or '0') / 100
                    cob.status = 'paga'
                    cob.valor_pago = valor_pago
                    try:
                        cob.pago_em = date(2000 + int(linha[114:116]),
                                           int(linha[112:114]),
                                           int(linha[110:112]))
                    except ValueError:
                        cob.pago_em = hoje()
                    aviso = _quitar_parcela(cob)
                    res['pagas'] += 1
                    res['detalhes'].append(
                        f'{cob.nosso_numero_fmt} {cob.pagador_nome}: '
                        f'PAGA R$ {cob.valor_pago}')
                    if aviso:
                        res['detalhes'].append(f'⚠ {aviso}')
            elif ocorr in OCORR_REGISTRADA:
                if cob.status == 'remessa':
                    cob.status = 'registrada'
                    res['registradas'] += 1
            elif ocorr in OCORR_BAIXA:
                if cob.status not in ('paga', 'baixada'):
                    cob.status = 'baixada'
                    res['baixadas'] += 1
            elif ocorr in OCORR_REJEITADA:
                cob.status = 'rejeitada'
                cob.motivo_retorno = (f'ocorr {ocorr} motivo '
                                      f'{linha[318:328].strip()}')
                res['rejeitadas'] += 1
                res['detalhes'].append(
                    f'{cob.nosso_numero_fmt} {cob.pagador_nome}: '
                    f'REJEITADA ({cob.motivo_retorno})')
            else:
                res['ignoradas'] += 1
        elif tipo == '8':
            cob = _achar_cobranca(linha[1:16])
            if cob is None:
                res['nao_encontradas'] += 1
                continue
            cob.pix_txid = linha[20:55].strip() or cob.pix_txid
            cob.pix_url = linha[56:133].strip() or cob.pix_url
            cob.pix_copia_cola = linha[134:390].strip() or cob.pix_copia_cola
            res['qrcode'] += 1
    db.session.commit()
    return res


def _quitar_parcela(cob):
    """Liquidação do boleto quita o vínculo B2B (best-effort): parcela
    avulsa OU a fatura mensal inteira (rateio pelas parcelas do
    fechamento). Devolve um aviso (str) quando o valor pago diverge do
    esperado — o caller anexa nos detalhes do retorno."""
    from app.utils import agora
    if cob.fatura:
        from app.services import faturas_b2b
        return faturas_b2b.quitar_fatura(cob.fatura,
                                         valor_pago=cob.valor_pago,
                                         quando=agora())
    p = cob.parcela
    if p is None or p.pago_em:
        return None
    p.valor_pago = cob.valor_pago or cob.valor
    p.pago_em = agora()
    p.forma_pagamento = 'boleto'
    return None
