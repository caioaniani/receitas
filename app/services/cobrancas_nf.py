"""Proteção B2B contra duas criações fiscais, inclusive após timeout/restart."""
import hashlib
import json

from app.extensions import db
from app.models import AppConfig, FaturaB2B, TentativaNFB2B, VendaB2B
from app.services.cobrancas_trava import OperacaoEmAndamento, chave_documento, trava
from app.utils import agora


def ultimo_erro(doc):
    tentativa = db.session.get(TentativaNFB2B, chave_documento(doc))
    return tentativa.erro if tentativa and not doc.nf_emitida_em else None


def assinatura_documento(doc):
    """Itens, valores e destinatário da NF; não inclui status/datas/e-mail."""
    vendas = sorted(doc.vendas, key=lambda v: v.id) if isinstance(doc, FaturaB2B) else [doc]
    dados = {'cliente': doc.cliente_id, 'total': doc.valor_total,
             'cnpj_cpf': ''.join(c for c in (doc.cliente.cnpj_cpf or '') if c.isdigit()) if doc.cliente else '',
             'vendas': [{'id': v.id, 'cliente': v.cliente_id, 'total': v.valor_total,
                         'frete': str(v.frete_valor or 0),
                         'itens': [(i.receita_id, i.produto_id, i.quantidade, i.preco_unitario, i.desconto_percentual)
                                   for i in sorted(v.itens, key=lambda i: i.id)]} for v in vendas]}
    return hashlib.sha256(json.dumps(dados, sort_keys=True, default=str).encode()).hexdigest()


def validar_assinatura(doc):
    a = db.session.get(TentativaNFB2B, chave_documento(doc))
    if a and a.assinatura and doc.tiny_nota_fiscal_id and a.assinatura != assinatura_documento(doc):
        raise ValueError('O cliente, os itens ou o total mudaram depois da emissão da NF. '
                         'Confira a nota e a venda antes de gerar ou enviar a cobrança.')


def sincronizar(doc):
    """Consulta a NF vinculada, inclusive paga/rejeitada, sem emitir nem cobrar."""
    from flask import current_app

    from app.services import tiny_nf

    try:
        with trava(chave_documento(doc)):
            db.session.refresh(doc, with_for_update=True)
            if not doc.tiny_nota_fiscal_id:
                return {'ok': False, 'autorizada': False,
                        'msg': 'Este documento ainda não tem uma NF vinculada ao Tiny.'}
            situacao = tiny_nf._sincronizar_situacao(doc)
            if not situacao:
                return {'ok': False, 'autorizada': False,
                        'msg': 'Não foi possível confirmar a situação da NF no Tiny. Tente atualizar novamente.'}
            if situacao['autorizada']:
                tentativa = db.session.get(TentativaNFB2B, chave_documento(doc))
                if tentativa:
                    tentativa.estado, tentativa.erro = 'concluida', None
                    # A assinatura original continua protegendo alterações locais.
                    db.session.commit()
                return {'ok': True, 'autorizada': True,
                        'msg': 'NF autorizada no Tiny. Situação atualizada no sistema.'}
            return {'ok': True, 'autorizada': False,
                    'msg': 'Consulta realizada: a autorização da NF ainda não foi confirmada no Tiny.'}
    except OperacaoEmAndamento as exc:
        db.session.rollback()
        return {'ok': False, 'autorizada': False, 'msg': str(exc)}
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Falha ao consultar NF B2B %s', chave_documento(doc))
        return {'ok': False, 'autorizada': False,
                'msg': 'Não foi possível consultar a NF no Tiny. Tente atualizar novamente.'}


def sincronizar_pendentes(limite=5):
    """Lotes por modelo com cursor persistente: rejeições antigas não bloqueiam a fila.

    Chamado sob a trava do worker. Só reconcilia a situação fiscal; pagamentos,
    boletos, envios e erros financeiros seguem seu próprio fluxo de conferência.
    """
    for modelo, tipo in ((VendaB2B, 'venda'), (FaturaB2B, 'fatura')):
        chave = f'b2b_nf_sync_cursor_{tipo}'
        cursor = AppConfig.get_int(chave, 0)
        pendentes = modelo.query.filter(
            modelo.tiny_nota_fiscal_id.isnot(None), modelo.tiny_nota_fiscal_id != '',
            modelo.nf_emitida_em.is_(None))
        documentos = pendentes.filter(modelo.id > cursor).order_by(modelo.id).limit(limite).all()
        if not documentos and cursor:
            documentos = pendentes.order_by(modelo.id).limit(limite).all()
        for doc in documentos:
            sincronizar(doc)
            AppConfig.set(chave, doc.id)
            db.session.commit()


def emitir(doc, montar_payload, usuario_id=None, recriar=False):
    from app.services import tiny_nf
    try:
        with trava(chave_documento(doc)):
            db.session.refresh(doc, with_for_update=True)
            if doc.status == 'cancelada' or getattr(doc, 'sem_cobranca', False):
                return {'ok': False, 'msg': 'Documento cancelado ou divulgação sem cobrança.'}
            if doc.nf_emitida_em and doc.tiny_nota_fiscal_id:
                return {'ok': True, 'nota_fiscal_id': doc.tiny_nota_fiscal_id,
                        'msg': 'NF já emitida. Uma nota autorizada não será recriada.'}
            chave = chave_documento(doc)
            tentativa = db.session.get(TentativaNFB2B, chave)
            if recriar and doc.tiny_nota_fiscal_id:
                situacao = tiny_nf._sincronizar_situacao(doc)
                if situacao and situacao['autorizada']:
                    return {'ok': True, 'nota_fiscal_id': doc.tiny_nota_fiscal_id,
                            'msg': 'NF já autorizada no Tiny. Nenhuma nova nota foi criada.'}
                if not situacao or not situacao['rejeitada'] or situacao.get('denegada'):
                    return {'ok': False, 'msg': 'Não foi confirmada uma rejeição que permita refazer. '
                            'A nota atual foi preservada; confira sua situação no Tiny.'}
            if tentativa and not doc.tiny_nota_fiscal_id and not recriar:
                return {'ok': False, 'msg': 'A criação anterior da NF não foi confirmada. '
                        'Confira no Tiny antes de refazer: ela pode ter sido criada lá.'}
            # Valida todos os dados ANTES de registrar intenção/chamar o provedor.
            payload = None
            if not doc.tiny_nota_fiscal_id or recriar:
                payload, erro = montar_payload()
                if erro:
                    if tentativa:
                        tentativa.erro = str(erro)[:500]
                        db.session.commit()
                    return {'ok': False, 'msg': erro}
            if not tentativa:
                tentativa = TentativaNFB2B(chave=chave)
                db.session.add(tentativa)
            tentativa.estado = 'iniciada'
            tentativa.usuario_id = usuario_id
            tentativa.iniciada_em = agora()
            erro_anterior = tentativa.erro if not recriar else None
            tentativa.erro = None
            if not doc.tiny_nota_fiscal_id or recriar:
                tentativa.assinatura = assinatura_documento(doc)
            db.session.commit()
            try:
                resultado = tiny_nf.emitir_nf_generico(doc, lambda: (payload, None), recriar=recriar)
            except Exception as exc:
                db.session.rollback()
                resultado = {'ok': False, 'msg': f'Emissão não confirmada: {exc}. Confira no Tiny antes de tentar novamente.'}
            tentativa = db.session.get(TentativaNFB2B, chave)
            if erro_anterior and str(resultado.get('msg', '')).startswith('NF rejeitada pela SEFAZ'):
                resultado['msg'] = erro_anterior
            tentativa.estado = 'concluida' if resultado.get('ok') else 'conferir'
            tentativa.erro = None if resultado.get('ok') else str(resultado.get('msg', 'Falha na emissão'))[:500]
            db.session.commit()
            return resultado
    except OperacaoEmAndamento as exc:
        return {'ok': False, 'msg': str(exc)}
