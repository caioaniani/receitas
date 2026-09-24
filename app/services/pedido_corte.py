"""Corte dos pedidos loja→indústria às 12h BRT (dono, 24/09/2026).

Pedidos para amanhã podem ser ajustados até 11:59:59. A partir das 12h,
criação, edição, cancelamento e exclusão ficam bloqueados para todos os
perfis, inclusive administrador, e para a automação. A edição verifica
as datas original e proposta, impedindo contornar o corte movendo o pedido.
A guarda deve rodar depois da trava da loja.

O refresh automático ocorre 30 minutos antes; a atualização das ordens,
5 minutos depois. A lista de pré-preparo continua sendo uma consulta viva,
não um snapshot criado por este serviço.

Mantém o regime de emergência de pedidos para hoje e as operações de
separação/entrega/recebimento. O corte D+1 não altera essas regras.
"""
from datetime import timedelta

from app.utils import agora

HORA_CORTE = 12


def corte_ativo(data_entrega, *, agora_dt=None):
    """True para entrega amanhã, a partir das 12h no relógio BRT.

    Datas sem entrega não participam do corte. O sistema usa BRT naive;
    `agora_dt` permite verificar a fronteira sem depender do relógio real.
    """
    if data_entrega is None:
        return False
    now = agora_dt or agora()
    return (now.hour >= HORA_CORTE
            and data_entrega == now.date() + timedelta(days=1))


def bloqueio_do_corte(datas, user=None, *, agora_dt=None):
    """Retorna (bloqueado, mensagem) para todas as datas tocadas pelo gesto.

    `user` permanece aceito por compatibilidade; nenhum perfil ignora o
    corte. Na edição, fornecer tanto a data atual quanto a nova.
    """
    if not any(corte_ativo(d, agora_dt=agora_dt) for d in datas):
        return False, None
    return True, (
        f'O pedido de AMANHÃ está fechado desde as {HORA_CORTE}:00 '
        '(horário de Brasília), horário de corte dos pedidos para a indústria. '
        'Não é possível criar, editar, cancelar ou excluir pedidos para amanhã. '
        'Para outra entrega, selecione uma data a partir de depois de amanhã.'
    )


def salvar_no_prazo(datas):
    """Grava a transação ou desfaz tudo se o processamento cruzou o corte.

    Retorna a mensagem de bloqueio, ou None após sucesso. O chamador deve
    ter adquirido a trava da loja e validado as datas antes das alterações.
    """
    from app.extensions import db

    db.session.flush()
    bloqueado, mensagem = bloqueio_do_corte(datas)
    if bloqueado:
        db.session.rollback()
        return mensagem
    db.session.commit()
    return None
