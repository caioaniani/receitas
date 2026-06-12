"""Imprimir entregas: tela /entregas/ e /entregas/painel agora abrem pra
todo usuario logado (decisao 11/06/2026); /entregas/imprimir gera 1 folha
A4 por pedido por via; via do entregador omite valores e cartinha."""
from unittest.mock import patch


def _login(c, login='admin', senha='123'):
    c.post('/auth/login', data={'login': login, 'senha': senha})


def _user(app, papel='funcionario', login='func1', loja_id=None):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login=login, papel=papel, loja_id=loja_id)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()
        return u.id


def _pedidos_fake():
    """Forma REAL do pedido VNDA (vnda.py::_normalizar_pedido): dinheiro
    fica em `total` (pedido) e `preco_unitario`/`subtotal` (item).
    Bug real (11/06/2026): os fakes antigos usavam valor_total/
    valor_unitario — nomes que NUNCA existiram nos dados reais — e o
    template lia esses nomes: testes verdes, prod imprimindo R$ 0,00."""
    return [
        {'code': 'VND-1', 'destinatario': 'Ana', 'endereco': 'Rua A, 1',
         'telefone': '11 99999-1111', 'periodo': '10h-12h', 'expresso': False,
         'cartinha_vnda': 'Feliz aniversario!', 'observacao': 'portaria 1',
         'total': 360.0,
         'itens': [{'nome': 'Cesta Bonjour', 'quantidade': 1,
                    'preco_unitario': 215, 'subtotal': 215},
                   {'nome': 'Croissant Tradicional', 'quantidade': 5,
                    'preco_unitario': 29, 'subtotal': 145}]},
        {'code': 'VND-2', 'destinatario': 'Bruno', 'endereco': 'Rua B, 2',
         'telefone': '', 'expresso': True, 'observacao': '',
         'total': 50.0,
         'itens': [{'nome': 'Sourdough Tradicional', 'quantidade': 1,
                    'subtotal': 50}]},
    ]


def _mock_carregamento():
    """Patcha as 3 chamadas que /imprimir faz pro VNDA/cartinhas/drivers."""
    return [
        patch('app.blueprints.entregas.routes.vnda.buscar_pedidos_do_dia',
              return_value={'pedidos': _pedidos_fake()}),
        patch('app.blueprints.entregas.routes._injetar_pedidos_locais',
              side_effect=lambda target, res: res),
        patch('app.blueprints.entregas.routes._carregar_overrides_full',
              return_value={}),
    ]


def test_botao_imprimir_existe_no_topo_da_tela(app, admin_user):
    """Independente da aba ativa (Operacao/legado), o botao 'imprimir
    selecionados' fica no topo e consome os checkboxes existentes
    (op-check da Operacao OU chk-imprimir do legado)."""
    c = app.test_client()
    _login(c)
    r = c.get('/entregas/')
    body = r.data
    assert b'btn-imprimir-sel' in body
    assert b'imprimir selecionados' in body
    assert b'modalImprimirVias' in body
    assert b'sel-imprimir-info' in body


def test_js_le_op_check_e_chk_imprimir(app):
    """codesSelecionados() considera os dois seletores — nao quebra quando
    o usuario esta na aba Operacao (que ja desenha op-check)."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert '.op-check:checked, .chk-imprimir:checked' in js


def test_js_envia_dados_completos_via_post(app):
    """Bug real (11/06/2026): mandar so codes via GET fazia o servidor
    rebuscar do VNDA pela data. Quando a data nao bate exato (override,
    cache, polling re-renderiza entre marcacao e clique) volta vazio.
    Agora o JS coleta os dados dos pedidos do estado em memoria
    (opUltimoResultado) e manda via POST — servidor nao precisa do VNDA."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert 'pedidosSnapshot' in js
    assert "addInput('pedidos_json'" in js
    assert "f.method = 'POST'" in js




def _post_imprimir(c, pedidos, vias='cliente', data='2026-06-11',
                   follow_redirects=True):
    """POST /entregas/imprimir com os dados dos pedidos (caminho default
    do JS). Com payload valido o servidor responde 303 pro GET ?lote=
    (Post/Redirect/Get — Safari nao re-busca POST na impressao e saia
    tudo em branco); por default seguimos o redirect ate a pagina."""
    import json as _j
    return c.post('/entregas/imprimir', data={
        'pedidos_json': _j.dumps(pedidos),
        'vias': vias, 'data': data,
    }, follow_redirects=follow_redirects)

def test_funcionario_sem_loja_acessa_entregas_e_painel(app):
    """Decisao 11/06/2026: /entregas/ e /entregas/painel sao pra TODOS os
    usuarios logados — antes funcionario sem loja_id levava 403."""
    uid = _user(app, papel='funcionario', loja_id=None)
    c = app.test_client()
    _login(c, login='func1', senha='senha123')
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert c.get('/entregas/').status_code == 200
    assert c.get('/entregas/painel').status_code == 200


def test_imprimir_renderiza_via_cliente_com_cartinha_e_valor(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&vias=cliente'
                  '&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data
    assert b'via do cliente' in body
    assert b'via do entregador' not in body
    assert 'Ana'.encode() in body
    assert 'Feliz aniversario!'.encode() in body          # cartinha
    assert 'R$ 360,00'.encode() in body                    # total
    assert 'R$ 215,00'.encode() in body                    # valor por item
    assert b'page-break-after: always' in body


def test_imprimir_via_motorista_omite_valores_e_cartinha(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&vias=motorista'
                  '&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data
    assert b'via do entregador' in body
    assert b'via do cliente' not in body
    # entregador precisa: destinatario, endereco, telefone, itens, janela
    assert 'Ana'.encode() in body
    assert b'Rua A, 1' in body
    assert b'99999-1111' in body
    assert 'Cesta Bonjour'.encode() in body
    # NAO precisa: cartinha, valor item, total, driver atribuido
    assert 'Feliz aniversario!'.encode() not in body
    assert b'R$ 215' not in body
    assert b'R$ 360' not in body
    # tem campo de conferencia (assinatura)
    assert b'assinatura' in body


def test_imprimir_duas_vias_gera_duas_folhas_por_pedido(app, admin_user):
    """codes=VND-1,VND-2 + vias=cliente,motorista → 4 page-breaks (1
    sobra como last-child sem break, mas ainda 4 folhas no DOM)."""
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1,VND-2'
                  '&vias=cliente,motorista&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    body = r.data.decode()
    assert body.count('class="folha"') == 4   # 2 pedidos × 2 vias
    # ordem: cliente VND-1, motorista VND-1, cliente VND-2, motorista VND-2
    i_a_cli = body.index('VND-1') < body.index('VND-2')
    assert i_a_cli   # pedido 1 antes do 2
    # expresso aparece destacado pro motorista
    assert 'EXPRESSO' in body


def test_imprimir_codes_invalido_devolve_pagina_vazia(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=NAO-EXISTE&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    assert 'Nenhum pedido selecionado'.encode() in r.data


def test_imprimir_aguenta_quantidade_none(app, admin_user):
    """Bug real (11/06/2026): selecionei TODOS e a impressao deu 500. Causa:
    algum item tem quantidade=None — sum(attribute=) do Jinja explode.
    O template agora soma defensivo (None→0).

    Este teste tambem cobre a COMPAT com os nomes legados valor_total/
    valor_unitario (pedidos locais antigos podem te-los): o template cai
    neles quando total/subtotal/preco_unitario nao existem."""
    c = app.test_client()
    _login(c)

    pedido_quebrado = {
        'code': 'VND-Q', 'destinatario': 'Z', 'endereco': 'Rua Q',
        'telefone': '', 'periodo': '', 'expresso': False,
        'valor_total': 100.0,
        'itens': [
            {'nome': 'Item ok', 'quantidade': 2, 'valor_total': 60},
            {'nome': 'Item sem qtd', 'quantidade': None,
             'valor_total': 40},
            {'nome': 'Item sem valor', 'quantidade': 1,
             'valor_total': None, 'valor_unitario': None},
        ],
    }
    mocks = [
        patch('app.blueprints.entregas.routes.vnda.buscar_pedidos_do_dia',
              return_value={'pedidos': [pedido_quebrado]}),
        patch('app.blueprints.entregas.routes._injetar_pedidos_locais',
              side_effect=lambda target, res: res),
        patch('app.blueprints.entregas.routes._carregar_overrides_full',
              return_value={}),
    ]
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-Q&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Item ok' in body
    assert 'Item sem qtd' in body
    # contagem deve somar so o que existe (2 + 0 + 1 = 3) — sem TypeError
    assert '(3 itens)' in body
    # compat: campos legados valor_total/valor_unitario ainda renderizam
    assert 'R$ 100,00' in body    # total do pedido (fallback valor_total)
    assert 'R$ 60,00' in body     # item (fallback it.valor_total)


def test_js_escuta_op_check_secao_pra_selecionar_todos(app):
    """Bug real (11/06/2026): 'selecionar todos da secao' nao habilitava o
    botao. Causa: op-check-secao seta .checked nos filhos por atribuicao
    direta, sem disparar 'change'. Listener agora escuta op-check-secao
    explicitamente + click como recurso final."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert "'op-check-secao'" in js
    assert 'op-check-secao' in js and 'atualizarBarraImprimir' in js


def test_botao_antigo_imprimir_agora_usa_modal(app, admin_user):
    """Bug real (11/06/2026): o botao 'imprimir' da barra de acoes da aba
    Operacao chamava window.print() puro, que ignorava a selecao e tentava
    imprimir a tela inteira. Agora delega pra abrirModalImprimir() — mesmo
    fluxo do botao 'imprimir selecionados' do topo."""
    c = app.test_client()
    _login(c)
    body = c.get('/entregas/').data
    # Nao pode mais ter o handler antigo no botao da aba Operacao
    assert b'onclick="abrirModalImprimir()"' in body


def test_imprimir_default_via_cliente(app, admin_user):
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-1&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    body = r.data
    assert b'via do cliente' in body
    assert b'via do entregador' not in body


def test_js_le_opUltimoResultado_da_iife_nao_window(app):
    """Bug real (11/06/2026): pedidoDoEstado lia window.opUltimoResultado
    (sempre undefined) em vez da variavel local da IIFE. Resultado:
    snapshot vazio e alerta 'Marque ao menos um pedido antes' mesmo com
    checkbox marcado e contador mostrando '1 selecionado(s)'."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    # Pra ignorar a mencao em comentario explicativo, exige que SE a
    # palavra aparece, esteja so dentro de comentario.
    linhas_codigo = [linha for linha in js.splitlines()
                     if 'window.opUltimoResultado' in linha
                     and not linha.lstrip().startswith('//')]
    assert not linhas_codigo, f'uso em codigo: {linhas_codigo!r}'
    assert "typeof opUltimoResultado !== 'undefined'" in js


def test_js_form_target_nomeado_nao_blank(app):
    """Bug real (11/06/2026): a aba aberta ficava em 'about:blank' porque
    window.open(_blank) e form.target='_blank' criam abas DIFERENTES.
    Com nome unico, o submit navega a mesma janela aberta."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    # nao pode mais ter '_blank' literal no fluxo de impressao
    assert "f.target = '_blank'" not in js
    assert "f.target = w ? '_blank' : '_self'" not in js
    # tem que ter nome unico (Date.now) e abertura prelimar
    assert 'imprimir-' in js
    assert "window.open('', nomeAba)" in js
    assert 'f.target = nomeAba' in js


def test_js_csrf_token_nao_usa_window(app):
    """Bug real (11/06/2026): addInput('csrf_token', window.CSRF_TOKEN || '')
    mandava string vazia porque base.html declara `const CSRF_TOKEN = "..."`
    (top-level binding, NAO em window). Flask-WTF entao rejeitava o POST com
    400 'The CSRF token is missing.' Corrigido com typeof check, igual ao
    padrao usado em todas as outras chamadas do mesmo arquivo."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    # nenhum uso de window.CSRF_TOKEN em codigo (so em comentario explicativo)
    linhas_codigo = [linha for linha in js.splitlines()
                     if 'window.CSRF_TOKEN' in linha
                     and not linha.lstrip().startswith('//')]
    assert not linhas_codigo, f'uso em codigo: {linhas_codigo!r}'
    # tem que usar o padrao typeof
    assert "typeof CSRF_TOKEN !== 'undefined' ? CSRF_TOKEN : ''" in js


def test_imprimir_post_aceita_csrf_token_valido(app, admin_user):
    """Smoke do POST /entregas/imprimir com CSRF token valido — garante que
    o endpoint nao rejeita o caminho real (form HTML com csrf_token oculto)."""
    import json
    import re
    c = app.test_client()
    # Login antes de ligar CSRF — login com CSRF on exigiria token tambem
    _login(c)
    # Liga CSRF SO pra exercitar o POST /imprimir
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        # Pega um token CSRF valido da pagina /entregas/
        r = c.get('/entregas/')
        m = re.search(rb'const CSRF_TOKEN = "([^"]+)"', r.data)
        assert m, 'CSRF_TOKEN nao foi renderizado no base.html'
        token = m.group(1).decode()
        # POST com o token correto vai ate o handler (e segue o 303 do
        # PRG ate a pagina final)
        r = c.post('/entregas/imprimir', data={
            'csrf_token': token,
            'pedidos_json': json.dumps(_pedidos_fake()),
            'vias': 'cliente',
            'data': '2026-06-11',
        }, follow_redirects=True)
        assert r.status_code == 200, (
            f'POST com CSRF valido deu {r.status_code} '
            f'(corpo: {r.data[:200]!r})'
        )
        assert b'via do cliente' in r.data
        # Sem token = 400 (prova que CSRF estava de fato ativo)
        r2 = c.post('/entregas/imprimir', data={
            'pedidos_json': json.dumps(_pedidos_fake()),
            'vias': 'cliente', 'data': '2026-06-11',
        })
        assert r2.status_code == 400
    finally:
        # Defensivo — conftest tambem restaura, mas explicito nao fere.
        app.config['WTF_CSRF_ENABLED'] = False


def test_post_renderiza_valores_reais_do_vnda(app, admin_user):
    """Bug real (11/06/2026): o template lia valor_total/valor_unitario —
    campos que so existiam nos fakes de teste. Pedido REAL do VNDA usa
    total/preco_unitario/subtotal → via do cliente imprimia R$ 0,00 em
    TUDO. Este teste POSTa a forma real e exige os valores certos."""
    c = app.test_client()
    _login(c)
    r = _post_imprimir(c, _pedidos_fake(), vias='cliente')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'R$ 360,00' in body     # p.total
    assert 'R$ 215,00' in body     # it.subtotal
    assert 'R$ 145,00' in body     # it.subtotal (2o item)
    assert 'R$ 0,00' not in body   # nada zerado na via do cliente


def test_js_snapshot_busca_nas_duas_abas(app):
    """Bug real (11/06/2026): pedidoDoEstado so olhava opUltimoResultado
    (aba Operacao). Quem marcava checkbox na aba legada 'Pedidos do Dia'
    (array `pedidos` da IIFE) — ou quando /api/atribuidos nem carregou —
    caia em snapshot vazio e a impressao morria."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert 'fontes.push(pedidos)' in js          # aba legada
    assert 'estado.sem_driver' in js             # aba Operacao
    assert 'dr.paradas' in js


def test_js_fallback_get_quando_snapshot_vazio(app):
    """Quando nenhum estado em memoria contem os codes marcados, o JS nao
    pode morrer num alert: cai no GET legado (servidor rebusca do VNDA) e,
    no pior caso, a pagina mostra o bloco de diagnostico."""
    import pathlib
    js = pathlib.Path('app/static/js/entregas.js').read_text()
    assert "window.open('/entregas/imprimir?' + qs, nomeAba)" in js
    assert 'codesSnapshotImpr' in js


def test_imprimir_post_vazio_mostra_diagnostico(app, admin_user):
    """POST que resulta em 0 pedidos mostra o bloco de diagnostico na
    pagina (3 bugs as cegas nessa tela ja; o proximo print do usuario
    tem que dizer o porque sozinho)."""
    c = app.test_client()
    _login(c)
    r = _post_imprimir(c, [], vias='cliente,motorista')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Nenhum pedido selecionado' in body
    assert 'diagn' in body                     # bloco diagnostico
    assert 'lista vazia' in body               # problema especifico
    assert 'metodo: POST' in body


def test_imprimir_get_diag_codes_sem_match(app, admin_user):
    """GET legado com codes que nao batem com a data: diagnostico lista
    os codes ausentes — distingue 'data errada' de 'VNDA fora'."""
    c = app.test_client()
    _login(c)
    mocks = _mock_carregamento()
    [m.start() for m in mocks]
    try:
        r = c.get('/entregas/imprimir?codes=VND-FANTASMA&data=2026-06-11')
    finally:
        [m.stop() for m in mocks]
    body = r.data.decode()
    assert 'Nenhum pedido selecionado' in body
    assert 'VND-FANTASMA' in body              # code sem match aparece
    assert 'codes_sem_match_na_data' in body


def test_post_redireciona_pra_get_lote(app, admin_user):
    """Bug real (11/06/2026, 2o print do dono): pagina resultado de POST
    imprimia TODA EM BRANCO no Safari — o print re-busca o documento e nao
    reenvia POST. Agora o POST persiste o payload (ImpressaoLote) e
    devolve 303 pro GET ?lote=<token>; o documento impresso e GET."""
    c = app.test_client()
    _login(c)
    r = _post_imprimir(c, _pedidos_fake(), vias='cliente,motorista',
                       follow_redirects=False)
    assert r.status_code == 303
    assert 'lote=' in r.headers['Location']
    assert 'vias=cliente%2Cmotorista' in r.headers['Location'] \
        or 'vias=cliente,motorista' in r.headers['Location']
    # O GET do redirect renderiza o conteudo completo, sem tocar o VNDA
    r2 = c.get(r.headers['Location'])
    assert r2.status_code == 200
    body = r2.data.decode()
    assert 'Ana' in body
    assert body.count('class="folha"') == 4   # 2 pedidos x 2 vias
    # Recarregar a MESMA URL funciona (era o ponto: documento re-buscavel)
    r3 = c.get(r.headers['Location'])
    assert 'Ana' in r3.data.decode()


def test_get_lote_expirado_mostra_diagnostico(app, admin_user):
    c = app.test_client()
    _login(c)
    r = c.get('/entregas/imprimir?lote=nao-existe-mais')
    assert r.status_code == 200
    body = r.data.decode()
    assert 'Nenhum pedido selecionado' in body
    assert 'expirado' in body


def test_post_limpa_lotes_velhos(app, admin_user):
    """Lotes de impressao sao efemeros: cada POST novo varre os com mais
    de 2 dias (a tabela nao pode crescer pra sempre)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import ImpressaoLote
    from app.utils import agora
    velho = ImpressaoLote(token='tok-velho', payload='[]',
                          criado_em=agora() - timedelta(days=3))
    db.session.add(velho)
    db.session.commit()
    c = app.test_client()
    _login(c)
    _post_imprimir(c, _pedidos_fake(), follow_redirects=False)
    assert ImpressaoLote.query.filter_by(token='tok-velho').first() is None
    assert ImpressaoLote.query.count() == 1   # so o lote recem-criado


def test_css_da_folha_e_bloco_simples(app, admin_user):
    """Decisao 11/06/2026 (apos 3 rodadas de bug de impressao no
    Safari): a pagina HTML e SO preview de tela — a impressao oficial e
    o PDF do servidor. O CSS da .folha volta ao bloco simples (que
    paginava certo na tela): sem flex, sem min-height, sem height fixo,
    sem overflow hidden."""
    c = app.test_client()
    _login(c)
    r = _post_imprimir(c, _pedidos_fake(), vias='cliente')
    body = r.data.decode()
    import re
    m = re.search(r'\.folha\s*\{([^}]*)\}', body)
    assert m, 'bloco .folha sumiu do CSS'
    bloco = m.group(1)
    assert 'flex' not in bloco, f'flex voltou: {bloco!r}'
    assert 'min-height' not in bloco, f'min-height voltou: {bloco!r}'
    assert 'height' not in bloco, f'height fixo voltou: {bloco!r}'
    assert 'overflow' not in bloco, f'overflow voltou: {bloco!r}'
    assert 'page-break-after: always' in bloco
    assert 'break-after: page' in bloco


def test_pagina_html_aponta_pro_pdf(app, admin_user):
    """O botao verde da barra agora abre o PDF do servidor (impressao
    congelada) em vez de chamar window.print() — que re-renderizava o
    documento no Safari e imprimia em branco."""
    c = app.test_client()
    _login(c)
    r = _post_imprimir(c, _pedidos_fake(), vias='cliente')
    body = r.data.decode()
    assert '/entregas/imprimir.pdf?' in body
    assert 'imprimir (PDF)' in body
    # o window.print() nao pode mais ser o caminho do botao
    assert 'onclick="window.print()"' not in body


def test_imprimir_15_pedidos_geram_exatamente_30_folhas(app, admin_user):
    """15 pedidos x 2 vias = EXATAMENTE 30 .folha no DOM do preview."""
    c = app.test_client()
    _login(c)
    pedidos = [
        {'code': f'V{i}', 'destinatario': f'Cliente {i}',
         'endereco': 'Rua X', 'telefone': '', 'total': 100,
         'itens': [{'nome': 'Item', 'quantidade': 1, 'subtotal': 100}]}
        for i in range(15)
    ]
    r = _post_imprimir(c, pedidos, vias='cliente,motorista')
    body = r.data.decode()
    assert body.count('class="folha"') == 30   # 15 x 2 vias, sem sobra


def test_pedido_so_com_code_nao_quebra_render(app, admin_user):
    """Pedido vazio (so com code) ainda renderiza .folha com cabecalho —
    nao pode virar div em branco que confunde paginacao do Safari."""
    c = app.test_client()
    _login(c)
    pedido = {'code': 'VND-VAZIO'}
    r = _post_imprimir(c, [pedido], vias='cliente,motorista')
    body = r.data.decode()
    assert body.count('class="folha"') == 2
    # cabecalho sempre presente
    assert 'Pedido #VND-VAZIO' in body
    assert 'via do cliente' in body
    assert 'via do entregador' in body


def test_debug_lote_retorna_diag_por_pedido(app, admin_user):
    """A rota /imprimir/debug/<token> mostra shape e tamanhos por
    pedido — pra diagnosticar lote especifico sem chutar."""
    c = app.test_client()
    _login(c)
    pedidos = [
        {'code': 'A1', 'destinatario': 'Ana', 'endereco': 'Rua A, 1',
         'telefone': '11 99999-1111', 'periodo': '10h-12h',
         'cartinha_vnda': 'oi', 'observacao': 'portaria',
         'total': 100, 'expresso': False,
         'itens': [{'nome': 'X', 'quantidade': 1, 'subtotal': 100}]},
    ]
    rpost = _post_imprimir(c, pedidos, follow_redirects=False)
    import re
    m = re.search(r'lote=([^&]+)', rpost.headers['Location'])
    token = m.group(1)
    r = c.get(f'/entregas/imprimir/debug/{token}')
    assert r.status_code == 200
    data = r.get_json()
    assert data['qtd_pedidos'] == 1
    assert data['payload_bytes'] > 0
    d0 = data['diag'][0]
    assert d0['code'] == 'A1'
    assert d0['destinatario'] is True
    assert d0['endereco_len'] == len('Rua A, 1')
    # cartinha_len no debug reflete a cartinha_vnda do payload ORIGINAL,
    # nao a cartinha resolvida (resolucao acontece no render, nao no
    # payload guardado).
    assert d0['qtd_itens'] == 1


def test_debug_lote_recusa_funcionario(app):
    """Funcionario comum NAO pode ver o debug — payload tem cartinha,
    telefone, endereco e e dado sensivel."""
    uid = _user(app, papel='funcionario', login='funcdbg',
                loja_id=None)
    c = app.test_client()
    _login(c, login='funcdbg', senha='senha123')
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    r = c.get('/entregas/imprimir/debug/qualquer-token')
    assert r.status_code == 403


def test_debug_lote_inexistente_devolve_404(app, admin_user):
    c = app.test_client()
    _login(c)
    r = c.get('/entregas/imprimir/debug/nao-existe')
    assert r.status_code == 404


# ── PDF: o caminho OFICIAL de impressao (11/06/2026) ────────────────────


def test_pdf_do_lote_retorna_pdf_valido(app, admin_user):
    """POST → 303 (lote) → GET imprimir.pdf?lote= devolve um PDF de
    verdade. E o documento congelado que o Safari imprime sem
    re-renderizar (a causa raiz das 3 rodadas de pagina em branco)."""
    c = app.test_client()
    _login(c)
    rpost = _post_imprimir(c, _pedidos_fake(), vias='cliente,motorista',
                           follow_redirects=False)
    import re
    m = re.search(r'lote=([^&]+)', rpost.headers['Location'])
    token = m.group(1)
    r = c.get(f'/entregas/imprimir.pdf?lote={token}'
              '&vias=cliente,motorista&data=2026-06-11')
    assert r.status_code == 200
    assert r.mimetype == 'application/pdf'
    assert r.data[:5] == b'%PDF-'
    assert 'inline' in r.headers.get('Content-Disposition', '')


def test_pdf_lote_expirado_devolve_pagina_diagnostico(app, admin_user):
    """PDF de lote inexistente nao pode ser PDF vazio mudo — devolve a
    pagina HTML com o bloco de diagnostico."""
    c = app.test_client()
    _login(c)
    r = c.get('/entregas/imprimir.pdf?lote=sumiu&vias=cliente')
    assert r.status_code == 200
    assert r.mimetype == 'text/html'
    assert 'expirado'.encode() in r.data


def test_pdf_exige_login(app, admin_user):
    c = app.test_client()
    r = c.get('/entregas/imprimir.pdf?lote=x', follow_redirects=False)
    assert r.status_code in (302, 401)


def test_apis_leitura_da_operacao_abertas_pra_funcionario(app):
    """A aba Operacao do /entregas/ chama /api/atribuidos, /api/lotes e
    /api/rotas. Como a tela abre pra todos (decisao 11/06/2026), os GETs
    read-only nao podem dar 403 pra funcionario — senao a aba fica vazia
    e a impressao morre com snapshot vazio."""
    uid = _user(app, papel='funcionario', loja_id=None)
    c = app.test_client()
    _login(c, login='func1', senha='senha123')
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    mocks = [
        patch('app.blueprints.entregas.routes.vnda.buscar_pedidos_do_dia',
              return_value={'pedidos': []}),
        patch('app.blueprints.entregas.routes._injetar_pedidos_locais',
              side_effect=lambda target, res: res),
    ]
    [m.start() for m in mocks]
    try:
        assert c.get('/entregas/api/atribuidos?data=2026-06-11') \
            .status_code == 200
        assert c.get('/entregas/api/lotes?data=2026-06-11') \
            .status_code == 200
        assert c.get('/entregas/api/rotas?data=2026-06-11') \
            .status_code == 200
    finally:
        [m.stop() for m in mocks]


def test_writes_de_atribuicao_continuam_guardados(app):
    """Abrir os reads NAO abre os writes: funcionario sem loja segue 403
    em POST de atribuicao (mexe na operacao de entrega)."""
    uid = _user(app, papel='funcionario', login='func2', loja_id=None)
    c = app.test_client()
    _login(c, login='func2', senha='senha123')
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    r = c.post('/entregas/api/atribuicao/VND-1', json={'driver_id': 1})
    assert r.status_code == 403
