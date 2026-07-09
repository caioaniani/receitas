"""Orquestração pagamento da loja online — Pagar.me (Fase 4).

Faz a ponte entre o PedidoOnline (Fase 3) e o serviço pagarme.py:
- `iniciar_pix(pedido)` / `iniciar_cartao(pedido, token, parcelas)`:
  cria um `PagamentoOnline` e dispara o Order no Pagar.me.
- `processar_webhook(evento)`: idempotente. 'order.paid'/'charge.paid'
  → marca pago + baixa estoque (`venda_site`). Eventos de estorno
  ('charge.refunded' / 'order.canceled' / ...) são REGISTRADOS para
  auditoria mas NÃO executam estorno automático (ver abaixo).

DECISÕES DE DINHEIRO (não desviar sem perguntar — CLAUDE.md peso especial):
- Pagar.me usa CENTAVOS; `pagarme._centavos` é o único conversor.
- Baixa de estoque acontece SÓ pelo webhook 'paid'; nunca no retorno do
  checkout. Reentrega do mesmo evento é absorvida por `PagarmeEvento`.
- ESTORNO AUTOMÁTICO DESATIVADO (decisão do dono 18/06/2026): o VNDA já
  sofreu cancelamento em massa no gateway (bug/abuso). Por isso o webhook
  de estorno NÃO cancela pedido nem devolve estoque — só loga. O reembolso
  é SEMPRE manual via `reembolsar_pedido` (botão no admin), deliberado e
  individual. `_marcar_estornado` continua existindo, mas só esse caminho
  manual o aciona.
- Loja de origem: a configurada em `AppConfig.loja_site_estoque_id`, com
  fallback pra mesma loja onde o VNDA baixa hoje (mantém a paridade).
- Item sem FK (`receita_id`/`produto_id`) é pulado e logado — não erra
  silenciosamente.
"""
import logging

from app.constants import VENDA_TIPOS_LOJA
from app.extensions import db
from app.models import (
    AppConfig,
    Loja,
    PagamentoOnline,
    PagarmeEvento,
    PedidoOnline,
)
from app.services import pagarme
from app.utils import agora

logger = logging.getLogger(__name__)

# Fallback de loja de origem do estoque pra venda do site quando não
# houver configuração explícita. Mesma loja que o VNDA usa hoje (`Loja
# Anesio Pinto Rosa`) — mantém a paridade quando virarmos a chave.
_LOJA_SITE_NOME_DEFAULT = 'Loja Anesio Pinto Rosa'

# Mensagem CLARA pro cliente quando o cartão é recusado (o motivo técnico do
# emissor fica em `PagamentoOnline.erro`, pro admin). Antes vazava "gateway
# 200:" pro cliente (08/07/2026).
_MSG_CARTAO_RECUSADO = (
    'Seu cartão foi recusado pelo banco emissor. Tente outro cartão, confira '
    'os dados, ou pague por Pix. Se continuar, fale com o seu banco.')

# Travas
assert 'venda_site' in VENDA_TIPOS_LOJA
assert 'venda_site_sem_estoque' in VENDA_TIPOS_LOJA
assert 'venda_site_estorno' in VENDA_TIPOS_LOJA


def loja_origem_site():
    """Loja onde o estoque do site é debitado quando o pedido é PAGO.

    Configuração em `AppConfig.loja_site_estoque_id`. Sem config, cai no
    nome default (mesma loja que o VNDA usa). Pode ser sobrescrito por
    pedido (retirada baixa da loja escolhida — ver _loja_baixa)."""
    loja_id = AppConfig.get_int('loja_site_estoque_id')
    if loja_id:
        loja = Loja.query.get(loja_id)
        if loja:
            return loja
    return Loja.query.filter_by(nome=_LOJA_SITE_NOME_DEFAULT).first()


def _loja_baixa(pedido):
    """Loja de onde o pedido baixa o estoque. Retirada baixa da loja
    escolhida pelo cliente; entrega/express baixa da loja_origem_site."""
    if pedido.modo_entrega == 'retirada' and pedido.loja_retirada_id:
        loja = Loja.query.get(pedido.loja_retirada_id)
        if loja:
            return loja
    return loja_origem_site()


# ── Iniciar pagamento ────────────────────────────────────────────────

def _zerar_pagamento_anterior(pedido):
    """Se o cliente já abriu um pagamento que ficou pendente e clicou de
    novo (ex: tentou Pix e mudou pra cartão), marca o velho como falhou
    pra ficar só UM pagamento ativo por pedido em cada momento."""
    for pag in pedido.pagamentos:
        if pag.status == 'pendente':
            pag.status = 'falhou'
            pag.erro = 'substituído por nova tentativa'


def iniciar_pix(pedido, expira_em_min=30):
    """Cria PagamentoOnline(metodo=pix) e dispara Order Pix no Pagar.me.
    Devolve o PagamentoOnline (com QR populado) ou None + erros."""
    _zerar_pagamento_anterior(pedido)
    pag = PagamentoOnline(pedido_id=pedido.id, metodo='pix',
                          valor=pedido.valor_total)
    db.session.add(pag)
    db.session.flush()

    res = pagarme.criar_pedido_pix(pedido, expira_em_min=expira_em_min)
    if not res.get('ok'):
        pag.status = 'falhou'
        pag.erro = res.get('erro') or 'falha desconhecida'
        # Guarda os IDs mesmo na falha pra reconciliar no painel do Pagar.me.
        pag.pagarme_order_id = res.get('order_id')
        pag.pagarme_charge_id = res.get('charge_id')
        db.session.commit()
        return None, [res.get('erro') or 'Erro ao gerar Pix']

    pag.pagarme_order_id = res.get('order_id')
    pag.pagarme_charge_id = res.get('charge_id')
    pag.pix_qr_code = res.get('qr_code')
    pag.pix_qr_code_url = res.get('qr_code_url')
    pag.pix_expira_em = res.get('expira_em')
    db.session.commit()
    return pag, []


def iniciar_cartao(pedido, card_token, parcelas=1, billing=None):
    """Cria PagamentoOnline(metodo=cartao) e dispara Order de cartão.
    Pagar.me responde 'paid' (capturou) ou 'failed'. Diferente do Pix, o
    cartão dá resposta imediata — mas mesmo assim a baixa de estoque
    espera o webhook. `billing` = endereço de cobrança (antifraude)."""
    if not card_token:
        return None, ['Cartão não foi tokenizado — tente de novo.']
    _zerar_pagamento_anterior(pedido)
    pag = PagamentoOnline(pedido_id=pedido.id, metodo='cartao',
                          valor=pedido.valor_total)
    db.session.add(pag)
    db.session.flush()

    res = pagarme.criar_pedido_cartao(pedido, card_token, parcelas=parcelas,
                                      billing=billing)
    if not res.get('ok'):
        # DUAS audiências: o ADMIN vê o motivo técnico real (do Pagar.me /
        # emissor, ex: "Transação não autorizada... (código 1000)"); o CLIENTE
        # vê uma mensagem CLARA e acionável — sem jargão tipo "gateway 200:".
        pag.status = 'falhou'
        pag.erro = res.get('erro') or 'falha desconhecida'
        pag.pagarme_order_id = res.get('order_id')
        pag.pagarme_charge_id = res.get('charge_id')
        db.session.commit()
        return None, [_MSG_CARTAO_RECUSADO]

    pag.pagarme_order_id = res.get('order_id')
    pag.pagarme_charge_id = res.get('charge_id')
    # Resposta imediata: 'paid' já vem aqui. A baixa de estoque acontece
    # quando o webhook 'paid' chegar (mesma fonte de verdade pros dois
    # métodos — evita race com o webhook).
    if (res.get('status') or '').lower() in ('failed', 'refused', 'canceled'):
        pag.status = 'falhou'
        pag.erro = res.get('erro') or f'cartão recusado ({res.get("status")})'
        db.session.commit()
        return None, [_MSG_CARTAO_RECUSADO]
    db.session.commit()
    return pag, []


# ── Webhook ──────────────────────────────────────────────────────────

def _encontrar_pedido(payload_data):
    """Procura o PedidoOnline referente ao evento. Tenta por
    pagarme_order_id (PagamentoOnline) e, em fallback, pelo `code` que o
    Order carrega (que setamos como o codigo do pedido)."""
    order_id = (payload_data.get('id')
                or (payload_data.get('order') or {}).get('id'))
    if order_id:
        pag = (PagamentoOnline.query
               .filter_by(pagarme_order_id=order_id).first())
        if pag:
            return pag.pedido, pag
    code = (payload_data.get('code')
            or (payload_data.get('order') or {}).get('code'))
    if code:
        ped = PedidoOnline.query.filter_by(codigo=code).first()
        if ped:
            pag = next((p for p in ped.pagamentos
                        if p.status == 'pendente'), None)
            return ped, pag
    return None, None


def _reservar_no_plano_do_dia(pedido):
    """Reserva no plano de estoque do site (EstoqueSitePlano) por DATA DE
    ENTREGA. Independente do EstoqueLoja fisico — controla so DISPONIBILIDADE
    no site (se a foccacia esta esgotada pra terca-feira ou nao). Sem
    data_entrega: pula sem erro (cliente sem prazo escolhido). Cestas: reserva
    a CESTA inteira (1 unid); componentes ficam dentro dela e nao consomem
    saldo individual do plano. Decisao do dono 22/06/2026."""
    if not pedido.data_entrega:
        return
    from app.services import loja_plano_dia
    for it in pedido.itens:
        if it.receita_id:
            kind, item_id = 'receita', it.receita_id
        elif it.produto_id:
            kind, item_id = 'produto', it.produto_id
        else:
            continue
        try:
            qtd = int(round(float(it.quantidade or 0)))
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        # `reservar` AUTO-CRIA linha com saldo virtual negativo quando nao tem
        # plano cadastrado — deixa rastro pra auditoria de "vendeu sem
        # planejar", mas NAO bloqueia a venda (paginas / validacao do checkout
        # cuidam disso ANTES).
        loja_plano_dia.reservar(kind, item_id, pedido.data_entrega, qtd)


def _devolver_ao_plano_do_dia(pedido):
    """Espelho do `_reservar_no_plano_do_dia`: cancelamento/reembolso devolve
    a reserva pra o saldo daquele dia. Idempotente: pode rodar varias vezes
    sem cair pra negativo (devolver trunca em 0)."""
    if not pedido.data_entrega:
        return
    from app.services import loja_plano_dia
    for it in pedido.itens:
        if it.receita_id:
            kind, item_id = 'receita', it.receita_id
        elif it.produto_id:
            kind, item_id = 'produto', it.produto_id
        else:
            continue
        try:
            qtd = int(round(float(it.quantidade or 0)))
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        loja_plano_dia.devolver(kind, item_id, pedido.data_entrega, qtd)


def _baixar_estoque(pedido, usuario_id=None):
    """Consome a reserva criada no checkout: baixa estoque DE VERDADE
    (decrementa `quantidade` e `quantidade_reservada` juntos) e registra
    MovEstoqueLoja('venda_site') por item.

    Idempotente — se o pedido ja tem mov 'venda_site', no-op (defesa em
    profundidade contra retry de webhook). Itens sem FK pulam (WARNING).
    """
    from app.services import loja_estoque_reserva
    loja = _loja_baixa(pedido)
    if not loja:
        logger.warning('venda_site: sem loja de origem (codigo=%s)',
                       pedido.codigo)
        return {'baixado': 0, 'faltou': 0, 'pulado': len(pedido.itens)}
    return loja_estoque_reserva.consumir(
        pedido, loja_id=loja.id, usuario_id=usuario_id)


def _estornar_estoque(pedido):
    """Reverte a baixa do site pelo MOTOR UNICO (`baixa_venda.estornar_venda`):
    inteiros pela referencia (cesta inclusa, via prefixo), fracoes pelo
    DebitoEstoqueMov. O mov `venda_site_estorno` mantem a quantidade NEGATIVA
    (convencao historica do site — ver `_SINAL_ESTORNO`).

    Reverte a VERSAO ATUAL da baixa (`_versao_estoque_atual`): pedido que teve
    a quantidade reduzida (`reduzir_item_pedido_pago`) foi rebaixado sob uma
    versao nova; o cancelamento total credita SO essa ultima baixa (as versoes
    anteriores ja foram estornadas na reducao). Pedido nunca reduzido = v0 =
    referencia original ('Site #<codigo>'), comportamento identico ao de antes."""
    from app.services.baixa_venda import estornar_venda
    loja = _loja_baixa(pedido)
    ref, pref = _ref_estoque(pedido.codigo, _versao_estoque_atual(pedido))
    res = estornar_venda('site', pref, ref, loja_id=loja.id if loja else None)
    return res['revertido_inteiros'] + res['revertido_fracoes']


def _versao_estoque_atual(pedido):
    """Versao atual da baixa de estoque do pedido, derivada das REFERENCIAS dos
    movimentos `venda_site` (sem coluna nova). A reducao de quantidade de um
    pedido pago (`reduzir_item_pedido_pago`) rebaixa sob 'Site #<codigo>#v<N>';
    a versao atual e o maior N presente (0 = baixa original, nunca reduzida).

    Assim o estorno (cancelamento total futuro) reverte SO a ultima baixa, sem
    creditar em dobro as versoes ja estornadas — reaproveitando o motor unico
    (`baixa_venda.estornar_venda`) por referencia, sem tocar no ledger antigo.

    Olha os DOIS ledgers: os movimentos INTEIROS (`MovEstoqueLoja`, referencia
    'Site #<codigo>#v<N>') E as FRACOES (`DebitoEstoqueMov`, pedido_ref
    'site:<codigo>#v<N>'). Sem a fracao, uma cesta com componente fracionario
    cujo rebaixa nao cruzou um inteiro criaria uma versao SO-fracionaria
    invisivel — o cancelamento total usaria a versao errada e deixaria fracao
    fantasma pra sempre (achado da revisao 08/07/2026).

    `<codigo>` e sempre `secrets.token_hex(4).upper()` (8 hex, tamanho fixo) —
    nunca contem espaco/`#v`/`%`/`_`, entao o `like` e o parse ancorado sao
    seguros. Se o formato de `codigo` mudar, revisar este parse."""
    import re

    from app.models import DebitoEstoqueMov, MovEstoqueLoja
    maxv = 0

    def _scan(valor, prefixo):
        nonlocal maxv
        if valor and valor.startswith(prefixo):
            m = re.match(r'(\d+)', valor[len(prefixo):])
            if m:
                maxv = max(maxv, int(m.group(1)))

    pref_int = f'Site #{pedido.codigo}#v'
    for (ref,) in (db.session.query(MovEstoqueLoja.referencia)
                   .filter(MovEstoqueLoja.tipo == 'venda_site',
                           MovEstoqueLoja.referencia.like(
                               f'Site #{pedido.codigo}%')).all()):
        _scan(ref, pref_int)

    pref_frac = f'site:{pedido.codigo}#v'
    for (pref,) in (db.session.query(DebitoEstoqueMov.pedido_ref)
                    .filter(DebitoEstoqueMov.canal == 'site',
                            DebitoEstoqueMov.pedido_ref.like(
                                f'site:{pedido.codigo}%')).all()):
        _scan(pref, pref_frac)

    return maxv


def _ref_estoque(codigo, versao):
    """(referencia, pedido_ref) da baixa de estoque numa versao. v0 = formato
    ORIGINAL (retrocompativel com pedidos ja baixados antes desta feature)."""
    if versao <= 0:
        return f'Site #{codigo}', f'site:{codigo}'
    return f'Site #{codigo}#v{versao}', f'site:{codigo}#v{versao}'


def _rebaixar_pedido(pedido, loja_id, referencia, pedido_ref, usuario_id=None):
    """Baixa TODOS os itens do pedido nas quantidades ATUAIS sob a referencia
    dada, pelo motor unico (explode cesta, acumula fracao). Espelha o passo 2
    de `loja_estoque_reserva.consumir`, mas com referencia versionada e SEM a
    guarda de idempotencia (o chamador — reducao — controla) nem reserva
    (o pago ja consumiu a reserva fisica)."""
    from app.services.baixa_venda import aplicar_venda
    total = {'baixado': 0, 'faltou': 0}
    for it in pedido.itens:
        if not (it.receita_id or it.produto_id):
            continue
        if int(it.quantidade or 0) <= 0:
            continue
        res = aplicar_venda(
            loja_id, receita_id=it.receita_id, produto_id=it.produto_id,
            qtd=it.quantidade, canal='site', referencia=referencia,
            pedido_ref=pedido_ref, usuario_id=usuario_id,
            nome_venda=it.nome, pular_sem_linha=True)
        total['baixado'] += res['baixado']
        total['faltou'] += res['faltou']
    return total


def reduzir_item_pedido_pago(pedido, item_id, nova_qtd, usuario_id=None):
    """Correcao OWNER-ONLY: reduz a quantidade de UM item de um pedido PAGO
    (cliente comprou 2 e era 1). Faz, na ordem (dinheiro primeiro):

    1. REFUND PARCIAL no Pagar.me = unidades removidas x preco unitario (o
       frete NAO e devolvido — a entrega ainda acontece; decisao do dono).
    2. ESTOQUE: estorna a baixa da VERSAO atual (credito integral) e rebaixa a
       quantidade nova sob a proxima versao — reaproveita o motor unico e deixa
       um cancelamento total futuro reverter so a ultima baixa.
    3. PLANO-DO-DIA: devolve as unidades removidas (disponibilidade do site).
    4. Item.quantidade / subtotal / total do pedido recalculados.

    NF: o Tiny aqui SO emite (nao ha API de cancelar/devolver NF). NAO mexe na
    NF autorizada — a mensagem AVISA que precisa correcao manual no Tiny.

    Retorna (ok, mensagem). Minimo 1 por item (pra zerar/cancelar, usar o
    'Reembolsar e cancelar'). Se o refund falhar, ABORTA sem tocar estoque/BD.

    CONCORRENCIA (dinheiro, peso especial): LOCK pessimista no pedido antes de
    tudo — sem ele, duplo submit / dois workers gunicorn liam qtd=2, ambos
    passavam a guarda e davam DOIS refunds parciais + estorno/rebaixa em dobro.
    Mesmo padrao do `_marcar_pago` (webhooks concorrentes). Sob a trava, a qtd e
    RELIDA: o 2o request ve a qtd ja reduzida e a guarda o barra. SQLite: no-op.
    (Retry sequencial APOS falha de commit segue o mesmo risco do
    `reembolsar_pedido` — refund feito e nao persistido; commit aqui e simples.)"""
    from decimal import Decimal

    db.session.refresh(pedido, with_for_update=True)
    if pedido.status != 'pago':
        return False, 'Só dá pra reduzir item de um pedido PAGO.'
    item = next((it for it in pedido.itens if it.id == item_id), None)
    if item is None:
        return False, 'Item não encontrado neste pedido.'
    db.session.refresh(item)          # relê a qtd sob a trava (concorrente pode ter mudado)
    try:
        nova = int(nova_qtd)
    except (TypeError, ValueError):
        return False, 'Quantidade inválida.'
    atual = int(item.quantidade or 0)
    if not (1 <= nova < atual):
        return False, (f'A nova quantidade tem que ficar entre 1 e {atual - 1} '
                       f'(hoje são {atual}). Pra remover o item ou cancelar '
                       f'tudo, use "Reembolsar e cancelar".')
    delta = atual - nova
    delta_valor = Decimal(str(item.preco_unitario or 0)) * delta

    # 1) DINHEIRO primeiro: refund parcial. Falhou -> aborta, nada mexeu.
    pago = next((p for p in pedido.pagamentos if p.status == 'pago'), None)
    charge_id = pago.pagarme_charge_id if pago else None
    if not charge_id:
        return False, 'Pedido sem cobrança paga no Pagar.me — não dá pra estornar.'
    res = pagarme.cancelar_charge(charge_id, valor_decimal=delta_valor)
    if not res.get('ok'):
        return False, f'Pagar.me recusou o estorno parcial: {res.get("erro")}'

    # 2) ESTOQUE: estorna a versao atual, aplica a qtd nova, rebaixa sob a proxima.
    loja = _loja_baixa(pedido)
    if loja:
        from app.services.baixa_venda import estornar_venda
        versao = _versao_estoque_atual(pedido)
        ref_atual, pref_atual = _ref_estoque(pedido.codigo, versao)
        estornar_venda('site', pref_atual, ref_atual,
                       usuario_id=usuario_id, loja_id=loja.id)
    item.quantidade = nova
    item.subtotal = Decimal(str(item.preco_unitario or 0)) * nova
    pedido.recalcular_total()
    if loja:
        ref_nova, pref_nova = _ref_estoque(pedido.codigo, versao + 1)
        _rebaixar_pedido(pedido, loja.id, ref_nova, pref_nova, usuario_id)

    # 3) PLANO-DO-DIA: devolve as unidades removidas (disponibilidade do site).
    if pedido.data_entrega:
        from app.services import loja_plano_dia
        if item.receita_id:
            loja_plano_dia.devolver('receita', item.receita_id,
                                    pedido.data_entrega, delta)
        elif item.produto_id:
            loja_plano_dia.devolver('produto', item.produto_id,
                                    pedido.data_entrega, delta)

    db.session.commit()
    logger.info('reduzir_item_pedido_pago: pedido %s item %s %d->%d, '
                'refund R$ %.2f', pedido.codigo, item_id, atual, nova,
                delta_valor)
    aviso_nf = (' A NF já foi emitida — CORRIJA MANUALMENTE no Tiny (o '
                'sistema não cancela NF autorizada).'
                if pedido.tiny_nota_fiscal_id else '')
    return True, (f'"{item.nome}" reduzido de {atual} para {nova}. Estornei '
                  f'R$ {delta_valor:.2f} no cartão e devolvi {delta} ao '
                  f'estoque e ao plano do dia.{aviso_nf}')


def _marcar_pago(pedido, pagamento):
    """Idempotente em si: se já está pago, no-op. Aplica baixa de estoque
    e seta `pago_em`/status.

    Lock pessimista (`with_for_update`) pra evitar RACE entre os eventos
    `order.paid` e `charge.paid` (que o Pagar.me dispara quase juntos):
    sem o lock, os dois workers liam `aguardando_pagamento`, marcavam pago
    em paralelo e mandavam DOIS e-mails de confirmação pro cliente (visto
    em 19/06/2026 — pedido 1491A6B5 recebeu 2 e-mails 'pedido confirmado').

    Em SQLite (testes/dev) o FOR UPDATE vira no-op silencioso — não quebra."""
    db.session.refresh(pedido, with_for_update=True)
    if pedido.status == 'pago':
        return False  # já processado
    pedido.status = 'pago'
    pedido.pago_em = agora()
    if pagamento:
        pagamento.status = 'pago'
        pagamento.pago_em = agora()
    _baixar_estoque(pedido)
    _reservar_no_plano_do_dia(pedido)
    _enviar_confirmacao(pedido)
    # NF NÃO entra aqui: ela commita por dentro (tiny_nf.emitir_nf) e não pode
    # rodar no meio da transação do pagamento. É chamada pelos callers DEPOIS
    # do commit do pago/baixa (processar_webhook / conciliar_pedido).
    return True


def _enviar_confirmacao(pedido):
    """E-mail de confirmação pro cliente (best-effort — nunca derruba o
    processamento do pagamento se o e-mail falhar)."""
    try:
        from app.services import email as email_svc
        if email_svc.disponivel():
            email_svc.enviar_confirmacao_pedido(pedido)
    except Exception:  # noqa: BLE001
        logger.exception('confirmacao de pedido por email falhou')


def _emitir_nf_e_enviar(pedido):
    """Emite a NF no Tiny e manda o e-mail com o link da DANFE pro cliente
    (decisão do dono 19/06/2026 — NF automática logo após o pagamento).

    Best-effort: NUNCA derruba o processamento do pagamento. Se a emissão
    falhar (Tiny fora, item sem SKU mapeado, rejeição fiscal), o pedido
    continua pago + estoque baixado; a NF fica pendente pra reemitir manual
    em `/admin/loja-online/pedidos/<codigo>` (mesmo botão de antes).

    `emitir_nf` já é IDEMPOTENTE: se a NF já foi emitida pra esse pedido, é
    no-op — então uma reentrega do webhook 'paid' não duplica.

    DEVE ser chamada DEPOIS do commit do pago/baixa — `tiny_nf.emitir_nf`
    commita por dentro, e qualquer falha aqui dá rollback pra NUNCA deixar a
    sessão suja (senão polui o request/teste seguinte)."""
    try:
        from app.services import email as email_svc
        from app.services import tiny_nf
        res = tiny_nf.emitir_nf(pedido)
        if not res.get('ok'):
            logger.warning('NF do pedido %s não foi emitida automaticamente: %s',
                           pedido.codigo, res.get('msg'))
            return
        if email_svc.disponivel():
            email_svc.enviar_nf_emitida(pedido)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('emissão automática de NF falhou (pedido %s)',
                         pedido.codigo)


def _marcar_estornado(pedido, pagamento):
    if pedido.status == 'cancelado':
        return False
    estado_anterior = pedido.status
    pedido.status = 'cancelado'
    pedido.motivo_cancelamento = 'reembolso'
    pedido.cancelado_em = agora()
    if pagamento:
        pagamento.status = 'estornado'
    # Só estorna estoque se já havia sido pago (= baixou).
    if estado_anterior == 'pago':
        _estornar_estoque(pedido)
        _devolver_ao_plano_do_dia(pedido)
    elif estado_anterior == 'aguardando_pagamento':
        # Pedido nunca chegou a pago — libera reserva (Pix expirado,
        # cancelamento manual antes do pagamento, etc).
        from app.services import loja_estoque_reserva
        loja = _loja_baixa(pedido)
        if loja:
            loja_estoque_reserva.liberar(pedido, loja_id=loja.id)
    return True


def _cobranca_ja_estornada_no_gateway(pagamento):
    """True se o Pagar.me CONFIRMA que a cobrança não está mais paga (cancelada/
    reembolsada). Usado quando `cancelar_charge` recusa com "This charge can not
    be canceled" (HTTP 412) — normalmente porque o dono já estornou no PAINEL do
    Pagar.me. Nesse caso o dinheiro já voltou e faz sentido sincronizar o estorno
    LOCAL (estoque/plano/status) em vez de travar.

    Fail-CLOSED (dinheiro, peso especial): qualquer dúvida — sem order_id,
    gateway fora, ou ainda 'paid' — retorna False, pra NUNCA creditar estoque
    com o dinheiro ainda preso no cartão do cliente."""
    if not pagamento or not pagamento.pagarme_order_id:
        return False
    consulta = pagarme.consultar_order(pagamento.pagarme_order_id)
    if not consulta.get('ok') or consulta.get('pago'):
        return False
    return consulta.get('charge_status') in (
        'canceled', 'cancelled', 'refunded', 'voided', 'chargedback')


def reembolsar_pedido(pedido):
    """Reembolso manual (admin). Cancela/estorna a cobrança no Pagar.me e,
    se já estava pago, devolve o estoque. Devolve (ok, mensagem).

    Idempotente o suficiente: se o pedido já está cancelado, no-op. O
    webhook 'charge.refunded' do Pagar.me também chega depois — como
    _marcar_estornado checa status, não duplica.

    Cobrança JÁ estornada no gateway (o dono cancelou no painel do Pagar.me e a
    cobrança não aceita novo cancelamento — HTTP 412): em vez de travar, CONFIRMA
    no gateway que o dinheiro voltou e SINCRONIZA o estorno local (caso real
    08/07/2026, pedido 6537F0EB). Só sincroniza com a confirmação do gateway —
    se ele ainda mostrar a cobrança ativa, o erro sobe."""
    if pedido.status == 'cancelado':
        return True, 'Pedido já estava cancelado.'
    # Acha a cobrança paga (ou a última com charge_id) pra estornar no gateway.
    pago = next((p for p in pedido.pagamentos if p.status == 'pago'), None)
    charge_id = (pago.pagarme_charge_id if pago else None) or next(
        (p.pagarme_charge_id for p in pedido.pagamentos
         if p.pagarme_charge_id), None)
    ja_estornado = False
    if charge_id:
        res = pagarme.cancelar_charge(charge_id)
        if not res.get('ok'):
            if not _cobranca_ja_estornada_no_gateway(pago):
                return False, (
                    f'Pagar.me recusou o estorno: {res.get("erro")}. Se você '
                    f'JÁ cancelou/reembolsou no painel do Pagar.me, aguarde uns '
                    f'segundos e clique de novo — o sistema sincroniza o '
                    f'cancelamento aqui quando o gateway confirmar o estorno.')
            ja_estornado = True
            logger.warning('reembolsar_pedido %s: cobrança já estornada no '
                           'gateway (%s) — sincronizando estorno local só aqui',
                           pedido.codigo, res.get('erro'))
    _marcar_estornado(pedido, pago)
    db.session.commit()
    if ja_estornado:
        return True, ('A cobrança já estava estornada no Pagar.me — sincronizei '
                      'o cancelamento aqui (estoque e plano devolvidos). Confira '
                      'a NF no Tiny se já foi emitida.')
    return True, 'Pedido reembolsado e estornado.'


def conciliar_pedido(codigo, aplicar=False):
    """Conciliação manual (rede de segurança pra webhook que não chegou).

    A fonte da verdade do pagamento é o GATEWAY, não o nosso retorno de
    checkout nem a chegada do webhook. Esta função consulta o Pagar.me pelo
    `pagarme_order_id` salvo e, se o gateway confirmar PAGO e `aplicar=True`,
    marca o pedido como pago localmente — MESMA lógica do webhook
    (`_marcar_pago`: baixa estoque + e-mail). Ignora a tabela de
    idempotência (`PagarmeEvento`), então funciona mesmo quando um reenvio
    do webhook viraria "duplicado". `_marcar_pago` é idempotente (no-op se
    já pago), então rodar duas vezes — ou o webhook chegar depois — não
    duplica baixa de estoque.

    Sem `aplicar` = dry-run (só diz o que o gateway reporta). Nunca levanta."""
    from app.models import PedidoOnline
    from app.services import pagarme
    p = PedidoOnline.query.filter_by(codigo=codigo).first()
    if not p:
        return {'ok': False, 'erro': 'pedido não encontrado', 'codigo': codigo}
    pag = next((pg for pg in p.pagamentos if pg.pagarme_order_id), None)
    if not pag:
        return {'ok': False, 'erro': 'pedido sem pagarme_order_id (não '
                'iniciado no Pagar.me?)', 'codigo': codigo,
                'status_local': p.status}
    consulta = pagarme.consultar_order(pag.pagarme_order_id)
    if not consulta.get('ok'):
        return {'ok': False, 'erro': 'falha ao consultar Pagar.me',
                'detalhe': consulta, 'codigo': codigo}
    out = {'ok': True, 'codigo': p.codigo, 'status_local': p.status,
           'pagarme_status': consulta.get('status'),
           'pagarme_pago': bool(consulta.get('pago')),
           'order_id': pag.pagarme_order_id}
    if not consulta.get('pago'):
        out['acao'] = 'nada — Pagar.me NÃO confirma pago'
        return out
    if not aplicar:
        out['acao'] = ('dry-run — Pagar.me confirma PAGO. '
                       'Adicione ?aplicar=1 pra marcar.')
        return out
    try:
        mudou = _marcar_pago(p, pag)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.exception('conciliar_pedido %s: _marcar_pago falhou', codigo)
        return {'ok': False, 'erro': f'falha ao marcar pago: {exc}',
                'codigo': codigo, 'status_local': p.status}
    if mudou:
        _emitir_nf_e_enviar(p)  # após commit; isolado
    out['acao'] = 'MARCADO PAGO' if mudou else 'já estava pago (no-op)'
    out['status_local'] = p.status
    return out


def processar_webhook(evento):
    """Recebe o JSON do webhook (já parsed). Idempotente por `id` do
    evento (PagarmeEvento). Retorna dict com o que foi feito (pra
    logs/teste); NUNCA levanta exceção pro chamador."""
    if not isinstance(evento, dict):
        return {'ok': False, 'erro': 'payload inválido'}
    evt_id = evento.get('id') or evento.get('event_id')
    tipo = (evento.get('type') or evento.get('event') or '').lower()
    if not evt_id:
        return {'ok': False, 'erro': 'evento sem id'}

    # Idempotência: tenta gravar primeiro; se já existe, era reentrega.
    novo = PagarmeEvento(evento_id=str(evt_id), tipo=tipo)
    db.session.add(novo)
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001 — IntegrityError em PG / SQLite
        db.session.rollback()
        return {'ok': True, 'duplicado': True}

    data = evento.get('data') or evento.get('object') or {}
    pedido, pagamento = _encontrar_pedido(data)
    if not pedido:
        logger.warning('webhook %s: pedido não encontrado (%s)', tipo, evt_id)
        return {'ok': True, 'sem_pedido': True}

    try:
        if tipo in ('order.paid', 'charge.paid'):
            mudou = _marcar_pago(pedido, pagamento)
            db.session.commit()
            # NF + e-mail SÓ depois do commit (isolado; não suja a transação
            # do pagamento). Idempotente: reenvio do webhook não duplica.
            if mudou:
                _emitir_nf_e_enviar(pedido)
            return {'ok': True, 'pago': True, 'mudou': mudou}
        if tipo in ('charge.refunded', 'order.canceled',
                    'charge.cancelled', 'charge.refunded.partial'):
            # ESTORNO AUTOMÁTICO DESATIVADO (decisão do dono 18/06/2026).
            # Um cancelamento em massa no gateway (bug/abuso — já aconteceu
            # no VNDA no passado) NÃO pode cancelar pedidos + devolver
            # estoque em cascata por aqui. O estorno agora é SEMPRE manual,
            # pelo admin ("Reembolsar e cancelar"), que emite o refund real
            # e devolve o estoque de forma deliberada e individual. O evento
            # fica registrado em PagarmeEvento só pra auditoria.
            logger.warning('webhook %s (%s): estorno automático DESATIVADO — '
                           'requer ação manual no admin', tipo, evt_id)
            return {'ok': True, 'estorno_ignorado': tipo}
        if tipo in ('order.payment_failed', 'charge.payment_failed'):
            if pagamento:
                pagamento.status = 'falhou'
                pagamento.erro = (data.get('failure_reason')
                                  or data.get('status')
                                  or 'recusado pelo Pagar.me')
            db.session.commit()
            return {'ok': True, 'falhou': True}
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.exception('webhook %s falhou: %s', tipo, exc)
        return {'ok': False, 'erro': str(exc)}
    # Tipo não tratado — registra (já está em PagarmeEvento) e retorna OK.
    return {'ok': True, 'ignorado': tipo}
