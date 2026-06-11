"""Tool consultar_cartinhas no copilot_svc — herdada automaticamente pelo
bot WhatsApp do dono (zapi_bot, copilot read-only) e pelo Slack."""
from app.extensions import db


def test_tool_consultar_cartinhas_registrada_e_executa(app, admin_user):
    from app.models import CartinhaEntrega, Usuario
    from app.services import copilot as cs
    from app.utils import agora
    uid = admin_user.id
    with app.app_context():
        db.session.add(CartinhaEntrega(pedido_code='VND-AAA',
                                       texto='Feliz dia!',
                                       atualizado_em=agora(),
                                       atualizado_por=uid))
        db.session.commit()
        assert 'consultar_cartinhas' in [t['name'] for t in cs.TOOLS]
        assert cs.PAPEIS_POR_TOOL['consultar_cartinhas'] == {'admin', 'gerente'}
        u = Usuario.query.get(uid)
        r = cs._executar_read('consultar_cartinhas', {'dias': 2}, u)
        assert 'VND-AAA' in r['texto'] and r['total'] == 1


def test_cartinhas_e_read_tool_visivel_no_modo_leitura(app, admin_user):
    """REQUER_APROVACAO nao contem a tool — o bot do dono (apenas_leitura)
    consegue usa-la."""
    from app.services import copilot as cs
    assert 'consultar_cartinhas' not in cs.REQUER_APROVACAO


def test_modo_leitura_nao_lista_writes_no_system_prompt(app, admin_user):
    """Caso real (bot WhatsApp 11/06/2026): o system prompt listava
    criar_tarefa (write) mesmo com o filtro apenas_leitura — o bot prometia
    'tenho a tool criar_tarefa' sem conseguir invoca-la. Prompt agora lista
    exatamente o que a API recebe + aviso de modo leitura."""
    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        todas = cs.tools_permitidas(u)
        so_leitura = [t for t in todas
                      if t['name'] not in cs.REQUER_APROVACAO]

        def linha_tools(prompt):
            return next(ln for ln in prompt.splitlines()
                        if ln.startswith('TOOLS QUE ESTE USUARIO'))

        normal = cs._build_system_prompt(u, tools_visiveis=todas)
        assert 'criar_tarefa' in linha_tools(normal)
        assert 'MODO SOMENTE LEITURA' not in normal

        leitura = cs._build_system_prompt(u, tools_visiveis=so_leitura,
                                          apenas_leitura=True)
        # a LISTA anunciada nao contem writes (mencoes em texto de regra
        # estatica sao inofensivas; a promessa vinha da lista)
        assert 'criar_tarefa' not in linha_tools(leitura)
        assert 'ajuste_estoque' not in linha_tools(leitura)
        assert 'consultar_cartinhas' in linha_tools(leitura)
        assert 'MODO SOMENTE LEITURA' in leitura


def test_prompt_tem_regra_anti_amnesia(app, admin_user):
    """Bug real (zapi_bot, 11/06/2026): o usuario disse 'me manda o link aqui'
    e o bot alucinou 'cada sessao comeca do zero pra mim', mesmo recebendo
    historico de 80 turnos. Causa: prompt nao mencionava memoria — Claude
    assumiu que era turno isolado. Fix: bloco MEMORIA com proibicao explicita
    das frases-mentira."""
    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        for kwargs in ({}, {'apenas_leitura': True,
                            'tools_visiveis': [t for t in cs.tools_permitidas(u)
                                               if t['name'] not in cs.REQUER_APROVACAO]}):
            p = cs._build_system_prompt(u, **kwargs)
            assert 'MEMORIA' in p
            assert 'historico completo desta conversa' in p
            # frases-mentira proibidas no prompt (em modo positivo: o prompt
            # PROIBE elas; sao exibidas como negativa)
            assert 'cada sessao comeca do zero' in p
            assert 'NUNCA diga' in p


def test_consultar_catalogo_site_devolve_url_da_pagina(app, admin_user):
    """Caso real (bot WhatsApp do dono, 11/06/2026): 'me manda o link da
    cesta de Dia dos Namorados' — o copilot nao tinha tool de catalogo do
    site. Agora reusa bot_tools.consultar_produtos e devolve nome + URL
    pra repassar no WhatsApp."""
    from unittest.mock import patch

    from app.models import Usuario
    from app.services import copilot as cs
    URL = ('https://www.padariaartesanalonline.com.br/produto/'
           'cesta-especial-dia-dos-namorados-51')
    fake = {'produtos': [{
        'nome': 'Cesta Especial Dia dos Namorados', 'sku': 'CESTA-NAM',
        'preco': 350.0, 'disponivel': True,
        'descricao': 'Cesta romantica', 'url': URL}]}

    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        assert 'consultar_catalogo_site' in [t['name'] for t in cs.TOOLS]
        assert cs.PAPEIS_POR_TOOL['consultar_catalogo_site'] == {
            'admin', 'gerente', 'funcionario'}
        assert 'consultar_catalogo_site' not in cs.REQUER_APROVACAO
        with patch('app.services.bot_tools.consultar_produtos',
                   return_value=fake):
            r = cs._executar_read('consultar_catalogo_site',
                                  {'busca': 'cesta namorados'}, u)
    assert 'Cesta Especial Dia dos Namorados' in r['texto']
    assert URL in r['texto']
    assert 'R$ 350' in r['texto']


def test_consultar_catalogo_site_vazio(app, admin_user):
    from unittest.mock import patch

    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        with patch('app.services.bot_tools.consultar_produtos',
                   return_value={'produtos': []}):
            r = cs._executar_read('consultar_catalogo_site',
                                  {'busca': 'xyzabc'}, u)
    assert 'Nada no catalogo' in r['texto'] and 'xyzabc' in r['texto']


def test_consultar_catalogo_site_propaga_erro(app, admin_user):
    from unittest.mock import patch

    from app.models import Usuario
    from app.services import copilot as cs
    with app.app_context():
        u = Usuario.query.get(admin_user.id)
        with patch('app.services.bot_tools.consultar_produtos',
                   return_value={'erro': 'VNDA fora do ar'}):
            r = cs._executar_read('consultar_catalogo_site',
                                  {'busca': 'sourdough'}, u)
    assert 'VNDA fora do ar' in r['texto']
