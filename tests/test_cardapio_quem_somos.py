"""Cardápio — bloco "Quem somos nós" (21/07/2026, pedido do dono).

A história da casa (escrita a partir do relato da fundação) aparece no
RODAPÉ do cardápio, antes das regras/métodos, nos TRÊS tipos (texto de
marca ≠ regras/preparo, que são só do atacado). AppConfig
`cardapio_quem_somos`, um parágrafo por linha; MESMO contrato do preparo:
chave AUSENTE = default no código; gravada VAZIA = escondido de propósito.
"""
import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Brioche', **kw):
    from app.models import Receita
    base = dict(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=18.0,
                preco_loja=20.0)
    base.update(kw)
    r = Receita(**base)
    db.session.add(r)
    db.session.commit()
    return r


# ── Helper / contrato AppConfig ────────────────────────────────────────────

def test_default_sem_config(app):
    """Chave AUSENTE → default do código (a história real: família,
    pandemia, Abraço em Forma de Pão, fermentação natural)."""
    from app.blueprints.main.routes import _quem_somos
    paragrafos = _quem_somos()
    assert len(paragrafos) == 4
    texto = ' '.join(paragrafos)
    assert 'Abraço em Forma de Pão' in texto
    assert 'fermentação natural' in texto
    assert 'T65' in texto and 'Callebaut' in texto


def test_custom_e_vazio(app):
    from app.blueprints.main.routes import _quem_somos
    from app.models import AppConfig
    AppConfig.set('cardapio_quem_somos', 'Nossa história.\n\nSegunda linha.')
    db.session.commit()
    assert _quem_somos() == ['Nossa história.', 'Segunda linha.']

    # Gravada VAZIA = dono apagou de propósito → bloco some (≠ default)
    AppConfig.set('cardapio_quem_somos', '')
    db.session.commit()
    assert _quem_somos() == []


# ── Tela ───────────────────────────────────────────────────────────────────

def test_tela_mostra_nos_tres_tipos(app, admin_user, cliente):
    """Texto de marca: aparece no atacado E na loja/site (diferente das
    regras/preparo, que seguem só no atacado)."""
    _receita()
    _login(cliente, admin_user)
    for tipo in ('atacado', 'loja', 'site'):
        body = cliente.get(f'/cardapio?tipo={tipo}').get_data(as_text=True)
        assert 'Quem somos nós' in body, tipo
        assert 'Abraço em Forma de Pão' in body, tipo


def test_tela_rodape_historia_antes_das_regras(app, admin_user, cliente):
    """Produtos primeiro (regra do dono 20/07); a história vem antes do
    operacional (regras do pedido)."""
    from app.models import AppConfig
    _receita()
    AppConfig.set('cardapio_atacado_pedido_minimo', 'R$ 500,00')
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Brioche') < body.index('Quem somos nós')
    assert body.index('Quem somos nós') < body.index('Regras do pedido')


def test_tela_esconde_quando_vazio(app, admin_user, cliente):
    from app.models import AppConfig
    _receita()
    AppConfig.set('cardapio_quem_somos', '')
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Quem somos nós' not in body


# ── Tela de regras (edição) ────────────────────────────────────────────────

def test_form_regras_salva_quem_somos(app, admin_user, cliente):
    from app.models import AppConfig
    _login(cliente, admin_user)
    body = cliente.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'name="quem_somos"' in body
    assert 'Abraço em Forma de Pão' in body   # textarea pré-preenchida
    cliente.post('/admin/cardapio-atacado/regras',
                 data={'quem_somos': 'História editada pelo dono.'})
    assert AppConfig.get('cardapio_quem_somos') == \
        'História editada pelo dono.'


# ── PDF ────────────────────────────────────────────────────────────────────

def _cats():
    return {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'descricao': None, 'imagem_url': None,
                      'img_ref': None}]}


def test_pdf_cresce_com_quem_somos_em_todos_os_tipos(app):
    from app.blueprints.main.routes import CARDAPIO_QUEM_SOMOS_DEFAULT
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    paragrafos = [ln for ln in CARDAPIO_QUEM_SOMOS_DEFAULT.splitlines()
                  if ln.strip()]
    for tipo in ('atacado', 'loja', 'site'):
        sem = gerar_cardapio_pdf(tipo, _cats(), [], quem_somos=None)
        com = gerar_cardapio_pdf(tipo, _cats(), [], quem_somos=paragrafos)
        assert com.startswith(b'%PDF')
        assert len(com) > len(sem) + 100, tipo
