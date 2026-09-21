"""Recupera vínculos perdidos com notas existentes; nunca inclui ou emite NF."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from flask import current_app

from app.extensions import db
from app.models import FaturaB2B, PedidoLoja, PedidoOnline, TentativaNFB2B, VendaB2B
from app.services import tiny
from app.services.cobrancas_trava import chave_documento, trava
from app.utils import agora


def _numero(valor):
    texto = str(valor or '').strip().replace('.', '')
    return int(texto) if texto.isdigit() else None


def _itens_por_sku(itens):
    agrupados = {}
    for linha in itens:
        item = linha['item']
        codigo = str(item.get('codigo') or '').strip()
        quantidade = Decimal(str(item['quantidade']))
        preco = Decimal(str(item['valor_unitario'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if not codigo or not quantidade.is_finite() or quantidade <= 0 or not preco.is_finite() or preco < 0:
            raise ValueError('Item fiscal incompleto.')
        chave = (codigo, preco)
        agrupados[chave] = agrupados.get(chave, Decimal('0')) + quantidade
    return agrupados


def _conferir_itens(doc, nf):
    from app.services.tiny_nf_b2b import _payload_itens

    esperados = []
    vendas = doc.vendas if isinstance(doc, FaturaB2B) else [doc]
    for venda in vendas:
        itens, faltantes = _payload_itens(venda)
        if faltantes:
            raise ValueError('Confira o cadastro dos itens no Tiny antes de recuperar a NF.')
        esperados.extend(itens)
    try:
        confere = bool(esperados) and _itens_por_sku(esperados) == _itens_por_sku(nf.get('itens') or [])
    except (InvalidOperation, ValueError, KeyError, TypeError):
        confere = False
    if not confere:
        raise ValueError('Os itens da NF localizada não conferem com esta venda ou fatura. '
                         'Confira produtos, quantidades e preços antes de vincular.')


def recuperar(doc):
    """Chamado sob a trava do documento; só vincula correspondência autorizada única."""
    from app.services.cobrancas_nf import assinatura_documento

    numero = _numero(doc.nf_numero)
    cnpj = tiny._so_digitos(doc.cliente.cnpj_cpf if doc.cliente else '')
    if not numero or len(cnpj) not in (11, 14):
        raise ValueError('Preencha o número da NF e o CPF/CNPJ do cliente para localizar a nota existente.')
    tentativa = db.session.get(TentativaNFB2B, chave_documento(doc))
    if tentativa and tentativa.assinatura and tentativa.assinatura != assinatura_documento(doc):
        raise ValueError('Os dados da venda ou fatura mudaram após a tentativa de emissão. '
                         'Confira os itens e valores antes de recuperar a NF.')
    notas = tiny.pesquisar_notas_fiscais(doc.nf_numero, cnpj)
    if len(notas) != 1 or not notas[0].get('id'):
        raise ValueError('Não foi encontrada uma única NF para este número e cliente no Tiny. '
                         'Confira o número antes de vincular a nota.')
    nota_id = str(notas[0]['id'])
    with trava(f'vinculo-nf:{nota_id}'):
        nf = tiny.obter_nota_fiscal(nota_id) or {}
        try:
            valor_confere = Decimal(str(nf.get('valor_nota'))) == Decimal(str(doc.valor_total))
        except (InvalidOperation, ValueError, TypeError):
            valor_confere = False
        if (str(nf.get('id')) != nota_id or _numero(nf.get('numero')) != numero
                or _numero(nf.get('serie')) != _numero(current_app.config.get('NF_SERIE', '1'))
                or nf.get('tipo_nota') != 'N' or str(nf.get('finalidade')) != '1'
                or tiny._so_digitos((nf.get('cliente') or {}).get('cpf_cnpj')) != cnpj
                or not valor_confere):
            raise ValueError('A NF localizada não confere com o número, série, cliente ou valor deste documento. '
                             'O vínculo foi preservado para conferência.')
        if not tiny.classificar_situacao_nota(nf)['autorizada']:
            raise ValueError('A nota localizada ainda não está autorizada no Tiny. '
                             'Corrija a nota existente e consulte novamente.')
        _conferir_itens(doc, nf)
        for modelo in (VendaB2B, FaturaB2B, PedidoOnline, PedidoLoja):
            vinculadas = modelo.query.filter_by(tiny_nota_fiscal_id=nota_id)
            if isinstance(doc, modelo):
                vinculadas = vinculadas.filter(modelo.id != doc.id)
            if vinculadas.first():
                raise ValueError('Esta NF já está vinculada a outra venda, fatura ou pedido. Confira a origem da nota.')
        doc.tiny_nota_fiscal_id = nota_id
        doc.nf_numero = str(nf['numero'])[:50]
        doc.nf_status = 'autorizada'
        doc.nf_emitida_em = doc.nf_emitida_em or agora()
        if tentativa:
            tentativa.estado, tentativa.erro = 'concluida', None
        db.session.commit()
    return {'ok': True, 'autorizada': True,
            'msg': 'NF existente localizada no Tiny e vinculada novamente. A nota está autorizada.'}
