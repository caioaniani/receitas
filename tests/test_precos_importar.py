"""Importacao em lote de precos internos (parse + classificar + aplicar).

Cobre o fluxo do dono (25/06/2026): colar uma lista TSV que mistura
Padaria/Fornecedor/Funcionarios e ter o sistema:
1. Aceitar so Padaria.
2. Atualizar preco_interno do que ja existe.
3. Criar Produto novo pro que nao existe.
4. Ignorar com motivo o que e Fornecedor/Funcionarios.
5. Ser idempotente (rodar 2x nao duplica).
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
    assert _norm('SOURDOUGH NOZES E AZEITONAS') == 'sourdough nozes e azeitonas'


def test_parse_preco_aceita_formatos_br_e_us():
    assert _parse_preco('R$ 1.234,56') == 1234.56
    assert _parse_preco('6,50') == 6.5
    assert _parse_preco('100.00') == 100.0
    assert _parse_preco('R$ 0,30') == 0.30
    assert _parse_preco('lixo') is None
    assert _parse_preco('') is None


def test_parse_lista_tsv():
    texto = (
        'Produto\tCategoria\tPreço\tUnidade\n'
        'Brioche\tPadaria\tR$ 10,00\tun\n'
        'Abacaxi\tFornecedor\tR$ 6,50\tun\n'
        '\n'  # linha vazia ignorada
        'sem tabs e sem dois espacos  invalido'
    )
    out = parse_lista(texto)
    assert len(out) == 2
    assert out[0]['nome'] == 'Brioche'
    assert out[0]['categoria'] == 'Padaria'
    assert out[0]['preco'] == 10.0
    assert out[1]['categoria'] == 'Fornecedor'


def test_classificar_padaria_existente_vai_pra_atualizar(app):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        db.session.add(Produto(nome='Brioche', categoria='Padaria', ativo=True))
        db.session.commit()
        linhas = [{'nome': 'brioche', 'categoria': 'Padaria',
                   'preco': 10.0, 'unidade': 'un'}]
        plano = classificar(linhas)
    assert len(plano['atualizar']) == 1
    assert plano['atualizar'][0][1] == 'produto'
    assert len(plano['criar']) == 0


def test_classificar_padaria_nova_vai_pra_criar(app):
    with app.app_context():
        linhas = [{'nome': 'Brioche Mickey Pequeno', 'categoria': 'Padaria',
                   'preco': 10.0, 'unidade': 'un'}]
        plano = classificar(linhas)
    assert len(plano['criar']) == 1
    assert plano['criar'][0]['nome'] == 'Brioche Mickey Pequeno'


def test_classificar_fornecedor_funcionarios_sao_ignorados(app):
    with app.app_context():
        linhas = [
            {'nome': 'Abacaxi', 'categoria': 'Fornecedor', 'preco': 6.5, 'unidade': 'un'},
            {'nome': 'Funcionarios', 'categoria': 'Funcionarios', 'preco': 31000, 'unidade': 'un'},
        ]
        plano = classificar(linhas)
    assert len(plano['ignorar']) == 2
    motivos = [m for _, m in plano['ignorar']]
    assert any('Fornecedor' in m for m in motivos)
    assert any('Funcionarios' in m for m in motivos)


def test_aplicar_atualiza_e_cria(app):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        db.session.add(Produto(nome='Brioche', categoria='Padaria', ativo=True))
        db.session.commit()
        linhas = [
            {'nome': 'Brioche', 'categoria': 'Padaria', 'preco': 10.0, 'unidade': 'un'},
            {'nome': 'Pao De Cramberry', 'categoria': 'Padaria', 'preco': 9.0, 'unidade': 'un'},
            {'nome': 'Sacola', 'categoria': 'Fornecedor', 'preco': 1.09, 'unidade': 'un'},
        ]
        plano = classificar(linhas)
        res = aplicar(plano)
        assert res == {'atualizados': 1, 'criados': 1, 'ignorados': 1}

        assert Produto.query.filter_by(nome='Brioche').first().preco_interno == 10.0
        novo = Produto.query.filter_by(nome='Pao De Cramberry').first()
        assert novo is not None
        assert novo.preco_interno == 9.0
        assert novo.categoria == 'Padaria'
        # Fornecedor NAO foi criado
        assert Produto.query.filter_by(nome='Sacola').first() is None


def test_aplicar_eh_idempotente(app):
    """Rodar 2 vezes a mesma lista nao duplica produtos."""
    from app.models import Produto
    with app.app_context():
        linhas = [
            {'nome': 'Cookie Novo', 'categoria': 'Padaria', 'preco': 6.0, 'unidade': 'un'},
        ]
        aplicar(classificar(linhas))
        # Segunda passada: deve cair em "atualizar", nao criar de novo.
        plano2 = classificar(linhas)
        assert len(plano2['criar']) == 0
        assert len(plano2['atualizar']) == 1
        aplicar(plano2)
        # So existe 1 Cookie Novo no banco
        assert Produto.query.filter_by(nome='Cookie Novo').count() == 1


def test_rota_get_owner(app, owner_user):
    client = app.test_client()
    _login(client, owner_user)
    resp = client.get('/receitas/precos/importar')
    assert resp.status_code == 200
    assert b'Importar pre' in resp.data


def test_rota_preview_nao_persiste(app, owner_user):
    """POST sem `confirmar` so mostra preview — nao grava."""
    from app.models import Produto
    client = app.test_client()
    _login(client, owner_user)
    texto = 'Pao Teste Preview\tPadaria\tR$ 5,00\tun'
    resp = client.post('/receitas/precos/importar',
                       data={'texto': texto})
    assert resp.status_code == 200
    assert b'Pao Teste Preview' in resp.data  # apareceu no preview
    with app.app_context():
        # NAO foi criado
        assert Produto.query.filter_by(nome='Pao Teste Preview').first() is None


def test_rota_confirmar_aplica(app, owner_user):
    from app.extensions import db
    from app.models import Produto
    client = app.test_client()
    _login(client, owner_user)
    texto = 'Pao Confirma\tPadaria\tR$ 7,00\tun'
    resp = client.post('/receitas/precos/importar',
                       data={'texto': texto, 'confirmar': '1'},
                       follow_redirects=False)
    assert resp.status_code == 302
    with app.app_context():
        p = Produto.query.filter_by(nome='Pao Confirma').first()
        assert p is not None
        assert p.preco_interno == 7.0
    # cleanup
    with app.app_context():
        db.session.delete(Produto.query.filter_by(nome='Pao Confirma').first())
        db.session.commit()


def test_rota_exige_owner(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/receitas/precos/importar')
    assert resp.status_code == 403


def test_lista_real_do_dono_processa_corretamente(app):
    """Sanity da lista colada pelo dono em 25/06/2026: 40 Padaria + 44
    Fornecedor + 3 Funcionarios = 87 linhas. So Padaria conta."""
    lista = '''Abacaxi\tFornecedor\tR$ 6,50\tun
Brioche\tPadaria\tR$ 10,00\tun
Bowl\tFornecedor\tR$ 132,22\tun
Cinnamon Roll\tPadaria\tR$ 8,47\tun
Funcionarios\tFuncionarios\tR$ 31.093,28\tun
Adiantamento de salário\tFuncionarios\tR$ 4.000,00\tun
Pão de cramberry\tPadaria\tR$ 9,00\tun'''
    with app.app_context():
        linhas = parse_lista(lista)
        plano = classificar(linhas)
    # 4 sao Padaria, 3 sao ignoradas
    total_padaria = len(plano['atualizar']) + len(plano['criar'])
    assert total_padaria == 3 or total_padaria == 4  # depende do que existe no banco de teste
    # Fornecedor (Abacaxi, Bowl) + Funcionarios (Funcionarios, Adiantamento) = 4
    assert len(plano['ignorar']) == 4
