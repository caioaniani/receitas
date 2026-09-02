"""Criação canônica de títulos, compartilhada pela tela e pela automação."""
from datetime import timedelta

from app.extensions import db
from app.models import Cobranca
from app.utils import hoje


def snapshot_pagador(cli):
    if not cli:
        return '', ''
    endereco = cli.endereco or ''
    if not endereco and cli.endereco_logradouro:
        endereco = ' '.join(x for x in (cli.endereco_logradouro, cli.endereco_numero,
                                      f'- {cli.endereco_bairro}' if cli.endereco_bairro else '') if x)
    return endereco, ''.join(ch for ch in (cli.endereco_cep or '') if ch.isdigit())


def da_parcela(p, usuario_id=None):
    """Não commita: título e intenção de remessa pertencem à mesma transação."""
    venda = p.venda
    db.session.refresh(venda, with_for_update=True)
    db.session.refresh(p, with_for_update=True)
    if venda.sem_cobranca or venda.status == 'cancelada':
        raise ValueError('Venda cancelada ou divulgação sem cobrança: não será gerado boleto.')
    if p.fatura_id or venda.fatura_id:
        raise ValueError('Parcela de fatura mensal: gere o boleto pela fatura, nunca individualmente.')
    existente = Cobranca.query.filter_by(parcela_id=p.id).first()
    if existente:
        return existente, False
    if p.saldo <= 0 or p.valor_pago:
        raise ValueError('Pagamento total ou parcial registrado: confira o saldo antes de gerar o boleto.')
    cli = venda.cliente
    endereco, cep = snapshot_pagador(cli)
    emissao = hoje()
    venc = max(p.vencimento, emissao + timedelta(days=7))
    cob = Cobranca(parcela_id=p.id, pagador_nome=venda.cliente_display[:100],
                   pagador_cnpj_cpf=(cli.cnpj_cpf if cli else '') or '',
                   pagador_endereco=endereco, pagador_cep=cep, valor=p.valor,
                   vencimento=venc, emissao=emissao, seu_numero=f'V{venda.id}P{p.numero}',
                   criado_por_id=usuario_id)
    if len(cob.seu_numero) > 10:
        raise ValueError('Referência do pedido excede o limite do banco. Confira a cobrança.')
    p.vencimento = venc
    db.session.add(cob)
    db.session.flush()
    return cob, True


def da_fatura(fatura, usuario_id=None):
    db.session.refresh(fatura, with_for_update=True)
    if fatura.status != 'fechada':
        raise ValueError('Somente fatura fechada pode gerar boleto.')
    existente = Cobranca.query.filter_by(fatura_id=fatura.id).first()
    if existente:
        return existente, False
    if any(p.valor_pago for p in fatura.parcelas) or fatura.valor_total <= 0:
        raise ValueError('Pagamento ou valor inválido na fatura. Confira o saldo antes de gerar boleto.')
    cli = fatura.cliente
    endereco, cep = snapshot_pagador(cli)
    emissao = hoje()
    venc = max(fatura.vencimento, emissao + timedelta(days=7))
    cob = Cobranca(fatura_id=fatura.id, pagador_nome=cli.nome[:100],
                   pagador_cnpj_cpf=cli.cnpj_cpf or '', pagador_endereco=endereco,
                   pagador_cep=cep, valor=fatura.valor_total, vencimento=venc,
                   emissao=emissao, seu_numero=fatura.codigo[:10], criado_por_id=usuario_id)
    fatura.vencimento = venc
    for p in fatura.parcelas:
        p.vencimento = venc
    db.session.add(cob)
    db.session.flush()
    return cob, True
