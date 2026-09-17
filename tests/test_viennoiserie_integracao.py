"""Massa única de 25kg, distribuição, ordem e estoque em unidades compatíveis."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    CronogramaOverride,
    EstoqueProducao,
    MateriaPrima,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.cronograma_viennoiserie import planejar_viennoiserie
from app.services.estoque_massa import saldo_bolas
from app.services.previsao_producao import _explodir_bom, balanco_industria
from app.services.producao import enviar_plano_do_dia, produzir_item_plano
from app.services.viennoiserie import padrao_massa
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda(congela_hoje):
    congela_hoje()


def cenario():
    massa = Receita(nome='Massa para folhar', categoria='Viennoiserie',
        peso_base=2000, peso_unitario=3580, rendimento_qtd=1, rendimento_unidade='bola',
        dias_producao=1, sugerir_pedido_loja=False, estoque_nao_abate=True,
        capacidade_amassadeira_g=112000)
    db.session.add(massa)
    db.session.flush()
    for nome, pct in [('FarinhaT45', 100), ('Água', 79.6)]:
        db.session.add(MateriaPrima(nome=nome, unidade='g', custo_por_kg=1, estoque_atual=200000))
        massa.ingredientes.append(ReceitaIngrediente(
            tipo='mp', ingrediente_nome=nome, porcentagem=pct))

    def produto(nome, pai, consumo, rendimento, *, vendavel=True):
        r = Receita(nome=nome, categoria='Viennoiserie', peso_base=1000,
            rendimento_qtd=rendimento, rendimento_unidade='un', peso_unitario=100,
            sugerir_pedido_loja=vendavel)
        r.ingredientes.append(ReceitaIngrediente(
            tipo='receita', ingrediente_nome=pai.nome, sub_receita=pai, porcentagem=consumo))
        db.session.add(r)
        db.session.flush()
        return r
    croissant = produto('Croissant Tradicional', massa, 1, 40)
    pain = produto('Pain au Chocolat', massa, 1, 45)
    danish_massa = produto('Massa de Danish', massa, 1, 45, vendavel=False)
    danish = produto('Danish de Maçã', danish_massa, 1, 1)
    db.session.commit()
    return massa, croissant, pain, danish_massa, danish


def linha(r, qs):
    return {'receita_id': r.id, 'nome': r.nome, 'em_estoque': 0, 'total': sum(qs),
        'por_dia': [{'data': (hoje() + timedelta(days=i)).isoformat(),
                    'qtd': q, 'fornadas': None} for i, q in enumerate(qs)]}


def calcular(rs, linhas, bal=None):
    dias = [hoje() + timedelta(days=i) for i in range(len(linhas[0]['por_dia']))]
    recs = {r.id: r for r in rs}
    planejar_viennoiserie(linhas, dias, recs, {r.id: r.dias_producao or 0 for r in rs},
                          bal or {'itens': []}, explodir=_explodir_bom)
    return {r['receita_id']: r for r in linhas}


def test_lote_compartilhado_prioriza_maior_e_nao_25kg_por_produto(app):
    rs = cenario()
    massa, croissant, pain, dm, danish = rs
    out = calcular(rs, [linha(croissant, [0, 120, 0]),
                        linha(pain, [0, 45, 0]), linha(danish, [0, 20, 0])])
    c = out[massa.id]['por_dia'][0]
    assert c['qtd'] == 1
    assert c['batelada_padrao']['farinha_g'] == 25000
    assert out[croissant.id]['por_dia'][1]['reforco_viennoiserie'] > out[pain.id]['por_dia'][1].get('reforco_viennoiserie', 0)
    assert sum(d['massa_g'] for d in c['massa_viennoiserie']['distribuicao']) <= 44900
    assert c['massa_viennoiserie']['residuo_g'] < 100


def test_falta_de_demanda_nao_cria_batimento_nem_produtos(app):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 0, 0])])
    assert out[rs[1].id]['total'] == 0
    assert rs[0].id not in out or out[rs[0].id]['total'] == 0


def test_override_zero_nao_recebe_excedente(app):
    rs = cenario()
    db.session.add(CronogramaOverride(receita_id=rs[2].id,
        data=hoje() + timedelta(days=1), qtd=0))
    db.session.commit()
    out = calcular(rs, [linha(rs[1], [0, 120, 0]), linha(rs[2], [0, 0, 0])])
    assert out[rs[2].id]['por_dia'][1]['qtd'] == 0


def test_excedente_cobre_futuro_sem_produzir_duas_vezes(app):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 50, 50, 50])])
    assert out[rs[1].id]['por_dia'][2]['qtd'] == 0
    assert out[rs[1].id]['por_dia'][3]['qtd'] == 0
    assert out[rs[0].id]['total'] == 1


def test_nao_antecipa_produto_com_limite_zero(app):
    rs = cenario()
    rs[1].antecedencia_max_dias = 0
    db.session.commit()
    out = calcular(rs, [linha(rs[1], [0, 50, 50])])
    assert out[rs[1].id]['por_dia'][2]['qtd'] >= 50


def test_ordem_antiga_em_bolas_nao_vira_batimentos(app, admin_user):
    rs = cenario()
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma', enviado_ao_padeiro=True)
    plano.itens.append(PlanejamentoItem(receita=rs[0], qtd_alvo=5, produzido_qtd=0))
    db.session.add(plano)
    db.session.commit()
    out = calcular(rs, [linha(rs[1], [0, 50, 0])])
    c = out[rs[0].id]['por_dia'][0]
    assert c['qtd'] == 5 and c['massa_legada']
    assert 'batelada_padrao' not in c
    assert out[rs[1].id]['por_dia'][1]['qtd'] == 50


def test_envio_reserva_25kg_e_credita_massa_exata_uma_vez(app, admin_user):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 50, 0])])
    crono = {'receitas': list(out.values())}
    plano = enviar_plano_do_dia(hoje(), admin_user.id, crono=crono)
    item = next(it for it in plano.itens if it.receita_id == rs[0].id)
    assert item.qtd_alvo == 1
    assert item.batelada_padrao.dados['unidade_producao'] == 'batimentos'
    farinha = MateriaPrima.query.filter_by(nome='FarinhaT45').one()
    assert farinha.estoque_atual == 175000
    assert produzir_item_plano(item.id, 1, admin_user.id)['ok']
    assert (saldo_bolas(rs[0]) * Decimal(3580)).quantize(Decimal('.000001')) == Decimal(44900)
    assert farinha.estoque_atual == 175000
    assert not produzir_item_plano(item.id, 1, admin_user.id)['ok']
    assert (saldo_bolas(rs[0]) * Decimal(3580)).quantize(Decimal('.000001')) == Decimal(44900)


def test_wip_batimento_nao_confunde_com_uma_bola(app, admin_user):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 50, 0])])
    enviar_plano_do_dia(hoje(), admin_user.id, crono={'receitas': list(out.values())})
    bal = balanco_industria(horizonte_dias=3, inicio_offset_dias=1, usar_cache=False)
    massa = next(r for r in bal['itens'] if r['receita_id'] == rs[0].id)
    assert massa['em_producao'] == pytest.approx(44900 / 3580)


def test_get_planejamento_nao_cria_ordem_ou_snapshot(app):
    rs = cenario()
    linhas = [linha(rs[1], [0, 50, 0])]
    esperado = calcular(rs, deepcopy(linhas))
    assert calcular(rs, deepcopy(linhas)) == esperado
    assert PlanejamentoProducao.query.count() == 0
    assert EstoqueProducao.query.count() == 0
    assert padrao_massa(rs[0])['massa_g'] == pytest.approx(44900)


def test_enviar_so_massa_preserva_destino_do_lote_nas_proximas_leituras(app, admin_user):
    rs = cenario()
    linhas = [linha(rs[1], [0, 50, 50, 50])]
    out = calcular(rs, deepcopy(linhas))
    quantidade = out[rs[1].id]['por_dia'][1]['qtd']
    enviar_plano_do_dia(hoje(), admin_user.id, crono={'receitas': list(out.values())})
    repetido = calcular(rs, deepcopy(linhas))
    assert repetido[rs[1].id]['por_dia'][1]['qtd'] == quantidade
    assert repetido[rs[1].id]['por_dia'][2]['qtd'] == 0
    assert repetido[rs[1].id]['por_dia'][3]['qtd'] == 0


def test_motor_com_pedidos_reais_expoe_batch_e_projecao_compativel(app):
    from app.models import Loja, PedidoItem, PedidoLoja
    from app.services.previsao_producao import cronograma_producao
    rs = cenario()
    loja = Loja(nome='Loja Teste', ativa=True)
    pedido = PedidoLoja(loja=loja, status='pendente', data_pedido=hoje(),
                        data_entrega=hoje() + timedelta(days=1))
    pedido.itens.append(PedidoItem(receita=rs[1], quantidade=50))
    db.session.add(pedido)
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=3)
    massa = next(r for r in crono['receitas'] if r['receita_id'] == rs[0].id)
    assert massa['por_dia'][0]['qtd'] == 1
    assert massa['projecao'][1]['producao'] == pytest.approx(44900 / 3580)
    assert massa['projecao'][1]['saldo'] >= 0
    assert massa['projecao'][1]['saida'] > 12


def test_retry_da_mesma_tela_nao_confirma_segundo_batimento(app, admin_user):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 600, 0])])
    plano = enviar_plano_do_dia(hoje(), admin_user.id, crono={'receitas': list(out.values())})
    it = next(it for it in plano.itens if it.receita_id == rs[0].id)
    assert it.qtd_alvo == 2
    assert produzir_item_plano(it.id, 1, admin_user.id, produzido_esperado=0)['ok']
    assert not produzir_item_plano(it.id, 1, admin_user.id, produzido_esperado=0)['ok']
    assert it.produzido_qtd == 1


def test_parciais_de_montagem_preservam_consumo_exato(app, admin_user):
    from app.services.estoque_massa import registrar_entrada_massa
    rs = cenario()
    registrar_entrada_massa(rs[0], 44900, admin_user.id, 'Teste')
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma', enviado_ao_padeiro=True)
    it = PlanejamentoItem(receita=rs[2], qtd_alvo=45, produzido_qtd=0)
    plano.itens.append(it)
    db.session.add(plano)
    db.session.commit()
    for n in range(45):
        assert produzir_item_plano(it.id, 1, admin_user.id, produzido_esperado=n)['ok']
    assert (saldo_bolas(rs[0]) * 3580).quantize(Decimal('.000001')) == Decimal(44900 - 3580)


def test_padeiro_e_industria_exibem_destinos_finais(app, admin_user):
    client = app.test_client()
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 50, 0]), linha(rs[4], [0, 20, 0])])
    plano = enviar_plano_do_dia(hoje(), admin_user.id, crono={'receitas': list(out.values())})
    it = next(it for it in plano.itens if it.receita_id == rs[0].id)
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    data = client.get(f'/padeiro/receita/{rs[0].id}.json?item_id={it.id}').get_json()
    assert data['farinha_g'] == 25000
    assert data['unidade_producao'] == 'batimentos'
    assert any(p['nome'] == 'Danish de Maçã' for p in data['produtos'])
    html = client.get('/padeiro/').get_data(as_text=True)
    assert 'produzido_esperado' in html
    assert client.get('/telaindustriateste/?view=week').status_code == 200


def test_destino_do_batimento_de_ontem_permanece_hoje(app, admin_user, congela_hoje):
    rs = cenario()
    out = calcular(rs, [linha(rs[1], [0, 50, 50])])
    alvo = out[rs[1].id]['por_dia'][1]['qtd']
    enviar_plano_do_dia(hoje(), admin_user.id, crono={'receitas': list(out.values())})
    congela_hoje(dia=18)
    novo = calcular(rs, [linha(rs[1], [50, 50])])
    assert novo[rs[1].id]['por_dia'][0]['qtd'] == alvo
    assert novo[rs[1].id]['por_dia'][1]['qtd'] == 0
