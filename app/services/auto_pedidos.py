"""Pedidos loja→indústria AUTOMÁTICOS + envio automático da produção
(10/08/2026, decisão do dono: "quero que o sistema, com base em venda,
estoque e previsão, faça os pedidos automaticamente de 3 dias na frente" +
AskUserQuestion "Automatizar TUDO (pedido + envio)" — REVOGA a regra de
04/07/2026 "enviar ao padeiro é gesto humano" APENAS pro envio das 19:00 de
um dia SEM ordem; ordem já enviada nunca é reescrita por caminho implícito).

Duas pontas, dois jobs do cron (`seru_cron`):

1. `gerar_pedidos_automaticos()` (meio-dia junto das ordens + refreshes
   06:30 e 11:30 BRT): roda o motor VENDA+ESTOQUE
   (`previsao_producao.sugerir_pedidos_por_venda` — o mesmo da tela
   /producao/pedidos-semana/estoque, escolha do dono) pra janela da SEMANA
   (amanhã até o PRÓXIMO DOMINGO — dono 17/08/2026: "os pedidos da semana
   também devem ser lançados tudo no domingo meio dia, prevendo o que vai
   ser vendido durante a semana baseado no histórico de vendas"; era
   D+1..D+3 de 10 a 17/08) e materializa via `pedidos_semana.aplicar_grade`
   (rascunho 'pendente' com o marcador padrão). No domingo o refresh das
   11:30 abre os pedidos de seg..dom; ao meio-dia a segunda já está sob
   corte. Os refreshes diários re-sincronizam a MESMA janela
   com o ESTOQUE ATUAL da loja (que o sync do Seru drena a cada 15min
   conforme o dia vende) — é por aí que a venda do próprio dia entra (a
   média histórica fecha em ontem).

   RE-SINCRONIZAÇÃO REAL: o motor recebe `ressincronizar_datas` com os dias
   que o cron pode reescrever — sem isso, dia com pedido devolve sugestão 0
   (`ja_tem`) e a quantidade congelaria na primeira criação (achado crítico
   da revisão de 13/08/2026).

   REGRAS DE RESPEITO (nunca sobrescrever gente):
   - (loja, dia) cujo pedido foi CRIADO ou MODIFICADO por um humano
     (criado_por/modificado_por_id preenchidos) é PULADO — a palavra da
     loja/admin vale mais que a do motor. Confirmar/voltar-status também
     carimbam modificado_por_id (o clique de revisão protege o pedido).
   - D+1 sob o corte (12h) NUNCA é tocado (`pedido_corte.corte_ativo`).
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
import json
import logging
import os
from datetime import date, timedelta

from app.extensions import db
from app.models import PedidoLoja
from app.utils import agora, hoje

logger = logging.getLogger(__name__)


# Nivelamento automático da carga (dono 17/08/2026: "a previsão serve
# exatamente para isso... distribuir automaticamente para segunda a sexta
# sem eu ter que apertar botão algum de equilibrar, o sistema deve
# equilibrar sozinho"): toda ordem criada/re-sincronizada pela automação sai
# com o "equilibrar carga" LIGADO — cada receita inteira num dia, fornadas
# niveladas seg-sex, entrega nunca atrasa (mesmo motor do antigo checkbox da
# tela, que virou padrão lá também — grid e ordem na mesma régua).
EQUILIBRAR_AUTO = True
STATUS_ENVIO_PLANO_KEY = 'auto_envio_plano_status'


def _janela_da_semana(hoje_d):
    """(início, fim) da janela da SEMANA: amanhã até o PRÓXIMO DOMINGO,
    inclusive (weekday: seg=0..dom=6). Fonte ÚNICA da janela dos pedidos
    automáticos E das ordens de produção (dono 17/08/2026: pedidos e ordens
    da semana saem juntos no domingo ao meio-dia). No domingo devolve
    seg..dom (7 dias); no sábado, só o domingo."""
    inicio = hoje_d + timedelta(days=1)
    fim = inicio + timedelta(days=(6 - inicio.weekday()) % 7)
    return inicio, fim


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
    rows = (PedidoLoja.query.populate_existing()
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
    Cancela os rascunhos sem liberar a trava da rodada. Retorna o nº cancelado."""
    from app.constants import STATUS_PEDIDO_ENTREGUES
    rows = (PedidoLoja.query.populate_existing()
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
        db.session.flush()
        logger.info('auto_pedidos: %d rascunho(s) redundante(s) absorvido(s) '
                    '(dia já tem pedido humano)', n)
    return n


def _rascunhos_por_dia(datas):
    """Rascunhos automáticos abertos por (loja_id, data) — o mais antigo é
    o alvo canônico (mesma regra do rascunho_automatico_aberto)."""
    from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
    out = {}
    rows = (PedidoLoja.query.populate_existing()
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


def gerar_pedidos_automaticos():
    """Confirma a rodada apenas enquanto suas datas continuam abertas.

    O cálculo pode atravessar o meio-dia. Nesse caso, desfaz também a
    absorção de rascunhos e os cancelamentos por zero, e recalcula a rodada
    inteira com o pedido fechado preservado na simulação dos dias seguintes.
    A conferência final ocorre antes do commit, ainda sob a trava das lojas.
    """
    from app.services.pedidos_semana import PedidoCorteError

    for tentativa in range(2):
        try:
            return _gerar_pedidos_automaticos()
        except PedidoCorteError:
            db.session.rollback()
            if tentativa:
                raise
            logger.info('auto_pedidos: corte alcançado durante a rodada; '
                        'recalculando com os pedidos fechados preservados')


def _gerar_pedidos_automaticos():
    """Materializa a sugestão do motor venda+estoque como pedidos da SEMANA
    (amanhã até o próximo domingo — dono 17/08/2026; era D+1..D+3).

    Retorna o dict do `aplicar_grade` + contadores próprios
    (`dias_pulados_corte`, `dias_pulados_humano`)."""
    from datetime import date as _date

    from app.models import Loja
    from app.services import pedidos_semana, previsao_producao
    from app.services.pedido_corte import corte_ativo
    from app.services.pedido_lock import travar_pedidos_lojas

    # A mesma trava protege criação, edição, cancelamento e limpeza por zero.
    # Mantê-la até o commit da grade impede que um gesto humano entre entre
    # o snapshot de proteção e a alteração do pedido (inclusive dia vazio).
    travar_pedidos_lojas(lid for (lid,) in db.session.query(Loja.id).all())
    db.session.expire_all()

    hoje_d = hoje()
    inicio, fim = _janela_da_semana(hoje_d)
    horizonte = (fim - hoje_d).days          # nº de dias a partir de amanhã

    # Datas que a rodada PODE reescrever (fora do corte). O motor trata os
    # rascunhos automáticos dessas datas como substituíveis — sem isso a
    # sugestão volta 0 pra dia já pedido e nada re-sincroniza.
    datas_janela = [inicio + timedelta(days=i) for i in range(horizonte)]
    datas_ressinc = [d for d in datas_janela if not corte_ativo(d)]

    # Dia com pedido humano E rascunho do cron: cancela o rascunho ANTES de
    # rodar o motor (o carry da simulação fica só com o pedido que vale).
    absorvidos = _absorver_rascunhos_orfaos(datas_ressinc)

    # O motor precisa conhecer os dias que a materialização vai pular.
    # Senão ele simula uma entrega nesses dias e usa a sobra para reduzir
    # os pedidos seguintes, embora essa entrega nunca seja criada.
    protegidos = _dias_protegidos(datas_janela)
    datas_corte = set(datas_janela) - set(datas_ressinc)

    sug = previsao_producao.sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=1,
        seguranca_pct=_seguranca_pct(),
        ressincronizar_datas=datas_ressinc,
        pedidos_bloqueados=protegidos, datas_bloqueadas=datas_corte)

    datas = [_date.fromisoformat(d['data']) for d in sug.get('dias') or []]
    # Re-checa após o cálculo: um gesto humano durante a previsão continua
    # protegido também na materialização.
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
                        prod.get('item_key') or prod.get('receita_id')
                        or prod.get('materia_prima_id') or prod.get('produto_id'))
                    qtd = 0
                if qtd <= 0:
                    continue
                itens.append({'receita_id': prod.get('receita_id'),
                              'materia_prima_id': prod.get('materia_prima_id'),
                              'produto_id': prod.get('produto_id'),
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
                      else ('m', it['materia_prima_id']) if it.get('materia_prima_id')
                      else ('p', it['produto_id']))
                     for it in itens_grade}
        for it_p in ped.itens:
            fk = (('r', it_p.receita_id) if it_p.receita_id
                  else ('m', it_p.materia_prima_id)
                  if it_p.materia_prima_id else ('p', it_p.produto_id)
                  if it_p.produto_id else None)
            if fk is not None and fk not in fks_grade:
                itens_grade.append({'receita_id': it_p.receita_id,
                                    'materia_prima_id': it_p.materia_prima_id,
                                    'produto_id': it_p.produto_id,
                                    'qtd': 0})

    grade = [{'loja_id': k[0], 'data_entrega': k[1], 'itens': v}
             for k, v in grade_por_dia.items()]

    # user_id=None: o rascunho nasce SEM autor humano — é assim que a
    # próxima rodada sabe que pode re-sincronizar (e que um toque humano
    # o torna intocável). O commit do aplicar_grade também persiste os
    # cancelamentos por sugestão-zerada feitos acima.
    # Mesmo se D+1 sumiu da grade ao chegar o corte durante o cálculo,
    # conferir as datas originais protege absorções/zeros já pendentes.
    out = pedidos_semana.aplicar_grade(
        grade, user_id=None, datas_adicionais_corte=datas_ressinc)
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
    """O "🔄 atualizar produção" AUTOMÁTICO da ordem DE AMANHÃ.

    POR QUE EXISTE (17/08/2026): a ordem de segunda saiu domingo 19:00 com
    3 itens/3.274 un e o grid do próprio dia amanhecia pedindo 8 itens/6.577
    — os itens de VÉSPERA (levain, lead-1, pré-preparo) são dirigidos pela
    demanda do dia seguinte, que o cron de pedidos re-sincroniza às
    06:30/11:30 DEPOIS de a ordem já ter congelado.

    POR QUE MIRA AMANHÃ, E NUNCA HOJE (dono 20/08/2026, caso "o padeiro ia
    fazer 300 de pão francês e do nada virou 400" — a rodada das 19:05
    reescreveu a ordem que ele estava começando a executar): **"Na data de
    hoje, nunca que deveríamos ter trocado ou feito alguma mudança no que o
    padeiro está produzindo hoje. Qualquer mudança deveria ter sido feita
    ontem."** Então a ordem de um dia recebe seus últimos ajustes na
    VÉSPERA e chega intocável no dia:
      - 06:45 → ajusta a ordem de AMANHÃ com o refresh de pedidos das 06:30;
      - 12:05 → número FINAL de amanhã, logo após o corte das 12:00 (que é
        justamente quando a demanda de amanhã congela).
    O dia corrente nunca é tocado por caminho automático — `enviar_plano_do_
    dia` tem a trava definitiva (defesa em profundidade).

    SÓ toca ordem criada pelo PRÓPRIO CRON (criado_por None): ordem enviada
    por humano nunca muda por caminho implícito (regra de 04/07/2026)."""
    from app.services.previsao_producao import (
        _ANT_INSUMO_MAX,
        cronograma_producao,
    )

    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'vendas').strip()
    # Insumo e pai precisam sair do mesmo cronograma. Só ordens já enviadas
    # pelo motor são atualizadas; dias ausentes e ordens humanas são preservados.
    dias = [hoje() + timedelta(days=k) for k in range(1, _ANT_INSUMO_MAX + 2)]
    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6,
                                inicio_offset_dias=0,
                                equilibrar=EQUILIBRAR_AUTO, motor=motor)
    return _executar_ordens(dias, crono, motor, 7, somente_enviadas=True)


def _receitas_do_dia(crono, data_iso):
    return {
        rec['receita_id'] for rec in crono['receitas']
        if not rec.get('retorno') and any(
            c['data'] == data_iso and
            (c['qtd'] > 0 or c.get('qtd_solicitada', 0) > 0)
            for c in rec['por_dia'])
    }


def _subs_envio(rid, receitas, linhas, data_iso):
    """Inclui o BOM congelado: editar a ficha não apaga a massa da ordem."""
    from app.services.previsao_producao import _subs_de

    subs = {sid: ratio for sid, ratio in _subs_de(rid, receitas) if ratio > 0}
    celulas = linhas.get(rid, {}).get('por_dia', [])
    exatas = [c for c in celulas if c['data'] == data_iso]
    for celula in exatas or celulas:
        for sub in (celula.get('batelada_padrao') or {}).get('subs', []):
            if sub['id'] in receitas and sub['quantidade'] > 0:
                subs.setdefault(sub['id'], 0.0)
    retorno = {r.retorno_receita_id for r in receitas.values() if r.retorno_receita_id}
    return {sid: ratio for sid, ratio in subs.items() if sid not in retorno}


def _data_preparo_envio(sid, consumo, receitas):
    from app.services.previsao_producao import producao_permitida_no_dia

    dia = consumo - timedelta(days=int(receitas[sid].dias_producao or 0))
    for _ in range(14):
        if producao_permitida_no_dia(receitas[sid], dia):
            return dia
        dia -= timedelta(days=1)
    return dia


def _necessidade_preparo_envio(sid, ate, receitas, linhas):
    """Demanda acumulada do mesmo cronograma, na unidade canônica do insumo."""
    from app.services.cronograma_bateladas import consumo_insumo

    linha = linhas.get(sid, {})
    necessidades = linha.get('_necessidade_insumo_por_dia')
    if isinstance(necessidades, list) and len(necessidades) == len(linha['por_dia']):
        return sum(float(q or 0) for c, q in zip(linha['por_dia'], necessidades)
                   if c['data'] <= ate)
    # Cronogramas sem vetor explícito: exigir todo o consumo calculado pelo
    # BOM, inclusive snapshots, em vez de presumir cobertura por estoque.
    total = 0.0
    for rid, rr in linhas.items():
        for c in rr['por_dia']:
            subs = _subs_envio(rid, receitas, linhas, c['data'])
            if sid in subs and _data_preparo_envio(
                    sid, date.fromisoformat(c['data']), receitas).isoformat() <= ate:
                total += consumo_insumo(c, sid, subs[sid])
    return total


def _preparo_substituto_valido(prova, sid, data_iso, necessario_em, necessario):
    """Prova de envio não é saldo: só vale na janela e nas fontes originais."""
    from app.models import PlanejamentoItem
    from app.services.viennoiserie import quantidade_em_bolas

    if not prova or (
            prova.get('janela_inicio') != hoje().isoformat()
            or prova.get('consumo_ate', '') < data_iso
            or prova['data'] > necessario_em
            or prova['quantidade'] < necessario
            or not prova.get('fontes')):
        return False
    for fonte in prova['fontes']:
        item = db.session.get(PlanejamentoItem, fonte['item_id'])
        if (item is None or item.receita_id != sid
                or item.planejamento_id != fonte['plano_id']
                or item.planejamento.data.isoformat() != fonte['data']
                or item.planejamento.enviado_ao_padeiro is False
                or item.dispensada_em is not None
                or item.falta_encerrada_em is not None
                or quantidade_em_bolas(item, max(
                    0, (item.qtd_alvo or 0) - (item.produzido_qtd or 0)))
                < fonte['quantidade']):
            return False
    return True


def _dias_insumos_bloqueados(receita_ids, falhas_por_dia, receitas,
                             data_iso, crono, novos_preparos):
    """Falhas permanecem entre rodadas até envio correto ou novo preparo.

    Só um novo envio positivo desta rodada, suficiente para a demanda
    acumulada e com antecedência, comprova a substituição de preparo antigo.
    Um plano preexistente ou a passagem do tempo não constitui essa prova.
    """
    linhas = {r['receita_id']: r for r in crono['receitas']}
    dependencias = set()

    def visitar(rid, dia, caminho=()):
        if rid in caminho:
            return
        for sid in _subs_envio(rid, receitas, linhas, dia.isoformat()):
            anterior = _data_preparo_envio(sid, dia, receitas)
            dependencias.add((sid, anterior.isoformat()))
            visitar(sid, anterior, (*caminho, rid))

    for rid in receita_ids:
        visitar(rid, date.fromisoformat(data_iso))
    bloqueadas = set()
    for sid, necessario_em in dependencias:
        for dia_falho, falha in falhas_por_dia.items():
            if dia_falho >= data_iso or sid not in falha.get('receita_ids', []):
                continue
            necessario = _necessidade_preparo_envio(
                sid, necessario_em, receitas, linhas)
            substituicao = falha.get('preparos_substituidos', {}).get(str(sid))
            if _preparo_substituto_valido(
                    substituicao, sid, data_iso, necessario_em, necessario):
                continue
            novos = [p for p in novos_preparos.get(sid, [])
                     if dia_falho < p['data'] <= necessario_em]
            quantidade = sum(p['quantidade'] for p in novos)
            if novos and necessario > 0 and quantidade >= necessario:
                falha.setdefault('preparos_substituidos', {})[str(sid)] = {
                    'data': max(p['data'] for p in novos),
                    'quantidade': quantidade, 'necessidade': necessario,
                    'ordens': [p['plano_id'] for p in novos],
                    'fontes': novos, 'janela_inicio': hoje().isoformat(),
                    'consumo_ate': data_iso}
            else:
                bloqueadas.add(dia_falho)
    return sorted(bloqueadas)


def enviar_ordens_da_semana(*, incluir_hoje=False, somente_pendentes=False):
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
    (mesmo princípio do 🔄 das 06:45/12:05, que atualiza dias futuros).
    Motor: env `AUTO_ENVIO_MOTOR` (default 'vendas').

    Uma ficha inválida desfaz o DIA inteiro, sem pular receita ou perder
    a validação de consumo. Dias independentes continuam; dependentes de
    insumos daquele dia ficam aguardando a correção. Resultado persistido
    em AppConfig para a tela informar o motivo, além do log do cron."""
    from app.services.previsao_producao import cronograma_producao
    motor = (os.environ.get('AUTO_ENVIO_MOTOR') or 'vendas').strip()
    hoje_d = hoje()
    inicio, fim = _janela_da_semana(hoje_d)
    if incluir_hoje:
        inicio = hoje_d
    # O grid precisa CONTER o fim (coluna fora do horizonte = envio no-op).
    horizonte = min(14, (fim - hoje_d).days + 1)
    # Uma única previsão para a rodada: pai e insumo são escritos a partir
    # da mesma distribuição, inclusive ao conferir dependências após falha.
    crono = cronograma_producao(horizonte_dias=horizonte,
                                janela_semanas=6, inicio_offset_dias=0,
                                equilibrar=EQUILIBRAR_AUTO, motor=motor)
    dias = [inicio + timedelta(days=n) for n in range((fim - inicio).days + 1)]
    return _executar_ordens(dias, crono, motor, horizonte,
                            incluir_hoje=incluir_hoje,
                            somente_pendentes=somente_pendentes)


def _executar_ordens(dias, crono, motor, horizonte, *, incluir_hoje=False,
                     somente_pendentes=False, somente_enviadas=False):
    """Envio e atualização usam as mesmas transações e provas de preparo.

    Cada dia inválido é desfeito e fica visível no status. Dias independentes
    continuam; dependentes aguardam o preparo, inclusive na rodada das 12:05.
    """
    from app.models import AppConfig, PlanejamentoProducao, Receita
    from app.services.auto_envio_status import ler_status
    from app.services.producao import (
        PlanoJaEnviadoError,
        aprovar_plano_do_dia,
        enviar_plano_do_dia,
    )
    hoje_d = hoje()
    inicio, fim = min(dias), max(dias)
    receitas = {r.id: r for r in Receita.query.all()}
    planos = {p.data: p for p in (
        PlanejamentoProducao.query
        .filter_by(origem='cronograma')
        .filter(PlanejamentoProducao.data >= inicio,
                PlanejamentoProducao.data <= fim))}

    out = {'tentativa_em': agora().isoformat(), 'de': inicio.isoformat(),
           'ate': fim.isoformat(), 'motor': motor, 'enviadas': [],
           'resincronizadas': [], 'puladas': [], 'vazias': [], 'falhas': [],
           'atualizadas': [], 'sem_ordem': [], 'ordem_humana': []}
    # Repetir a rodada ou virar o dia não corrige um envio que falhou.
    # Preserve também preparos passados: a UI oculta datas antigas, mas seus
    # dependentes não podem assumir que a massa foi preparada sem resolução.
    falhas_por_dia = {}
    anteriores = ler_status().get('falhas')
    for falha in anteriores if isinstance(anteriores, list) else []:
        if not isinstance(falha, dict) or not isinstance(falha.get('erro'), str):
            continue
        try:
            iso_falha = date.fromisoformat(falha.get('data')).isoformat()
        except (TypeError, ValueError):
            continue
        falhas_por_dia[iso_falha] = falha

    novos_preparos = {}

    dia = inicio
    while dia <= fim:
        iso = dia.isoformat()
        plano = planos.get(dia)
        enviado = plano is not None and plano.enviado_ao_padeiro is not False
        if somente_enviadas and (plano is None or plano.enviado_ao_padeiro is not True):
            out['sem_ordem'].append(iso)
            dia += timedelta(days=1)
            continue
        # Recuperação explícita de buracos: hoje só pode nascer se estiver
        # ausente. Nenhum envio anterior nem rascunho humano é reinterpretado.
        if plano is not None and (
                (incluir_hoje and dia == hoje_d) or
                (somente_pendentes and (enviado or plano.criado_por is not None))):
            out['puladas'].append(iso)
            dia += timedelta(days=1)
            continue
        if enviado and plano.criado_por is not None:
            out['puladas'].append(iso)     # ordem HUMANA: nunca tocada
            out['ordem_humana'].append(iso)
            dia += timedelta(days=1)
            continue
        receita_ids = _receitas_do_dia(crono, iso)
        bloqueada_por = _dias_insumos_bloqueados(
            receita_ids, falhas_por_dia, receitas, iso, crono, novos_preparos)
        if bloqueada_por:
            falhas_por_dia[iso] = {
                'data': iso, 'bloqueada_por': bloqueada_por,
                'receita_ids': sorted(receita_ids),
                'erro': 'Aguardando corrigir o envio dos preparos de '
                        + ', '.join(bloqueada_por) + '.'}
            dia += timedelta(days=1)
            continue
        try:
            if not enviado:
                plano = aprovar_plano_do_dia(
                    dia, user_id=None, horizonte_dias=horizonte, motor=motor,
                    equilibrar=EQUILIBRAR_AUTO, crono=crono, commit=False)
            plano = enviar_plano_do_dia(
                dia, user_id=None, horizonte_dias=horizonte, motor=motor,
                equilibrar=EQUILIBRAR_AUTO, crono=crono)
        except PlanoJaEnviadoError:
            # Corrida: um humano enviou entre o snapshot e o aprovar — a
            # ordem dele vale.
            out['puladas'].append(iso)
            dia += timedelta(days=1)
            continue
        except ValueError as exc:
            # Nada deste dia pode vazar para o commit do próximo ou para o
            # status: reserva, itens e aprovação pertencem à mesma transação.
            db.session.rollback()
            logger.exception('ordens_semana: envio de %s recusado', iso)
            falhas_por_dia[iso] = {'data': iso, 'erro': str(exc),
                                   'receita_ids': sorted(receita_ids)}
            dia += timedelta(days=1)
            continue
        if plano is None:
            out['vazias'].append(iso)      # grid sem nada a produzir no dia
        else:
            out['resincronizadas' if enviado else 'enviadas'].append(iso)
            if somente_enviadas:
                out['atualizadas'].append({'data': iso, 'itens': len(plano.itens or [])})
            if not enviado and falhas_por_dia:
                from app.services.viennoiserie import quantidade_em_bolas
                for item in plano.itens:
                    quantidade = quantidade_em_bolas(
                        item, max(0, (item.qtd_alvo or 0) - (item.produzido_qtd or 0)))
                    if quantidade > 0:
                        novos_preparos.setdefault(item.receita_id, []).append({
                            'data': iso, 'quantidade': quantidade, 'plano_id': plano.id,
                            'item_id': item.id})
        falhas_por_dia.pop(iso, None)
        dia += timedelta(days=1)
    out['falhas'] = [falhas_por_dia[d] for d in sorted(falhas_por_dia)]
    AppConfig.set(STATUS_ENVIO_PLANO_KEY, json.dumps(out, ensure_ascii=False))
    db.session.commit()
    logger.info('ordens_semana: %s..%s enviadas=%s resinc=%s puladas=%s '
                'vazias=%s falhas=%s (motor=%s)', out['de'], out['ate'],
                out['enviadas'], out['resincronizadas'], out['puladas'],
                out['vazias'], out['falhas'], motor)
    return out
