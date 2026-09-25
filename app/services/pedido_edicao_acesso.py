"""Concessão individual do dono para editar pedidos depois do horário de corte.

A concessão não troca o perfil nem concede acesso a outras lojas ou operações.
As guardas de status, estoque, nota fiscal e justificativa pertencem à edição.
"""

import json
from datetime import datetime

from app.extensions import db
from app.models import AppConfig, Usuario
from app.utils import agora

_PREFIXO = 'pedido_edicao_fora_prazo:'


def _conta(usuario):
    usuario_id = getattr(usuario, 'id', None)
    if type(usuario_id) is not int or usuario_id <= 0:
        return None
    return db.session.get(Usuario, usuario_id)


def _elegivel(usuario):
    if (usuario is None or usuario.somente_treino
            or usuario.papel not in ('gerente', 'admin')):
        return False
    if usuario.is_admin():
        return True
    from app.services import permissoes

    return (permissoes.pode(usuario.papel, 'web_pedidos')
            and permissoes.pode(usuario.papel, 'web_pedido_operar'))


def pode_editar_fora_prazo(usuario):
    """Apenas uma concessão válida e explícita libera o corte de edição.

    Não usa nome/login como autorização. Dados ausentes ou malformados negam
    acesso; a elegibilidade atual é reavaliada também depois de conceder.
    """
    conta = _conta(usuario)
    if not _elegivel(conta):
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
    if type(permitido) is not bool:
        raise ValueError('Informe se a edição fora do prazo está autorizada.')
    conta = _conta(usuario)
    if conta is None:
        raise ValueError('A conta selecionada não existe.')
    if permitido and not _elegivel(conta):
        raise ValueError(
            'Esta liberação exige perfil Gerente ou Admin com acesso a pedidos '
            'e sem a restrição de somente treinamento.')
    registro = {
        'autorizado': permitido,
        'concedido_por_id': dono.id,
        'atualizado_em': agora().isoformat(),
    }
    return AppConfig.set(f'{_PREFIXO}{conta.id}', json.dumps(registro))
