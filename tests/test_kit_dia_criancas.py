"""Kit Dia das Crianças (07/10/2026): cadastro por seed + `Produto.site_ate`."""
from datetime import datetime
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services import kit_dia_criancas, loja_catalogo


def _base():
    db.session.add_all([
        Receita(nome='Croissant Tradicional', categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000),
        Receita(nome='Brioche', categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000),
        Produto(nome='Salada de Frutas 100g', categoria='Acompanhamentos', ativo=True),
    ])
    db.session.commit()


def test_cria_kit_com_composicao_informada(app):
    _base()
    r = kit_dia_criancas.criar(subir_foto=False)
    db.session.commit()
    assert r['criado'] and r['brioche_novo'] and r['componentes'] == 5
    kit = db.session.get(Produto, r['kit_id'])
    assert kit.preco_site == 120.0 and kit.site_ativo and kit.categoria == 'Cestas'
    assert kit.site_ate == datetime(2026, 10, 12, 18, 0)
    itens = ProdutoItem.query.filter_by(produto_id=kit.id).all()
    assert sorted(i.item_nome for i in itens) == sorted([
        'Croissant Tradicional', 'Brioche do Mickey', 'Salada de Frutas 100g',
        'Bisnaga de chocolate', 'Bisnaga de confete'])
    assert all(i.quantidade == 1 for i in itens)
    assert all(i.receita_id or i.produto_componente_id or i.materia_prima_id for i in itens)
    brioche = Receita.query.filter_by(nome='Brioche do Mickey').one()
    assert brioche.categoria == 'Pães' and brioche.site_ativo is False
    mp = MateriaPrima.query.filter_by(nome='Bisnaga de chocolate').one()
    assert mp.unidade == 'un' and mp.custo_por_kg is None


def test_idempotente_nao_mexe_no_kit_existente(app):
    _base()
    r = kit_dia_criancas.criar(subir_foto=False)
    db.session.commit()
    kit = db.session.get(Produto, r['kit_id'])
    kit.preco_site = 99.0
    db.session.commit()
    r2 = kit_dia_criancas.criar(subir_foto=False)
    assert r2 == {'kit_id': kit.id, 'criado': False}
    assert db.session.get(Produto, kit.id).preco_site == 99.0
    assert Produto.query.filter_by(nome='Kit Dia das Crianças').count() == 1


def test_reusa_itens_existentes(app):
    _base()
    db.session.add_all([Receita(nome='Brioche do Mickey', categoria='X', rendimento_qtd=1,
                                rendimento_unidade='unidades', peso_base=1000),
                        MateriaPrima(nome='Bisnaga de Confete', unidade='un')])
    db.session.commit()
    r = kit_dia_criancas.criar(subir_foto=False)
    assert r['brioche_novo'] is False
    assert r['mps']['Bisnaga de Confete'][1] is False
    assert Receita.query.filter_by(nome='Brioche do Mickey').count() == 1


def test_sem_croissant_recusa_sem_gravar(app):
    db.session.add(Produto(nome='Salada de Frutas 100g', ativo=True))
    db.session.commit()
    with pytest.raises(ValueError, match='Croissant Tradicional'):
        kit_dia_criancas.criar(subir_foto=False)
    db.session.rollback()
    assert Produto.query.filter_by(nome='Kit Dia das Crianças').count() == 0


def _produto_site(site_ate):
    p = Produto(nome='Caixa X', categoria='Cestas', ativo=True, site_ativo=True,
                preco_site=50.0, site_ate=site_ate)
    db.session.add(p)
    db.session.commit()
    return p


def test_site_ate_no_passado_tira_da_vitrine_e_do_checkout(app):
    p = _produto_site(datetime(2026, 10, 12, 18, 0))
    with patch('app.utils.agora', return_value=datetime(2026, 10, 12, 18, 0)):
        assert loja_catalogo.por_id_publicado('produto', p.id) is None
        assert all(i['id'] != p.id or i['kind'] != 'produto'
                   for i in loja_catalogo.produtos_publicados())


def test_site_ate_no_futuro_e_nulo_seguem_a_venda(app):
    futuro = _produto_site(datetime(2026, 10, 12, 18, 0))
    with patch('app.utils.agora', return_value=datetime(2026, 10, 12, 17, 59)):
        assert loja_catalogo.por_id_publicado('produto', futuro.id) is not None
    futuro.site_ate = None
    db.session.commit()
    with patch('app.utils.agora', return_value=datetime(2030, 1, 1)):
        assert loja_catalogo.por_id_publicado('produto', futuro.id) is not None


def test_tela_salva_e_limpa_vender_ate(app, admin_user):
    p = _produto_site(None)
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    pagina = c.get(f'/produtos/{p.id}')
    assert pagina.status_code == 200 and 'name="site_ate"' in pagina.get_data(as_text=True)
    app.config['WTF_CSRF_ENABLED'] = False
    url = f'/produtos/{p.id}/salvar'
    c.post(url, data={'nome': 'Caixa X', 'site_ate': '2026-10-12T18:00'})
    assert db.session.get(Produto, p.id).site_ate == datetime(2026, 10, 12, 18, 0)
    c.post(url, data={'nome': 'Caixa X', 'site_ate': 'lixo'})
    assert db.session.get(Produto, p.id).site_ate == datetime(2026, 10, 12, 18, 0)
    c.post(url, data={'nome': 'Caixa X', 'site_ate': ''})
    assert db.session.get(Produto, p.id).site_ate is None


# --- D+2 (regra da Caixa de Mini) + SKU do Abraço no Tiny (07/10/2026) ---

def _kit_e_abraco(sku_abraco='ABR-01', confirmado=True):
    from app.models import TinyProdutoMap
    from app.utils import agora
    _base()
    r = kit_dia_criancas.criar(subir_foto=False)
    ab = Produto(nome='Abraço em Forma de Pães', categoria='Cestas', ativo=True)
    cx = Produto(nome='Caixa de Mini', categoria='Cestas', ativo=True, sob_encomenda=True)
    db.session.add_all([ab, cx])
    db.session.flush()
    if sku_abraco:
        db.session.add(TinyProdutoMap(canal='site', kind='produto', item_id=ab.id,
                                      tiny_sku=sku_abraco, tiny_nome='ABRACO PAES',
                                      auto_match=not confirmado,
                                      confirmado_em=agora() if confirmado else None))
    db.session.commit()
    return db.session.get(Produto, r['kit_id'])


def test_kit_vira_sob_encomenda_e_usa_sku_do_abraco(app):
    from app.services import tiny_nf
    kit = _kit_e_abraco()
    r = kit_dia_criancas.ajustar_d2_e_fiscal()
    db.session.commit()
    assert kit.sob_encomenda is True and r['caixa_mini_sob_encomenda'] is True
    assert r['sku_motivo'] == 'copiado_do_abraco'
    assert tiny_nf.sku_do_item('produto', kit.id) == 'ABR-01'


@pytest.mark.parametrize('sku, confirmado', [(None, True), ('ABR-01', False)])
def test_sku_do_abraco_ausente_ou_nao_confirmado_nao_e_copiado(app, sku, confirmado):
    from app.services import tiny_nf
    kit = _kit_e_abraco(sku, confirmado)
    r = kit_dia_criancas.ajustar_d2_e_fiscal()
    assert kit.sob_encomenda is True
    assert r['sku_motivo'] == 'abraco_sem_sku_confirmado'
    assert tiny_nf.sku_do_item('produto', kit.id) is None


def test_sku_definido_por_humano_no_kit_nao_e_sobrescrito(app, admin_user):
    from app.models import TinyProdutoMap
    from app.services import tiny_nf
    kit = _kit_e_abraco()
    db.session.add(TinyProdutoMap(canal='site', kind='produto', item_id=kit.id,
                                  tiny_sku='KIT-99', confirmado_por=admin_user.id))
    db.session.commit()
    r = kit_dia_criancas.ajustar_d2_e_fiscal()
    assert r['sku_motivo'] == 'kit_ja_tem_sku_definido_por_humano'
    assert tiny_nf.sku_do_item('produto', kit.id) == 'KIT-99'
