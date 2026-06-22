"""Service de Orcamento B2B (encomendas corporativas, eventos, cestas
em volume). Pre-pedido: atendente monta lista, gera PDF, manda ao cliente.

Diferenca chave pra VendaB2B: orcamento NAO baixa estoque, NAO cria
parcela financeira. So vira "real" se aprovado e convertido em venda
(conversao manual via botao na tela de detalhe — Fase 2).

Dinheiro em Decimal sempre (CLAUDE.md, peso especial).
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models import ClienteB2B, Orcamento, OrcamentoItem, Produto, Receita
from app.utils import agora, hoje

STATUS_VALIDOS = ('rascunho', 'enviado', 'aprovado', 'recusado')
# Ordem alfabetica final na lista: rascunho > enviado > aprovado/recusado.
STATUS_LABEL = {
    'rascunho': 'Rascunho',
    'enviado': 'Enviado',
    'aprovado': 'Aprovado',
    'recusado': 'Recusado',
}


def proximo_codigo():
    """Gera codigo 'ORC-YYYY-NNNN' (NNNN reseta por ano). Sequencial em cima
    do MAX existente do ano (rapido e idempotente em cima do unique do banco
    — se 2 atendentes criarem juntos, o segundo retry pega o proximo).
    """
    ano = hoje().year
    prefixo = f'ORC-{ano}-'
    ultimo = (db.session.query(func.max(Orcamento.codigo))
              .filter(Orcamento.codigo.like(f'{prefixo}%'))
              .scalar())
    seq = 1
    if ultimo:
        try:
            seq = int(ultimo.split('-')[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f'{prefixo}{seq:04d}'


def _normalizar_qtd(v):
    try:
        d = Decimal(str(v).replace(',', '.'))
        return d if d > 0 else Decimal('0')
    except Exception:  # noqa: BLE001
        return Decimal('0')


def _normalizar_preco(v):
    try:
        d = Decimal(str(v).replace(',', '.'))
        return d if d >= 0 else Decimal('0')
    except Exception:  # noqa: BLE001
        return Decimal('0')


def montar_item_payload(linha):
    """Recebe dict do form (1 linha do orcamento) e devolve dict pronto
    pro PedidoOnlineItem-like. Resolve catalogo se kind+id vier; senao,
    item livre (sem FK) com nome digitado.

    Aceita duas formas de identificar o catalogo:
      - `catalogo`: 'receita:5' | 'produto:3' | 'livre' (forma do form HTML,
        1 campo so — robusto, nao depende de JS pra separar)
      - `kind` + `id`: 'receita'|'produto'|'livre' + id (forma do copilot/API)
    Mais:
      nome: texto (sempre — pode sobrescrever o do catalogo)
      qtd, unidade, preco_unitario, observacao
    """
    # Forma combinada do form ('receita:5') tem prioridade se vier.
    catalogo = (linha.get('catalogo') or '').strip()
    if catalogo and ':' in catalogo:
        kind, _, _cid = catalogo.partition(':')
        kind = kind.strip()
        linha = {**linha, 'id': _cid.strip()}
    elif catalogo in ('receita', 'produto', 'livre'):
        kind = catalogo
    else:
        kind = (linha.get('kind') or 'livre').strip()
    nome = (linha.get('nome') or '').strip()
    receita_id = None
    produto_id = None
    if kind == 'receita':
        try:
            rid = int(linha.get('id'))
            r = Receita.query.get(rid)
            if r:
                receita_id = r.id
                if not nome:
                    nome = r.nome
        except (TypeError, ValueError):
            pass
    elif kind == 'produto':
        try:
            pid = int(linha.get('id'))
            p = Produto.query.get(pid)
            if p:
                produto_id = p.id
                if not nome:
                    nome = p.nome
        except (TypeError, ValueError):
            pass
    return {
        'receita_id': receita_id,
        'produto_id': produto_id,
        'nome': nome[:200] or '(sem nome)',
        'quantidade': _normalizar_qtd(linha.get('qtd')),
        'unidade': (linha.get('unidade') or '').strip()[:20] or None,
        'preco_unitario': _normalizar_preco(linha.get('preco_unitario')),
        'observacao': (linha.get('observacao') or '').strip()[:200] or None,
    }


def _parse_data(raw):
    """ISO 'YYYY-MM-DD' do <input type=date> -> date. Vazio/invalido -> None."""
    s = (raw or '').strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def criar_orcamento(form, itens_raw, *, usuario_id=None):
    """Cria orcamento + itens dentro de UMA transacao. Devolve (orc, erros).

    form: dict-like (request.form). itens_raw: lista de linhas (cada uma com
    kind/id/nome/qtd/preco_unitario/...).
    """
    erros = []
    cliente_id = None
    raw_cli = (form.get('cliente_id') or '').strip()
    if raw_cli:
        try:
            cliente_id = int(raw_cli)
            if not ClienteB2B.query.get(cliente_id):
                cliente_id = None
        except ValueError:
            cliente_id = None

    cliente_nome = (form.get('cliente_nome') or '').strip()
    if not cliente_id and not cliente_nome:
        erros.append('Informe o cliente (escolha um cadastrado ou digite o nome).')

    itens_norm = []
    for raw in itens_raw or []:
        item = montar_item_payload(raw)
        if item['quantidade'] <= 0:
            continue
        itens_norm.append(item)
    if not itens_norm:
        erros.append('Adicione pelo menos 1 item ao orcamento.')

    if erros:
        return None, erros

    validade_dias = 7
    raw_val = (form.get('validade_dias') or '').strip()
    if raw_val:
        try:
            validade_dias = max(1, min(int(raw_val), 90))
        except ValueError:
            validade_dias = 7

    orc = Orcamento(
        codigo=proximo_codigo(),
        data=hoje(),
        valido_ate=hoje() + timedelta(days=validade_dias),
        data_entrega=_parse_data(form.get('data_entrega')),
        cliente_id=cliente_id,
        cliente_nome=cliente_nome or None,
        cliente_documento=(form.get('cliente_documento') or '').strip() or None,
        cliente_email=(form.get('cliente_email') or '').strip() or None,
        cliente_telefone=(form.get('cliente_telefone') or '').strip() or None,
        cliente_endereco=(form.get('cliente_endereco') or '').strip() or None,
        desconto_valor=_normalizar_preco(form.get('desconto_valor') or 0),
        frete_valor=_normalizar_preco(form.get('frete_valor') or 0),
        observacao=(form.get('observacao') or '').strip() or None,
        criado_por_id=usuario_id,
    )
    db.session.add(orc)
    db.session.flush()
    for it in itens_norm:
        oi = OrcamentoItem(orcamento_id=orc.id, **it)
        oi.recalcular_subtotal()
        orc.itens.append(oi)
    orc.recalcular_total()
    db.session.commit()
    return orc, []


def atualizar_orcamento(orc, form, itens_raw):
    """Reescreve campos editaveis + substitui a lista de itens. So permite
    se status = 'rascunho' ou 'enviado'. Devolve (ok, erros).
    """
    if orc.status not in ('rascunho', 'enviado'):
        return False, ['Orcamento ja foi aprovado/recusado — nao editavel.']

    raw_cli = (form.get('cliente_id') or '').strip()
    try:
        orc.cliente_id = int(raw_cli) if raw_cli else None
    except ValueError:
        orc.cliente_id = None
    orc.cliente_nome = (form.get('cliente_nome') or '').strip() or None
    orc.cliente_documento = (form.get('cliente_documento') or '').strip() or None
    orc.cliente_email = (form.get('cliente_email') or '').strip() or None
    orc.cliente_telefone = (form.get('cliente_telefone') or '').strip() or None
    orc.cliente_endereco = (form.get('cliente_endereco') or '').strip() or None
    orc.observacao = (form.get('observacao') or '').strip() or None
    orc.desconto_valor = _normalizar_preco(form.get('desconto_valor') or 0)
    # data_entrega: explicitamente aceita '' como "limpar" (None).
    orc.data_entrega = _parse_data(form.get('data_entrega'))
    raw_val = (form.get('validade_dias') or '').strip()
    if raw_val:
        try:
            dias = max(1, min(int(raw_val), 90))
            orc.valido_ate = orc.data + timedelta(days=dias)
        except ValueError:
            pass

    # Substitui itens: apaga os antigos, adiciona os novos.
    for it in list(orc.itens):
        db.session.delete(it)
    db.session.flush()

    itens_norm = []
    for raw in itens_raw or []:
        item = montar_item_payload(raw)
        if item['quantidade'] <= 0:
            continue
        itens_norm.append(item)
    if not itens_norm:
        return False, ['Adicione pelo menos 1 item ao orcamento.']

    for it in itens_norm:
        oi = OrcamentoItem(orcamento_id=orc.id, **it)
        oi.recalcular_subtotal()
        orc.itens.append(oi)
    orc.recalcular_total()
    db.session.commit()
    return True, []


def marcar_status(orc, novo, *, usuario_id=None):
    """Transicao de status. Permite:
      rascunho -> enviado
      enviado -> aprovado | recusado
      enviado -> rascunho (volta pra edicao)
      aprovado/recusado -> (final, nao volta)
    """
    if novo not in STATUS_VALIDOS:
        return False, f'status invalido: {novo}'
    transicoes = {
        'rascunho': {'enviado'},
        'enviado': {'aprovado', 'recusado', 'rascunho'},
        'aprovado': set(),
        'recusado': set(),
    }
    if novo not in transicoes.get(orc.status, set()):
        return False, f'transicao {orc.status} -> {novo} nao permitida'
    orc.status = novo
    if novo == 'enviado':
        orc.enviado_em = agora()
    elif novo == 'aprovado':
        orc.aprovado_em = agora()
    elif novo == 'recusado':
        orc.recusado_em = agora()
    db.session.commit()
    return True, None
