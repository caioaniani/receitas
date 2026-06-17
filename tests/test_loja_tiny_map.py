"""Mapeamento de SKU do Tiny (Fase 5): liga itens do site ao SKU pra NF.

Cobre: definir/limpar SKU, sugestão fuzzy por nome (sem sobrescrever
confirmado), e a tela admin owner-only.
"""
from unittest.mock import patch


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _produto_pub(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def test_definir_e_limpar_sku(app):
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        p = _produto_pub(db)
        tiny_nf.definir_sku('produto', p.id, 'SKU-123', tiny_nome='Box Mimo')
        assert tiny_nf.sku_do_item('produto', p.id) == 'SKU-123'
        # Limpar volta a pendente
        tiny_nf.definir_sku('produto', p.id, '')
        assert tiny_nf.sku_do_item('produto', p.id) is None


def test_sincronizar_match_exato_confirma(app):
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        p = _produto_pub(db, nome='Box Mimo')
        # Nome normaliza igual (acento/caixa) -> match EXATO -> confirma auto
        fake = [{'sku': 'TINY-BOX', 'nome': 'BOX MIMO', 'tiny_id': '9'}]
        with patch('app.services.tiny.listar_produtos', return_value=fake):
            res = tiny_nf.sincronizar_sugestoes()
        assert res['exatos'] == 1
        assert tiny_nf.sku_do_item('produto', p.id) == 'TINY-BOX'


def test_sincronizar_fuzzy_sugere(app):
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        p = _produto_pub(db, nome='Brioche')
        # Nome PARECIDO (não idêntico) -> sugestão pra conferir
        fake = [{'sku': 'BR-1', 'nome': 'Brioche SITE', 'tiny_id': '7'}]
        with patch('app.services.tiny.listar_produtos', return_value=fake):
            res = tiny_nf.sincronizar_sugestoes()
        assert res['sugeridos'] == 1
        assert tiny_nf.sku_do_item('produto', p.id) == 'BR-1'


def test_importar_planilha_csv(app):
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        p = _produto_pub(db, nome='Box Mimo')
        csv = (b'ID,Codigo (SKU),Descricao,Unidade\n'
               b'1,202383,Box Mimo,UN\n'
               b'2,99,Outro Produto,UN\n')
        res = tiny_nf.importar_planilha(csv, 'produtos.csv')
        assert res['exatos'] == 1
        assert tiny_nf.sku_do_item('produto', p.id) == '202383'


def test_importar_planilha_formato_invalido(app):
    from app.services import tiny_nf
    with app.app_context():
        res = tiny_nf.importar_planilha(b'lixo sem colunas', 'x.csv')
        assert res.get('erro')


def test_sync_nao_sobrescreve_confirmado(app):
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        p = _produto_pub(db, nome='Box Mimo')
        tiny_nf.definir_sku('produto', p.id, 'SKU-MANUAL')  # humano confirmou
        fake = [{'sku': 'TINY-OUTRO', 'nome': 'Box Mimo', 'tiny_id': '9'}]
        with patch('app.services.tiny.listar_produtos', return_value=fake):
            tiny_nf.sincronizar_sugestoes()
        # Mantém o que o humano pôs
        assert tiny_nf.sku_do_item('produto', p.id) == 'SKU-MANUAL'


def test_sync_sem_tiny_devolve_erro(app):
    from app.services import tiny_nf
    with app.app_context():
        with patch('app.services.tiny.listar_produtos', return_value=[]):
            res = tiny_nf.sincronizar_sugestoes()
        assert res.get('erro')


def test_tela_skus_owner(app):
    from app.extensions import db
    c = _owner(app)
    _produto_pub(db, nome='Bonjour')
    r = c.get('/admin/loja-online/tiny-skus')
    assert r.status_code == 200
    assert b'Bonjour' in r.data
    assert b'SKU' in r.data


def test_definir_sku_via_rota(app):
    from app.extensions import db
    from app.services import tiny_nf
    c = _owner(app)
    p = _produto_pub(db, nome='Family Box')
    r = c.post('/admin/loja-online/tiny-skus/definir', data={
        'kind': 'produto', 'item_id': str(p.id), 'sku': 'FAM-1',
    }, follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        assert tiny_nf.sku_do_item('produto', p.id) == 'FAM-1'


def test_tela_skus_nao_owner_bloqueado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='G', login='g', papel='admin', is_owner=False)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    assert c.get('/admin/loja-online/tiny-skus').status_code in (302, 403)
