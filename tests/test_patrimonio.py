"""Patrimônio — inventário de móveis e equipamentos com etiquetas QR
(20/07/2026, pedido do dono: "colar aqueles códigos de barras ou QR Code").

Ativo (1 linha = 1 etiqueta) + AtivoConferencia ("vi este ativo aqui").
Etiquetas em PDF do servidor (3×7 por A4); a página do QR é a de conferir,
aberta pela câmera de qualquer celular com login de funcionário.
"""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import Ativo, AtivoConferencia, Loja, Usuario
from app.utils import agora


def _loja(nome='Loja Patrimonio'):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    return lj


def _ativo(nome='Forno Turbo', loja_id=None, **kw):
    a = Ativo(nome=nome, loja_id=loja_id, **kw)
    db.session.add(a)
    db.session.commit()
    return a


def _login(client, login, senha='123'):
    client.post('/auth/login', data={'login': login, 'senha': senha})


def _funcionario():
    u = Usuario(nome='func pat', login='funcpat', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    return u


# ── Modelo ───────────────────────────────────────────────────────────────────

def test_codigo_e_local(app):
    lj = _loja()
    a1 = _ativo('Amassadeira')
    a2 = _ativo('Freezer vertical', loja_id=lj.id)
    assert a1.codigo == f'A-{a1.id:04d}'
    assert a1.local_nome == 'Indústria'
    assert a2.local_nome == 'Loja Patrimonio'
    assert a1.situacao == 'em_uso'


# ── Cadastro (admin) ─────────────────────────────────────────────────────────

def test_novo_single_com_valor_ptbr(app, admin_user):
    c = app.test_client()
    _login(c, 'admin')
    resp = c.post('/patrimonio/novo', data={
        'nome': 'Forno de lastro', 'categoria': 'Forno', 'loja_id': 'ind',
        'valor_aquisicao': '12.345,67', 'adquirido_em': '2024-03-01',
        'numero_serie': 'XYZ-9',
    }, follow_redirects=True)
    assert resp.status_code == 200
    a = Ativo.query.filter_by(nome='Forno de lastro').one()
    assert a.valor_aquisicao == Decimal('12345.67')
    assert a.loja_id is None
    assert a.adquirido_em.isoformat() == '2024-03-01'


def test_novo_em_lote(app, admin_user):
    lj = _loja()
    c = app.test_client()
    _login(c, 'admin')
    c.post('/patrimonio/novo', data={
        'nomes_lote': 'Mesa inox 1,90m\n\nGeladeira 4 portas\nBatedeira 20L',
        'categoria': 'Mobiliário', 'loja_id': str(lj.id),
    }, follow_redirects=True)
    ativos = Ativo.query.order_by(Ativo.id).all()
    assert [a.nome for a in ativos] == ['Mesa inox 1,90m',
                                        'Geladeira 4 portas', 'Batedeira 20L']
    assert all(a.loja_id == lj.id and a.categoria == 'Mobiliário'
               for a in ativos)


def test_lista_exige_admin(app):
    _funcionario()
    c = app.test_client()
    _login(c, 'funcpat')
    assert c.get('/patrimonio/').status_code in (302, 403)


def test_lista_renderiza_e_conta_nao_conferidos(app, admin_user):
    a1 = _ativo('Forno Nunca Visto')
    a2 = _ativo('Freezer Visto Ontem')
    db.session.add(AtivoConferencia(ativo_id=a2.id, estado='ok'))
    db.session.commit()
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/patrimonio/').get_data(as_text=True)
    assert a1.codigo in html and a2.codigo in html
    assert 'nunca conferido' in html
    assert '1 sem conferência' in html         # só o a1 (a2 conferido hoje)


def test_conferencia_velha_conta_como_nao_conferido(app, admin_user):
    a = _ativo('Chapa antiga')
    db.session.add(AtivoConferencia(ativo_id=a.id, estado='ok',
                                    momento=agora() - timedelta(days=90)))
    db.session.commit()
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/patrimonio/').get_data(as_text=True)   # desde default 30d
    assert '1 sem conferência' in html


# ── Conferência via QR (qualquer funcionário logado) ─────────────────────────

def test_funcionario_confere_ok(app):
    u = _funcionario()
    a = _ativo('TV do padeiro')
    c = app.test_client()
    _login(c, 'funcpat')
    resp = c.post(f'/patrimonio/{a.id}/conferir',
                  data={'estado': 'ok', 'loja_id_visto': 'ind'},
                  follow_redirects=True)
    assert resp.status_code == 200
    conf = AtivoConferencia.query.filter_by(ativo_id=a.id).one()
    assert conf.estado == 'ok'
    assert conf.usuario_id == u.id
    assert conf.loja_id_visto is None


def test_conferir_com_problema_grava_observacao(app):
    _funcionario()
    a = _ativo('Amassadeira 2')
    c = app.test_client()
    _login(c, 'funcpat')
    c.post(f'/patrimonio/{a.id}/conferir',
           data={'estado': 'problema', 'observacao': 'fazendo barulho',
                 'loja_id_visto': 'ind'},
           follow_redirects=True)
    conf = AtivoConferencia.query.filter_by(ativo_id=a.id).one()
    assert conf.estado == 'problema'
    assert conf.observacao == 'fazendo barulho'


def test_conferir_exige_login(app):
    a = _ativo('Sem login')
    c = app.test_client()
    resp = c.get(f'/patrimonio/{a.id}/conferir')
    assert resp.status_code in (302, 401)


def test_local_divergente_avisa_na_lista(app, admin_user):
    """Conferência viu o ativo em OUTRO local: a lista avisa (o cadastro
    não muda sozinho — mover é gesto do admin)."""
    lj = _loja('Loja Longe')
    a = _ativo('Cadeira andarilha')            # cadastro: indústria
    db.session.add(AtivoConferencia(ativo_id=a.id, estado='ok',
                                    loja_id_visto=lj.id))
    db.session.commit()
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/patrimonio/').get_data(as_text=True)
    assert 'visto em Loja Longe' in html


# ── Situação / baixa ─────────────────────────────────────────────────────────

def test_baixar_e_reativar(app, admin_user):
    a = _ativo('Freezer velho')
    c = app.test_client()
    _login(c, 'admin')
    c.post(f'/patrimonio/{a.id}/situacao', data={'situacao': 'baixado'},
           follow_redirects=True)
    assert a.situacao == 'baixado' and a.baixado_em is not None
    c.post(f'/patrimonio/{a.id}/situacao', data={'situacao': 'em_uso'},
           follow_redirects=True)
    assert a.situacao == 'em_uso' and a.baixado_em is None


# ── Etiquetas PDF ────────────────────────────────────────────────────────────

def test_etiquetas_pdf_gera_e_exclui_baixado(app, admin_user):
    from unittest.mock import patch
    _ativo('Forno etiqueta')
    baixado = _ativo('Baixado sem etiqueta')
    baixado.situacao = 'baixado'
    db.session.commit()
    c = app.test_client()
    _login(c, 'admin')
    with patch('app.services.patrimonio_pdf.gerar_etiquetas_pdf',
               wraps=None) as ge:
        ge.return_value = b'%PDF-fake'
        resp = c.get('/patrimonio/etiquetas.pdf')
        assert resp.status_code == 200
        nomes = [a.nome for a in ge.call_args[0][0]]
    assert 'Forno etiqueta' in nomes
    assert 'Baixado sem etiqueta' not in nomes


def test_etiquetas_pdf_documento_real(app, admin_user):
    for i in range(3):
        _ativo(f'Ativo Real {i}')
    c = app.test_client()
    _login(c, 'admin')
    resp = c.get('/patrimonio/etiquetas.pdf')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data.startswith(b'%PDF')
    assert len(resp.data) > 2000                # 3 QRs embutidos


def test_etiquetas_pdf_22_ativos_2_paginas(app):
    """21 por página (3×7): o 22º abre a segunda página."""
    from app.services.patrimonio_pdf import gerar_etiquetas_pdf
    ativos = []
    for i in range(22):
        ativos.append(_ativo(f'Ativo {i:02d}'))
    pdf = gerar_etiquetas_pdf(ativos, 'https://x')
    # A árvore /Type /Pages também casa o prefixo — 1 página = count 2,
    # 2 páginas = count 3 (achado de revisão: >= 2 era vácuo).
    assert pdf.count(b'/Type /Page') >= 3


def test_valor_round_trip_da_edicao_nao_multiplica(app, admin_user):
    """Achado de revisão (crítico): o form de editar re-renderizava o
    Decimal em formato en ('12345.67') e o parse antigo removia todo '.'
    → cada salvamento multiplicava o valor por 100. O round-trip agora é
    estável: renderiza pt-BR e o parse é o canônico da casa."""
    a = _ativo('Forno Caro', valor_aquisicao=Decimal('12345.67'))
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/patrimonio/').get_data(as_text=True)
    assert '12345,67' in html                      # render pt-BR no form
    # Salva o form como o browser mandaria (valor re-renderizado, sem mexer).
    c.post(f'/patrimonio/{a.id}/editar', data={
        'nome': 'Forno Caro', 'loja_id': 'ind', 'valor_aquisicao': '12345,67',
    }, follow_redirects=True)
    assert a.valor_aquisicao == Decimal('12345.67')   # NÃO virou 1.234.567
    # Valor inválido mantém o gravado (não vira None calado).
    c.post(f'/patrimonio/{a.id}/editar', data={
        'nome': 'Forno Caro', 'loja_id': 'ind', 'valor_aquisicao': 'abc',
    }, follow_redirects=True)
    assert a.valor_aquisicao == Decimal('12345.67')


def test_posts_de_gestao_exigem_admin(app):
    """Funcionário confere, mas NÃO cadastra/edita/baixa/imprime."""
    _funcionario()
    a = _ativo('Protegido')
    c = app.test_client()
    _login(c, 'funcpat')
    respostas = [
        c.post('/patrimonio/novo', data={'nome': 'X'}),
        c.post(f'/patrimonio/{a.id}/editar', data={'nome': 'Y'}),
        c.post(f'/patrimonio/{a.id}/situacao', data={'situacao': 'baixado'}),
        c.get('/patrimonio/etiquetas.pdf'),
    ]
    assert all(r.status_code in (302, 403) for r in respostas)
    assert a.nome == 'Protegido' and a.situacao == 'em_uso'
    assert Ativo.query.count() == 1


def test_conferir_nao_expoe_valor_de_aquisicao(app):
    """A página do QR é de qualquer funcionário — valor contábil não
    aparece nela (privacidade; trava de regressão pedida em revisão)."""
    _funcionario()
    a = _ativo('Forno Sigiloso', valor_aquisicao=Decimal('98765.43'))
    c = app.test_client()
    _login(c, 'funcpat')
    html = c.get(f'/patrimonio/{a.id}/conferir').get_data(as_text=True)
    assert '98765' not in html and '98.765' not in html


def test_area_nav_tem_link(app, admin_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    c = app.test_client()
    _login(c, 'admin')
    html = c.get('/').get_data(as_text=True)
    assert '/patrimonio' in html
