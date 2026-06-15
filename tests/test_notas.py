"""Memória persistente do agente (15/06/2026).

Cobre o stack inteiro:
- Service: registro, busca por keyword (com normalizacao), arquivamento.
- Tools: copilot (consultar_notas + registrar_nota) e bot Padeiro
  (so consultar_notas — bot nao escreve).
- Rotas /notas: lista, criar, editar, arquivar, restaurar + permissao.

Os testes "ancoram" a regra de produto (o copilot LE antes de responder,
o bot SO LE) — sem esses travas, futuras refatorias podem regridir
silenciosamente.
"""


def _admin_logado(app, admin_user):
    """Cliente HTTP com sessão de admin."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


# ── Service ─────────────────────────────────────────────────────────────

def test_registrar_busca_e_arquivar(app):
    from app.models import Nota
    from app.services import notas
    with app.app_context():
        n = notas.registrar(
            'Cookie corta em 5', 'Cookie do café é cortado em 5 pedaços.',
            tags=['cookie', 'cafe'], origem='copilot_slack',
            criada_por_id=None)
        assert n.id is not None
        assert n.tags == 'cookie,cafe'
        assert n.ativa is True

        encontradas = notas.buscar('cookie')
        assert len(encontradas) == 1 and encontradas[0].id == n.id

        # Arquivar tira das buscas
        notas.arquivar(n.id)
        assert notas.buscar('cookie') == []
        # Mas a nota ainda existe no banco (soft delete)
        assert Nota.query.count() == 1


def test_busca_normaliza_acento_e_caixa(app):
    """'Anésio' bate com 'anesio' (caso real: loja Anésio Pinto Rosa)."""
    from app.services import notas
    with app.app_context():
        notas.registrar('Loja Anésio só faz retirada',
                        'Só a unidade Anésio Pinto Rosa aceita retirada do site.',
                        tags='loja-anesio,retirada')
        # busca lowercase sem acento bate
        assert notas.buscar('anesio')
        # busca com acento tambem bate
        assert notas.buscar('Anésio')
        # busca de outra palavra também bate (estado_efetivo: tag tem peso)
        assert notas.buscar('retirada')


def test_busca_rankeia_por_titulo_acima_de_conteudo(app):
    """Peso: título=5, tag=3, conteúdo=1. Quem tem o termo no título sobe."""
    from app.services import notas
    with app.app_context():
        a = notas.registrar('Croissant nutella', 'Sugerir Nutella+Morango se esgotar.')
        b = notas.registrar('Promoção sexta', 'No final do dia mandar croissant pra equipe.')
        r = notas.buscar('croissant')
        assert r[0].id == a.id, 'titulo tem peso maior — A vem primeiro'
        assert {x.id for x in r} == {a.id, b.id}


def test_busca_vazia_devolve_recentes(app):
    """Sem termo, retorna catch-all (mais novas primeiro)."""
    from app.services import notas
    with app.app_context():
        a = notas.registrar('A', 'aaa')
        b = notas.registrar('B', 'bbb')
        r = notas.buscar('')
        assert [n.id for n in r] == [b.id, a.id]


def test_termo_curto_nao_devolve_lixo(app):
    """Termo 1 char vira catch-all — não devolve TODAS as notas."""
    from app.services import notas
    with app.app_context():
        notas.registrar('Pão francês', 'O pão é cortado pela manhã.')
        # 'a' tem 1 char < MIN_TERMO → catch-all (devolve a unica nota)
        r = notas.buscar('a')
        assert len(r) == 1


def test_arquivar_inexistente_devolve_none(app):
    from app.services import notas
    with app.app_context():
        assert notas.arquivar(99999) is None


# ── Tools do copilot ────────────────────────────────────────────────────

def test_copilot_tem_tools_registrar_e_consultar_notas():
    """Sem isso, o LLM vê a tool no prompt mas a chamada quebra."""
    from app.services import copilot
    nomes = [t['name'] for t in copilot.TOOLS]
    assert 'registrar_nota' in nomes
    assert 'consultar_notas' in nomes
    # Handlers existem
    assert 'registrar_nota' in copilot._READ_HANDLERS
    assert 'consultar_notas' in copilot._READ_HANDLERS


def test_copilot_registrar_nota_NAO_exige_aprovacao(app):
    """Anotação é leve — atrito de Block Kit mata o uso. Confirmação fica
    inline no texto. (Se errou, admin arquiva em /notas.)"""
    from app.services import copilot
    assert 'registrar_nota' not in copilot.REQUER_APROVACAO


def test_copilot_registrar_via_executor_persiste(app, admin_user):
    from app.models import Nota
    from app.services import copilot
    with app.app_context():
        out = copilot._read_registrar_nota(
            {'titulo': 'Teste', 'conteudo': 'corpo', 'tags': 'x,y'}, admin_user)
        assert '✅' in out.get('texto', '')
        assert Nota.query.count() == 1
        n = Nota.query.first()
        assert n.titulo == 'Teste' and n.criada_por_id == admin_user.id


def test_copilot_registrar_recusa_campos_vazios(app, admin_user):
    from app.services import copilot
    with app.app_context():
        out = copilot._read_registrar_nota(
            {'titulo': '', 'conteudo': 'x'}, admin_user)
        assert 'erro' in out


def test_copilot_consultar_devolve_texto_serializado(app, admin_user):
    from app.services import copilot, notas
    with app.app_context():
        notas.registrar('Regra A', 'detalhe da regra A', tags='regra')
        out = copilot._read_consultar_notas({'termo': 'regra'}, admin_user)
        assert 'Regra A' in out['texto']
        assert '#' in out['texto']  # tem id pra o LLM pedir 'arquiva a #N'


def test_copilot_consultar_vazio_da_dica(app, admin_user):
    from app.services import copilot
    with app.app_context():
        out = copilot._read_consultar_notas({'termo': 'inexistente_xyz'}, admin_user)
        # Texto guia o LLM a sugerir registrar_nota
        assert 'registrar_nota' in out['texto']


def test_copilot_permissoes_escrita_so_admin():
    """Quem define regra de negócio = admin/owner. Funcionário lê, não escreve.
    Trava regressão — se alguém liberar pra todos, vira lixo dump."""
    from app.services import copilot
    assert copilot.PAPEIS_POR_TOOL['registrar_nota'] == {'admin'}
    # Consultar é aberto (todos precisam saber as regras)
    assert 'funcionario' in copilot.PAPEIS_POR_TOOL['consultar_notas']


# ── Tool do bot Padeiro ─────────────────────────────────────────────────

def test_bot_padeiro_so_le_notas_NAO_escreve():
    """Bot de atendimento NUNCA grava nota (cliente final pediu coisa
    estranha, bot poderia "ensinar errado"). Só consulta."""
    from app.services import chatbot
    nomes = [t['name'] for t in chatbot.TOOLS]
    assert 'consultar_notas' in nomes
    assert 'registrar_nota' not in nomes


def test_bot_padeiro_executor_consulta_notas(app):
    from app.services import chatbot, notas
    with app.app_context():
        notas.registrar(
            'Loja X só vende Y de manhã',
            'Loja Anésio Pinto Rosa só tem Iogurte 200ml até 12h.',
            tags='loja-anesio,iogurte')
        out = chatbot._executar_tool('consultar_notas', {'termo': 'iogurte'})
        assert out.get('notas')
        assert 'Iogurte' in out['texto']


# ── Rotas /notas ────────────────────────────────────────────────────────

def test_rota_index_exige_login(app):
    """Sem login, redireciona pra /login. Notas tem dado interno."""
    c = app.test_client()
    r = c.get('/notas/')
    assert r.status_code == 302
    assert '/login' in r.headers.get('Location', '')


def test_rota_index_admin_lista(app, admin_user):
    from app.services import notas
    with app.app_context():
        notas.registrar('Visivel', 'aaa', tags='t')
    c = _admin_logado(app, admin_user)
    r = c.get('/notas/')
    assert r.status_code == 200
    assert b'Visivel' in r.data or 'Visivel'.encode() in r.data


def test_rota_busca_filtra(app, admin_user):
    from app.services import notas
    with app.app_context():
        notas.registrar('Cookie corta em 5', 'detalhe cookie', tags='cookie')
        notas.registrar('Croissant esgota sexta', 'detalhe c', tags='croissant')
    c = _admin_logado(app, admin_user)
    r = c.get('/notas/?q=cookie')
    assert b'Cookie' in r.data
    assert b'Croissant esgota' not in r.data


def test_rota_nova_admin_cria(app, admin_user):
    from app.models import Nota
    c = _admin_logado(app, admin_user)
    r = c.post('/notas/nova', data={
        'titulo': 'Teste rota', 'conteudo': 'corpo md', 'tags': 'x,y',
        'csrf_token': 'na'  # CSRF off em test
    })
    assert r.status_code == 302
    with app.app_context():
        assert Nota.query.filter_by(titulo='Teste rota').count() == 1


def test_rota_editar_admin(app, admin_user):
    from app.models import Nota
    from app.services import notas
    with app.app_context():
        nid = notas.registrar('Antes', 'corpo', tags='t').id
    c = _admin_logado(app, admin_user)
    r = c.post(f'/notas/{nid}/editar', data={
        'titulo': 'Depois', 'conteudo': 'novo corpo', 'tags': 'novo',
        'csrf_token': 'na'})
    assert r.status_code == 302
    with app.app_context():
        n = Nota.query.get(nid)
        assert n.titulo == 'Depois' and n.atualizada_em is not None


def test_rota_arquivar_e_restaurar(app, admin_user):
    from app.models import Nota
    from app.services import notas
    with app.app_context():
        nid = notas.registrar('Pra arquivar', 'x', tags='t').id
    c = _admin_logado(app, admin_user)
    r = c.post(f'/notas/{nid}/arquivar', data={'csrf_token': 'na'})
    assert r.status_code == 302
    with app.app_context():
        assert Nota.query.get(nid).arquivada_em is not None
    r2 = c.post(f'/notas/{nid}/restaurar', data={'csrf_token': 'na'})
    assert r2.status_code == 302
    with app.app_context():
        assert Nota.query.get(nid).arquivada_em is None


def test_rota_escrita_exige_admin(app):
    """Funcionário pode listar, mas não criar/arquivar."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login='func', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    # Lê
    assert c.get('/notas/').status_code == 200
    # Nova: barrada (admin_required)
    r = c.get('/notas/nova')
    assert r.status_code in (302, 403)


def test_chatbot_prompt_menciona_consultar_notas():
    """Bot Padeiro precisa saber que tem essa tool — sem isso o Opus não
    chama."""
    from app.services.chatbot_prompt import PROMPT
    assert 'consultar_notas' in PROMPT


def test_copilot_prompt_menciona_notas(app, admin_user):
    """Copilot tem que ver as 2 tools no prompt."""
    from app.services.copilot import _build_system_prompt
    with app.app_context():
        s = _build_system_prompt(admin_user)
    assert 'consultar_notas' in s
    assert 'registrar_nota' in s
