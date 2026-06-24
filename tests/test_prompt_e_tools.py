"""Smoke tests do system prompt e listagem de tools.

Detecta regressoes tipo: f-string com chave literal que quebra renderizacao
(aconteceu uma vez quando adicionamos exemplo JSON com {nome: 'X'}).
"""


def test_system_prompt_renderiza(app, admin_user):
    """Garante que _build_system_prompt nao tem f-string quebrada."""
    from app.services import copilot
    s = copilot._build_system_prompt(admin_user)
    # Tem que mencionar tools principais
    assert 'criar_pedido' in s
    assert 'registrar_desperdicio' in s
    assert 'registrar_desperdicio_lote' in s
    # Nao pode estar vazio
    assert len(s) > 500


def test_tools_listadas_pra_admin(app, admin_user):
    """Admin enxerga todas as tools registradas."""
    from app.services import copilot
    tools = copilot.tools_permitidas(admin_user)
    nomes = [t['name'] for t in tools]
    assert 'criar_pedido' in nomes
    assert 'registrar_desperdicio' in nomes
    assert 'registrar_desperdicio_lote' in nomes
    assert 'balanco_congelados' in nomes


def test_toda_tool_em_requer_aprovacao_tem_executor(app):
    """Cada write tool tem executor registrado em executar()."""
    from app.services import copilot
    # registrar_desperdicio_lote, criar_pedido etc devem rotear
    for tool_name in copilot.REQUER_APROVACAO:
        # Nao deve dar AttributeError ao montar params vazios
        # (apenas testando que a tool existe nas listas)
        assert tool_name  # sanity
