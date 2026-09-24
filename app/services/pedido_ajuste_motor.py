"""Justificativas e comparação dos ajustes humanos aos pedidos da indústria."""
import json

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    AuditLog,
    FotoRecebimento,
    MovEstoqueLoja,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoItem,
    PedidoItemFoto,
    PedidoLoja,
)
from app.services.pedido_edicao_acesso import pode_editar_fora_prazo
from app.utils import hoje as hoje_brt

TABELA_AJUSTES = 'pedido_ajuste_motor'


def pedido_aberto_para_justificar(loja_id, data_entrega, usuario):
    """Impede usar 'novo pedido' para alterar o existente sem justificativa.

    Chamadores já devem ter adquirido a trava da loja antes da consulta.
    """
    if not pode_editar_fora_prazo(usuario):
        return None
    return (PedidoLoja.query.filter_by(loja_id=loja_id, data_entrega=data_entrega)
            .filter(PedidoLoja.status.in_(('pendente', 'confirmado')))
            .order_by(PedidoLoja.id).first())


def validar_justificativa(dados):
    resultado = {}
    for campo, rotulo in (
        ('descricao_alteracao', 'o que está mudando'),
        ('motivo_alteracao', 'por que está mudando'),
    ):
        valor = dados.get(campo)
        if not isinstance(valor, str) or not 10 <= len(valor.strip()) <= 1000:
            raise ValueError(f'Informe {rotulo}, com 10 a 1.000 caracteres.')
        resultado[campo] = valor.strip()
    return resultado


def bloqueio_edicao_livre(pedido):
    """A exceção de horário não reabre entrega, nota ou estoque já movimentado."""
    if pedido.data_entrega and pedido.data_entrega < hoje_brt():
        return 'Pedidos de dias anteriores não podem ser alterados por esta liberação.'
    if (pedido.status not in ('pendente', 'confirmado')
            or pedido.driver_id is not None or pedido.tiny_nota_fiscal_id
            or pedido.nf_status or pedido.nf_emitida_em or pedido.nf_numero
            or any(q.usado_em is not None for q in pedido.qrcodes)
            or any(i.quantidade_recebida is not None for i in pedido.itens)):
        return 'Este pedido já avançou na entrega ou na emissão da nota e não pode ser alterado.'
    ids = [i.id for i in pedido.itens]
    if (FotoRecebimento.query.filter_by(pedido_id=pedido.id).first()
            or (ids and PedidoItemFoto.query.filter(
                PedidoItemFoto.pedido_item_id.in_(ids)).first())):
        return 'Este pedido já tem conferência registrada e não pode ser alterado.'
    for model in (MovEstoqueProducao, MovEstoqueLoja, MovimentacaoEstoque):
        ref = model.referencia
        if model.query.filter(or_(
                ref.ilike(f'%pedido #{pedido.id}'),
                ref.ilike(f'%pedido #{pedido.id} %'),
                ref.ilike(f'%pedido #{pedido.id}(%'))).first():
            return 'Este pedido já movimentou estoque e não pode ser alterado.'
    return None


def snapshot_pedido(pedido):
    """Consulta os itens persistidos; o REPLACE deixa a coleção ORM antiga."""
    itens = (PedidoItem.query.filter_by(pedido_id=pedido.id)
             .options(joinedload(PedidoItem.receita), joinedload(PedidoItem.produto),
                      joinedload(PedidoItem.materia_prima))
             .order_by(PedidoItem.id).all())
    return {
        'pedido_id': pedido.id, 'loja_id': pedido.loja_id,
        'loja': pedido.loja.nome,
        'data_entrega': pedido.data_entrega.isoformat() if pedido.data_entrega else None,
        'observacao': pedido.observacao,
        'modificado_por_id': pedido.modificado_por_id,
        'itens': [{
            'receita_id': i.receita_id, 'produto_id': i.produto_id,
            'materia_prima_id': i.materia_prima_id,
            'nome': i.nome_item, 'quantidade': i.quantidade,
            'estado': i.estado, 'observacao': i.observacao,
        } for i in itens],
    }


def preparar_ajuste(pedido, usuario, dados):
    """Sem permissão especial, não altera o fluxo de edição já existente."""
    if not pode_editar_fora_prazo(usuario):
        return None
    motivo = validar_justificativa(dados)
    bloqueio = bloqueio_edicao_livre(pedido)
    if bloqueio:
        raise ValueError(bloqueio)
    return {'antes': snapshot_pedido(pedido), **motivo}


def registrar_ajuste(pedido, usuario, ajuste, *, canal):
    """Pedido e justificativa são confirmados ou desfeitos na mesma transação."""
    if ajuste is None:
        if pode_editar_fora_prazo(usuario):
            raise ValueError('A liberação de edição mudou. Reabra o pedido e informe a justificativa.')
        return
    # Também valida novamente se a permissão foi revogada durante o formulário.
    if not pode_editar_fora_prazo(usuario):
        raise ValueError('A liberação de edição foi revogada. Reabra o pedido.')
    db.session.flush()
    depois = snapshot_pedido(pedido)
    depois.update({
        'descricao_alteracao': ajuste['descricao_alteracao'],
        'motivo_alteracao': ajuste['motivo_alteracao'],
        'canal': canal,
    })
    db.session.add(AuditLog(
        usuario_id=usuario.id, tabela=TABELA_AJUSTES, registro_id=pedido.id,
        acao='update', antes=json.dumps(ajuste['antes'], ensure_ascii=False),
        depois=json.dumps(depois, ensure_ascii=False),
    ))


def historico_ajustes(pedido_id):
    """Histórico legível sem interpretar a justificativa como instrução."""
    rows = (AuditLog.query.filter_by(tabela=TABELA_AJUSTES, registro_id=pedido_id)
            .options(joinedload(AuditLog.usuario))
            .order_by(AuditLog.criado_em.desc(), AuditLog.id.desc()).all())
    historico = []
    for row in rows:
        antes, depois = json.loads(row.antes), json.loads(row.depois)
        historico.append({
            'autor': row.usuario.nome if row.usuario else 'Usuário removido',
            'criado_em': row.criado_em,
            'descricao': depois['descricao_alteracao'],
            'motivo': depois['motivo_alteracao'],
            'antes': antes, 'depois': depois,
        })
    return historico
