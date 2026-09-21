from datetime import date

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovimentacaoEstoque,
    PreBaixaMP,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import (
    consolidar_lista_compras,
    enviar_plano_do_dia,
    ordem_compra_consolidada,
    produzir_item_plano,
)


def _cliente(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.mark.parametrize('cadastro', ['receitas', 'produtos'])
def test_cadastro_ajax_permite_custo_pendente_e_zero_explicito(app, admin_user, cadastro):
    client = _cliente(app, admin_user)
    for nome, raw, esperado in [('Azeitonas', '', None), ('Água', '0', 0.0),
                                 ('Chocolate', '1.234,56', 1234.56)]:
        response = client.post(f'/{cadastro}/api/nova-mp',
                               data={'mp_nome': nome, 'mp_custo': raw})
        assert response.json['success']
        assert response.json['custo'] == esperado
        assert MateriaPrima.query.filter_by(nome=nome).one().custo_por_kg == esperado
    for raw in ('-1', 'nan', 'inf', 'não é preço'):
        response = client.post(f'/{cadastro}/api/nova-mp',
                               data={'mp_nome': 'Inválida', 'mp_custo': raw})
        assert not response.json['success']
    assert MateriaPrima.query.filter_by(nome='Inválida').count() == 0


def test_banco_salva_sem_custo_e_preenche_depois_sem_mudar_estoque(app, admin_user):
    client = _cliente(app, admin_user)
    form = {'mp_id[]': [''], 'nome[]': ['Azeitonas'], 'unidade[]': ['g'],
            'custo_por_kg[]': [''], 'fornecedor[]': [''], 'observacoes[]': ['']}
    assert client.post('/materias-primas/salvar', data=form).status_code == 302
    mp = MateriaPrima.query.filter_by(nome='Azeitonas').one()
    assert mp.custo_por_kg is None
    form.update({'mp_id[]': [str(mp.id)], 'custo_por_kg[]': ['25,40']})
    assert client.post('/materias-primas/salvar', data=form).status_code == 302
    assert mp.custo_por_kg == 25.4
    assert mp.estoque_atual == 0
    assert MovimentacaoEstoque.query.count() == 0


def _receita(nome):
    mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=None,
                      estoque_atual=100000)
    rec = Receita(nome=nome, categoria='Pães', peso_base=1000,
                  rendimento_qtd=10, rendimento_unidade='un', peso_unitario=100)
    db.session.add_all([mp, rec])
    db.session.flush()
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='mp', ingrediente_nome=mp.nome, porcentagem=100, eh_base=True))
    db.session.commit()
    return mp, rec


@pytest.mark.parametrize('nome', ['Pão comum', 'Sourdough Tradicional'])
def test_envio_e_producao_com_custo_pendente_preservam_baixas(app, admin_user, nome):
    mp, rec = _receita(nome)
    dia = date(2026, 9, 21)
    crono = {'receitas': [{'receita_id': rec.id, 'por_dia': [
        {'data': dia.isoformat(), 'qtd': 10}]}]}
    plano = enviar_plano_do_dia(dia, admin_user.id, crono=crono)
    item = plano.itens[0]
    saldo_reservado = mp.estoque_atual
    total_reservado = PreBaixaMP.query.filter_by(plano_id=plano.id).one().quantidade
    assert total_reservado > 0
    assert produzir_item_plano(item.id, 4, admin_user.id)['ok']
    assert produzir_item_plano(item.id, item.qtd_alvo - 4, admin_user.id)['ok']
    assert item.produzido_qtd == item.qtd_alvo
    assert mp.estoque_atual == pytest.approx(saldo_reservado)
    assert PreBaixaMP.query.filter_by(plano_id=plano.id).one().quantidade == pytest.approx(0)
    assert EstoqueProducao.query.filter_by(receita_id=rec.id).one().quantidade == item.qtd_alvo
    assert mp.custo_por_kg is None


def test_compras_e_telas_exibem_pendencia_sem_perder_quantidades(app, admin_user):
    mp, rec = _receita('Pão comum')
    mp.estoque_atual = 0
    produto = Produto(nome='Cesta teste', ativo=True)
    produto.itens.append(ProdutoItem(tipo='mp', item_nome=mp.nome, materia_prima=mp,
                                     quantidade=200))
    db.session.add(produto)
    db.session.commit()
    itens = [{'receita_id': rec.id, 'multiplicador': 2}]
    lista = consolidar_lista_compras(itens)
    assert lista[mp.nome]['quantidade'] == 2000
    assert lista[mp.nome]['custo_estimado'] is None
    compra = ordem_compra_consolidada(itens)
    assert compra['total_compra'] is None
    assert compra['fornecedores'][0]['itens'][0]['comprar'] == 2000
    client = _cliente(app, admin_user)
    for url in ['/materias-primas/?v2=1', '/materias-primas/estoque',
                f'/materias-primas/estoque/{mp.id}/historico',
                f'/produtos/{produto.id}', '/produtos/']:
        response = client.get(url)
        assert response.status_code == 200, url
        assert 'Custo pendente' in response.text, url
    assert client.get(f'/receitas/{rec.id}').status_code == 200


@pytest.mark.parametrize(('raw', 'esperado'), [(None, None), (' ', None),
                                               (0, 0), ('0', 0), ('1.234,56', 1234.56)])
def test_parse_custo_opcional_distingue_zero_de_ausencia(raw, esperado):
    from app.services.custo_opcional import parse_custo_opcional
    assert parse_custo_opcional(raw) == esperado
