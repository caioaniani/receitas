"""Lógica de domínio da Lista de Compras semanal.

Helpers reusados pelas rotas (blueprint `lista_compras`) e pelo seed:
- domingo da semana corrente (BRT)
- obter/criar semana de uma loja
- histórico (tenho/pedido/sobrou) da semana anterior pra referência da UI
- salvar quantidades parciais (auto-save) + transições de status

Mantém regras de negócio (status válidos, quem pode editar o quê) num só lugar.
"""
from datetime import timedelta

from app.extensions import db
from app.utils import agora, hoje

STATUS_ABERTA = 'aberta'
STATUS_ENVIADA = 'enviada'
STATUS_FECHADA = 'fechada'
STATUS_VALIDOS = (STATUS_ABERTA, STATUS_ENVIADA, STATUS_FECHADA)


def domingo_da_semana(d=None):
    """Domingo da semana que contém `d` (default: hoje em BRT).

    Python weekday: Mon=0..Sun=6. Pra voltar pro domingo:
    delta = (weekday + 1) % 7  (0 se ja domingo, 1 se segunda, ... 6 se sabado).
    """
    if d is None:
        d = hoje()
    return d - timedelta(days=(d.weekday() + 1) % 7)


def obter_ou_criar_semana(loja_id, data_inicio=None, criado_por_id=None):
    """Retorna a ListaComprasSemana de (loja_id, data_inicio). Cria se nao
    existir, com status 'aberta'."""
    from app.models import ListaComprasSemana
    if data_inicio is None:
        data_inicio = domingo_da_semana()
    sem = (ListaComprasSemana.query
           .filter_by(loja_id=loja_id, data_semana_inicio=data_inicio)
           .first())
    if sem:
        return sem
    sem = ListaComprasSemana(
        loja_id=loja_id,
        data_semana_inicio=data_inicio,
        status=STATUS_ABERTA,
        criado_por_id=criado_por_id,
    )
    db.session.add(sem)
    db.session.commit()
    return sem


def historico_anterior(loja_id, data_inicio):
    """Dict {item_id: {'tenho': N, 'pedido': N, 'sobrou': N}} da SEMANA
    IMEDIATAMENTE ANTERIOR à `data_inicio` (mesma loja).

    Usado pra mostrar 'semana passada: tinha X · pediram Y · sobrou Z' ao
    lado de cada item na tela do gerente.
    """
    from app.models import ListaComprasSemana
    anterior_data = data_inicio - timedelta(days=7)
    sem_ant = (ListaComprasSemana.query
               .filter_by(loja_id=loja_id, data_semana_inicio=anterior_data)
               .first())
    if not sem_ant:
        return {}
    out = {}
    for q in sem_ant.quantidades:
        out[q.item_id] = {
            'tenho': q.tenho, 'pedido': q.pedido, 'sobrou': q.sobrou,
        }
    return out


def _obter_ou_criar_qtd(semana_id, item_id):
    from app.models import ListaComprasItemQtd
    q = (ListaComprasItemQtd.query
         .filter_by(semana_id=semana_id, item_id=item_id).first())
    if q:
        return q
    q = ListaComprasItemQtd(semana_id=semana_id, item_id=item_id)
    db.session.add(q)
    return q


def salvar_tenho(semana, item_id, tenho):
    """Auto-save do gerente: atualiza 'tenho' de um item.
    Bloqueia se semana ja foi enviada/fechada."""
    if semana.status != STATUS_ABERTA:
        return False, f'semana já {semana.status}'
    try:
        tenho = max(0, int(tenho or 0))
    except (TypeError, ValueError):
        return False, 'quantidade inválida'
    q = _obter_ou_criar_qtd(semana.id, item_id)
    q.tenho = tenho
    q.atualizado_em = agora()
    db.session.commit()
    return True, None


def enviar_semana(semana, usuario_id):
    """Gerente fecha o preenchimento e manda pro gerente geral."""
    if semana.status != STATUS_ABERTA:
        return False, f'semana já {semana.status}'
    semana.status = STATUS_ENVIADA
    semana.enviada_em = agora()
    semana.enviada_por_id = usuario_id
    db.session.commit()
    return True, None


def salvar_pedido_sobrou(semana, item_id, pedido=None, sobrou=None):
    """Gerente geral preenche/atualiza 'pedido' (vou pedir agora) e/ou
    'sobrou' (atualiza histórico). Permite editar ainda quando 'aberta' tb."""
    if semana.status == STATUS_FECHADA:
        return False, 'semana já fechada'
    q = _obter_ou_criar_qtd(semana.id, item_id)
    if pedido is not None:
        try:
            q.pedido = max(0, int(pedido or 0))
        except (TypeError, ValueError):
            return False, 'pedido inválido'
    if sobrou is not None:
        try:
            q.sobrou = max(0, int(sobrou or 0))
        except (TypeError, ValueError):
            return False, 'sobrou inválido'
    q.atualizado_em = agora()
    db.session.commit()
    return True, None


def fechar_semana(semana, usuario_id):
    """Gerente geral marca semana como comprada (status final)."""
    if semana.status == STATUS_FECHADA:
        return False, 'semana já fechada'
    semana.status = STATUS_FECHADA
    semana.fechada_em = agora()
    semana.fechada_por_id = usuario_id
    db.session.commit()
    return True, None


def reabrir_semana(semana):
    """Volta uma semana enviada/fechada pra 'aberta' (so admin/owner usa)."""
    semana.status = STATUS_ABERTA
    semana.enviada_em = None
    semana.enviada_por_id = None
    semana.fechada_em = None
    semana.fechada_por_id = None
    db.session.commit()
    return True, None


def itens_da_loja_agrupados(loja_id):
    """Catálogo da loja organizado em [(grupo, [itens]), ...] na ordem de UI.
    Ignora itens inativos."""
    from app.models import ItemListaCompras
    itens = (ItemListaCompras.query
             .filter_by(loja_id=loja_id, ativo=True)
             .order_by(ItemListaCompras.grupo,
                       ItemListaCompras.ordem,
                       ItemListaCompras.nome_item)
             .all())
    grupos = {}
    ordem_grupos = []
    for it in itens:
        if it.grupo not in grupos:
            grupos[it.grupo] = []
            ordem_grupos.append(it.grupo)
        grupos[it.grupo].append(it)
    return [(g, grupos[g]) for g in ordem_grupos]


def quantidades_por_item(semana):
    """{item_id: ListaComprasItemQtd} pra UI."""
    return {q.item_id: q for q in semana.quantidades}
