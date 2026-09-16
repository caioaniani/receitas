"""Uma ficha congelada é a fonte da pesagem, alvo e consumo de pão."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import (
    ConsumoSubFracao,
    EstoqueProducao,
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
)
from app.models.producao_batelada import PlanejamentoItemBatelada
from app.services.bateladas_paes import (
    componentes_item,
    consumir_item,
    farinha_padrao_g,
    mise_item,
    normalizar_item,
    padrao_receita,
    resumo_item,
)


def _receita(nome='Sourdough tradicional', *, farinha=100, agua=70, peso=500,
             peso_base=1000, cadastrar_mp=True, **kwargs):
    rec = Receita(nome=nome, categoria='Pães', peso_base=peso_base,
                  peso_unitario=peso, rendimento_qtd=4, rendimento_unidade='un',
                  capacidade_amassadeira_g=50000, **kwargs)
    db.session.add(rec)
    db.session.flush()
    for nome_ing, pct in [('Farinha branca', farinha), ('Água', agua), ('Sal', 2)]:
        rec.ingredientes.append(ReceitaIngrediente(
            tipo='mp', ingrediente_nome=nome_ing, porcentagem=pct))
        if cadastrar_mp and not MateriaPrima.query.filter_by(nome=nome_ing).first():
            db.session.add(MateriaPrima(nome=nome_ing, unidade='g',
                                        custo_por_kg=1, estoque_atual=100000))
    db.session.flush()
    return rec


def _item(rec, alvo=1, produzido=0):
    plano = PlanejamentoProducao(data=date(2026, 9, 16), nome='Teste bateladas',
                                 enviado_ao_padeiro=False)
    it = PlanejamentoItem(receita=rec, qtd_alvo=alvo, produzido_qtd=produzido,
                          planejamento=plano)
    db.session.add(plano)
    db.session.flush()
    return it


def _sub(rec, qtd=0.17, tipo='receita', peso=1000):
    sub = Receita(nome='Levain (pé)', categoria='Insumos', peso_base=1000,
                  rendimento_qtd=1, rendimento_unidade='un', peso_unitario=peso,
                  sub_na_amassadeira=True)
    db.session.add(sub)
    db.session.flush()
    rec.ingredientes.append(ReceitaIngrediente(
        tipo=tipo, ingrediente_nome='Nome antigo do levain', porcentagem=qtd,
        sub_receita=sub))
    db.session.flush()
    return sub


@pytest.mark.parametrize(('nome', 'familia', 'esperado'), [
    ('Sourdough Tradicional', None, 25000),
    ('Sourdough integral', 'viennoiserie', 25000),
    ('Sourdough 7 Grãos', 'pao_sourdough', 25000),
    ('Pão Francês', None, 12000),
    ('PÃO FRANCÊS SOURDOUGH', 'pao_sourdough', 12000),
    ('Pão Integral', 'pao_sourdough', 25000),
    ('Pão sem classificação', None, None),
    ('Brioche', 'pao_sourdough', None),
    ('Baguete sourdough', 'pao_sourdough', None),
    ('Sourdough Tradicional — Retorno', 'pao_sourdough', None),
    ('Levain', 'pao_sourdough', None),
    ('Granola', 'pao_sourdough', None),
    ('Iogurte', 'pao_sourdough', None),
    ('Massa sourdough', 'pao_sourdough', None),
    ('Mini Sourdough Avocado', 'pao_sourdough', None),
    ('Mini Sourdough com posta de Lagarto', 'pao_sourdough', None),
])
def test_classificacao(nome, familia, esperado):
    assert farinha_padrao_g(SimpleNamespace(nome=nome, familia=familia)) == esperado


def test_retorno_sem_rotulo_tambem_excluido(app):
    original = _receita()
    destino = _receita('Sourdough antigo')
    original.retorno_receita = destino
    db.session.flush()
    assert farinha_padrao_g(destino) is None
    assert farinha_padrao_g(original) == 25000


def test_montagem_sourdough_e_fornada_especial_nao_viram_batelada(app):
    pao = _receita()
    montado = Receita(nome='Sourdough com ovo', peso_base=1000, rendimento_qtd=1,
                      rendimento_unidade='un', familia='pao_sourdough')
    montado.ingredientes.append(ReceitaIngrediente(
        tipo='receita', ingrediente_nome=pao.nome, porcentagem=1, sub_receita=pao))
    db.session.add(montado)
    db.session.flush()
    assert farinha_padrao_g(montado) is None
    assert farinha_padrao_g(SimpleNamespace(
        nome='Focaccia Gorgonzola', categoria='Fornadas especiais',
        familia='pao_sourdough')) is None


@pytest.mark.parametrize(('nome', 'farinha', 'unidades'), [
    ('Sourdough integral', 25000, 86), ('Pão Francês', 12000, 41),
])
def test_padrao_farinha_e_outros_ingredientes_proporcionais(app, nome, farinha, unidades):
    rec = _receita(nome)
    dados = padrao_receita(rec)
    assert dados['farinha_g'] == farinha
    assert dados['escala'] == farinha / 1000
    assert dados['unidades'] == unidades
    quantidades = {ing['nome']: ing['qtd'] for ing in dados['ingredientes']}
    assert quantidades == {'Farinha branca': farinha, 'Água': farinha * .7,
                           'Sal': farinha * .02}


def test_soma_farinha_mix_antes_de_escalar_inclusive_direta(app):
    rec = _receita(farinha=60, agua=70)
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp_direto', ingrediente_nome='Farinha integral', porcentagem=650))
    db.session.add(MateriaPrima(nome='Farinha integral', custo_por_kg=1,
                                estoque_atual=100000))
    dados = padrao_receita(rec)
    assert dados['escala'] == 20  # 25.000 / (600 + 650), não peso_base.
    quantidades = {ing['nome']: ing['qtd'] for ing in dados['ingredientes']}
    assert quantidades['Farinha branca'] == 12000
    assert quantidades['Farinha integral'] == 13000
    assert quantidades['Água'] == 14000


def test_mp_direta_e_unidade_mantem_semantica(app):
    rec = _receita()
    for tipo, nome, qtd in [('mp_direto', 'Sementes', 45), ('mp_un', 'Embalagem', 2)]:
        rec.ingredientes.append(ReceitaIngrediente(
            tipo=tipo, ingrediente_nome=nome, porcentagem=qtd))
        db.session.add(MateriaPrima(nome=nome, custo_por_kg=1, estoque_atual=100000))
    dados = padrao_receita(rec)
    ings = {i['nome']: i for i in dados['ingredientes']}
    assert ings['Sementes']['qtd'] == 1125
    assert ings['Embalagem']['qtd'] == 50
    assert ings['Embalagem']['unidade'] == 'un'
    assert dados['massa_g'] == 43000 + 1125


def test_levain_entra_na_massa_real_e_no_aviso_sem_dividir_batelada(app):
    rec = _receita(agua=85)
    sub = _sub(rec, tipo='sub_pct', qtd=20, peso=1)
    dados = padrao_receita(rec)
    assert dados['farinha_g'] == 25000
    assert dados['massa_g'] == 51750  # 25.000 + 21.250 + 500 + 5.000.
    assert dados['rendimento_teorico'] == 103.5
    assert dados['unidades'] == 103
    assert dados['subs'] == [{'id': sub.id, 'quantidade': 5000}]
    assert dados['ingredientes'][-1]['nome'] == 'Levain (pé)'
    assert dados['ingredientes'][-1]['unidade'] == 'g'
    assert dados['ingredientes'][-1]['qtd'] == 5000
    assert '51.75 kg' in dados['avisos'][0]
    assert 'não foi dividida' in dados['avisos'][0]


def test_ficha_real_tradicional_25kg_nao_arredonda_farinha_para_rendimento(app):
    rec = _receita(agua=80)
    rec.ingredientes[0].ingrediente_nome = 'FarinhaT65'
    rec.ingredientes[0].eh_base = False
    MateriaPrima.query.filter_by(nome='Farinha branca').one().nome = 'FarinhaT65'
    rec.capacidade_amassadeira_g = 112000
    _sub(rec, tipo='receita', qtd=200, peso=1)
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='Fermento', porcentagem=.1))
    db.session.add(MateriaPrima(nome='Fermento', custo_por_kg=1,
                                estoque_atual=100000))
    dados = padrao_receita(rec)
    assert dados['farinha_g'] == 25000
    assert dados['massa_g'] == 50525
    assert dados['rendimento_teorico'] == 101.05
    assert dados['unidades'] == 101
    assert dados['avisos'] == []
    it = _item(rec, alvo=102)
    normalizar_item(it)
    assert it.qtd_alvo == 202
    assert resumo_item(it)['farinha_total_g'] == 50000
    assert componentes_item(it, 101)['subs'] == {rec.ingredientes[3].sub_receita_id: 5000}


def test_ficha_real_sete_graos_com_base_fracionaria_e_duas_subs(app):
    rec = _receita('Sourdough 7 Grãos', agua=85, peso_base=40086)
    _sub(rec, tipo='sub_pct', qtd=20, peso=1)
    graos = _sub(rec, tipo='sub_pct', qtd=10, peso=1)
    graos.nome = '7 grãos (grãos para pão)'
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='Fermento', porcentagem=.1))
    db.session.add(MateriaPrima(nome='Fermento', custo_por_kg=1, estoque_atual=100000))
    dados = padrao_receita(rec)
    assert dados['escala'] == pytest.approx(25000 / 40086)
    assert dados['massa_g'] == 54275
    assert dados['rendimento_teorico'] == 108.55
    assert dados['unidades'] == 108
    assert dados['subs'][0]['quantidade'] == 5000
    assert dados['subs'][1]['quantidade'] == 2500


def test_ficha_real_frances_12kg_rende_237_unidades(app):
    rec = _receita('Pão Francês Fermentado', agua=75, peso=100)
    _sub(rec, tipo='receita', qtd=200, peso=1)
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome='Fermento', porcentagem=.5))
    db.session.add(MateriaPrima(nome='Fermento', custo_por_kg=1, estoque_atual=100000))
    dados = padrao_receita(rec)
    assert dados['farinha_g'] == 12000
    assert dados['massa_g'] == 23700
    assert dados['unidades'] == 237


def test_agua_marcada_base_nao_entra_na_farinha_nomeada(app):
    rec = _receita()
    rec.ingredientes[1].eh_base = True
    dados = padrao_receita(rec)
    assert dados['escala'] == 25
    assert dados['ingredientes'][0]['qtd'] == 25000


def test_rendimento_cadastrado_sem_peso_nao_arredonda_escala(app):
    rec = _receita(peso=None, peso_base=1800)
    dados = padrao_receita(rec)
    assert dados['escala'] == pytest.approx(25000 / 1800)
    assert dados['rendimento_teorico'] == pytest.approx(4 * 25000 / 1800)
    assert dados['unidades'] == 55
    assert 'Sem peso unitário' in dados['avisos'][0]


def test_previsao_dispensa_mp_mas_snapshot_exige_cadastro(app):
    rec = _receita(cadastrar_mp=False)
    assert padrao_receita(rec, resolver_estoque=False)['farinha_g'] == 25000
    with pytest.raises(ValueError, match='matéria-prima'):
        padrao_receita(rec)
    it = _item(rec)
    with pytest.raises(ValueError):
        normalizar_item(it)
    assert it.batelada_padrao is None


@pytest.mark.parametrize('problema', ['sem_farinha', 'negativo', 'nan', 'sem_base'])
def test_ficha_invalida_nao_cria_batelada(app, problema):
    rec = _receita()
    if problema == 'sem_farinha':
        rec.ingredientes[0].ingrediente_nome = 'Outro ingrediente'
    elif problema == 'negativo':
        rec.ingredientes[0].porcentagem = -1
    elif problema == 'nan':
        rec.ingredientes[0].porcentagem = float('nan')
    else:
        rec.peso_base = 0
    with pytest.raises(ValueError):
        with db.session.no_autoflush:
            padrao_receita(rec)


def test_sub_nao_vinculada_trava_ficha(app):
    rec = _receita()
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='sub_pct', ingrediente_nome='Levain sem cadastro', porcentagem=20))
    with pytest.raises(ValueError, match='Vincule a sub-receita'):
        padrao_receita(rec)


def test_alvo_sobe_para_bateladas_completas_e_sabores_independentes(app):
    trad = _item(_receita(), alvo=87)
    integral = _item(_receita('Sourdough Integral'), alvo=1)
    assert normalizar_item(trad)['bateladas'] == 2
    assert normalizar_item(integral)['bateladas'] == 1
    assert trad.qtd_alvo == 172
    assert integral.qtd_alvo == 86
    assert resumo_item(trad)['farinha_total_g'] == 50000
    assert resumo_item(integral)['farinha_total_g'] == 25000
    trad.qtd_alvo = 1
    normalizar_item(trad)
    assert trad.qtd_alvo == 86


def test_nao_cria_snapshot_em_ordem_legada_com_producao_ou_sem_autorizacao(app):
    rec = _receita()
    legado = _item(rec, produzido=1)
    sem_gesto = _item(rec)
    sem_necessidade = _item(rec, alvo=0)
    assert normalizar_item(legado) is None
    assert normalizar_item(sem_gesto, permitir_novo=False) is None
    assert normalizar_item(sem_necessidade) is None
    assert mise_item(legado) is None
    assert componentes_item(legado, 10) is None
    assert consumir_item(legado, 10, None, 'Legado') is None


def test_snapshot_nao_muda_quando_ficha_nome_mp_e_etapas_mudam(app):
    rec = _receita()
    rec.modo_preparo = 'Preparar\n\nFermentar'
    rec.etapas.append(ReceitaEtapa(nome='Fermentar', ordem=1, duracao_min=90,
                                   equipamento='camara_fria', ativa=False,
                                   descricao='Controlar a temperatura'))
    it = _item(rec)
    normalizar_item(it)
    congelado = deepcopy(it.batelada_padrao.dados)
    rec.nome = 'Nome alterado'
    rec.peso_base = 2000
    rec.ingredientes[0].porcentagem = 40
    rec.etapas[0].duracao_min = 200
    rec.modo_preparo = 'Texto novo'
    MateriaPrima.query.filter_by(nome='Farinha branca').one().nome = 'Farinha renomeada'
    it.qtd_alvo = 87
    normalizar_item(it, permitir_novo=False)
    db.session.commit()
    db.session.expire_all()
    assert it.batelada_padrao.dados == congelado
    mise = mise_item(it)
    assert mise['nome'] == 'Sourdough tradicional'
    assert mise['bateladas'] == 2
    assert mise['farinha_g'] == 25000
    assert mise['ingredientes'][0]['qtd'] == 25000
    assert mise['processo'][0]['duracao'] == '1,5h'
    assert mise['etapas'] == ['Preparar', 'Fermentar']
    mise['ingredientes'][0]['qtd'] = 1
    assert it.batelada_padrao.dados['ingredientes'][0]['qtd'] == 25000


def test_rendimento_fracionado_e_duas_confirmacoes_consumem_exatamente_lote(app, admin_user):
    rec = _receita(agua=70.3)
    sub = _sub(rec)
    it = _item(rec)
    normalizar_item(it)
    dados = resumo_item(it)
    assert dados['rendimento_teorico'] == 94.65
    assert dados['unidades'] == 94
    db.session.add(EstoqueProducao(receita_id=sub.id, quantidade=10))
    db.session.flush()
    farinha = MateriaPrima.query.filter_by(nome='Farinha branca').one()
    for qtd in (30, 64):
        consumir_item(it, qtd, admin_user.id, 'Teste parcial')
        it.produzido_qtd += qtd  # Mesma ordem do chamador produzir_item_plano.
    db.session.flush()
    assert farinha.estoque_atual == pytest.approx(75000)
    movs = MovimentacaoEstoque.query.filter_by(materia_prima_id=farinha.id).all()
    assert sum(m.quantidade for m in movs) == pytest.approx(25000)
    assert EstoqueProducao.query.filter_by(receita_id=sub.id).one().quantidade == 6
    assert ConsumoSubFracao.query.filter_by(receita_id=sub.id).one().fracao_pendente == pytest.approx(.25)
    assert it.produzido_qtd == 94


def test_consumo_usa_id_congelado_e_falha_se_mp_foi_removida(app, admin_user):
    it = _item(_receita())
    normalizar_item(it)
    farinha = MateriaPrima.query.filter_by(nome='Farinha branca').one()
    farinha.nome = 'Farinha com novo nome'
    consumir_item(it, 43, admin_user.id, 'Confirmado')
    db.session.flush()
    assert farinha.estoque_atual == 87500
    sal = MateriaPrima.query.filter_by(nome='Sal').one()
    sal_id = sal.id
    MovimentacaoEstoque.query.filter_by(materia_prima_id=sal_id).delete()
    db.session.delete(sal)
    db.session.flush()
    antes = MovimentacaoEstoque.query.count()
    with pytest.raises(ValueError, match='removido'):
        consumir_item(it, 43, admin_user.id, 'Não confirmar')
    assert MovimentacaoEstoque.query.count() == antes
    assert farinha.estoque_atual == 87500


def test_snapshot_apaga_com_item_sem_alterar_ficha(app):
    it = _item(_receita())
    normalizar_item(it)
    db.session.commit()
    assert PlanejamentoItemBatelada.query.count() == 1
    db.session.delete(it)
    db.session.commit()
    assert PlanejamentoItemBatelada.query.count() == 0
    assert Receita.query.count() == 1
