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
from decimal import Decimal

from app.extensions import db
from app.models import (
    SeruLojaMap,
    VendaMapa,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.services import seru
from app.services.vendas_itens import (
    _carregar_catalogo,
    _nome_loja,
    montar_linhas,
)


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
    """Busca os pedidos do Seru e (re)grava `VendaSeruDiaria` do intervalo.

    Idempotente: apaga TODAS as linhas de [data_inicial, data_final] e regrava a
    partir da API (createdAt BRT no intervalo, cancelados fora). Retorna
    {'dias': n, 'linhas': n, 'pedidos': n}."""
    pedidos = seru.listar_pedidos_completo(
        data_inicial, data_final, expandir_dias_frente=expandir_dias_frente)

    # (data, company.name) -> seru_nome -> acumulador (por PRODUTO)
    por_dia = defaultdict(lambda: defaultdict(lambda: {
        'qtd': Decimal('0'), 'fat': Decimal('0'), 'peds': set(), 'sku': None}))
    # (data, company.name) -> totais da LOJA (pedidos DISTINTOS + faturamento).
    # Somar n_pedidos por produto inflaria (1 pedido, 3 itens = 3x).
    por_dia_loja = defaultdict(lambda: {'peds': set(), 'fat': Decimal('0')})
    dias_vistos = set()
    n_pedidos = 0
    for p in pedidos:
        if not isinstance(p, dict) or p.get('canceledAt'):
            continue
        d = seru.data_local(p.get('createdAt'))
        if not d or not (data_inicial <= d <= data_final):
            continue
        ln = _nome_loja(p) or '(sem loja)'
        pid = p.get('id') or p.get('orderNumber') or p.get('code')
        dias_vistos.add(d)
        n_pedidos += 1
        for it in seru.extrair_itens(p):
            if it['cancelado']:
                continue
            tot = Decimal(str(it['total']))
            e = por_dia[(d, ln)][it['nome']]
            e['qtd'] += Decimal(str(it['qtd']))
            e['fat'] += tot
            if not e['sku']:
                e['sku'] = it['sku']
            if pid is not None:
                e['peds'].add(pid)
            lj = por_dia_loja[(d, ln)]
            lj['fat'] += tot
            if pid is not None:
                lj['peds'].add(pid)

    loja_ids = _loja_id_por_nome()
    # Apaga o intervalo inteiro (nao so os dias com pedido): um dia que ficou
    # sem venda — ex: tudo cancelado — tem que zerar tambem.
    VendaSeruDiaria.query.filter(
        VendaSeruDiaria.data >= data_inicial,
        VendaSeruDiaria.data <= data_final).delete(synchronize_session=False)
    VendaSeruDiaLoja.query.filter(
        VendaSeruDiaLoja.data >= data_inicial,
        VendaSeruDiaLoja.data <= data_final).delete(synchronize_session=False)
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
            n_pedidos=len(lj['peds']), faturamento=lj['fat']))
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

    lojas_out = []
    for ln in sorted(por_loja):
        linhas, fat, itens = montar_linhas(por_loja[ln], receitas, produtos, maps)
        n_ped = sum(p['n_pedidos'] for p in linhas)
        lojas_out.append({
            'loja': ln, 'total_pedidos': n_ped, 'total_itens': itens,
            'faturamento': fat, 'produtos': linhas,
        })

    cons_linhas, cons_fat, cons_itens = montar_linhas(cons, receitas, produtos, maps)
    pendentes = sum(1 for p in cons_linhas
                    if p['estado_map'] in ('pendente', 'sem_map'))

    return {
        'inicio': data_inicial.isoformat(),
        'fim': data_final.isoformat(),
        'total_pedidos': sum(lo['total_pedidos'] for lo in lojas_out),
        'total_itens_vendidos': cons_itens,
        'faturamento_total': cons_fat,
        'pendentes_count': pendentes,
        'lojas': lojas_out,
        'consolidado': cons_linhas,
        'lojas_no_intervalo': sorted(lojas_vistas),
        'fonte': 'banco',
    }
