"""Disponibilidade do site por regra semanal e excecao de entrega.

As regras semanais resolvem a rotina (ex.: focaccia somente sabado e domingo),
as excecoes cuidam de uma data fora do normal e o plano diario antigo continua
como fallback. Nada aqui mexe no estoque fisico da loja.

DEFAULT_QTD_PLANEJADA = 99999 (24/06/2026): quando o servidor auto-cria
linha (caller `reservar` sem plano cadastrado), usa esse valor — alinha com
a regra do dono "campo vazio na tela = 99999 = sem limite". Antes era 0, que
deixava o item ESGOTADO depois da primeira venda (caso real: Bonjura e Box
Mimo ficaram esgotados sem o dono ter setado limite).

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
from app.models import (
    EstoqueSiteExcecao,
    EstoqueSitePlano,
    EstoqueSiteRegraSemanal,
)

logger = logging.getLogger(__name__)

# Valor que a tela admin mostra no campo "Planejado" quando nao ha linha
# cadastrada (= "sem limite"). O servidor TAMBEM usa esse valor quando
# auto-cria linha em `reservar` — alinha com a expectativa do dono.
DEFAULT_QTD_PLANEJADA = 99999


def _limite_como_planejado(qtd_limite):
    """Traduz o ``None`` legivel da regra (sem limite) pro valor interno."""
    return DEFAULT_QTD_PLANEJADA if qtd_limite is None else qtd_limite


def _planejado_efetivo(kind, item_id, data, *, row_plano=None):
    """Retorna ``(quantidade, fonte)`` aplicando a precedencia da tela nova.

    Excecao por data > regra semanal > plano diario legado > livre. A reserva
    fica no plano diario, mas nunca transforma uma venda em excecao: quando
    ha regra semanal, o ``qtd_planejada`` dessa linha e ignorado.
    """
    excecao = (db.session.query(EstoqueSiteExcecao)
               .filter_by(kind=kind, item_id=item_id, data=data)
               .first())
    if excecao is not None:
        return _limite_como_planejado(excecao.qtd_limite), 'excecao'

    regra = (db.session.query(EstoqueSiteRegraSemanal)
             .filter_by(kind=kind, item_id=item_id)
             .first())
    if regra is not None:
        if not regra.permite(data):
            return 0, 'regra_semanal'
        return _limite_como_planejado(regra.qtd_limite), 'regra_semanal'

    if row_plano is None:
        row_plano = (db.session.query(EstoqueSitePlano)
                     .filter_by(kind=kind, item_id=item_id, data=data)
                     .first())
    if row_plano is not None:
        return row_plano.qtd_planejada or 0, 'plano_anterior'
    return None, 'livre'


def configuracao_dia(kind, item_id, data):
    """Configuracao resolvida para a UI, sem expor o sentinela ``99999``."""
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind=kind, item_id=item_id, data=data)
           .first())
    planejado, fonte = _planejado_efetivo(
        kind, item_id, data, row_plano=row)
    reservado = (row.qtd_reservada or 0) if row else 0
    return {
        'fonte': fonte,
        'qtd_limite': (None if planejado is None
                       or planejado >= DEFAULT_QTD_PLANEJADA
                       else planejado),
        'sem_limite': planejado is None
        or planejado >= DEFAULT_QTD_PLANEJADA,
        'disponivel': planejado is None or planejado > reservado,
        'qtd_reservada': reservado,
        'saldo': (None if planejado is None
                  or planejado >= DEFAULT_QTD_PLANEJADA
                  else max(0, planejado - reservado)),
    }


def salvar_regra_semanal(kind, item_id, dias, qtd_limite=None):
    """Cria ou atualiza a regra recorrente de um item."""
    dias = {int(d) for d in dias}
    if not dias:
        raise ValueError('escolha pelo menos um dia da semana')
    if any(d < 0 or d > 6 for d in dias):
        raise ValueError('dia da semana invalido')
    if qtd_limite is not None and qtd_limite <= 0:
        raise ValueError('o limite precisa ser maior que zero')
    mask = sum(1 << d for d in dias)
    row = (db.session.query(EstoqueSiteRegraSemanal)
           .filter_by(kind=kind, item_id=item_id).first())
    if row is None:
        row = EstoqueSiteRegraSemanal(
            kind=kind, item_id=item_id, dias_mask=mask,
            qtd_limite=qtd_limite)
        db.session.add(row)
    else:
        row.dias_mask = mask
        row.qtd_limite = qtd_limite
    db.session.commit()
    return row


def remover_regra_semanal(kind, item_id):
    row = (db.session.query(EstoqueSiteRegraSemanal)
           .filter_by(kind=kind, item_id=item_id).first())
    if row is not None:
        db.session.delete(row)
        db.session.commit()


def salvar_excecao(kind, item_id, data, qtd_limite=None):
    """Salva excecao: ``None`` libera; zero bloqueia; positivo limita."""
    if qtd_limite is not None and qtd_limite < 0:
        raise ValueError('o limite nao pode ser negativo')
    row = (db.session.query(EstoqueSiteExcecao)
           .filter_by(kind=kind, item_id=item_id, data=data).first())
    if row is None:
        row = EstoqueSiteExcecao(
            kind=kind, item_id=item_id, data=data,
            qtd_limite=qtd_limite)
        db.session.add(row)
    else:
        row.qtd_limite = qtd_limite
    db.session.commit()
    return row


def remover_excecao(kind, item_id, data):
    row = (db.session.query(EstoqueSiteExcecao)
           .filter_by(kind=kind, item_id=item_id, data=data).first())
    if row is not None:
        db.session.delete(row)
    # Se nao ha regra semanal, um plano diario legado ainda seria aplicado
    # depois de remover a excecao e a opcao "seguir a regra" pareceria nao
    # funcionar. Neutraliza apenas o limite antigo; preserva a reserva/venda.
    regra = (db.session.query(EstoqueSiteRegraSemanal)
             .filter_by(kind=kind, item_id=item_id).first())
    if regra is None:
        plano = (db.session.query(EstoqueSitePlano)
                 .filter_by(kind=kind, item_id=item_id, data=data).first())
        if plano is not None:
            plano.qtd_planejada = (
                DEFAULT_QTD_PLANEJADA + (plano.qtd_reservada or 0))
    db.session.commit()


def saldo(kind, item_id, data):
    """qtd_planejada - qtd_reservada pra (item, data). Devolve `None` se NAO
    existe plano cadastrado — sinaliza pro caller "sem controle" (vitrine
    cai no fallback do EstoqueLoja, igual antes)."""
    row = (db.session.query(EstoqueSitePlano)
           .filter_by(kind=kind, item_id=item_id, data=data).first())
    planejado, _fonte = _planejado_efetivo(
        kind, item_id, data, row_plano=row)
    if planejado is None:
        return None
    reservado = (row.qtd_reservada or 0) if row else 0
    return max(0, planejado - reservado)


def tem_plano(data):
    """True se ha alguma linha cadastrada pra essa data. Usado pela vitrine
    pra decidir entre "consulta plano" e "cai no EstoqueLoja"."""
    n_plano = (db.session.query(func.count(EstoqueSitePlano.id))
               .filter_by(data=data).scalar())
    n_excecao = (db.session.query(func.count(EstoqueSiteExcecao.id))
                 .filter_by(data=data).scalar())
    n_regra = db.session.query(func.count(EstoqueSiteRegraSemanal.id)).scalar()
    return bool(n_plano or n_excecao or n_regra)


def saldos_para_dia(data):
    """{(kind, item_id): saldo} de TUDO que ta planejado pra essa data.
    Vitrine usa isso pra marcar esgotado/disponivel."""
    planos = {(r.kind, r.item_id): r
              for r in db.session.query(EstoqueSitePlano)
              .filter_by(data=data).all()}
    excecoes = {(r.kind, r.item_id): r
                for r in db.session.query(EstoqueSiteExcecao)
                .filter_by(data=data).all()}
    regras = {(r.kind, r.item_id): r
              for r in db.session.query(EstoqueSiteRegraSemanal).all()}
    chaves = set(planos) | set(excecoes) | set(regras)
    out = {}
    for chave in chaves:
        row = planos.get(chave)
        if chave in excecoes:
            planejado = _limite_como_planejado(
                excecoes[chave].qtd_limite)
        elif chave in regras:
            regra = regras[chave]
            planejado = (_limite_como_planejado(regra.qtd_limite)
                          if regra.permite(data) else 0)
        elif row is not None:
            planejado = row.qtd_planejada or 0
        else:  # pragma: no cover - a uniao das chaves torna impossivel
            continue
        reservado = (row.qtd_reservada or 0) if row else 0
        out[chave] = max(0, planejado - reservado)
    return out


def saldos_no_periodo(di, df):
    """{data: {(kind, item_id): saldo}} resolvido num intervalo.

    Carrega planos, excecoes e regras em tres consultas fixas, sem fazer uma
    nova consulta para cada dia que o vigia do bot percorre.
    """
    from datetime import timedelta

    planos = (db.session.query(EstoqueSitePlano)
              .filter(EstoqueSitePlano.data >= di,
                      EstoqueSitePlano.data <= df).all())
    excecoes = (db.session.query(EstoqueSiteExcecao)
                .filter(EstoqueSiteExcecao.data >= di,
                        EstoqueSiteExcecao.data <= df).all())
    regras = db.session.query(EstoqueSiteRegraSemanal).all()
    planos_por_dia = {}
    for row in planos:
        planos_por_dia.setdefault(row.data, {})[
            (row.kind, row.item_id)] = row
    excecoes_por_dia = {}
    for row in excecoes:
        excecoes_por_dia.setdefault(row.data, {})[
            (row.kind, row.item_id)] = row
    regras_map = {(r.kind, r.item_id): r for r in regras}

    out = {}
    data = di
    while data <= df:
        planos_dia = planos_por_dia.get(data, {})
        excecoes_dia = excecoes_por_dia.get(data, {})
        chaves = set(planos_dia) | set(excecoes_dia) | set(regras_map)
        saldos = {}
        for chave in chaves:
            row = planos_dia.get(chave)
            if chave in excecoes_dia:
                planejado = _limite_como_planejado(
                    excecoes_dia[chave].qtd_limite)
            elif chave in regras_map:
                regra = regras_map[chave]
                planejado = (_limite_como_planejado(regra.qtd_limite)
                              if regra.permite(data) else 0)
            else:
                planejado = row.qtd_planejada or 0
            reservado = (row.qtd_reservada or 0) if row else 0
            saldos[chave] = max(0, planejado - reservado)
        if saldos:
            out[data] = saldos
        data += timedelta(days=1)
    return out


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
    pra esse item naquela data, AUTO-CRIA com DEFAULT_QTD_PLANEJADA (99999):
    alinha com a regra "sem plano cadastrado = sem limite" da tela admin.

    Antes de 24/06/2026, criava com qtd_planejada=0 — o que zerava o item no
    site DEPOIS da primeira venda (incidente Bonjura/Box Mimo: dono esqueceu
    de setar limite manual; primeira venda deixou item esgotado). Agora cria
    com 99999, replicando o comportamento default da tela.

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
    planejado, fonte = _planejado_efetivo(
        kind, item_id, data, row_plano=row)
    if planejado is None:
        planejado = DEFAULT_QTD_PLANEJADA
    reservado = (row.qtd_reservada or 0) if row else 0
    disponivel = planejado - reservado
    if disponivel < qtd:
        return False
    if row is None:
        # A linha diaria guarda a reserva/auditoria. Com regra semanal ou
        # excecao, seu ``qtd_planejada`` nao vira uma nova excecao porque a
        # resolucao sempre prioriza as tabelas novas.
        row = EstoqueSitePlano(
            kind=kind, item_id=item_id, data=data,
            qtd_planejada=planejado, qtd_reservada=qtd)
        db.session.add(row)
    else:
        row.qtd_reservada = reservado + qtd
        if fonte in ('regra_semanal', 'excecao'):
            row.qtd_planejada = planejado
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


def reparar_linhas_orfas():
    """Corrige linhas com qtd_planejada=0 e qtd_reservada>0 (criadas pelo
    bug pre-24/06/2026 do `reservar`). Sobe o planejado pra default + reservado
    pra restaurar saldo positivo.

    Ex: linha (planejada=0, reservada=1) → vira (planejada=99999+1=100000,
    reservada=1). Saldo = 99999 — item volta a vender.

    Idempotente: linhas já normais (planejada > reservada) NÃO mexe.
    Retorna a lista de linhas corrigidas pra log."""
    quebradas = (db.session.query(EstoqueSitePlano)
                 .filter(EstoqueSitePlano.qtd_planejada == 0,
                         EstoqueSitePlano.qtd_reservada > 0)
                 .all())
    corrigidas = []
    for row in quebradas:
        antes = row.qtd_planejada
        # Mantém saldo = 99999 (qtd_reservada continua, qtd_planejada sobe).
        row.qtd_planejada = DEFAULT_QTD_PLANEJADA + (row.qtd_reservada or 0)
        corrigidas.append({
            'kind': row.kind, 'item_id': row.item_id,
            'data': row.data.isoformat() if row.data else None,
            'antes': antes, 'depois': row.qtd_planejada,
            'reservada': row.qtd_reservada,
        })
    if corrigidas:
        db.session.commit()
        logger.warning('plano_dia.reparar_linhas_orfas: corrigidas %d linhas',
                       len(corrigidas))
    return corrigidas


def _hoje_brt():
    """Hoje em BRT (delegado ao app.utils, evita importar agora() neste topo
    sem precisar)."""
    from app.utils import hoje
    return hoje()


def replicar_para_proximos_dias(kind, item_id, qtd_planejada, *,
                                  data_inicio, dias=14):
    """Cria/atualiza linhas pra (item, data_inicio..data_inicio+dias-1) com o
    mesmo `qtd_planejada`. SOBRESCREVE valores existentes — o usuario clicou
    em "replicar" sabendo o que quer.

    Decisao do dono 23/06/2026 — fluxo padrao = "default 99999, eu altero e
    peco pra replicar". Sem sobrescrita, replicar so afetaria dias virgens
    e nao bate a expectativa."""
    from datetime import timedelta
    if qtd_planejada < 0:
        raise ValueError('qtd_planejada nao pode ser negativa')
    n = 0
    for i in range(dias):
        d = data_inicio + timedelta(days=i)
        definir(kind, item_id, d, qtd_planejada)
        n += 1
    return n


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
