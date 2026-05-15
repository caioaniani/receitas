"""Renderers Block Kit pra previews/resultados das tools no Slack.

Cada tool de WRITE precisa de uma preview com botoes Confirmar/Cancelar.
Tools de READ usam markdown simples.

Como funciona:
- `build_preview(tipo_acao, params, token)` → dict Block Kit
- `build_resultado(tipo_acao, resultado)` → blocks de sucesso/erro
- `build_texto(texto_md)` → blocks de texto simples (read tools)

Convencao do botao: action_id='copilot_confirmar' ou 'copilot_cancelar',
value=token. O blueprint /slack/interact resolve o token → SlackAcaoPendente.
"""

# ── Helpers ────────────────────────────────────────────────────────────


def _section(text_md):
    return {'type': 'section',
            'text': {'type': 'mrkdwn', 'text': text_md[:3000]}}


def _header(text):
    return {'type': 'header',
            'text': {'type': 'plain_text', 'text': text[:150]}}


def _divider():
    return {'type': 'divider'}


def _fields(pares):
    """Lista de (label, valor) → section com fields markdown."""
    return {
        'type': 'section',
        'fields': [
            {'type': 'mrkdwn', 'text': f'*{lbl}:*\n{val}'[:2000]}
            for lbl, val in pares if val is not None and val != ''
        ][:10],
    }


def _botoes(token, label_confirmar='Confirmar', label_cancelar='Cancelar'):
    return {
        'type': 'actions',
        'elements': [
            {'type': 'button', 'style': 'primary',
             'text': {'type': 'plain_text', 'text': label_confirmar},
             'action_id': 'copilot_confirmar', 'value': token},
            {'type': 'button', 'style': 'danger',
             'text': {'type': 'plain_text', 'text': label_cancelar},
             'action_id': 'copilot_cancelar', 'value': token},
        ],
    }


def _ctx(texto):
    return {'type': 'context',
            'elements': [{'type': 'mrkdwn', 'text': texto[:300]}]}


# ── Previews por tool de write ─────────────────────────────────────────


def _preview_criar_pedido(p, token):
    loja = (p.get('loja_resolvida') or {}).get('nome') or (
        f'id={p["loja_id"]}' if p.get('loja_id') else '(escolher)')
    itens_txt = '\n'.join(
        f"- {it.get('quantidade')}x {(it.get('resolvido') or {}).get('nome') or it.get('nome_original') or '?'}"
        for it in (p.get('itens') or [])
    ) or '(vazio)'
    blocks = [
        _header('Criar pedido'),
        _fields([
            ('Loja', loja),
            ('Data entrega', p.get('data_entrega') or '?'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _section(f'*Itens:*\n{itens_txt[:2000]}'),
        _botoes(token, 'Criar pedido', 'Cancelar'),
    ]
    return blocks


def _preview_receber_mp(p, token):
    mp_nome = (p.get('mp_resolvida') or {}).get('nome') or p.get('mp_nome') or '?'
    blocks = [
        _header('Receber materia-prima'),
        _fields([
            ('MP', mp_nome),
            ('Quantidade', f"{p.get('quantidade')} {(p.get('mp_resolvida') or {}).get('unidade') or ''}"),
            ('Preco total', f"R$ {p.get('preco_total')}" if p.get('preco_total') is not None else None),
            ('Fornecedor', p.get('fornecedor') or '—'),
            ('Referencia', p.get('referencia') or '—'),
        ]),
        _botoes(token, 'Receber', 'Cancelar'),
    ]
    return blocks


def _preview_ajuste_estoque(p, token):
    mp_nome = (p.get('mp_resolvida') or {}).get('nome') or p.get('mp_nome') or '?'
    sinal = '+' if p.get('tipo') == 'entrada' else '-'
    return [
        _header('Ajuste de estoque'),
        _fields([
            ('MP', mp_nome),
            ('Operacao', f"{sinal}{p.get('quantidade')} ({p.get('tipo')})"),
            ('Motivo', p.get('motivo') or '—'),
        ]),
        _botoes(token, 'Aplicar', 'Cancelar'),
    ]


def _preview_mudar_status_pedido(p, token):
    return [
        _header('Mudar status do pedido'),
        _fields([
            ('Pedido ID', p.get('pedido_id')),
            ('Novo status', p.get('novo_status')),
        ]),
        _botoes(token, 'Aplicar', 'Cancelar'),
    ]


def _preview_criar_fornecedor(p, token):
    return [
        _header('Cadastrar fornecedor'),
        _fields([
            ('Nome', p.get('nome')),
            ('CNPJ', p.get('cnpj') or '—'),
            ('Telefone', p.get('telefone') or '—'),
            ('Contato', p.get('contato') or '—'),
            ('Email', p.get('email') or '—'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _botoes(token, 'Cadastrar', 'Cancelar'),
    ]


def _preview_marcar_ponto(p, token):
    return [
        _header('Marcar ponto'),
        _fields([
            ('Funcionario', p.get('funcionario_nome') or '?'),
            ('Tipo', p.get('tipo') or '?'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _botoes(token, 'Registrar', 'Cancelar'),
    ]


def _preview_criar_tarefa(p, token):
    return [
        _header('Criar tarefa'),
        _fields([
            ('Titulo', p.get('titulo')),
            ('Projeto', p.get('projeto_nome') or 'Inbox'),
            ('Prazo', p.get('prazo') or '—'),
            ('Prioridade', p.get('prioridade') or '—'),
        ]),
        _botoes(token, 'Criar', 'Cancelar'),
    ]


def _preview_marcar_tarefa_feita(p, token):
    return [
        _header('Marcar tarefa como feita'),
        _fields([
            ('Tarefa ID', p.get('tarefa_id')),
            ('Titulo', p.get('titulo') or '?'),
        ]),
        _botoes(token, 'Marcar feita', 'Cancelar'),
    ]


def _preview_balanco_congelados(p, token):
    totais = p.get('totais') or {}
    itens = p.get('itens') or []
    resumo = '\n'.join(
        f"- {(i.get('resolvido') or {}).get('nome') or i.get('nome') or '?'}: {i.get('quantidade')}"
        for i in itens[:20]
    )
    if len(itens) > 20:
        resumo += f'\n_... +{len(itens) - 20} itens_'
    return [
        _header('Balanco de congelados'),
        _fields([
            ('Total itens', totais.get('total_itens')),
            ('Resolvidos', totais.get('resolvidos')),
            ('Nao resolvidos', totais.get('nao_resolvidos')),
            ('Delta total', totais.get('delta_total')),
        ]),
        _section(f'*Itens:*\n{resumo[:2500] or "(vazio)"}'),
        _botoes(token, 'Aplicar balanco', 'Cancelar'),
    ]


def _preview_entrada_lote_loja(p, token):
    itens = p.get('itens') or []
    loja = p.get('loja_nome') or f'id={p.get("loja_id")}'
    resumo = '\n'.join(
        f"- {i.get('nome') or '?'}: +{i.get('quantidade')}"
        for i in itens[:20]
    )
    if len(itens) > 20:
        resumo += f'\n_... +{len(itens) - 20}_'
    return [
        _header('Entrada em lote na loja'),
        _fields([('Loja', loja), ('Referencia', p.get('referencia') or '—')]),
        _section(f'*Itens:*\n{resumo[:2500] or "(vazio)"}'),
        _botoes(token, 'Aplicar entrada', 'Cancelar'),
    ]


def _preview_receber_pedido(p, token):
    return [
        _header('Receber pedido'),
        _fields([
            ('Pedido ID', p.get('pedido_id')),
        ]),
        _section(':information_source: Soma no estoque da loja. Sem divergencia.'),
        _botoes(token, 'Confirmar recebimento', 'Cancelar'),
    ]


def _preview_anexar_foto_pedido(p, token):
    n = p.get('_n_imagens') or 0
    return [
        _header('Anexar foto ao pedido'),
        _fields([
            ('Pedido ID', p.get('pedido_id')),
            ('Fotos a anexar', n if n else '?'),
        ]),
        _botoes(token, 'Anexar', 'Cancelar'),
    ]


def _preview_registrar_desperdicio(p, token):
    return [
        _header('Registrar desperdicio'),
        _fields([
            ('Loja', p.get('loja_nome') or f'id={p.get("loja_id")}' if p.get('loja_id') else '?'),
            ('Item', p.get('item_nome')),
            ('Quantidade', p.get('quantidade')),
            ('Motivo', p.get('motivo') or 'vencido'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _botoes(token, 'Registrar', 'Cancelar'),
    ]


_PREVIEW_BUILDERS = {
    'criar_pedido': _preview_criar_pedido,
    'receber_mp': _preview_receber_mp,
    'ajuste_estoque': _preview_ajuste_estoque,
    'mudar_status_pedido': _preview_mudar_status_pedido,
    'criar_fornecedor': _preview_criar_fornecedor,
    'marcar_ponto': _preview_marcar_ponto,
    'criar_tarefa': _preview_criar_tarefa,
    'marcar_tarefa_feita': _preview_marcar_tarefa_feita,
    'balanco_congelados': _preview_balanco_congelados,
    'entrada_lote_loja': _preview_entrada_lote_loja,
    'registrar_desperdicio': _preview_registrar_desperdicio,
    'anexar_foto_pedido': _preview_anexar_foto_pedido,
    'receber_pedido': _preview_receber_pedido,
}

# Chaves de params que NUNCA devem aparecer no preview generico (sao grandes/internas).
_SKIP_KEYS_PREVIEW = {'imagens'}


def build_preview(tipo_acao, params, token, explicacao=None):
    """Constroi Block Kit pra preview de write tool. Inclui explicacao."""
    builder = _PREVIEW_BUILDERS.get(tipo_acao)
    blocks = []
    if explicacao:
        blocks.append(_section(_md_pra_slack(explicacao)))
    if builder:
        blocks.extend(builder(params, token))
    else:
        # Fallback generico: lista params + botoes (pula chaves internas/grandes)
        blocks.append(_header(f'Confirmar acao: {tipo_acao}'))
        pares = [(k, v) for k, v in params.items()
                 if not k.startswith('_') and k not in _SKIP_KEYS_PREVIEW
                 and v not in (None, '')]
        blocks.append(_fields(pares))
        blocks.append(_botoes(token, 'Confirmar', 'Cancelar'))
    blocks.append(_ctx('_acao expira em 10min_'))
    return blocks


def build_resultado(resultado, ok=True):
    """Apos clique de botao, monta blocks pra chat.update."""
    if resultado is None:
        resultado = {}
    if ok and resultado.get('ok'):
        partes = ['Feito.']
        for k in ('pedido_id', 'mov_id', 'tarefa_id', 'desperdicio_id', 'fornecedor_id'):
            if resultado.get(k):
                partes.append(f'_{k}_: `{resultado[k]}`')
        if resultado.get('url'):
            partes.append(f"<{resultado['url']}|abrir no sistema>")
        return [_section(' · '.join(partes))]
    erro = resultado.get('erro') if isinstance(resultado, dict) else str(resultado)
    return [_section(f':warning: Erro: {erro or "desconhecido"}')]


def build_texto(texto_md):
    """Read tool ou conversa pura — markdown convertido pro estilo Slack."""
    if not texto_md:
        return [_section('_(sem resposta)_')]
    md = _md_pra_slack(texto_md)
    # Slack tem limite de 3000 chars por section — quebra se preciso
    if len(md) <= 2900:
        return [_section(md)]
    chunks = []
    while md:
        chunks.append(_section(md[:2900]))
        md = md[2900:]
        if len(chunks) >= 10:
            break
    return chunks


def build_cancelado():
    return [_section(':x: Cancelado.')]


def build_expirado():
    return [_section(':clock1: Acao expirou. Mande de novo se ainda quiser.')]


# ── Conversao de markdown ──────────────────────────────────────────────


def _md_pra_slack(texto):
    """Slack tem mrkdwn proprio: **bold** -> *bold*, etc.

    Conversao defensiva: nao quebra se o texto ja vier em Slack mrkdwn.
    """
    if not texto:
        return ''
    # **bold** -> *bold*
    import re
    out = re.sub(r'\*\*([^*\n]+?)\*\*', r'*\1*', texto)
    # __italic__ -> _italic_
    out = re.sub(r'__([^_\n]+?)__', r'_\1_', out)
    # # heading -> *heading*
    out = re.sub(r'^(#{1,6})\s+(.+)$', r'*\2*', out, flags=re.MULTILINE)
    # [text](url) -> <url|text>
    out = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', out)
    return out
