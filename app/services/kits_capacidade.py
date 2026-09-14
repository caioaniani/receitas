"""Reserva de capacidade das datas de kits, sem qualquer estoque físico."""
import logging

from app.extensions import db
from app.services import loja_plano_dia
from app.utils import agora

logger = logging.getLogger(__name__)


def _operacoes(entregas):
    operacoes = []
    for registro in entregas:
        pedido = registro.pedido
        itens = {}
        for item in pedido.itens:
            item_id = item.receita_id or item.produto_id
            if not item_id or not pedido.data_entrega:
                raise ValueError('Entrega de kit sem produto vinculado ou data')
            chave = (item.kind, item_id)
            anterior = itens.get(chave, (0, item.nome))
            itens[chave] = (anterior[0] + int(item.quantidade), item.nome)
        for (kind, item_id), (quantidade, nome) in itens.items():
            operacoes.append((pedido.data_entrega, kind, item_id,
                              registro.pedido_id, registro, quantidade, nome))
    return sorted(operacoes, key=lambda op: op[:4])


def reservar_compra(compra, *, pagamento_recebido=False):
    """Segura todos os dias atomicamente; caller confirma ou desfaz a transação.

    Dinheiro recebido após expiração deve aparecer como demanda real mesmo
    quando a capacidade liberada já foi vendida. Nessa exceção reserva acima
    do limite e registra alerta permanente na entrega para atuação do owner.
    """
    entregas = [e for e in compra.entregas if not e.reserva_plano]
    for dia, kind, item_id, _, registro, quantidade, nome in _operacoes(entregas):
        reservou = loja_plano_dia.reservar(kind, item_id, dia, quantidade, commit=False)
        if not reservou:
            if not pagamento_recebido:
                return False, [f'{dia:%d/%m/%Y}: {nome} não tem capacidade suficiente. '
                               'Escolha outra data; nenhuma cobrança foi feita.']
            loja_plano_dia.reservar(kind, item_id, dia, quantidade, commit=False, forcar=True)
            alerta = (f'{agora():%d/%m/%Y %H:%M}: pagamento recebido após liberação '
                      f'da capacidade; {quantidade}x {nome} excede o limite da data {dia:%d/%m/%Y}. '
                      'Conferir produção e combinar a entrega com o cliente.')
            registro.alerta_capacidade = '\n'.join(filter(None, [registro.alerta_capacidade, alerta]))
            logger.warning('Kit %s, entrega %s: %s', compra.id, registro.pedido_id, alerta)
    for registro in entregas:
        registro.reserva_plano = True
    db.session.flush()
    return True, []


def liberar_entregas(entregas):
    """Devolve somente reservas deste kit, uma vez, com locks na mesma ordem."""
    ativas = [e for e in entregas if e.reserva_plano and not e.coletado_em]
    for dia, kind, item_id, _, _registro, quantidade, _nome in _operacoes(ativas):
        loja_plano_dia.devolver(kind, item_id, dia, quantidade, commit=False)
    for registro in ativas:
        registro.reserva_plano = False
    db.session.flush()


def liberar_compra(compra):
    liberar_entregas(compra.entregas)
