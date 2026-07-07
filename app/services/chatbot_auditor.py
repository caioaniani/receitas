"""Auditor proativo do chatbot — agente ativo que vai atras dos problemas.

Roda 1x por dia (cron 19h BRT no seru_cron) + sob demanda em
/admin/auditor/run. Le todos os VigiaVeredito do periodo, agrega, manda pra
Claude Sonnet detectar PADROES (handoff evitavel repetido, produto com erro
recorrente, momento de pico critico, perda de venda) e escreve um resumo
acionavel pro WhatsApp do dono via Z-API.

Usa Sonnet (nao Haiku) porque eh meta-analise — vale a pena. Volume baixo
(1x dia), custo ~R$0,30/dia.
"""
import json
import logging
import os
from collections import Counter
from datetime import datetime, timedelta

from flask import current_app

logger = logging.getLogger(__name__)

MODELO = 'claude-sonnet-4-6'
MAX_TOKENS = 1200

PROMPT_AUDITOR = """Você é o Auditor do bot de atendimento da O Pão (padaria artesanal).
Sua função é olhar os dados agregados do dia (ou periodo) e devolver:

1. Resumo numérico curto
2. PROBLEMAS identificados — apenas os que aparecem como PADRÃO (>=2 ocorrências OU 1 grave)
3. Sugestão concreta de ajuste pra cada problema

Tipos de problema relevantes:
- Bot empurrando pro humano coisa que deveria saber (ex: 3x dúvida de conteúdo de cesta, 2x agendamento, etc)
- Produto recebendo afirmação incorreta repetida (esgotado errado, preço estranho)
- Clientes desistindo no mesmo ponto (perda de venda recorrente)
- Cliente irritado/surtando (mesmo só 1 caso, é grave)
- Bot dando resposta confusa/truncada/contraditória

TOM (regra do dono, 02/07/2026): você é o BALANÇO FRIO do dia — alarme em
tempo real é papel do VIGIA, que já avisou o dono na hora. NUNCA use 🚨,
"URGENTE", "venda em risco" ou tom de pânico. Relate fatos + sugestão.
AMOSTRA PEQUENA (< 10 conversas): NÃO manchete porcentagem (1/2 = "50%" é
ruído, não tendência) e não fale em "100% dos handoffs" — use números
absolutos ("2 conversas, 1 handoff") e tom neutro. 1 caso é 1 caso: descreva
factualmente o que houve e a sugestão, sem extrapolar padrão.

Horário de pico: só afirme com base no histograma `por_hora` dos dados —
sem ele (ausente/vazio), não fale de pico.

Seja DIRETO e CURTO. Linguagem coloquial brasileira, sem corporativês. NUNCA invente número.

Responda APENAS com JSON neste formato:
{
  "tem_problemas": true|false,
  "resumo_curto": "1-2 linhas com o número do dia",
  "problemas": [
    {"tema": "frase curta", "ocorrencias": N, "exemplos": ["frase 1", "frase 2"], "sugestao": "ajuste concreto"}
  ],
  "destaque": "frase única pro topo do WhatsApp (ex: 'Tudo tranquilo hoje' ou '3 vendas perdidas no frete')"
}

NUNCA texto fora do JSON."""


PROMPT_AUDITOR_RESUMO = """Você é o Auditor do bot de atendimento da O Pão (padaria artesanal).
Esta é a auditoria de FIM DE DIA — o dono vai ler isto como balanço diário.
Sempre devolva o relatório, MESMO se o dia foi tranquilo (vai pra registro).

METRICA-MAE: `contencao_pct` = % das conversas que terminaram SEM
transferir_para_humano. Meta do dono = 90%. Com 10+ conversas, mencione o
valor real no `destaque` ou `resumo_curto` (ex: "Contenção 87% (meta 90%)").
NUNCA arredonde pra cima — número exato dos dados. Com MENOS de 10 conversas,
NÃO manchete porcentagem (1/2 = "50%" é ruído, não tendência): use números
absolutos ("2 conversas, 1 handoff") e tom neutro.

`handoffs_preguicosos` = handoffs em que o bot NAO chamou tool de busca
antes (so transferir_para_humano ou nada). E SINTOMA DE PROMPT FALHO,
nao limite de capacidade — sempre vire `problema` no relatorio quando >=1,
mas descrito FACTUALMENTE (1 caso é 1 caso, sem extrapolar padrão).

TOM (regra do dono, 02/07/2026): você é o BALANÇO FRIO do fim do dia —
alarme em tempo real é papel do VIGIA, que já avisou o dono na hora. NUNCA
use 🚨, "URGENTE", "venda em risco" ou tom de pânico. Fato + sugestão.

DADOS que você recebe (use-os, não invente):
- `por_hora`: histograma REAL de eventos por hora — cite horário de pico SÓ
  a partir dele; se ausente/vazio, não fale de pico.
- `funil_site`: pedidos do site no período (criados/pagos/cancelados +
  faturamento pago) — cruze com as conversas quando fizer sentido (ex:
  muita conversa e pouco pedido pago = atrito em algum ponto).
- `comparativo_dia_anterior`: os mesmos números de ontem — cite tendência
  (melhorou/piorou) em 1 frase quando a diferença for relevante; sem essa
  chave, não compare com dia nenhum.

Devolva:
1. Destaque do dia (1 frase curta, com contenção real se tiver dado)
2. Resumo numérico (1-2 linhas, números reais)
3. INSIGHTS — o que aprendemos hoje sobre o atendimento (top temas que o cliente perguntou, horário de pico do `por_hora`, funil do site, tendência vs ontem). 1-3 bullets.
4. PROBLEMAS — só os que viraram padrão (>=2 ocorrências) OU graves. Pode ser lista vazia.
5. Sugestões concretas pra cada problema (não invente — só se houver dado).

Linguagem coloquial brasileira, direto, curto. NUNCA invente número.

Responda APENAS com JSON:
{
  "destaque": "1 frase pro topo",
  "resumo_curto": "1-2 linhas com números",
  "insights": ["bullet 1", "bullet 2"],
  "problemas": [
    {"tema": "frase curta", "ocorrencias": N, "exemplos": ["frase 1"], "sugestao": "ajuste"}
  ]
}

NUNCA texto fora do JSON."""


def disponivel():
    cfg = current_app.config
    return bool(os.environ.get('ANTHROPIC_API_KEY') or cfg.get('ANTHROPIC_API_KEY'))


def _numero_destino():
    cfg = current_app.config
    return ((cfg.get('CHATBOT_VIGIA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())


_TRACKING_PREFIXES = ('[FOLLOWUP', '[ABANDONO', '[ESPERA_HUMANO')


def _eh_conversa_real(v):
    """Filtra registros que NAO representam conversa real de cliente:
    - conv_id 'teste-*' (do /admin/vigia/teste)
    - mensagens-tracking dos detectores deterministicos (followup,
      abandono, espera humano) que gravam VigiaVeredito sem `bot_acao`
      e mensagem com prefixo [FOLLOWUP/[ABANDONO/[ESPERA_HUMANO.
    Sem esse filtro, o denominador inflaria e a contencao apareceria
    menor do que e na realidade."""
    cid = (v.conv_id or '')
    if cid.startswith('teste-'):
        return False
    msg = (v.mensagem_cliente or '')
    if msg.startswith(_TRACKING_PREFIXES):
        return False
    return True


def _tools_de(v):
    """Devolve a lista de tools persistida no veredito. None = registro SEM
    o dado (bot antigo / coluna vazia / JSON corrompido) — diferente de []
    (o bot rodou e nao chamou tool nenhuma). Best-effort — nao quebra o
    auditor."""
    raw = (v.tools_usadas or '').strip()
    if not raw:
        return None
    try:
        out = json.loads(raw)
        return out if isinstance(out, list) else None
    except (ValueError, TypeError):
        return None


def _eh_handoff_preguicoso(v):
    """Delegado pra regra UNICA `chatbot_vigia.handoff_foi_preguicoso` —
    antes cada modulo tinha a propria copia e elas divergiam (aqui
    `encerrar_conversa` contava como "tentou algo"; no vigia nao). Registro
    sem `tools_usadas` (None) NAO conta como preguicoso: sem dado, nao da
    pra acusar."""
    from app.services.chatbot_vigia import handoff_foi_preguicoso
    return handoff_foi_preguicoso(_tools_de(v), conv_id=v.conv_id)


def _funil_site(inicio, fim):
    """Funil de vendas do SITE no periodo (PedidoOnline): criados, pagos,
    cancelados e faturamento pago. Da ao auditor o elo que faltava entre
    "conversa boa" e "venda fechada" (muita conversa + pouco pedido pago =
    atrito em algum ponto). `pago_em` (nao status) marca pagamento — o
    status continua transitando depois (enviado/entregue). Best-effort:
    erro devolve None e o relatorio sai sem o funil."""
    try:
        from app.models import PedidoOnline
        pedidos = (PedidoOnline.query
                   .filter(PedidoOnline.criado_em >= inicio,
                           PedidoOnline.criado_em < fim)
                   .all())
        pagos = [p for p in pedidos if p.pago_em is not None]
        faturamento = sum((p.valor_total or 0) for p in pagos)
        return {
            'pedidos_criados': len(pedidos),
            'pedidos_pagos': len(pagos),
            'pedidos_cancelados': sum(
                1 for p in pedidos if p.status == 'cancelado'),
            'faturamento_pago': float(round(faturamento, 2)),
        }
    except Exception:  # noqa: BLE001
        logger.exception('auditor: funil do site falhou')
        return None


# Chaves que entram no comparativo com o dia anterior (resumo das 19h).
# So numeros agregados — as amostras/motivos de ontem nao interessam.
_CHAVES_COMPARATIVO = ('conversas_unicas', 'handoffs', 'contencao_pct',
                       'handoffs_preguicosos', 'gravidade_alta',
                       'gravidade_media', 'funil_site')


def _resumo_comparativo(dados):
    """Versao enxuta dos dados de um periodo, pro Sonnet citar tendencia
    (melhorou/piorou vs ontem) sem receber o relatorio inteiro de novo."""
    if not dados:
        return None
    return {k: dados.get(k) for k in _CHAVES_COMPARATIVO}


def _coletar_periodo(inicio, fim):
    """Le VigiaVeredito do periodo e devolve uma estrutura compacta pro
    Sonnet trabalhar. Tudo agregado pra caber no prompt."""
    from app.models import VigiaVeredito

    todos = (VigiaVeredito.query
             .filter(VigiaVeredito.criado_em >= inicio,
                     VigiaVeredito.criado_em < fim)
             .order_by(VigiaVeredito.criado_em.asc())
             .all())
    if not todos:
        return None
    veredictos = [v for v in todos if _eh_conversa_real(v)]
    if not veredictos:
        return None

    total = len(veredictos)
    handoffs = [v for v in veredictos if (v.bot_acao or '') == 'handoff']
    alta = [v for v in veredictos if v.gravidade == 'alta']
    media = [v for v in veredictos if v.gravidade == 'media']
    conv_unicas = len({v.conv_id for v in veredictos if v.conv_id})

    # Conversas distintas que tiveram >=1 handoff no periodo.
    conv_com_handoff = len({v.conv_id for v in handoffs if v.conv_id})
    handoffs_preguicosos = [v for v in handoffs if _eh_handoff_preguicoso(v)]
    conv_preguicosa = len({v.conv_id for v in handoffs_preguicosos if v.conv_id})

    # Taxa de contencao = % das conversas distintas que terminaram SEM
    # handoff. Meta do dono = 90%. Arredonda 1 casa pra evitar ruido.
    contencao_pct = (round(100.0 * (conv_unicas - conv_com_handoff) / conv_unicas, 1)
                     if conv_unicas else None)
    preguicosos_pct = (round(100.0 * len(handoffs_preguicosos) / len(handoffs), 1)
                       if handoffs else None)

    # Top motivos de handoff (ja vem do bot quando faz handoff)
    motivos_handoff = Counter(
        (v.bot_motivo or '').strip().lower()
        for v in handoffs if (v.bot_motivo or '').strip()
    ).most_common(10)

    # Top motivos do vigia (o que ELE achou)
    motivos_vigia = Counter(
        (v.motivo_vigia or '').strip()[:120]
        for v in veredictos if (v.motivo_vigia or '').strip()
    ).most_common(15)

    # Amostras de mensagens nos handoffs (limitado, pra dar contexto).
    # Inclui as tools usadas pra o Sonnet conseguir cruzar a queixa do
    # vigia com o que o bot DE FATO tentou.
    amostras_handoff = []
    for v in handoffs[:30]:
        msg = (v.mensagem_cliente or '').strip()[:200]
        motivo = (v.bot_motivo or '').strip()[:120]
        if msg:
            amostras_handoff.append({'msg': msg, 'motivo': motivo,
                                     'cliente': v.cliente or '',
                                     'tools': _tools_de(v)})

    # Casos de alta (raros, mas todos)
    casos_alta = [{
        'cliente': v.cliente or '', 'msg': (v.mensagem_cliente or '')[:200],
        'motivo': (v.motivo_vigia or '')[:200],
        'tools': _tools_de(v),
    } for v in alta]

    # Histograma real por hora — o prompt sempre pediu "horario de pico"
    # mas ate 02/07/2026 o Sonnet nao recebia timestamp nenhum (inventava
    # ou omitia). Hora BRT do criado_em de cada evento de conversa real.
    por_hora = Counter(v.criado_em.hour for v in veredictos if v.criado_em)

    return {
        'total_eventos': total,
        'conversas_unicas': conv_unicas,
        'handoffs': len(handoffs),
        'conversas_com_handoff': conv_com_handoff,
        'handoffs_preguicosos': len(handoffs_preguicosos),
        'conversas_preguicosas': conv_preguicosa,
        'contencao_pct': contencao_pct,
        'preguicosos_pct': preguicosos_pct,
        'gravidade_alta': len(alta),
        'gravidade_media': len(media),
        'top_motivos_handoff': motivos_handoff,
        'top_motivos_vigia': motivos_vigia,
        'amostras_handoff': amostras_handoff,
        'casos_alta': casos_alta,
        'por_hora': {f'{h:02d}h': n for h, n in sorted(por_hora.items())},
        'funil_site': _funil_site(inicio, fim),
    }


def _chamar_sonnet(api_key, contexto, prompt_sistema=None):
    import anthropic
    # timeout: roda em cron/rota best-effort — nunca vale segurar 10min.
    # SEM cache_control de proposito: as execucoes ficam HORAS entre si
    # (7/9/12/15/19h) e o cache ephemeral vive 5min — pagariamos o premio
    # de escrita (1.25x) sem nunca ler de volta.
    client = anthropic.Anthropic(api_key=api_key, timeout=60, max_retries=1)
    resp = client.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        system=prompt_sistema or PROMPT_AUDITOR,
        messages=[{'role': 'user', 'content': contexto}],
    )
    from app.services import uso_ia
    uso_ia.registrar('auditor', MODELO, getattr(resp, 'usage', None))
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:].strip()
        texto = texto.rsplit('```', 1)[0].strip()
    return json.loads(texto)


# Abaixo disto, porcentagem de contencao e ruido (1/2 = "50%") — a linha do
# topo mostra so os numeros absolutos. Pedido do dono (02/07/2026): o auditor
# fazia alarde com amostra minuscula.
_AMOSTRA_MINIMA_PCT = 10


def _linha_contencao(dados):
    """Renderiza 'Contencao: 87,5% (28/32 conversas) | preguicoso: 2/4'.
    Com amostra pequena (< _AMOSTRA_MINIMA_PCT conversas) troca a manchete de
    porcentagem por numeros absolutos. Vazia se nao tem dados suficientes."""
    if not dados:
        return ''
    conv = dados.get('conversas_unicas') or 0
    com_hand = dados.get('conversas_com_handoff') or 0
    pct = dados.get('contencao_pct')
    if not conv or pct is None:
        return ''
    sem_hand = conv - com_hand
    if conv < _AMOSTRA_MINIMA_PCT:
        base = (f'*Conversas:* {conv} no período · {sem_hand} resolvida(s) '
                f'pelo bot · {com_hand} com handoff')
    else:
        base = f'*Contenção:* {pct}% ({sem_hand}/{conv} conversas)'
    preg = dados.get('handoffs_preguicosos') or 0
    handoffs = dados.get('handoffs') or 0
    if handoffs:
        base += f'  ·  preguiçoso: {preg}/{handoffs}'
    return base


def _montar_mensagem(rel, inicio, fim, *, titulo='Auditor do bot', dados=None):
    linhas = []
    periodo = (f'{inicio.strftime("%d/%m")} a {fim.strftime("%d/%m")}'
               if (fim - inicio).days > 1
               else inicio.strftime('%d/%m'))
    linhas.append(f'*{titulo} — {periodo}*')
    contencao = _linha_contencao(dados)
    if contencao:
        linhas.append('')
        linhas.append(contencao)
    if rel.get('destaque'):
        linhas.append('')
        linhas.append(rel['destaque'])
    if rel.get('resumo_curto'):
        linhas.append('')
        linhas.append(rel['resumo_curto'])
    insights = rel.get('insights') or []
    if insights:
        linhas.append('')
        linhas.append('*Insights:*')
        for ins in insights:
            ins = (ins or '').strip()
            if ins:
                linhas.append(f'• {ins}')
    problemas = rel.get('problemas') or []
    if problemas:
        linhas.append('')
        linhas.append('*Problemas:*')
        for p in problemas:
            t = (p.get('tema') or '').strip()
            n = p.get('ocorrencias') or 0
            s = (p.get('sugestao') or '').strip()
            if t:
                linhas.append(f'• {t} ({n}x)')
                if s:
                    linhas.append(f'  → {s}')
    return '\n'.join(linhas)


def auditar_periodo(inicio, fim, *, enviar=True, forcar_envio=False,
                    prompt_sistema=None, titulo='Auditor do bot',
                    dados_extras=None):
    """Audita o periodo [inicio, fim). Retorna {'ok', 'rel', 'enviado',
    'mensagem'} ou {'pulou'}/{'erro'}. Best-effort.

    - `enviar`: se False, so retorna sem mexer em Z-API (preview).
    - `forcar_envio`: True envia MESMO dia tranquilo (modo resumo de fim de dia).
    - `prompt_sistema`: prompt customizado (padrao = PROMPT_AUDITOR pra
      janelas curtas, PROMPT_AUDITOR_RESUMO pra fim de dia).
    - `dados_extras`: dict mesclado nos dados agregados antes de ir pro
      Sonnet (ex: `comparativo_dia_anterior` no resumo das 19h)."""
    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'pulou': 'sem ANTHROPIC_API_KEY'}

    dados = _coletar_periodo(inicio, fim)
    if not dados:
        return {'pulou': 'sem dados no periodo', 'ok': True}
    if dados_extras:
        dados.update(dados_extras)

    contexto = (
        f'Periodo: {inicio.isoformat()} a {fim.isoformat()}\n\n'
        f'DADOS AGREGADOS:\n{json.dumps(dados, ensure_ascii=False, indent=2)}'
    )
    try:
        rel = _chamar_sonnet(api_key, contexto, prompt_sistema=prompt_sistema)
    except Exception as exc:  # noqa: BLE001
        logger.exception('auditor: Sonnet falhou')
        return {'erro': str(exc)}

    mensagem = _montar_mensagem(rel, inicio, fim, titulo=titulo, dados=dados)
    resultado = {'ok': True, 'rel': rel, 'mensagem': mensagem,
                 'dados': dados, 'enviado': False}

    if not enviar:
        return resultado
    # Modo "janela curta": so manda se tiver problema. Modo "fim de dia"
    # (forcar_envio=True): manda mesmo dia tranquilo.
    if not forcar_envio:
        if not rel.get('tem_problemas') and not (rel.get('problemas') or []):
            logger.info('auditor: sem problemas, sem envio')
            return resultado

    numero = _numero_destino()
    if not numero:
        logger.warning('auditor: sem destino — relatorio nao enviado')
        return resultado

    try:
        from app.services import zapi
        envio = zapi.enviar_texto(numero, mensagem)
        resultado['enviado'] = bool(envio.get('ok'))
        resultado['envio'] = envio
    except Exception as exc:  # noqa: BLE001
        logger.exception('auditor: envio Z-API falhou')
        resultado['erro_envio'] = str(exc)
    return resultado


def auditar_dia(dia=None, *, enviar=True):
    """Audita um dia inteiro em BRT (default = ontem inteiro)."""
    from app.utils import hoje as _hoje
    base = dia or (_hoje() - timedelta(days=1))
    inicio = datetime.combine(base, datetime.min.time())
    fim = inicio + timedelta(days=1)
    return auditar_periodo(inicio, fim, enviar=enviar)


def auditar_hoje(*, enviar=True):
    """Audita o dia corrente (ate agora) — usado pelo botao on-demand."""
    from app.utils import agora as _agora
    from app.utils import hoje as _hoje
    inicio = datetime.combine(_hoje(), datetime.min.time())
    fim = _agora()
    return auditar_periodo(inicio, fim, enviar=enviar)


CHAVE_ULTIMA_EXEC = 'chatbot_auditor_ultima_exec'
# Quanto tempo pra tras ir na primeira execucao (quando nao ha registro).
# 24h cobre desde a ultima rodada do dia anterior (19h), sem ir longe demais.
FALLBACK_JANELA_H = 24


def auditar_dia_resumo(dia=None, *, enviar=True):
    """Resumo de fim de dia (sempre envia, mesmo dia tranquilo). Audita o
    DIA INTEIRO em BRT. Usado pelo cron das 19h no modo hibrido.

    Diferente das janelas curtas: traz numeros + insights + problemas, e
    nao depende do ponteiro `ultima_exec` (sempre olha o dia inteiro).
    Inclui `comparativo_dia_anterior` (mesmos numeros de ontem) pro Sonnet
    citar tendencia real em vez de olhar o dia no vacuo."""
    from app.utils import hoje as _hoje
    base = dia or _hoje()
    inicio = datetime.combine(base, datetime.min.time())
    fim = inicio + timedelta(days=1)
    comparativo = _resumo_comparativo(
        _coletar_periodo(inicio - timedelta(days=1), inicio))
    extras = ({'comparativo_dia_anterior': comparativo}
              if comparativo else None)
    return auditar_periodo(inicio, fim, enviar=enviar, forcar_envio=True,
                           prompt_sistema=PROMPT_AUDITOR_RESUMO,
                           titulo='Resumo do dia',
                           dados_extras=extras)


def auditar_janela_pendente(*, enviar=True):
    """Audita a janela desde a ULTIMA execucao registrada ate agora. Anti-spam
    nativo: rodando 5x por dia, cada execucao olha so o que aconteceu desde a
    anterior — sem repetir o mesmo problema 5 vezes.

    Persiste `chatbot_auditor_ultima_exec` em AppConfig (sobrevive deploy)."""
    from app.models import AppConfig
    from app.utils import agora as _agora

    fim = _agora()
    ultima = AppConfig.get(CHAVE_ULTIMA_EXEC)
    if ultima:
        try:
            inicio = datetime.fromisoformat(ultima)
        except (TypeError, ValueError):
            inicio = fim - timedelta(hours=FALLBACK_JANELA_H)
    else:
        inicio = fim - timedelta(hours=FALLBACK_JANELA_H)

    if (fim - inicio).total_seconds() < 60:
        return {'pulou': 'janela muito curta (<1min desde a ultima exec)'}

    res = auditar_periodo(inicio, fim, enviar=enviar)
    # So avanca o ponteiro se a execucao chegou ao Sonnet de fato (ou foi
    # legitimamente "sem dados"). Erro de API NAO avanca — proxima tentativa
    # cobre a mesma janela e nao perde nada.
    if 'erro' not in res:
        try:
            from app.extensions import db
            AppConfig.set(CHAVE_ULTIMA_EXEC, fim.isoformat())
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.exception('auditor: salvar ultima_exec falhou')
    return res
