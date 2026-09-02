"""Outbox B2B: somente eventos novos. Não varre vendas antigas para emitir.

O upload CNAB continua manual. E-mail ao cliente exige registro bancário.
Não repete efeitos externos incertos; ficam para conferência humana.
"""
from decimal import Decimal
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from markupsafe import escape
from sqlalchemy import or_

from app.extensions import db
from app.models import (
    AppConfig,
    AutomacaoCobranca,
    AvisoRemessa,
    Cobranca,
    CobrancaRemessa,
    ConfirmacaoRegistroBoleto,
    FaturaB2B,
    Usuario,
    VendaB2B,
)
from app.services.cobrancas_trava import OperacaoEmAndamento, chave_documento, trava
from app.utils import agora

RESPONSAVEIS = ('caio@opao.online', 'dakson@opao.online')
URL_PAINEL = 'https://gestao.opaopadariaartesanal.com.br/cobrancas/automacao'
ESTADOS = {
    'pendente': 'Na fila de emissão', 'banco': 'Aguardando registro no Sicredi',
    'erro': 'Precisa de conferência', 'enviado': 'NF + boleto enviados',
    'ignorada': 'Fora da cobrança automática',
}


def enfileirar(doc, usuario_id=None):
    """Mesma transação da entrega/fechamento. Sem commits nem integrações."""
    fatura = isinstance(doc, FaturaB2B)
    if not fatura and (doc.sem_cobranca or doc.status != 'ativa'
                       or doc.fatura_id or not doc.parcelas
                       or (doc.cliente and doc.cliente.faturamento_mensal)):
        return None  # Mensal SEMPRE continua no fechamento, nunca na entrega.
    chave = chave_documento(doc)
    existente = AutomacaoCobranca.query.filter_by(chave=chave).first()
    if existente:
        return existente
    job = AutomacaoCobranca(chave=chave, tipo='fatura' if fatura else 'venda',
                           documento_id=doc.id, usuario_id=usuario_id,
                           referencia=f'Fatura {doc.codigo}' if fatura else f'Venda #{doc.id}')
    db.session.add(job)
    return job


def preparar_avisos(remessa):
    for destino in RESPONSAVEIS:
        if not AvisoRemessa.query.filter_by(remessa_id=remessa.id, destinatario=destino).first():
            db.session.add(AvisoRemessa(remessa_id=remessa.id, destinatario=destino))


def banco_confirmado(c):
    if c.status == 'registrada':
        return True
    a = db.session.get(ConfirmacaoRegistroBoleto, c.id)
    return bool(c.status == 'remessa' and a and
                (a.remessa_id, a.nosso_numero, a.valor, a.vencimento) ==
                (c.remessa_id, c.nosso_numero, c.valor, c.vencimento))


def confirmar_registro(remessa, usuario_id):
    """Atestado explícito do operador; NÃO falsifica retorno CNAB/status."""
    titulos = Cobranca.query.filter_by(remessa_id=remessa.id, status='remessa').with_for_update().all()
    if not titulos:
        raise ValueError('Não há boletos aguardando registro nesta remessa.')
    for c in titulos:
        confirmar_titulo(c, usuario_id)
    db.session.commit()


def confirmar_titulo(c, usuario_id):
    if c.status != 'remessa' or not c.remessa_id or not c.nosso_numero:
        raise ValueError('Boleto sem remessa válida para confirmação.')
    a = db.session.get(ConfirmacaoRegistroBoleto, c.id)
    if a is None:
        a = ConfirmacaoRegistroBoleto(cobranca_id=c.id)
        db.session.add(a)
    a.remessa_id, a.nosso_numero = c.remessa_id, c.nosso_numero
    a.valor, a.vencimento = c.valor, c.vencimento
    a.usuario_id, a.confirmado_em = usuario_id, agora()


def remessas_pendentes():
    c, a = Cobranca, ConfirmacaoRegistroBoleto
    return (CobrancaRemessa.query.join(c, c.remessa_id == CobrancaRemessa.id)
            .outerjoin(a, a.cobranca_id == c.id)
            .filter(c.status == 'remessa', or_(a.cobranca_id.is_(None),
                    a.remessa_id != c.remessa_id, a.nosso_numero != c.nosso_numero,
                    a.valor != c.valor, a.vencimento != c.vencimento))
            .distinct().order_by(CobrancaRemessa.numero).all())


def _validar(doc, job):
    if not doc:
        raise ValueError('Origem excluída. Nada será emitido ou enviado.')
    if job.tipo == 'venda':
        if (doc.status != 'ativa' or doc.sem_cobranca or doc.fatura_id
                or doc.status_entrega != 'entregue'
                or (doc.cliente and doc.cliente.faturamento_mensal)):
            raise ValueError('Venda não elegível: confira entrega, cancelamento, divulgação ou fechamento mensal.')
    elif doc.status != 'fechada':
        raise ValueError('Fatura não está fechada ou já foi paga/cancelada.')
    parcelas = list(doc.parcelas)
    if not parcelas or sum((p.valor for p in parcelas), Decimal('0')) != doc.valor_total or doc.valor_total <= 0:
        raise ValueError('Parcelas e total do documento não conferem.')
    if any(p.valor_pago or p.pago_em or p.valor <= 0 for p in parcelas):
        raise ValueError('Há pagamento total/parcial ou parcela sem valor. Confira antes de cobrar.')
    if any((p.forma_pagamento or '').lower() not in ('', 'boleto') for p in parcelas):
        raise ValueError('Pedido negociado com outra forma de pagamento. Não será criado boleto automaticamente.')
    if not doc.cliente or not doc.cliente.ativo:
        raise ValueError('Cliente ausente ou inativo. Confira o cadastro.')
    from app.services.cobrancas_envio import email_valido
    if not email_valido((doc.cliente.email or '').strip()):
        raise ValueError('Complete o e-mail do cliente antes da emissão automática.')
    from app.services.cobrancas_nf import validar_assinatura
    validar_assinatura(doc)


def _mudar(job, estado, erro=None):
    job.estado, job.erro, job.atualizado_em = estado, str(erro)[:500] if erro else None, agora()
    db.session.commit()


def processar(job):
    from app.services import cobrancas_preparo, tiny_nf_b2b
    from app.services.central_cobrancas import carregar
    from app.services.cobrancas_envio import enviar_automatico
    from app.services.sicredi_cnab import gerar_remessa
    modelo = FaturaB2B if job.tipo == 'fatura' else VendaB2B
    doc = db.session.get(modelo, job.documento_id)
    _validar(doc, job)
    usuario = db.session.get(Usuario, job.usuario_id) if job.usuario_id else None
    uid = usuario.id if usuario else None  # Fonte pode ser o padeiro, não amplie sua permissão.
    actor = SimpleNamespace(id=uid, nome='Automação B2B · entrega/fechamento')
    if not (doc.nf_emitida_em and doc.tiny_nota_fiscal_id):
        fn = tiny_nf_b2b.emitir_nf_fatura if job.tipo == 'fatura' else tiny_nf_b2b.emitir_nf
        result = fn(doc, user_id=uid)
        if not result.get('ok'):
            raise ValueError(result.get('msg') or 'Emissão de NF não confirmada.')
    # Releia depois de integrações/commits. Nunca cobre uma origem cancelada no intervalo.
    db.session.expire_all()
    _validar(doc, job)
    with trava(chave_documento(doc)):
        if job.tipo == 'fatura':
            titulos = [cobrancas_preparo.da_fatura(doc, uid)[0]]
        else:
            titulos = [cobrancas_preparo.da_parcela(p, uid)[0] for p in doc.parcelas]
        for c in titulos:
            r = carregar('boleto', c.id)
            if c.valor != r.saldo or c.status not in ('pendente', 'remessa', 'registrada'):
                raise ValueError('Boleto existente incompatível com o saldo/situação. Confira na área Banco.')
        job.cobranca_ids = [c.id for c in titulos]
        db.session.commit()
        pendentes = [c for c in titulos if c.status == 'pendente']
        if pendentes:
            _, erros = gerar_remessa(pendentes, user_id=uid)
            if erros:
                raise ValueError('; '.join(erros))
    if not all(banco_confirmado(c) for c in titulos):
        _mudar(job, 'banco')
        return
    for c in titulos:
        _validar(doc, job)
        r = carregar('boleto', c.id)
        chave = str(uuid5(NAMESPACE_URL, f'opao-cobranca-auto:{job.chave}:{c.id}'))
        envio, _ = enviar_automatico(r, chave, actor)
        if envio.status != 'aceito':
            raise ValueError(envio.erro or 'Envio não confirmado. Confira o histórico antes de reenviar.')
    _mudar(job, 'enviado')


def _avisar(aviso):
    from app.services import email
    rem = db.session.get(CobrancaRemessa, aviso.remessa_id)
    if not rem:
        aviso.estado, aviso.erro = 'falha', 'Remessa não encontrada.'
        db.session.commit()
        return
    titulos = Cobranca.query.filter_by(remessa_id=rem.id, status='remessa').all()
    if not any(not banco_confirmado(c) for c in titulos):
        aviso.estado = 'dispensado'
        db.session.commit()
        return
    # Persistir antes do serviço impede repetição após queda com resposta perdida.
    aviso.estado = 'enviando'
    db.session.commit()
    texto = (f'A remessa {rem.nome_arquivo} (#{rem.numero}, {rem.n_titulos} boleto(s)) está pronta. '
             'Caio ou Dakson precisa baixar o arquivo no ERP e enviá-lo pelo Internet Banking do Sicredi. '
             'Depois de conferir que os boletos foram registrados, confirme no ERP ou importe o retorno do banco. '
             'O e-mail automático com NF + boleto aguarda essa confirmação. '
             f'Acesse: {URL_PAINEL}. Aviso interno; não é cobrança ao cliente.')
    try:
        res = email.enviar(aviso.destinatario, f'Enviar remessa ao Sicredi — {rem.nome_arquivo}',
                           f'<p>{escape(texto)}</p><p><a href="{URL_PAINEL}">Abrir pendências do Sicredi</a></p>', texto=texto)
    except Exception:
        res = {'ok': False, 'incerto': True, 'erro': 'Resposta do serviço de e-mail não confirmada.'}
    aviso.estado = 'aceito' if res.get('ok') and res.get('id') else 'incerto' if res.get('incerto') or res.get('ok') else 'falha'
    aviso.provedor_id = str(res['id'])[:150] if res.get('id') else None
    aviso.erro = None if aviso.estado == 'aceito' else str(res.get('erro') or 'Envio não confirmado.')[:500]
    aviso.enviado_em = agora()
    db.session.commit()


def executar():
    """Um ciclo limitado; toda falha visível, sem repetição cega."""
    from flask import current_app
    try:
        with trava('worker'):
            ids = [j.id for j in AutomacaoCobranca.query.filter(
                AutomacaoCobranca.estado.in_(('pendente', 'banco'))
            ).order_by(AutomacaoCobranca.atualizado_em).limit(20).all()]
            for id in ids:
                try:
                    processar(db.session.get(AutomacaoCobranca, id))
                except OperacaoEmAndamento:
                    db.session.rollback()  # Outra aba está operando; próximo ciclo.
                except Exception as exc:
                    db.session.rollback()
                    current_app.logger.exception('Automação B2B %s interrompida', id)
                    _mudar(db.session.get(AutomacaoCobranca, id), 'erro', exc)
            avisos = AvisoRemessa.query.filter_by(estado='pendente').order_by(AvisoRemessa.id).limit(40).all()
            for aviso in avisos:
                aid = aviso.id
                try:
                    _avisar(aviso)
                except Exception as exc:
                    db.session.rollback()
                    current_app.logger.exception('Aviso de remessa %s interrompido', aid)
                    aviso = db.session.get(AvisoRemessa, aid)
                    aviso.estado, aviso.erro = 'incerto', str(exc)[:500]
                    db.session.commit()
            AppConfig.set('cobrancas_automacao_ultimo_ciclo', agora().isoformat())
            db.session.commit()
    except OperacaoEmAndamento:
        pass  # Outro worker já está processando.


def executar_no_app(app):
    with app.app_context():
        executar()
