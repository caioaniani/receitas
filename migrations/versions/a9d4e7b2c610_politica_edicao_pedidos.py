"""Ativa edição justificada para contas com acesso operacional a pedidos.

Não altera perfis, concessões individuais, pedidos ou escopo de lojas.
O estado anterior fica guardado para uma reversão explícita desta revisão.
"""
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

revision = 'a9d4e7b2c610'
down_revision = 'c83a91d52f06'
branch_labels = None
depends_on = None

_CHAVE = 'pedido_edicao_por_perfil'
_BACKUP = f'migration:{revision}:{_CHAVE}'
_VALOR = json.dumps({'ativo': True, 'origem': 'politica_edicao_pedidos'})


def _tabelas():
    config = sa.table('app_config', sa.column('key', sa.String(100)),
                      sa.column('value', sa.Text()))
    auditoria = sa.table(
        'audit_log', sa.column('usuario_id', sa.Integer()),
        sa.column('criado_em', sa.DateTime()), sa.column('tabela', sa.String(60)),
        sa.column('registro_id', sa.Integer()), sa.column('acao', sa.String(10)),
        sa.column('antes', sa.Text()), sa.column('depois', sa.Text()),
        sa.column('ip', sa.String(45)), sa.column('user_agent', sa.String(300)),
    )
    return config, auditoria


def _registrar(bind, auditoria, antes, depois, acao):
    bind.execute(auditoria.insert().values(
        usuario_id=None,
        criado_em=datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None),
        tabela='pedido_edicao_politica', registro_id=None, acao=acao,
        antes=json.dumps(antes) if antes is not None else None,
        depois=json.dumps(depois) if depois is not None else None,
        ip=None, user_agent=f'migration:{revision}',
    ))


def upgrade():
    bind = op.get_bind()
    config, auditoria = _tabelas()
    backup = bind.execute(sa.select(config.c.key).where(config.c.key == _BACKUP)).first()
    if backup is not None:
        return
    anterior = bind.execute(
        sa.select(config.c.value).where(config.c.key == _CHAVE)).first()
    estado_anterior = {'existia': anterior is not None,
                       'valor': anterior[0] if anterior is not None else None}
    bind.execute(config.insert().values(key=_BACKUP, value=json.dumps(estado_anterior)))
    if anterior is None:
        bind.execute(config.insert().values(key=_CHAVE, value=_VALOR))
    else:
        bind.execute(config.update().where(config.c.key == _CHAVE).values(value=_VALOR))
    _registrar(
        bind, auditoria,
        {'chave': _CHAVE, 'valor': anterior[0]} if anterior is not None else None,
        {'chave': _CHAVE, 'valor': _VALOR, 'origem': 'politica_edicao_pedidos'},
        'insert' if anterior is None else 'update',
    )


def downgrade():
    bind = op.get_bind()
    config, auditoria = _tabelas()
    backup = bind.execute(
        sa.select(config.c.value).where(config.c.key == _BACKUP)).first()
    if backup is None:
        return
    estado_anterior = json.loads(backup[0])
    atual = bind.execute(sa.select(config.c.value).where(config.c.key == _CHAVE)).first()
    if estado_anterior['existia']:
        if atual is None:
            bind.execute(config.insert().values(key=_CHAVE, value=estado_anterior['valor']))
        else:
            bind.execute(config.update().where(config.c.key == _CHAVE)
                         .values(value=estado_anterior['valor']))
        depois = {'chave': _CHAVE, 'valor': estado_anterior['valor']}
        acao = 'update' if atual is not None else 'insert'
    else:
        bind.execute(config.delete().where(config.c.key == _CHAVE))
        depois, acao = None, 'delete'
    _registrar(bind, auditoria,
               {'chave': _CHAVE, 'valor': atual[0]} if atual is not None else None,
               depois, acao)
    bind.execute(config.delete().where(config.c.key == _BACKUP))
