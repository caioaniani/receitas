"""Pedidos loja→indústria AUTOMÁTICOS + envio automático da produção
(10/08/2026, decisão do dono: "quero que o sistema, com base em venda,
estoque e previsão, faça os pedidos automaticamente de 3 dias na frente" +
AskUserQuestion "Automatizar TUDO (pedido + envio)" — REVOGA a regra de
04/07/2026 "enviar ao padeiro é gesto humano" APENAS pro envio das 18:00 de
um dia SEM ordem; ordem já enviada nunca é reescrita por caminho implícito).

Duas pontas, dois jobs do cron (`seru_cron`):

1. `gerar_pedidos_automaticos()` (06:30 e 17:30 BRT): roda o motor
   VENDA+ESTOQUE (`previsao_producao.sugerir_pedidos_por_venda` — o mesmo
   da tela /producao/pedidos-semana/estoque, escolha do dono) pra janela
   D+1..D+3 e materializa via `pedidos_semana.aplicar_grade` (rascunho
   'pendente' com o marcador padrão). A rodada das 17:30 é o refresh antes
   do corte: o motor lê o ESTOQUE ATUAL da loja (que o sync do Seru drena a
   cada 15min conforme o dia vende) — é por aí que a venda do próprio dia
   entra (a média histórica fecha em ontem).

   RE-SINCRONIZAÇÃO REAL: o motor recebe `ressincronizar_datas` com os dias
   que o cron pode reescrever — sem isso, dia com pedido devolve sugestão 0
   (`ja_tem`) e a quantidade congelaria na primeira criação (achado crítico
   da revisão de 13/08/2026).

   REGRAS DE RESPEITO (nunca sobrescrever gente):
   - (loja, dia) cujo pedido foi CRIADO ou MODIFICADO por um humano
     (criado_por/modificado_por_id preenchidos) é PULADO — a palavra da
     loja/admin vale mais que a do motor. Confirmar/voltar-status também
     carimbam modificado_por_id (o clique de revisão protege o pedido).
   - D+1 sob o corte das 18h NUNCA é tocado (`pedido_corte.corte_ativo`).
   - Loja/dia sem sugestão (>0) não cria pedido vazio.
   - Pedido finalizado ANTES da data (entrega antecipada de emergência) não
     protege o dia — mesmo carve-out do motor/aplicar_grade (caso Anesio
     08/07); o pedido REAL do dia ainda nasce.

   RETROALIMENTAÇÃO (decisão documentada, 13/08/2026): o pedido-máquina que
   segue 'pendente' fica FORA da média de pedidos (exclusão de rascunho
   abandonado de sempre); depois de separado/entregue ele ENTRA no
   histórico. Excluí-lo para sempre faria a média (denominador com zeros
   por data) definhar até zero em ~6 semanas e o grid além de D+3
   subestimar. O eco é limitado: `quantidade_recebida` (conferência humana
   na entrega) corrige o número, e a acurácia expõe `circularidade_pct`.

2. `enviar_plano_automatico()` (18:00 BRT, logo após o corte travar o
   pedido de amanhã): se a ordem de AMANHÃ ainda não foi enviada, aprova e
   ENVIA ao padeiro pelo cronograma (motor env `AUTO_ENVIO_MOTOR`, default
   'pedidos' — o firme dos pedidos automáticos conta em qualquer motor).
   Ordem JÁ ENVIADA (gesto humano na tela, com o motor/equilibrar DELE) é
   respeitada: o cron NÃO reenvia — reenviar com outros parâmetros mudaria
   os números do padeiro em silêncio (regra "ordem enviada nunca muda por
   caminho implícito", preservada).

Kill-switches: `AUTO_PEDIDOS=0` e `AUTO_ENVIO_PLANO=0` (default ligados —
pedido explícito do dono). Locks 7758/7759 no `seru_cron`.
"""
import logging
import os
from datetime import timedelta

from app.models import PedidoLoja
from app.utils import hoje

logger = logging.getLogger(__name__)

HORIZONTE_DIAS = 3


def _dias_protegidos(datas):
    """(loja_id, data) que o cron NÃO pode tocar: pedido criado/modificado
    por HUMANO ou já além de editável. Exceção espelhada do motor e do
    aplicar_grade: pedido ENTREGUE/RECEBIDO antes da data (entrega
    antecipada de emergência) não é "o pedido do dia" e não protege."""
    from app.constants import STATUS_PEDIDO_EDITAVEIS, STATUS_PEDIDO_ENTREGUES
    protegidos = set()
    rows = (PedidoLoja.query
            .filter(PedidoLoja.data_entrega.in_(datas),
                    PedidoLoja.status != 'cancelado')
            .all())
    for p in rows:
        if p.status in STATUS_PEDIDO_ENTREGUES:
            # datas aqui são sempre futuras (D+1..D+N): entregue = antecipada.
            continue
        humano = (p.criado_por is not None
                  or p.modificado_por_id is not None)
        if humano or p.status not in STATUS_PEDIDO_EDITAVEIS:
            protegidos.add((p.loja_id, p.data_entrega))
    return protegidos


def _seguranca_pct():
    """Env `AUTO_PEDIDOS_SEGURANCA_PCT` com piso 0 e WARNING em valor
    ilegível — int('abc') mataria o job em silêncio a cada rodada."""
    bruto = (os.environ.get('AUTO_PEDIDOS_SEGURANCA_PCT') or '0').strip()
    try:
        return max(0, int(bruto or 0))
    except ValueError:
        logger.warning('AUTO_PEDIDOS_SEGURANCA_PCT=%r ilegível — usando 0',
                       bruto)
        return 0


def gerar_pedidos_automaticos(horizonte=HORIZONTE_DIAS):
    """Materializa a sugestão do motor venda+estoque como pedidos D+1..D+N.

    Retorna o dict do `aplicar_grade` + contadores próprios
    (`dias_pulados_corte`, `dias_pulados_humano`)."""
    from datetime import date as _date

    from app.services import pedidos_semana, previsao_producao
    from app.services.pedido_corte import corte_ativo

    # Datas que a rodada PODE reescrever (fora do corte). O motor trata os
    # rascunhos automáticos dessas datas como substituíveis — sem isso a
    # sugestão volta 0 pra dia já pedido e nada re-sincroniza.
    datas_janela = [hoje() + timedelta(days=1 + i) for i in range(horizonte)]
    datas_ressinc = [d for d in datas_janela if not corte_ativo(d)]

    sug = previsao_producao.sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=1,
        seguranca_pct=_seguranca_pct(),
        ressincronizar_datas=datas_ressinc)

    datas = [_date.fromisoformat(d['data']) for d in sug.get('dias') or []]
    protegidos = _dias_protegidos(datas)

    grade = []
    pulados_corte = set()
    pulados_humano = set()
    for lj in sug.get('lojas') or []:
        loja_id = lj['loja_id']
        for i, data_ent in enumerate(datas):
            if corte_ativo(data_ent):
                pulados_corte.add(data_ent)
                continue
            if (loja_id, data_ent) in protegidos:
                pulados_humano.add((loja_id, data_ent))
                continue
            itens = []
            for prod in lj.get('produtos') or []:
                try:
                    qtd = int((prod.get('por_dia') or [])[i])
                except (IndexError, TypeError, ValueError):
                    logger.warning(
                        'auto_pedidos: por_dia ilegível (loja=%s dia=%s '
                        'item=%s) — tratado como 0', loja_id, data_ent,
                        prod.get('receita_id') or prod.get('materia_prima_id'))
                    qtd = 0
                if qtd <= 0:
                    continue
                itens.append({'receita_id': prod.get('receita_id'),
                              'materia_prima_id': prod.get('materia_prima_id'),
                              'qtd': qtd})
            if itens:
                grade.append({'loja_id': loja_id, 'data_entrega': data_ent,
                              'itens': itens})

    # user_id=None: o rascunho nasce SEM autor humano — é assim que a
    # próxima rodada sabe que pode re-sincronizar (e que um toque humano
    # o torna intocável).
    out = pedidos_semana.aplicar_grade(grade, user_id=None)
    out['dias_pulados_corte'] = sorted(d.isoformat() for d in pulados_corte)
    out['dias_pulados_humano'] = len(pulados_humano)
    logger.info('auto_pedidos: %s criados, %s atualizados, %s dia(s) sob '
                'corte, %s (loja,dia) de humano preservados',
                out.get('criados'), out.get('atualizados'),
                len(pulados_corte), len(pulados_humano))
    return out


def enviar_plano_automatico():
    """ENVIA ao padeiro a ordem de produção de AMANHÃ (18:00, logo após o
    corte) — SÓ quando ninguém enviou antes. Ordem já enviada por gesto
    humano fica como está: o humano escolheu motor/equilibrar na tela dele e
    reenviar com os defaults do cron reescreveria os números do padeiro em
    silêncio. Motor: env `AUTO_ENVIO_MOTOR` (default 'pedidos')."""
    from app.models import PlanejamentoProducao
    from app.services.producao import (
        PlanoJaEnviadoError,
        aprovar_plano_do_dia,
        enviar_plano_do_dia,
    )
    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'pedidos').strip()
    amanha = hoje() + timedelta(days=1)

    ja_enviado = (PlanejamentoProducao.query
                  .filter_by(data=amanha, origem='cronograma')
                  .filter(PlanejamentoProducao.enviado_ao_padeiro.is_(True))
                  .first())
    if ja_enviado is not None:
        n = len(ja_enviado.itens or [])
        logger.info('auto_envio: ordem de %s JÁ enviada (%d item[ns]) — '
                    'não reenviando', amanha.isoformat(), n)
        return {'data': amanha.isoformat(), 'itens': n, 'motor': motor,
                'ja_enviado': True}

    try:
        aprovar_plano_do_dia(amanha, user_id=None, motor=motor)
    except PlanoJaEnviadoError:
        # Corrida: um humano enviou entre a checagem acima e o aprovar.
        # A ordem dele vale — não reenviamos.
        logger.info('auto_envio: ordem de %s enviada por humano durante a '
                    'rodada — não reenviando', amanha.isoformat())
        return {'data': amanha.isoformat(), 'itens': 0, 'motor': motor,
                'ja_enviado': True}
    plano = enviar_plano_do_dia(amanha, user_id=None, motor=motor)
    if plano is None:
        # Dia sem nada a produzir no grid: nada a enviar (o service já
        # commitou a limpeza que fez).
        logger.info('auto_envio: %s sem itens no grid — nada enviado',
                    amanha.isoformat())
        return {'data': amanha.isoformat(), 'itens': 0, 'motor': motor,
                'vazio': True}
    n_itens = len(getattr(plano, 'itens', []) or [])
    logger.info('auto_envio: ordem de %s enviada ao padeiro (%d item[ns], '
                'motor=%s)', amanha.isoformat(), n_itens, motor)
    return {'data': amanha.isoformat(), 'itens': n_itens, 'motor': motor}
