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
from app.services import estoque_helpers, pagarme
from app.utils import agora

logger = logging.getLogger(__name__)

# Fallback de loja de origem do estoque pra venda do site quando não
# houver configuração explícita. Mesma loja que o VNDA usa hoje (`Loja
# Anesio Pinto Rosa`) — mantém a paridade quando virarmos a chave.
_LOJA_SITE_NOME_DEFAULT = 'Loja Anesio Pinto Rosa'

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
        pag.status = 'falhou'
        pag.erro = res.get('erro') or 'falha desconhecida'
        db.session.commit()
        return None, [res.get('erro') or 'Pagamento recusado pelo cartão.']

    pag.pagarme_order_id = res.get('order_id')
    pag.pagarme_charge_id = res.get('charge_id')
    # Resposta imediata: 'paid' já vem aqui. A baixa de estoque acontece
    # quando o webhook 'paid' chegar (mesma fonte de verdade pros dois
    # métodos — evita race com o webhook).
    if (res.get('status') or '').lower() in ('failed', 'refused', 'canceled'):
        pag.status = 'falhou'
        pag.erro = f'cartão recusado ({res.get("status")})'
        db.session.commit()
        return None, ['Pagamento recusado pelo cartão.']
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


def _baixar_estoque(pedido, usuario_id=None):
    """Aplica MovEstoqueLoja('venda_site') por item do pedido. Itens sem
    FK (receita_id/produto_id) são pulados (sinal de catálogo solto —
    logado WARNING, igual seru_sync)."""
    loja = _loja_baixa(pedido)
    if not loja:
        logger.warning('venda_site: sem loja de origem (codigo=%s)',
                       pedido.codigo)
        return {'baixado': 0, 'faltou': 0, 'pulado': len(pedido.itens)}
    ref = f'Site #{pedido.codigo}'
    total = {'baixado': 0, 'faltou': 0, 'pulado': 0}
    for it in pedido.itens:
        if it.receita_id:
            chave = {'loja_id': loja.id, 'receita_id': it.receita_id}
        elif it.produto_id:
            chave = {'loja_id': loja.id, 'produto_id': it.produto_id}
        else:
            logger.warning('venda_site: item sem FK em pedido %s (%s)',
                           pedido.codigo, it.nome)
            total['pulado'] += 1
            continue
        r = estoque_helpers.baixar_loja_por_prioridade(
            chave, int(it.quantidade),
            tipo_mov='venda_site',
            sem_estoque_tipo='venda_site_sem_estoque',
            referencia=ref, usuario_id=usuario_id)
        total['baixado'] += r['baixado']
        total['faltou'] += r['faltou']
    return total


def _estornar_estoque(pedido):
    """Reverte a baixa: pra cada MovEstoqueLoja('venda_site') do pedido,
    devolve a quantidade ao saldo de EstoqueLoja e registra um
    MovEstoqueLoja('venda_site_estorno') com quantidade negativa
    (espelha o estorno do seru_sync)."""
    from app.models import EstoqueLoja, MovEstoqueLoja
    ref_baixa = f'Site #{pedido.codigo}'
    movs = (MovEstoqueLoja.query
            .filter(MovEstoqueLoja.tipo == 'venda_site',
                    MovEstoqueLoja.referencia == ref_baixa)
            .all())
    n = 0
    for m in movs:
        el = EstoqueLoja.query.get(m.estoque_loja_id)
        if el is not None:
            el.quantidade = (el.quantidade or 0) + m.quantidade
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=m.estoque_loja_id,
            tipo='venda_site_estorno',
            quantidade=-m.quantidade,
            referencia=f'Estorno Site #{pedido.codigo}'))
        n += 1
    return n


def _marcar_pago(pedido, pagamento):
    """Idempotente em si: se já está pago, no-op. Aplica baixa de estoque
    e seta `pago_em`/status."""
    if pedido.status == 'pago':
        return False  # já processado
    pedido.status = 'pago'
    pedido.pago_em = agora()
    if pagamento:
        pagamento.status = 'pago'
        pagamento.pago_em = agora()
    _baixar_estoque(pedido)
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
    pedido.cancelado_em = agora()
    if pagamento:
        pagamento.status = 'estornado'
    # Só estorna estoque se já havia sido pago (= baixou).
    if estado_anterior == 'pago':
        _estornar_estoque(pedido)
    return True


def reembolsar_pedido(pedido):
    """Reembolso manual (admin). Cancela/estorna a cobrança no Pagar.me e,
    se já estava pago, devolve o estoque. Devolve (ok, mensagem).

    Idempotente o suficiente: se o pedido já está cancelado, no-op. O
    webhook 'charge.refunded' do Pagar.me também chega depois — como
    _marcar_estornado checa status, não duplica."""
    if pedido.status == 'cancelado':
        return True, 'Pedido já estava cancelado.'
    # Acha a cobrança paga (ou a última com charge_id) pra estornar no gateway.
    pago = next((p for p in pedido.pagamentos if p.status == 'pago'), None)
    charge_id = (pago.pagarme_charge_id if pago else None) or next(
        (p.pagarme_charge_id for p in pedido.pagamentos
         if p.pagarme_charge_id), None)
    if charge_id:
        res = pagarme.cancelar_charge(charge_id)
        if not res.get('ok'):
            return False, f'Pagar.me recusou o estorno: {res.get("erro")}'
    _marcar_estornado(pedido, pago)
    db.session.commit()
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
