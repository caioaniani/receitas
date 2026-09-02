"""Vendas B2B da industria.

REGIME DA BAIXA (07/07/2026, decisao do dono): o estoque da industria so
baixa quando o padeiro SEPARA o pedido na tela /padeiro — igual ao
PedidoLoja, onde separar/enviar e que mexe no fisico. Regras:

- Venda COM data_entrega (entra na fila do padeiro): criada SEM baixa;
  a baixa acontece em `baixar_na_separacao` (rota padeiro.separar_b2b).
- Venda IMEDIATA (sem data_entrega, nunca aparece no /padeiro): baixa na
  criacao (senao nunca baixaria).
- `VendaB2B.estoque_baixado_em` marca o estado (NULL = aguardando
  separacao); as QUANTIDADES continuam no ledger MovEstoqueProducao.
- Enquanto nao baixa, a venda e demanda COMPROMETIDA: entra no balanco/
  cronograma (previsao_producao) e desconta do estoque DISPONIVEL exibido
  nos forms/previews (`comprometido_b2b_pendente`).
- Reverter separado→pendente ESTORNA a baixa (re-separar baixa de novo).

Estoque sai do EstoqueProducao (industria/freezer). Quando falta saldo,
registra MovEstoqueProducao tipo='venda_b2b_sem_estoque' (igual logica
das vendas Seru) — sai mesmo assim e fica como auditoria.

Estoque baixado eh rastreado pelos proprios MovEstoqueProducao: o saldo
ainda devido pela venda eh `Σ(venda_b2b) − Σ(venda_b2b_estorno)`. Isso
torna cancelar/editar/reabrir corretos e idempotentes mesmo encadeados.
"""

import logging
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import update as sa_update

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    Produto,
    Receita,
    VendaB2B,
    VendaB2BItem,
    VendaB2BParcela,
)
from app.utils import agora, hoje

logger = logging.getLogger(__name__)


def _get_or_create_estoque(receita_id=None, produto_id=None):
    """Acha (ou cria zerado) a linha de EstoqueProducao do item.

    `.with_for_update()` no SELECT: trava a linha ate o commit. Sem isso,
    2 admins registrando venda B2B do mesmo item ao mesmo tempo podem
    ler o mesmo saldo (100), cada um decidir 'sobram 80' e 'sobram 70',
    e o segundo commit sobrescrever o primeiro — sai 50 do estoque mas
    o sistema registra 30. Diferente do Seru sync (advisory lock 7723
    serializa workers), B2B vem de UI, sem essa protecao previa."""
    filtro = {
        'receita_id': receita_id,
        'produto_id': produto_id,
    }
    ep = EstoqueProducao.query.filter_by(**filtro).with_for_update().first()
    if not ep:
        ep = EstoqueProducao(**filtro, quantidade=0)
        db.session.add(ep)
        db.session.flush()
    return ep


def _baixar_componente_mp(venda, produto_obj, mp_id, nome_comp, qtd_mp, user=None):
    """Baixa o componente MP de uma cesta vendida no B2B.

    MP da industria vive em `MateriaPrima.estoque_atual` + ledger
    `MovimentacaoEstoque` (EstoqueProducao nao tem coluna de MP). Aceita
    fracao (ex: 0.2 kg por cesta). O movimento 'saida' registra APENAS o
    que saiu de fato (min contra o disponivel) — assim o estorno por saldo
    devolve exato; a falta vai na referencia e num warning (o ledger de MP
    nao tem tipo 'sem_estoque' como o MovEstoqueProducao)."""
    if qtd_mp <= 0:
        return
    mp = MateriaPrima.query.get(mp_id)
    if not mp:
        return
    disponivel = float(mp.estoque_atual or 0)
    baixa = min(qtd_mp, disponivel)
    falta = qtd_mp - baixa
    ref = f'Venda B2B #{venda.id} [{produto_obj.nome} → cesta] {nome_comp}'
    if falta > 0:
        ref += f' — faltou {falta:g}'
        logger.warning(
            'Venda B2B #%s: componente MP %r da cesta %r sem saldo '
            '(pedia %.3f, havia %.3f).', venda.id, nome_comp,
            produto_obj.nome, qtd_mp, disponivel)
    if baixa > 0:
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
            referencia=ref, usuario_id=getattr(user, 'id', None)))
        mp.estoque_atual = disponivel - baixa


def _baixar_item(venda, tipo, item_id, qtd, user=None):
    """Baixa EstoqueProducao para 1 item da venda.

    Cesta (Produto com componentes) baixa cada componente; senao baixa o
    item direto. Registra `venda_b2b` pro que saiu e `venda_b2b_sem_estoque`
    pra falta. Toda referencia comeca com `Venda B2B #{id} ` (espaco) — o
    espaco evita que o LIKE de #5 case com #50 no estorno.
    """
    componentes_cesta = []
    produto_obj = None
    if tipo == 'produto':
        from app.services.cestas import componentes_de_cesta
        produto_obj = Produto.query.get(item_id)
        componentes_cesta = componentes_de_cesta(produto_obj)

    if componentes_cesta:
        for col, comp_id, nome_comp, qtd_por_cesta in componentes_cesta:
            if col == 'materia_prima_id':
                # Componente MP (item comprado pronto, ex: pote de geleia):
                # o estoque de MP da industria vive em MateriaPrima.
                # estoque_atual + ledger MovimentacaoEstoque (EstoqueProducao
                # nem tem coluna de MP). Mesmo padrao da baixa da producao;
                # o movimento registra so o que SAIU (o estorno devolve
                # exato) e a falta fica na referencia + warning.
                _baixar_componente_mp(venda, produto_obj, comp_id, nome_comp,
                                      qtd * float(qtd_por_cesta or 0), user)
                continue
            qtd_baixar = int(round(qtd * qtd_por_cesta))
            if qtd_baixar <= 0:
                continue
            # Componente baixa a PROPRIA linha (receita OU produto) — antes
            # componente produto caia numa linha anonima all-NULL, que podia
            # ate casar com linha orfa de nome_pendente (fix 08/07/2026).
            ep_c = _get_or_create_estoque(
                receita_id=comp_id if col == 'receita_id' else None,
                produto_id=comp_id if col == 'produto_id' else None,
            )
            saldo_c = ep_c.quantidade or 0
            baixa_c = min(qtd_baixar, saldo_c)
            ep_c.quantidade = saldo_c - baixa_c
            if baixa_c > 0:
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep_c.id,
                    tipo='venda_b2b',
                    quantidade=baixa_c,
                    referencia=(f'Venda B2B #{venda.id} '
                                f'[{produto_obj.nome} → cesta] {nome_comp}'),
                    usuario_id=getattr(user, 'id', None),
                ))
            if qtd_baixar > baixa_c:
                falta_c = qtd_baixar - baixa_c
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep_c.id,
                    tipo='venda_b2b_sem_estoque',
                    quantidade=falta_c,
                    referencia=(f'Venda B2B #{venda.id} '
                                f'[{produto_obj.nome} → cesta] {nome_comp} — faltou {falta_c}'),
                    usuario_id=getattr(user, 'id', None),
                ))
        return

    ep = _get_or_create_estoque(
        receita_id=item_id if tipo == 'receita' else None,
        produto_id=item_id if tipo == 'produto' else None,
    )
    saldo = ep.quantidade or 0
    baixa = min(qtd, saldo)
    ep.quantidade = saldo - baixa
    if baixa > 0:
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b',
            quantidade=baixa,
            referencia=f'Venda B2B #{venda.id} ({venda.cliente_display})',
            usuario_id=getattr(user, 'id', None),
        ))
    if qtd > baixa:
        falta = qtd - baixa
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b_sem_estoque',
            quantidade=falta,
            referencia=f'Venda B2B #{venda.id} sem saldo (faltou {falta})',
            usuario_id=getattr(user, 'id', None),
        ))


def _saldo_baixado_por_ep(venda_id):
    """{estoque_producao_id: saldo ainda baixado} = baixas − estornos da venda.

    O espaco apos o id no LIKE evita casar #5 com #50/#500.
    """
    saldo = defaultdict(int)
    baixas = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == 'venda_b2b',
        MovEstoqueProducao.referencia.like(f'Venda B2B #{venda_id} %'),
    ).all()
    for m in baixas:
        saldo[m.estoque_producao_id] += (m.quantidade or 0)
    estornos = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == 'venda_b2b_estorno',
        MovEstoqueProducao.referencia.like(f'Estorno venda B2B #{venda_id} %'),
    ).all()
    for m in estornos:
        saldo[m.estoque_producao_id] -= (m.quantidade or 0)
    return saldo


def _saldo_mp_baixado(venda_id):
    """{materia_prima_id: saldo ainda baixado} dos componentes MP de cesta
    da venda = saidas − estornos no ledger MovimentacaoEstoque (mesma
    disciplina do espaco apos o id no LIKE)."""
    saldo = defaultdict(float)
    saidas = MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.tipo == 'saida',
        MovimentacaoEstoque.referencia.like(f'Venda B2B #{venda_id} %'),
    ).all()
    for m in saidas:
        saldo[m.materia_prima_id] += (m.quantidade or 0)
    estornos = MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.tipo == 'entrada',
        MovimentacaoEstoque.referencia.like(f'Estorno venda B2B #{venda_id} %'),
    ).all()
    for m in estornos:
        saldo[m.materia_prima_id] -= (m.quantidade or 0)
    return saldo


def _estornar_estoque(venda, user=None, motivo='cancelada'):
    """Devolve ao EstoqueProducao (e ao ledger de MP, no caso de componente
    MP de cesta) o saldo ainda baixado pela venda.

    Idempotente: depois de estornar o saldo zera, entao chamadas seguintes
    nao fazem nada. Cobre cesta, falta parcial e edicoes encadeadas.
    Limpa o marcador de regime (`estoque_baixado_em`) — o estorno e sempre
    TOTAL (por saldo), entao depois dele a venda volta a "nao baixada".
    Estorno de MP com preco_unitario=None — vale R$ 0 nos relatorios de
    compra (mesmo padrao dos estornos de pedido).
    """
    saldo = _saldo_baixado_por_ep(venda.id)
    for ep_id, qtd in saldo.items():
        if qtd <= 0:
            continue
        ep = EstoqueProducao.query.get(ep_id)
        if not ep:
            continue
        ep.quantidade = (ep.quantidade or 0) + qtd
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b_estorno',
            quantidade=qtd,
            referencia=f'Estorno venda B2B #{venda.id} ({motivo})',
            usuario_id=getattr(user, 'id', None),
        ))
    for mp_id, qtd_mp in _saldo_mp_baixado(venda.id).items():
        if qtd_mp <= 0:
            continue
        mp = MateriaPrima.query.get(mp_id)
        if not mp:
            continue
        mp.estoque_atual = (mp.estoque_atual or 0) + qtd_mp
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp.id, tipo='entrada', quantidade=qtd_mp,
            preco_unitario=None,
            referencia=f'Estorno venda B2B #{venda.id} ({motivo})',
            usuario_id=getattr(user, 'id', None),
        ))
    venda.estoque_baixado_em = None


def _baixar_venda(venda, user=None):
    """Baixa o estoque de TODOS os itens da venda e marca o regime
    (`estoque_baixado_em`). E o unico caminho de baixa — chamado na
    criacao (venda IMEDIATA, sem data_entrega) ou na SEPARACAO pelo
    padeiro (decisao do dono 07/07/2026)."""
    for vi in venda.itens:
        tipo = 'receita' if vi.receita_id else 'produto'
        _baixar_item(venda, tipo, vi.receita_id or vi.produto_id,
                     int(vi.quantidade or 0), user)
    venda.estoque_baixado_em = agora()


def _claim_baixa(venda):
    """Reivindica o direito de baixar a venda por UPDATE condicional no
    marcador (`WHERE estoque_baixado_em IS NULL`) — CLAIM, mesmo padrao do
    Confirmar do Slack. Dois requests concorrentes no mesmo caminho de
    baixa (SEPARAR 2x, POSTs simultaneos na data de entrega, reabrir 2x)
    leriam ambos o marcador NULL e baixariam em DOBRO; com o claim so um
    vence (rowcount 1). NAO commita — o caller fecha a transacao."""
    claimed = db.session.execute(
        sa_update(VendaB2B)
        .where(VendaB2B.id == venda.id,
               VendaB2B.estoque_baixado_em.is_(None))
        .values(estoque_baixado_em=agora())
        .execution_options(synchronize_session=False)
    ).rowcount
    if not claimed:
        db.session.expire(venda, ['estoque_baixado_em'])
        return False
    return True


def baixar_na_separacao(venda, user=None):
    """Baixa da SEPARACAO (tela /padeiro). Idempotente pelo marcador:
    venda ja baixada (regime antigo — baixou na criacao — ou clique
    duplo/re-separacao) nao baixa de novo. Devolve True se baixou agora.
    NAO commita — a rota do padeiro fecha a transacao com o status."""
    if venda.estoque_baixado_em:
        return False
    if not _claim_baixa(venda):
        return False
    _baixar_venda(venda, user)
    return True


def sincronizar_baixa_com_data(venda, user=None):
    """Mantem o regime da baixa coerente quando a DATA DE ENTREGA muda:

    - limpou a data (virou venda IMEDIATA, sai da fila do padeiro) e ainda
      nao baixou → baixa agora (senao nunca baixaria);
    - ganhou data (entrou na fila) antes de separar e ja tinha baixado
      (era imediata) → estorna; o pao segue no freezer ate a separacao,
      que baixa de novo.

    So age em venda ativa ainda 'pendente'. NAO commita — o caller fecha.
    """
    if venda.status != 'ativa' or venda.status_entrega != 'pendente':
        return
    if venda.data_entrega is None and not venda.estoque_baixado_em:
        if _claim_baixa(venda):
            _baixar_venda(venda, user)
    elif venda.data_entrega is not None and venda.estoque_baixado_em:
        _estornar_estoque(venda, user=user,
                          motivo='entrou na fila do padeiro')


def comprometido_b2b_pendente(excluir_venda_id=None):
    """{(kind, item_id): qtd} do que as vendas B2B AGUARDANDO SEPARACAO
    ainda vao tirar do EstoqueProducao (cesta explodida em componentes —
    mesma explosao da baixa). Usado pra exibir o estoque DISPONIVEL
    (fisico − comprometido) nos forms/previews: sem isso, duas vendas
    podem ser aprovadas contra o mesmo saldo e a falta so aparece dias
    depois, na separacao.

    `excluir_venda_id`: no form de EDITAR uma venda pendente, a propria
    venda nao deve descontar do disponivel exibido pra ela mesma.

    Componente MP de cesta fica FORA: a baixa dele acontece no ledger de
    MP (`_baixar_componente_mp`), nao numa linha de EstoqueProducao — e o
    disponivel exibido (estoque_map) so cobre receitas/produtos.
    Componentes receita e produto contam na PROPRIA linha, espelhando a
    baixa real (`_baixar_item`)."""
    from app.services.cestas import componentes_de_cesta

    pend = defaultdict(int)
    q_itens = (VendaB2BItem.query
               .join(VendaB2B, VendaB2BItem.venda_id == VendaB2B.id)
               .filter(VendaB2B.status == 'ativa',
                       VendaB2B.estoque_baixado_em.is_(None)))
    if excluir_venda_id:
        q_itens = q_itens.filter(VendaB2B.id != excluir_venda_id)
    for vi in q_itens.all():
        qtd = int(vi.quantidade or 0)
        if qtd <= 0:
            continue
        if vi.receita_id:
            pend[('receita', vi.receita_id)] += qtd
            continue
        produto = Produto.query.get(vi.produto_id)
        comps = componentes_de_cesta(produto) if produto else []
        if not comps:
            pend[('produto', vi.produto_id)] += qtd
            continue
        for col, comp_id, _nome, qtd_por in comps:
            if col == 'materia_prima_id':
                continue
            q = int(round(qtd * qtd_por))
            if q <= 0:
                continue
            kind = 'receita' if col == 'receita_id' else 'produto'
            pend[(kind, comp_id)] += q
    return dict(pend)


def _aplicar_itens(venda, itens, user=None, baixar=True):
    """Cria os VendaB2BItem e, se `baixar`, baixa o estoque de cada item.
    Retorna total Decimal. `baixar=False` no regime novo (venda com
    data_entrega): a baixa fica pra separacao no /padeiro.

    Ignora linha com qtd<=0, tipo invalido ou sem id (mesma regra do form).
    """
    total = Decimal('0')
    for it in itens:
        tipo = it.get('tipo')
        item_id = it.get('id')
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        if tipo not in ('receita', 'produto') or not item_id:
            continue
        try:
            preco = Decimal(str(it.get('preco_unitario') or 0))
        except (TypeError, ValueError):
            preco = Decimal('0')
        try:
            desc = float(it.get('desconto_percentual') or 0)
        except (TypeError, ValueError):
            desc = 0.0
        est = (str(it.get('estado') or '').strip().lower() or None)
        if est not in (None, 'backup', 'assado'):
            est = None
        obs = (str(it.get('observacao') or '').strip()[:200] or None)

        vi = VendaB2BItem(
            venda_id=venda.id,
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
            quantidade=qtd,
            preco_unitario=preco,
            desconto_percentual=desc,
            estado=est,
            observacao=obs,
        )
        db.session.add(vi)
        total += vi.valor_total
        if baixar:
            _baixar_item(venda, tipo, item_id, qtd, user)
    return total


def _aplicar_parcelas(venda, parcelas):
    """Cria as parcelas da venda. Sem parcelas explicitas:

    - cliente com FATURAMENTO MENSAL: NAO cria parcela nenhuma — a venda
      fica na conta do mes e so vira recebivel quando o fechamento
      (FaturaB2B) criar a parcela com o vencimento da fatura. Sem isso a
      parcela unica automatica tirava a venda do universo do fechamento
      e a feature nunca fechava conta nenhuma (achado da revisao
      07/07/2026).
    - demais clientes: 1 parcela unica ao total (comportamento original).

    Parcela explicita SEMPRE vale (excecao negociada — a venda fica FORA
    do fechamento mensal de proposito)."""
    if not parcelas:
        if venda.cliente and venda.cliente.faturamento_mensal:
            return
        db.session.add(VendaB2BParcela(
            venda_id=venda.id, numero=1,
            vencimento=venda.data_venda,
            valor=venda.valor_total,
        ))
        return
    for n, p in enumerate(parcelas, start=1):
        venc = p.get('vencimento')
        if isinstance(venc, str):
            from datetime import date
            venc = date.fromisoformat(venc)
        db.session.add(VendaB2BParcela(
            venda_id=venda.id, numero=n,
            vencimento=venc,
            valor=Decimal(str(p.get('valor') or 0)),
            forma_pagamento=(p.get('forma_pagamento') or '').strip() or None,
        ))


def _normalizar_frete(frete_valor):
    """Frete cobrado do cliente → Decimal(0.01). Negativo é erro (desconto
    tem caminho proprio, por item); dinheiro nunca passa por float.
    inf/nan (POST forjado passa pelo float() da rota) viram ValueError
    tratado em vez de InvalidOperation/500."""
    from decimal import InvalidOperation
    try:
        frete = Decimal(str(frete_valor or 0)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError(f'frete invalido: {frete_valor!r}') from None
    if frete < 0:
        raise ValueError('frete nao pode ser negativo')
    return frete


def criar_venda(*, cliente_id=None, cliente_nome=None, data_venda=None,
                data_entrega=None, itens, parcelas=None, observacao=None,
                nf_numero=None, frete_valor=0, user=None, commit=True):
    """Cria venda B2B + itens + parcelas + baixa estoque.

    itens: lista de {tipo: 'receita'|'produto', id, quantidade,
                     preco_unitario, desconto_percentual, estado, observacao}
    parcelas: lista de {vencimento (date), valor, forma_pagamento (str)}
              ou None pra criar 1 parcela unica ao total
    frete_valor: frete da entrega cobrado do cliente (R$). SOMA no
              valor_total — parcela/boleto/fatura herdam; a NF do Tiny
              recebe o valor no campo valor_frete (itens + frete fecham
              o mesmo total nas duas pontas).
    commit=False: so faz flush — o caller fecha a transacao. Usado pela
              conversao de orcamento pra persistir venda + vinculo
              (orc.venda_id) num commit UNICO (sem janela de crash em que
              a venda existe mas o orcamento nao aponta pra ela).

    Returns: VendaB2B persistida (com id).
    """
    if not itens:
        raise ValueError('venda sem itens')
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')
    frete = _normalizar_frete(frete_valor)

    venda = VendaB2B(
        data_venda=data_venda or hoje(),
        data_entrega=data_entrega,
        cliente_id=cliente_id,
        cliente_nome=(cliente_nome or '').strip() or None,
        observacao=(observacao or '').strip() or None,
        nf_numero=(nf_numero or '').strip() or None,
        criado_por_id=getattr(user, 'id', None),
        valor_total=0,
        frete_valor=frete,
    )
    db.session.add(venda)
    db.session.flush()

    # Regime da baixa: venda com data_entrega vai pra fila do padeiro e so
    # baixa na SEPARACAO; venda imediata (sem data) baixa aqui mesmo.
    baixar_agora = data_entrega is None
    total = _aplicar_itens(venda, itens, user, baixar=baixar_agora)
    if baixar_agora:
        venda.estoque_baixado_em = agora()
    venda.valor_total = (total + frete).quantize(Decimal('0.01'),
                                                 rounding=ROUND_HALF_UP)
    _aplicar_parcelas(venda, parcelas)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return venda


def editar_venda(venda, *, cliente_id=None, cliente_nome=None, data_venda=None,
                 data_entrega=None, itens, parcelas=None, observacao=None,
                 nf_numero=None, frete_valor=0, user=None):
    """Edita uma venda ATIVA por completo: estorna o estoque baixado, apaga
    itens/parcelas antigos, recria com os novos dados e re-baixa.

    Recusa se a venda estiver cancelada (reabra antes) ou se ja houver
    pagamento (protege as contas a receber de inconsistencia). Pra mexer so
    no cabecalho de uma venda paga, use `editar_cabecalho`.
    """
    if venda.status == 'cancelada':
        raise ValueError('venda cancelada — reabra antes de editar')
    if venda.fatura_id:
        # Editar apagaria/recriaria a parcela do fechamento SEM fatura_id e
        # mudaria o total por fora da fatura/boleto/NF (revisao 07/07/2026).
        raise ValueError(
            f'venda faturada ({venda.fatura.codigo}) — cancele a fatura em '
            'B2B → Faturas mensais antes de editar')
    if venda.valor_pago and venda.valor_pago > 0:
        raise ValueError('venda com pagamento registrado — itens nao podem ser editados')
    if not itens:
        raise ValueError('venda sem itens')
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')
    frete = _normalizar_frete(frete_valor)

    # Boleto Sicredi: apagar a parcela NULLifica o FK da Cobranca (achado
    # da revisao 20/07/2026, reproduzido: boleto orfao segue vivo com o
    # valor ANTIGO, a liquidacao nao acha a parcela e silencia, e a parcela
    # nova vira candidata a um SEGUNDO boleto). Mesmo guard do
    # excluir_venda: titulo que ja foi ao banco trava a edicao; pendente
    # (nunca enviado) e apagado junto — o usuario gera outro no total novo.
    cobrancas_pendentes = []
    for p in venda.parcelas:
        for cob in (p.cobranca or []):      # backref é lista
            if cob.status != 'pendente':
                raise ValueError(
                    f'parcela {p.numero} tem boleto que já foi ao banco '
                    f'(status {cob.status}) — o valor do título não pode '
                    'mudar por baixo; trate pelo retorno/baixa antes de '
                    'editar a venda')
            cobrancas_pendentes.append(cob)

    _estornar_estoque(venda, user=user, motivo='edicao')
    for cob in cobrancas_pendentes:
        db.session.delete(cob)
    for vi in list(venda.itens):
        db.session.delete(vi)
    for p in list(venda.parcelas):
        db.session.delete(p)
    db.session.flush()

    venda.cliente_id = cliente_id
    venda.cliente_nome = (cliente_nome or '').strip() or None
    if data_venda:
        venda.data_venda = data_venda
    venda.data_entrega = data_entrega
    venda.observacao = (observacao or '').strip() or None
    venda.nf_numero = (nf_numero or '').strip() or None

    # Re-baixa so o que ja devia estar baixado: venda imediata (sem data)
    # ou que ja passou da separacao. Venda pendente na fila segue sem
    # baixa — o estorno acima foi no-op (saldo 0) e a separacao baixa.
    baixar = (venda.data_entrega is None
              or venda.status_entrega != 'pendente')
    total = _aplicar_itens(venda, itens, user, baixar=baixar)
    if baixar:
        venda.estoque_baixado_em = agora()
    venda.frete_valor = frete
    venda.valor_total = (total + frete).quantize(Decimal('0.01'),
                                                 rounding=ROUND_HALF_UP)
    _aplicar_parcelas(venda, parcelas)
    db.session.commit()
    return venda


def editar_cabecalho(venda, *, cliente_id=None, cliente_nome=None,
                     data_venda=None, data_entrega=None, observacao=None,
                     nf_numero=None):
    """Edita so o cabecalho (cliente, datas, obs, nf) — NAO toca itens, estoque
    nem parcelas. Usado quando a venda ja tem pagamento e os itens estao travados.
    """
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')
    venda.cliente_id = cliente_id
    venda.cliente_nome = (cliente_nome or '').strip() or None
    if data_venda:
        venda.data_venda = data_venda
    venda.data_entrega = data_entrega
    venda.observacao = (observacao or '').strip() or None
    venda.nf_numero = (nf_numero or '').strip() or None
    db.session.commit()
    return venda


def reabrir_venda(venda, user=None):
    """Venda cancelada volta a 'ativa'. Re-baixa o estoque APENAS se a
    venda esta fora da fila do padeiro (imediata, sem data_entrega) ou se
    ja tinha passado da separacao — venda pendente na fila volta SEM baixa
    e o padeiro baixa ao separar (regime 07/07/2026).

    Idempotente: se a venda nao esta cancelada, nao faz nada.
    """
    if venda.status != 'cancelada':
        return venda
    venda.status = 'ativa'
    venda.cancelado_em = None
    venda.cancelado_por_id = None
    if venda.data_entrega is None or venda.status_entrega != 'pendente':
        if _claim_baixa(venda):
            _baixar_venda(venda, user)
    db.session.commit()
    return venda


_STATUS_ENTREGA_ORDEM = ['pendente', 'separado', 'em_transporte', 'entregue']


def reverter_status_entrega(venda, user=None):
    """Volta um passo no status de entrega (entregue→separado→pendente).

    Voltar de separado→pendente ESTORNA a baixa da separacao (regime
    07/07/2026): o pao volta pro freezer no sistema como voltou no fisico,
    e a proxima separacao baixa de novo (idempotente pelo marcador).
    Idempotente em 'pendente'.
    """
    cur = venda.status_entrega or 'pendente'
    if cur in _STATUS_ENTREGA_ORDEM and _STATUS_ENTREGA_ORDEM.index(cur) > 0:
        novo = _STATUS_ENTREGA_ORDEM[_STATUS_ENTREGA_ORDEM.index(cur) - 1]
        if (novo == 'pendente' and venda.data_entrega is not None
                and venda.estoque_baixado_em):
            _estornar_estoque(venda, user=user, motivo='volta pra separar')
        venda.status_entrega = novo
        db.session.commit()
    return venda


def cancelar_venda(venda, user=None):
    """Estorna estoque (por saldo) e marca venda como cancelada. Idempotente.

    Venda FATURADA nao cancela: a fatura/boleto/NF do fechamento ficariam
    cobrando venda morta (revisao 07/07/2026) — cancele a fatura primeiro.
    """
    if venda.status == 'cancelada':
        return venda
    if venda.fatura_id:
        raise ValueError(
            f'venda faturada ({venda.fatura.codigo}) — cancele a fatura em '
            'B2B → Faturas mensais antes de cancelar a venda')
    _estornar_estoque(venda, user=user, motivo='cancelada')
    venda.status = 'cancelada'
    venda.cancelado_em = agora()
    venda.cancelado_por_id = getattr(user, 'id', None)
    db.session.commit()
    return venda


def excluir_venda(venda, user=None):
    """Exclui a venda DEFINITIVAMENTE (limpeza de teste / lançamento
    errado — pedido do dono 07/07/2026, virada pra produção). Só o dono
    (rota owner_required).

    Estorna o estoque ainda baixado (idempotente, por saldo — venda já
    cancelada não devolve em dobro) e apaga itens/parcelas via cascade.
    Os MovEstoqueProducao FICAM (referência por texto, sem FK) — a
    história do estoque não se apaga.

    Recusas (dinheiro tem peso especial):
    - venda faturada: cancele a fatura antes;
    - pagamento registrado (valor_pago > 0): registro financeiro não some;
    - parcela com cobrança que já foi ao banco (pendente é apagada junto).
    """
    if venda.fatura_id:
        raise ValueError(
            f'venda faturada ({venda.fatura.codigo}) — cancele a fatura em '
            'B2B → Faturas mensais antes de excluir')
    if venda.valor_pago and venda.valor_pago > 0:
        raise ValueError('venda com pagamento registrado — registro '
                         'financeiro não se apaga')
    cobrancas_pendentes = []
    for p in venda.parcelas:
        for cob in (p.cobranca or []):      # backref é lista
            if cob.status != 'pendente':
                raise ValueError(
                    f'parcela {p.numero} tem boleto que já foi ao banco '
                    f'(status {cob.status}) — resolva pelo retorno antes')
            cobrancas_pendentes.append(cob)
    _estornar_estoque(venda, user=user, motivo='exclusao')
    for cob in cobrancas_pendentes:
        db.session.delete(cob)
    # Orcamento convertido nesta venda volta pra fila de Aprovados (pode
    # ser convertido de novo) — senao o vinculo aponta pro nada.
    from app.models import Orcamento
    for orc in Orcamento.query.filter_by(venda_id=venda.id).all():
        orc.venda_id = None
    db.session.delete(venda)                # itens/parcelas via cascade
    db.session.commit()


def receber_pagamento(parcela, valor, forma_pagamento=None, observacao=None):
    """Soma valor ao valor_pago da parcela. Marca pago_em se quitar.

    Numeric(10, 2) no banco garante precisao exata em centavos —
    sem necessidade de tolerancia de arredondamento.
    """
    from decimal import InvalidOperation
    if parcela.venda:
        db.session.refresh(parcela.venda, with_for_update=True)
    if parcela.venda and parcela.venda.sem_cobranca:
        raise ValueError('Divulgação sem cobrança: não registrar recebimento como se fosse uma venda cobrável.')
    try:
        v = Decimal(str(valor))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError('valor invalido')
    if v <= 0:
        raise ValueError('valor deve ser > 0')

    pago_atual = Decimal(parcela.valor_pago or 0)
    parcela.valor_pago = pago_atual + v

    if forma_pagamento:
        parcela.forma_pagamento = forma_pagamento
    if observacao:
        parcela.observacao = observacao

    if parcela.valor_pago >= Decimal(parcela.valor or 0):
        parcela.pago_em = agora()
    db.session.commit()
    return parcela


def preco_sugerido(receita_id=None, produto_id=None, cliente=None):
    """Retorna o preco sugerido pro item na venda B2B.

    Prioridade (06/07/2026 — o atacado cobra valores diferentes por
    cliente):
    1. Preco ESPECIFICO do cliente (`PrecoClienteB2B`) — valor final,
       sem desconto percentual em cima.
    2. Preco atacado do cadastro (Receita.preco_venda / Produto.
       preco_atacado) com o desconto percentual do cliente aplicado
       (comportamento antigo, inalterado pra quem nao tem tabela).
    Retorna float ou None se nao houver preco.
    """
    from app.models import PrecoClienteB2B
    if cliente and (receita_id or produto_id):
        esp = PrecoClienteB2B.query.filter_by(
            cliente_id=cliente.id,
            kind='receita' if receita_id else 'produto',
            item_id=receita_id or produto_id).first()
        if esp:
            return round(float(esp.preco), 2)
    preco = None
    if receita_id:
        r = Receita.query.get(receita_id)
        preco = r.preco_venda if r else None
    elif produto_id:
        p = Produto.query.get(produto_id)
        preco = p.preco_atacado if p else None
    if not preco:
        return None
    if cliente and cliente.desconto_percentual:
        preco = preco * (1 - cliente.desconto_percentual / 100.0)
    return round(preco, 2)
