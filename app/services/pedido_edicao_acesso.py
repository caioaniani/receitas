"""Edição após o corte para quem já tem acesso operacional a pedidos.

A política não troca o perfil nem concede acesso a outras lojas ou operações.
As guardas de status, estoque, nota fiscal e justificativa pertencem à edição.
Sem a política ativa, permanece a concessão individual anterior.
"""

import json
from datetime import datetime

from app.extensions import db
from app.models import AppConfig, Usuario
from app.utils import agora

_PREFIXO = 'pedido_edicao_fora_prazo:'
_POLITICA = 'pedido_edicao_por_perfil'


def _conta(usuario):
    usuario_id = getattr(usuario, 'id', None)
    if type(usuario_id) is not int or usuario_id <= 0:
        return None
    return db.session.get(Usuario, usuario_id, populate_existing=True)


def _elegivel_legado(usuario):
    if (usuario is None or usuario.somente_treino
            or usuario.papel not in ('gerente', 'admin')):
        return False
    if usuario.is_admin():
        return True
    from app.services import permissoes

    return (permissoes.pode(usuario.papel, 'web_pedidos')
            and permissoes.pode(usuario.papel, 'web_pedido_operar'))


def _elegivel(usuario):
    if usuario is None or usuario.papel in ('observador', 'relatorio_loja'):
        return False
    from app.services import acesso_pedidos_loja, permissoes

    # Esta concessão já permite pedidos à indústria mesmo para contas de
    # treinamento. As rotas continuam limitando a edição à loja concedida.
    if acesso_pedidos_loja.tem_liberacao(usuario):
        return True
    if usuario.somente_treino:
        return False
    if usuario.is_admin():
        return True
    return (permissoes.pode(usuario.papel, 'web_pedido_operar')
            or permissoes.pode(usuario.papel, 'editar_pedido'))


def politica_por_perfil_ativa():
    """Somente uma configuração explícita ativa a política por perfil."""
    registro = (AppConfig.query.populate_existing()
                .filter_by(key=_POLITICA).first())
    try:
        valor = json.loads(registro.value) if registro else None
    except (TypeError, ValueError):
        return False
    return isinstance(valor, dict) and valor.get('ativo') is True


def elegivel_edicao_fora_prazo(usuario):
    """Elegibilidade da conta persistida, conforme a política em vigor."""
    conta = _conta(usuario)
    if politica_por_perfil_ativa():
        return _elegivel(conta)
    return _elegivel_legado(conta)


def pode_editar_fora_prazo(usuario):
    """Libera edição pela política vigente, sem ampliar operações ou lojas.

    Sem a política por perfil, exige uma concessão individual válida. Não usa
    nome/login como autorização e reavalia a elegibilidade atual da conta.
    """
    conta = _conta(usuario)
    if politica_por_perfil_ativa():
        return _elegivel(conta)
    if not _elegivel_legado(conta):
        return False
    registro_config = (AppConfig.query.populate_existing()
                       .filter_by(key=f'{_PREFIXO}{conta.id}').first())
    valor = registro_config.value if registro_config else None
    try:
        registro = json.loads(valor)
        if (not isinstance(registro, dict)
                or registro.get('autorizado') is not True
                or type(registro.get('concedido_por_id')) is not int
                or registro['concedido_por_id'] <= 0
                or not isinstance(registro.get('atualizado_em'), str)):
            return False
        datetime.fromisoformat(registro['atualizado_em'])
    except (TypeError, ValueError):
        return False
    return True


def definir_acesso_edicao_fora_prazo(usuario, permitido, autor):
    """Concede/revoga e registra o dono responsável; o chamador faz commit.

    A revogação continua disponível se o destinatário mudar de perfil.
    """
    dono = _conta(autor)
    if dono is None or not dono.is_dono():
        raise PermissionError('Somente o dono pode alterar esta permissão.')
    if politica_por_perfil_ativa():
        raise ValueError(
            'A edição fora do prazo acompanha o acesso a pedidos do perfil '
            'ou da loja. Não é necessário conceder uma liberação individual.')
    if type(permitido) is not bool:
        raise ValueError('Informe se a edição fora do prazo está autorizada.')
    conta = _conta(usuario)
    if conta is None:
        raise ValueError('A conta selecionada não existe.')
    if permitido and not _elegivel_legado(conta):
        raise ValueError(
            'Esta liberação exige perfil Gerente ou Admin com acesso a pedidos '
            'e sem a restrição de somente treinamento.')
    registro = {
        'autorizado': permitido,
        'concedido_por_id': dono.id,
        'atualizado_em': agora().isoformat(),
    }
    return AppConfig.set(f'{_PREFIXO}{conta.id}', json.dumps(registro))
