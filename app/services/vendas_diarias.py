"""Persistencia das vendas do Seru por dia (`VendaSeruDiaria`).

Captura os pedidos da API e grava um snapshot agregado por (data, loja_seru,
seru_nome). O relatorio de itens-vendidos le DAQUI em vez de re-consultar a API
a cada request — com ~600 pedidos/dia a consulta ao vivo estoura em ranges
largos (era o "erro de rede" da tela). NAO substitui o MovEstoqueLoja (baixa de
estoque); e a fonte do relatorio/faturamento por loja.

Idempotente: capturar um intervalo apaga as linhas daquele intervalo e regrava
(um pedido cancelado depois some do snapshot). Dinheiro em `Decimal` (regra do
projeto). `data` = createdAt em BRT.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    SeruLojaMap,
    VendaMapa,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.services import seru
from app.services.vendas_itens import (
    _carregar_catalogo,
    _nome_loja,
    montar_linhas,
)


def _str_chave(v):
    """Normaliza qualquer valor pra string usavel como chave de breakdown
    (dict/lista viram nome legivel) — MESMA logica do endpoint ao vivo
    (`_api_vendas_impl._s`), pra o snapshot bater com a consulta direta."""
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return str(v.get('name') or v.get('label') or v.get('tag')
                   or v.get('code') or v.get('type') or '')
    if isinstance(v, (list, tuple)):
        return ', '.join(_str_chave(x) for x in v if x is not None)
    return str(v)


def _dec(v):
    """Valor monetario da API -> Decimal, tolerante a None/lixo (nunca explode a
    captura por um campo malformado)."""
    try:
        return Decimal(str(v)) if v is not None else Decimal('0')
    except (TypeError, ValueError, ArithmeticError):
        return Decimal('0')


def _marketplace_tag(pedido):
    """Canal de delivery canonico do pedido Seru, ou ``''``.

    A API normalmente manda ``salesChannel.tag`` (ifood/99food/rappi), mas
    algumas contas mandam so o nome. A normalizacao acontece na captura para
    os paineis lerem apenas o snapshot local, sem chamar a API.
    """
    bruto = seru.canal_tag(pedido) or _str_chave(
        pedido.get('salesChannel') if isinstance(pedido, dict) else None)
    compacto = ''.join(ch for ch in bruto.lower() if ch.isalnum())
    if 'ifood' in compacto:
        return 'ifood'
    if compacto == '99' or compacto.startswith('99food'):
        return '99food'
    if 'rappi' in compacto:
        return 'rappi'
    return ''


def _loja_id_por_nome():
    """{company.name(lower): loja_id} dos SeruLojaMap confirmados (pra carimbar
    o vinculo resolvido; leitura do relatorio agrupa por loja_seru de qualquer
    forma, entao loja_id e so um extra util)."""
    out = {}
    for m in SeruLojaMap.query.filter(
            SeruLojaMap.loja_id.isnot(None),
            SeruLojaMap.confirmado_em.isnot(None)).all():
        if m.seru_company_name:
            out[m.seru_company_name.strip().lower()] = m.loja_id
    return out


def capturar_periodo(data_inicial, data_final, expandir_dias_frente=0):
    """Busca os pedidos do Seru e (re)grava o snapshot do intervalo:
    `VendaSeruDiaria` (por produto), `VendaSeruDiaLoja` (totais/loja) e
    `VendaSeruDiaBreakdown` (pagamento/canal/cancelados/sem_itens/desconto —
    eixos da tela 'Vendas PDV' + cockpit da home). Cancelados guardam contagem
    (chave '') E valor (chave 'v'); desconto = soma do `discount` das vendas
    nao canceladas.

    Idempotente: apaga TODAS as linhas de [data_inicial, data_final] nas tres
    tabelas e regrava a partir da API (createdAt BRT no intervalo). Retorna
    {'dias': n, 'linhas': n, 'pedidos': n}."""
    pedidos = seru.listar_pedidos_completo(
        data_inicial, data_final, expandir_dias_frente=expandir_dias_frente)

    # (data, company.name) -> seru_nome -> acumulador (por PRODUTO)
    por_dia = defaultdict(lambda: defaultdict(lambda: {
        'qtd': Decimal('0'), 'fat': Decimal('0'), 'peds': set(), 'sku': None}))
    # (data, company.name) -> totais da LOJA. Somar n_pedidos por produto
    # inflaria (1 pedido, 3 itens = 3x). `fat` = soma dos subtotais dos itens
    # (base do relatorio); `fat_ped` = soma do TOTAL do pedido (inclui kit/box,
    # cujos itens vem com preco 0) — base do faturamento do bot.
    por_dia_loja = defaultdict(lambda: {
        'peds': set(), 'fat': Decimal('0'), 'fat_ped': Decimal('0')})
    # Breakdowns da tela 'Vendas PDV', por (data, company.name). Pagamento usa
    # value|total|amount; canal usa o TOTAL do pedido; marketplace usa CONTAGEM
    # de pedidos (iFood/99/Rappi); cancelados = contagem.
    por_dia_pagto = defaultdict(lambda: defaultdict(lambda: Decimal('0')))
    por_dia_canal = defaultdict(lambda: defaultdict(lambda: Decimal('0')))
    por_dia_marketplace = defaultdict(lambda: defaultdict(int))
    # Cancelados: contagem E valor (total do pedido cancelado) por (data, loja).
    # Desconto: soma do `discount` (R$ do pedido, top-level da API Seru) das
    # vendas NAO canceladas — o "quanto de desconto saiu no dia" do cockpit.
    por_dia_cancel = defaultdict(int)
    por_dia_cancel_v = defaultdict(lambda: Decimal('0'))
    por_dia_desconto = defaultdict(lambda: Decimal('0'))
    # Cobranca SEM itens ("PDV Facil" so-valor — caso Nebraska 17/07/2026,
    # teste de impressora que virou R$7.028,50 de "venda"): valor E contagem
    # por (data, company, TAG DO CANAL). Dono 18/07: canal de DELIVERY
    # (99food — integracao que nao itemiza) e venda REAL e conta no
    # faturamento; avulsa (pdv-facil e afins) fica FORA do resumo e vai pro
    # rodape "investigar".
    por_dia_sem_itens = defaultdict(lambda: {'v': Decimal('0'), 'n': 0})
    dias_vistos = set()
    n_pedidos = 0
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue
        ln = _nome_loja(p) or '(sem loja)'
        dias_vistos.add(d)
        # Helper canonico: cancelado por canceledAt OU status=='canceled'
        # (caso 18/07: cobranca cancelada veio SEM canceledAt e contava
        # como venda no snapshot).
        if seru.pedido_cancelado(p):
            por_dia_cancel[(d, ln)] += 1
            por_dia_cancel_v[(d, ln)] += _dec(p.get('total'))
            continue
        pid = p.get('id') or p.get('orderNumber') or p.get('code')
        n_pedidos += 1
        lj = por_dia_loja[(d, ln)]
        total_ped = _dec(p.get('total'))
        lj['fat_ped'] += total_ped                           # total do pedido
        # `discount` = desconto do pedido em R$ (top-level da API Seru; a lista
        # traz o mesmo objeto do detalhe). So conta em venda nao cancelada.
        desc = _dec(p.get('discount'))
        if desc > 0:
            por_dia_desconto[(d, ln)] += desc
        if pid is not None:
            lj['peds'].add(pid)
        for pay in (p.get('payments') or []):
            if not isinstance(pay, dict):
                continue
            metodo = _str_chave(pay.get('method') or pay.get('type')) or '—'
            por_dia_pagto[(d, ln)][metodo] += _dec(
                pay.get('value') or pay.get('total') or pay.get('amount'))
        canal = _str_chave(p.get('salesChannel')) or '—'
        por_dia_canal[(d, ln)][canal] += total_ped
        marketplace = _marketplace_tag(p)
        if marketplace:
            por_dia_marketplace[(d, ln)][marketplace] += 1
        tem_item = False
        for it in seru.extrair_itens(p):
            if it['cancelado']:
                continue
            tem_item = True
            tot = Decimal(str(it['total']))
            e = por_dia[(d, ln)][it['nome']]
            e['qtd'] += Decimal(str(it['qtd']))
            e['fat'] += tot
            if not e['sku']:
                e['sku'] = it['sku']
            if pid is not None:
                e['peds'].add(pid)
            lj['fat'] += tot                                 # subtotais dos itens
        if not tem_item and total_ped > 0:
            tag = seru.canal_tag(p) or 'outro'
            por_dia_sem_itens[(d, ln, tag)]['v'] += total_ped
            por_dia_sem_itens[(d, ln, tag)]['n'] += 1

    loja_ids = _loja_id_por_nome()
    # Apaga o intervalo inteiro (nao so os dias com pedido): um dia que ficou
    # sem venda — ex: tudo cancelado — tem que zerar tambem.
    for _modelo in (VendaSeruDiaria, VendaSeruDiaLoja, VendaSeruDiaBreakdown):
        _modelo.query.filter(
            _modelo.data >= data_inicial,
            _modelo.data <= data_final).delete(synchronize_session=False)
    db.session.flush()

    linhas = 0
    for (d, ln), itens in por_dia.items():
        lid = loja_ids.get(ln.strip().lower())
        for nome, e in itens.items():
            db.session.add(VendaSeruDiaria(
                data=d, loja_seru=ln, loja_id=lid, seru_nome=nome,
                sku=e['sku'], qtd=e['qtd'], faturamento=e['fat'],
                n_pedidos=len(e['peds'])))
            linhas += 1
    for (d, ln), lj in por_dia_loja.items():
        db.session.add(VendaSeruDiaLoja(
            data=d, loja_seru=ln, loja_id=loja_ids.get(ln.strip().lower()),
            n_pedidos=len(lj['peds']), faturamento=lj['fat'],
            faturamento_pedidos=lj['fat_ped']))
    for (d, ln), metodos in por_dia_pagto.items():
        for metodo, val in metodos.items():
            db.session.add(VendaSeruDiaBreakdown(
                data=d, loja_seru=ln, dimensao='pagamento',
                chave=metodo[:120], valor=val))
    for (d, ln), canais in por_dia_canal.items():
        for canal, val in canais.items():
            db.session.add(VendaSeruDiaBreakdown(
                data=d, loja_seru=ln, dimensao='canal',
                chave=canal[:120], valor=val))
    for (d, ln), marketplaces in por_dia_marketplace.items():
        for marketplace, quantidade in marketplaces.items():
            db.session.add(VendaSeruDiaBreakdown(
                data=d, loja_seru=ln, dimensao='marketplace',
                chave=marketplace, valor=Decimal(quantidade)))
    for (d, ln, tag), si in por_dia_sem_itens.items():
        # dimensao nova (18/07/2026): cobrancas SEM itens do dia, POR CANAL
        # — chave '<tag>' = VALOR e '<tag>:n' = CONTAGEM. So grava quando
        # houver (linha ausente = zero). Leitura tolera o formato antigo
        # (chave ''/'n' agregado, gravado por poucas horas em 18/07).
        db.session.add(VendaSeruDiaBreakdown(
            data=d, loja_seru=ln, dimensao='sem_itens', chave=tag[:118],
            valor=si['v']))
        db.session.add(VendaSeruDiaBreakdown(
            data=d, loja_seru=ln, dimensao='sem_itens',
            chave=f'{tag[:116]}:n', valor=Decimal(si['n'])))
    for (d, ln), qtd in por_dia_cancel.items():
        # chave '' = CONTAGEM (compat: leitura antiga lê '' como contagem);
        # chave 'v' = VALOR (total dos pedidos cancelados). Dia capturado antes
        # da chave 'v' existir tem só a contagem — valor 0 até recapturar.
        db.session.add(VendaSeruDiaBreakdown(
            data=d, loja_seru=ln, dimensao='cancelados',
            chave='', valor=Decimal(qtd)))
        db.session.add(VendaSeruDiaBreakdown(
            data=d, loja_seru=ln, dimensao='cancelados',
            chave='v', valor=por_dia_cancel_v[(d, ln)]))
    for (d, ln), val in por_dia_desconto.items():
        db.session.add(VendaSeruDiaBreakdown(
            data=d, loja_seru=ln, dimensao='desconto',
            chave='', valor=val))
    db.session.commit()
    return {'dias': len(dias_vistos), 'linhas': linhas, 'pedidos': n_pedidos}


def dias_capturados(data_inicial, data_final):
    """Conjunto de datas que JA tem snapshot no intervalo (pra saber o que
    falta capturar)."""
    rows = (db.session.query(VendaSeruDiaria.data)
            .filter(VendaSeruDiaria.data >= data_inicial,
                    VendaSeruDiaria.data <= data_final)
            .distinct().all())
    return {r[0] for r in rows}


def agregar_por_loja_do_banco(data_inicial, data_final):
    """Le `VendaSeruDiaria` e devolve a MESMA forma de
    `vendas_itens.agregar_itens_por_loja` (lojas + consolidado), aplicando o
    estado do mapeamento (VendaMapa) e o match local no momento da leitura —
    SEM tocar na API."""
    receitas, produtos = _carregar_catalogo()
    rows = VendaSeruDiaria.query.filter(
        VendaSeruDiaria.data >= data_inicial,
        VendaSeruDiaria.data <= data_final).all()

    # loja -> nome -> acumulador ; e consolidado -> nome -> acumulador
    por_loja = defaultdict(lambda: defaultdict(
        lambda: {'qtd': 0.0, 'faturamento': 0.0, 'n_pedidos': 0, 'sku': None}))
    cons = defaultdict(
        lambda: {'qtd': 0.0, 'faturamento': 0.0, 'n_pedidos': 0, 'sku': None})
    lojas_vistas = set()
    nomes = set()
    for r in rows:
        ln = r.loja_seru
        lojas_vistas.add(ln)
        nomes.add(r.seru_nome)
        q = float(r.qtd or 0)
        f = float(r.faturamento or 0)
        for alvo in (por_loja[ln][r.seru_nome], cons[r.seru_nome]):
            alvo['qtd'] += q
            alvo['faturamento'] += f
            alvo['n_pedidos'] += int(r.n_pedidos or 0)
            if not alvo['sku']:
                alvo['sku'] = r.sku

    maps = {}
    if nomes:
        maps = {m.nome_externo: m for m in VendaMapa.query.filter(
            VendaMapa.canal == 'seru',
            VendaMapa.nome_externo.in_(list(nomes))).all()}

    # Pedidos DISTINTOS por loja (VendaSeruDiaLoja): cada pedido tem 1 dia + 1
    # loja, entao somar por (dia, loja) da o total EXATO (somar n_pedidos por
    # produto inflaria — 1 pedido de 3 itens contaria 3x).
    ped_loja = defaultdict(int)
    for ln, n in (db.session.query(
            VendaSeruDiaLoja.loja_seru, VendaSeruDiaLoja.n_pedidos)
            .filter(VendaSeruDiaLoja.data >= data_inicial,
                    VendaSeruDiaLoja.data <= data_final).all()):
        ped_loja[ln] += int(n or 0)

    lojas_out = []
    for ln in sorted(por_loja):
        linhas, fat, itens = montar_linhas(por_loja[ln], receitas, produtos, maps)
        lojas_out.append({
            'loja': ln, 'total_pedidos': ped_loja.get(ln, 0),
            'total_itens': itens, 'faturamento': fat, 'produtos': linhas,
        })

    cons_linhas, cons_fat, cons_itens = montar_linhas(cons, receitas, produtos, maps)
    pendentes = sum(1 for p in cons_linhas
                    if p['estado_map'] in ('pendente', 'sem_map'))

    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_pedidos': sum(ped_loja.values()),
        'total_itens_vendidos': cons_itens,
        'faturamento_total': cons_fat,
        'pendentes_count': pendentes,
        'lojas': lojas_out,
        'consolidado': cons_linhas,
        'lojas_no_intervalo': sorted(set(lojas_vistas) | set(ped_loja)),
        'fonte': 'banco',
    }


def garantir_capturado(data_inicial, data_final):
    """Garante snapshot do intervalo: captura da API os dias que faltam + SEMPRE
    hoje (vendas de hoje crescem). Best-effort — se a API falhar, segue com o que
    ja esta no banco. Passado ja capturado nao rebate na API."""
    from app.utils import hoje as _hoje
    hoje = _hoje()
    ja = dias_capturados(data_inicial, data_final)
    precisa = []
    d = data_inicial
    while d <= data_final:
        if d not in ja or d == hoje:
            precisa.append(d)
        d += timedelta(days=1)
    if not precisa:
        return
    cap_ini, cap_fim = min(precisa), max(precisa)
    dias_extra = min(max(0, (hoje - cap_fim).days) if cap_fim < hoje else 0, 7)
    try:
        capturar_periodo(cap_ini, cap_fim, expandir_dias_frente=dias_extra)
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            'garantir_capturado: captura falhou; usa snapshot existente')


def agregar_flat(data_inicial, data_final, loja_seru=None, capturar=True):
    """Relatorio FLAT (consolidado por produto) lido do banco — mesma forma de
    `vendas_itens.agregar_itens`. Filtro opcional por loja_seru (company.name).
    capturar=True garante os dias faltantes + hoje antes de ler."""
    if capturar:
        garantir_capturado(data_inicial, data_final)
    receitas, produtos = _carregar_catalogo()
    q = VendaSeruDiaria.query.filter(
        VendaSeruDiaria.data >= data_inicial,
        VendaSeruDiaria.data <= data_final)
    if loja_seru:
        q = q.filter(VendaSeruDiaria.loja_seru == loja_seru)
    rows = q.all()

    agg = defaultdict(lambda: {'qtd': 0.0, 'faturamento': 0.0,
                               'n_pedidos': 0, 'sku': None})
    lojas_vistas = set()
    nomes = set()
    for r in rows:
        lojas_vistas.add(r.loja_seru)
        nomes.add(r.seru_nome)
        e = agg[r.seru_nome]
        e['qtd'] += float(r.qtd or 0)
        e['faturamento'] += float(r.faturamento or 0)
        if not e['sku']:
            e['sku'] = r.sku
    # Todas as lojas do intervalo (pro dropdown), independente do filtro.
    for (ln,) in (db.session.query(VendaSeruDiaria.loja_seru)
                  .filter(VendaSeruDiaria.data >= data_inicial,
                          VendaSeruDiaria.data <= data_final).distinct().all()):
        lojas_vistas.add(ln)

    maps = {}
    if nomes:
        maps = {m.nome_externo: m for m in VendaMapa.query.filter(
            VendaMapa.canal == 'seru',
            VendaMapa.nome_externo.in_(list(nomes))).all()}
    linhas, fat, itens = montar_linhas(agg, receitas, produtos, maps)

    ql = db.session.query(VendaSeruDiaLoja.n_pedidos).filter(
        VendaSeruDiaLoja.data >= data_inicial,
        VendaSeruDiaLoja.data <= data_final)
    if loja_seru:
        ql = ql.filter(VendaSeruDiaLoja.loja_seru == loja_seru)
    total_pedidos = sum(int(n or 0) for (n,) in ql.all())

    sem_match = sum(1 for p in linhas if not p['match'])
    pendentes = sum(1 for p in linhas if p['estado_map'] in ('pendente', 'sem_map'))
    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'loja': loja_seru,
        'total_pedidos': total_pedidos,
        'total_itens_vendidos': itens,
        'faturamento_total': fat,
        'produtos': linhas,
        'sem_match_count': sem_match,
        'pendentes_count': pendentes,
        'lojas_no_intervalo': sorted(lojas_vistas),
        'fonte': 'banco',
    }


def faturamento_por_loja(data_inicial, data_final, capturar=True):
    """Faturamento PDV (Seru) por loja no intervalo, lido do banco. Usa o TOTAL
    do pedido (`faturamento_pedidos`, inclui kit/box) — mesma base do endpoint de
    faturamento; NAO o subtotal de itens (que subconta kit/box). Retorna
    (total, {loja_seru: faturamento}, n_pedidos)."""
    if capturar:
        garantir_capturado(data_inicial, data_final)
    por_loja = defaultdict(float)
    n_ped = 0
    for ln, f, n in (db.session.query(
            VendaSeruDiaLoja.loja_seru, VendaSeruDiaLoja.faturamento_pedidos,
            VendaSeruDiaLoja.n_pedidos)
            .filter(VendaSeruDiaLoja.data >= data_inicial,
                    VendaSeruDiaLoja.data <= data_final).all()):
        por_loja[ln] += float(f or 0)
        n_ped += int(n or 0)
    total = round(sum(por_loja.values()), 2)
    return total, {k: round(v, 2) for k, v in por_loja.items()}, n_ped


def cancelamentos_descontos_do_banco(data_inicial, data_final):
    """Le do snapshot (`VendaSeruDiaBreakdown`) os CANCELAMENTOS (contagem +
    valor) e o total de DESCONTOS do periodo, SEM tocar na API — fonte do
    painel Vendas da home do admin (cancelamentos/descontos do dia).

    Cancelados: dimensao 'cancelados', chave '' = contagem, chave 'v' = valor.
    Descontos: dimensao 'desconto', chave '' = soma do `discount` das vendas
    nao canceladas. Dia capturado ANTES dessas linhas existirem devolve 0 no
    campo faltante (contagem de cancelados sempre existe; valor/desconto ficam
    0 ate o cron recapturar) — nunca quebra, so subconta ate atualizar."""
    cancel_n = 0
    cancel_v = 0.0
    desconto = 0.0
    for dim, chave, val in (db.session.query(
            VendaSeruDiaBreakdown.dimensao, VendaSeruDiaBreakdown.chave,
            VendaSeruDiaBreakdown.valor)
            .filter(VendaSeruDiaBreakdown.data >= data_inicial,
                    VendaSeruDiaBreakdown.data <= data_final,
                    VendaSeruDiaBreakdown.dimensao.in_(
                        ('cancelados', 'desconto'))).all()):
        v = float(val or 0)
        if dim == 'cancelados':
            if chave == 'v':
                cancel_v += v
            else:
                cancel_n += int(v)
        else:  # desconto
            desconto += v
    return {'cancelados_n': cancel_n,
            'cancelados_valor': round(cancel_v, 2),
            'desconto': round(desconto, 2)}


def vendas_pdv_do_banco(data_inicial, data_final, capturar=True):
    """Agrega a tela 'Vendas PDV' (faturamento + pagamento + canal + loja +
    cancelados) lendo do banco (`VendaSeruDiaLoja` + `VendaSeruDiaBreakdown`),
    SEM tocar na API. Devolve os totais globais e um `por_loja_detalhe` pra o
    filtro por loja da tela funcionar sem os pedidos crus.

    O faturamento usa o TOTAL do pedido (`faturamento_pedidos`, inclui kit/box) —
    mesma base do endpoint ao vivo. `n_pedidos` conta pedidos DISTINTOS nao
    cancelados; `cancelados` e a contagem por loja."""
    if capturar:
        garantir_capturado(data_inicial, data_final)

    # loja -> acumulador (total do pedido, pedidos distintos, breakdowns).
    det = defaultdict(lambda: {
        'total': 0.0, 'n_pedidos': 0, 'cancelados': 0, 'cancelados_valor': 0.0,
        'desconto': 0.0,
        'sem_itens': 0.0, 'sem_itens_n': 0,
        'delivery_sem_itens': 0.0, 'delivery_sem_itens_n': 0,
        'por_pagamento': defaultdict(float), 'por_canal': defaultdict(float)})

    for ln, fat_ped, n in (db.session.query(
            VendaSeruDiaLoja.loja_seru, VendaSeruDiaLoja.faturamento_pedidos,
            VendaSeruDiaLoja.n_pedidos)
            .filter(VendaSeruDiaLoja.data >= data_inicial,
                    VendaSeruDiaLoja.data <= data_final).all()):
        det[ln]['total'] += float(fat_ped or 0)
        det[ln]['n_pedidos'] += int(n or 0)

    for ln, dim, chave, val in (db.session.query(
            VendaSeruDiaBreakdown.loja_seru, VendaSeruDiaBreakdown.dimensao,
            VendaSeruDiaBreakdown.chave, VendaSeruDiaBreakdown.valor)
            .filter(VendaSeruDiaBreakdown.data >= data_inicial,
                    VendaSeruDiaBreakdown.data <= data_final).all()):
        v = float(val or 0)
        if dim == 'pagamento':
            det[ln]['por_pagamento'][chave or '—'] += v
        elif dim == 'canal':
            det[ln]['por_canal'][chave or '—'] += v
        elif dim == 'cancelados':
            # chave '' = contagem; chave 'v' = valor (total cancelado). Sem o
            # split, o valor entraria como contagem (dinheiro virando número
            # de pedidos) — daí a distinção explícita.
            if chave == 'v':
                det[ln]['cancelados_valor'] += v
            else:
                det[ln]['cancelados'] += int(v)
        elif dim == 'desconto':
            det[ln]['desconto'] += v
        elif dim == 'sem_itens':
            # Cobranca so-valor por CANAL (18/07/2026): chave '<tag>' =
            # valor, '<tag>:n' = contagem. DELIVERY (99food etc.) e venda
            # real → bucket informativo, conta no faturamento; o resto
            # (pdv-facil/outro) fica FORA do resumo e no rodape
            # "investigar". Compat: chave ''/'n' (formato agregado das
            # primeiras horas) cai no bucket avulsa. Dia capturado antes
            # da dimensao existir nao tem linhas — total cheio, como era.
            from app.constants import SEM_ITENS_CANAIS_DELIVERY
            eh_n = chave == 'n' or (chave or '').endswith(':n')
            tag = ('' if chave in ('', 'n')
                   else (chave[:-2] if (chave or '').endswith(':n')
                         else chave))
            delivery = tag in SEM_ITENS_CANAIS_DELIVERY
            alvo = 'delivery_sem_itens' if delivery else 'sem_itens'
            if eh_n:
                det[ln][alvo + '_n'] += int(v)
            else:
                det[ln][alvo] += v

    total = 0.0
    n_ped = cancelados = 0
    cancelados_valor = 0.0
    desconto_total = 0.0
    sem_itens_total = 0.0
    sem_itens_n = 0
    delivery_total = 0.0
    delivery_n = 0
    por_pagamento = defaultdict(float)
    por_canal = defaultdict(float)
    por_loja = {}
    por_loja_sem_itens = {}
    por_loja_sem_itens_n = {}
    por_loja_delivery = {}
    por_loja_detalhe = {}
    for ln, d in det.items():
        total += d['total']
        n_ped += d['n_pedidos']
        cancelados += d['cancelados']
        cancelados_valor += d['cancelados_valor']
        desconto_total += d['desconto']
        sem_itens_total += d['sem_itens']
        sem_itens_n += d['sem_itens_n']
        delivery_total += d['delivery_sem_itens']
        delivery_n += d['delivery_sem_itens_n']
        # `por_loja` segue sendo o TOTAL cheio (fat_ped, semantica de
        # sempre); o front subtrai `por_loja_sem_itens` pra exibir a venda
        # COM produto na linha e o resto no rodape (decisao do dono 18/07).
        por_loja[ln] = round(d['total'], 2)
        if d['sem_itens'] > 0:
            por_loja_sem_itens[ln] = round(d['sem_itens'], 2)
        if d['sem_itens_n'] > 0:
            por_loja_sem_itens_n[ln] = d['sem_itens_n']
        if d['delivery_sem_itens'] > 0:
            por_loja_delivery[ln] = round(d['delivery_sem_itens'], 2)
        for k, v in d['por_pagamento'].items():
            por_pagamento[k] += v
        for k, v in d['por_canal'].items():
            por_canal[k] += v
        por_loja_detalhe[ln] = {
            'total': round(d['total'], 2),
            'sem_itens': round(d['sem_itens'], 2),
            'sem_itens_n': d['sem_itens_n'],
            'delivery_sem_itens': round(d['delivery_sem_itens'], 2),
            'n_pedidos': d['n_pedidos'],
            'cancelados': d['cancelados'],
            'cancelados_valor': round(d['cancelados_valor'], 2),
            'desconto': round(d['desconto'], 2),
            'por_pagamento': {k: round(v, 2)
                              for k, v in d['por_pagamento'].items()},
            'por_canal': {k: round(v, 2) for k, v in d['por_canal'].items()},
        }

    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_valor': round(total, 2),
        'sem_itens_total': round(sem_itens_total, 2),
        'sem_itens_n': sem_itens_n,
        'delivery_sem_itens_total': round(delivery_total, 2),
        'delivery_sem_itens_n': delivery_n,
        'n_pedidos': n_ped,
        'cancelados': cancelados,
        'cancelados_valor': round(cancelados_valor, 2),
        'desconto': round(desconto_total, 2),
        'por_pagamento': {k: round(v, 2) for k, v in por_pagamento.items()},
        'por_canal': {k: round(v, 2) for k, v in por_canal.items()},
        'por_loja': por_loja,
        'por_loja_sem_itens': por_loja_sem_itens,
        'por_loja_sem_itens_n': por_loja_sem_itens_n,
        'por_loja_delivery_sem_itens': por_loja_delivery,
        'por_loja_detalhe': por_loja_detalhe,
        'fonte': 'banco',
    }
