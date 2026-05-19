"""Vendas manuais de loja (sem API PDV) + sugestao de pedido.

Cenario: loja sem integracao automatica (ex: Anesio). Admin cola texto
das vendas do dia (igual balanco congelados) → cada item vira uma linha
em `VendaManualLoja`. NAO baixa estoque — eh so historico pra previsao.

Pra sugerir pedido, junta:
- Vendas reais (MovEstoqueLoja tipo IN venda_seru/venda_vnda/etc)
- Vendas manuais (VendaManualLoja) — fundo a fundo

Calcula media diaria nos ultimos N dias + olha estoque atual + sugere
qtd pra `dias_cobertura` dias.
"""
import math
from collections import defaultdict
from datetime import date, timedelta

from app.extensions import db
from app.models import (VendaManualLoja, MovEstoqueLoja, EstoqueLoja, LojaProdutoMap,
                        Loja, Receita, Produto, MateriaPrima)
from app.services import estoque_loja_lote as svc_lote
from app.utils import agora, hoje


VENDAS_REAIS = ('venda_seru', 'venda_seru_sem_estoque',
                'venda_vnda', 'venda_vnda_sem_estoque')


def parsear_lista(texto):
    """Reusa o parser do estoque_loja_lote (mesmo formato 'Nome: qtd')."""
    return svc_lote.parsear_lista(texto)


def resolver_lista(parseados, loja_id):
    """Reusa o resolver (fuzzy match + apelidos globais)."""
    return svc_lote.resolver_lista(parseados, loja_id)


def aplicar_vendas_manuais(itens_resolvidos, loja_id, data_venda, user):
    """Cria VendaManualLoja pra cada item resolvido. NAO mexe em estoque.

    Itens nao resolvidos sao ignorados (devolve em `ignorados`). Pra
    nao perder histórico, o admin pode vincular o apelido depois e
    relancar.

    Retorna {aplicados: [{nome, tipo, quantidade}], ignorados: [...]}.
    """
    if not loja_id or not data_venda:
        return {'aplicados': [], 'ignorados': [{'linha': '*', 'motivo': 'sem_loja_ou_data'}]}

    aplicados = []
    ignorados = []

    for item in itens_resolvidos:
        if item.get('erro'):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': item['erro']})
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            ignorados.append({'linha': item.get('linha', '?'),
                                'nome': item.get('nome'),
                                'motivo': 'nao_resolvido'})
            continue
        try:
            qtd = int(item['quantidade'])
        except (KeyError, TypeError, ValueError):
            ignorados.append({'linha': item.get('linha', '?'), 'motivo': 'qtd_invalida'})
            continue
        if qtd <= 0:
            continue

        # Pendente NAO entra como venda (orfao no estoque, nao vinculado)
        if resolvido['tipo'] == 'pendente':
            ignorados.append({'linha': item.get('linha', '?'),
                                'nome': item.get('nome'),
                                'motivo': 'item_pendente_de_vinculacao'})
            continue

        vm = VendaManualLoja(
            loja_id=loja_id, data_venda=data_venda,
            receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
            produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
            materia_prima_id=resolvido['id'] if resolvido['tipo'] == 'mp' else None,
            quantidade=qtd,
            criado_por_id=getattr(user, 'id', None),
        )
        db.session.add(vm)
        aplicados.append({
            'nome': resolvido['nome'],
            'tipo': resolvido['tipo'],
            'quantidade': qtd,
        })

    if aplicados:
        db.session.commit()
    return {'aplicados': aplicados, 'ignorados': ignorados}


def _chave_item(receita_id=None, produto_id=None, mp_id=None):
    if receita_id:
        return ('receita', receita_id)
    if produto_id:
        return ('produto', produto_id)
    if mp_id:
        return ('mp', mp_id)
    return None


def sugerir_pedido(loja_id, data_inicio=None, data_fim=None,
                    dias_cobertura=7):
    """Calcula sugestao de pedido pra uma loja num intervalo de datas.

    Soma vendas reais (Seru/VNDA via MovEstoqueLoja) + vendas manuais
    (VendaManualLoja) entre data_inicio e data_fim. Calcula media diaria
    e sugere qtd pra `dias_cobertura` dias.

    data_inicio/data_fim sao `date`. Se nao fornecidos, usa ultimos 14 dias.
    Retorna lista [{tipo, id, nome, media_diaria, estoque_atual,
                    qtd_sugerida, vendas_periodo, por_fonte}].
    `por_fonte` = {'vnda': qtd, 'seru': qtd, 'manual': qtd}
    """
    if not loja_id:
        return []
    if data_fim is None:
        data_fim = hoje()
    if data_inicio is None:
        data_inicio = data_fim - timedelta(days=14)
    if data_inicio > data_fim:
        data_inicio, data_fim = data_fim, data_inicio
    dias_periodo = max(1, (data_fim - data_inicio).days + 1)

    # 1. Vendas reais via MovEstoqueLoja
    vendas_por_item = defaultdict(int)  # (tipo, id) → qtd_total
    fontes_por_item = defaultdict(set)
    # MovEstoqueLoja.data eh datetime — filtro pelo range completo (inclui fim)
    from datetime import datetime, time
    dt_inicio = datetime.combine(data_inicio, time.min)
    dt_fim = datetime.combine(data_fim, time.max)
    por_fonte_item = defaultdict(lambda: defaultdict(int))  # {(tipo,id): {fonte: qtd}}
    movs = (db.session.query(MovEstoqueLoja, EstoqueLoja)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(EstoqueLoja.loja_id == loja_id,
                    MovEstoqueLoja.tipo.in_(VENDAS_REAIS),
                    MovEstoqueLoja.data >= dt_inicio,
                    MovEstoqueLoja.data <= dt_fim)
            .all())
    for mov, el in movs:
        chave = _chave_item(el.receita_id, el.produto_id, el.materia_prima_id)
        if not chave:
            continue
        qtd = int(mov.quantidade or 0)
        vendas_por_item[chave] += qtd
        fonte = mov.tipo.split('_')[1]  # seru / vnda
        fontes_por_item[chave].add(fonte)
        por_fonte_item[chave][fonte] += qtd

    # 2. Vendas manuais (por data_venda — Date, nao datetime)
    manuais = VendaManualLoja.query.filter(
        VendaManualLoja.loja_id == loja_id,
        VendaManualLoja.data_venda >= data_inicio,
        VendaManualLoja.data_venda <= data_fim,
    ).all()
    for vm in manuais:
        chave = _chave_item(vm.receita_id, vm.produto_id, vm.materia_prima_id)
        if not chave:
            continue
        qtd = int(vm.quantidade or 0)
        vendas_por_item[chave] += qtd
        fontes_por_item[chave].add('manual')
        por_fonte_item[chave]['manual'] += qtd

    if not vendas_por_item:
        return []

    # 3. Estoque atual da loja por chave
    estoque_por_item = {}
    for el in EstoqueLoja.query.filter_by(loja_id=loja_id).all():
        chave = _chave_item(el.receita_id, el.produto_id, el.materia_prima_id)
        if chave:
            estoque_por_item[chave] = el.quantidade or 0

    # 4. Resolve nomes
    nome_por_chave = {}
    receitas_ids = [k[1] for k in vendas_por_item if k[0] == 'receita']
    produtos_ids = [k[1] for k in vendas_por_item if k[0] == 'produto']
    mps_ids = [k[1] for k in vendas_por_item if k[0] == 'mp']
    if receitas_ids:
        for r in Receita.query.filter(Receita.id.in_(receitas_ids)).all():
            nome_por_chave[('receita', r.id)] = r.nome
    if produtos_ids:
        for p in Produto.query.filter(Produto.id.in_(produtos_ids)).all():
            nome_por_chave[('produto', p.id)] = p.nome
    if mps_ids:
        for m in MateriaPrima.query.filter(MateriaPrima.id.in_(mps_ids)).all():
            nome_por_chave[('mp', m.id)] = m.nome

    # 5. Monta sugestao
    out = []
    for chave, total_vendas in vendas_por_item.items():
        tipo, item_id = chave
        media = total_vendas / dias_periodo
        estoque_atual = estoque_por_item.get(chave, 0)
        ideal = math.ceil(media * dias_cobertura)
        qtd_sugerida = max(0, ideal - estoque_atual)
        out.append({
            'tipo': tipo,
            'id': item_id,
            'nome': nome_por_chave.get(chave, '?'),
            'media_diaria': round(media, 2),
            'vendas_periodo': total_vendas,
            'estoque_atual': estoque_atual,
            'ideal_cobertura': ideal,
            'qtd_sugerida': qtd_sugerida,
            'fontes': sorted(fontes_por_item.get(chave, [])),
            'por_fonte': dict(por_fonte_item.get(chave, {})),
        })
    out.sort(key=lambda x: -x['media_diaria'])
    return out
