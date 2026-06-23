"""Tela admin de acompanhamento dos pedidos do site (Fase 3).

Lista + detalhe + editar + imprimir + avanço de status liberados a TODOS os
usuários logados (22/06/2026, pra a equipe usar pelo painel de entregas).
Reembolso/cancelamento (dinheiro) e emissão de NF (fiscal) continuam owner-only.
"""
from decimal import Decimal


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


def _admin_nao_owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Ger', login='ger', papel='admin', is_owner=False)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido(db, codigo=None, status='aguardando_pagamento', nome='Maria'):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(nome_cliente=nome, email_cliente='m@x.com',
                     telefone_cliente='11999', modo_entrega='retirada',
                     status=status, subtotal=Decimal('20'),
                     valor_total=Decimal('20'))
    if codigo:
        p.codigo = codigo
    p.itens.append(PedidoOnlineItem(
        kind='produto', nome='Box Mimo', preco_unitario=Decimal('20'),
        quantidade=1, subtotal=Decimal('20')))
    db.session.add(p)
    db.session.commit()
    return p


def test_lista_pedidos_owner_200(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, nome='Cliente Teste')   # nasce 'aguardando_pagamento'
    # Default da tela e' 'pago' (22/06/2026); pra ver o aguardando, vai pra aba Todos.
    r = c.get('/admin/loja-online/pedidos?status=todos')
    assert r.status_code == 200
    assert b'Cliente Teste' in r.data
    assert b'Pedidos do site' in r.data


def test_default_lista_eh_pagos(app):
    """22/06/2026 — sem ?status= na URL, a tela mostra SO pagos (verde, foco
    da operacao). Pra ver todos, o usuario clica em 'Todos' (?status=todos)."""
    from app.extensions import db
    _pedido(db, codigo='AGU01', status='aguardando_pagamento')
    _pedido(db, codigo='PAG01', status='pago')
    c = _owner(app)
    # Sem ?status= -> default = pago
    r = c.get('/admin/loja-online/pedidos')
    assert r.status_code == 200
    assert b'PAG01' in r.data
    assert b'AGU01' not in r.data
    # ?status=todos -> mostra tudo (incluindo aguardando)
    r2 = c.get('/admin/loja-online/pedidos?status=todos')
    assert b'PAG01' in r2.data
    assert b'AGU01' in r2.data


def test_lista_filtra_por_status(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='AAAA0001', status='aguardando_pagamento')
    _pedido(db, codigo='BBBB0002', status='cancelado')
    r = c.get('/admin/loja-online/pedidos?status=cancelado')
    assert r.status_code == 200
    assert b'BBBB0002' in r.data
    assert b'AAAA0001' not in r.data


def test_detalhe_pedido_mostra_itens(app):
    from app.extensions import db
    c = _owner(app)
    p = _pedido(db, codigo='CCCC0003')
    r = c.get(f'/admin/loja-online/pedidos/{p.codigo}')
    assert r.status_code == 200
    assert b'Box Mimo' in r.data
    assert b'CCCC0003' in r.data


def test_cancelar_pedido(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _owner(app)
    p = _pedido(db, codigo='DDDD0004')
    r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
               follow_redirects=False)
    assert r.status_code == 302
    atual = PedidoOnline.query.filter_by(codigo='DDDD0004').first()
    assert atual.status == 'cancelado'
    assert atual.cancelado_em is not None


def test_cancelar_pedido_entregue_nao_muda(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _owner(app)
    p = _pedido(db, codigo='EEEE0005', status='entregue')
    c.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
           follow_redirects=True)
    atual = PedidoOnline.query.filter_by(codigo='EEEE0005').first()
    assert atual.status == 'entregue'  # não cancelou


def test_nao_owner_ve_pedidos(app):
    """Liberado a TODOS os usuários logados (22/06/2026): ver lista e detalhe."""
    from app.extensions import db
    c = _admin_nao_owner(app)
    p = _pedido(db, codigo='SEE0001', nome='Cliente Teste')   # aguardando_pagamento
    r = c.get('/admin/loja-online/pedidos?status=todos')  # default e' 'pago'
    assert r.status_code == 200
    assert b'Cliente Teste' in r.data
    assert c.get(f'/admin/loja-online/pedidos/{p.codigo}').status_code == 200


def test_cancelar_bloqueia_nao_owner(app):
    """Reembolso/cancelamento mexe em dinheiro → continua exclusivo do dono."""
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        _pedido(db, codigo='CANC99', status='pago')
    c = _admin_nao_owner(app)
    r = c.post('/admin/loja-online/pedidos/CANC99/cancelar',
               follow_redirects=False)
    assert r.status_code == 403
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='CANC99').first()
        assert p.status == 'pago'   # não cancelou nem reembolsou


def test_emitir_nf_bloqueia_nao_owner(app):
    """Emissão de NF (fiscal) → continua exclusiva do dono."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='NF99', status='pago')
    c = _admin_nao_owner(app)
    r = c.post('/admin/loja-online/pedidos/NF99/emitir-nf',
               follow_redirects=False)
    assert r.status_code == 403


def test_dashboard_e_logo_owner_only(app):
    """Dashboard /admin/loja-online (mostra FATURAMENTO) e config do logo
    da loja ficam owner-only — decisao 22/06/2026: equipe nao precisa ver
    valor de venda nem mexer no logo."""
    c = _admin_nao_owner(app)
    assert c.get('/admin/loja-online').status_code == 403
    assert c.post('/admin/loja-online/logo', data={}).status_code == 403
    assert c.post('/admin/loja-online/logo/remover').status_code == 403


def test_ordenacao_e_tiny_continuam_owner_only(app):
    """Decisao do dono 22/06/2026: ordenar produtos, ordenar categorias e
    SKUs do Tiny ficam SO com ele. Pedidos do site fica liberado, mas essas
    tres funcoes de curadoria/config fiscal nao."""
    c = _admin_nao_owner(app)
    # ordem dos produtos
    assert c.get('/admin/loja-online/ordem-produtos').status_code == 403
    # ordem das categorias
    assert c.get('/admin/loja-online/categorias').status_code == 403
    # SKUs do Tiny
    assert c.get('/admin/loja-online/tiny-skus').status_code == 403
    # POSTs relacionados tambem
    assert c.post('/admin/loja-online/produtos/ordem',
                  json={'itens': []}).status_code == 403
    assert c.post('/admin/loja-online/categorias/ordem',
                  json={'categorias': []}).status_code == 403


def test_painel_pedidos_online_json_nao_owner(app):
    """Drawer do painel de entregas: lista/busca JSON liberada a qualquer
    usuário logado (mesmo público do /entregas/painel)."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='PJ0001', nome='Joana Painel')
    c = _admin_nao_owner(app)
    r = c.get('/entregas/api/painel/pedidos-online')
    assert r.status_code == 200
    assert any(p['codigo'] == 'PJ0001' for p in r.get_json()['pedidos'])
    r2 = c.get('/entregas/api/painel/pedidos-online?q=Joana')
    assert any(p['cliente'] == 'Joana Painel' for p in r2.get_json()['pedidos'])


def test_pedido_inexistente_404(app):
    c = _owner(app)
    assert c.get('/admin/loja-online/pedidos/NAOEXISTE').status_code == 404


# ── Editar pedido (logística/contato; não mexe em dinheiro) ──────────────

def _post_editar(c, codigo, **over):
    base = {
        'nome_cliente': 'Maria Souza', 'email_cliente': 'm@x.com',
        'telefone_cliente': '11988887777', 'modo_entrega': 'agendada',
        'data_entrega': '2026-06-25', 'janela_entrega': '08:00–09:00',
        'endereco_cep': '04077000', 'endereco_logradouro': 'Rua X',
        'endereco_numero': '10', 'endereco_bairro': 'Moema',
        'endereco_cidade': 'São Paulo', 'endereco_uf': 'sp',
        'cartinha': 'Feliz aniversário!',
    }
    base.update(over)
    return c.post(f'/admin/loja-online/pedidos/{codigo}/editar',
                  data=base, follow_redirects=False)


def test_editar_pedido_atualiza_logistica(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        _pedido(db, codigo='EDIT01')
    c = _owner(app)
    r = _post_editar(c, 'EDIT01', cartinha='Com carinho')
    assert r.status_code == 302
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='EDIT01').first()
        assert p.cartinha == 'Com carinho'
        assert p.modo_entrega == 'agendada'
        assert p.data_entrega.isoformat() == '2026-06-25'
        assert p.endereco_uf == 'SP'              # normalizado maiúsculo
        assert p.endereco_cidade == 'São Paulo'
        assert 'Rua X' in p.endereco_entrega      # snapshot reconstruído


def test_editar_nao_altera_dinheiro(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        p = _pedido(db, codigo='EDIT02')
        antes_total, antes_sub = p.valor_total, p.subtotal
    c = _owner(app)
    _post_editar(c, 'EDIT02', cartinha='x')
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='EDIT02').first()
        assert p.valor_total == antes_total
        assert p.subtotal == antes_sub


def test_editar_data_invalida_nao_salva(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        _pedido(db, codigo='EDIT03', nome='Orig')
    c = _owner(app)
    r = _post_editar(c, 'EDIT03', data_entrega='32/13/2026',
                     nome_cliente='NovoNome')
    assert r.status_code == 302   # volta pro detalhe com flash de erro
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='EDIT03').first()
        assert p.nome_cliente == 'Orig'   # abortou antes de salvar


def test_editar_retirada_seta_loja(app):
    from app.extensions import db
    from app.models import Loja, PedidoOnline
    with app.app_context():
        loja = Loja(nome='Anesio', endereco='Rua A', ativa=True)
        db.session.add(loja)
        db.session.commit()
        loja_id = loja.id
        _pedido(db, codigo='EDIT04')
    c = _owner(app)
    r = _post_editar(c, 'EDIT04', modo_entrega='retirada',
                     loja_retirada_id=str(loja_id))
    assert r.status_code == 302
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='EDIT04').first()
        assert p.modo_entrega == 'retirada'
        assert p.loja_retirada_id == loja_id


def test_imprimir_pedido_pdf(app):
    """Impressão reusa o gerador do /entregas — devolve PDF (cliente+motoboy)."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='PRINT01')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos/PRINT01/imprimir.pdf')
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'
    assert r.data[:4] == b'%PDF'


def test_editar_nao_owner_agora_permitido(app):
    """Editar logística/contato (não mexe em dinheiro) liberado a todos."""
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        _pedido(db, codigo='EDIT05', nome='Maria')
    c = _admin_nao_owner(app)
    r = _post_editar(c, 'EDIT05')
    assert r.status_code == 302
    with app.app_context():
        p = PedidoOnline.query.filter_by(codigo='EDIT05').first()
        assert p.nome_cliente == 'Maria Souza'   # editou (liberado)


# ── Filtro por DATA de entrega + impressão da seleção ───────────────────

def _pedido_com_data(db, codigo, data_iso, status='pago'):
    from datetime import date as _date
    p = _pedido(db, codigo=codigo, status=status)
    p.data_entrega = _date.fromisoformat(data_iso)
    db.session.commit()
    return p


def test_filtro_data_unica_filtra_lista(app):
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'DT01', '2026-06-25')
        _pedido_com_data(db, 'DT02', '2026-06-26')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos?data=2026-06-25')
    assert r.status_code == 200
    assert b'DT01' in r.data
    assert b'DT02' not in r.data


def test_filtro_intervalo_de_datas(app):
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'INT01', '2026-06-20')   # antes do intervalo
        _pedido_com_data(db, 'INT02', '2026-06-25')   # dentro
        _pedido_com_data(db, 'INT03', '2026-06-30')   # depois
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos'
              '?data_ini=2026-06-23&data_fim=2026-06-27')
    assert r.status_code == 200
    assert b'INT02' in r.data
    assert b'INT01' not in r.data
    assert b'INT03' not in r.data


def test_busca_respeita_filtro_de_data_ativo(app):
    """Sem isso, digitar no busca traria pedidos de outros dias e
    atropelaria o filtro de data — confunde a operação."""
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'BD01', '2026-06-25', status='pago')
        _pedido_com_data(db, 'BD02', '2026-06-26', status='pago')
        # ambos têm o mesmo cliente 'Maria' (default do _pedido) →
        # buscar 'mar' bateria nos dois sem filtro de data.
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=mar&data=2026-06-25')
    assert r.status_code == 200
    assert b'BD01' in r.data
    assert b'BD02' not in r.data


def test_imprimir_selecao_gera_pdf_multipaginas(app):
    """Seleção de N pedidos × 2 vias (cliente + motoboy) = 2N páginas."""
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'SEL01', '2026-06-25')
        _pedido_com_data(db, 'SEL02', '2026-06-25')
        _pedido_com_data(db, 'SEL03', '2026-06-25')   # NÃO selecionado
    c = _owner(app)
    r = c.post('/admin/loja-online/pedidos/imprimir-selecao.pdf',
               data={'codigos': ['SEL01', 'SEL02']})
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'
    assert r.data[:4] == b'%PDF'
    # 2 pedidos × 2 vias = 4 páginas. fpdf2 grava '/Count 4' no PDF.
    assert b'/Count 4' in r.data


def test_imprimir_selecao_vazia_redireciona(app):
    c = _owner(app)
    r = c.post('/admin/loja-online/pedidos/imprimir-selecao.pdf',
               data={}, follow_redirects=False)
    assert r.status_code == 302   # volta com flash de aviso


def test_imprimir_selecao_nao_owner_ok(app):
    """Impressão da seleção liberada a todos (22/06/2026) — antes owner-only."""
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'SEL10', '2026-06-25')
    c = _admin_nao_owner(app)
    r = c.post('/admin/loja-online/pedidos/imprimir-selecao.pdf',
               data={'codigos': ['SEL10']}, follow_redirects=False)
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'


def test_lista_tem_checkbox_e_form_de_selecao(app):
    """Travas de UI: as linhas têm checkbox `.chk-pedido` apontando pro form
    externo, e o botão 'Imprimir seleção' existe na página."""
    from app.extensions import db
    with app.app_context():
        _pedido_com_data(db, 'UI01', '2026-06-25')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos')
    assert r.status_code == 200
    assert b'chk-pedido' in r.data
    assert b'id="form-imprimir-selecao"' in r.data
    assert b'Imprimir sele' in r.data   # 'Imprimir seleção' (sem depender do acento UTF-8)


# ── Popup do painel: modo embed + visibilidade das ações por papel ──────────

def test_detalhe_embed_esconde_sidebar(app):
    """Modo embed (iframe do popup do painel): sem sidebar; o form leva o flag
    adiante pra o redirect pós-POST manter a janela limpa."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='EMB001')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos/EMB001?embed=1')
    assert r.status_code == 200
    assert b'class="embed-mode"' in r.data
    assert b'id="sidebar"' not in r.data
    assert b'name="embed" value="1"' in r.data


def test_detalhe_nao_owner_esconde_acoes_de_dinheiro(app):
    """Não-owner vê o pedido e edita logística, mas NÃO vê reembolso/NF."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='HIDE01', status='pago')
    c = _admin_nao_owner(app)
    r = c.get('/admin/loja-online/pedidos/HIDE01')
    assert r.status_code == 200
    assert 'Reembolsar e cancelar'.encode() not in r.data
    assert b'Emitir NF' not in r.data
    assert 'Salvar altera'.encode() in r.data   # edição continua disponível


def test_detalhe_embed_permite_iframe_same_origin(app):
    """Popup do painel: o detalhe em ?embed=1 pode ser embutido em iframe
    same-origin (SAMEORIGIN + frame-ancestors 'self'); sem embed segue DENY."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='FRM001')
    c = _owner(app)
    r_embed = c.get('/admin/loja-online/pedidos/FRM001?embed=1')
    assert r_embed.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in r_embed.headers.get(
        'Content-Security-Policy', '')
    r_normal = c.get('/admin/loja-online/pedidos/FRM001')
    assert r_normal.headers.get('X-Frame-Options') == 'DENY'


def test_detalhe_owner_pago_ve_acoes(app):
    """Owner (status pago) continua vendo reembolso e emissão de NF."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, codigo='OWN01', status='pago')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos/OWN01')
    assert r.status_code == 200
    assert 'Reembolsar e cancelar'.encode() in r.data
    assert b'Emitir NF' in r.data
