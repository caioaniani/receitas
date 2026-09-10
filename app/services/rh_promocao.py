"""Promoção individual explícita: prévia, cargo vigente e histórico juntos.

O chamador autoriza a operação, verifica a prévia contra o estado atual sob
lock e controla a transação. Este serviço não faz commit, não cria cargos ou
vínculos e não altera acesso, liderança, benefícios ou folhas já fechadas.
"""
from datetime import date, datetime

from app.extensions import db
from app.models import Cargo, PlanoCarreiraCargoVinculo, PlanoCarreiraFaixa, Usuario
from app.services import plano_carreira_import, rh_movimentacao
from app.services.rh_cargos import nome_cargo_exibicao, normalizar_nome_cargo
from app.services.rh_remuneracao import comparar
from app.utils import hoje


def _chave_cargo(nome):
    return normalizar_nome_cargo(nome_cargo_exibicao(nome))


def cargos_disponiveis(funcionario):
    """Exclui inativos e aliases do cargo vigente, sem sugerir uma promoção."""
    atual = rh_movimentacao.snapshot(funcionario)
    chave_atual = _chave_cargo(atual['cargo_nome'])
    return [cargo for cargo in Cargo.query.filter_by(ativo=True)
            .order_by(Cargo.nome, Cargo.id).all()
            if _chave_cargo(cargo.nome) != chave_atual]


def _faixa_destino(cargo):
    faixas = (PlanoCarreiraFaixa.query
              .join(PlanoCarreiraCargoVinculo,
                    PlanoCarreiraCargoVinculo.faixa_id == PlanoCarreiraFaixa.id)
              .filter(PlanoCarreiraCargoVinculo.cargo_id == cargo.id).all())
    if len(faixas) > 1:
        raise ValueError(
            'O novo cargo está ligado a mais de uma faixa do plano. '
            'Revise os vínculos antes de promover.')
    return faixas[0] if faixas else None


def prever(funcionario, cargo, data_efetiva, *, observacao=None):
    """Valida a decisão e descreve seus efeitos, sem alterar o cadastro.

    A confiança conserva a regra vigente de 40% da base; seu valor em reais
    acompanha a nova base. O total de referência exclui VT, VR e horas extras
    e não é o total líquido da folha. A premiação permanece como cadastrada.
    """
    with db.session.no_autoflush:
        if not funcionario or not funcionario.id:
            raise ValueError('Salve o cadastro antes de registrar a promoção.')
        if funcionario.ativo is not True:
            raise ValueError('Somente funcionários ativos podem ser promovidos.')
        if not cargo or not cargo.id or cargo.ativo is not True:
            raise ValueError('Escolha um cargo ativo para a promoção.')
        if (not isinstance(data_efetiva, date)
                or isinstance(data_efetiva, datetime)):
            raise ValueError('Informe uma data de promoção válida.')
        if data_efetiva > hoje():
            raise ValueError('A data de promoção não pode estar no futuro.')
        if funcionario.data_admissao and data_efetiva < funcionario.data_admissao:
            raise ValueError('A data de promoção não pode ser anterior à admissão.')
        observacao = (observacao or '').strip()
        if len(observacao) > 2000:
            raise ValueError('A observação deve ter no máximo 2000 caracteres.')
        antes = rh_movimentacao.snapshot(funcionario)
        if _chave_cargo(antes['cargo_nome']) == _chave_cargo(cargo.nome):
            raise ValueError('Escolha um cargo diferente do cargo atual.')
        faixa = _faixa_destino(cargo)
        enquadramento = funcionario.enquadramento_carreira
        if enquadramento and not faixa:
            raise ValueError(
                'O novo cargo não possui faixa vinculada no plano de carreira. '
                'Corrija o vínculo no plano antes de promover.')
        return {
            'antes': antes,
            'cargo_atual': antes['cargo_nome'],
            'cargo_novo': nome_cargo_exibicao(cargo.nome),
            'cargo_novo_id': cargo.id,
            **comparar(funcionario, cargo),
            'faixa_destino': faixa,
            'plano_sera_sincronizado': bool(enquadramento and faixa),
            'data_efetiva': data_efetiva,
            'observacao': observacao,
            'avisos': [],
        }


def aplicar(funcionario, cargo, data_efetiva, *, actor_id, observacao=None):
    """Aplica a promoção confirmada e registra seu histórico, sem commit."""
    with db.session.no_autoflush:
        actor = db.session.get(Usuario, actor_id) if actor_id else None
        if not actor or not actor.is_dono():
            raise ValueError('Somente o dono pode confirmar uma promoção.')
        previa = prever(funcionario, cargo, data_efetiva, observacao=observacao)
        funcionario.cargo = cargo
        funcionario.cargo_id = cargo.id
        funcionario.funcao = cargo.nome
        funcionario.salario_base = cargo.salario_base
        plano_carreira_import.sincronizar_enquadramento_com_cargo(funcionario)
        evento = rh_movimentacao.registrar_mudanca(
            funcionario, previa['antes'], actor_id=actor_id,
            origem='promocao_equipe', tipo='promocao',
            data_efetiva=data_efetiva, observacao=previa['observacao'])
        return {**previa, 'evento': evento}
