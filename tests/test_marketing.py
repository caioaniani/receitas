"""E-mail marketing via Listmonk (05/08/2026).

Pedido do dono: disparar propaganda e feliz aniversário pros e-mails da base
— tanto de quem comprou no site quanto de quem usou o Wi-Fi das lojas.

O Listmonk é SEMPRE mockado (mesmo padrão da Anthropic/Seru/Tiny): nenhum
teste pode bater na instância real nem, muito menos, disparar e-mail.

O fixture `app` mantém um app-context ativo — não abrir outro aninhado.
"""
from datetime import date
from unittest.mock import patch

from app.extensions import db


def _cliente(nome, email, dia=None, mes=None, ativo=True, telefone=None):
    from app.models import Cliente
    c = Cliente(nome=nome, email=email, telefone=telefone,
                aniversario_dia=dia, aniversario_mes=mes, ativo=ativo)
    db.session.add(c)
    db.session.commit()
    return c


def _pedido_pago(email, pago=True, divulgacao=False):
    from app.models import PedidoOnline
    from app.utils import agora
    p = PedidoOnline(codigo=f'c{email[:8]}', nome_cliente='X',
                     email_cliente=email, modo_entrega='retirada',
                     subtotal=10, valor_total=10, divulgacao=divulgacao,
                     status='pago', pago_em=agora() if pago else None)
    db.session.add(p)
    db.session.commit()
    return p


def _sessao_wifi(email):
    from datetime import timedelta

    from app.models import WifiPortalSessao
    from app.utils import agora
    s = WifiPortalSessao(token=f't{email}', codigo='WIFI-AAA111', nome='X',
                         email=email, telefone='11999998888',
                         senha_hash='x', aceite_lgpd_em=agora(),
                         expira_em=agora() + timedelta(minutes=30))
    db.session.add(s)
    db.session.commit()
    return s


def _cfg(app):
    app.config['LISTMONK_URL'] = 'https://mkt.example.com'
    app.config['LISTMONK_API_TOKEN'] = 'tok'
    app.config['LISTMONK_API_USER'] = 'api_padaria'


# ── Quem entra em cada lista ─────────────────────────────────────────

def test_site_so_pega_quem_pagou(app):
    from app.services import marketing
    _cliente('a@x.com', 'a@x.com')
    _cliente('b@x.com', 'b@x.com')
    _pedido_pago('a@x.com')
    _pedido_pago('b@x.com', pago=False)
    emails = [c['email'] for c in marketing.contatos_do_site()]
    assert emails == ['a@x.com']


def test_site_ignora_divulgacao(app):
    """Brinde/PR não é compra — não vira base de marketing."""
    from app.services import marketing
    _cliente('D', 'd@x.com')
    _pedido_pago('d@x.com', divulgacao=True)
    assert marketing.contatos_do_site() == []


def test_wifi_pega_quem_passou_pelo_portal(app):
    from app.services import marketing
    _cliente('W', 'w@x.com')
    _cliente('Z', 'z@x.com')
    _sessao_wifi('w@x.com')
    emails = [c['email'] for c in marketing.contatos_do_wifi()]
    assert emails == ['w@x.com']


def test_wifi_pega_o_cadastro_do_modo_radius(app):
    """REGRESSÃO (05/08/2026): o portal no modo RADIUS cria só o `Cliente`,
    sem `WifiPortalSessao`. Derivar da sessão fazia a lista mostrar 1 pessoa
    em vez de dezenas."""
    from app.services import marketing
    _cliente('R', 'r@x.com')                      # cadastro antigo, sem origem
    c = _cliente('W', 'w@x.com')
    c.origem = 'wifi'
    db.session.commit()
    emails = [x['email'] for x in marketing.contatos_do_wifi()]
    assert emails == ['w@x.com']


def test_criar_conta_do_portal_carimba_a_origem(app):
    """A origem tem que ser gravada NA HORA — inferir depois foi o erro."""
    from app.services import wifi_portal
    status, c = wifi_portal.criar_conta_direta({
        'nome': 'Ana Silva', 'email': 'ana@x.com', 'telefone': '11999998888',
        'senha': 'segredo123', 'aniversario_dia': 9, 'aniversario_mes': 8,
        'nascimento_ano': None})
    assert status == 'criada' and c.origem == 'wifi'


def test_descadastrado_fica_fora_de_tudo(app):
    from app.services import marketing
    from app.utils import agora
    c = _cliente('A', 'a@x.com', dia=5, mes=8)
    _pedido_pago('a@x.com')
    _sessao_wifi('a@x.com')
    assert marketing.contatos_do_site()
    c.marketing_descadastro_em = agora()
    db.session.commit()
    assert marketing.contatos_do_site() == []
    assert marketing.contatos_do_wifi() == []
    assert marketing.aniversariantes(date(2026, 8, 5)) == []


def test_cliente_inativo_fica_fora(app):
    from app.services import marketing
    _cliente('A', 'a@x.com', ativo=False)
    _pedido_pago('a@x.com')
    assert marketing.contatos_do_site() == []


def test_aniversario_vai_nos_attribs(app):
    """dia/mês precisam viajar em `attribs` — é por eles que a consulta do
    Listmonk monta a lista do dia."""
    import json

    from app.services import marketing
    _cliente('A', 'a@x.com', dia=9, mes=8, telefone='11999998888')
    c = marketing.aniversariantes(date(2026, 8, 9))[0]
    a = json.loads(c['attribs_json'])
    assert a['aniv_dia'] == 9 and a['aniv_mes'] == 8
    assert a['telefone'] == '11999998888'


# ── Sincronização ────────────────────────────────────────────────────

def test_sincronizar_sem_config_nao_estoura(app):
    from app.services import marketing
    app.config['LISTMONK_URL'] = ''
    app.config['LISTMONK_API_TOKEN'] = ''
    st = marketing.sincronizar()
    assert 'não configurado' in st['erro']


def test_sincronizar_puxa_descadastro_antes_de_importar(app):
    """Ordem importa: importar antes de colher re-inscreveria quem acabou de
    cancelar."""
    from app.services import marketing
    _cfg(app)
    ordem = []
    with patch('app.services.listmonk.garantir_lista', side_effect=[1, 2, 3, 9]), \
         patch('app.services.listmonk.descadastrados', return_value={}) as desc, \
         patch('app.services.listmonk.mudar_listas'), \
         patch('app.services.listmonk.importar') as imp:
        desc.side_effect = lambda lid: ordem.append('desc') or {}
        imp.side_effect = lambda *a, **k: ordem.append('imp')
        st = marketing.sincronizar()
    assert st['erro'] is None
    assert ordem[0] == 'desc' and 'imp' in ordem


def test_descadastro_e_propagado_e_marcado_no_banco(app):
    from app.services import marketing
    _cfg(app)
    c = _cliente('A', 'A@X.com')
    with patch('app.services.listmonk.descadastrados',
               side_effect=[{'a@x.com': 77}, {}]), \
         patch('app.services.listmonk.mudar_listas') as mud:
        n = marketing.marcar_descadastros([1, 2])
    assert n == 1
    db.session.refresh(c)
    assert c.marketing_descadastro_em is not None
    # propagou o "não quero" pras DUAS listas, por id (nunca montando SQL)
    mud.assert_called_once_with([77], 'unsubscribe', [1, 2])


def test_descadastro_nao_remarca_quem_ja_saiu(app):
    from app.services import marketing
    from app.utils import agora
    _cfg(app)
    antes = agora()
    c = _cliente('A', 'a@x.com')
    c.marketing_descadastro_em = antes
    db.session.commit()
    with patch('app.services.listmonk.descadastrados',
               return_value={'a@x.com': 7}), \
         patch('app.services.listmonk.mudar_listas'):
        assert marketing.marcar_descadastros([1]) == 0
    db.session.refresh(c)
    assert c.marketing_descadastro_em == antes


# ── Campanha de aniversário ──────────────────────────────────────────

def _patches(n_lista, listas=(1, 2, 3, 9)):
    """Mocka o Listmonk inteiro; devolve os mocks que os testes conferem."""
    return (
        patch('app.services.listmonk.garantir_lista',
              side_effect=lambda nome, desc='': dict(zip(
                  ['Clientes do site', 'Wi-Fi das lojas', 'Sorteio 2026',
                   'Aniversariantes de hoje'], listas))[nome]),
        patch('app.services.listmonk.descadastrados', return_value={}),
        patch('app.services.listmonk.mudar_listas'),
        patch('app.services.listmonk.mudar_listas_por_query'),
        patch('app.services.listmonk.contar', return_value=n_lista),
        patch('app.services.listmonk.criar_campanha', return_value=55),
        patch('app.services.listmonk.iniciar_campanha'),
    )


def test_aniversario_monta_lista_do_dia_e_envia(app):
    from app.services import marketing
    _cfg(app)
    p = _patches(3)
    with p[0], p[1], p[2], p[3] as query, p[4], p[5] as cria, p[6] as inicia:
        st = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert st['erro'] is None and st['enviada'] is True and st['n'] == 3
    # 1º: esvazia a transiente. 2º: enche com o dia/mês de hoje.
    limpar, encher = query.call_args_list
    assert limpar.args[1] == 'remove' and limpar.args[2] == [9]
    assert encher.args[1] == 'add' and encher.args[2] == [9]
    assert "aniv_dia' = '9'" in encher.args[0]
    assert "aniv_mes' = '8'" in encher.args[0]
    # a consulta é limitada às NOSSAS listas de origem
    assert encher.kwargs['listas_origem'] == [1, 2, 3]
    assert cria.call_args.args[3] == [9]     # campanha mira só a transiente
    inicia.assert_called_once_with(55)


def test_aniversario_sem_ninguem_nao_cria_campanha(app):
    from app.services import marketing
    _cfg(app)
    p = _patches(0)
    with p[0], p[1], p[2], p[3], p[4], p[5] as cria, p[6]:
        st = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert st['pulou'] and st['campanha_id'] is None
    cria.assert_not_called()


def test_aniversario_acima_do_teto_nao_envia(app):
    """Muita gente no MESMO dia = consulta errada, não festa."""
    from app.services import marketing
    _cfg(app)
    p = _patches(5000)
    with p[0], p[1], p[2], p[3], p[4], p[5] as cria, p[6] as inicia:
        st = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert 'teto' in st['erro']
    cria.assert_not_called()
    inicia.assert_not_called()


def test_automatico_nasce_desligado_e_so_faz_rascunho(app):
    """Sem o gesto do dono, nada sai — a campanha fica em rascunho."""
    from app.services import marketing
    _cfg(app)
    assert marketing.envio_automatico_ligado() is False
    p = _patches(2)
    with p[0], p[1], p[2], p[3], p[4], p[5] as cria, p[6] as inicia:
        st = marketing.campanha_aniversario(date(2026, 8, 9))
    cria.assert_called_once()
    inicia.assert_not_called()
    assert st['enviada'] is False and 'rascunho' in st['pulou']


def test_nao_envia_duas_vezes_no_mesmo_dia(app):
    from app.models import AppConfig
    from app.services import marketing
    _cfg(app)
    AppConfig.set(marketing.CFG_ANIV_ATIVO, '1')
    db.session.commit()
    p = _patches(2)
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6] as inicia:
        marketing.campanha_aniversario(date(2026, 8, 9))
        st = marketing.campanha_aniversario(date(2026, 8, 9))
    assert st['pulou'] == 'já enviada hoje'
    assert inicia.call_count == 1


def test_colhe_descadastro_da_transiente_antes_de_apagar(app):
    """Quem cancela no e-mail de aniversário cancela na lista que é refeita
    todo dia — se não colhermos ANTES, o "não quero mais" some."""
    from app.services import marketing
    _cfg(app)
    c = _cliente('A', 'a@x.com')
    ordem = []
    with patch('app.services.listmonk.garantir_lista',
               side_effect=lambda n, d='': {'Clientes do site': 1,
                                            'Wi-Fi das lojas': 2,
                                            'Sorteio 2026': 3,
                                            'Aniversariantes de hoje': 9}[n]), \
         patch('app.services.listmonk.descadastrados',
               side_effect=lambda lid: (ordem.append(f'colhe{lid}')
                                        or ({'a@x.com': 7} if lid == 9 else {}))), \
         patch('app.services.listmonk.mudar_listas'), \
         patch('app.services.listmonk.mudar_listas_por_query',
               side_effect=lambda q, acao, *a, **k: ordem.append(acao)), \
         patch('app.services.listmonk.contar', return_value=0):
        marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert ordem.index('colhe9') < ordem.index('remove')
    db.session.refresh(c)
    assert c.marketing_descadastro_em is not None


def test_falha_ao_disparar_nao_vira_dois_parabens(app):
    """O marcador do dia é gravado ANTES do disparo: se o "iniciar" quebrar,
    hoje fica sem e-mail — nunca com dois."""
    from app.models import AppConfig
    from app.services import marketing
    _cfg(app)
    p = _patches(2)
    with p[0], p[1], p[2], p[3], p[4], p[5], \
         patch('app.services.listmonk.iniciar_campanha',
               side_effect=RuntimeError('timeout')) as inicia:
        st = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
        assert 'timeout' in st['erro'] and st['enviada'] is False
        assert AppConfig.get(marketing.CFG_ANIV_ULTIMO) == '2026-08-09'
        st2 = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert st2['pulou'] == 'já enviada hoje'
    assert inicia.call_count == 1


def test_resumo_le_a_contagem_numa_requisicao_so(app):
    """Painel do dono não pode ficar pendurado num `contar` por lista."""
    from app.services import marketing
    _cfg(app)
    with patch('app.services.listmonk.listas_detalhe',
               return_value={'Clientes do site': {'id': 1, 'n': 42}}) as det, \
         patch('app.services.listmonk.contar') as contar:
        r = marketing.resumo()
    det.assert_called_once()
    contar.assert_not_called()
    assert r['listas'][0]['n'] == 42
    assert r['listas'][3]['id'] is None      # transiente ainda não existe


def test_falha_do_listmonk_nao_sobe_pro_cron(app):
    from app.services import marketing
    _cfg(app)
    with patch('app.services.listmonk.garantir_lista',
               side_effect=RuntimeError('listmonk fora')):
        st = marketing.campanha_aniversario(date(2026, 8, 9), enviar=True)
    assert 'listmonk fora' in st['erro'] and st['enviada'] is False


def test_resumo_sem_config_avisa(app):
    from app.services import marketing
    app.config['LISTMONK_URL'] = ''
    app.config['LISTMONK_API_TOKEN'] = ''
    r = marketing.resumo()
    assert r['disponivel'] is False and 'LISTMONK_URL' in r['erro']


# ── Tela do dono ─────────────────────────────────────────────────────

def _login(c, user_id):
    with c.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True


def test_painel_exige_owner(app):
    assert app.test_client().get('/admin/marketing').status_code \
        in (302, 401, 403)


def test_painel_abre_pro_dono(app, owner_user):
    uid = owner_user.id
    app.config['LISTMONK_URL'] = ''
    app.config['LISTMONK_API_TOKEN'] = ''
    c = app.test_client()
    _login(c, uid)
    r = c.get('/admin/marketing')
    assert r.status_code == 200
    assert 'Feliz aniversário automático' in r.get_data(as_text=True)


def test_salvar_liga_o_automatico(app, owner_user):
    from app.models import AppConfig
    from app.services import marketing
    uid = owner_user.id
    c = app.test_client()
    _login(c, uid)
    r = c.post('/admin/marketing/salvar',
               data={'assunto': 'Parabéns!', 'corpo': '<p>oi</p>', 'auto': '1'},
               follow_redirects=True)
    assert r.status_code == 200
    assert AppConfig.get(marketing.CFG_ANIV_ATIVO) == '1'
    assert AppConfig.get(marketing.CFG_ANIV_ASSUNTO) == 'Parabéns!'
    assert marketing.envio_automatico_ligado() is True
