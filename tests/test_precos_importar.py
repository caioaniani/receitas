"""Importacao em lote de precos internos (parse + classificar + aplicar).

Refeito em 25/06/2026 apos decisao do dono: NAO filtra automaticamente
Fornecedor/Funcionarios — mostra TUDO no preview e ele marca caso a caso.
"""
from app.services.precos_importar import (
    _norm,
    _parse_preco,
    aplicar,
    classificar,
    parse_lista,
)


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_norm_lower_e_sem_acentos():
    assert _norm('Pão Francês') == 'pao frances'
    assert _norm('  Brioche   ') == 'brioche'


def test_parse_preco_aceita_formatos_br_e_us():
    assert _parse_preco('R$ 1.234,56') == 1234.56
    assert _parse_preco('6,50') == 6.5
    assert _parse_preco('100.00') == 100.0
    assert _parse_preco('lixo') is None


def test_parse_lista_tsv():
    texto = (
        'Produto\tCategoria\tPreço\tUnidade\n'
        'Brioche\tPadaria\tR$ 10,00\tun\n'
        'Abacaxi\tFornecedor\tR$ 6,50\tun\n'
    )
    out = parse_lista(texto)
    assert len(out) == 2
    assert out[0]['nome'] == 'Brioche'
    assert out[0]['preco'] == 10.0


def test_classificar_existente_vira_atualizar(app):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        db.session.add(Produto(nome='Brioche', categoria='Padaria', ativo=True))
        db.session.commit()
        plano = classificar([{'nome': 'brioche', 'categoria': 'Padaria',
                              'preco': 10.0, 'unidade': 'un'}])
    assert len(plano) == 1
    assert plano[0]['acao'] == 'atualizar'
    assert plano[0]['tipo'] == 'produto'
    assert plano[0]['sugestao_marcar'] is True


def test_classificar_novo_vira_criar(app):
    with app.app_context():
        plano = classificar([{'nome': 'Brioche Mickey Pequeno',
                              'categoria': 'Padaria', 'preco': 10.0,
                              'unidade': 'un'}])
    assert plano[0]['acao'] == 'criar'
    assert plano[0]['obj'] is None


def test_fornecedor_funcionarios_sao_listados_mas_desmarcados(app):
    """Mudanca de 25/06/2026: NAO filtra mais. Tudo vai pro preview;
    Fornecedor/Funcionarios vem com sugestao_marcar=False (desmarcados)."""
    with app.app_context():
        plano = classificar([
            {'nome': 'Abacaxi', 'categoria': 'Fornecedor', 'preco': 6.5,
             'unidade': 'un'},
            {'nome': 'Funcionarios', 'categoria': 'Funcionarios',
             'preco': 31000, 'unidade': 'un'},
            {'nome': 'Brioche', 'categoria': 'Padaria', 'preco': 10.0,
             'unidade': 'un'},
        ])
    assert len(plano) == 3
    assert plano[0]['sugestao_marcar'] is False
    assert plano[1]['sugestao_marcar'] is False
    assert plano[2]['sugestao_marcar'] is True


def test_aplicar_so_persiste_indices_marcados(app):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        db.session.add(Produto(nome='Brioche', categoria='Padaria', ativo=True))
        db.session.commit()
        plano = classificar([
            {'nome': 'Brioche', 'categoria': 'Padaria', 'preco': 10.0,
             'unidade': 'un'},
            {'nome': 'Pao De Cramberry', 'categoria': 'Padaria', 'preco': 9.0,
             'unidade': 'un'},
            {'nome': 'Sacola', 'categoria': 'Fornecedor', 'preco': 1.09,
             'unidade': 'un'},
        ])
        # Marca so o Brioche (idx 0) e o Pao de Cramberry (idx 1). Sacola (idx 2) NAO.
        res = aplicar(plano, {0, 1})
        assert res['atualizados'] == 1
        assert res['criados'] == 1
        assert res['desmarcados'] == 1
        assert Produto.query.filter_by(nome='Brioche').first().preco_interno == 10.0
        assert Produto.query.filter_by(nome='Pao De Cramberry').first() is not None
        assert Produto.query.filter_by(nome='Sacola').first() is None


def test_aplicar_marcando_fornecedor_PERMITE_cadastrar(app):
    """O dono pode marcar Coca-cola (Fornecedor) se for produto que revende.
    A categoria vai como esta no input — nao tem mais filtro automatico."""
    from app.models import Produto
    with app.app_context():
        plano = classificar([
            {'nome': 'Coca-cola lata', 'categoria': 'Fornecedor',
             'preco': 3.45, 'unidade': 'un'},
        ])
        # Por default vinha desmarcado (sugestao_marcar=False),
        # mas o dono marca explicitamente:
        aplicar(plano, {0})
        novo = Produto.query.filter_by(nome='Coca-cola lata').first()
        assert novo is not None
        assert novo.preco_interno == 3.45
        # Categoria preservada (vai como Fornecedor mesmo).
        assert novo.categoria == 'Fornecedor'


def test_aplicar_eh_idempotente(app):
    from app.models import Produto
    with app.app_context():
        linhas = [{'nome': 'Cookie Novo', 'categoria': 'Padaria',
                   'preco': 6.0, 'unidade': 'un'}]
        plano1 = classificar(linhas)
        aplicar(plano1, {0})
        plano2 = classificar(linhas)
        assert plano2[0]['acao'] == 'atualizar'  # ja existe
        aplicar(plano2, {0})
        assert Produto.query.filter_by(nome='Cookie Novo').count() == 1


def test_rota_get_owner(app, owner_user):
    client = app.test_client()
    _login(client, owner_user)
    resp = client.get('/receitas/precos/importar')
    assert resp.status_code == 200


def test_rota_preview_nao_persiste(app, owner_user):
    from app.models import Produto
    client = app.test_client()
    _login(client, owner_user)
    resp = client.post('/receitas/precos/importar',
                       data={'texto': 'Pao Teste Preview\tPadaria\tR$ 5,00\tun'})
    assert resp.status_code == 200
    assert b'Pao Teste Preview' in resp.data
    with app.app_context():
        assert Produto.query.filter_by(nome='Pao Teste Preview').first() is None


def test_rota_confirmar_so_aplica_marcados(app, owner_user):
    from app.extensions import db
    from app.models import Produto
    client = app.test_client()
    _login(client, owner_user)
    texto = ('Marcado A\tPadaria\tR$ 5,00\tun\n'
             'Nao Marcado B\tPadaria\tR$ 7,00\tun')
    resp = client.post('/receitas/precos/importar',
                       data={'texto': texto, 'confirmar': '1',
                             'marcar': ['0']},  # so o primeiro
                       follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        assert Produto.query.filter_by(nome='Marcado A').first() is not None
        assert Produto.query.filter_by(nome='Nao Marcado B').first() is None
        # cleanup
        db.session.delete(Produto.query.filter_by(nome='Marcado A').first())
        db.session.commit()


def test_rota_exige_owner(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/receitas/precos/importar')
    assert resp.status_code == 403
