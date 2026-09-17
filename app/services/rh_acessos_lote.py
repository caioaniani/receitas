"""Revisar destinatários não envia mensagens nem troca credenciais.

A confirmação consome uma revisão uma única vez no banco, inclusive entre
workers. Falha parcial não autoriza repetir o lote: é necessária nova revisão.
"""
import hashlib
import json
import secrets

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Funcionario, Loja, RhReenvioAcessoExecucao, Usuario
from app.services import treino_acessos


def _assinador():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'],
                                  salt='rh-reenvio-revisado-v1')


def _estado(pessoa):
    usuario = pessoa.usuario
    campos = [pessoa.id, pessoa.nome, pessoa.email, pessoa.ativo,
              sorted(l.id for l in pessoa.lojas), pessoa.usuario_id,
              usuario.email, usuario.login, usuario.senha_hash,
              usuario.is_owner, usuario.senha_provisoria]
    return hashlib.sha256(json.dumps(campos, ensure_ascii=False).encode()).hexdigest()


def prever(modo, *, loja_id=None, pessoa_id=None, ids=None):
    if modo not in ('pendentes', 'todos'):
        raise ValueError('Escolha o tipo de reenvio.')
    loja = db.session.get(Loja, loja_id) if loja_id else None
    if loja_id and loja is None:
        raise ValueError('Unidade não encontrada.')
    query = (Funcionario.query.options(joinedload(Funcionario.usuario),
                                      selectinload(Funcionario.lojas))
             .filter(Funcionario.ativo.is_(True), Funcionario.usuario_id.isnot(None)))
    if loja_id:
        query = query.filter(Funcionario.lojas.any(Loja.id == loja_id))
    if pessoa_id:
        query = query.filter(Funcionario.id == pessoa_id)
    if ids is not None:
        query = query.filter(Funcionario.id.in_(ids))
    incluidos, excluidos = [], []
    for pessoa in query.order_by(Funcionario.nome, Funcionario.id).all():
        usuario = pessoa.usuario
        if not usuario or (modo == 'pendentes' and not usuario.senha_provisoria):
            continue
        email = (treino_acessos._email_normalizado(pessoa.email)
                 or treino_acessos._email_normalizado(usuario.email))
        motivo = 'Conta do proprietário protegida' if usuario.is_owner else None
        if not motivo:
            validacao = treino_acessos._validar_email_da_pessoa(pessoa, email, usuario)
            if not validacao['ok']:
                motivo = ('Sem e-mail válido' if not email else
                          'E-mail em conflito com outro cadastro; conferir individualmente')
        item = dict(pessoa=pessoa, email=email, motivo=motivo)
        if motivo:
            excluidos.append(item)
        else:
            item['estado'] = _estado(pessoa)
            incluidos.append(item)
    return dict(incluidos=incluidos, excluidos=excluidos, modo=modo,
                loja_id=loja_id, pessoa_id=pessoa_id,
                escopo=loja.nome if loja else 'Todas as unidades')


def assinar(previa, autor_id):
    return _assinador().dumps({
        'autor': autor_id, 'modo': previa['modo'],
        'loja': previa['loja_id'], 'pessoa': previa['pessoa_id'],
        'itens': [[i['pessoa'].id, i['estado']] for i in previa['incluidos']],
        'nonce': secrets.token_hex(24),
    })


def confirmar(token, modo, autor_id):
    try:
        dados = _assinador().loads(token, max_age=1800)
    except BadData:
        raise ValueError('A revisão expirou ou é inválida. Confira os destinatários novamente.') from None
    if dados.get('autor') != autor_id or dados.get('modo') != modo:
        raise ValueError('Esta revisão não corresponde ao usuário ou ao tipo de envio.')
    previa = prever(modo, loja_id=dados['loja'], pessoa_id=dados['pessoa'],
                    ids=[i[0] for i in dados['itens']])
    atual = [[i['pessoa'].id, i['estado']] for i in previa['incluidos']]
    if not atual or atual != dados['itens']:
        raise ValueError('Um cadastro, destinatário ou acesso mudou. Revise o lote novamente; nenhum e-mail foi enviado.')
    # INSERT com PK única faz a reserva atômica; replay não depende de cookie.
    if db.session.get(RhReenvioAcessoExecucao, dados['nonce']):
        raise ValueError('Esta revisão já foi utilizada. Confira o resultado antes de preparar outro envio.')
    db.session.add(RhReenvioAcessoExecucao(
        id=dados['nonce'], autor_id=autor_id, quantidade=len(atual)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError('Esta revisão já está em execução. Não repita o envio.') from None
    return previa


def enviar_revisado(item):
    # Os serviços de envio confirmam um destinatário por transação. Releia
    # cada pessoa após commits anteriores para não usar uma identidade antiga.
    pessoa = (Funcionario.query.filter_by(id=item['pessoa'].id)
              .populate_existing().with_for_update().one())
    if pessoa.usuario_id:
        (Usuario.query.filter_by(id=pessoa.usuario_id)
         .populate_existing().with_for_update().one())
    db.session.expire(pessoa, ['usuario'])
    db.session.expire(pessoa, ['lojas'])
    if not pessoa.usuario or _estado(pessoa) != item['estado']:
        db.session.rollback()
        return {'ok': False, 'motivo': 'cadastro_alterado'}
    return treino_acessos.reenviar_acesso(pessoa)
