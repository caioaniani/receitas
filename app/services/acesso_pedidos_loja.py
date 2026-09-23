"""Permissão estreita para criar e editar pedidos da loja à indústria.

O dono escolhe uma única loja. A liberação não muda perfil, loja da conta,
restrição de treinamento nem permissões de expedição, estoque ou finanças.
"""

from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import AcessoPedidosLoja, Funcionario, Loja, Usuario
from app.utils import agora

ENDPOINTS = frozenset({
    'pedidos.lista',
    'pedidos.novo',
    'pedidos.detalhe',
    'pedidos.editar',
    'pedidos.buscar_itens',
})


def liberacao(usuario):
    """Retorna apenas uma concessão ainda válida para a pessoa e a loja."""
    usuario_id = getattr(usuario, 'id', None)
    if (not usuario_id or getattr(usuario, 'papel', None) != 'funcionario'
            or getattr(usuario, 'is_owner', False)):
        return None
    return (AcessoPedidosLoja.query
            .join(Usuario, AcessoPedidosLoja.usuario_id == Usuario.id)
            .join(Funcionario, AcessoPedidosLoja.funcionario_id == Funcionario.id)
            .join(Loja, AcessoPedidosLoja.loja_id == Loja.id)
            .options(joinedload(AcessoPedidosLoja.loja))
            .filter(AcessoPedidosLoja.usuario_id == usuario_id,
                    AcessoPedidosLoja.ativo.is_(True),
                    Usuario.papel == 'funcionario',
                    Usuario.is_owner.is_not(True),
                    Funcionario.usuario_id == usuario_id,
                    Funcionario.ativo.is_(True),
                    Loja.ativa.is_(True),
                    Loja.nome != 'Industria')
            .first())


def loja_liberada(usuario):
    """Loja da concessão válida, sem fallback para outra unidade."""
    acesso = liberacao(usuario)
    return acesso.loja if acesso else None


def tem_liberacao(usuario):
    return liberacao(usuario) is not None


def _validar_ator(ator):
    ator_id = getattr(ator, 'id', None)
    dono = db.session.get(Usuario, ator_id) if ator_id else None
    if dono is None or not dono.is_dono():
        raise PermissionError('Somente o dono pode alterar esta permissão.')
    return dono


def salvar(usuario, loja_id, ator):
    """Concede/substitui a loja, preservando a concessão inicial; não faz commit."""
    dono = _validar_ator(ator)
    usuario_id = getattr(usuario, 'id', None)
    conta = db.session.get(Usuario, usuario_id) if usuario_id else None
    if conta is None:
        raise ValueError('A conta selecionada não existe.')
    if conta.papel != 'funcionario' or conta.is_dono():
        raise ValueError('Esta liberação é exclusiva para o perfil Funcionário.')
    pessoas = (Funcionario.query
               .filter_by(usuario_id=conta.id, ativo=True)
               .limit(2).all())
    if len(pessoas) != 1:
        raise ValueError('Vincule a conta a um único funcionário ativo no RH.')
    try:
        loja_id = int(loja_id)
    except (TypeError, ValueError):
        raise ValueError('Selecione uma loja válida.') from None
    loja = db.session.get(Loja, loja_id) if loja_id > 0 else None
    if loja is None or not loja.ativa or loja.nome == 'Industria':
        raise ValueError('Selecione uma loja ativa para os pedidos à indústria.')

    acesso = db.session.get(AcessoPedidosLoja, conta.id)
    instante = agora()
    if acesso is None:
        acesso = AcessoPedidosLoja(
            usuario_id=conta.id,
            concedido_por_id=dono.id,
            concedido_em=instante,
        )
        db.session.add(acesso)
    acesso.funcionario_id = pessoas[0].id
    acesso.funcionario = pessoas[0]
    acesso.loja_id = loja.id
    acesso.loja = loja
    acesso.ativo = True
    acesso.atualizado_por_id = dono.id
    acesso.atualizado_em = instante
    return acesso


def revogar(usuario, ator):
    """Desativa a concessão, preservando a auditoria; não faz commit."""
    dono = _validar_ator(ator)
    usuario_id = getattr(usuario, 'id', None)
    acesso = db.session.get(AcessoPedidosLoja, usuario_id) if usuario_id else None
    if acesso is not None and acesso.ativo:
        acesso.ativo = False
        acesso.atualizado_por_id = dono.id
        acesso.atualizado_em = agora()
    return acesso
