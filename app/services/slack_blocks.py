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
    loja = (p.get('loja_resolvida') or {}).get('nome')
    if not loja and p.get('loja_id'):
        from app.models import Loja
        lobj = Loja.query.get(p['loja_id'])
        loja = lobj.nome if lobj else f'id={p["loja_id"]}'
    if not loja:
        loja = '(escolher)'
    def _fmt_item(it):
        from app.constants import render_item_com_estado
        nome_base = (it.get('resolvido') or {}).get('nome') or it.get('nome_original') or '?'
        nome = render_item_com_estado(nome_base, it.get('estado'))
        base = f"- {it.get('quantidade')}x {nome}"
        obs = (it.get('observacao') or '').strip()
        return f"{base} _({obs})_" if obs else base
    itens_txt = '\n'.join(_fmt_item(it) for it in (p.get('itens') or [])) or '(vazio)'
    blocks = [
        _header('Criar pedido'),
        _fields([
            ('Loja', loja),
            ('Data entrega', p.get('data_entrega') or '?'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _section(f'*Itens:*\n{itens_txt[:2000]}'),
    ]
    if p.get('merge_pedido_id'):
        blocks.append(_section(
            f'⚠️ Ja existe o pedido *#{p["merge_pedido_id"]}* pra essa loja nessa '
            'data — os itens serao *adicionados nele* (nao cria pedido novo).'))
        label_ok = f'Adicionar ao #{p["merge_pedido_id"]}'
    else:
        label_ok = 'Criar pedido'
    blocks.append(_botoes(token, label_ok, 'Cancelar'))
    return blocks


def _preview_editar_pedido(p, token):
    from app.constants import render_item_com_estado
    pid = p.get('pedido_id') or '?'
    atual = p.get('pedido_atual') or {}
    loja = atual.get('loja_nome') or '?'
    status = atual.get('status') or '?'

    # Diff de data
    data_at = atual.get('data_entrega') or '?'
    data_nova = p.get('data_entrega')
    data_txt = data_at if not data_nova or data_nova == data_at else f'{data_at} → *{data_nova}*'

    # Diff de obs
    obs_at = atual.get('observacao') or '—'
    obs_nova_raw = p.get('observacao')
    if obs_nova_raw is None:
        obs_txt = obs_at
    else:
        obs_norm = (obs_nova_raw or '').strip() or '—'
        obs_txt = obs_at if obs_norm == obs_at else f'{obs_at} → *{obs_norm}*'

    # Itens: se itens_novos veio, mostra novos. Senao mostra atuais.
    itens_novos = p.get('itens')
    if itens_novos is not None:
        def _fmt_novo(it):
            nome_base = (it.get('resolvido') or {}).get('nome') or it.get('nome_original') or '?'
            nome = render_item_com_estado(nome_base, it.get('estado'))
            base = f"- {it.get('quantidade')}x {nome}"
            obs = (it.get('observacao') or '').strip()
            return f"{base} _({obs})_" if obs else base
        itens_txt = '\n'.join(_fmt_novo(it) for it in itens_novos) or '(vazio)'
        itens_header = '*Itens NOVOS (substituem os atuais):*'
    else:
        def _fmt_atual(it):
            nome = render_item_com_estado(it.get('nome') or '?', it.get('estado'))
            base = f"- {it.get('quantidade')}x {nome}"
            obs = (it.get('observacao') or '').strip()
            return f"{base} _({obs})_" if obs else base
        itens_txt = '\n'.join(_fmt_atual(it) for it in (atual.get('itens') or [])) or '(vazio)'
        itens_header = '*Itens (sem alteracao):*'

    blocks = [
        _header(f'Editar pedido #{pid}'),
        _fields([
            ('Loja', loja),
            ('Status', status),
            ('Data entrega', data_txt),
            ('Observacao', obs_txt),
        ]),
        _section(f'{itens_header}\n{itens_txt[:2000]}'),
        _botoes(token, 'Salvar alteracoes', 'Cancelar'),
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


def _preview_registrar_desperdicio_lote(p, token):
    itens = p.get('itens') or []
    totais = p.get('totais') or {}
    loja = p.get('loja_nome') or (f'id={p.get("loja_id")}' if p.get('loja_id') else '?')
    motivo = p.get('motivo') or 'vencido'
    n_ok = totais.get('resolvidos') or 0
    n_nao = totais.get('nao_resolvidos') or 0
    total_qtd = totais.get('delta_total') or 0

    def _fmt(i):
        nome = (i.get('resolvido') or {}).get('nome') or i.get('nome') or '?'
        qtd = i.get('quantidade')
        obs = i.get('observacao')
        marker = ''
        if i.get('erro'):
            marker = ' ⚠'
        elif not i.get('resolvido'):
            marker = ' ⚠ (nao encontrado)'
        base = f"- {qtd}x {nome}{marker}"
        return f"{base} _({obs})_" if obs else base

    resumo = '\n'.join(_fmt(i) for i in itens[:25])
    if len(itens) > 25:
        resumo += f'\n_... +{len(itens) - 25} itens_'
    return [
        _header('Registrar desperdicio em lote'),
        _fields([
            ('Loja', loja),
            ('Motivo', motivo),
            ('Itens com match', n_ok),
            ('Nao encontrados', n_nao),
            ('Total a baixar', f'-{total_qtd}'),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _section(f'*Itens:*\n{resumo[:2500] or "(vazio)"}'),
        _botoes(token, f'Registrar {n_ok} item(s)', 'Cancelar'),
    ]


def _preview_criar_venda_b2b(p, token):
    itens = p.get('itens') or []
    parcelas = p.get('parcelas') or []
    cliente = p.get('cliente_nome_resolvido') or p.get('cliente_nome') or '?'
    tag_cli = '(avulso)' if p.get('cliente_avulso') else '(cadastrado)'
    desc = p.get('cliente_desconto') or 0
    extra = f' · {desc:.0f}% desc' if desc else ''

    def _fmt(it):
        nome = (it.get('resolvido') or {}).get('nome') or it.get('nome_original') or '?'
        est = it.get('estado')
        tag_est = f' [{est.upper()}]' if est else ''
        marker = '' if it.get('resolvido') else ' ⚠'
        preco = it.get('preco_unitario') or 0
        subt = it.get('subtotal') or 0
        return f"- {it.get('quantidade')}x {nome}{tag_est}{marker} · R$ {preco:.2f} = R$ {subt:.2f}"

    itens_txt = '\n'.join(_fmt(i) for i in itens[:25]) or '(vazio)'
    if len(itens) > 25:
        itens_txt += f'\n_... +{len(itens) - 25}_'

    parc_txt = ''
    if parcelas:
        parc_txt = '\n*Parcelas:*\n' + '\n'.join(
            f"- {p.get('vencimento')} · R$ {float(p.get('valor') or 0):.2f}"
            + (f" ({p.get('forma_pagamento')})" if p.get('forma_pagamento') else '')
            for p in parcelas[:10]
        )

    return [
        _header('Criar venda B2B'),
        _fields([
            ('Cliente', f'{cliente} {tag_cli}{extra}'),
            ('Data', p.get('data_venda') or 'hoje'),
            ('Entrega (padaria)', p.get('data_entrega') or '⚠ informe'),
            ('NF', p.get('nf_numero') or '—'),
            ('Total', f"R$ {p.get('total') or 0:.2f}"),
            ('Observacao', p.get('observacao') or '—'),
        ]),
        _section(f'*Itens (baixa do freezer):*\n{itens_txt[:2500]}{parc_txt[:1000]}'),
        _botoes(token, 'Criar venda', 'Cancelar'),
    ]


def _preview_criar_cliente_b2b(p, token):
    return [
        _header('Cadastrar cliente B2B'),
        _fields([
            ('Nome', p.get('nome')),
            ('CNPJ/CPF', p.get('cnpj_cpf') or '—'),
            ('Telefone', p.get('telefone') or '—'),
            ('Contato', p.get('contato') or '—'),
            ('Desconto', f"{p.get('desconto_percentual') or 0:.0f}%"),
            ('E-mail', p.get('email') or '—'),
        ]),
        _botoes(token, 'Cadastrar', 'Cancelar'),
    ]


_PREVIEW_BUILDERS = {
    'criar_pedido': _preview_criar_pedido,
    'editar_pedido': _preview_editar_pedido,
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
    'registrar_desperdicio_lote': _preview_registrar_desperdicio_lote,
    'criar_venda_b2b': _preview_criar_venda_b2b,
    'criar_cliente_b2b': _preview_criar_cliente_b2b,
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
        # Mensagem inicial personalizada por tipo de acao
        STATUS_LABEL = {
            'pendente': 'pedido feito',
            'confirmado': 'pedido feito',
            # 'separado' = momento em que o QR Code é gerado pro motorista.
            # Pro usuario isso eh 'enviado' (saiu da industria, motorista
            # ja tem o QR pra apresentar na loja).
            'separado': 'enviado',
            'em_transporte': 'enviado',
            'entregue': 'recebido',
            'cancelado': 'cancelado',
        }
        if resultado.get('pedido_id') and resultado.get('novo_status'):
            label = STATUS_LABEL.get(resultado['novo_status'], resultado['novo_status'])
            partes = [f"✓ pedido #{resultado['pedido_id']} marcado como *{label}*."]
            label_botao = 'Abrir pedido'
        elif resultado.get('pedido_id'):
            partes = [f"✓ pedido #{resultado['pedido_id']} criado."]
            label_botao = 'Abrir pedido'
        elif resultado.get('venda_id'):
            partes = [f"✓ venda B2B #{resultado['venda_id']} criada."]
            label_botao = 'Abrir venda'
        elif resultado.get('desperdicio_id') or resultado.get('total_aplicados'):
            n = resultado.get('total_aplicados') or 1
            loja_nome = resultado.get('loja') or ''
            sufixo = f' em {loja_nome}' if loja_nome else ''
            partes = [f"✓ {n} desperdicio(s) registrado(s){sufixo}."]
            label_botao = 'Ver desperdícios'
        else:
            partes = ['Feito.']
            label_botao = 'Abrir no sistema'
        for k in ('mov_id', 'tarefa_id', 'fornecedor_id', 'cliente_id'):
            if resultado.get(k):
                partes.append(f'_{k}_: `{resultado[k]}`')

        blocks = [_section(' · '.join(partes))]

        # Botao "abrir no sistema" — mais visivel que link inline, principalmente
        # no mobile do Slack.
        if resultado.get('url'):
            url = resultado['url']
            if url.startswith('/'):
                import os
                base = (os.environ.get('APP_BASE_URL')
                        or 'https://gestao.opaopadariaartesanal.com.br').rstrip('/')
                url = f'{base}{url}'
            blocks.append({
                'type': 'actions',
                'elements': [{
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': label_botao},
                    'url': url,
                    'style': 'primary',
                }],
            })
        # Se gerou QR Code (ex: separou pedido → QR saida pro motorista),
        # mostra a imagem inline pro motorista escanear no celular.
        if resultado.get('qr_png_url'):
            blocks.append(_section(
                ':qrcode: *Pedido enviado.* Motorista escaneia o QR abaixo + digita o PIN.\n'
                f'<{resultado.get("qr_url", "")}|abrir pagina>'
            ))
            blocks.append({
                'type': 'image',
                'image_url': resultado['qr_png_url'],
                'alt_text': 'QR Code de saida do pedido',
            })
            blocks.append(_section(
                ':information_source: Depois de escanear, o motorista verá um '
                'botão no celular pra gerar o *QR de entrega*. Quando chegar na '
                'loja, alguém escaneia esse QR + digita o PIN da loja pra '
                'finalizar como *recebido* e somar no estoque automaticamente.'
            ))
        return blocks
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
