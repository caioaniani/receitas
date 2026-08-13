"""Pedidos loja→indústria AUTOMÁTICOS + envio automático da produção
(10/08/2026, decisão do dono: "quero que o sistema, com base em venda,
estoque e previsão, faça os pedidos automaticamente de 3 dias na frente" +
AskUserQuestion "Automatizar TUDO (pedido + envio)" — REVOGA a regra de
04/07/2026 "enviar ao padeiro é gesto humano").

Duas pontas, dois jobs do cron (`seru_cron`):

1. `gerar_pedidos_automaticos()` (06:30 e 17:30 BRT): roda o motor
   VENDA+ESTOQUE (`previsao_producao.sugerir_pedidos_por_venda` — o mesmo
   da tela /producao/pedidos-semana/estoque, escolha do dono) pra janela
   D+1..D+3 e materializa via `pedidos_semana.aplicar_grade` (rascunho
   'pendente' com o marcador padrão — o motor de MÉDIA já exclui esses
   rascunhos do histórico, sem retroalimentação). A rodada das 17:30 é o
   refresh com a venda do próprio dia, antes do corte.

   REGRAS DE RESPEITO (nunca sobrescrever gente):
   - (loja, dia) cujo pedido foi CRIADO ou MODIFICADO por um humano
     (criado_por/modificado_por_id preenchidos) é PULADO — a palavra da
     loja/admin vale mais que a do motor.
   - D+1 sob o corte das 18h NUNCA é tocado (`pedido_corte.corte_ativo`).
   - Loja/dia sem sugestão (>0) não cria pedido vazio.

2. `enviar_plano_automatico()` (18:00 BRT, logo após o corte travar o
   pedido de amanhã): aprova e ENVIA ao padeiro a ordem de produção de
   AMANHÃ pelo cronograma (motor env `AUTO_ENVIO_MOTOR`, default
   'pedidos'). Re-pressável por desenho (`enviar_plano_do_dia` re-sincroniza
   a ordem — os pedidos acabaram de congelar, o balanço está estável).

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
    por HUMANO (ou já além de editável — aplicar_grade também pula esses,
    mas aqui evitamos até a tentativa)."""
    from app.constants import STATUS_PEDIDO_EDITAVEIS
    protegidos = set()
    rows = (PedidoLoja.query
            .filter(PedidoLoja.data_entrega.in_(datas),
                    PedidoLoja.status != 'cancelado')
            .all())
    for p in rows:
        humano = (p.criado_por is not None
                  or p.modificado_por_id is not None)
        if humano or p.status not in STATUS_PEDIDO_EDITAVEIS:
            protegidos.add((p.loja_id, p.data_entrega))
    return protegidos


def gerar_pedidos_automaticos(horizonte=HORIZONTE_DIAS):
    """Materializa a sugestão do motor venda+estoque como pedidos D+1..D+N.

    Retorna o dict do `aplicar_grade` + contadores próprios
    (`dias_pulados_corte`, `dias_pulados_humano`)."""
    from datetime import date as _date

    from app.services import pedidos_semana, previsao_producao
    from app.services.pedido_corte import corte_ativo

    seguranca = int(os.environ.get('AUTO_PEDIDOS_SEGURANCA_PCT', '0') or 0)
    sug = previsao_producao.sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=1,
        seguranca_pct=seguranca)

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
    """Aprova + ENVIA ao padeiro a ordem de produção de AMANHÃ (18:00, logo
    após o corte). Motor: env `AUTO_ENVIO_MOTOR` (default 'pedidos' — o
    default histórico da tela; o firme dos pedidos automáticos conta em
    qualquer motor)."""
    from app.services.producao import (
        PlanoJaEnviadoError,
        aprovar_plano_do_dia,
        enviar_plano_do_dia,
    )
    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'pedidos').strip()
    amanha = hoje() + timedelta(days=1)
    try:
        aprovar_plano_do_dia(amanha, user_id=None, motor=motor)
    except PlanoJaEnviadoError:
        # Já enviado (por gesto humano ou rodada anterior): o enviar abaixo
        # re-sincroniza a ordem com o grid — comportamento re-pressável.
        pass
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
