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

# A comparação das vendas é contra a SEMANA PASSADA: o mesmo dia-da-semana,
# exatamente 7 dias antes ("sexta vs sexta passada"). Decisão do dono
# 23/07/2026, substituindo a média das últimas 6 ocorrências do dia-da-semana
# — ele quer comparar com UM dia concreto, não com uma média.
_DIAS_COMPARACAO = 7


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


def _vendas_tiny(dia):
    """Faturamento do PDV do TINY no dia: {nome_da_loja: {'total', 'n'}}.

    A Cantina vende pelo PDV do Tiny, NAO pelo Seru (27/07/2026) — sem esta
    fonte o cockpit do dono mostrava a padaria inteira MENOS a Cantina, e o
    "Total" saía subestimado. Fonte: `TinyPedidoProcessado` (o registro de
    idempotência do sync já guarda valor e data de cada venda); cancelada
    não conta.

    Só existe o que o sync já importou (janela ontem+hoje a cada 15 min):
    dia anterior ao início da integração vem vazio, e é por isso que a
    comparação com a semana passada da Cantina fica "sem comparação" no
    começo — não é queda, é falta de histórico.
    """
    from app.models import Loja
    from app.services import tiny_pdv_sync

    por_loja_id = tiny_pdv_sync.faturamento_do_dia_por_loja(dia)
    if not por_loja_id:
        return {}
    nomes = dict(db.session.query(Loja.id, Loja.nome)
                 .filter(Loja.id.in_(list(por_loja_id))).all())
    return {nomes.get(lid, 'PDV Tiny'): d for lid, d in por_loja_id.items()}


def vendas_ontem(capturar=True):
    """Vendas de ONTEM: PDV por loja (vs a SEMANA PASSADA) + site.

    A comparação é contra o MESMO dia-da-semana 7 dias antes ("sexta vs sexta
    passada" — decisão do dono 23/07/2026): `comparado_com` traz a data-base,
    `pdv_base`/`base` o faturamento dela e `*_delta_pct` a variação. Base
    ausente ou ZERO devolve delta None (não existe % sobre zero).

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
    cd = vendas_diarias.cancelamentos_descontos_do_banco(ontem, ontem)
    snapshot_ok = ontem in vendas_diarias.dias_capturados(ontem, ontem)
    vinculo = _resolver_loja_seru()

    # Base da comparação: a MESMA data 7 dias antes ("sexta vs sexta passada").
    # Uma query só, por LOJA (dois companies da mesma Loja somam antes).
    comparado_com = ontem - timedelta(days=_DIAS_COMPARACAO)
    base_por_loja = defaultdict(float)           # loja -> fat da semana passada
    for loja_seru, fat in (db.session.query(
            VendaSeruDiaLoja.loja_seru, VendaSeruDiaLoja.faturamento_pedidos)
            .filter(VendaSeruDiaLoja.data == comparado_com).all()):
        base_por_loja[vinculo.get(loja_seru, loja_seru)] += float(fat or 0)
    for nome, d in _vendas_tiny(comparado_com).items():
        base_por_loja[nome] += d['total']

    # Loja que vendeu na semana passada e ZEROU ontem NÃO some — é exatamente
    # a anomalia que o briefing existe pra mostrar (PDV fora o dia inteiro,
    # loja fechada...): entra com R$ 0 e queda de 100%.
    fat_por_loja = defaultdict(float)
    for loja_seru, fat in por_company.items():
        fat_por_loja[vinculo.get(loja_seru, loja_seru)] += fat
    # PDV do Tiny (Cantina) entra na MESMA lista, somando por nome de loja:
    # se um dia a loja tiver os dois PDVs, viram uma linha só.
    tiny = _vendas_tiny(ontem)
    tiny_total = sum(d['total'] for d in tiny.values())
    for nome, d in tiny.items():
        fat_por_loja[nome] += d['total']
        total += d['total']
        n_pedidos += d['n']
    for nome in base_por_loja:
        fat_por_loja.setdefault(nome, 0.0)
    lojas = []
    for nome, fat in sorted(fat_por_loja.items(), key=lambda kv: -kv[1]):
        # base 0/ausente => delta None ("sem comparação"): não dá pra calcular
        # variação percentual sobre zero (nem inventar 'crescimento infinito').
        base = base_por_loja.get(nome)
        delta = ((fat - base) / base * 100.0) if base else None
        lojas.append({'loja': nome, 'faturamento': fat, 'base': base,
                      'delta_pct': round(delta, 1) if delta is not None else None})

    # Mesma comparação pro TOTAL do PDV (soma das lojas na data-base) — dá o
    # "ontem vs a mesma sexta da semana passada" do total, não só por loja.
    pdv_base = sum(base_por_loja.values()) or None
    pdv_delta = ((total - pdv_base) / pdv_base * 100.0) if pdv_base else None

    ini = datetime.combine(ontem, time.min)
    fim = datetime.combine(hoje(), time.min)
    site_rows = (db.session.query(
        func.count(PedidoOnline.id),
        func.coalesce(func.sum(PedidoOnline.valor_total), 0))
        .filter(PedidoOnline.pago_em >= ini,
                PedidoOnline.pago_em < fim,
                # Divulgacao nunca conta como venda (pago_em ja e NULL nela —
                # guard explicito pra documentar/blindar).
                PedidoOnline.divulgacao.is_(False)).one())
    site_total = float(site_rows[1] or 0)
    return {
        'ontem': ontem,
        'label': '%s %s' % (_DOW_PT[ontem.weekday()], ontem.strftime('%d/%m')),
        'pdv_total': total,
        # Base da comparação: o MESMO dia-da-semana 7 dias antes.
        'comparado_com': comparado_com,
        'comparado_com_label': '%s %s' % (_DOW_PT[comparado_com.weekday()],
                                          comparado_com.strftime('%d/%m')),
        'pdv_base': pdv_base,
        'pdv_delta_pct': round(pdv_delta, 1) if pdv_delta is not None else None,
        'n_pedidos': n_pedidos,
        'por_loja': lojas,
        'site_qtd': int(site_rows[0] or 0),
        'site_total': site_total,
        'total_geral': round(total + site_total, 2),
        'cancelados_n': cd['cancelados_n'],
        'cancelados_valor': cd['cancelados_valor'],
        'desconto': cd['desconto'],
        'snapshot_ok': snapshot_ok,
    }


def vendas_hoje(capturar=False):
    """Vendas de HOJE até agora (parciais): PDV do snapshot + site pago hoje.

    O cron do Seru recaptura ontem+hoje a cada ~15 min, então o snapshot de
    hoje fica no máximo esse tanto atrasado. Default `capturar=False` (modo
    da home — nunca bate na API); SEM delta DE PROPÓSITO: comparar um dia
    INCOMPLETO com um dia CHEIO (a semana passada inteira) só geraria um
    "-60%" falso a manhã inteira. A comparação vive no painel de ONTEM.
    """
    from app.models import PedidoOnline
    from app.services import vendas_diarias

    hoje_d = hoje()
    total, por_company, n_pedidos = vendas_diarias.faturamento_por_loja(
        hoje_d, hoje_d, capturar=capturar)
    cd = vendas_diarias.cancelamentos_descontos_do_banco(hoje_d, hoje_d)
    vinculo = _resolver_loja_seru()
    fat_por_loja = defaultdict(float)
    for loja_seru, fat in por_company.items():
        fat_por_loja[vinculo.get(loja_seru, loja_seru)] += fat
    lojas = [{'loja': nome, 'faturamento': fat}
             for nome, fat in sorted(fat_por_loja.items(), key=lambda kv: -kv[1])]

    ini = datetime.combine(hoje_d, time.min)
    site_rows = (db.session.query(
        func.count(PedidoOnline.id),
        func.coalesce(func.sum(PedidoOnline.valor_total), 0))
        .filter(PedidoOnline.pago_em >= ini,
                PedidoOnline.divulgacao.is_(False)).one())
    site_total = float(site_rows[1] or 0)
    return {
        'hoje': hoje_d,
        'label': '%s %s' % (_DOW_PT[hoje_d.weekday()], hoje_d.strftime('%d/%m')),
        'pdv_total': total,
        'n_pedidos': n_pedidos,
        'por_loja': lojas,
        'site_qtd': int(site_rows[0] or 0),
        'site_total': site_total,
        'total_geral': round(total + site_total, 2),
        'cancelados_n': cd['cancelados_n'],
        'cancelados_valor': cd['cancelados_valor'],
        'desconto': cd['desconto'],
    }


def cancelados_descontos_detalhe(dia):
    """Detalhe AO VIVO (bate na API Seru) dos pedidos CANCELADOS e dos com
    DESCONTO de `dia` — o drill-down do cockpit da home ("abrir" cancelamentos/
    descontos). Read-only, NAO persiste. Diferente do resto do briefing (que le
    so o snapshot): a lista pedido-a-pedido nao existe no snapshot, entao ESTE
    caminho — e SO ele, no clique explicito — consulta a API. Levanta a excecao
    pra rota tratar (502) se o Seru cair.

    Cada pedido cancelado: hora, loja, valor (total), caixa, tem NF autorizada.
    Cada pedido com desconto (nao cancelado): hora, loja, subtotal, desconto,
    total. Loja resolvida pelo vinculo (mesma da home)."""
    from app.services import seru
    from app.services.venda_sem_item_vigia import _nf_autorizada
    from app.services.vendas_itens import _nome_loja

    def _f(v):
        """Valor da API -> float tolerante (nunca explode por um campo torto)."""
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    pedidos = seru.listar_pedidos_completo(dia, dia)
    vinculo = _resolver_loja_seru()
    cancelados, descontos = [], []
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        # UM pedido torto (campo malformado) NAO pode derrubar o modal inteiro
        # (mesma regra do venda_sem_item_vigia) — pula e loga, nunca 502 geral.
        try:
            if seru.data_local(p.get('createdAt')) != dia:
                continue
            ln_raw = _nome_loja(p) or '(sem loja)'
            loja = vinculo.get(ln_raw, ln_raw)
            dh = seru.datahora_local(p.get('createdAt'))
            hora = dh.strftime('%H:%M') if dh else '?'
            total = _f(p.get('total'))
            cx = p.get('cashier')
            caixa = cx.get('code') if isinstance(cx, dict) else None
            if seru.pedido_cancelado(p):
                cancelados.append({
                    'codigo': p.get('code'), 'hora': hora, 'loja': loja,
                    'valor': round(total, 2), 'caixa': caixa,
                    'nf': _nf_autorizada(p)})
                continue
            desc = _f(p.get('discount'))
            if desc > 0:
                descontos.append({
                    'codigo': p.get('code'), 'hora': hora, 'loja': loja,
                    'subtotal': round(_f(p.get('subtotal')), 2),
                    'desconto': round(desc, 2), 'total': round(total, 2)})
        except Exception:  # noqa: BLE001 — pedido torto isolado, resto segue
            logger.warning('cancelados_descontos_detalhe: pedido ignorado (%s)',
                           p.get('id') or p.get('code'), exc_info=True)
    cancelados.sort(key=lambda x: x['hora'])
    descontos.sort(key=lambda x: -x['desconto'])
    return {
        'cancelados': cancelados,
        'descontos': descontos,
        'cancelados_valor': round(sum(c['valor'] for c in cancelados), 2),
        'desconto_total': round(sum(d['desconto'] for d in descontos), 2),
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
            comp = ' (%s: %s, %s%.0f%%)' % (
                v['comparado_com_label'], _fmt_brl(lj['base']),
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
