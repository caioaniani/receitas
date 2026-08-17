"""Pedidos loja→indústria AUTOMÁTICOS + envio automático da produção
(10/08/2026, decisão do dono: "quero que o sistema, com base em venda,
estoque e previsão, faça os pedidos automaticamente de 3 dias na frente" +
AskUserQuestion "Automatizar TUDO (pedido + envio)" — REVOGA a regra de
04/07/2026 "enviar ao padeiro é gesto humano" APENAS pro envio das 19:00 de
um dia SEM ordem; ordem já enviada nunca é reescrita por caminho implícito).

Duas pontas, dois jobs do cron (`seru_cron`):

1. `gerar_pedidos_automaticos()` (06:30 e 18:30 BRT): roda o motor
   VENDA+ESTOQUE (`previsao_producao.sugerir_pedidos_por_venda` — o mesmo
   da tela /producao/pedidos-semana/estoque, escolha do dono) pra janela
   D+1..D+3 e materializa via `pedidos_semana.aplicar_grade` (rascunho
   'pendente' com o marcador padrão). A rodada das 18:30 é o refresh antes
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
   - D+1 sob o corte (19h) NUNCA é tocado (`pedido_corte.corte_ativo`).
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

2. `enviar_ordens_da_semana()` (dono 17/08/2026: "a ordem de produção da
   semana soltando ela no domingo, meio-dia, até o próximo domingo" —
   SUBSTITUI o envio diário das 19:00 de 10/08/2026): envia ao padeiro a
   ordem de cada dia de AMANHÃ até o PRÓXIMO DOMINGO que ainda não tem
   ordem enviada. No domingo ao meio-dia isso abre a semana inteira
   (seg..dom); nos demais dias o job mantém a semana FIEL AO GRID —
   re-preenche buraco (dia excluído, deploy que engoliu o disparo) e
   RE-SINCRONIZA as ordens do próprio cron com o grid do dia. Motor: env
   `AUTO_ENVIO_MOTOR`, default 'vendas' (decisão do dono 17/08/2026:
   "baseado no histórico de vendas e estoque"; o firme dos pedidos
   automáticos conta em qualquer motor). Ordem enviada por HUMANO é
   intocável — "ordem enviada nunca muda por caminho implícito" vale pra
   gesto humano; o 🔄 (item 3) segue sendo a precisão do PRÓPRIO dia.
   Pra tirar um dia da produção, ZERE as células no grid (envio de dia
   vazio limpa a ordem); excluir a ordem faz o meio-dia seguinte
   reenviá-la do grid.

Kill-switches: `AUTO_PEDIDOS=0` e `AUTO_ENVIO_PLANO=0` (default ligados —
pedido explícito do dono). Locks 7758/7759 no `seru_cron`.
"""
import logging
import os
from datetime import timedelta

from app.extensions import db
from app.models import PedidoLoja
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

HORIZONTE_DIAS = 3


def _e_rascunho_auto(p):
    from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
    return (p.status == 'pendente' and p.criado_por is None
            and p.modificado_por_id is None
            and (p.observacao or '').startswith(MARCADOR_RASCUNHO_AUTO))


def _dias_protegidos(datas):
    """(loja_id, data) que o cron NÃO pode tocar: pedido criado/modificado
    por HUMANO ou já além de editável. Exceção espelhada do motor e do
    aplicar_grade: pedido ENTREGUE/RECEBIDO antes da data (entrega
    antecipada de emergência) não é "o pedido do dia" e não protege.

    CANCELADO por humano (modificado_por_id preenchido — as rotas de
    cancelar carimbam) TAMBÉM protege: "cancelei o pedido de sexta" é a
    palavra da loja; sem isso o cron ressuscitava o pedido na rodada
    seguinte (achado da revisão rodada 2). Cancelado sem carimbo
    (histórico antigo, absorção pelo próprio cron) não protege."""
    from app.constants import STATUS_PEDIDO_EDITAVEIS, STATUS_PEDIDO_ENTREGUES
    protegidos = set()
    rows = (PedidoLoja.query
            .filter(PedidoLoja.data_entrega.in_(datas))
            .all())
    for p in rows:
        if p.status == 'cancelado':
            if p.modificado_por_id is not None:
                protegidos.add((p.loja_id, p.data_entrega))
            continue
        if p.status in STATUS_PEDIDO_ENTREGUES:
            # datas aqui são sempre futuras (D+1..D+N): entregue = antecipada.
            continue
        humano = (p.criado_por is not None
                  or p.modificado_por_id is not None)
        if humano or p.status not in STATUS_PEDIDO_EDITAVEIS:
            protegidos.add((p.loja_id, p.data_entrega))
    return protegidos


def _absorver_rascunhos_orfaos(datas):
    """(loja, dia) com pedido de HUMANO e TAMBÉM rascunho do cron — estado
    de colisão (edição de data movendo um pedido pra cima do rascunho,
    legado pré-fix, corrida): o rascunho é redundância de máquina e a dobra
    não pode esperar o próximo gesto humano (achado da revisão rodada 2).
    Cancela os rascunhos e commita. Retorna o nº cancelado."""
    from app.constants import STATUS_PEDIDO_ENTREGUES
    rows = (PedidoLoja.query
            .filter(PedidoLoja.data_entrega.in_(datas),
                    PedidoLoja.status != 'cancelado')
            .all())
    por_dia = {}
    for p in rows:
        por_dia.setdefault((p.loja_id, p.data_entrega), []).append(p)
    n = 0
    for _, ps in por_dia.items():
        rascs = [p for p in ps if _e_rascunho_auto(p)]
        outros = [p for p in ps
                  if not _e_rascunho_auto(p)
                  and p.status not in STATUS_PEDIDO_ENTREGUES]
        if rascs and outros:
            for p_r in rascs:
                p_r.status = 'cancelado'
                p_r.modificado_em = agora()
                n += 1
    if n:
        db.session.commit()
        logger.info('auto_pedidos: %d rascunho(s) redundante(s) absorvido(s) '
                    '(dia já tem pedido humano)', n)
    return n


def _rascunhos_por_dia(datas):
    """Rascunhos automáticos abertos por (loja_id, data) — o mais antigo é
    o alvo canônico (mesma regra do rascunho_automatico_aberto)."""
    from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
    out = {}
    rows = (PedidoLoja.query
            .filter(PedidoLoja.data_entrega.in_(datas),
                    PedidoLoja.status == 'pendente',
                    PedidoLoja.criado_por.is_(None),
                    PedidoLoja.modificado_por_id.is_(None),
                    PedidoLoja.observacao.like(MARCADOR_RASCUNHO_AUTO + '%'))
            .order_by(PedidoLoja.id)
            .all())
    for p in rows:
        out.setdefault((p.loja_id, p.data_entrega), p)
    return out


def _seguranca_pct():
    """Env `AUTO_PEDIDOS_SEGURANCA_PCT` com piso 0 e WARNING em valor
    ilegível ou negativo — int('abc') mataria o job em silêncio a cada
    rodada, e -10 calado esconderia config errada."""
    bruto = (os.environ.get('AUTO_PEDIDOS_SEGURANCA_PCT') or '0').strip()
    try:
        val = int(bruto or 0)
    except ValueError:
        logger.warning('AUTO_PEDIDOS_SEGURANCA_PCT=%r ilegível — usando 0',
                       bruto)
        return 0
    if val < 0:
        logger.warning('AUTO_PEDIDOS_SEGURANCA_PCT=%r negativo — usando 0',
                       bruto)
        return 0
    return val


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

    # Dia com pedido humano E rascunho do cron: cancela o rascunho ANTES de
    # rodar o motor (o carry da simulação fica só com o pedido que vale).
    absorvidos = _absorver_rascunhos_orfaos(datas_ressinc)

    sug = previsao_producao.sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=1,
        seguranca_pct=_seguranca_pct(),
        ressincronizar_datas=datas_ressinc)

    datas = [_date.fromisoformat(d['data']) for d in sug.get('dias') or []]
    protegidos = _dias_protegidos(datas)

    grade_por_dia = {}
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
                grade_por_dia[(loja_id, data_ent)] = itens

    # SINCRONIZAÇÃO ATÉ ZERO (achado da revisão rodada 2): sugestão que CAI
    # a 0 também tem que chegar no rascunho — item que sumiu da sugestão vai
    # na grade com qtd 0 explícita (o _sincronizar_itens remove), e dia cuja
    # sugestão zerou POR INTEIRO cancela o rascunho (estoque subiu e cobre —
    # deixar os 50 velhos congelarem no corte viraria produção desnecessária).
    rascunhos = _rascunhos_por_dia(datas_ressinc)
    cancelados_zero = 0
    for (loja_id, data_ent), ped in rascunhos.items():
        if (loja_id, data_ent) in protegidos or corte_ativo(data_ent):
            continue
        itens_grade = grade_por_dia.get((loja_id, data_ent))
        if itens_grade is None:
            ped.status = 'cancelado'
            ped.modificado_em = agora()
            cancelados_zero += 1
            continue
        fks_grade = {(('r', it['receita_id']) if it.get('receita_id')
                      else ('m', it['materia_prima_id']))
                     for it in itens_grade}
        for it_p in ped.itens:
            fk = (('r', it_p.receita_id) if it_p.receita_id
                  else ('m', it_p.materia_prima_id)
                  if it_p.materia_prima_id else None)
            if fk is not None and fk not in fks_grade:
                itens_grade.append({'receita_id': it_p.receita_id,
                                    'materia_prima_id': it_p.materia_prima_id,
                                    'qtd': 0})

    grade = [{'loja_id': k[0], 'data_entrega': k[1], 'itens': v}
             for k, v in grade_por_dia.items()]

    # user_id=None: o rascunho nasce SEM autor humano — é assim que a
    # próxima rodada sabe que pode re-sincronizar (e que um toque humano
    # o torna intocável). O commit do aplicar_grade também persiste os
    # cancelamentos por sugestão-zerada feitos acima.
    out = pedidos_semana.aplicar_grade(grade, user_id=None)
    out['dias_pulados_corte'] = sorted(d.isoformat() for d in pulados_corte)
    out['dias_pulados_humano'] = len(pulados_humano)
    out['rascunhos_absorvidos'] = absorvidos
    out['rascunhos_cancelados_zero'] = cancelados_zero
    logger.info('auto_pedidos: %s criados, %s atualizados, %s dia(s) sob '
                'corte, %s (loja,dia) de humano preservados, %s absorvido(s), '
                '%s cancelado(s) por sugestão zerada',
                out.get('criados'), out.get('atualizados'),
                len(pulados_corte), len(pulados_humano), absorvidos,
                cancelados_zero)
    return out


def atualizar_plano_automatico():
    """O "🔄 atualizar produção" AUTOMÁTICO da ordem DE HOJE (17/08/2026,
    caso real do 1º fim de semana: a ordem de segunda saiu domingo 19:00 com
    3 itens/3.274 un e o grid do próprio dia amanhecia pedindo 8 itens/6.577
    — os itens de VÉSPERA do dia, levain/lead-1/pré-preparo, são dirigidos
    pela demanda de AMANHÃ, que o cron de pedidos re-sincroniza às
    06:30/18:30 DEPOIS de a ordem já ter congelado; antes da automação era
    o dono que dava o 🔄 na mão).

    Roda às 06:45 (pós-refresh de pedidos da manhã) e às 19:05 (pós-corte —
    a demanda de amanhã acabou de congelar; número final pra madrugada).
    SÓ toca ordem criada pelo PRÓPRIO CRON (criado_por None): ordem enviada
    por humano nunca muda por caminho implícito (regra de 04/07/2026
    preservada)."""
    from app.models import PlanejamentoProducao
    from app.services.producao import enviar_plano_do_dia

    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'vendas').strip()
    hoje_d = hoje()
    plano = (PlanejamentoProducao.query
             .filter_by(data=hoje_d, origem='cronograma')
             .filter(PlanejamentoProducao.enviado_ao_padeiro.is_(True))
             .first())
    if plano is None:
        logger.info('auto_atualiza: %s sem ordem enviada — nada a atualizar',
                    hoje_d.isoformat())
        return {'data': hoje_d.isoformat(), 'sem_ordem': True}
    if plano.criado_por is not None:
        logger.info('auto_atualiza: ordem de %s foi enviada por humano — '
                    'intocada', hoje_d.isoformat())
        return {'data': hoje_d.isoformat(), 'ordem_humana': True}
    plano2 = enviar_plano_do_dia(hoje_d, user_id=None, motor=motor)
    n = len(getattr(plano2, 'itens', []) or []) if plano2 is not None else 0
    logger.info('auto_atualiza: ordem de %s re-sincronizada com o grid '
                '(%d item[ns], motor=%s)', hoje_d.isoformat(), n, motor)
    return {'data': hoje_d.isoformat(), 'itens': n, 'atualizada': True,
            'motor': motor}


def enviar_ordens_da_semana():
    """Solta a ORDEM DE PRODUÇÃO DA SEMANA (dono 17/08/2026: "a ordem de
    produção da semana soltando ela no domingo, meio-dia, até o próximo
    domingo"): envia ao padeiro a ordem de cada dia de AMANHÃ até o
    PRÓXIMO DOMINGO (inclusive) que ainda não tem ordem enviada.

    No domingo ao meio-dia isso abre a semana inteira (seg..dom). Nos
    demais dias o job mantém a semana FIEL AO GRID: dia sem ordem é
    enviado (rede contra dia excluído/deploy que engoliu o disparo — o
    APScheduler não persiste misfire) e ordem do PRÓPRIO CRON é
    RE-SINCRONIZADA com o grid do dia (o grid é a verdade; enviar é
    re-pressável e preserva o já produzido). Ordem enviada por HUMANO é
    intocável — "ordem enviada nunca muda por caminho implícito" vale pra
    gesto humano; ordem de cron sempre foi mantida por refresh automático
    (mesmo princípio do 🔄 das 06:45/19:05, que segue sendo a precisão do
    PRÓPRIO dia). Motor: env `AUTO_ENVIO_MOTOR` (default 'vendas')."""
    from app.models import PlanejamentoProducao
    from app.services.producao import (
        PlanoJaEnviadoError,
        aprovar_plano_do_dia,
        enviar_plano_do_dia,
    )
    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'vendas').strip()
    hoje_d = hoje()
    inicio = hoje_d + timedelta(days=1)
    # Próximo domingo INCLUSIVE a partir de amanhã (weekday: seg=0..dom=6).
    fim = inicio + timedelta(days=(6 - inicio.weekday()) % 7)
    # O grid precisa CONTER o fim (coluna fora do horizonte = envio no-op).
    horizonte = min(14, (fim - hoje_d).days + 1)

    planos = {p.data: p for p in (
        PlanejamentoProducao.query
        .filter_by(origem='cronograma')
        .filter(PlanejamentoProducao.data >= inicio,
                PlanejamentoProducao.data <= fim))}

    out = {'de': inicio.isoformat(), 'ate': fim.isoformat(), 'motor': motor,
           'enviadas': [], 'resincronizadas': [], 'puladas': [], 'vazias': []}
    dia = inicio
    while dia <= fim:
        iso = dia.isoformat()
        plano = planos.get(dia)
        enviado = plano is not None and plano.enviado_ao_padeiro is not False
        if enviado and plano.criado_por is not None:
            out['puladas'].append(iso)     # ordem HUMANA: nunca tocada
            dia += timedelta(days=1)
            continue
        if enviado:
            # Ordem do PRÓPRIO CRON: re-sincroniza com o grid de agora.
            r = enviar_plano_do_dia(dia, user_id=None,
                                    horizonte_dias=horizonte, motor=motor)
            out['resincronizadas' if r is not None else 'vazias'].append(iso)
            dia += timedelta(days=1)
            continue
        try:
            aprovar_plano_do_dia(dia, user_id=None, horizonte_dias=horizonte,
                                 motor=motor)
        except PlanoJaEnviadoError:
            # Corrida: um humano enviou entre o snapshot e o aprovar — a
            # ordem dele vale.
            out['puladas'].append(iso)
            dia += timedelta(days=1)
            continue
        plano = enviar_plano_do_dia(dia, user_id=None,
                                    horizonte_dias=horizonte, motor=motor)
        if plano is None:
            out['vazias'].append(iso)      # grid sem nada a produzir no dia
        else:
            out['enviadas'].append(iso)
        dia += timedelta(days=1)
    logger.info('ordens_semana: %s..%s enviadas=%s resinc=%s puladas=%s '
                'vazias=%s (motor=%s)', out['de'], out['ate'],
                out['enviadas'], out['resincronizadas'], out['puladas'],
                out['vazias'], motor)
    return out
