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
    orc.frete_valor = _normalizar_preco(form.get('frete_valor') or 0)
    # data_entrega: explicitamente aceita '' como "limpar" (None).
    orc.data_entrega = _parse_data(form.get('data_entrega'))
    raw_val = (form.get('validade_dias') or '').strip()
    if raw_val:
        try:
            dias = max(1, min(int(raw_val), 90))
            orc.valido_ate = orc.data + timedelta(days=dias)
        except ValueError:
            pass

    # Substitui itens: apaga os antigos PELA COLECAO (delete-orphan faz o
    # DELETE no flush). NAO trocar por db.session.delete(it) direto: o
    # delete direto nao tira o objeto de orc.itens ate o commit expirar, e
    # o recalcular_total() abaixo somava itens VELHOS + novos — caso real
    # orc-2026-0003 (18/08/2026): 200x5 editado pra 80x5 gravou subtotal/
    # total R$ 1.400 (1.000 dos deletados + 400 do novo). A venda da
    # aprovacao nao herdava o erro (criar_venda recalcula dos itens), mas
    # tela e PDF do orcamento mostravam o total inflado.
    orc.itens.clear()
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


def validar_para_aprovacao(orc):
    """Aprovar e o gesto AMARRADO aos processos (decisao do dono
    07/07/2026): fazer orcamento e leve, mas aprovar exige tudo que a
    venda/producao precisa — porque aprovar VIRA venda na hora. Devolve
    lista de erros (vazia = pode aprovar):

    - data de entrega/retirada obrigatoria (entra na fila do padeiro);
    - todo item vinculado ao catalogo (linha livre pode existir no papel,
      mas nao passa da aprovacao — nao tem estoque nem SKU de NF);
    - quantidade inteira (a venda conta unidades);
    - desconto em valor absoluto zerado — a venda nao tem esse campo;
      embuta nos precos unitarios antes de aprovar. FRETE passa direto
      (20/07/2026): a venda ganhou o campo frete_valor (soma no total,
      vai no boleto e no valor_frete da NF do Tiny) — a regra antiga
      "embuta o frete" existia so porque o campo nao existia.
    """
    erros = []
    if not (orc.cliente_id or (orc.cliente_nome or '').strip()):
        erros.append('cliente obrigatorio (cadastrado ou avulso)')
    if not orc.data_entrega:
        erros.append('informe a data de entrega/retirada (o pedido entra '
                     'na fila do padeiro por ela)')
    livres = [it.nome for it in orc.itens
              if not (it.receita_id or it.produto_id)]
    if livres:
        erros.append('itens de linha livre precisam ser amarrados ao '
                     'catalogo (ou removidos) antes de aprovar: '
                     + ', '.join(livres))
    # Item vinculado quando estava ATIVO pode ter sido arquivado/desativado
    # antes da aprovacao — a aprovacao cria VendaB2B NA HORA e o balanco de
    # producao (filtra arquivadas) nunca enxergaria o comprometido
    # (varredura 19/07/2026). Re-checa no gesto que vira dinheiro.
    mortos = []
    for it in orc.itens:
        if it.receita_id and it.receita and it.receita.arquivada_em:
            mortos.append(it.nome or it.receita.nome)
        elif it.produto_id and it.produto and not it.produto.ativo:
            mortos.append(it.nome or it.produto.nome)
    if mortos:
        erros.append('item(ns) arquivado(s)/desativado(s) no catalogo — '
                     'troque ou remova antes de aprovar: ' + ', '.join(mortos))
    fracionados = [it.nome for it in orc.itens
                   if float(it.quantidade or 0) != int(float(it.quantidade or 0))]
    if fracionados:
        erros.append('quantidade fracionada nao vira venda — arredonde: '
                     + ', '.join(fracionados))
    if not orc.itens:
        erros.append('orcamento sem itens')
    if (orc.desconto_valor or 0) > 0:
        erros.append('desconto em R$ nao existe na venda — embuta o '
                     'desconto nos precos unitarios antes de aprovar')
    return erros


def _converter_em_venda(orc, usuario_id=None):
    """Cria a VendaB2B a partir do orcamento aprovado e vincula
    (orc.venda_id). A venda nasce com a data de entrega do orcamento →
    entra na fila do padeiro SEM baixar estoque (a baixa e na separacao,
    regime 07/07/2026). Cliente mensal fica sem parcela (conta do mes);
    os demais ganham a parcela unica padrao.

    NAO commita (criar_venda com commit=False): status aprovado (claim),
    venda e vinculo persistem num commit UNICO no caller — nao existe
    janela de crash em que a venda exista orfa sem o orcamento apontar
    pra ela."""
    from app.services import vendas_b2b

    itens = [{'tipo': 'receita' if it.receita_id else 'produto',
              'id': it.receita_id or it.produto_id,
              'quantidade': int(float(it.quantidade or 0)),
              # Numeric(10,2) do orcamento segue Decimal ate a venda
              # (dinheiro nunca passa por float — CLAUDE.md).
              'preco_unitario': it.preco_unitario or Decimal('0'),
              'desconto_percentual': 0,
              'observacao': it.observacao}
             for it in orc.itens]
    venda = vendas_b2b.criar_venda(
        cliente_id=orc.cliente_id,
        cliente_nome=None if orc.cliente_id else orc.cliente_nome,
        data_entrega=orc.data_entrega,
        itens=itens,
        # Frete do orcamento vira o frete da venda (Numeric segue Decimal)
        # — o valor_total da venda fecha igual ao do orcamento.
        frete_valor=orc.frete_valor or Decimal('0'),
        observacao=f'Origem: orcamento {orc.codigo}',
        user=None,
        commit=False,
    )
    if usuario_id:
        venda.criado_por_id = usuario_id
    orc.venda_id = venda.id
    return venda


def arquivar(orc):
    """Arquiva um RASCUNHO que nao foi pra frente (pedido do dono
    08/07/2026): sai de Pendentes sem virar 'recusado' — recusado significa
    que o cliente disse nao; rascunho arquivado so morreu na gaveta.
    Devolve (ok, erro)."""
    if orc.status != 'rascunho':
        return False, ('so rascunho se arquiva — enviado/aprovado/recusado '
                       'seguem o fluxo de status')
    if orc.arquivado_em:
        return False, 'orcamento ja arquivado'
    orc.arquivado_em = agora()
    db.session.commit()
    return True, None


def desarquivar(orc):
    """Volta o rascunho arquivado pra lista de Pendentes."""
    if not orc.arquivado_em:
        return False, 'orcamento nao esta arquivado'
    orc.arquivado_em = None
    db.session.commit()
    return True, None


def marcar_status(orc, novo, *, usuario_id=None):
    """Transicao de status. Permite:
      rascunho -> enviado
      enviado -> aprovado | recusado
      enviado -> rascunho (volta pra edicao)
      aprovado/recusado -> (final, nao volta)

    APROVAR (07/07/2026): valida os dados obrigatorios (ver
    `validar_para_aprovacao`) e CRIA a venda vinculada na hora — o pedido
    entra na fila do padeiro pela data de entrega; o estoque so baixa na
    separacao. Claim, venda e vinculo persistem num commit UNICO.

    ATENCAO (contrato): nos caminhos de falha da aprovacao (claim perdido
    ou erro na conversao) ha `db.session.rollback()` — nao chame com
    mudancas pendentes de outra coisa na sessao.
    """
    if novo not in STATUS_VALIDOS:
        return False, f'status invalido: {novo}'
    if orc.arquivado_em:
        return False, 'orcamento arquivado — desarquive antes de mudar o status'
    transicoes = {
        'rascunho': {'enviado'},
        'enviado': {'aprovado', 'recusado', 'rascunho'},
        'aprovado': set(),
        'recusado': set(),
    }
    if novo not in transicoes.get(orc.status, set()):
        return False, f'transicao {orc.status} -> {novo} nao permitida'
    if novo == 'aprovado':
        erros = validar_para_aprovacao(orc)
        if erros:
            return False, ('para aprovar (e virar venda): '
                           + '; '.join(erros))
        # CLAIM atomico (mesmo padrao do Confirmar do Slack): dois POSTs
        # de aprovar quase simultaneos leriam ambos status='enviado' e
        # converteriam DUAS vezes (duas vendas na fila do padeiro). O
        # UPDATE condicional garante que so um vence; o perdedor ve
        # rowcount 0.
        from sqlalchemy import update as sa_update
        claimed = db.session.execute(
            sa_update(Orcamento)
            .where(Orcamento.id == orc.id, Orcamento.status == 'enviado')
            .values(status='aprovado', aprovado_em=agora())
            .execution_options(synchronize_session=False)
        ).rowcount
        if not claimed:
            db.session.rollback()
            return False, ('orcamento ja processado por outra acao — '
                           'recarregue a pagina')
    orc.status = novo
    if novo == 'enviado':
        orc.enviado_em = agora()
    elif novo == 'aprovado':
        orc.aprovado_em = agora()
        if not orc.venda_id:
            try:
                _converter_em_venda(orc, usuario_id=usuario_id)
            except ValueError as exc:
                db.session.rollback()
                return False, f'aprovacao abortada: {exc}'
    elif novo == 'recusado':
        orc.recusado_em = agora()
    db.session.commit()
    return True, None
