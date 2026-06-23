"""Plano de estoque do site por dia de entrega (22/06/2026).

Substitui o filtro de "esgotado" do `loja_catalogo._estoque_site_map()` quando
ha plano cadastrado pra aquela data. Sem plano = comportamento de antes
(fail-open / fallback no EstoqueLoja). Permite ao dono definir "hoje 0
foccacia, sexta 20" sem mexer no estoque fisico (que segue em EstoqueLoja).

Operacoes principais:
- `saldo(kind, item_id, data)`: quanto sobra pra vender no site naquela data.
  Devolve `None` se NAO existe plano (= sem controle, vitrine cai no fallback).
- `tem_plano(data)`: True se ha qualquer linha de plano pra essa data
  (controla se a vitrine consulta o plano ou cai no estoque atual).
- `reservar(kind, item_id, data, qtd)`: incrementa qtd_reservada com lock —
  retorna True se conseguiu, False se nao tem saldo. Chamado no webhook
  pagar.me quando o pedido vira 'pago'.
- `devolver(kind, item_id, data, qtd)`: decrementa qtd_reservada — chamado
  no cancelamento/reembolso.
- `definir(kind, item_id, data, qtd_planejada)`: upsert do planejado (tela
  admin).

Padrao igual ao `loja_estoque_reserva.py` (EstoqueLoja): row-level lock
(SELECT FOR UPDATE) pra evitar race condition de oversell.
"""
import logging
from datetime import date as _date_type

from sqlalchemy import func, select

from app.extensions import db
from app.models import EstoqueSitePlano

logger = logging.getLogger(__name__)


def saldo(kind, item_id, data):
    """qtd_planejada - qtd_reservada pra (item, data). Devolve `None` se NAO
    existe plano cadastrado — sinaliza pro caller "sem controle" (vitrine
    cai no fallback do EstoqueLoja, igual antes)."""
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind=kind, item_id=item_id, data=data)
           .first())
    if row is None:
        return None
    return max(0, (row.qtd_planejada or 0) - (row.qtd_reservada or 0))


def tem_plano(data):
    """True se ha alguma linha cadastrada pra essa data. Usado pela vitrine
    pra decidir entre "consulta plano" e "cai no EstoqueLoja"."""
    n = (db.session.query(func.count(EstoqueSitePlano.id))
         .filter_by(data=data).scalar())
    return bool(n)


def saldos_para_dia(data):
    """{(kind, item_id): saldo} de TUDO que ta planejado pra essa data.
    Vitrine usa isso pra marcar esgotado/disponivel."""
    rows = (db.session.query(EstoqueSitePlano)
            .filter_by(data=data).all())
    return {(r.kind, r.item_id): max(0, (r.qtd_planejada or 0)
                                     - (r.qtd_reservada or 0))
            for r in rows}


def definir(kind, item_id, data, qtd_planejada):
    """Upsert do planejado (tela admin). NAO mexe em reservada.
    qtd_planejada deve ser >= 0; 0 = ESGOTADO planejado (cliente nao compra)."""
    if qtd_planejada < 0:
        raise ValueError('qtd_planejada nao pode ser negativa')
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind=kind, item_id=item_id, data=data)
           .first())
    if row is None:
        row = EstoqueSitePlano(kind=kind, item_id=item_id, data=data,
                                qtd_planejada=qtd_planejada,
                                qtd_reservada=0)
        db.session.add(row)
    else:
        row.qtd_planejada = qtd_planejada
    db.session.commit()
    return row


def reservar(kind, item_id, data, qtd):
    """Reserva `qtd` no plano de (item, data). Atomico — pega row lock
    (SELECT FOR UPDATE no Postgres) pra evitar oversell.

    Retorna True se conseguiu, False se nao tem saldo. Se nao existe plano
    pra esse item naquela data, AUTO-CRIA com qtd_planejada=0 e qtd_reservada=qtd
    (deixa saldo NEGATIVO virtual no banco — mas saldo() trunca em 0). Isso
    eh proposital pra a baixa fisica e a reserva continuarem batendo mesmo
    quando o dono esqueceu de planejar (auditoria fica clara: rows com saldo
    negativo = vendeu sem planejar).

    Sem plano de dia: ainda eh chamado, mas eh idempotente em servico de
    cancelamento. Caller decide se chamar baseado em `tem_plano(data)`."""
    if qtd <= 0:
        return True
    # SELECT FOR UPDATE: trava a linha pra evitar 2 reservas simultaneas
    # gerarem oversell. fallback gracioso se nao existe linha (cria).
    try:
        # Postgres suporta with_for_update; SQLite ignora silenciosamente.
        stmt = (select(EstoqueSitePlano)
                .filter_by(kind=kind, item_id=item_id, data=data)
                .with_for_update())
        row = db.session.execute(stmt).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        row = (db.session.query(EstoqueSitePlano)
               .filter_by(kind=kind, item_id=item_id, data=data).first())
    if row is None:
        # Sem plano: cria linha com 0 planejado e ja reserva — saldo virtual
        # negativo deixa rastro do oversell pra auditoria.
        row = EstoqueSitePlano(kind=kind, item_id=item_id, data=data,
                                qtd_planejada=0, qtd_reservada=qtd)
        db.session.add(row)
        db.session.commit()
        return True  # nao havia limite cadastrado; vendeu mesmo assim
    disponivel = (row.qtd_planejada or 0) - (row.qtd_reservada or 0)
    if disponivel < qtd:
        return False
    row.qtd_reservada = (row.qtd_reservada or 0) + qtd
    db.session.commit()
    return True


def devolver(kind, item_id, data, qtd):
    """Decrementa qtd_reservada (cancelamento/reembolso). NAO recria linha se
    nao existir — devolver algo que nunca foi reservado eh no-op."""
    if qtd <= 0:
        return
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind=kind, item_id=item_id, data=data).first())
    if row is None:
        logger.warning(
            'loja_plano_dia.devolver: tentou devolver %s na linha %s/%s/%s '
            'que nao existe (no-op)', qtd, kind, item_id, data)
        return
    nova = max(0, (row.qtd_reservada or 0) - qtd)
    row.qtd_reservada = nova
    db.session.commit()


def _hoje_brt():
    """Hoje em BRT (delegado ao app.utils, evita importar agora() neste topo
    sem precisar)."""
    from app.utils import hoje
    return hoje()


def planos_proximos_dias(dias=14, comeco=None):
    """Pra a tela admin de planejamento: devolve {(kind, item_id, data): row}
    pra todos os planos nos proximos `dias` dias a partir de `comeco`
    (default hoje BRT). Inclui datas SEM linha tambem ficam ausentes no
    dict; o template preenche com 0."""
    from datetime import timedelta
    ini = comeco or _hoje_brt()
    fim = ini + timedelta(days=dias - 1)
    rows = (db.session.query(EstoqueSitePlano)
            .filter(EstoqueSitePlano.data >= ini,
                    EstoqueSitePlano.data <= fim)
            .all())
    if not isinstance(ini, _date_type):
        raise TypeError('comeco precisa ser date')
    return {(r.kind, r.item_id, r.data): r for r in rows}
