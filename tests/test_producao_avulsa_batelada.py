"""Produção real fora da ordem não pode arredondar estoque nem duplicar baixa."""

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    CronogramaOverride,
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueProducao,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaIngrediente,
)
from app.utils import hoje


@pytest.fixture
def frances(app):
    receita = Receita(nome='Pão Francês Fermentado', categoria='Pães',
                      peso_base=1000, peso_unitario=100,
                      rendimento_qtd=20, rendimento_unidade='un')
    db.session.add(receita)
    for nome, pct in [('Farinha', 100), ('Água', 75), ('Sal', 2), ('Fermento', .5)]:
        db.session.add(MateriaPrima(nome=nome, custo_por_kg=4, estoque_atual=100000))
        receita.ingredientes.append(ReceitaIngrediente(
            tipo='mp', ingrediente_nome=nome, porcentagem=pct))
    levain = Receita(nome='Levain', categoria='Insumos', peso_base=1000,
                     rendimento_qtd=1000, rendimento_unidade='g', peso_unitario=1,
                     sub_na_amassadeira=True)
    db.session.add(levain)
    db.session.flush()
    receita.ingredientes.append(ReceitaIngrediente(
        tipo='receita', ingrediente_nome=levain.nome,
        sub_receita=levain, porcentagem=200))
    db.session.add(EstoqueProducao(receita_id=levain.id, quantidade=50000))
    db.session.commit()
    return receita


def cliente(app, usuario):
    client = app.test_client()
    with client.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True
    return client


def payload(rec, qtd=220, chave='primeiro-lancamento-220'):
    return {'chave_envio': chave,
            'itens': [{'ref': f'receita:{rec.id}', 'quantidade': qtd}]}


def test_220_reais_cria_ordem_concluida_sem_17_ficticios_e_repete_sem_duplicar(
        app, admin_user, frances):
    c = cliente(app, admin_user)
    for _ in range(2):
        resposta = c.post('/padeiro/produzir', json=payload(frances))
        assert resposta.status_code == 200, resposta.json
        assert resposta.json['resumo'] == [{'nome': frances.nome, 'qtd': 220}]
    plano = PlanejamentoProducao.query.one()
    item = plano.itens[0]
    assert plano.data == hoje() and plano.status == 'executado'
    assert plano.origem == 'avulsa_padeiro'
    assert item.qtd_alvo == item.produzido_qtd == 220
    assert item.batelada_padrao.bateladas == 1
    assert item.batelada_padrao.dados['unidades'] == 237
    assert EstoqueProducao.query.filter_by(receita_id=frances.id).one().quantidade == 220
    assert MovEstoqueProducao.query.filter_by(tipo='producao').count() == 1
    assert all(p.quantidade == 0 for p in PreBaixaMP.query.all())
    farinha = MateriaPrima.query.filter_by(nome='Farinha').one()
    assert farinha.estoque_atual == pytest.approx(100000 - 12000 * 220 / 237)
    assert CronogramaOverride.query.count() == 0
    historico = c.get('/padeiro/producao-historico.json').json['historico']
    assert len(historico) == 1 and historico[0]['qtd'] == 220
    detalhe = c.get(f'/producao/{plano.id}')
    assert detalhe.status_code == 200
    assert 'Unidades produzidas' in detalhe.text
    assert '<strong>220</strong>' in detalhe.text
    assert 'Rendimento de referência: 237' in detalhe.text
    # Mesmo recibo não pode ser reaproveitado para aumentar a produção.
    assert c.post('/padeiro/produzir', json=payload(frances, 221)).status_code == 400
    assert MovEstoqueProducao.query.filter_by(tipo='producao').count() == 1


def test_ordem_hoje_existente_encaminha_sem_criar_extra(app, admin_user, frances):
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma', enviado_ao_padeiro=True)
    plano.itens.append(PlanejamentoItem(receita=frances, qtd_alvo=237, produzido_qtd=0))
    db.session.add(plano)
    db.session.commit()
    resposta = cliente(app, admin_user).post('/padeiro/produzir', json=payload(frances))
    assert resposta.status_code == 409
    assert resposta.json['ordem_url'].startswith('/padeiro/?data=')
    assert PlanejamentoProducao.query.count() == 1
    assert MovEstoqueProducao.query.count() == 0
    assert not AppConfig.query.filter(AppConfig.key.like('producao_tv:%')).first()


def test_falha_desfaz_recibo_ordem_reserva_e_todo_lote(app, admin_user, frances, monkeypatch):
    from app.services import producao
    executar = producao.produzir_item_plano

    def falhar(*args, **kwargs):
        executar(*args, **kwargs)
        raise ValueError('falha depois de creditar')

    monkeypatch.setattr(producao, 'produzir_item_plano', falhar)
    resposta = cliente(app, admin_user).post('/padeiro/produzir', json=payload(frances))
    assert resposta.status_code == 400
    assert PlanejamentoProducao.query.count() == 0
    assert MovEstoqueProducao.query.count() == 0
    assert PreBaixaMP.query.count() == 0
    assert MateriaPrima.query.filter_by(nome='Farinha').one().estoque_atual == 100000
    assert not AppConfig.query.filter(AppConfig.key.like('producao_tv:%')).first()


def test_padeiro_pode_registrar_extra_sem_permissao_de_admin(app, frances):
    from app.models import Usuario
    padeiro = Usuario(nome='Padeiro', login='padeiro-extra-teste', papel='padeiro')
    padeiro.set_senha('teste-local')
    db.session.add(padeiro)
    db.session.commit()
    resposta = cliente(app, padeiro).post('/padeiro/produzir', json=payload(frances))
    assert resposta.status_code == 200, resposta.json
    assert PlanejamentoProducao.query.one().criado_por == padeiro.id
