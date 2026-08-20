"""Divulgacao — pedido "como do site" SEM pagamento (brinde/PR).

Pedido do dono (21/07/2026): lancar um pedido igual ao do site (destinatario,
entrega ou retirada, data, itens), mas SEM etapa de pagamento; ele aparece no
/entregas/painel como um pedido normal, marcado com uma ESTRELA pra a equipe
saber que e divulgacao, nao venda.

Decisoes do dono (AskUserQuestion 21/07/2026):
- BAIXA o estoque fisico da loja (o pao sai pela porta), mas o movimento fica
  MARCADO como divulgacao (canal 'divulgacao' no motor unico `baixa_venda`),
  logo FORA da previsao de venda (tipo `venda_site_divulgacao` nao entra em
  VENDA_TIPOS_DEMANDA_*) e de qualquer relatorio que filtra 'venda_site'.
- Lancado por uma TELA ADMIN (`/admin/loja-online/divulgacao`).

FORA de faturamento: o `PedidoOnline` nasce com `pago_em=NULL` (nunca foi pago)
e `status='divulgacao'` — as somas de faturamento do site filtram por `pago_em`,
entao a divulgacao ja sai delas; os pontos que contam por outra chave ganharam
guard explicito da flag (`briefing_dono`, `chatbot_auditor._funil_site`).

NAO emite NF, NAO cobra, NAO manda e-mail de confirmacao. A loja de baixa segue
a MESMA regra do site: retirada baixa da loja escolhida; entrega/express baixa
de `loja_origem_site()`.
"""
import logging

from app.extensions import db
from app.models import PedidoOnline, PedidoOnlineItem, Produto, Receita
from app.utils import agora

logger = logging.getLogger(__name__)

STATUS_DIVULGACAO = 'divulgacao'
# Email placeholder do pedido (campo NOT NULL do modelo). Nunca recebe e-mail —
# divulgacao nao dispara confirmacao. So preenche a coluna.
EMAIL_PLACEHOLDER = 'divulgacao@opao.online'
MODOS_ENTREGA_DIVULGACAO = ('agendada', 'retirada', 'express')


def _loja_baixa(pedido):
    """Loja de onde a divulgacao baixa estoque (mesma regra do site):
    retirada baixa da loja ESCOLHIDA; entrega/express de `loja_origem_site()`."""
    from app.services.loja_pagamento import loja_origem_site
    if pedido.modo_entrega == 'retirada':
        return pedido.loja_retirada
    return loja_origem_site()


def _resolver_item(kind, item_id):
    """(obj, nome, preco_snapshot) do item do catalogo. So itens ATIVOS
    (helpers canonicos) — divulgacao e fluxo NOVO, nunca ressuscita arquivado.
    Preco = preco_site (referencia do valor doado; nao entra em faturamento)."""
    if kind == 'receita':
        obj = Receita.ativas().filter_by(id=item_id).first()
        if obj:
            return obj, obj.nome, float(obj.preco_site or 0)
    elif kind == 'produto':
        obj = Produto.query.filter_by(id=item_id, ativo=True).first()
        if obj:
            return obj, obj.nome, float(obj.preco_site or 0)
    return None, None, 0.0


def criar_divulgacao(*, itens, modo_entrega='agendada', loja_retirada_id=None,
                     nome_destinatario, telefone=None, data_entrega=None,
                     janela_entrega=None, endereco=None, cartinha=None,
                     usuario_id=None, permitir_hoje=False):
    """Cria o pedido de divulgacao E baixa o estoque numa transacao unica.

    `itens`: lista de {'kind': 'receita'|'produto', 'id': int, 'qtd': int}.
    Item de MENU configuravel aceita tambem 'comp' ({produto_item_id: qtd},
    a escolha dos minis) — validada como no site (total exato); ausente =
    pre-selecao do cadastro.
    `endereco`: dict com as chaves do snapshot (entrega/express); None na
    retirada. Levanta ValueError em entrada invalida (o caller flasheia).

    Retorna o PedidoOnline criado.
    """
    from decimal import Decimal

    if modo_entrega not in MODOS_ENTREGA_DIVULGACAO:
        raise ValueError('modo de entrega invalido')
    # Todos os campos sao OBRIGATORIOS (decisao do dono 21/07/2026).
    nome_destinatario = (nome_destinatario or '').strip()
    if not nome_destinatario:
        raise ValueError('informe o nome de quem recebe')
    if not (telefone or '').strip():
        raise ValueError('informe o telefone')
    if data_entrega is None:
        raise ValueError('informe a data de entrega')
    # Data minima por papel (decisao do dono 08/08/2026: "eu como owner devo
    # conseguir lancar para quando quiser"): com `permitir_hoje` (o DONO), a
    # divulgacao pode ser pra HOJE — o painel de entregas já mostra o dia
    # corrente e as janelas de hoje vem cortadas pelo horario (mesma regra do
    # site). Sem a flag (papel marketing), vale a regra original de 21/07:
    # nunca no mesmo dia. Passado nao existe pra ninguem — nao ha o que
    # entregar ontem.
    from app.utils import hoje as _hoje
    if permitir_hoje:
        if data_entrega < _hoje():
            raise ValueError('a data não pode ser no passado')
    elif data_entrega <= _hoje():
        raise ValueError('a data tem que ser a partir de amanhã')
    if not (janela_entrega or '').strip():
        raise ValueError('informe a janela/horario')
    if modo_entrega == 'retirada':
        if not loja_retirada_id:
            raise ValueError('escolha a loja de retirada')
    else:
        e = endereco or {}
        faltando = [c for c in ('cep', 'logradouro', 'numero', 'bairro',
                                'cidade', 'uf')
                    if not (e.get(c) or '').strip()]
        if faltando:
            raise ValueError('endereco incompleto (%s)' % ', '.join(faltando))

    # Resolve itens ANTES de criar o pedido (falha cedo, sem lixo no banco).
    from app.services import loja_menu
    linhas = []
    for it in (itens or []):
        try:
            qtd = int(it.get('qtd') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        obj, nome, preco = _resolver_item(it.get('kind'), it.get('id'))
        if obj is None:
            raise ValueError('item invalido ou arquivado: %s' % it.get('id'))
        comp = None
        if it.get('kind') == 'produto' and loja_menu.eh_menu(obj):
            # MENU CONFIGURAVEL (20/08/2026, caso pedido 24FB0FFB — dono:
            # "nao apareceu os minis pra eu selecionar como no site"): mesma
            # autoridade do checkout — normaliza a escolha contra o cadastro,
            # exige o total EXATO e o valor de referencia vira a soma dos
            # `preco_menu` escolhidos. Sem escolha nenhuma vale a
            # PRE-SELECAO (mesmo contrato do site: nao mexeu = padrao).
            comp = loja_menu.normalizar(obj, it.get('comp'))
            erro = loja_menu.validar(obj, comp)
            if erro:
                raise ValueError(erro)
            preco_menu = loja_menu.preco(obj, comp)
            if preco_menu is None:
                raise ValueError('%s tem item escolhido sem preço cadastrado '
                                 '— ajuste na tela da cesta' % obj.nome)
            preco = preco_menu           # Decimal — referencia do valor doado
        linhas.append((it['kind'], obj, nome, preco, qtd, comp))
    if not linhas:
        raise ValueError('adicione ao menos um item')

    endereco = endereco or {}
    pedido = PedidoOnline(
        divulgacao=True,
        status=STATUS_DIVULGACAO,
        pago_em=None,                         # nunca foi pago (fora do faturamento)
        nome_cliente=nome_destinatario,
        email_cliente=EMAIL_PLACEHOLDER,
        telefone_cliente=(telefone or '').strip() or None,
        modo_entrega=modo_entrega,
        loja_retirada_id=(loja_retirada_id if modo_entrega == 'retirada'
                          else None),
        data_entrega=data_entrega,
        janela_entrega=(janela_entrega or '').strip() or None,
        cartinha=(cartinha or '').strip() or None,
        subtotal=Decimal('0'), frete_valor=Decimal('0'),
        valor_total=Decimal('0'),
    )
    if modo_entrega != 'retirada':
        pedido.endereco_entrega = (endereco.get('linha') or '').strip() or None
        pedido.endereco_cep = (endereco.get('cep') or '').strip() or None
        pedido.endereco_logradouro = (endereco.get('logradouro') or '').strip() or None
        pedido.endereco_numero = (endereco.get('numero') or '').strip() or None
        pedido.endereco_complemento = (endereco.get('complemento') or '').strip() or None
        pedido.endereco_bairro = (endereco.get('bairro') or '').strip() or None
        pedido.endereco_cidade = (endereco.get('cidade') or '').strip() or None
        pedido.endereco_uf = (endereco.get('uf') or '').strip().upper()[:2] or None
    db.session.add(pedido)
    db.session.flush()

    for kind, obj, nome, preco, qtd, comp in linhas:
        p_dec = preco if isinstance(preco, Decimal) else Decimal(str(preco))
        poi = PedidoOnlineItem(
            kind=kind,
            receita_id=obj.id if kind == 'receita' else None,
            produto_id=obj.id if kind == 'produto' else None,
            nome=nome, preco_unitario=p_dec, quantidade=qtd,
            subtotal=p_dec * qtd)
        pedido.itens.append(poi)
        if comp:
            _anexar_componentes(poi, obj, comp)
    pedido.recalcular_total()        # valor_total = soma (valor DOADO, referencia)
    db.session.flush()

    _baixar_estoque(pedido, usuario_id=usuario_id)
    db.session.commit()
    logger.info('divulgacao criada: %s (%d item(ns), loja=%s)',
                pedido.codigo, len(linhas),
                getattr(_loja_baixa(pedido), 'nome', '?'))
    return pedido


def _anexar_componentes(poi, produto, comp):
    """Persiste a composicao ESCOLHIDA do menu no pedido (mesmo formato do
    checkout do site — `loja_checkout`): e ELA que o painel/PDF mostram pra
    cozinha e que a baixa explode (`composicao_escolhida`), nunca a
    pre-selecao do cadastro."""
    from decimal import Decimal as _D

    from app.models import PedidoOnlineItemComponente
    from app.services import loja_menu
    por_id = {s['pi_id']: s for s in loja_menu.slots(produto)}
    for pi_id, qtd in sorted(comp.items()):
        s = por_id.get(int(pi_id))
        if not s or qtd <= 0:
            continue
        poi.componentes.append(PedidoOnlineItemComponente(
            produto_item_id=s['pi_id'],
            tipo={'receita_id': 'receita', 'produto_id': 'produto',
                  'materia_prima_id': 'mp'}[s['col']],
            receita_id=s['alvo_id'] if s['col'] == 'receita_id' else None,
            produto_componente_id=(s['alvo_id'] if s['col'] == 'produto_id'
                                   else None),
            materia_prima_id=(s['alvo_id'] if s['col'] == 'materia_prima_id'
                              else None),
            nome=s['nome'][:200], quantidade=int(qtd),
            preco_unitario=(_D(str(s['preco']))
                            if s['preco'] is not None else None),
        ))


def _baixar_estoque(pedido, usuario_id=None):
    """Baixa fisica pelo MOTOR UNICO, canal 'divulgacao' (tipo proprio,
    rastreavel e fora da previsao). Explode cesta e acumula fracao igual a
    venda do site; tolera shortfall (`venda_site_divulgacao_sem_estoque`).
    MENU configuravel explode pela composicao ESCOLHIDA (componentes
    persistidos no pedido), como no site — sem componentes cai no cadastro.
    Sem reserva/plano-do-dia — divulgacao nasce e baixa na hora."""
    from app.services.baixa_venda import aplicar_venda
    from app.services.loja_estoque_reserva import composicao_escolhida
    loja = _loja_baixa(pedido)
    if not loja:
        logger.warning('divulgacao %s: sem loja de baixa', pedido.codigo)
        return {'baixado': 0, 'faltou': 0}
    ref = f'Divulgacao #{pedido.codigo}'
    total = {'baixado': 0, 'faltou': 0}
    for it in pedido.itens:
        if not (it.receita_id or it.produto_id):
            continue
        res = aplicar_venda(
            loja.id, receita_id=it.receita_id, produto_id=it.produto_id,
            qtd=it.quantidade, canal='divulgacao', referencia=ref,
            pedido_ref=f'divulgacao:{pedido.codigo}', usuario_id=usuario_id,
            nome_venda=it.nome, pular_sem_linha=True,
            composicao=composicao_escolhida(it))
        total['baixado'] += res['baixado']
        total['faltou'] += res['faltou']
    return total


def cancelar_divulgacao(pedido, *, usuario_id=None):
    """Cancela a divulgacao ESTORNANDO o estoque baixado (mov
    `venda_site_divulgacao_estorno`). Idempotente: pedido ja cancelado = no-op.
    So admin (gate na rota)."""
    if pedido.status == 'cancelado':
        return {'ja_cancelado': True, 'revertido': 0}
    from app.services.baixa_venda import estornar_venda
    loja = _loja_baixa(pedido)
    res = estornar_venda('divulgacao', f'divulgacao:{pedido.codigo}',
                         f'Divulgacao #{pedido.codigo}',
                         loja_id=loja.id if loja else None,
                         usuario_id=usuario_id)
    pedido.status = 'cancelado'
    pedido.cancelado_em = agora()
    pedido.motivo_cancelamento = 'cancelado_admin'
    db.session.commit()
    revertido = res['revertido_inteiros'] + res['revertido_fracoes']
    logger.info('divulgacao %s cancelada (estorno=%d)', pedido.codigo, revertido)
    return {'ja_cancelado': False, 'revertido': revertido}
