"""Matriz PAPEIS_POR_TOOL — consistencia + regressao.

Garante que:
- Toda tool definida (TOOL_*) tem entrada em PAPEIS_POR_TOOL
- pode_usar() respeita o mapeamento
- Default (tool sem mapeamento) = so admin
"""


def _tools_definidas():
    """Extrai os nomes de todas as TOOL_*[name] do copilot.py."""
    from app.services import copilot
    nomes = set()
    for attr in dir(copilot):
        if not attr.startswith('TOOL_'):
            continue
        obj = getattr(copilot, attr)
        if isinstance(obj, dict) and 'name' in obj:
            nomes.add(obj['name'])
    return nomes


def test_toda_tool_tem_permissao_explicita():
    """Esquecer de mapear uma tool em PAPEIS_POR_TOOL eh facil — esse
    teste garante que nao acontece silenciosamente."""
    from app.services.copilot import PAPEIS_POR_TOOL
    tools = _tools_definidas()
    nao_mapeadas = tools - set(PAPEIS_POR_TOOL.keys())
    assert not nao_mapeadas, (
        f'Tools sem entrada em PAPEIS_POR_TOOL: {sorted(nao_mapeadas)}. '
        'Adicione explicitamente — default seria so admin, mas o mapeamento '
        'explicito serve de documentacao.'
    )


def test_mapeamento_nao_tem_tool_inexistente():
    """Detecta tool no mapa que foi removida do schema."""
    from app.services.copilot import PAPEIS_POR_TOOL
    tools = _tools_definidas()
    fantasmas = set(PAPEIS_POR_TOOL.keys()) - tools
    # Toleramos 'receber_pedido' como sinonimo de mudar_status_pedido
    fantasmas -= {'receber_pedido'}
    assert not fantasmas, f'PAPEIS_POR_TOOL tem entradas pra tools inexistentes: {sorted(fantasmas)}'


def test_papeis_validos():
    """Papeis canonicos do sistema (inclui o tier owner)."""
    from app.services.copilot import PAPEIS_POR_TOOL
    canonicos = {'owner', 'admin', 'gerente', 'funcionario', 'producao'}
    for tool, papeis in PAPEIS_POR_TOOL.items():
        invalidos = papeis - canonicos
        assert not invalidos, f'{tool} tem papel invalido: {invalidos}'


def test_admin_pode_tudo_menos_owner_only(app, admin_user):
    """Admin (nao-owner) passa em tudo, exceto nas tools marcadas {'owner'}."""
    from app.services.copilot import PAPEIS_POR_TOOL, pode_usar
    for tool, papeis in PAPEIS_POR_TOOL.items():
        esperado = papeis != {'owner'}
        assert pode_usar(tool, admin_user) == esperado, (
            f'admin em {tool}: esperado {esperado}, papeis={papeis}')


def test_owner_only_tools_de_rh(app):
    """marcar_ponto e consultar_funcionario sao owner-only: gerente e admin
    nao-owner sao bloqueados; o owner passa."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import pode_usar
    gerente = Usuario(nome='G', login='g_rh', papel='gerente')
    admin = Usuario(nome='A', login='a_rh', papel='admin')
    owner = Usuario(nome='O', login='o_rh', papel='admin', is_owner=True)
    for u in (gerente, admin, owner):
        u.set_senha('x')
    db.session.add_all([gerente, admin, owner])
    db.session.commit()

    for tool in ('marcar_ponto', 'consultar_funcionario'):
        assert not pode_usar(tool, gerente)
        assert not pode_usar(tool, admin)
        assert pode_usar(tool, owner)


def test_owner_passa_em_tudo(app):
    """Owner eh superconjunto de admin: passa em qualquer tool."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import PAPEIS_POR_TOOL, pode_usar
    owner = Usuario(nome='O', login='o_all', papel='admin', is_owner=True)
    owner.set_senha('x')
    db.session.add(owner)
    db.session.commit()
    for tool in PAPEIS_POR_TOOL:
        assert pode_usar(tool, owner), f'owner bloqueado em {tool}'


def test_funcionario_bloqueado_em_tools_de_admin(app):
    """Funcionario nao pode fazer balanco de congelados, criar venda b2b, etc."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import pode_usar
    u = Usuario(nome='F', login='f', papel='funcionario')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()

    assert not pode_usar('balanco_congelados', u)
    assert not pode_usar('criar_venda_b2b', u)
    assert not pode_usar('criar_fornecedor', u)
    assert not pode_usar('entrada_lote_loja', u)


def test_funcionario_pode_consultas_basicas(app):
    """Funcionario pode consultar pedidos, estoque, tarefas, registrar desperdicio."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import pode_usar
    u = Usuario(nome='F', login='f', papel='funcionario')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()

    assert pode_usar('consultar_pedido', u)
    assert pode_usar('consultar_estoque', u)
    assert pode_usar('registrar_desperdicio', u)
    assert pode_usar('marcar_tarefa_feita', u)


def test_default_e_admin_only(app):
    """Tool desconhecida (nao mapeada) so deve ser usavel por admin."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import pode_usar
    admin = Usuario(nome='A', login='a', papel='admin')
    func = Usuario(nome='F', login='f', papel='funcionario')
    admin.set_senha('x')
    func.set_senha('x')
    db.session.add_all([admin, func])
    db.session.commit()

    assert pode_usar('tool_fictícia_que_nao_existe', admin)
    assert not pode_usar('tool_fictícia_que_nao_existe', func)


def test_anonimo_nao_pode_nada(app):
    """User sem autenticacao eh bloqueado em tudo."""
    from app.services.copilot import pode_usar

    class FakeAnon:
        is_authenticated = False

    assert not pode_usar('consultar_pedido', FakeAnon())
    assert not pode_usar('consultar_pedido', None)
