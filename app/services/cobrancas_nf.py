"""Proteção B2B contra duas criações fiscais, inclusive após timeout/restart."""
import hashlib
import json

from app.extensions import db
from app.models import FaturaB2B, TentativaNFB2B
from app.services.cobrancas_trava import OperacaoEmAndamento, chave_documento, trava
from app.utils import agora


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
            if tentativa and not doc.tiny_nota_fiscal_id and not recriar:
                return {'ok': False, 'msg': 'A criação anterior da NF não foi confirmada. '
                        'Confira no Tiny antes de refazer: ela pode ter sido criada lá.'}
            # Valida todos os dados ANTES de registrar intenção/chamar o provedor.
            payload = None
            if not doc.tiny_nota_fiscal_id or recriar:
                payload, erro = montar_payload()
                if erro:
                    return {'ok': False, 'msg': erro}
            if not tentativa:
                tentativa = TentativaNFB2B(chave=chave)
                db.session.add(tentativa)
            tentativa.estado = 'iniciada'
            tentativa.usuario_id = usuario_id
            tentativa.iniciada_em = agora()
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
            tentativa.estado = 'concluida' if resultado.get('ok') else 'conferir'
            tentativa.erro = None if resultado.get('ok') else str(resultado.get('msg', 'Falha na emissão'))[:500]
            db.session.commit()
            return resultado
    except OperacaoEmAndamento as exc:
        return {'ok': False, 'msg': str(exc)}
