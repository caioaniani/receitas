"""Cobrança única de kits, com efeitos financeiros por entrega real."""
import logging

import requests

from app.extensions import db
from app.models import EntregaKit
from app.services.compra_kits import grupo_do_pedido, pedidos_do_grupo, reler_entregas
from app.utils import agora, fmt_brl, para_brt

logger = logging.getLogger(__name__)


def _alertar_dono(texto, chave, **kw):
    """Aviso ao dono sobre dinheiro que precisa de conferência (sessão isolada,
    crítico com teto/hora). Best-effort: nunca derruba o pagamento."""
    from app.services import loja_alerta
    loja_alerta.alertar_pedido_pago(texto, chave, **kw)


def travar(pedido, *, todos=False):
    """Sempre compra → pedidos em ID crescente; compartilha a trava com expiração."""
    compra = grupo_do_pedido(pedido)
    if compra:
        db.session.refresh(compra, with_for_update=True)
        pedidos = sorted(pedidos_do_grupo(compra), key=lambda p: p.id)
        if todos:
            for entrega in pedidos:
                db.session.refresh(entrega, with_for_update=True)
        else:
            db.session.refresh(pedido, with_for_update=True)
        reler_entregas(compra)
        return compra, pedidos
    db.session.refresh(pedido, with_for_update=True)
    return None, [pedido]


def preparar_cobranca(pedido):
    """Normaliza o destinatário financeiro e valida todo o grupo sob trava."""
    compra, pedidos = travar(pedido, todos=True)
    principal = compra.pedido_principal if compra else pedido
    if ((compra and (compra.pago_em or compra.expira_em <= agora()))
            or any(p.status != 'aguardando_pagamento' or p.pago_em for p in pedidos)):
        return principal, False
    return principal, True


def marcar_pago(pedido, pagamento, *, enviar_confirmacao=True, usuario_id=None):
    """Persiste todas as entregas juntas, sem consumir estoque físico antecipado."""
    from app.services import loja_pagamento

    compra, pedidos = travar(pedido, todos=True)
    if not compra:
        raise ValueError('Pedido sem compra de kit')
    status_anterior = None
    if pagamento:
        db.session.refresh(pagamento)
        status_anterior = pagamento.status
        if pagamento.status != 'estornado':
            pagamento.status = 'pago'
            pagamento.pago_em = pagamento.pago_em or agora()
    principal = compra.pedido_principal
    if compra.pago_em:
        # Reentrega do mesmo evento chega com a tentativa já 'pago': sem aviso.
        # Outra tentativa (QR antigo, cartão após Pix, gateway após recebimento
        # externo) paga depois da confirmação = o cliente pagou em dobro.
        # Mesma regra do avulso (loja_pagamento._marcar_pago, 27/09/2026).
        segundo = bool(
            pagamento and status_anterior not in ('pago', 'estornado')
            and (loja_pagamento._tem_pagamento_externo(principal)
                 or any(p.id != pagamento.id and p.status == 'pago'
                        for p in principal.pagamentos)))
        if segundo:
            logger.warning('Kit %s: segundo pagamento recebido (%s); cliente cobrado '
                           'em duplicidade.', compra.id,
                           pagamento.pagarme_charge_id or pagamento.id)
            _alertar_dono(
                f'⚠️ Compra de kit {principal.codigo} ({compra.kit_nome}) foi PAGA '
                f'DUAS VEZES (nova cobrança via {pagamento.metodo}, '
                f'{fmt_brl(pagamento.valor)}). Estorne a cobrança a mais no painel '
                'do Pagar.me.',
                f'kit_duplicado|{compra.id}|{pagamento.id}')
        return False
    # Cancelamento deliberado não pode ser revertido por um webhook atrasado.
    # QR expirado é diferente: o gateway ainda pode confirmar dinheiro recebido.
    from app.models.loja_online import MOTIVOS_EXPIRACAO_AUTOMATICA
    if any(p.pago_em or p.divulgacao or p.status not in ('aguardando_pagamento', 'cancelado')
           or (p.status == 'cancelado'
               and p.motivo_cancelamento not in MOTIVOS_EXPIRACAO_AUTOMATICA)
           for p in pedidos):
        logger.error('Pagamento recebido para kit %s com entrega incompatível; '
                     'requer conferência do owner.', compra.id)
        # Dinheiro do gateway numa compra que não aguardava pagamento: o dono
        # precisa estornar ou falar com o cliente. Só no PRIMEIRO registro da
        # tentativa (order.paid e charge.paid chegam os dois; a tentativa já
        # 'pago' quer dizer que este aviso já saiu). Sem tentativa identificada
        # não há o que estornar por aqui — o log acima registra.
        if pagamento and status_anterior not in ('pago', 'estornado'):
            cancelada = any(p.status == 'cancelado' and p.motivo_cancelamento != 'pix_expirado'
                            for p in pedidos)
            situacao = ('estava CANCELADA e recebeu pagamento. Ela continua cancelada'
                        if cancelada else
                        'recebeu pagamento, mas as entregas não aguardavam pagamento. '
                        'Nada foi alterado')
            _alertar_dono(
                f'⚠️ Compra de kit {principal.codigo} ({compra.kit_nome}) {situacao} '
                f'({fmt_brl(pagamento.valor)} via {pagamento.metodo}) — estorne o valor '
                'no painel do Pagar.me ou fale com o cliente.',
                f'kit_cancelado_pago|{compra.id}|{pagamento.id}')
        return False
    from app.services.kits_capacidade import reservar_compra
    reservar_compra(compra, pagamento_recebido=True)
    from app.services.loja_fiscal import agendar
    instante = agora()
    for entrega in pedidos:
        entrega.status = 'pago'
        entrega.pago_em = instante
        entrega.cancelado_em = None
        entrega.motivo_cancelamento = None
        entrega.reserva_expira_em = None
        agendar(entrega, base=instante)
    compra.pago_em = instante
    if enviar_confirmacao:
        loja_pagamento._enviar_confirmacao(compra.pedido_principal)
    return True


def apos_confirmacao(pedido):
    """Uma confirmação; NF e Purchase por entrega, com seus valores individuais."""
    from app.services import loja_pagamento

    compra = grupo_do_pedido(pedido)
    principal = compra.pedido_principal if compra else pedido
    pedidos = pedidos_do_grupo(compra) if compra else [pedido]
    loja_pagamento._enviar_confirmacao(principal)
    for entrega in pedidos:
        # Mesmo transaction_id do acompanhamento individual no navegador;
        # somar os eventos representa exatamente o total pago pelo grupo.
        loja_pagamento._reportar_purchase(entrega)


def cancelar_entrega_paga(pedido):
    """Efeito LOCAL após refund confirmado. Não presume retorno físico de produtos."""
    from app.services.kits_capacidade import liberar_entregas

    registro = db.session.get(EntregaKit, pedido.id)
    if not registro:
        raise ValueError('Pedido sem entrega de kit')
    pedido.status = 'cancelado'
    pedido.motivo_cancelamento = 'reembolso'
    pedido.cancelado_em = agora()
    liberar_entregas([registro])


# Leitura da cobrança (GET /charges/{id}) para resolver um estorno incerto.
# `pagarme.py` só tem `consultar_order`, que não traz valores estornados.
_CHARGE_TOTALMENTE_ESTORNADA = frozenset({'refunded', 'canceled', 'cancelled', 'voided'})
# Transação que só existe como estorno concluído. Os status parciais ficam de
# fora: podem ser a transação do PAGAMENTO com o valor cheio, o que faria um
# refund de outra entrega parecer ter coberto esta.
_TRANSACAO_ESTORNADA = frozenset({'refunded', 'voided'})
# Falha explícita do cancelamento/estorno no adquirente.
_TRANSACAO_ESTORNO_FALHOU = frozenset({'error_on_refunding', 'error_on_voiding'})


def _ler_charge(charge_id):
    """Cobrança crua do Pagar.me, ou None quando não dá pra confiar na leitura.

    Fail-closed: sem chave, rede/HTTP com falha, corpo sem JSON ou de outra
    cobrança = None (o estorno continua 'solicitado')."""
    from app.services import pagarme
    if not pagarme.disponivel() or not charge_id:
        return None
    try:
        r = requests.get(f'{pagarme._BASE}/charges/{charge_id}',
                         headers=pagarme._headers(), timeout=pagarme._TIMEOUT)
    except requests.RequestException as exc:
        logger.warning('Estorno de kit: leitura da cobrança %s falhou: %s', charge_id, exc)
        return None
    if r.status_code != 200:
        logger.warning('Estorno de kit: cobrança %s respondeu HTTP %s', charge_id, r.status_code)
        return None
    try:
        corpo = r.json()
    except ValueError:
        return None
    if not isinstance(corpo, dict) or corpo.get('id') != charge_id:
        return None
    return corpo


def _centavos_ou_none(valor):
    return valor if type(valor) is int and valor >= 0 else None


def _estornado_na_charge(charge):
    """Menor valor comprovadamente devolvido na cobrança (centavos) ou None.

    `canceled_amount` é o total devolvido. Refund PARCIAL mantém a cobrança
    'paid', então o status sozinho não prova nada; cobrança inteira estornada
    vale o valor pago; na falta dos dois, a última transação de estorno
    concluído é um piso (nunca soma um valor que o gateway não mostrou)."""
    evidencias = []
    cancelado = _centavos_ou_none(charge.get('canceled_amount'))
    if cancelado is not None:
        evidencias.append(cancelado)
    if str(charge.get('status') or '').lower() in _CHARGE_TOTALMENTE_ESTORNADA:
        total = (_centavos_ou_none(charge.get('paid_amount'))
                 or _centavos_ou_none(charge.get('amount')))
        if total is not None:
            evidencias.append(total)
    transacao = charge.get('last_transaction') or {}
    if (isinstance(transacao, dict)
            and str(transacao.get('status') or '').lower() in _TRANSACAO_ESTORNADA
            and transacao.get('success') is not False):
        valor = _centavos_ou_none(transacao.get('amount'))
        if valor is not None:
            evidencias.append(valor)
    return max(evidencias) if evidencias else None


def _transacao_depois_de(transacao, momento):
    """A transação do gateway foi criada a partir de `momento` (BRT naive)?

    False quando não dá para provar: sem data, data ilegível ou sem fuso."""
    from datetime import datetime
    bruto = transacao.get('created_at') if isinstance(transacao, dict) else None
    if not isinstance(bruto, str) or momento is None:
        return False
    try:
        criada = datetime.fromisoformat(bruto.strip().replace('Z', '+00:00'))
    except ValueError:
        return False
    if criada.tzinfo is None:
        return False
    return para_brt(criada) >= momento


def _decidir_estorno_incerto(registro, charge):
    """('confirmado'|'recusado'|'duvida', detalhe) para um estorno 'solicitado'.

    Confirma só se o valor devolvido na cobrança cobre TODOS os estornos
    confirmados e pendentes dela (inclusive este): com duas entregas incertas
    e só uma devolvida, não dá pra saber qual — as duas seguem pendentes.

    'recusado' libera um NOVO estorno, então exige prova de que a falha é
    DESTA entrega: ela é a única pendente na cobrança e a transação falha é
    posterior ao pedido de estorno. Sem isso, a falha do estorno de outra
    entrega (a última transação da cobrança) marcava esta como recusada e o
    dono pedia de novo um estorno que pode já ter saído (revisão 28/09/2026)."""
    from app.models.kits_cafe import ReembolsoKit
    from app.services import pagarme
    if charge is None:
        return 'duvida', 'não foi possível consultar a cobrança no Pagar.me agora'
    registros = ReembolsoKit.query.filter(
        ReembolsoKit.pagarme_charge_id == registro.pagarme_charge_id,
        ReembolsoKit.status.in_(('confirmado', 'solicitado'))).all()
    confirmados = sum(pagarme._centavos(r.valor) for r in registros if r.status == 'confirmado')
    necessario = sum(pagarme._centavos(r.valor) for r in registros)
    estornado = _estornado_na_charge(charge)
    if estornado is not None and estornado >= necessario:
        return 'confirmado', 'estorno concluído no Pagar.me'
    transacao = charge.get('last_transaction') or {}
    status_transacao = (str(transacao.get('status') or '').lower()
                        if isinstance(transacao, dict) else '')
    if status_transacao in _TRANSACAO_ESTORNO_FALHOU:
        pendentes = [r for r in registros if r.status == 'solicitado']
        if ((estornado is None or estornado <= confirmados)
                and len(pendentes) == 1 and pendentes[0].pedido_id == registro.pedido_id
                and _transacao_depois_de(transacao, registro.criado_em)):
            return 'recusado', f'estorno não realizado pelo Pagar.me ({status_transacao})'
        return 'duvida', ('o Pagar.me mostra uma falha de estorno nesta cobrança, mas não '
                          'dá para saber se é a desta entrega')
    if estornado is None:
        return 'duvida', 'o Pagar.me ainda não mostra o valor devolvido'
    return 'duvida', (f'o Pagar.me mostra {fmt_brl(estornado / 100)} devolvido(s), '
                      f'menos que os {fmt_brl(necessario / 100)} solicitados nesta cobrança')


RESOLUCOES_MANUAIS = ('confirmado', 'recusado')


def resolver_estorno_manual(pedido, decisao, motivo, usuario):
    """O dono decide um estorno 'solicitado' que o gateway não esclarece.

    Caso típico: duas entregas pendentes na mesma cobrança e só uma devolvida
    — nenhuma é decidida sozinha e as duas coletas ficam bloqueadas. Depois
    de conferir no painel do Pagar.me, o dono marca:
      - 'confirmado': o valor desta entrega voltou ao cliente → a entrega é
        cancelada pelos mesmos efeitos locais do estorno confirmado;
      - 'recusado': nada foi devolvido → a entrega segue ativa e um novo
        estorno pode ser pedido.
    Nunca chama o gateway. O motivo fica no registro (e no /audit)."""
    from app.models.kits_cafe import ReembolsoKit
    from app.services.tiny_nf import _trava_nf_kit

    motivo = ' '.join(str(motivo or '').split())
    if decisao not in RESOLUCOES_MANUAIS:
        return False, 'Escolha se o valor foi devolvido ou não.'
    if len(motivo) < 10:
        return False, ('Descreva o que você conferiu no Pagar.me (pelo menos 10 '
                       'caracteres) — fica registrado junto do estorno.')
    with _trava_nf_kit(pedido.id) as adquirido:
        if not adquirido:
            return False, ('A nota fiscal desta entrega está sendo processada. '
                           'Aguarde e tente novamente.')
        try:
            travar(pedido, todos=True)
            registro = db.session.get(ReembolsoKit, pedido.id)
            if registro is not None:
                db.session.refresh(registro, with_for_update=True)
            if registro is None or registro.status != 'solicitado':
                db.session.rollback()
                return False, 'Esta entrega não tem estorno pendente de conferência.'
            nome = getattr(usuario, 'nome', None) or getattr(usuario, 'login', None) or 'dono'
            nota = (f'Conferido manualmente por {nome} em {agora():%d/%m/%Y %H:%M} '
                    f'({"valor devolvido" if decisao == "confirmado" else "nada devolvido"}): '
                    f'{motivo}')[:2000]
            registro.status = decisao
            registro.erro = nota
            if decisao == 'confirmado':
                registro.confirmado_em = agora()
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('Falha na conferência manual do estorno %s', pedido.id)
            return False, 'Não foi possível registrar a conferência. Tente novamente.'
        if decisao == 'recusado':
            return True, ('Registrado: nenhum valor foi devolvido. A entrega continua ativa '
                          'e um novo estorno pode ser pedido, se ainda for o caso.')
        # Mesmos efeitos locais do estorno confirmado pelo gateway.
        return _reembolsar_entrega(pedido)


def reembolsar_entrega(pedido):
    """Serializa o reembolso com emissão/envio da NF desta entrega."""
    from app.services.tiny_nf import _trava_nf_kit
    with _trava_nf_kit(pedido.id) as adquirido:
        if not adquirido:
            return False, 'A nota fiscal desta entrega está sendo processada. Aguarde e tente novamente.'
        return _reembolsar_entrega(pedido)


def _reembolsar_entrega(pedido):
    """Refund parcial da cobrança comum, restrito ao valor desta entrega.

    A API documenta idempotência de criação de pedidos, não do DELETE de
    cobrança. Por isso a intenção é persistida ANTES da rede: se a resposta
    for incerta ou o processo cair, outro clique não devolve dinheiro de novo.
    Confirmado é persistido antes dos efeitos locais para permitir recuperação
    de falha de banco sem repetir a chamada financeira.
    """
    from app.models.kits_cafe import ReembolsoKit
    from app.services import loja_pagamento, pagarme

    try:
        compra, pedidos = travar(pedido, todos=True)
        if not compra:
            return False, 'Pedido não pertence a uma compra de kit.'
        if loja_pagamento._tem_pagamento_externo(pedido):
            return False, ('Este pagamento foi recebido fora do site. O Pagar.me '
                           'não pode devolver esse valor; o estorno automático não está disponível.')
        if pedido.status == 'cancelado':
            return True, 'Esta entrega já estava cancelada.'
        if not compra.pago_em or not pedido.pago_em:
            return False, 'Esta entrega ainda não teve pagamento confirmado.'
        pago = next((p for p in compra.pedido_principal.pagamentos
                     if p.status == 'pago' and p.pagarme_charge_id), None)
        if not pago:
            return False, 'Compra sem cobrança paga no Pagar.me; confira o pagamento.'
        registro = db.session.get(ReembolsoKit, pedido.id)
        if registro and registro.status == 'solicitado':
            # Resposta incerta de uma tentativa anterior (timeout, 202,
            # pending_refund). Nunca reenvia o DELETE: consulta a cobrança no
            # gateway SEM segurar as travas de linha (a trava fiscal desta
            # entrega continua) e só decide com prova. Sem prova, mantém.
            charge_id = registro.pagarme_charge_id
            db.session.commit()
            leitura = _ler_charge(charge_id)
            compra, pedidos = travar(pedido, todos=True)
            db.session.refresh(registro, with_for_update=True)
            if registro.status == 'solicitado':
                decisao, detalhe = _decidir_estorno_incerto(registro, leitura)
                if decisao == 'recusado':
                    registro.status = 'recusado'
                    registro.erro = detalhe[:2000]
                    db.session.commit()
                    return False, (f'O Pagar.me recusou o estorno anterior desta entrega: {detalhe}. '
                                   'Nenhum valor foi devolvido e a entrega continua ativa. '
                                   'Solicite o estorno de novo, se ainda for o caso.')
                if decisao != 'confirmado':
                    registro.erro = f'{agora():%d/%m/%Y %H:%M}: {detalhe}'[:2000]
                    db.session.commit()
                    return False, ('Já existe uma solicitação de estorno desta entrega sem '
                                   f'conclusão confirmada ({detalhe}). Confira no Pagar.me '
                                   'antes de qualquer novo estorno.')
                registro.status = 'confirmado'
                registro.confirmado_em = agora()
                registro.erro = None
                db.session.commit()
                compra, pedidos = travar(pedido, todos=True)
                db.session.refresh(registro, with_for_update=True)
        if not registro or registro.status == 'recusado':
            if not registro:
                registro = ReembolsoKit(pedido_id=pedido.id, valor=pedido.valor_total,
                                        pagarme_charge_id=pago.pagarme_charge_id)
                db.session.add(registro)
            registro.status = 'solicitado'
            registro.erro = None
            registro.criado_em = agora()
            db.session.commit()
            # A coleta vê o registro solicitado e aguarda a resolução. Um
            # segundo clique encontra o mesmo registro e nunca chama a API.
            resultado = pagarme.cancelar_charge(
                registro.pagarme_charge_id, valor_decimal=registro.valor, detalhar=True)
            compra, pedidos = travar(pedido, todos=True)
            db.session.refresh(registro, with_for_update=True)
            if not resultado.get('ok'):
                registro.erro = str(resultado.get('erro') or 'Resposta sem confirmação')[:2000]
                # Sem indicação explícita de recusa, assume que dinheiro pode
                # ter saído: não transforma timeout/erro desconhecido em retry.
                if resultado.get('incerto') is False:
                    registro.status = 'recusado'
                db.session.commit()
                return False, (f'Estorno desta entrega não confirmado: {registro.erro}. '
                               'As outras entregas continuam ativas.')
            registro.status = 'confirmado'
            registro.confirmado_em = agora()
            db.session.commit()
            compra, pedidos = travar(pedido, todos=True)
            db.session.refresh(registro, with_for_update=True)
        if registro.status != 'confirmado':
            return False, 'Estorno pendente de conferência.'
        # Outro worker pode concluir os efeitos locais entre o commit do
        # comprovante financeiro e a reaquisição das travas. Relê sob lock;
        # repetir a devolução do plano diminuiria a reserva de outro cliente.
        if pedido.status == 'cancelado':
            return True, 'Esta entrega já estava cancelada.'
        cancelar_entrega_paga(pedido)
        # A cobrança representa o mês. Cancelar a primeira entrega não pode
        # torná-la estornada enquanto há outras entregas pagas ainda ativas.
        if all(p.status == 'cancelado' for p in pedidos):
            pago.status = 'estornado'
        db.session.commit()
    except Exception:  # noqa: BLE001 — dinheiro não pode ser reenviado após erro
        db.session.rollback()
        logger.exception('Falha no reembolso da entrega de kit %s', pedido.id)
        return False, ('Não foi possível concluir o registro do estorno. Confira o pedido; '
                       'uma solicitação já enviada não será repetida automaticamente.')

    try:
        from app.services import email as email_svc
        if email_svc.disponivel():
            email_svc.enviar_reembolso_confirmado(pedido, valor=registro.valor, metodo=pago.metodo)
    except Exception:  # noqa: BLE001 — e-mail não desfaz dinheiro persistido
        logger.exception('Falha no comprovante de reembolso da entrega %s', pedido.id)
    return True, ('Esta entrega foi reembolsada e cancelada. As demais datas permanecem '
                  'como estavam. Confira a NF desta entrega no Tiny, se já foi emitida.')
