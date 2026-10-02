"""Etapas de produção de receita — fonte ÚNICA de parse/salvamento (14/07/2026).

Antes estes helpers viviam privados no blueprint de receitas (editor do admin).
Com a ficha de preparo do padeiro (/padeiro/fichas) passando a gravar as MESMAS
etapas, o parse do form e o replace no banco centralizam aqui — os dois editores
não podem divergir (regra do CLAUDE.md sobre implementações duplicadas).

Cada etapa: nome + duração (min) + tipo de trabalho (padeiro/máquina/descanso,
via RECURSO_MAP) + `descricao` (o QUE fazer na etapa — passo a passo que o
padeiro preenche; alimenta o drawer de mise en place e o fluxograma) +
`parametros` (dict JSON com os valores da ficha de produção — velocidades do
batimento, temperaturas, local da fermentação, dobras, forno/teto/lastro —,
importado da planilha pelo serviço `fichas_producao`; os editores só o
PRESERVAM, em `parametros[]`, e o fluxograma/TV o mostram pelo
`resumo_parametros`).
"""
import json

from app.extensions import db
from app.models import ReceitaEtapa

# Tipo de trabalho (select do form) -> (equipamento, ativa).
#  - padeiro: mão de obra (ocupa a pessoa).
#  - amassadeira/forno: máquina trabalha sozinha (1 de cada na padaria).
#  - camara_fria/descanso: fermentação/descanso passivo — não ocupa ninguém.
#  - congelar: passo FINAL (freezer) — produto pronto e congelado; não é
#    fermentação (não vira marcador de câmara fria nem antecipa a produção).
RECURSO_MAP = {
    'padeiro': (None, True),
    'amassadeira': ('amassadeira', True),
    'forno': ('forno', True),
    'camara_fria': ('camara_fria', False),
    'congelar': ('congelar', False),
    'descanso': (None, False),
}

DESCRICAO_MAX = 2000
PARAMETROS_MAX = 4000     # JSON por etapa no form (hidden) — ficha real ~300 B
DURACAO_MAX = 100000      # minutos; acima disso é dedo errado, não tempo real

# Esquema FECHADO dos parâmetros da ficha de produção (fichas_producao.py).
# Chave fora daqui é descartada; tipo errado idem — o rótulo e o Gantt nunca
# veem lixo, venha ele do hidden do editor ou de uma planilha torta.
_PARAM_NUM = ('velocidade_1_min', 'velocidade_2_min', 'temp_ambiente_c',
              'temp_liquido_c', 'temp_massa_final_c', 'temp_c', 'duracao_h',
              'forno_c', 'teto', 'lastro', 'alvo_interno_c')
_PARAM_TXT = ('ponto_massa', 'temp_liquido_txt', 'local', 'duracao_origem',
              'tempo_como_escrito', 'unidade_teto_lastro', 'origem')
_ALT_NUM = ('temp_c', 'tempo_min')
_ALT_TXT = ('local', 'tempo_como_escrito')
_TXT_MAX = 200
_NUM_MIN, _NUM_MAX = -1000.0, 1000000.0
# Chave -> como o rótulo a chama (pra dizer "leitura incerta: líquido").
_ROTULO_CHAVE = {
    'velocidade_1_min': 'V1', 'velocidade_2_min': 'V2', 'ponto_massa': 'ponto',
    'temp_ambiente_c': 'ambiente', 'temp_liquido_c': 'líquido',
    'temp_liquido_txt': 'líquido', 'temp_massa_final_c': 'massa ao fim',
    'local': 'local', 'temp_c': 'temperatura', 'duracao_h': 'duração',
    'dobras_min': 'dobras', 'tempo_como_escrito': 'tempo', 'tempo_min': 'tempo',
    'forno_c': 'forno', 'teto': 'teto', 'lastro': 'lastro',
    'unidade_teto_lastro': 'unidade', 'alvo_interno_c': 'alvo interno',
}


def rotulo_chaves(chaves):
    """Nomes legíveis das chaves de parâmetro (pra avisos e pro rótulo)."""
    return ', '.join(dict.fromkeys(_ROTULO_CHAVE.get(k, k) for k in chaves))


def duracao_valida(raw):
    """Minutos inteiros na régua [0, DURACAO_MAX] — a MESMA para o form do
    editor e para a planilha (duas réguas para a mesma coluna divergiam)."""
    try:
        n = int(float(raw or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(n, DURACAO_MAX))


def _num_ok(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float('inf'), float('-inf')):
        return None
    if not (_NUM_MIN <= v <= _NUM_MAX):
        return None
    return int(v) if float(v).is_integer() else float(v)


def _txt_ok(v):
    if not isinstance(v, str):
        return None
    t = v.strip()[:_TXT_MAX]
    return t or None


def validar_parametros(p):
    """Dict de parâmetros SANEADO (só chaves/tipos do esquema) ou None.
    Nunca levanta."""
    if not isinstance(p, dict) or not p:
        return None
    out = {}
    for k in _PARAM_NUM:
        v = _num_ok(p.get(k))
        if v is not None:
            out[k] = v
    for k in _PARAM_TXT:
        v = _txt_ok(p.get(k))
        if v is not None:
            out[k] = v
    dobras = p.get('dobras_min')
    if isinstance(dobras, (list, tuple)):
        ok = [_num_ok(d) for d in dobras[:10]]
        ok = [d for d in ok if d is not None and d >= 0]
        if ok:
            out['dobras_min'] = ok
    alts = p.get('alternativas')
    if isinstance(alts, (list, tuple)):
        lista = []
        for a in alts[:5]:
            if not isinstance(a, dict):
                continue
            alt = {}
            for k in _ALT_NUM:
                v = _num_ok(a.get(k))
                if v is not None:
                    alt[k] = v
            for k in _ALT_TXT:
                v = _txt_ok(a.get(k))
                if v is not None:
                    alt[k] = v
            if alt:
                lista.append(alt)
        if lista:
            out['alternativas'] = lista
    incerto = p.get('incerto')
    if isinstance(incerto, (list, tuple)):
        ok = [k for k in incerto if isinstance(k, str) and k in _ROTULO_CHAVE]
        if ok:
            out['incerto'] = list(dict.fromkeys(ok))
    return out or None


def _num_br(v):
    """Número no formato da padaria: 5,4 / 20 / 28,6."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        if float(v).is_integer():
            return str(int(v))
        return ('%.2f' % float(v)).rstrip('0').rstrip('.').replace('.', ',')
    return str(v)


def _dur_br(minutos):
    m = int(minutos or 0)
    if m and m % 60 == 0:
        return '%d h' % (m // 60)
    if m > 60:
        return ('%.1f h' % (m / 60.0)).replace('.', ',')
    return '%d min' % m


def _metodo_label(m):
    partes = []
    if m.get('local'):
        partes.append(str(m['local']))
    if m.get('temp_c') is not None:
        partes.append('%s °C' % _num_br(m['temp_c']))
    if m.get('tempo_min') is not None:
        partes.append(_dur_br(m['tempo_min']))
    elif m.get('tempo_como_escrito'):
        partes.append(str(m['tempo_como_escrito']))
    return ' · '.join(partes)


def resumo_parametros(p, duracao_min=None):
    """Rótulo legível dos parâmetros de uma etapa — FONTE ÚNICA do texto que
    o Gantt, a TV do padeiro, os editores e a sonda mostram. Chaves que não
    existem na ficha ficam de fora; chaves desconhecidas ou com tipo errado
    são ignoradas (o dict passa pelo esquema antes). `tempo_como_escrito` só
    aparece quando a duração não pôde ser lida. Nunca levanta: uma linha
    torta no banco não pode derrubar o Gantt nem a TV."""
    try:
        return _resumo_parametros(validar_parametros(p), duracao_min)
    except Exception:  # noqa: BLE001 — defesa final do render
        return ''


def _resumo_parametros(p, duracao_min=None):
    if not p:
        return ''
    partes = []
    if p.get('velocidade_1_min') is not None:
        partes.append('V1 %s min' % _num_br(p['velocidade_1_min']))
    if p.get('velocidade_2_min') is not None:
        partes.append('V2 %s min' % _num_br(p['velocidade_2_min']))
    if p.get('ponto_massa'):
        partes.append('ponto: %s' % p['ponto_massa'])
    if p.get('temp_liquido_c') is not None:
        partes.append('líquido %s °C' % _num_br(p['temp_liquido_c']))
    elif p.get('temp_liquido_txt'):
        partes.append('líquido: %s' % p['temp_liquido_txt'])
    if p.get('temp_ambiente_c') is not None:
        partes.append('ambiente %s °C' % _num_br(p['temp_ambiente_c']))
    if p.get('temp_massa_final_c') is not None:
        partes.append('massa ao fim %s °C' % _num_br(p['temp_massa_final_c']))
    if p.get('local'):
        partes.append(str(p['local']))
    if p.get('temp_c') is not None:
        partes.append('%s °C' % _num_br(p['temp_c']))
    if p.get('dobras_min'):
        # Intervalos consecutivos (a conta da duração soma os valores): a
        # ficha diz "30, 30, 25" = dobra após 30 min, outra 30 min depois…
        d = [_num_br(x) for x in p['dobras_min']]
        if len(d) == 1:
            partes.append('dobra após %s min' % d[0])
        else:
            partes.append('dobras a cada %s e %s min' % (', '.join(d[:-1]), d[-1]))
    if p.get('tempo_como_escrito') and not duracao_min:
        partes.append('na ficha: %s' % p['tempo_como_escrito'])
    for alt in p.get('alternativas') or []:
        lbl = _metodo_label(alt) if isinstance(alt, dict) else ''
        if lbl:
            partes.append('alternativa: %s' % lbl)
    if p.get('forno_c') is not None:
        partes.append('%s °C' % _num_br(p['forno_c']))
    if p.get('teto') is not None:
        partes.append('teto %s' % _num_br(p['teto']))
    if p.get('lastro') is not None:
        partes.append('lastro %s' % _num_br(p['lastro']))
    if p.get('unidade_teto_lastro'):
        partes.append('(%s)' % p['unidade_teto_lastro'])
    if p.get('alvo_interno_c') is not None:
        partes.append('alvo interno %s °C' % _num_br(p['alvo_interno_c']))
    if p.get('incerto'):
        partes.append('leitura incerta na ficha: %s' % rotulo_chaves(p['incerto']))
    return ' · '.join(partes)


def parametros_de_form(raw):
    """JSON do hidden `parametros[]` → dict SANEADO pelo esquema ou None
    (vazio/ilegível/não-objeto/sem chave conhecida = None; nunca levanta —
    o editor não pode travar por um hidden torto)."""
    raw = (raw or '').strip()
    if not raw or len(raw) > PARAMETROS_MAX:
        return None
    try:
        v = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return validar_parametros(v)


def recurso_de_etapa(e):
    """Valor do select 'tipo de trabalho' a partir da etapa salva."""
    if e.equipamento in ('amassadeira', 'forno', 'camara_fria', 'congelar'):
        return e.equipamento
    return 'padeiro' if e.ativa else 'descanso'


def parse_etapas_form(form):
    """Lê as linhas de etapa do form → lista de dicts
    {nome, duracao_min, equipamento, ativa, descricao}, pulando linhas sem
    nome. Usado pelo editor do admin E pela ficha do padeiro."""
    out = []
    nomes = form.getlist('nome[]')
    duracoes = form.getlist('duracao[]')
    recursos = form.getlist('recurso[]')
    descricoes = form.getlist('descricao[]')
    parametros = form.getlist('parametros[]')
    for i, nome in enumerate(nomes):
        nome = (nome or '').strip()
        if not nome:
            continue            # linha vazia = ignora
        dur_raw = duracoes[i] if i < len(duracoes) else 0
        dur_min = duracao_valida(dur_raw)
        recurso = recursos[i] if i < len(recursos) else ''
        equip, ativa = RECURSO_MAP.get(recurso, (None, True))
        desc = (descricoes[i] if i < len(descricoes) else '') or ''
        desc = desc.strip()[:DESCRICAO_MAX] or None
        params = parametros_de_form(parametros[i] if i < len(parametros) else '')
        out.append({'nome': nome[:80], 'duracao_min': dur_min,
                    'equipamento': equip, 'ativa': ativa, 'descricao': desc,
                    'parametros': params})
    return out


def set_etapas(receita_id, etapas):
    """Substitui as etapas de uma receita pela lista de dicts (ordem = ordem
    da lista). Não commita — o chamador fecha a transação."""
    ReceitaEtapa.query.filter_by(receita_id=receita_id).delete()
    for i, e in enumerate(etapas):
        db.session.add(ReceitaEtapa(
            receita_id=receita_id, ordem=i, nome=e['nome'],
            duracao_min=e.get('duracao_min') or 0,
            equipamento=e.get('equipamento'),
            ativa=bool(e.get('ativa', True)),
            descricao=e.get('descricao'),
            parametros=e.get('parametros') or None))


def listar(receita_id):
    """Etapas da receita na ordem cadastrada."""
    return (ReceitaEtapa.query.filter_by(receita_id=receita_id)
            .order_by(ReceitaEtapa.ordem).all())
