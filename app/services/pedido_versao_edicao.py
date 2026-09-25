"""Controle de concorrência do formulário de edição de pedidos.

O chamador deve reler o pedido sob a trava da loja antes de validar e manter
essa trava até o commit. O token representa o pedido que foi mostrado no
formulário; não concede permissão nem substitui a proteção CSRF.
"""
import hashlib
import hmac
import json

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer
from sqlalchemy import select

from app.extensions import db
from app.models import Loja, MateriaPrima, PedidoItem, PedidoLoja, Produto, Receita

_SALT = 'pedido-versao-edicao-v1'
_CONFLITO = (
    'Este pedido mudou desde que a página foi aberta. '
    'Confira o pedido atual e revise suas alterações antes de salvar.'
)
_SEM_VERSAO = (
    'Esta página não tem uma versão válida do pedido. '
    'Confira o pedido atual e revise suas alterações antes de salvar.'
)
_REVISAO = 'Confirme que conferiu o pedido atual antes de aplicar suas alterações.'


def _serializer():
    return URLSafeSerializer(
        current_app.secret_key, salt=_SALT,
        signer_kwargs={'digest_method': hashlib.sha256},
    )


def _identidade(objeto):
    with db.session.no_autoflush:
        identificador = getattr(objeto, 'id', None)
    if type(identificador) is not int or identificador <= 0:
        raise ValueError(_SEM_VERSAO)
    return identificador


def _iso(valor):
    return valor.isoformat() if valor is not None else None


def _snapshot(pedido):
    """Lê valores persistidos, sem flush ou reaproveitar objetos ORM antigos.

    Todos os campos persistidos do cabeçalho e dos itens participam da
    comparação. Assim uma mudança de quantidade sem atualizar o carimbo de
    edição também é detectada. Nomes são apenas rótulos para a revisão.
    """
    pedido_id = _identidade(pedido)
    with db.session.no_autoflush:
        cabecalho = db.session.execute(
            select(*PedidoLoja.__table__.c)
            .where(PedidoLoja.id == pedido_id)
        ).mappings().one_or_none()
        if cabecalho is None:
            raise ValueError('Este pedido não existe mais. Volte à lista de pedidos.')
        itens = db.session.execute(
            select(*PedidoItem.__table__.c)
            .where(PedidoItem.pedido_id == pedido_id)
            .order_by(PedidoItem.id)
        ).mappings().all()
    return {'pedido': dict(cabecalho), 'itens': [dict(item) for item in itens]}


def _resumo(pedido):
    bruto = json.dumps(
        _snapshot(pedido), sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, default=str,
    )
    return hashlib.sha256(bruto.encode('utf-8')).hexdigest()


def gerar_versao_edicao(pedido, usuario, revisao=False):
    """Assina o estado visto; a revisão explícita também faz parte do token."""
    if type(revisao) is not bool:
        raise ValueError(_SEM_VERSAO)
    return _serializer().dumps({
        'versao': 1,
        'pedido_id': _identidade(pedido),
        'usuario_id': _identidade(usuario),
        'resumo': _resumo(pedido),
        'revisao': revisao,
    })


def validar_versao_edicao(pedido, usuario, token, confirmou_revisao=False):
    """Valida sob a trava; retorna se o formulário exigia revisão explícita.

    Formulários anteriores ao deploy não têm token e devem passar pela tela
    de revisão, preservando o rascunho. Nunca há fallback para a sessão, pois
    várias abas compartilham o mesmo cookie.
    """
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise ValueError(_SEM_VERSAO)
    try:
        dados = _serializer().loads(token)
    except BadData:
        raise ValueError(_SEM_VERSAO) from None
    if (not isinstance(dados, dict)
            or type(dados.get('versao')) is not int or dados['versao'] != 1
            or type(dados.get('pedido_id')) is not int
            or dados['pedido_id'] != _identidade(pedido)
            or type(dados.get('usuario_id')) is not int
            or dados['usuario_id'] != _identidade(usuario)
            or type(dados.get('revisao')) is not bool
            or not isinstance(dados.get('resumo'), str)
            or len(dados['resumo']) != 64
            or any(c not in '0123456789abcdef' for c in dados['resumo'])):
        raise ValueError(_SEM_VERSAO)
    if not hmac.compare_digest(dados['resumo'], _resumo(pedido)):
        raise ValueError(_CONFLITO)
    if dados['revisao'] and confirmou_revisao is not True:
        raise ValueError(_REVISAO)
    return dados['revisao']


def dados_revisao_edicao(pedido):
    """Composição persistida para comparar com os campos digitados.

    Gerar os dados e o token de revisão enquanto a mesma trava ainda está
    adquirida garante que o usuário veja exatamente o estado assinado.
    """
    snapshot = _snapshot(pedido)
    cabecalho = snapshot['pedido']
    with db.session.no_autoflush:
        loja = db.session.execute(
            select(Loja.nome).where(Loja.id == cabecalho['loja_id'])
        ).scalar_one_or_none()
        nomes = db.session.execute(
            select(PedidoItem.id, Receita.nome, Produto.nome, MateriaPrima.nome)
            .select_from(PedidoItem)
            .outerjoin(Receita, PedidoItem.receita_id == Receita.id)
            .outerjoin(Produto, PedidoItem.produto_id == Produto.id)
            .outerjoin(MateriaPrima, PedidoItem.materia_prima_id == MateriaPrima.id)
            .where(PedidoItem.pedido_id == cabecalho['id'])
        ).all()
    por_id = {
        item_id: receita or produto or (f'{mp} (MP)' if mp else 'Item removido')
        for item_id, receita, produto, mp in nomes
    }
    return {
        'pedido_id': cabecalho['id'],
        'loja': loja or 'Loja removida',
        'data_entrega': _iso(cabecalho['data_entrega']),
        'status': cabecalho['status'],
        'observacao': cabecalho['observacao'],
        'modificado_em': _iso(cabecalho['modificado_em']),
        'itens': [{
            'nome': por_id.get(item['id'], 'Item removido'),
            'quantidade': item['quantidade'],
            'estado': item['estado'],
            'observacao': item['observacao'],
        } for item in snapshot['itens']],
    }
