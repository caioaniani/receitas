"""Servico do Copilot: interpreta comandos em linguagem natural via
Claude Haiku 4.5 e retorna acoes estruturadas pra preview/aprovacao.

Tools suportadas:
- criar_pedido (write) — pedido de loja pra producao
- consultar_pedido (read) — consulta pedidos por cliente/data/status
- consultar_estoque (read) — consulta estoque MP / produtos
- receber_mp (write) — entrada de materia-prima
- ajuste_estoque (write) — ajuste manual (quebra/perda)

Toda acao 'write' retorna preview pra aprovacao manual antes de executar.
Acoes 'read' executam direto e retornam texto."""
import json
import logging
import os
from datetime import date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import Loja, Produto, Receita, MateriaPrima

logger = logging.getLogger(__name__)


# ── Tools ──────────────────────────────────────────────────────────────

TOOL_CRIAR_PEDIDO = {
    "name": "criar_pedido",
    "description": (
        "Cria um pedido de loja pra producao. Use quando o usuario pedir pra "
        "encomendar/pedir/solicitar produtos pra uma data. NAO executa direto — "
        "retorna preview pra usuario confirmar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"], "description": "ID da loja destinataria. Null se nao mencionada."},
            "data_entrega": {"type": "string", "description": "Data YYYY-MM-DD. Resolva 'amanha'/'sexta'/etc."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome EXATO do produto/receita do catalogo."},
                        "quantidade": {"type": "integer", "minimum": 1},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["data_entrega", "itens"],
    },
}

TOOL_CONSULTAR_PEDIDO = {
    "name": "consultar_pedido",
    "description": "Consulta pedidos por loja, data, status ou ID. Retorna lista de pedidos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"]},
            "data_de": {"type": ["string", "null"], "description": "Data inicial YYYY-MM-DD."},
            "data_ate": {"type": ["string", "null"], "description": "Data final YYYY-MM-DD."},
            "status": {"type": ["string", "null"], "enum": [None, "pendente", "confirmado", "separado", "em_transporte", "entregue", "cancelado"]},
            "pedido_id": {"type": ["integer", "null"]},
        },
    },
}

TOOL_CONSULTAR_ESTOQUE = {
    "name": "consultar_estoque",
    "description": "Consulta estoque atual de uma materia-prima especifica, ou lista MPs com estoque baixo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": ["string", "null"], "description": "Nome EXATO da MP. Null pra listar todas baixas."},
            "apenas_baixo": {"type": "boolean", "default": False, "description": "Se true, lista so MPs abaixo do estoque minimo."},
        },
    },
}

TOOL_RECEBER_MP = {
    "name": "receber_mp",
    "description": "Registra entrada de materia-prima no estoque. NAO executa direto — retorna preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": "string", "description": "Nome EXATO da MP do catalogo."},
            "quantidade": {"type": "number", "minimum": 0.01},
            "preco_total": {"type": ["number", "null"], "description": "Valor total pago (R$). Calcula preco_unitario automaticamente."},
            "preco_unitario": {"type": ["number", "null"], "description": "Preco por unidade. Alternativa ao preco_total."},
            "referencia": {"type": ["string", "null"], "description": "Ex: 'NF 12345 - Fornecedor X'."},
        },
        "required": ["mp_nome", "quantidade"],
    },
}

TOOL_AJUSTE_ESTOQUE = {
    "name": "ajuste_estoque",
    "description": "Ajuste manual de estoque MP (quebra, perda, contagem, devolucao). NAO executa direto — preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": "string"},
            "quantidade": {"type": "number", "minimum": 0.01, "description": "Sempre positiva. tipo decide se entra ou sai."},
            "tipo": {"type": "string", "enum": ["entrada", "saida"]},
            "motivo": {"type": "string", "description": "Ex: 'quebra por umidade', 'contagem fisica', 'devolucao'."},
        },
        "required": ["mp_nome", "quantidade", "tipo", "motivo"],
    },
}

TOOLS = [TOOL_CRIAR_PEDIDO, TOOL_CONSULTAR_PEDIDO, TOOL_CONSULTAR_ESTOQUE, TOOL_RECEBER_MP, TOOL_AJUSTE_ESTOQUE]

# Quais tools requerem preview/aprovacao (writes)
REQUER_APROVACAO = {'criar_pedido', 'receber_mp', 'ajuste_estoque'}


def _catalogo_texto():
    """Lista produtos + receitas + MPs formatados pra contexto do LLM."""
    linhas = ["PRODUTOS (use o nome exato):"]
    for p in Produto.query.order_by(Produto.nome).all():
        linhas.append(f"  - {p.nome}")
    linhas.append("")
    linhas.append("RECEITAS (use o nome exato):")
    for r in Receita.query.order_by(Receita.nome).all():
        linhas.append(f"  - {r.nome}")
    linhas.append("")
    linhas.append("MATERIAS PRIMAS (use o nome exato):")
    for m in MateriaPrima.query.order_by(MateriaPrima.nome).all():
        unidade = m.unidade or '?'
        linhas.append(f"  - {m.nome} ({unidade})")
    return "\n".join(linhas)


def _lojas_texto(user):
    if user.is_admin():
        lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    else:
        lojas = [user.loja] if user.loja_id and user.loja else []
    if not lojas:
        return "(nenhuma loja disponivel)"
    return "\n".join(f"  - id={l.id}: {l.nome}" for l in lojas)


def _build_system_prompt(user):
    hoje = date.today().isoformat()
    return f"""Voce e' um assistente de gestao de uma padaria. Interpreta comandos em
linguagem natural e estrutura acoes pra o usuario confirmar.

Hoje e' {hoje}.

LOJAS DISPONIVEIS:
{_lojas_texto(user)}

{_catalogo_texto()}

TOOLS DISPONIVEIS:
- criar_pedido: criar encomenda de produtos pra producao entregar numa loja
- consultar_pedido: ver pedidos existentes (por loja, data, status, id)
- consultar_estoque: ver estoque de MP especifica ou MPs em alerta
- receber_mp: registrar entrada de materia-prima (compra/fornecedor)
- ajuste_estoque: quebra, perda, contagem fisica de MP

REGRAS:
- Use o nome EXATO dos catalogos. Se ambiguo ('100 croissants' com varios tipos),
  escolha o mais provavel e mencione na sua resposta-texto que o usuario confirme.
- Datas relativas: resolva 'amanha', 'sexta', 'segunda', etc. pra YYYY-MM-DD.
- Pra criar_pedido sem loja mencionada (admin), use loja_id=null e explique
  que o usuario precisa escolher no preview.
- Se algo for impossivel ou ambiguo demais, NAO use tool — responda em texto
  pedindo clarificacao.
- Respostas: portugues brasileiro, lowercase, conciso (1-2 frases).
"""


def interpretar(prompt_text, user, historico=None):
    """Chama Claude. Retorna dict com tipo, params, explicacao.

    historico: lista de {role: 'user'|'assistant', content: str} com
    conversas anteriores nessa sessao. Permite copilot lembrar contexto
    ('ah entendi, foi aqui' depois de uma resposta).
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY') or current_app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'tipo': 'erro', 'explicacao': 'Copilot indisponivel: ANTHROPIC_API_KEY nao configurada.', 'raw': None}
    try:
        import anthropic
    except ImportError:
        return {'tipo': 'erro', 'explicacao': 'Biblioteca anthropic nao instalada.', 'raw': None}

    client = anthropic.Anthropic(api_key=api_key)
    system = _build_system_prompt(user)

    # Monta mensagens: historico (filtrado/sanitizado) + prompt atual
    messages = []
    for m in (historico or []):
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            messages.append({'role': role, 'content': content})
    messages.append({'role': 'user', 'content': prompt_text})
    # Limita as ultimas 20 mensagens pra nao estourar tokens
    if len(messages) > 20:
        messages = messages[-20:]
        # Garante que comece com user (Claude exige primeira msg = user)
        while messages and messages[0]['role'] != 'user':
            messages = messages[1:]

    try:
        response = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=2000,
            system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            tools=TOOLS,
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot: erro Anthropic')
        return {'tipo': 'erro', 'explicacao': f'Erro Anthropic: {exc}', 'raw': None}

    tool_call = None
    tool_name = None
    texto_partes = []
    for block in response.content:
        if block.type == 'tool_use':
            tool_call = block.input
            tool_name = block.name
        elif block.type == 'text':
            texto_partes.append(block.text)

    explicacao = ' '.join(texto_partes).strip() or '(sem comentario do copilot)'
    raw = {
        'stop_reason': response.stop_reason,
        'usage': {
            'input': response.usage.input_tokens, 'output': response.usage.output_tokens,
            'cache_read': getattr(response.usage, 'cache_read_input_tokens', 0),
            'cache_create': getattr(response.usage, 'cache_creation_input_tokens', 0),
        },
    }

    if tool_call and tool_name:
        # Enriquece params com info do banco (matches de produto/MP)
        params = _enriquecer_params(tool_name, tool_call, user)
        # Tools 'read' executam direto agora e retornam resultado no campo `resultado`
        if tool_name not in REQUER_APROVACAO:
            resultado = _executar_read(tool_name, params, user)
            return {
                'tipo': tool_name, 'params': params, 'explicacao': explicacao,
                'resultado': resultado, 'requer_aprovacao': False, 'raw': raw,
            }
        return {
            'tipo': tool_name, 'params': params, 'explicacao': explicacao,
            'requer_aprovacao': True, 'raw': raw,
        }
    return {'tipo': 'conversa', 'explicacao': explicacao, 'raw': raw}


def _enriquecer_params(tool_name, tool_input, user):
    """Adiciona matches do banco aos params pra o preview poder mostrar opcoes."""
    if tool_name == 'criar_pedido':
        return _enriquecer_criar_pedido(tool_input)
    if tool_name in ('receber_mp', 'ajuste_estoque'):
        nome = (tool_input.get('mp_nome') or '').strip()
        matches = _resolver_mp(nome) if nome else []
        return {**tool_input, 'mp_matches': matches, 'mp_resolvida': matches[0] if matches else None}
    # consultar_pedido / consultar_estoque: passam direto
    return tool_input


def _enriquecer_criar_pedido(tool_input):
    itens_enriq = []
    for item in (tool_input.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        qtd = int(item.get('quantidade') or 0)
        if not nome or qtd <= 0:
            continue
        matches = _resolver_produto(nome)
        itens_enriq.append({
            'nome_original': nome, 'quantidade': qtd,
            'matches': matches, 'resolvido': matches[0] if matches else None,
        })
    loja_id = tool_input.get('loja_id')
    loja_nome = None
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            loja_nome = l.nome
        else:
            loja_id = None
    return {
        'loja_id': loja_id, 'loja_nome': loja_nome,
        'data_entrega': tool_input.get('data_entrega'),
        'itens': itens_enriq, 'observacao': tool_input.get('observacao'),
    }


def _resolver_produto(nome):
    from sqlalchemy import func
    matches = []
    p = Produto.query.filter(func.lower(Produto.nome) == nome.lower()).first()
    if p:
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'exato'})
    r = Receita.query.filter(func.lower(Receita.nome) == nome.lower()).first()
    if r:
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'exato'})
    if matches:
        return matches
    for p in Produto.query.filter(Produto.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'fuzzy'})
    for r in Receita.query.filter(Receita.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'fuzzy'})
    return matches


def _resolver_mp(nome):
    from sqlalchemy import func
    matches = []
    m = MateriaPrima.query.filter(func.lower(MateriaPrima.nome) == nome.lower()).first()
    if m:
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade, 'match': 'exato'})
    if matches:
        return matches
    for m in MateriaPrima.query.filter(MateriaPrima.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade, 'match': 'fuzzy'})
    return matches


# ── Executores READ (sem aprovacao) ───────────────────────────────────

def _executar_read(tool_name, params, user):
    try:
        if tool_name == 'consultar_pedido':
            return _read_consultar_pedido(params, user)
        if tool_name == 'consultar_estoque':
            return _read_consultar_estoque(params, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot read tool falhou')
        return {'erro': str(exc)}
    return {'erro': f'tool nao implementada: {tool_name}'}


def _read_consultar_pedido(params, user):
    from app.models import PedidoLoja
    q = PedidoLoja.query
    if params.get('pedido_id'):
        p = q.get(params['pedido_id'])
        if not p:
            return {'texto': f'Pedido #{params["pedido_id"]} nao encontrado.'}
        return {'texto': _formatar_pedido(p)}
    if params.get('loja_id'):
        q = q.filter_by(loja_id=params['loja_id'])
    if params.get('status'):
        q = q.filter_by(status=params['status'])
    try:
        if params.get('data_de'):
            q = q.filter(PedidoLoja.data_entrega >= datetime.strptime(params['data_de'], '%Y-%m-%d').date())
        if params.get('data_ate'):
            q = q.filter(PedidoLoja.data_entrega <= datetime.strptime(params['data_ate'], '%Y-%m-%d').date())
    except ValueError:
        pass
    pedidos = q.order_by(PedidoLoja.data_entrega.desc()).limit(15).all()
    if not pedidos:
        return {'texto': 'Nenhum pedido encontrado com esses filtros.'}
    linhas = [f'**{len(pedidos)} pedido(s) encontrado(s):**']
    for p in pedidos:
        linhas.append(f'- #{p.id} · {p.loja.nome} · {p.data_entrega.strftime("%d/%m/%Y") if p.data_entrega else "—"} · {p.status} · {len(p.itens)} itens')
    return {'texto': '\n'.join(linhas)}


def _formatar_pedido(p):
    from app.models import PedidoItem
    linhas = [f'**Pedido #{p.id}** — {p.loja.nome}']
    linhas.append(f'Data: {p.data_entrega.strftime("%d/%m/%Y") if p.data_entrega else "—"}')
    linhas.append(f'Status: {p.status}')
    if p.observacao:
        linhas.append(f'Obs: {p.observacao}')
    linhas.append('Itens:')
    for it in p.itens:
        linhas.append(f'  - {it.quantidade}× {it.nome_item}')
    return '\n'.join(linhas)


def _read_consultar_estoque(params, user):
    from app.models import MovimentacaoEstoque, AlertaEstoque
    from sqlalchemy import func as sqlfunc
    mp_nome = (params.get('mp_nome') or '').strip()
    apenas_baixo = bool(params.get('apenas_baixo'))

    if mp_nome:
        matches = _resolver_mp(mp_nome)
        if not matches:
            return {'texto': f'MP "{mp_nome}" nao encontrada.'}
        m = MateriaPrima.query.get(matches[0]['id'])
        saldo = _calcular_saldo_mp(m.id)
        alerta = AlertaEstoque.query.filter_by(materia_prima_id=m.id).first()
        txt = f'**{m.nome}**: {saldo} {m.unidade or ""} em estoque.'
        if alerta and saldo < alerta.estoque_minimo:
            txt += f'\n⚠ ABAIXO do minimo ({alerta.estoque_minimo} {m.unidade}).'
        return {'texto': txt}

    if apenas_baixo:
        alertas = AlertaEstoque.query.all()
        baixos = []
        for a in alertas:
            saldo = _calcular_saldo_mp(a.materia_prima_id)
            if saldo < a.estoque_minimo:
                m = a.materia_prima
                baixos.append(f'- {m.nome}: {saldo} {m.unidade or ""} (min: {a.estoque_minimo})')
        if not baixos:
            return {'texto': 'Nenhuma MP esta abaixo do estoque minimo.'}
        return {'texto': '**MPs em alerta:**\n' + '\n'.join(baixos)}

    return {'texto': 'Especifique uma MP ou peca pra listar as baixas (apenas_baixo=true).'}


def _calcular_saldo_mp(mp_id):
    from app.models import MovimentacaoEstoque
    from sqlalchemy import func as sqlfunc
    entradas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='entrada').scalar() or 0
    saidas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='saida').scalar() or 0
    return round(entradas - saidas, 3)


# ── Executores WRITE (aprovacao obrigatoria) ──────────────────────────

def executar(tipo_acao, params, user):
    """Roteador dos executores write. Chamado apos aprovacao no preview."""
    if tipo_acao == 'criar_pedido':
        return executar_criar_pedido(params, user)
    if tipo_acao == 'receber_mp':
        return executar_receber_mp(params, user)
    if tipo_acao == 'ajuste_estoque':
        return executar_ajuste_estoque(params, user)
    return {'ok': False, 'erro': f'tipo de acao desconhecido: {tipo_acao}'}


def executar_criar_pedido(params, user):
    from app.models import PedidoLoja, PedidoItem
    loja_id = params.get('loja_id')
    if not loja_id:
        return {'ok': False, 'erro': 'Loja nao especificada'}
    loja = Loja.query.get(loja_id)
    if not loja:
        return {'ok': False, 'erro': f'Loja {loja_id} nao encontrada'}
    try:
        data_entrega = datetime.strptime(params.get('data_entrega'), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return {'ok': False, 'erro': f'Data invalida'}
    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Pedido sem itens'}
    pedido = PedidoLoja(
        loja_id=loja_id, data_entrega=data_entrega,
        observacao=(params.get('observacao') or '').strip() or None,
        criado_por=user.id, status='pendente',
    )
    db.session.add(pedido)
    db.session.flush()
    salvos = 0
    nao_resolvidos = []
    for item in itens:
        qtd = int(item.get('quantidade') or 0)
        if qtd <= 0:
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            nao_resolvidos.append(item.get('nome_original') or '?')
            continue
        pi = PedidoItem(pedido_id=pedido.id, quantidade=qtd)
        if resolvido['tipo'] == 'produto':
            pi.produto_id = resolvido['id']
        elif resolvido['tipo'] == 'receita':
            pi.receita_id = resolvido['id']
        db.session.add(pi)
        salvos += 1
    if salvos == 0:
        db.session.rollback()
        return {'ok': False, 'erro': f'Nenhum item resolvido. Nao achei: {", ".join(nao_resolvidos)}'}
    db.session.commit()
    return {'ok': True, 'pedido_id': pedido.id, 'itens_salvos': salvos, 'nao_resolvidos': nao_resolvidos,
            'registro_tipo': 'pedido_loja', 'registro_id': pedido.id, 'url': f'/pedidos/{pedido.id}'}


def executar_receber_mp(params, user):
    from app.models import MovimentacaoEstoque
    resolvida = params.get('mp_resolvida')
    if not resolvida or not resolvida.get('id'):
        return {'ok': False, 'erro': f'MP nao identificada: {params.get("mp_nome")}'}
    quantidade = float(params.get('quantidade') or 0)
    if quantidade <= 0:
        return {'ok': False, 'erro': 'Quantidade invalida'}
    preco_unitario = params.get('preco_unitario')
    preco_total = params.get('preco_total')
    if preco_total and not preco_unitario:
        preco_unitario = float(preco_total) / quantidade
    mov = MovimentacaoEstoque(
        materia_prima_id=resolvida['id'], tipo='entrada',
        quantidade=quantidade,
        preco_unitario=float(preco_unitario) if preco_unitario else None,
        referencia=(params.get('referencia') or '').strip() or None,
        usuario_id=user.id,
    )
    db.session.add(mov)
    db.session.commit()
    return {'ok': True, 'mov_id': mov.id, 'registro_tipo': 'movimentacao_estoque', 'registro_id': mov.id}


def executar_ajuste_estoque(params, user):
    from app.models import MovimentacaoEstoque
    resolvida = params.get('mp_resolvida')
    if not resolvida or not resolvida.get('id'):
        return {'ok': False, 'erro': f'MP nao identificada: {params.get("mp_nome")}'}
    quantidade = float(params.get('quantidade') or 0)
    tipo = params.get('tipo')
    motivo = (params.get('motivo') or '').strip()
    if quantidade <= 0 or tipo not in ('entrada', 'saida') or not motivo:
        return {'ok': False, 'erro': 'Parametros invalidos'}
    mov = MovimentacaoEstoque(
        materia_prima_id=resolvida['id'], tipo=tipo,
        quantidade=quantidade,
        referencia=f'Ajuste: {motivo}',
        usuario_id=user.id,
    )
    db.session.add(mov)
    db.session.commit()
    return {'ok': True, 'mov_id': mov.id, 'registro_tipo': 'movimentacao_estoque', 'registro_id': mov.id}
