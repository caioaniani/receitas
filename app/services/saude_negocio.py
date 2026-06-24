"""Radar de saude do negocio: Contas a Pagar + catalogo de Receitas.

Nasceu de uma angustia real do dono (2026-06-10): "nao sei o estado real" —
documentos chegando pelo Slack sem revisao, receitas com ficha incompleta e
margem desconhecida. Este servico transforma isso em NUMEROS:

- `resumo_contas()`   vencidas / vencendo 7d / extracao incompleta / novas 24h
- `resumo_receitas()` ficha incompleta / sem preco / margem critica
- digest diario 07:30 BRT no WhatsApp do dono (`seru_cron`, DIGEST_SAUDE=0
  desliga) + consulta na hora em `GET /admin/saude`.

"Extracao incompleta" = conta aberta sem valor OU sem vencimento: e o rastro
de quando a IA falhou na leitura do documento do Slack (o fluxo NUNCA descarta
o documento — cria a conta vazia; aqui ela ganha visibilidade pra revisao).
"""
import logging
from datetime import timedelta

from flask import current_app
from sqlalchemy import func, or_

from app.utils import agora, hoje

logger = logging.getLogger(__name__)


def _fmt_reais(v):
    txt = f'{float(v or 0):,.2f}'
    return 'R$ ' + txt.replace(',', 'X').replace('.', ',').replace('X', '.')


def resumo_contas():
    """Numeros de ContaPagar que importam pro radar. Valores como float."""
    from app.models import ContaPagar
    h = hoje()
    em7 = h + timedelta(days=7)
    ha24h = agora() - timedelta(hours=24)

    abertas = ContaPagar.query.filter_by(status='aberto')

    def _conta_e_soma(q):
        qtd = q.count()
        soma = (q.with_entities(
            func.coalesce(func.sum(ContaPagar.valor_total), 0)).scalar()) or 0
        return qtd, float(soma)

    venc_q = abertas.filter(ContaPagar.vencimento.isnot(None),
                            ContaPagar.vencimento < h)
    prox_q = abertas.filter(ContaPagar.vencimento.isnot(None),
                            ContaPagar.vencimento >= h,
                            ContaPagar.vencimento <= em7)
    incompletas_q = abertas.filter(or_(ContaPagar.valor_total.is_(None),
                                       ContaPagar.vencimento.is_(None)))

    vencidas, vencidas_total = _conta_e_soma(venc_q)
    vencendo, vencendo_total = _conta_e_soma(prox_q)
    return {
        'abertas': abertas.count(),
        'vencidas': vencidas,
        'vencidas_total': vencidas_total,
        'vencendo_7d': vencendo,
        'vencendo_7d_total': vencendo_total,
        'extracao_incompleta': incompletas_q.count(),
        # Fase 2: abertas que NENHUM humano conferiu (dados = chute da IA)
        'nao_revisadas': abertas.filter(
            ContaPagar.revisada_em.is_(None)).count(),
        'novas_24h': ContaPagar.query.filter(
            ContaPagar.criado_em >= ha24h).count(),
    }


def resumo_receitas(margem_minima=None):
    """Saude do catalogo: fichas incompletas, sem preco e margem critica.

    Margem por canal = (preco - custo_unitario) / preco * 100 — mesma conta
    do copilot (`_read_consultar_margem`). `margem_minima` default vem de
    SAUDE_MARGEM_MINIMA (30%)."""
    from app.models import Receita
    from app.services import custos as custos_svc

    if margem_minima is None:
        margem_minima = float(current_app.config.get('SAUDE_MARGEM_MINIMA', 30))

    dados = custos_svc.calcular_custos_receitas()
    custos = dados.get('custos') or {}

    # Arquivada nao pede acao — fora do radar de fichas incompletas.
    receitas = (Receita.query.filter(Receita.arquivada_em.is_(None))
                .order_by(Receita.nome).all())
    ficha_incompleta = []
    sem_preco = []
    margem_critica = []

    for r in receitas:
        tem_ingredientes = bool(getattr(r, 'ingredientes', None))
        if not tem_ingredientes:
            ficha_incompleta.append(r.nome)
            continue   # sem ficha nao da pra calcular custo/margem

        precos = (('atacado', r.preco_venda), ('loja', r.preco_loja),
                  ('site', r.preco_site))
        com_preco = [(canal, p) for canal, p in precos if p and p > 0]
        if not com_preco:
            sem_preco.append(r.nome)
            continue

        custo = custos.get(r.nome)
        if not custo or custo <= 0:
            continue   # motor de custos nao resolveu (ex: MP sem custo)
        for canal, preco in com_preco:
            margem = (preco - custo) / preco * 100
            if margem < margem_minima:
                margem_critica.append({
                    'nome': r.nome, 'canal': canal,
                    'preco': round(float(preco), 2),
                    'custo': round(float(custo), 2),
                    'margem': round(margem, 1),
                })

    margem_critica.sort(key=lambda x: x['margem'])
    return {
        'total_receitas': len(receitas),
        'ficha_incompleta': ficha_incompleta,
        'sem_preco': sem_preco,
        'margem_critica': margem_critica,
        'margem_minima': margem_minima,
        'circulares': dados.get('circulares') or [],
    }


def montar_digest_saude():
    """Texto WhatsApp com o radar. So mostra o que precisa de acao —
    secao zerada vira 1 linha de ✅."""
    c = resumo_contas()
    r = resumo_receitas()

    linhas = ['*📊 Radar do negocio*', '', '*Contas a pagar*']
    tem_alerta_contas = False
    if c['vencidas']:
        linhas.append(f'🔴 {c["vencidas"]} VENCIDA(S) — '
                      f'{_fmt_reais(c["vencidas_total"])}')
        tem_alerta_contas = True
    if c['vencendo_7d']:
        linhas.append(f'🟡 {c["vencendo_7d"]} vencendo em 7 dias — '
                      f'{_fmt_reais(c["vencendo_7d_total"])}')
        tem_alerta_contas = True
    if c['extracao_incompleta']:
        linhas.append(f'🔍 {c["extracao_incompleta"]} sem valor/vencimento '
                      '(IA nao leu — revisar em /contas-pagar)')
        tem_alerta_contas = True
    if c.get('nao_revisadas'):
        linhas.append(f'👀 {c["nao_revisadas"]} aguardando conferencia humana')
        tem_alerta_contas = True
    if c['novas_24h']:
        linhas.append(f'📥 {c["novas_24h"]} nova(s) nas ultimas 24h')
        tem_alerta_contas = True
    if not tem_alerta_contas:
        linhas.append('✅ Em dia — nada vencido ou pendente.')

    linhas += ['', '*Receitas*']
    tem_alerta_rec = False
    if r['margem_critica']:
        linhas.append(f'🔴 {len(r["margem_critica"])} com margem abaixo de '
                      f'{r["margem_minima"]:.0f}%:')
        for m in r['margem_critica'][:5]:
            linhas.append(f'  • {m["nome"]} ({m["canal"]}): {m["margem"]:.0f}% '
                          f'— custa {_fmt_reais(m["custo"])}, '
                          f'vende {_fmt_reais(m["preco"])}')
        if len(r['margem_critica']) > 5:
            linhas.append(f'  _...e mais {len(r["margem_critica"]) - 5}_')
        tem_alerta_rec = True
    if r['ficha_incompleta']:
        linhas.append(f'📋 {len(r["ficha_incompleta"])} sem ficha '
                      '(ingredientes nao cadastrados)')
        tem_alerta_rec = True
    if r['sem_preco']:
        linhas.append(f'💸 {len(r["sem_preco"])} sem preco de venda')
        tem_alerta_rec = True
    if not tem_alerta_rec:
        linhas.append('✅ Catalogo saudavel.')

    linhas += ['', '_Detalhes: /admin/saude_']
    return '\n'.join(linhas)


def enviar_digest_saude():
    """Envia o radar pro WhatsApp do dono. Best-effort (so loga falha)."""
    cfg = current_app.config
    numero = ((cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
              or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())
    if not numero:
        logger.info('digest saude: sem numero de destino configurado')
        return {'ok': False, 'motivo': 'sem numero'}
    try:
        texto = montar_digest_saude()
    except Exception:  # noqa: BLE001
        logger.exception('digest saude: falha montando')
        return {'ok': False, 'motivo': 'falha montando'}
    from app.services import zapi
    return zapi.enviar_texto(numero, texto)
