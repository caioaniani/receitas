"""Tradução de registros técnicos pra linguagem natural.

Audit log, movimentações de estoque, handshake, sync de PDV — tudo passa por
aqui pra ficar legível pra quem não programa. Centraliza tambem os mapas
{tipo_tecnico → rotulo amigavel} que antes viviam duplicados em 4 templates.
"""
from datetime import date, datetime

# ── Tipos de movimentação ───────────────────────────────────────────────

TIPOS_MOV_LOJA = {
    'entrada_pedido': 'Entrada (pedido recebido)',
    'entrada_manual': 'Entrada manual',
    'ajuste_negativo': 'Ajuste negativo',
    'saida_lote': 'Saída em lote (PDV manual)',
    'venda_loja_sem_estoque': 'Saída sem estoque',
    'venda_seru': 'Venda no PDV (Seru)',
    'venda_seru_estorno': 'Estorno de venda Seru',
    'venda_seru_sem_estoque': 'Venda Seru sem estoque',
    'venda_vnda': 'Venda no site (VNDA)',
    'venda_vnda_estorno': 'Estorno de venda VNDA',
    'venda_vnda_sem_estoque': 'Venda VNDA sem estoque',
    'desperdicio': 'Desperdício',
    'desperdicio_sem_estoque': 'Desperdício sem estoque',
    'desperdicio_estorno': 'Estorno de desperdício',
    'consolidacao_estado': 'Juntou linhas duplicadas',
}

TIPOS_MOV_PRODUCAO = {
    'entrada': 'Entrada na indústria',
    'saida_pedido': 'Saída para pedido',
    'ajuste': 'Ajuste',
    'balanco': 'Balanço (correção)',
    'desperdicio': 'Desperdício',
}

TIPOS_MOV_MP = {
    'entrada': 'Entrada (compra/recebimento)',
    'saida': 'Saída (uso na produção)',
}

# ── Handshake (entrega de pedido) ───────────────────────────────────────

HANDSHAKE_ETAPAS = {
    'scan': 'QR escaneado',
    'pin_ok': 'PIN correto — entrega confirmada',
    'pin_fail': 'PIN incorreto',
    'pin_vazio': 'PIN não digitado',
    'sucesso': 'Concluído com sucesso',
    'erro_status': 'Erro de status (pedido em estado errado)',
    'erro_executor': 'Erro interno ao processar',
    'erro_loja_sem_pin': 'Loja não tem PIN cadastrado',
    'erro_tipo': 'Tipo de QR inválido',
    'scan_falha': 'Falha ao escanear',
    'forcar_entrega': 'Entrega forçada (admin)',
}

HANDSHAKE_TIPOS = {
    'saida': 'Saída da indústria',
    'entrega': 'Entrega na loja',
}

# ── Tabelas (nome friendly do registro pro audit log) ──────────────────

TABELAS_LABEL = {
    'pedido_loja': 'pedido',
    'pedido_item': 'item do pedido',
    'pedido_local': 'pedido local',
    'usuario': 'usuário',
    'funcionario': 'funcionário',
    'receita': 'receita',
    'receita_ingrediente': 'ingrediente de receita',
    'materia_prima': 'matéria-prima',
    'produto': 'produto',
    'produto_item': 'componente de produto',
    'loja': 'loja',
    'cargo': 'cargo',
    'estoque_loja': 'estoque da loja',
    'estoque_producao': 'estoque da indústria',
    'atribuicao': 'atribuição de tarefa',
    'atribuicao_entrega': 'atribuição de entrega',
    'fornecedor': 'fornecedor',
    'desperdicio': 'desperdício',
    'planejamento_producao': 'plano de produção',
    'permissao_papel': 'permissão de papel',
    'tarefa': 'tarefa',
    'preco_loja_receita': 'preço por loja',
    'loja_produto_map': 'mapeamento loja/produto',
    'seru_loja_map': 'mapeamento loja Seru',
    'seru_produto_map': 'mapeamento produto Seru',
    'vnda_produto_map': 'mapeamento produto VNDA',
    'conta_pagar': 'conta a pagar',
}

# ── Campos (nome friendly) ──────────────────────────────────────────────

CAMPOS_LABEL = {
    'data_entrega': 'data de entrega',
    'observacao': 'observação',
    'observacoes': 'observações',
    'quantidade': 'quantidade',
    'estado': 'estado',
    'status': 'status',
    'preco_venda': 'preço de venda',
    'preco_loja': 'preço de loja',
    'preco_site': 'preço de site',
    'preco_atacado': 'preço atacado',
    'nome': 'nome',
    'login': 'login',
    'papel': 'papel',
    'is_owner': 'é dono',
    'loja_id': 'loja',
    'receita_id': 'receita',
    'produto_id': 'produto',
    'materia_prima_id': 'matéria-prima',
    'driver_id': 'motorista',
    'criado_por': 'criado por',
    'modificado_por_id': 'modificado por',
    'modificado_em': 'modificado em',
    'criado_em': 'criado em',
    'ativo': 'ativo',
    'ativa': 'ativa',
    'estoque_atual': 'estoque atual',
    'estoque_minimo': 'estoque mínimo',
    'unidade': 'unidade',
    'custo_por_kg': 'custo por kg',
    'fornecedor': 'fornecedor',
    'categoria': 'categoria',
    'cargo_id': 'cargo',
    'salario_base': 'salário base',
    'data_admissao': 'admissão',
    'data_demissao': 'demissão',
    'modo_preparo': 'modo de preparo',
    'rendimento_qtd': 'rendimento (qtd)',
    'rendimento_unidade': 'rendimento (unidade)',
    'peso_base': 'peso base',
    'peso_unitario': 'peso unitário',
    'perda_percentual': 'perda %',
    'custo_embalagem': 'custo embalagem',
    'referencia': 'referência',
    'tipo': 'tipo',
    'data': 'data',
    'preco_unitario': 'preço unitário',
    'valor_total': 'valor total',
    'valor': 'valor',
    'valor_pago': 'valor pago',
    'permitido': 'permitido',
    'capacidade': 'capacidade',
    'pin': 'PIN',
    'codigo_barras': 'código de barras',
    'descricao': 'descrição',
    'telefone': 'telefone',
    'email': 'e-mail',
    'endereco': 'endereço',
    'imagem_url': 'imagem',
    'imagem_dropbox_url': 'imagem',
    'imagem_blob': 'imagem',
    'senha_hash': 'senha (hash)',
}

# Campos chatos no diff (timestamps automáticos, hashes binarios). Suprimimos
# do "diff humano" mas continuam no JSON cru pra quem quiser inspecionar.
CAMPOS_SUPRIMIDOS = {
    'modificado_em',
    'atualizado_em',
    'criado_em',
    'imagem_blob',
    'imagem_mimetype',
    'senha_hash',
}


# ── Formatação de valores ───────────────────────────────────────────────

def formatar_valor(valor):
    """Converte um valor cru (do JSON do audit) pra string legível."""
    if valor is None or valor == '':
        return '—'
    if isinstance(valor, bool):
        return 'sim' if valor else 'não'
    if isinstance(valor, datetime):
        return valor.strftime('%d/%m/%Y %H:%M')
    if isinstance(valor, date):
        return valor.strftime('%d/%m/%Y')
    s = str(valor)
    # detecta data/datetime ISO (vem como string do JSON)
    if len(s) >= 10 and s[4:5] == '-' and s[7:8] == '-':
        try:
            if 'T' in s:
                # normaliza possivel '+00:00' / 'Z' / microsegundos
                base = s.split('+')[0].rstrip('Z')
                d = datetime.fromisoformat(base[:19])
                return d.strftime('%d/%m/%Y %H:%M')
            d = datetime.strptime(s[:10], '%Y-%m-%d')
            return d.strftime('%d/%m/%Y')
        except (ValueError, TypeError):
            pass
    # trunca strings muito longas
    if len(s) > 120:
        return s[:117] + '…'
    return s


def campo_label(campo):
    return CAMPOS_LABEL.get(campo, campo.replace('_', ' '))


def tabela_label(tabela):
    return TABELAS_LABEL.get(tabela, (tabela or '').replace('_', ' '))


# ── Tradutor principal do AuditLog ──────────────────────────────────────

def traduzir_audit(log, antes, depois):
    """Frase em linguagem natural pra uma linha do AuditLog.

    `antes` e `depois` são dicts (já parseados do JSON) ou None.
    Retorna dict:
      - 'frase': str — uma linha tipo "Marina editou pedido #128: data 29/05 → 30/05"
      - 'mudancas': list — pra update, [{campo, antes, depois}, ...] (campos com mudanca real)
    """
    nome_reg = tabela_label(log.tabela)
    quem = (log.usuario.nome if log.usuario else None) or 'Sistema'
    rid = log.registro_id

    if log.acao == 'insert':
        ident = _identificador(depois) or (f'#{rid}' if rid else '')
        frase = f"{quem} criou {nome_reg} {ident}".strip()
        return {'frase': frase, 'mudancas': []}

    if log.acao == 'delete':
        ident = _identificador(antes) or (f'#{rid}' if rid else '')
        frase = f"{quem} excluiu {nome_reg} {ident}".strip()
        return {'frase': frase, 'mudancas': []}

    # update
    ident = _identificador(depois or antes) or (f'#{rid}' if rid else '')
    mudancas = _diff_campos(antes or {}, depois or {})
    if not mudancas:
        frase = f"{quem} editou {nome_reg} {ident} (sem mudanças detectadas)"
    else:
        partes = [f"{m['campo']}: {m['antes']} → {m['depois']}" for m in mudancas[:3]]
        sufixo = f", +{len(mudancas) - 3} outras" if len(mudancas) > 3 else ''
        frase = f"{quem} editou {nome_reg} {ident} — {'; '.join(partes)}{sufixo}"
    return {'frase': frase.strip(), 'mudancas': mudancas}


def _identificador(snapshot):
    """Extrai um identificador friendly do snapshot ('nome' se tiver, senão #id)."""
    if not isinstance(snapshot, dict):
        return ''
    nome = snapshot.get('nome') or snapshot.get('login')
    if nome:
        return f'"{nome}"'
    sid = snapshot.get('id')
    if sid:
        return f'#{sid}'
    return ''


def _diff_campos(antes, depois):
    """Lista de campos com mudança real, com nomes/valores formatados."""
    todos = set(antes.keys()) | set(depois.keys())
    mudancas = []
    for c in sorted(todos):
        if c in CAMPOS_SUPRIMIDOS:
            continue
        va = antes.get(c)
        vd = depois.get(c)
        if va == vd:
            continue
        mudancas.append({
            'campo': campo_label(c),
            'antes': formatar_valor(va),
            'depois': formatar_valor(vd),
        })
    return mudancas


# ── Helpers curtos pra templates dos históricos operacionais ────────────

def mov_loja_label(tipo):
    return TIPOS_MOV_LOJA.get(tipo, tipo or '')

def mov_producao_label(tipo):
    return TIPOS_MOV_PRODUCAO.get(tipo, tipo or '')

def mov_mp_label(tipo):
    return TIPOS_MOV_MP.get(tipo, tipo or '')

def handshake_etapa_label(etapa):
    return HANDSHAKE_ETAPAS.get(etapa, etapa or '')

def handshake_tipo_label(tipo):
    return HANDSHAKE_TIPOS.get(tipo, tipo or '')


def frase_mov_loja(mov, item_nome, loja_nome=None, usuario_nome=None):
    """Uma frase pra um MovEstoqueLoja: 'Maria registrou venda no PDV (Seru):
    5× Croissant na Loja Nebraska (Seru #1234)'."""
    quem = usuario_nome or 'Sistema'
    tipo = mov_loja_label(mov.tipo)
    qtd = mov.quantidade
    quando = mov.data.strftime('%H:%M') if mov.data else ''
    loja_str = f' na {loja_nome}' if loja_nome else ''
    ref = f' ({mov.referencia})' if mov.referencia else ''
    return f'{quando} — {quem}: {tipo} — {qtd}× {item_nome}{loja_str}{ref}'.strip(' —')


def frase_mov_producao(mov, item_nome, usuario_nome=None):
    quem = usuario_nome or 'Sistema'
    tipo = mov_producao_label(mov.tipo)
    quando = mov.data.strftime('%H:%M') if mov.data else ''
    ref = f' ({mov.referencia})' if mov.referencia else ''
    return f'{quando} — {quem}: {tipo} — {mov.quantidade}× {item_nome}{ref}'.strip(' —')


def frase_mov_mp(mov, mp_nome, mp_unidade=''):
    tipo = mov_mp_label(mov.tipo)
    quando = mov.data.strftime('%H:%M') if mov.data else ''
    preco = f' a R$ {mov.preco_unitario:.2f}/{mp_unidade}' if mov.preco_unitario else ''
    ref = f' ({mov.referencia})' if mov.referencia else ''
    qtd_str = f'{mov.quantidade:.1f}'
    return f'{quando} — {tipo}: {qtd_str} {mp_unidade} de {mp_nome}{preco}{ref}'.strip(' —')
