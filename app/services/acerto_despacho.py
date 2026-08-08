"""Acerto de DESPACHO DIRETO da indústria (08/08/2026, véspera do Dia dos
Pais — decisão do dono: "ajuste cirúrgico por pedido").

O problema: pedido do SITE baixa `EstoqueLoja` da loja de origem NO PAGAMENTO
(`loja_pagamento._marcar_pago` → `_baixar_estoque`) e NUNCA debita a
indústria. Num evento em que a mercadoria sai DIRETO da indústria (Dia dos
Pais: ~106 pedidos), o resultado é distorção dupla: a loja de origem drenada
por mercadoria que nunca passou na prateleira dela, e a indústria inflada
(produção creditada em `EstoqueProducao` sem débito na saída).

O acerto, POR PEDIDO (rastreável, cirúrgico):
- **Crédito na loja**: estorna a baixa do site de cada pedido pelo MOTOR
  ÚNICO (`loja_pagamento._estornar_estoque` → `baixa_venda.estornar_venda`,
  que respeita a VERSÃO da baixa e as frações). Só devolve o que saiu de
  saldo REAL (`venda_site_sem_estoque` nunca mexeu em saldo — nada a
  devolver). Os movimentos de estorno são RE-DATADOS pra data da baixa
  original (pós-revisão): sem isso, ~255 croissants negativos cairiam no
  dia da EXECUÇÃO e zerariam a demanda daquele (item, dia) na previsão
  (`prever_demanda` clampa em 0) — re-datado, a venda e o estorno se anulam
  no MESMO dia histórico, que é a semântica certa (venda de evento não é
  demanda da loja).
- **Débito na indústria**: agrega a composição FÍSICA despachada (cesta
  explodida; menu pela composição ESCOLHIDA; item sob_encomenda ENTRA aqui
  — saiu da indústria — embora nunca tenha baixado loja) e debita
  `EstoqueProducao` (receita/produto, mov `saida_site_direto`) e
  `MateriaPrima.estoque_atual` (MP), espelhando a semântica de
  `pedido_estoque.baixar_industria_pedido`: falta NUNCA vira saldo negativo
  — fica anotada nos avisos E persistida em mov
  `saida_site_direto_sem_estoque` (padrão da casa; o JSON da resposta se
  perde, o ledger não).

Idempotência POR PEDIDO em AppConfig (`acerto_despacho_<data>` = JSON de
códigos já acertados): rodar de novo só pega pedidos novos. A fase 1 do
`estornar_venda` (inteiros) exige chamada única por referência — é o
marcador que garante. Marker ILEGÍVEL levanta erro (nunca degrada pra
"nunca acertado", que re-creditaria tudo em silêncio).

CONCORRÊNCIA (pós-revisão): o executar pega um advisory lock GLOBAL do
acerto (7757) + `serializar_lojas` ascendente de TODAS as lojas envolvidas
ANTES de ler o marcador — duas execuções simultâneas não dobram estoque
(a segunda espera, relê o marcador e vira no-op) e a ordem canônica de
locks não deadlocka com o sync do Seru.

Os fluxos de cancelamento/redução pós-acerto têm guarda própria em
`loja_pagamento` (`_acertado_no_despacho`) — re-creditar a loja depois do
acerto duplicaria estoque.

Dry-run por default (`executar=False`): monta o plano inteiro sem escrever.
NUNCA rodar antes do despacho físico — o gesto é do dono, depois do evento.
"""
import json
import logging

from sqlalchemy import text

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoOnline,
)
from app.services.cestas import composicao_de_venda

logger = logging.getLogger(__name__)

# Pedido que despachou fisicamente (cancelado JÁ estornou no cancelamento;
# aguardando_pagamento nunca baixou).
STATUS_ACERTAVEIS = ('pago', 'em_preparo', 'a_caminho', 'entregue')
_TIPO_MOV_INDUSTRIA = 'saida_site_direto'
_TIPO_MOV_INDUSTRIA_SEM = 'saida_site_direto_sem_estoque'
# Advisory lock GLOBAL do acerto (registro de locks do projeto; 7756 era o
# último usado — Tiny PDV).
_LOCK_ACERTO = 7757
_TOL = 1e-6
# Referências de movimento que NASCEM do acumulador de fração — a fase 1 do
# estorno as PULA (baixa_venda.py); a prévia precisa pular igual, senão o
# dry-run promete devolver mais do que o executar devolve.
_TAGS_FRACAO = ('(fracao)', '(fator')


def _chave_marker(data):
    return f'acerto_despacho_{data.isoformat()}'


def _codigos_acertados(data):
    """Códigos já acertados. Marker ILEGÍVEL = erro alto e claro — degradar
    pra set() vazio reabriria a idempotência inteira e re-creditaria tudo."""
    bruto = AppConfig.get(_chave_marker(data))
    if not bruto:
        return set()
    try:
        v = json.loads(bruto)
    except (TypeError, ValueError) as e:
        raise ValueError(
            'marcador %s ilegível (%s) — NÃO rodar o acerto até corrigir a '
            'chave no AppConfig; degradar pra vazio re-creditaria tudo'
            % (_chave_marker(data), e)) from e
    if not isinstance(v, list):
        raise ValueError('marcador %s não é lista — corrigir antes de rodar'
                         % _chave_marker(data))
    return set(v)


def _pedidos_do_dia(data):
    return (PedidoOnline.query
            .filter(PedidoOnline.data_entrega == data,
                    PedidoOnline.divulgacao.is_(False),
                    PedidoOnline.status.in_(STATUS_ACERTAVEIS))
            .order_by(PedidoOnline.criado_em)
            .all())


def _componentes_do_pedido(pedido):
    """Composição FÍSICA despachada: [(col, id, nome, qtd_total)].

    Menu configurável usa a composição ESCOLHIDA persistida no pedido
    (`loja_estoque_reserva.composicao_escolhida`); cesta comum explode pelo
    cadastro (`composicao_de_venda`); item simples é identidade. Item
    sob_encomenda ENTRA (saiu fisicamente da indústria)."""
    from app.services.loja_estoque_reserva import composicao_escolhida
    out = []
    for it in (pedido.itens or []):
        qtd = it.quantidade or 0
        if qtd <= 0:
            continue
        comp = composicao_escolhida(it) or composicao_de_venda(
            receita_id=it.receita_id, produto_id=it.produto_id)
        if not comp:
            # Item legado sem FK — sem linha possível (espelho do WARNING de
            # baixar_industria_pedido; simétrico: também nunca baixou loja).
            logger.warning('acerto_despacho: item #%s do pedido %s sem FK '
                           '(receita/produto) — fora do débito', it.id,
                           pedido.codigo)
            continue
        for col, cid, nome, qpu in comp:
            out.append((col, cid, nome, float(qtd) * float(qpu or 0)))
    return out


def _refs_do_pedido(pedido):
    from app.services.loja_pagamento import _ref_estoque, _versao_estoque_atual
    return _ref_estoque(pedido.codigo, _versao_estoque_atual(pedido))


def _previa_credito_loja(pedido):
    """O que a fase 1 do estorno devolveria HOJE, POR LOJA: {loja: {item: un}}.

    Espelha o filtro real do `estornar_venda`: só movs `venda_site` da
    referência da versão ATUAL, PULANDO os que nasceram do acumulador de
    fração (tags '(fracao)'/'(fator' — a fase 2 os trata via
    DebitoEstoqueMov e pode devolver menos que o acumulado). Read-only."""
    ref, _pref = _refs_do_pedido(pedido)
    rows = (db.session.query(MovEstoqueLoja, EstoqueLoja, Loja)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .join(Loja, EstoqueLoja.loja_id == Loja.id)
            .filter(MovEstoqueLoja.tipo == 'venda_site',
                    db.or_(MovEstoqueLoja.referencia == ref,
                           MovEstoqueLoja.referencia.like(ref + ' %')))
            .all())
    por_loja = {}
    for mov, el, loja in rows:
        if any(t in (mov.referencia or '') for t in _TAGS_FRACAO):
            continue
        itens = por_loja.setdefault(loja.nome, {})
        itens[el.nome_item] = itens.get(el.nome_item, 0) + int(mov.quantidade or 0)
    return por_loja


def _data_baixa_original(pedido):
    """Datetime da baixa original do pedido (pra RE-DATAR o estorno)."""
    ref, _pref = _refs_do_pedido(pedido)
    return (db.session.query(db.func.max(MovEstoqueLoja.data))
            .filter(MovEstoqueLoja.tipo == 'venda_site',
                    db.or_(MovEstoqueLoja.referencia == ref,
                           MovEstoqueLoja.referencia.like(ref + ' %')))
            .scalar())


def _travar_execucao(pedidos):
    """Lock global do acerto + lojas envolvidas em ordem canônica (ANTES de
    ler o marcador). No-op fora do Postgres (SQLite dos testes)."""
    from app.services.estoque_helpers import serializar_lojas
    from app.services.loja_pagamento import _loja_baixa
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'),
                           {'k': _LOCK_ACERTO})
    lojas = set()
    for p in pedidos:
        loja = _loja_baixa(p)
        if loja is not None:
            lojas.add(loja.id)
    serializar_lojas(lojas)


def acertar(data, executar=False, usuario_id=None):
    """Monta (e, com `executar=True`, aplica) o acerto do dia. Retorna dict
    com o plano completo — o mesmo nos dois modos, pra o dry-run ser fiel."""
    pedidos = _pedidos_do_dia(data)
    if executar:
        # Locks ANTES do marcador: execução concorrente espera aqui, relê o
        # marcador já atualizado e vira no-op (nunca credita/debita 2x).
        _travar_execucao(pedidos)
    ja = _codigos_acertados(data)
    novos = [p for p in pedidos if p.codigo not in ja]

    # ── Plano: crédito POR LOJA (por pedido) + débito agregado indústria ──
    credito_por_loja = {}      # loja -> {item: un} (prévia dos inteiros)
    debito = {}                # (col, id) -> {'nome', 'qtd'}
    for p in novos:
        for loja_nome, itens in _previa_credito_loja(p).items():
            alvo = credito_por_loja.setdefault(loja_nome, {})
            for nome, q in itens.items():
                alvo[nome] = alvo.get(nome, 0) + q
        for col, cid, nome, q in _componentes_do_pedido(p):
            d = debito.setdefault((col, cid), {'nome': nome, 'qtd': 0.0})
            d['qtd'] += q

    avisos = []
    plano_debito = []
    for (col, cid), d in sorted(debito.items(), key=lambda kv: kv[1]['nome']):
        qtd = d['qtd']
        if col == 'materia_prima_id':
            mp = db.session.get(MateriaPrima, cid)
            disp = float(mp.estoque_atual or 0) if mp else 0.0
            plano_debito.append({'tipo': 'mp', 'id': cid, 'nome': d['nome'],
                                 'qtd': round(qtd, 3), 'disponivel': disp,
                                 'falta': round(max(0.0, qtd - disp), 3)})
            continue
        inteiro = int(qtd + _TOL)
        resto = qtd - inteiro
        if resto > _TOL:
            avisos.append('%s: fração de %.3f fora do débito (só inteiros '
                          'na indústria)' % (d['nome'], resto))
        disp = int(db.session.query(
            db.func.coalesce(db.func.sum(EstoqueProducao.quantidade), 0))
            .filter(getattr(EstoqueProducao, col) == cid).scalar() or 0)
        plano_debito.append({'tipo': col.replace('_id', ''), 'id': cid,
                             'nome': d['nome'], 'qtd': inteiro,
                             'disponivel': disp,
                             'falta': max(0, inteiro - disp)})

    plano = {
        'data': data.isoformat(),
        'executado': False,
        'pedidos_no_dia': len(pedidos),
        'pedidos_a_acertar': [p.codigo for p in novos],
        'pedidos_retirada': sorted(p.codigo for p in novos
                                   if p.modo_entrega == 'retirada'),
        'ja_acertados': sorted(ja & {p.codigo for p in pedidos}),
        'credito_por_loja': {lj: dict(sorted(itens.items()))
                             for lj, itens in sorted(credito_por_loja.items())},
        'credito_loja_total_un': sum(q for itens in credito_por_loja.values()
                                     for q in itens.values()),
        'debito_industria': plano_debito,
        'avisos': avisos,
    }
    if not executar:
        return plano
    if not novos:
        plano['nada_a_fazer'] = True
        return plano

    # ── Executar: transação única; qualquer erro desfaz tudo ──────────────
    from app.services.loja_pagamento import _estornar_estoque
    try:
        creditado_movs = 0
        for p in novos:
            dt_orig = _data_baixa_original(p)
            mov_max = (db.session.query(db.func.max(MovEstoqueLoja.id))
                       .scalar() or 0)
            creditado_movs += _estornar_estoque(p)
            if dt_orig is not None:
                # RE-DATA os estornos recém-criados pra data da baixa
                # original (ver docstring do módulo: previsão de demanda).
                db.session.flush()
                (MovEstoqueLoja.query
                 .filter(MovEstoqueLoja.id > mov_max,
                         MovEstoqueLoja.tipo == 'venda_site_estorno')
                 .update({'data': dt_orig}, synchronize_session=False))

        dd_mm = data.strftime('%d/%m')
        ref = ('Acerto despacho direto site %s (%d pedidos)'
               % (dd_mm, len(novos)))
        from app.services.estoque_congelados import obter_linha_producao
        for item in plano_debito:
            if item['qtd'] <= 0:
                continue
            if item['tipo'] == 'mp':
                mp = db.session.get(MateriaPrima, item['id'])
                if mp is None:
                    continue
                disp = float(mp.estoque_atual or 0)
                baixa = min(float(item['qtd']), disp)
                mp.estoque_atual = disp - baixa
                falta = float(item['qtd']) - baixa
                ref_mp = ref + (' — faltaram %g' % falta if falta > _TOL else '')
                db.session.add(MovimentacaoEstoque(
                    materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
                    referencia=ref_mp, usuario_id=usuario_id))
                if falta > _TOL:
                    avisos.append('%s (MP): havia %g de %g — faltaram %g'
                                  % (item['nome'], disp, item['qtd'], falta))
                continue
            ep = obter_linha_producao(
                receita_id=item['id'] if item['tipo'] == 'receita' else None,
                produto_id=item['id'] if item['tipo'] == 'produto' else None,
                usuario_id=usuario_id)
            disp = int(ep.quantidade or 0)
            baixa = min(int(item['qtd']), disp)
            ep.quantidade = disp - baixa
            if baixa > 0:
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo=_TIPO_MOV_INDUSTRIA,
                    quantidade=baixa, referencia=ref, usuario_id=usuario_id))
            falta = int(item['qtd']) - baixa
            if falta > 0:
                # Falta NUNCA vira saldo negativo — anotada no retorno E
                # persistida no ledger (o JSON da resposta se perde).
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo=_TIPO_MOV_INDUSTRIA_SEM,
                    quantidade=falta,
                    referencia=ref + ' — sem saldo', usuario_id=usuario_id))
                avisos.append('%s: indústria tinha %d de %d — faltaram %d'
                              % (item['nome'], disp, item['qtd'], falta))

        AppConfig.set(_chave_marker(data),
                      json.dumps(sorted(ja | {p.codigo for p in novos})))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('acerto_despacho falhou — transação desfeita')
        raise

    plano['executado'] = True
    # CONTAGEM de movimentos revertidos (retorno do estornar_venda), não
    # unidades — o total de unidades previsto está em credito_loja_total_un.
    plano['credito_aplicado_movs'] = creditado_movs
    plano['avisos'] = avisos
    return plano
