"""Permissoes operacionais editaveis por papel — web + copilot + Slack unificados.

O CODIGO define o padrao (`CAP_DEFAULT`) que espelha EXATAMENTE o comportamento
legado dos decorators e do `PAPEIS_POR_TOOL`. Assim, ativar este modulo NAO muda
nada ate o owner editar pela tela. A tabela `PermissaoPapel` sobrepoe o padrao
(so guarda o que difere). admin/owner NAO entram na matriz — tem sempre acesso
total (evita lockout e mantem o tier superusuario).

Slack ja reusa o copilot, entao basta o copilot consultar daqui pra as 3 pontas
ficarem iguais.
"""
import os
import threading
import time

# Papeis editaveis na matriz (admin/owner sao sempre full, fora da matriz).
PAPEIS_EDITAVEIS = ['gerente', 'producao', 'padeiro', 'rh', 'funcionario']

PAPEL_LABEL = {
    'gerente': 'Gerente',
    'producao': 'Produção',
    'padeiro': 'Padeiro',
    'rh': 'RH',
    'funcionario': 'Funcionário',
}

# (grupo, chave, rotulo) — ordem de exibicao na pagina.
CAPACIDADES = [
    ('Telas (web)', 'web_pedidos', 'Acessar pedidos (ver / criar na tela)'),
    ('Telas (web)', 'web_pedido_operar', 'Operar pedido (confirmar / separar / cancelar / receber)'),
    ('Telas (web)', 'web_estoque_loja', 'Estoque de loja / relatório / preços'),
    ('Telas (web)', 'web_producao', 'Produção (plano / congelados / separação)'),
    ('Telas (web)', 'web_catalogo', 'Catálogo (receitas / MP / produtos / fornecedores)'),
    ('Telas (web)', 'web_rh', 'RH (ponto / férias / cargos)'),
    ('Telas (web)', 'web_padeiro', 'Tela do padeiro (touchscreen)'),
    ('Telas (web)', 'web_lista_compras', 'Lista de compras semanal (preencher na loja)'),
    ('Telas (web)', 'web_lista_compras_consolidar', 'Lista de compras — consolidar (gerente geral)'),

    ('Copilot / Slack', 'criar_pedido', 'Criar pedido'),
    ('Copilot / Slack', 'editar_pedido', 'Editar pedido'),
    ('Copilot / Slack', 'mudar_status_pedido', 'Mudar status de pedido'),
    ('Copilot / Slack', 'receber_mp', 'Receber matéria-prima'),
    ('Copilot / Slack', 'ajuste_estoque', 'Ajustar estoque'),
    ('Copilot / Slack', 'receber_pedido', 'Receber pedido (loja)'),
    ('Copilot / Slack', 'anexar_foto_pedido', 'Anexar foto a pedido'),
    ('Copilot / Slack', 'registrar_desperdicio', 'Registrar desperdício'),
    ('Copilot / Slack', 'registrar_desperdicio_lote', 'Registrar desperdício em lote'),
    ('Copilot / Slack', 'criar_tarefa', 'Criar tarefa'),
    ('Copilot / Slack', 'marcar_tarefa_feita', 'Marcar tarefa feita'),
    ('Copilot / Slack', 'consultar_pedido', 'Consultar pedido'),
    ('Copilot / Slack', 'consultar_estoque', 'Consultar estoque'),
    ('Copilot / Slack', 'consultar_foco', 'Consultar foco do dia'),
    ('Copilot / Slack', 'consultar_tarefas', 'Consultar tarefas'),
    ('Copilot / Slack', 'consultar_desperdicio', 'Consultar desperdício'),
    ('Copilot / Slack', 'consultar_fornecedores', 'Consultar fornecedores'),
    ('Copilot / Slack', 'consultar_caixa', 'Consultar caixa'),
    ('Copilot / Slack', 'consultar_vendas_itens', 'Consultar vendas (itens)'),
    ('Copilot / Slack', 'consultar_cliente_b2b', 'Consultar cliente B2B'),
]

_TODOS = set(PAPEIS_EDITAVEIS)

# Padrao por capacidade = papeis editaveis que a tem HOJE. admin/owner implicitos.
# Web: espelha os decorators. Copilot: espelha PAPEIS_POR_TOOL via papel_efetivo
# (producao/padeiro/rh colapsam pra 'funcionario' no copilot — por isso tools
# nivel-funcionario valem pros 5, e tools nivel-gerente so pro gerente).
CAP_DEFAULT = {
    # ── Telas web (decorators) ──
    'web_pedidos': {'gerente', 'producao', 'rh', 'funcionario'},  # pedidos_required = todos menos padeiro
    'web_pedido_operar': {'gerente', 'producao', 'padeiro'},      # operacional_pedido_required
    'web_estoque_loja': {'gerente'},                              # gerente_required (pode_lojas)
    'web_producao': {'producao'},                                 # producao_required
    'web_catalogo': {'producao'},                                 # catalogo_required (pode_catalogo)
    'web_rh': {'rh'},                                             # rh_required
    'web_padeiro': {'padeiro', 'producao'},                       # padeiro_required
    # Lista de compras semanal — gerente da loja preenche 'tenho';
    # produção tb pode (cobre a Indústria como uma das unidades).
    'web_lista_compras': {'gerente', 'producao'},
    # Consolidar/decidir o que pedir: vazio pra editaveis (= so admin/owner).
    'web_lista_compras_consolidar': set(),
    # ── Copilot/Slack — nivel gerente ──
    'criar_pedido': {'gerente'},
    'editar_pedido': {'gerente'},
    'mudar_status_pedido': {'gerente'},
    'receber_mp': {'gerente'},
    'ajuste_estoque': {'gerente'},
    'consultar_fornecedores': {'gerente'},
    'consultar_caixa': {'gerente'},
    'consultar_vendas_itens': {'gerente'},
    'consultar_cliente_b2b': {'gerente'},
    # ── Copilot/Slack — nivel funcionario (todos os editaveis) ──
    'receber_pedido': set(_TODOS),
    'anexar_foto_pedido': set(_TODOS),
    'registrar_desperdicio': set(_TODOS),
    'registrar_desperdicio_lote': set(_TODOS),
    'consultar_desperdicio': set(_TODOS),
    'consultar_pedido': set(_TODOS),
    'consultar_estoque': set(_TODOS),
    'consultar_foco': set(_TODOS),
    'consultar_tarefas': set(_TODOS),
    'criar_tarefa': set(_TODOS),
    'marcar_tarefa_feita': set(_TODOS),
}

# ── Cache dos overrides (por worker) ───────────────────────────────────
_cache_lock = threading.Lock()
_cache = {'data': None, 'ts': 0.0}


def _overrides():
    """{(papel, capacidade): bool} vindo da tabela. Cache curto por worker.
    Em teste (PYTEST_RUNNING) recarrega sempre — evita vazar estado entre testes."""
    ttl = 0.0 if os.environ.get('PYTEST_RUNNING') else 30.0
    now = time.time()
    with _cache_lock:
        if _cache['data'] is not None and (now - _cache['ts']) < ttl:
            return _cache['data']
    data = {}
    try:
        from app.models import PermissaoPapel
        for row in PermissaoPapel.query.all():
            data[(row.papel, row.capacidade)] = bool(row.permitido)
    except Exception:  # noqa: BLE001 — tabela ausente / fora de contexto: usa default
        data = {}
    with _cache_lock:
        _cache['data'] = data
        _cache['ts'] = time.time()
    return data


def invalidar():
    """Forca recarregar o cache (chamar apos salvar)."""
    with _cache_lock:
        _cache['data'] = None
        _cache['ts'] = 0.0


def eh_editavel(capacidade):
    return capacidade in CAP_DEFAULT


def pode(papel, capacidade):
    """Esse PAPEL real (gerente/producao/padeiro/rh/funcionario) tem a capacidade?

    admin/owner NAO devem passar por aqui — os callers (decorators / pode_usar)
    liberam admin+owner antes. Capacidade desconhecida = nega (menor privilegio).
    """
    if capacidade not in CAP_DEFAULT:
        return False
    overrides = _overrides()
    chave = (papel, capacidade)
    if chave in overrides:
        return overrides[chave]
    return papel in CAP_DEFAULT[capacidade]


def estado_atual():
    """Matriz pra UI: lista de {grupo, chave, rotulo, estados:{papel:bool}}."""
    overrides = _overrides()
    linhas = []
    for grupo, chave, rotulo in CAPACIDADES:
        estados = {}
        for p in PAPEIS_EDITAVEIS:
            estados[p] = overrides.get((p, chave), p in CAP_DEFAULT[chave])
        linhas.append({'grupo': grupo, 'chave': chave, 'rotulo': rotulo, 'estados': estados})
    return linhas


def salvar(form):
    """Grava overrides a partir do form (checkbox name = 'capacidade__papel').
    So persiste o que DIFERE do default — mantem a tabela enxuta."""
    from app.extensions import db
    from app.models import PermissaoPapel
    PermissaoPapel.query.delete()
    for _grupo, chave, _rotulo in CAPACIDADES:
        for p in PAPEIS_EDITAVEIS:
            marcado = form.get(f'{chave}__{p}') is not None
            default = p in CAP_DEFAULT[chave]
            if marcado != default:
                db.session.add(PermissaoPapel(papel=p, capacidade=chave, permitido=marcado))
    db.session.commit()
    invalidar()
