"""Registra mudanças reais e datas informadas; nunca decide uma promoção.

As funções só adicionam eventos à transação do chamador (sem commit), não
alteram cargo, salário ou enquadramento e não reconstroem datas históricas.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import inspect

from app.extensions import db
from app.models import (
    Cargo,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraFaixa,
    RhMovimentacao,
    Usuario,
)
from app.services.rh_cargos import nome_cargo_exibicao, normalizar_nome_cargo
from app.utils import hoje

TIPOS = ('alteracao', 'promocao', 'aplicacao_plano')


def _cargo_chave(nome):
    return normalizar_nome_cargo(nome_cargo_exibicao(nome))


def _cargo_atual(funcionario):
    """Respeita tanto atribuição do relacionamento quanto do cargo_id."""
    estado = inspect(funcionario)
    relacionados = estado.attrs.cargo.history.added
    if relacionados:
        return relacionados[0]
    if estado.attrs.cargo_id.history.has_changes():
        return (db.session.get(Cargo, funcionario.cargo_id)
                if funcionario.cargo_id else None)
    return funcionario.cargo


def snapshot(funcionario):
    """Cargo vigente e faixa equivalente única, sem usar proposta da planilha.

    O nível só é afirmado quando existe uma única família/faixa compatível.
    Mesmo que um enquadramento sugerido indique N5, ele não vira nível atual.
    """
    with db.session.no_autoflush:
        cargo = _cargo_atual(funcionario)
        nome = cargo.nome if cargo else funcionario.funcao
        chave = _cargo_chave(nome)
        faixas = set()
        if chave and cargo:
            vinculadas = (db.session.query(PlanoCarreiraFaixa, Cargo)
                          .join(PlanoCarreiraCargoVinculo,
                                PlanoCarreiraCargoVinculo.faixa_id ==
                                PlanoCarreiraFaixa.id)
                          .join(Cargo, Cargo.id ==
                                PlanoCarreiraCargoVinculo.cargo_id).all())
            faixas = {(faixa.familia, faixa.nivel)
                      for faixa, vinculado in vinculadas
                      if _cargo_chave(vinculado.nome) == chave}
        familia, nivel = next(iter(faixas)) if len(faixas) == 1 else (None, None)
        # Equivalência explicitamente definida pelo dono: Atendente é N1.
        # Não extrapolamos essa convenção para outros nomes nem resolvemos
        # um vínculo ambíguo do plano com uma suposição.
        if cargo and not faixas and chave == 'atendente 1':
            familia, nivel = 'Atendimento', 1
        return {
            'cargo_id': cargo.id if cargo else None,
            'cargo_nome': nome_cargo_exibicao(nome) or None,
            'familia': familia,
            'nivel': nivel,
        }


def _chave_snapshot(estado):
    return (_cargo_chave(estado.get('cargo_nome')),
            normalizar_nome_cargo(estado.get('familia')),
            estado.get('nivel'))


def _validar_data(data_efetiva, *, obrigatoria=False):
    if data_efetiva is None and not obrigatoria:
        return
    if not isinstance(data_efetiva, date) or isinstance(data_efetiva, datetime):
        raise ValueError('Informe uma data de promoção válida.')
    if data_efetiva > hoje():
        raise ValueError('A data de promoção não pode estar no futuro.')


def _nova(funcionario, antes, depois, *, actor_id, origem, tipo,
          data_efetiva, observacao=None):
    if tipo not in TIPOS:
        raise ValueError('Tipo de movimentação inválido.')
    if not origem or len(origem) > 60:
        raise ValueError('Informe uma origem de até 60 caracteres.')
    if not funcionario.id:
        raise ValueError('Salve o cadastro antes de registrar movimentações.')
    _validar_data(data_efetiva)
    if (data_efetiva and funcionario.data_admissao
            and data_efetiva < funcionario.data_admissao):
        raise ValueError('A data de promoção não pode ser anterior à admissão.')
    observacao = (observacao or '').strip()
    if len(observacao) > 2000:
        raise ValueError('A observação deve ter no máximo 2000 caracteres.')
    usuario = db.session.get(Usuario, actor_id) if actor_id else None
    if actor_id and usuario is None:
        raise ValueError('Responsável pelo registro não encontrado.')
    evento = RhMovimentacao(
        funcionario_id=funcionario.id, tipo=tipo, origem=origem,
        data_efetiva=data_efetiva, registrado_por_id=actor_id,
        registrado_por_nome=usuario.nome if usuario else None,
        cargo_anterior_id=antes.get('cargo_id'),
        cargo_anterior=antes.get('cargo_nome'),
        familia_anterior=antes.get('familia'),
        nivel_anterior=antes.get('nivel'),
        cargo_novo_id=depois.get('cargo_id'),
        cargo_novo=depois.get('cargo_nome'),
        familia_nova=depois.get('familia'),
        nivel_novo=depois.get('nivel'),
        observacao=observacao or None,
    )
    db.session.add(evento)
    return evento


def registrar_mudanca(funcionario, antes, *, actor_id=None, origem,
                      tipo='alteracao', data_efetiva=None, observacao=None):
    """Adiciona evento só se cargo/família/nível real mudou, sem commit.

    ``antes`` deve ser capturado antes da edição. Chamar novamente depois de
    salvar os mesmos valores não cria evento nem afirma que houve promoção.
    Uma promoção só recebe esse tipo se o chamador a declarar explicitamente.
    """
    depois = snapshot(funcionario)
    if _chave_snapshot(antes) == _chave_snapshot(depois):
        return None
    return _nova(
        funcionario, antes, depois, actor_id=actor_id, origem=origem,
        tipo=tipo, data_efetiva=data_efetiva, observacao=observacao)


def registrar_promocao_historica(funcionario, data_efetiva, *, actor_id,
                                 observacao=None, antes=None):
    """Anota a data informada da promoção para o cargo atual sem mudá-lo.

    O cargo anterior desconhecido fica vazio. Repetir a mesma data/destino
    devolve o evento existente em vez de duplicar o histórico.
    """
    _validar_data(data_efetiva, obrigatoria=True)
    depois = snapshot(funcionario)
    if not depois['cargo_nome']:
        raise ValueError('Cadastre o cargo atual antes de informar a promoção.')
    existentes = RhMovimentacao.query.filter_by(
        funcionario_id=funcionario.id, tipo='promocao',
        data_efetiva=data_efetiva).all()
    for evento in existentes:
        estado = {'cargo_nome': evento.cargo_novo,
                  'familia': evento.familia_nova, 'nivel': evento.nivel_novo}
        if _chave_snapshot(estado) == _chave_snapshot(depois):
            return evento
    return _nova(
        funcionario, antes or {}, depois, actor_id=actor_id,
        origem='historico_informado', tipo='promocao',
        data_efetiva=data_efetiva, observacao=observacao)


def registrar_aplicacao_plano(funcionario, antes, *, actor_id, origem):
    """Audita uma aprovação explícita mesmo se não houver troca de cargo.

    O chamador garante que a decisão ainda não foi aprovada e controla a
    transação. A data da decisão não é presumida como data de promoção.
    """
    return _nova(
        funcionario, antes, snapshot(funcionario), actor_id=actor_id,
        origem=origem, tipo='aplicacao_plano', data_efetiva=None)


def ultimas_promocoes(funcionario_ids):
    """Última promoção conhecida por pessoa; alterações não são promoções."""
    if not funcionario_ids:
        return {}
    eventos = (RhMovimentacao.query.filter(
        RhMovimentacao.funcionario_id.in_(funcionario_ids),
        RhMovimentacao.tipo == 'promocao')
        .order_by(RhMovimentacao.data_efetiva.desc().nullslast(),
                  RhMovimentacao.registrado_em.desc(),
                  RhMovimentacao.id.desc()).all())
    ultimas = {}
    for evento in eventos:
        ultimas.setdefault(evento.funcionario_id, evento)
    return ultimas
