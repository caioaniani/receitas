"""Planejamento assistido por IA — Opus 4.8 (08/07/2026, pedido do dono).

Duas frentes, SEMPRE por cima dos motores deterministicos (a IA nao
inventa a conta — ela ajusta a sugestao com contexto e justifica):

1. PEDIDO LOJA→INDUSTRIA (`sugerir_pedido_loja_ia`): botao "Sugerir por
   IA" nas DUAS telas de pedidos da semana — /pedidos-semana/media
   (modo='media', base = grade da MEDIA, contraprova = VENDA+ESTOQUE)
   e /pedidos-semana/estoque (modo='venda', 11/07/2026 a pedido do
   dono: base = motor VENDA+ESTOQUE com o `seguranca_pct` da tela,
   contraprova = MEDIA; itens identificados por `item_key` porque a
   grade inclui MPs). Entrada = os dois motores + estoque + desperdicio
   recente + calendario (feriados/vesperas ficam a cargo do modelo —
   nao existe tabela de datas especiais). Saida = quantidades por dia
   POR PRODUTO com motivo, que o JS preenche na grade EDITAVEL. Nada e
   criado aqui: o pedido continua nascendo pelos botoes "Gerar" de
   sempre (aplicar_grade → rascunho pendente).

2. PRODUCAO (`analisar_producao_ia`): botao "Analisar por IA" na tela
   /telaindustriateste. Entrada = cronograma calculado + alertas de
   falta + pendencias do padeiro + carga de fornadas por dia. Saida =
   AJUSTES pontuais de celula (receita × data → qtd) com motivo +
   parecer. Aplicar (gesto humano) grava via `cronograma_edit.
   editar_celula` — vira OVERRIDE de rascunho, com todas as guardas
   (fornada especial etc.). ENVIAR ao padeiro segue 100% humano
   (decisao do dono: ordem enviada so muda por gesto explicito).

Modelo: Opus 4.8 por decisao do dono (08/07/2026) — excecao consciente a
padronizacao Sonnet. Override via env PLANEJAMENTO_IA_MODELO. Custo em
UsoIA (funcoes 'pedido_loja_ia' / 'producao_ia').
"""

import json
import logging
import os
import re
from datetime import timedelta

from app.utils import hoje

logger = logging.getLogger(__name__)

MODELO = os.environ.get('PLANEJAMENTO_IA_MODELO', 'claude-sonnet-5')

# Template unico pros dois modos da tela de pedidos (media | venda+estoque):
# muda qual motor e a BASE exibida na grade e o campo identificador que o
# modelo deve ecoar ({id_campo}) — o resto das regras e identico.
_SYSTEM_PEDIDO_TPL = """Voce e o planejador de reposicao de uma padaria
artesanal em Sao Paulo. Uma LOJA pede produtos para a INDUSTRIA por dia.

Recebera as datas do horizonte (com dia da semana) e, por produto:
- {id_campo}: identificador do produto — ECOE exatamente como recebeu;
- por_dia_media: sugestao do motor de MEDIA historica{base_media};
- por_dia_venda: sugestao do motor VENDA+ESTOQUE (ponto de reposicao,
  simula o estoque dia a dia){base_venda} — quando existir;
- estoque_atual da loja, media_semanal, lote (caixa), minimo;
- dias_travados: dias que JA TEM pedido (com o que foi pedido) — NAO
  proponha mudanca neles, devolva o valor ja pedido;
- desperdicio_7d: o que a loja jogou fora na ultima semana.

Proponha a quantidade POR DIA de cada produto. Regras:
- Pense no calendario brasileiro/paulistano: feriado, vespera e dia de
  semana mudam a demanda (voce conhece as datas — use-as).
- Desperdicio recente alto = nao inflar; falta recorrente = reforcar.
- Respeite a caixa (lote) quando pedir compensa; produto abaixo de 1
  caixa e decisao de negocio — explique no motivo.
- So liste produto em que voce DIVERGIU do motor da tela OU tem algo a
  dizer; produto omitido = manter a sugestao da tela.
- Motivo de 1 frase por produto listado; parecer geral curto no fim.

Responda APENAS JSON valido (sem markdown):
{{"itens": [{{"{id_campo}": {id_exemplo}, "por_dia": [0, 10, ...],
             "motivo": "..."}}],
 "parecer": "..."}}
`por_dia` com exatamente o numero de dias do horizonte, inteiros >= 0,
na ordem das datas."""

_BASE_TELA = ' (a BASE exibida na tela — e dela que voce diverge)'
_CONTRAPROVA = ', a contraprova'

_SYSTEM_PEDIDO = _SYSTEM_PEDIDO_TPL.format(
    id_campo='receita_id', id_exemplo='1',
    base_media=_BASE_TELA, base_venda=_CONTRAPROVA)

_SYSTEM_PEDIDO_VENDA = _SYSTEM_PEDIDO_TPL.format(
    id_campo='item_key', id_exemplo='"123"',
    base_media=_CONTRAPROVA, base_venda=_BASE_TELA)

_SYSTEM_PRODUCAO = """Voce e o analista de PCP de uma padaria artesanal
em Sao Paulo. A industria produz para as lojas e clientes B2B.

Recebera o CRONOGRAMA calculado pelo sistema (por receita: quantidade por
dia, fornadas, total, estoque, se ja foi editado a mao), os alertas de
falta (entrega firme descoberta), as pendencias do padeiro (producao
agendada e vencida) e a carga total de fornadas por dia.

Proponha AJUSTES pontuais: (receita, data, quantidade nova) com motivo de
1 frase cada. Regras:
- So proponha celulas que MUDAM (nao repita o valor atual).
- Pense no calendario brasileiro/paulistano (feriado/vespera muda venda).
- Alerta de falta = prioridade maxima (antecipar/aumentar producao).
- Pendencia VENCIDA do padeiro = demanda que talvez precise reforco hoje.
- Dia com pico de fornadas estourado = redistribuir para dias vizinhos.
- `dias_fechados` estao TRANCADOS pelo gestor — nao proponha ajuste
  neles (seriam recusados).
- Linha com retorno=true e feita SO de sobras devolvidas — NUNCA
  proponha producao para ela.
- Termine com um parecer geral curto (riscos e o porque dos ajustes).

Responda APENAS JSON valido (sem markdown):
{"ajustes": [{"receita_id": 1, "data": "2026-07-09", "qtd": 40,
              "motivo": "..."}],
 "parecer": "..."}"""


def _chamar_opus(system, payload_texto, funcao):
    """Chamada padrao: Opus 4.8, timeout, custo em UsoIA, parse de JSON.
    Devolve (dados, None) ou (None, mensagem_de_erro)."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None, 'ANTHROPIC_API_KEY nao configurada'
    try:
        import anthropic
    except ImportError:
        return None, 'biblioteca anthropic nao instalada'
    client = anthropic.Anthropic(api_key=api_key, timeout=120,
                                 max_retries=1)
    try:
        from app.services import uso_ia
        response = client.messages.create(
            model=MODELO, max_tokens=4000, system=system,
            thinking={'type': 'disabled'},  # proposta em JSON, sem tools
            messages=[{'role': 'user', 'content': payload_texto}])
        uso_ia.registrar(funcao, MODELO, getattr(response, 'usage', None))
        if getattr(response, 'stop_reason', None) == 'max_tokens':
            # Grid grande pode estourar a saida — refaz UMA vez com teto
            # maior (padrao do retry de truncamento do chatbot, P2 02/07);
            # sem isso o JSON cortado viraria "resposta invalida" eterno.
            response = client.messages.create(
                model=MODELO, max_tokens=8000, system=system,
                messages=[{'role': 'user', 'content': payload_texto}])
            uso_ia.registrar(funcao, MODELO,
                             getattr(response, 'usage', None))
        bruto = ''.join(b.text for b in response.content
                        if getattr(b, 'type', '') == 'text')
        bruto = re.sub(r'^```(?:json)?\s*|\s*```$', '', bruto.strip(),
                       flags=re.MULTILINE)
        return json.loads(bruto), None
    except json.JSONDecodeError:
        logger.warning('%s: resposta nao-JSON do modelo', funcao)
        return None, 'a IA devolveu resposta invalida — tente de novo'
    except Exception as exc:
        # Detalhe cru (SDK/HTTP) so no log — a UI recebe algo generico.
        logger.warning('%s: falha na chamada: %s', funcao, exc)
        return None, 'a IA não respondeu — tente de novo em instantes'


def _desperdicio_recente(loja_id, dias=7):
    """[{nome, quantidade}] do que a loja descartou nos ultimos N dias."""
    from app.models import Desperdicio
    corte = hoje() - timedelta(days=dias)
    out = {}
    q = (Desperdicio.query
         .filter(Desperdicio.loja_id == loja_id,
                 Desperdicio.criado_em >= corte))
    for d in q.all():
        nome = None
        if d.receita_id and d.receita:
            nome = d.receita.nome
        elif d.produto_id and d.produto:
            nome = d.produto.nome
        elif d.materia_prima_id and d.materia_prima:
            nome = d.materia_prima.nome
        if nome:
            out[nome] = out.get(nome, 0) + float(d.quantidade or 0)
    return [{'nome': n, 'quantidade': q} for n, q in sorted(out.items())]


def sugerir_pedido_loja_ia(loja_id, *, horizonte_dias=7, janela_semanas=6,
                           inicio_offset_dias=1, modo='media',
                           seguranca_pct=0):
    """Proposta da IA para o pedido de UMA loja, no formato da grade da
    tela de pedidos da semana. `modo` escolhe a BASE (o motor da tela que
    chamou): 'media' (/pedidos-semana/media, itens por `receita_id`) ou
    'venda' (/pedidos-semana/estoque, itens por `item_key` — inclui MPs;
    `seguranca_pct` e o colchao da tela). O outro motor entra como
    contraprova no contexto. Devolve
    {'itens': [{receita_id|item_key, por_dia, motivo, mudou, aviso}],
     'parecer', 'dias', 'modelo_usado'} ou {'erro': ...}. NAO grava nada."""
    from app.services.previsao_producao import (
        media_semanal_pedidos,
        sugerir_pedidos_por_venda,
    )
    media = media_semanal_pedidos(horizonte_dias=horizonte_dias,
                                  janela_semanas=janela_semanas,
                                  inicio_offset_dias=inicio_offset_dias)
    venda = sugerir_pedidos_por_venda(horizonte_dias=horizonte_dias,
                                      janela_semanas=janela_semanas,
                                      inicio_offset_dias=inicio_offset_dias,
                                      seguranca_pct=seguranca_pct)
    if modo == 'venda':
        grade, contra_grade = venda, media
        id_campo, system = 'item_key', _SYSTEM_PEDIDO_VENDA
    else:
        modo = 'media'
        grade, contra_grade = media, venda
        id_campo, system = 'receita_id', _SYSTEM_PEDIDO

    loja = next((lj for lj in grade['lojas']
                 if lj['loja_id'] == int(loja_id)), None)
    if loja is None:
        return {'erro': 'loja sem sugestao na grade (sem historico?)'}

    def _chave(p):
        # 'media': linhas so de receita (int); 'venda': item_key str
        # ('<receita_id>' ou 'mp:<id>').
        return p['item_key'] if id_campo == 'item_key' else p['receita_id']

    # Contraprova do OUTRO motor, casada por receita_id (MP so existe na
    # grade de venda — fica sem contraprova, o que e verdade).
    contra_loja = next((lj for lj in contra_grade['lojas']
                        if lj['loja_id'] == int(loja_id)), None)
    contra_por_rid = {}
    if contra_loja:
        contra_por_rid = {p['receita_id']: p['por_dia']
                          for p in contra_loja['produtos']
                          if p.get('receita_id') and not p.get('eh_mp')}

    dias = grade['dias']
    produtos_ctx = []
    for p in loja['produtos']:
        travados = [
            {'data': dias[i]['data'], 'ja_pedido': p['ja_pedido'][i]}
            for i, d in enumerate(dias)
            if d['data'] in loja['ja_tem']
        ]
        contra = contra_por_rid.get(p.get('receita_id'))
        produtos_ctx.append({
            id_campo: _chave(p),
            'nome': p['nome'],
            'por_dia_media': p['por_dia'] if modo == 'media' else contra,
            'por_dia_venda': p['por_dia'] if modo == 'venda' else contra,
            'estoque_atual': p['estoque_atual'],
            'media_semanal': p['media_semanal'],
            'lote': p['lote'],
            'minimo': p['minimo'],
            'dias_travados': travados,
        })
    payload = {
        'hoje': grade['hoje'],
        'loja': loja['loja_nome'],
        'dias': dias,
        'produtos': produtos_ctx,
        'desperdicio_7d': _desperdicio_recente(loja['loja_id']),
    }
    dados, erro = _chamar_opus(
        system, json.dumps(payload, ensure_ascii=False),
        'pedido_loja_ia')
    if erro:
        return {'erro': erro}

    # Sanitiza contra a grade REAL: so itens da loja, por_dia do
    # tamanho certo (inteiros >= 0), dia travado devolve o ja pedido.
    por_chave = {_chave(p): p for p in loja['produtos']}
    n = len(dias)
    itens_ok = []
    for it in (dados.get('itens') or []):
        if id_campo == 'item_key':
            chave = str(it.get('item_key') or '')
        else:
            try:
                chave = int(it.get('receita_id'))
            except (TypeError, ValueError):
                continue
        base = por_chave.get(chave)
        if base is None:
            continue
        bruto = it.get('por_dia') or []
        por_dia = []
        livres = []                          # indices dos dias sem pedido
        for i in range(n):
            try:
                v = max(0, int(bruto[i]))
            except (IndexError, TypeError, ValueError):
                v = base['por_dia'][i]
            if dias[i]['data'] in loja['ja_tem']:
                v = base['ja_pedido'][i]     # travado: nao mexe
            else:
                livres.append(i)
            por_dia.append(v)
        # `mudou` so olha os dias LIVRES (travado sempre difere do motor,
        # que os zera — comparar tudo marcaria falso positivo).
        mudou = any(por_dia[i] != base['por_dia'][i] for i in livres)
        aviso = None
        total_motor = sum(base['por_dia'][i] for i in livres)
        total_ia = sum(por_dia[i] for i in livres)
        if total_motor and total_ia > 3 * total_motor:
            aviso = (f'{base["nome"]}: proposta {total_ia} un e mais '
                     f'de 3x a sugestao do motor ({total_motor}) — confira')
        elif not total_motor and total_ia > 0:
            aviso = (f'{base["nome"]}: o motor nao sugere nada e a IA '
                     f'propoe {total_ia} un — confira')
        itens_ok.append({id_campo: chave, 'nome': base['nome'],
                         'por_dia': por_dia,
                         'motivo': (it.get('motivo') or '').strip(),
                         'mudou': mudou, 'aviso': aviso})
    return {'itens': itens_ok,
            'parecer': (dados.get('parecer') or '').strip(),
            'dias': [d['data'] for d in dias],
            'modelo_usado': MODELO}


def analisar_producao_ia(*, horizonte_dias=7, janela_semanas=6,
                         inicio_offset_dias=0, equilibrar=False,
                         motor='pedidos'):
    """Proposta da IA para o cronograma da industria: lista de AJUSTES de
    celula (receita × data → qtd) com motivo + parecer. Devolve
    {'ajustes': [{receita_id, nome, data, qtd, atual, motivo}], 'parecer',
     'modelo_usado'} ou {'erro': ...}. NAO grava nada — aplicar e gesto
    humano na tela (vira override via editar_celula)."""
    from app.services.previsao_producao import cronograma_producao
    from app.services.producao_pendente import pendencias_por_receita

    crono = cronograma_producao(horizonte_dias=horizonte_dias,
                                janela_semanas=janela_semanas,
                                inicio_offset_dias=inicio_offset_dias,
                                equilibrar=equilibrar, motor=motor)
    pend = pendencias_por_receita()
    linhas_ctx = []
    for r in crono['receitas']:
        # Fora da proposta: linha de retorno (so de sobras devolvidas) e
        # INSUMO (massa etc. — o MRP deriva dos finais; ajustar insumo a
        # mao brigaria com a explosao).
        if r.get('retorno') or r.get('insumo'):
            continue
        pd = pend.get(r['receita_id']) or {}
        linhas_ctx.append({
            'receita_id': r['receita_id'],
            'nome': r['nome'],
            'por_dia': [{'data': c['data'], 'qtd': c['qtd']}
                        for c in r['por_dia']],
            'total': r.get('total'),
            'em_estoque': r.get('em_estoque'),
            'editado_a_mao': bool(r.get('editado')),
            'pendencia_agendada': pd.get('agendado') or 0,
            'pendencia_vencida': pd.get('vencido') or 0,
        })
    # Carga por dia (mesma conta do rodape da tela): fornadas de todas as
    # linhas — e o que o "equilibrar" tenta nivelar.
    fornadas_por_dia = []
    for i, dia in enumerate(crono['dias']):
        forn = 0
        for rr in crono['receitas']:
            c = rr['por_dia'][i]
            if c.get('fornadas'):
                forn += c['fornadas']
        fornadas_por_dia.append({'data': dia['data'],
                                 'fornadas': round(forn, 1)})
    from app.services.cronograma_edit import dias_fechados
    payload = {
        'hoje': crono.get('hoje'),
        'dias': crono.get('dias'),
        'linhas': linhas_ctx,
        'alertas_falta': crono.get('alertas_falta') or [],
        'fornadas_por_dia': fornadas_por_dia,
        'dias_fechados': sorted(d.isoformat() if hasattr(d, 'isoformat')
                                else str(d) for d in dias_fechados()),
    }
    dados, erro = _chamar_opus(
        _SYSTEM_PRODUCAO, json.dumps(payload, ensure_ascii=False),
        'producao_ia')
    if erro:
        return {'erro': erro}

    # Sanitiza contra o cronograma REAL: receita/data existentes, qtd
    # inteira >= 0, e descarta ajuste igual ao valor atual.
    por_rid = {r['receita_id']: r for r in crono['receitas']
               if not r.get('retorno') and not r.get('insumo')}
    ajustes_ok = []
    for a in (dados.get('ajustes') or []):
        try:
            rid = int(a.get('receita_id'))
            qtd = max(0, int(a.get('qtd')))
        except (TypeError, ValueError):
            continue
        linha = por_rid.get(rid)
        if linha is None:
            continue
        data = str(a.get('data') or '')
        cel = next((c for c in linha['por_dia'] if c['data'] == data), None)
        if cel is None or int(cel['qtd'] or 0) == qtd:
            continue
        ajustes_ok.append({'receita_id': rid, 'nome': linha['nome'],
                           'data': data, 'qtd': qtd,
                           'atual': int(cel['qtd'] or 0),
                           'motivo': (a.get('motivo') or '').strip()})
    return {'ajustes': ajustes_ok,
            'parecer': (dados.get('parecer') or '').strip(),
            'modelo_usado': MODELO}
