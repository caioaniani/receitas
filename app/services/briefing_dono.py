"""Briefing diário do dono — cockpit push (16/07/2026).

Pedido do dono ("não estou conseguindo pilotar o avião"): toda manhã UMA
mensagem no WhatsApp com o que aconteceu (vendas de ontem) e o que precisa
DELE hoje (decisões paradas), em vez de 146 telas pra ir puxar. Fonte única:
as mesmas funções alimentam o bloco "Precisa de você hoje" da home do admin
(`main.index`) e a página `/admin/briefing` (preview + envio manual).

Regras de desenho:
- Só LEITURA + contagens baratas (EXISTS/COUNT/SUM) — o bloco da home roda a
  cada carga da página do admin; nada de balanço/explosão aqui.
- O envio via cron usa o mesmo `zapi.enviar_texto` dos vigias (1 msg/dia não
  encosta no teto anti-spam de 30/h).
- URLs nos itens são caminhos literais das telas (estáveis, viram link no
  bloco da home; no WhatsApp vão como texto).
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import func

from app.extensions import db
from app.utils import hoje

logger = logging.getLogger(__name__)

_DOW_PT = ('seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom')

# Quantas ocorrências passadas do MESMO dia-da-semana entram na média de
# comparação das vendas de ontem (mesma ordem de grandeza da janela de 6
# semanas dos motores de previsão).
_SEMANAS_MEDIA = 6


def _fmt_brl(v):
    """R$ 1.234 (sem centavos — briefing é ordem de grandeza, não contábil)."""
    s = f'{float(v or 0):,.0f}'.replace(',', '.')
    return f'R$ {s}'


# ── Pendências que exigem decisão ────────────────────────────────────────────

def pendencias(incluir_owner=True):
    """Lista de itens acionáveis AGORA, cada um {chave, rotulo, qtd, url}.

    `incluir_owner=False` esconde os itens cujas telas são owner-only
    (espelha o gate do dashboard) — usado no bloco da home quando quem
    olha é admin comum.
    """
    from app.models import (
        ContaPagar,
        EstoqueLoja,
        EstoqueProducao,
        Orcamento,
        PlanejamentoItem,
        PlanejamentoProducao,
    )
    hoje_d = hoje()
    itens = []

    # 1. Ordem de produção de HOJE. A máquina de estado real é o booleano
    # enviado_ao_padeiro (status='aprovado' é setado na criação e não vale
    # como sinal) — mesma convenção do painel do cronograma.
    tem_rascunho_hoje = db.session.query(PlanejamentoProducao.id).filter(
        PlanejamentoProducao.origem == 'cronograma',
        PlanejamentoProducao.data == hoje_d,
        PlanejamentoProducao.enviado_ao_padeiro.is_(False),
    ).first() is not None
    tem_enviada_hoje = db.session.query(PlanejamentoProducao.id).filter(
        PlanejamentoProducao.origem == 'cronograma',
        PlanejamentoProducao.data == hoje_d,
        PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
    ).first() is not None
    if tem_rascunho_hoje:
        itens.append({'chave': 'ordem_rascunho',
                      'rotulo': 'Ordem de hoje aprovada mas NÃO enviada ao padeiro',
                      'qtd': None, 'url': '/telaindustriateste/'})
    elif not tem_enviada_hoje:
        itens.append({'chave': 'ordem_ausente',
                      'rotulo': 'Hoje ainda sem ordem de produção enviada',
                      'qtd': None, 'url': '/telaindustriateste/'})

    # 2. Produção vencida (ordem de dias anteriores com falta, não dispensada).
    # Piso de 30 dias e SEM filtro de origem — espelha a conta canônica da
    # auditoria (producao_pendente.listar_pendencias, dias_vencido=30): mais
    # antigo que isso é abandono, e plano avulso/déficit também conta lá.
    falta = (func.coalesce(PlanejamentoItem.qtd_alvo, 0)
             - func.coalesce(PlanejamentoItem.produzido_qtd, 0))
    vencidas = db.session.query(
        func.coalesce(func.sum(falta), 0),
    ).select_from(PlanejamentoItem).join(
        PlanejamentoProducao,
        PlanejamentoItem.planejamento_id == PlanejamentoProducao.id,
    ).filter(
        PlanejamentoProducao.enviado_ao_padeiro.isnot(False),
        PlanejamentoProducao.data < hoje_d,
        PlanejamentoProducao.data >= hoje_d - timedelta(days=30),
        PlanejamentoItem.dispensada_em.is_(None),
        falta > 0,
    ).scalar() or 0
    if vencidas:
        itens.append({'chave': 'producao_vencida',
                      'rotulo': 'Produção vencida sem confirmação do padeiro',
                      'qtd': int(vencidas), 'url': '/telaindustriateste/'})

    # 2b. Baixas presas (19/07/2026 — caso retirada #16 Nebraska): QR não
    # escaneado deixa o estoque errado até alguém agir. Reusa a MESMA
    # verificação do alerta de WhatsApp (queries baratas com limit).
    from app.services.alertas_operacionais import verificar_baixas_presas
    presas = verificar_baixas_presas()
    if presas['retiradas']:
        itens.append({'chave': 'retiradas_presas',
                      'rotulo': ('Retiradas de sobra presas em transporte '
                                 '(loja baixou; indústria não creditada)'),
                      'qtd': len(presas['retiradas']),
                      'url': '/pedidos/retiradas'})
    if presas['separados']:
        itens.append({'chave': 'separados_presos',
                      'rotulo': ('Pedidos "separado" com entrega vencida '
                                 '(QR de saída não escaneado)'),
                      'qtd': len(presas['separados']),
                      'url': '/pedidos'})

    # 3. Orçamentos B2B parados (rascunho/enviado ativos) + aprovados que
    # ainda não viraram venda (legado pré-regime).
    orc_parados = Orcamento.query.filter(
        Orcamento.status.in_(('rascunho', 'enviado')),
        Orcamento.arquivado_em.is_(None),
    ).count()
    if orc_parados:
        itens.append({'chave': 'orcamentos',
                      'rotulo': 'Orçamentos B2B aguardando fechamento',
                      'qtd': orc_parados, 'url': '/b2b/orcamentos'})
    orc_aprovados = Orcamento.query.filter_by(status='aprovado',
                                              venda_id=None).count()
    if orc_aprovados:
        itens.append({'chave': 'orcamentos_aprovados',
                      'rotulo': 'Orçamentos aprovados sem venda criada',
                      'qtd': orc_aprovados, 'url': '/b2b/?f=aprovados'})

    # 4. Contas a pagar vencidas ou vencendo hoje (NF+boleto do mesmo
    # recebimento contam 1 — filtro relacionado_id espelha a lista).
    contas = ContaPagar.query.filter(
        ContaPagar.status == 'aberto',
        ContaPagar.relacionado_id.is_(None),
        ContaPagar.vencimento.isnot(None),
        ContaPagar.vencimento <= hoje_d,
    ).count()
    if contas:
        itens.append({'chave': 'contas_pagar',
                      'rotulo': 'Contas a pagar vencidas/vencendo hoje',
                      'qtd': contas, 'url': '/contas-pagar/'})

    # 5. Estoque com nome pendente de vínculo (balanço achou item sem
    # cadastro; sem vínculo a baixa de venda não acha a linha).
    ep = EstoqueProducao.query.filter(
        EstoqueProducao.receita_id.is_(None),
        EstoqueProducao.produto_id.is_(None),
        EstoqueProducao.nome_pendente.isnot(None),
        EstoqueProducao.nome_pendente != '',
    ).count()
    if ep:
        itens.append({'chave': 'pendente_congelados',
                      'rotulo': 'Itens sem vínculo no estoque da indústria',
                      'qtd': ep, 'url': '/pedidos/congelados'})
    el = EstoqueLoja.query.filter(
        EstoqueLoja.receita_id.is_(None),
        EstoqueLoja.produto_id.is_(None),
        EstoqueLoja.materia_prima_id.is_(None),
        EstoqueLoja.nome_pendente.isnot(None),
        EstoqueLoja.nome_pendente != '',
    ).count()
    if el:
        itens.append({'chave': 'pendente_lojas',
                      'rotulo': 'Itens sem vínculo no estoque das lojas',
                      'qtd': el, 'url': '/pedidos/estoque-loja'})

    if incluir_owner:
        # 6. Órfãos de cesta (componente sem FK não baixa estoque na venda).
        from app.services.cestas import contar_produto_itens_orfaos
        orfaos = contar_produto_itens_orfaos()
        if orfaos:
            itens.append({'chave': 'cestas_orfaos',
                          'rotulo': 'Componentes de cesta sem vínculo',
                          'qtd': orfaos, 'url': '/produtos/cestas/orfaos'})

        # 7. Pendências do sync PDV (loja/produto não mapeado trava baixa).
        try:
            from app.services import pdv_saude
            pdv = pdv_saude.contar_pendencias()
        except Exception:  # noqa: BLE001 — painel PDV fora não derruba o briefing
            logger.exception('briefing: contagem de pendencias PDV falhou')
            pdv = 0
        if pdv:
            itens.append({'chave': 'pdv_mapeamentos',
                          'rotulo': 'Mapeamentos do PDV pendentes',
                          'qtd': pdv, 'url': '/pdv/mapeamentos'})

        # Vigias linkam telas owner-only — dentro do gate (achado A2 da
        # revisão: admin comum clicava e levava 403).
        itens.extend(_vigias_doentes())
    return itens


def _vigias_doentes():
    """Vigias com estado 'doente' persistido + alertas ALTA não reconhecidos."""
    from app.models import AppConfig
    out = []
    vigias = (
        ('vigia_chatwoot_quebrado_desde', 'Vigia do Chatwoot acusando problema',
         '/admin/debug-chatwoot'),
        ('site_vigia_quebrado_desde', 'Vigia do SITE acusando problema',
         '/admin/vigia-site'),
        ('pdv_vigia_quebrado_desde', 'Vigia do PDV acusando problema',
         '/pdv/mapeamentos'),
        ('uso_ia_vigia_estourado_desde', 'Custo de IA acima do teto diário',
         '/admin/uso-ia'),
    )
    for chave, rotulo, url in vigias:
        if AppConfig.get(chave):
            out.append({'chave': chave, 'rotulo': rotulo, 'qtd': None,
                        'url': url})
    try:
        from app.services.chatbot_vigia import alertas_pendentes_resumo
        resumo = alertas_pendentes_resumo()
        if resumo.get('pendentes'):
            out.append({'chave': 'vigia_bot',
                        'rotulo': 'Alertas do vigia do bot sem reconhecimento',
                        'qtd': resumo['pendentes'], 'url': '/entregas/painel'})
    except Exception:  # noqa: BLE001 — vigia fora não derruba o briefing
        logger.exception('briefing: resumo do vigia do bot falhou')
    return out


# ── Vendas de ontem ──────────────────────────────────────────────────────────

def _resolver_loja_seru():
    """company name do Seru → nome da LOJA vinculada (SeruLojaMap confirmado).

    A MESMA loja física pode ter mais de um company no Seru (caso real
    17/07/2026, dono: "Bread & Brew e O Pão Filial Nebraska são a mesma
    loja") — agrupar pelo nome cru duplicaria a loja no briefing. Sem
    vínculo confirmado, fica o nome cru.
    """
    from app.models import Loja, SeruLojaMap
    nomes = {lj.id: lj.nome for lj in
             db.session.query(Loja.id, Loja.nome).all()}
    out = {}
    for m in SeruLojaMap.query.all():
        if m.loja_id and m.confirmado_em and m.loja_id in nomes:
            out[m.seru_company_name] = nomes[m.loja_id]
    return out


def vendas_ontem(capturar=True):
    """Vendas de ONTEM: PDV por loja (vs média do mesmo dia-da-semana) + site.

    PDV lê o snapshot `VendaSeruDiaLoja` (faturamento_pedidos = total do
    pedido, inclui kit/box — mesma base do /api/bot/faturamento), AGRUPADO
    pela Loja vinculada (dois companies da mesma loja somam numa linha).
    Site soma `PedidoOnline.valor_total` dos PAGOS ontem (por pago_em).

    `capturar=False` lê SÓ o snapshot do banco, sem chance de bater na API
    Seru — é o modo do bloco da home do admin (carrega a cada visita; o cron
    de 15 min já mantém ontem+hoje capturados). O briefing/cron continua com
    capturar=True. `snapshot_ok=False` no retorno = ontem sem snapshot (a
    tela avisa em vez de mostrar um R$ 0 falso).
    """
    from app.models import PedidoOnline, VendaSeruDiaLoja
    from app.services import vendas_diarias

    ontem = hoje() - timedelta(days=1)
    total, por_company, n_pedidos = vendas_diarias.faturamento_por_loja(
        ontem, ontem, capturar=capturar)
    snapshot_ok = ontem in vendas_diarias.dias_capturados(ontem, ontem)
    vinculo = _resolver_loja_seru()

    # Média das últimas ocorrências do MESMO dia-da-semana, por LOJA (dois
    # companies do mesmo dia somam antes da média), lida numa query só
    # (janela de 8 semanas; o dow filtra em Python — weekday() no SQL
    # diverge entre SQLite e Postgres).
    ini_hist = ontem - timedelta(days=7 * (_SEMANAS_MEDIA + 2))
    por_dia = defaultdict(float)                 # (loja, data) -> fat
    for loja_seru, d, fat in (db.session.query(
            VendaSeruDiaLoja.loja_seru, VendaSeruDiaLoja.data,
            VendaSeruDiaLoja.faturamento_pedidos)
            .filter(VendaSeruDiaLoja.data >= ini_hist,
                    VendaSeruDiaLoja.data < ontem).all()):
        if d.weekday() == ontem.weekday():
            por_dia[(vinculo.get(loja_seru, loja_seru), d)] += float(fat or 0)
    hist = defaultdict(list)
    for (nome, d), fat in por_dia.items():
        hist[nome].append((d, fat))

    # Loja com histórico e venda ZERO ontem NÃO some — é exatamente a
    # anomalia que o briefing existe pra mostrar (PDV fora o dia inteiro,
    # loja fechada...): entra com R$ 0 e queda de 100% vs a média.
    fat_por_loja = defaultdict(float)
    for loja_seru, fat in por_company.items():
        fat_por_loja[vinculo.get(loja_seru, loja_seru)] += fat
    for nome in hist:
        fat_por_loja.setdefault(nome, 0.0)
    lojas = []
    for nome, fat in sorted(fat_por_loja.items(), key=lambda kv: -kv[1]):
        ocorr = sorted(hist.get(nome, []), reverse=True)[:_SEMANAS_MEDIA]
        media = (sum(f for _, f in ocorr) / len(ocorr)) if ocorr else None
        delta = ((fat - media) / media * 100.0) if media else None
        lojas.append({'loja': nome, 'faturamento': fat, 'media': media,
                      'delta_pct': round(delta, 1) if delta is not None else None})

    # Média do TOTAL PDV do mesmo dia-da-semana (soma das lojas POR DATA,
    # depois média das últimas ocorrências) — dá o "ontem vs terça típica"
    # do total, não só por loja.
    total_por_data = defaultdict(float)
    for (_, d), fat in por_dia.items():
        total_por_data[d] += fat
    ocorr_tot = sorted(total_por_data.items(), reverse=True)[:_SEMANAS_MEDIA]
    pdv_media = (sum(f for _, f in ocorr_tot) / len(ocorr_tot)) if ocorr_tot else None
    pdv_delta = ((total - pdv_media) / pdv_media * 100.0) if pdv_media else None

    ini = datetime.combine(ontem, time.min)
    fim = datetime.combine(hoje(), time.min)
    site_rows = (db.session.query(
        func.count(PedidoOnline.id),
        func.coalesce(func.sum(PedidoOnline.valor_total), 0))
        .filter(PedidoOnline.pago_em >= ini,
                PedidoOnline.pago_em < fim).one())
    site_total = float(site_rows[1] or 0)
    return {
        'ontem': ontem,
        'label': '%s %s' % (_DOW_PT[ontem.weekday()], ontem.strftime('%d/%m')),
        'pdv_total': total,
        'pdv_media': pdv_media,
        'pdv_delta_pct': round(pdv_delta, 1) if pdv_delta is not None else None,
        'n_pedidos': n_pedidos,
        'por_loja': lojas,
        'site_qtd': int(site_rows[0] or 0),
        'site_total': site_total,
        'total_geral': round(total + site_total, 2),
        'snapshot_ok': snapshot_ok,
    }


def custo_ia_ontem():
    """Gasto de IA de ONTEM (janela fechada de calendário BRT, em USD)."""
    from app.models import UsoIA
    ini = datetime.combine(hoje() - timedelta(days=1), time.min)
    fim = datetime.combine(hoje(), time.min)
    total = (db.session.query(func.coalesce(func.sum(UsoIA.custo_usd), 0))
             .filter(UsoIA.criado_em >= ini,
                     UsoIA.criado_em < fim).scalar()) or 0
    return float(total)


# ── Montagem e envio ─────────────────────────────────────────────────────────

def montar():
    """Reúne tudo num dict — consumido pela página e pelo texto do WhatsApp."""
    return {
        'hoje_label': '%s %s' % (_DOW_PT[hoje().weekday()],
                                 hoje().strftime('%d/%m')),
        'vendas': vendas_ontem(),
        'pendencias': pendencias(incluir_owner=True),
        'custo_ia': custo_ia_ontem(),
    }


def montar_texto(dados=None):
    """O texto do WhatsApp (uma mensagem, seções curtas)."""
    d = dados or montar()
    v = d['vendas']
    linhas = ['☀️ *Briefing O Pão* — %s' % d['hoje_label'], '']
    linhas.append('💰 *Vendas de ontem (%s)*' % v['label'])
    linhas.append('PDV: %s · %d pedidos' % (_fmt_brl(v['pdv_total']),
                                            v['n_pedidos']))
    for lj in v['por_loja']:
        if lj['delta_pct'] is not None:
            sinal = '+' if lj['delta_pct'] >= 0 else ''
            comp = ' (média %s: %s, %s%.0f%%)' % (
                v['label'].split()[0], _fmt_brl(lj['media']),
                sinal, lj['delta_pct'])
        else:
            comp = ''
        linhas.append(' · %s: %s%s' % (lj['loja'], _fmt_brl(lj['faturamento']),
                                       comp))
    linhas.append('Site: %d pagos · %s' % (v['site_qtd'],
                                           _fmt_brl(v['site_total'])))
    linhas.append('*Total: %s* (PDV + site)' % _fmt_brl(v['total_geral']))
    linhas.append('')
    pend = d['pendencias']
    if pend:
        linhas.append('📋 *Precisa de você (%d)*' % len(pend))
        for it in pend:
            qtd = ' (%d)' % it['qtd'] if it.get('qtd') else ''
            linhas.append(' · %s%s → %s' % (it['rotulo'], qtd, it['url']))
    else:
        linhas.append('✅ Nada pendente — tudo rodando.')
    linhas.append('')
    linhas.append('🤖 IA ontem: US$ %.2f' % d['custo_ia'])
    return '\n'.join(linhas)


def enviar_briefing(texto=None):
    """Monta (se preciso) e envia o briefing pro WhatsApp do dono.

    Destino: SÓ `ZAPI_BOT_DONO_NUMERO` — de propósito NÃO cai no número dos
    vigias (`CHATWOOT_VIGIA_INFRA_NUMERO`), que pode ser um GRUPO da equipe:
    o briefing carrega faturamento por loja e custo, é o cockpit PESSOAL.
    `critico=True` porque é 1 msg/dia e a manhã de um incidente (teto do
    Z-API cheio por alertas) é justamente quando ele mais importa — sem
    isso viraria resumo de digest e o cron só tentaria de novo amanhã.
    """
    from flask import current_app

    from app.services import zapi
    dono = (current_app.config.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
    if not dono:
        logger.warning('briefing: sem numero do dono configurado')
        return {'ok': False, 'erro': 'sem numero do dono configurado'}
    resultado = zapi.enviar_texto(dono, texto or montar_texto(),
                                  critico=True)
    if not resultado.get('ok'):
        logger.warning('briefing: envio falhou: %s', resultado)
    return resultado
