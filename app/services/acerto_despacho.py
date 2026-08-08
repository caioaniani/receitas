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
  devolver).
- **Débito na indústria**: agrega a composição FÍSICA despachada (cesta
  explodida; menu pela composição ESCOLHIDA; item sob_encomenda ENTRA aqui
  — saiu da indústria — embora nunca tenha baixado loja) e debita
  `EstoqueProducao` (receita/produto) e `MateriaPrima.estoque_atual` (MP),
  espelhando a semântica de `pedido_estoque.baixar_industria_pedido`:
  falta NUNCA vira saldo negativo, fica anotada.

Idempotência POR PEDIDO em AppConfig (`acerto_despacho_<data>` = JSON de
códigos já acertados): rodar de novo só pega pedidos novos. A fase 1 do
`estornar_venda` (inteiros) exige chamada única por referência — é o
marcador que garante.

Dry-run por default (`executar=False`): monta o plano inteiro sem escrever.
NUNCA rodar antes do despacho físico — o gesto é do dono, depois do evento.
"""
import json
import logging

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    EstoqueProducao,
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
_TOL = 1e-6


def _chave_marker(data):
    return f'acerto_despacho_{data.isoformat()}'


def _codigos_acertados(data):
    try:
        v = json.loads(AppConfig.get(_chave_marker(data)) or '[]')
        return set(v) if isinstance(v, list) else set()
    except (TypeError, ValueError):
        return set()


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
        for col, cid, nome, qpu in comp:
            out.append((col, cid, nome, float(qtd) * float(qpu or 0)))
    return out


def _previa_credito_loja(pedido):
    """O que o estorno do pedido devolveria HOJE (inteiros por item, pela
    referência da versão ATUAL da baixa). Read-only — espelha a fase 1 do
    `estornar_venda`; frações ficam de fora da prévia (pequenas)."""
    from app.services.loja_pagamento import _ref_estoque, _versao_estoque_atual
    ref, _pref = _ref_estoque(pedido.codigo, _versao_estoque_atual(pedido))
    rows = (db.session.query(MovEstoqueLoja, EstoqueLoja)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(MovEstoqueLoja.tipo == 'venda_site',
                    db.or_(MovEstoqueLoja.referencia == ref,
                           MovEstoqueLoja.referencia.like(ref + ' %')))
            .all())
    por_item = {}
    for mov, el in rows:
        nome = el.nome_item
        por_item[nome] = por_item.get(nome, 0) + int(mov.quantidade or 0)
    return por_item


def acertar(data, executar=False, usuario_id=None):
    """Monta (e, com `executar=True`, aplica) o acerto do dia. Retorna dict
    com o plano completo — o mesmo nos dois modos, pra o dry-run ser fiel."""
    ja = _codigos_acertados(data)
    pedidos = _pedidos_do_dia(data)
    novos = [p for p in pedidos if p.codigo not in ja]

    # ── Plano: crédito na loja (por pedido) + débito agregado na indústria ──
    credito_loja = {}          # nome_item -> un (prévia dos inteiros)
    debito = {}                # (col, id) -> {'nome', 'qtd'}
    for p in novos:
        for nome, q in _previa_credito_loja(p).items():
            credito_loja[nome] = credito_loja.get(nome, 0) + q
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
        'ja_acertados': sorted(ja & {p.codigo for p in pedidos}),
        'credito_loja': dict(sorted(credito_loja.items())),
        'credito_loja_total_un': sum(credito_loja.values()),
        'debito_industria': plano_debito,
        'avisos': avisos,
    }
    if not executar or not novos:
        return plano

    # ── Executar: transação única; qualquer erro desfaz tudo ──────────────
    from app.services.loja_pagamento import _estornar_estoque
    try:
        creditado_total = 0
        for p in novos:
            creditado_total += _estornar_estoque(p)

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
                ref_mp = ref + (' — faltaram %g' % (item['qtd'] - baixa)
                                if item['qtd'] > baixa else '')
                db.session.add(MovimentacaoEstoque(
                    materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
                    referencia=ref_mp, usuario_id=usuario_id))
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
            if item['qtd'] > baixa:
                # Falta NUNCA vira saldo negativo — fica anotada no retorno
                # (mesma regra do baixar_industria_pedido).
                avisos.append('%s: indústria tinha %d de %d — faltaram %d'
                              % (item['nome'], disp, item['qtd'],
                                 int(item['qtd']) - baixa))

        AppConfig.set(_chave_marker(data),
                      json.dumps(sorted(ja | {p.codigo for p in novos})))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('acerto_despacho falhou — transação desfeita')
        raise

    plano['executado'] = True
    plano['credito_aplicado_un'] = creditado_total
    plano['avisos'] = avisos
    return plano
