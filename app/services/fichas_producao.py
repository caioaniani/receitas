"""Fichas de produção da padaria (planilha do dono, 02/10/2026) → etapas do
fluxograma/Gantt.

O dono transcreveu as fichas manuscritas de produção numa planilha com quatro
abas — **1. Batimento**, **2. Fermentação** (a "CAIXA"), **3. Fermentação
final** e **4. Forno** — e pediu: "cadastrar e usar essas infos no gantt/
fluxograma de produção; apagar todos os registros que o gantt usa hoje e
trocar por esses dessa planilha".

Este módulo é a fonte única desse gesto:

- `ler_planilha(origem)` lê o xlsx (caminho ou bytes) e devolve, por receita
  da planilha, a lista de etapas no formato de `etapas_receita.set_etapas`
  (nome, duracao_min, equipamento, ativa, descricao) mais `parametros` — o
  JSON estruturado da etapa (velocidades, temperaturas, local, dobras, forno,
  teto/lastro...). Célula em branco (ou com traço) NÃO vira parâmetro nem
  tempo inventado: o que a ficha não diz fica fora e aparece em `avisos`.
  Célula marcada em AMARELO na planilha ("leitura incerta" na legenda do
  dono) entra com o valor, mas a chave vai em `parametros['incerto']` e o
  rótulo diz que a leitura é incerta.
- `casar(fichas)` liga cada nome da planilha a UMA receita não arquivada por
  nome normalizado EXATO ("Sourdough Tradicional" nunca casa "Mini Sourdough
  Tradicional"); ausente ou ambígua fica de fora, listada. A mesma
  normalização agrupa as linhas da planilha: grafias diferentes do mesmo
  nome entre as abas viram UMA ficha (com aviso).
- `aplicar(fichas, casamento, ...)` SUBSTITUI todas as `ReceitaEtapa` do
  sistema (de TODAS as receitas, como o dono pediu), depois de guardar um
  backup JSON em `AppConfig` (o passo a passo escrito pelo padeiro nunca se
  perde sem volta), reescreve o `processo` congelado das ordens ainda abertas
  (o Gantt lê o snapshot de batelada, não a ficha viva) e grava o marcador
  `fichas_producao_import`. Recusa (ValueError, nada apagado) quando nenhuma
  receita da planilha casa com o cadastro. Em Postgres serializa o gesto com
  um advisory lock transacional. Não commita — o chamador (rota ou seed de
  startup) fecha a transação.

Ordem das etapas geradas por receita: Batimento (amassadeira, ativa, V1+V2)
→ Fermentação em caixa (passiva; dobras) → Mesa: do corte à modelagem
(passiva) → Fermentação final (passiva; câmara fria ou ambiente; a segunda
forma da ficha vira `alternativas`) → Forno (forno, ativa). Receita sem dado
numa aba não ganha a etapa daquela aba.
"""
import hashlib
import io
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app.extensions import db
from app.models import AppConfig, Receita, ReceitaEtapa
from app.services.etapas_receita import (
    duracao_valida,
    rotulo_chaves,
    validar_parametros,
)
from app.utils import agora, hoje

ARQUIVO_SEED = Path(__file__).resolve().parents[1] / 'seeds_data' / 'fichas_producao_2026_10.xlsx'
MARCADOR_IMPORT = 'fichas_producao_import'
PREFIXO_BACKUP = 'fichas_producao_backup_'
# Advisory lock transacional (Postgres) que serializa dois "aplicar"
# concorrentes — o wipe global + inserts não pode cruzar com outro igual.
LOCK_KEY_APLICAR = 7768
# Duração acima disso (minutos) é dedo errado na ficha, não tempo real.
DURACAO_MAX_MIN = 100000
# Preenchimento AMARELO da planilha do dono = "leitura incerta" (legenda).
_FILL_INCERTO = 'FFF2CC'
# Traço na célula = "não se aplica" na ficha (ex.: Pão Francês sem câmara fria).
_TRACOS = {'-', '–', '—', '‒', '―'}

# Nomes canônicos das etapas geradas (o diário de produção sugere os nomes
# da ficha; manter estáveis).
NOME_BATIMENTO = 'Batimento'
NOME_CAIXA = 'Fermentação em caixa'
NOME_MESA = 'Mesa: do corte à modelagem'
NOME_FINAL = 'Fermentação final'
NOME_FORNO = 'Forno'

_LOCAL_LABEL = {'cf': 'Câmara fria', 'camara fria': 'Câmara fria',
                'a': 'Ambiente', 'ambiente': 'Ambiente'}


class PlanilhaInvalida(ValueError):
    """A planilha não tem a estrutura esperada (abas/cabeçalhos)."""


# ── normalização ─────────────────────────────────────────────────────────────

def normalizar_nome(texto):
    """Chave de comparação: sem acento, caixa baixa, espaços colapsados."""
    s = unicodedata.normalize('NFKD', str(texto or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.casefold().split())


def _vazio(v):
    if v is None:
        return True
    if isinstance(v, str):
        t = v.strip()
        return not t or t in _TRACOS
    return False


def _numero(v):
    """Número da célula (int quando inteiro), ou None se não for número."""
    if isinstance(v, bool) or _vazio(v):
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:        # NaN
            return None
        return int(v) if float(v).is_integer() else float(v)
    if isinstance(v, str):
        t = v.strip().replace(',', '.')
        if re.fullmatch(r'-?\d+(\.\d+)?', t):
            f = float(t)
            return int(f) if f.is_integer() else f
    return None


def _texto(v):
    if _vazio(v):
        return None
    return str(v).strip()


def _minutos(v):
    """Minutos a partir de número, timedelta, datetime.time, datetime (hora
    de planilha) ou texto ('1:30', '12 h', '43 min', '24h'). None quando não
    dá pra ler."""
    if _vazio(v):
        return None
    if isinstance(v, timedelta):
        return int(round(v.total_seconds() / 60))
    if isinstance(v, datetime):
        # Hora ≥ 24 h numa célula h:mm vira datetime na época do Excel. O
        # openpyxl (`from_excel`, época Windows) reproduz o bug do ano
        # bissexto de 1900: serial < 60 ganha +1 dia, então 26:00 (serial
        # 1,083) volta como 1900-01-01 02:00 — base 1899-12-31 até
        # 1900-02-28; de 1900-03-01 em diante a base é 1899-12-30.
        if v.year <= 1900:
            base = (datetime(1899, 12, 31) if v < datetime(1900, 3, 1)
                    else datetime(1899, 12, 30))
            m = int(round((v - base).total_seconds() / 60))
            return m if m >= 0 else None
        return v.hour * 60 + v.minute
    if hasattr(v, 'hour') and hasattr(v, 'minute'):     # datetime.time
        return int(v.hour) * 60 + int(v.minute)
    n = _numero(v)
    if n is not None:
        return int(round(n))
    t = str(v).strip().lower().replace(',', '.')
    m = re.fullmatch(r'(\d{1,3}):(\d{2})(?::\d{2})?', t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(h|hora|horas|hr|hrs)', t)
    if m:
        return int(round(float(m.group(1)) * 60))
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(min|minutos|m)', t)
    if m:
        return int(round(float(m.group(1))))
    return None


def _horas_min(v):
    """Coluna em HORAS ('Duração (h)', 'MESA h:mm'): número puro = horas;
    o resto passa pelo `_minutos` (hora da planilha, '1:30', '12 h')."""
    if _vazio(v):
        return None
    n = _numero(v)
    if n is not None:
        return int(round(float(n) * 60))
    return _minutos(v)


def _local(v):
    t = _texto(v)
    if t is None:
        return None
    return _LOCAL_LABEL.get(normalizar_nome(t), t)


def _como_escrito(v):
    if isinstance(v, timedelta):
        total = int(round(v.total_seconds() / 60))
        return '%d:%02d' % divmod(total, 60)
    if isinstance(v, datetime):
        m = _minutos(v) or 0
        return '%d:%02d' % divmod(m, 60)
    if hasattr(v, 'hour') and hasattr(v, 'minute'):
        return '%d:%02d' % (v.hour, v.minute)
    return _texto(v)


def _incerto(cell):
    """Célula com o preenchimento amarelo da legenda ("leitura incerta")."""
    try:
        fill = cell.fill
        if not fill or not fill.fill_type:
            return False
        rgb = getattr(fill.fgColor, 'rgb', None)
        return isinstance(rgb, str) and rgb.upper().endswith(_FILL_INCERTO)
    except Exception:  # noqa: BLE001 — estilo é bônus, nunca derruba a leitura
        return False


# ── leitura do xlsx ───────────────────────────────────────────────────────────

def _carregar(origem):
    import openpyxl
    if isinstance(origem, (bytes, bytearray)):
        fonte = io.BytesIO(bytes(origem))
    elif hasattr(origem, 'read'):
        fonte = io.BytesIO(origem.read())
    else:
        fonte = str(origem)
    try:
        # Sem read_only: precisamos do preenchimento das células (amarelo =
        # leitura incerta). A planilha é pequena (4 abas, ~20 linhas cada).
        return openpyxl.load_workbook(fonte, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl levanta vários tipos
        raise PlanilhaInvalida(f'Não foi possível abrir a planilha: {exc}') from exc


def _aba(wb, *termos, excluir=()):
    for ws in wb.worksheets:
        t = normalizar_nome(ws.title)
        if all(x in t for x in termos) and not any(x in t for x in excluir):
            return ws
    raise PlanilhaInvalida('Aba "%s" não encontrada na planilha.' % ' '.join(termos))


def _linhas(ws):
    """(cabecalho_normalizado->indice, [(numero_linha, nome, celulas)]) das
    linhas de dados: do cabeçalho ("Receita" na 1ª coluna) até a legenda ou
    o fim. `celulas` são objetos Cell (valor + preenchimento)."""
    cab = None
    saida = []
    for row in ws.iter_rows():
        if not row:
            continue
        n = row[0].row
        primeiro = _texto(row[0].value)
        if cab is None:
            if primeiro and normalizar_nome(primeiro) == 'receita':
                cab = {}
                for i, c in enumerate(row):
                    if not _vazio(c.value):
                        cab.setdefault(normalizar_nome(c.value), i)
            continue
        if primeiro is None:
            continue
        if normalizar_nome(primeiro).startswith('legenda'):
            break
        saida.append((n, primeiro, row))
    if cab is None:
        raise PlanilhaInvalida(
            f'Aba "{ws.title}": cabeçalho "Receita" não encontrado.')
    return cab, saida


def _coluna(cab, *termos, excluir=()):
    """Índice da coluna cujo cabeçalho normalizado contém TODOS os termos e
    nenhum de `excluir`; com mais de um candidato vale o MAIS ESPECÍFICO
    (cabeçalho mais curto) — 'teto (valor)' ganha de 'unidade teto / lastro'
    em qualquer ordem de colunas."""
    candidatos = [(len(chave), idx) for chave, idx in cab.items()
                  if all(t in chave for t in termos)
                  and not any(x in chave for x in excluir)]
    if not candidatos:
        return None
    return min(candidatos)[1]


def _exigir(ws, cab, *termos, excluir=()):
    idx = _coluna(cab, *termos, excluir=excluir)
    if idx is None:
        raise PlanilhaInvalida(
            f'Aba "{ws.title}": coluna "{" ".join(termos)}" não encontrada.')
    return idx


def _cel(row, idx):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _val(row, idx):
    c = _cel(row, idx)
    return None if c is None else c.value


def _origem(ws, n):
    return f'{ws.title} · linha {n}'


def _limpar(d):
    return {k: v for k, v in d.items() if v is not None and v != [] and v != ''}


def _marcar_incertos(params, row, chaves_colunas):
    """Acrescenta `incerto` = chaves cujo valor veio de célula amarela (uma
    chave pode cobrir várias colunas — as três dobras — e entra uma vez)."""
    incertos = [chave for chave, idx in chaves_colunas
                if chave in params and idx is not None
                and _incerto(_cel(row, idx))]
    if incertos:
        params['incerto'] = list(dict.fromkeys(incertos))
    return params


def _amarelas_vazias(row, colunas, nome, origem, avisos):
    """Célula amarela SEM valor: a legenda diz "leitura incerta", mas não há
    o que ler — não vira parâmetro; avisa pra conferir na ficha."""
    rotulos = [rotulo for rotulo, idx in colunas
               if idx is not None and _vazio(_val(row, idx))
               and _incerto(_cel(row, idx))]
    if rotulos:
        avisos.append(f'{nome}: célula amarela em branco na ficha '
                      f'({", ".join(rotulos)}; {origem}) — leitura incerta sem '
                      'valor; confira a ficha.')


def _duracao_lida(valor, nome, rotulo, origem, avisos):
    """Duração em minutos dentro da régua; fora dela vira 0 + aviso (nunca
    um número inventado nem um estouro de coluna na hora de aplicar)."""
    if valor is None:
        return None
    if valor < 0 or valor > DURACAO_MAX_MIN:
        avisos.append(f'{nome}: {rotulo} fora da faixa ({valor} min) — '
                      f'ignorado ({origem}).')
        return 0
    return int(valor)


def ler_planilha(origem):
    """Lê o xlsx e devolve
    {'fichas': {nome_planilha: [etapa, ...]}, 'avisos': [...],
     'ordem': [nomes na ordem em que aparecem]}.
    Cada etapa: nome, duracao_min, equipamento, ativa, descricao=None,
    parametros (dict, sempre com 'origem'). Nomes com grafia diferente entre
    as abas são a MESMA ficha (chave normalizada; vale a 1ª grafia)."""
    wb = _carregar(origem)
    try:
        ws_bat = _aba(wb, 'batimento')
        ws_caixa = _aba(wb, 'fermentacao', excluir=('final',))
        ws_final = _aba(wb, 'fermentacao', 'final')
        ws_forno = _aba(wb, 'forno')
        avisos = []
        partes = {}      # chave normalizada -> partes da ficha
        ordem = []       # chaves normalizadas na ordem de APARIÇÃO (qualquer
        #                  aba, mesmo linha vazia): a prévia segue a planilha
        grafias = {}     # chave -> grafia exibida (a primeira)

        def _visto(nome):
            chave = normalizar_nome(nome)
            if chave not in grafias:
                grafias[chave] = nome
                ordem.append(chave)
            return chave

        def _slot(nome, ws, n):
            chave = _visto(nome)
            if chave not in partes:
                partes[chave] = {'batimento': [], 'caixa': [], 'final': [],
                                 'forno': []}
            if nome != grafias[chave] and not any(
                    a.startswith(f'"{nome}"') for a in avisos):
                avisos.append(f'"{nome}" ({_origem(ws, n)}) foi lido como a '
                              f'mesma receita de "{grafias[chave]}" '
                              '(grafia diferente entre as abas).')
            return partes[chave]

        def _repetida(slot, parte, nome, ws, n):
            if slot[parte]:
                avisos.append(f'{nome}: linha repetida em "{ws.title}" '
                              f'({_origem(ws, n)}) — só a primeira vale.')
                return True
            return False

        # 1. Batimento
        cab, linhas = _linhas(ws_bat)
        c_v1 = _exigir(ws_bat, cab, 'velocidade 1')
        c_v2 = _exigir(ws_bat, cab, 'velocidade 2')
        c_ponto = _coluna(cab, 'ponto')
        c_amb = _coluna(cab, 'ambiente')
        c_liq = _coluna(cab, 'liquido')
        c_fim = _coluna(cab, 'apos')
        for n, nome, row in linhas:
            _visto(nome)
            _amarelas_vazias(row, [('V1', c_v1), ('V2', c_v2), ('ponto', c_ponto),
                                   ('ambiente', c_amb), ('líquido', c_liq),
                                   ('massa ao fim', c_fim)],
                             nome, _origem(ws_bat, n), avisos)
            v1 = _numero(_val(row, c_v1))
            v2 = _numero(_val(row, c_v2))
            ponto = _texto(_val(row, c_ponto))
            t_amb = _numero(_val(row, c_amb))
            liq_raw = _val(row, c_liq)
            t_liq = _numero(liq_raw)
            t_liq_txt = None if t_liq is not None else _texto(liq_raw)
            t_fim = _numero(_val(row, c_fim))
            if all(x is None for x in (v1, v2, ponto, t_amb, t_liq,
                                       t_liq_txt, t_fim)):
                continue
            slot = _slot(nome, ws_bat, n)
            if _repetida(slot, 'batimento', nome, ws_bat, n):
                continue
            if v1 is None and v2 is None:
                avisos.append(f'{nome}: batimento sem tempo de velocidade na '
                              f'ficha ({_origem(ws_bat, n)}).')
            dur = _duracao_lida(int(round((v1 or 0) + (v2 or 0))), nome,
                                'tempo de batimento', _origem(ws_bat, n), avisos)
            params = _limpar({
                'velocidade_1_min': v1, 'velocidade_2_min': v2,
                'ponto_massa': ponto, 'temp_ambiente_c': t_amb,
                'temp_liquido_c': t_liq, 'temp_liquido_txt': t_liq_txt,
                'temp_massa_final_c': t_fim, 'origem': _origem(ws_bat, n)})
            _marcar_incertos(params, row, [
                ('velocidade_1_min', c_v1), ('velocidade_2_min', c_v2),
                ('ponto_massa', c_ponto), ('temp_ambiente_c', c_amb),
                ('temp_liquido_c', c_liq), ('temp_liquido_txt', c_liq),
                ('temp_massa_final_c', c_fim)])
            slot['batimento'].append({
                'nome': NOME_BATIMENTO, 'duracao_min': dur or 0,
                'equipamento': 'amassadeira', 'ativa': True, 'descricao': None,
                'parametros': params})

        # 2. Fermentação em caixa (+ mesa)
        cab, linhas = _linhas(ws_caixa)
        c_local = _exigir(ws_caixa, cab, 'local')
        c_temp = _exigir(ws_caixa, cab, 'temperatura')
        c_dur = _coluna(cab, 'duracao')
        c_dobras = [_coluna(cab, f'dobra {i}') for i in (1, 2, 3)]
        if c_dobras[0] is None:
            raise PlanilhaInvalida(f'Aba "{ws_caixa.title}": coluna "dobra 1" '
                                   'não encontrada.')
        c_mesa = _coluna(cab, 'mesa')
        for n, nome, row in linhas:
            _visto(nome)
            _amarelas_vazias(row, [('local', c_local), ('temperatura', c_temp),
                                   ('duração', c_dur)]
                             + [(f'dobra {i + 1}', c)
                                for i, c in enumerate(c_dobras)]
                             + [('mesa', c_mesa)],
                             nome, _origem(ws_caixa, n), avisos)
            local = _local(_val(row, c_local))
            temp = _numero(_val(row, c_temp))
            dur_raw = _val(row, c_dur)
            dur_h_min = _horas_min(dur_raw)
            dobras = [_numero(_val(row, c)) for c in c_dobras if c is not None]
            dobras = [int(round(d)) for d in dobras if d is not None]
            mesa_raw = _val(row, c_mesa)
            mesa_min = _horas_min(mesa_raw)
            if (all(x is None for x in (local, temp, dur_h_min)) and not dobras
                    and mesa_min is None and _vazio(dur_raw) and _vazio(mesa_raw)):
                continue
            slot = _slot(nome, ws_caixa, n)
            # "Local" sozinho não é dado: a etapa de caixa só nasce com
            # temperatura, duração ou dobras (linha só com MESA = só a Mesa).
            tem_caixa = (temp is not None or dur_h_min is not None or dobras
                         or not _vazio(dur_raw))
            if not tem_caixa and local is not None:
                avisos.append(f'{nome}: fermentação em caixa só com o local '
                              f'("{local}") na ficha, sem temperatura, duração '
                              f'nem dobras — etapa não criada '
                              f'({_origem(ws_caixa, n)}).')
            if tem_caixa and _repetida(slot, 'caixa', nome, ws_caixa, n):
                continue
            if tem_caixa:
                if dur_h_min is not None:
                    dur = dur_h_min
                    duracao_origem = 'ficha'
                else:
                    if not _vazio(dur_raw):
                        avisos.append(f'{nome}: "Duração (h)" ilegível '
                                      f'("{dur_raw}"; {_origem(ws_caixa, n)}).')
                    if dobras:
                        dur = sum(dobras)                 # até a última dobra
                        duracao_origem = 'ate_ultima_dobra'
                        avisos.append(f'{nome}: a ficha não diz a duração total '
                                      f'da fermentação em caixa — o fluxograma '
                                      f'usa o tempo até a última dobra ({dur} '
                                      f'min; {_origem(ws_caixa, n)}).')
                    else:
                        dur = 0
                        duracao_origem = 'ausente'
                        avisos.append(f'{nome}: fermentação em caixa sem tempo '
                                      f'nem dobras na ficha '
                                      f'({_origem(ws_caixa, n)}).')
                dur = _duracao_lida(dur, nome, 'fermentação em caixa',
                                    _origem(ws_caixa, n), avisos)
                cf = (local is not None
                      and normalizar_nome(local).startswith('camara'))
                params = _limpar({
                    'local': local, 'temp_c': temp,
                    'duracao_h': (round(dur_h_min / 60.0, 2)
                                  if dur_h_min is not None else None),
                    'dobras_min': dobras, 'duracao_origem': duracao_origem,
                    'origem': _origem(ws_caixa, n)})
                # qualquer das três dobras amarela marca 'dobras_min'
                _marcar_incertos(params, row, [
                    ('local', c_local), ('temp_c', c_temp), ('duracao_h', c_dur)]
                    + [('dobras_min', c) for c in c_dobras if c is not None])
                slot['caixa'].append({
                    'nome': NOME_CAIXA, 'duracao_min': dur or 0,
                    'equipamento': 'camara_fria' if cf else None, 'ativa': False,
                    'descricao': None, 'parametros': params})
            if mesa_min is not None:
                mesa_min = _duracao_lida(mesa_min, nome, 'tempo de mesa',
                                         _origem(ws_caixa, n), avisos)
                params_mesa = _limpar({'tempo_como_escrito': _como_escrito(mesa_raw),
                                       'origem': _origem(ws_caixa, n)})
                _marcar_incertos(params_mesa, row, [('tempo_como_escrito', c_mesa)])
                slot['caixa'].append({
                    'nome': NOME_MESA, 'duracao_min': mesa_min or 0,
                    'equipamento': None, 'ativa': False, 'descricao': None,
                    'parametros': params_mesa})
            elif not _vazio(mesa_raw):
                avisos.append(f'{nome}: tempo de mesa ilegível ("{mesa_raw}"; '
                              f'{_origem(ws_caixa, n)}).')

        # 3. Fermentação final (uma linha por método; a 1ª com tempo manda)
        cab, linhas = _linhas(ws_final)
        c_local = _exigir(ws_final, cab, 'local')
        c_temp = _exigir(ws_final, cab, 'temperatura')
        c_txt = _coluna(cab, 'como escrito')
        c_min = _coluna(cab, '(min)')
        if c_txt is None and c_min is None:
            raise PlanilhaInvalida(f'Aba "{ws_final.title}": coluna de tempo '
                                   'não encontrada.')
        metodos = {}
        for n, nome, row in linhas:
            _visto(nome)
            _amarelas_vazias(row, [('local', c_local), ('temperatura', c_temp),
                                   ('tempo', c_txt), ('tempo (min)', c_min)],
                             nome, _origem(ws_final, n), avisos)
            local = _local(_val(row, c_local))
            temp = _numero(_val(row, c_temp))
            txt_raw = _val(row, c_txt)
            txt = _texto(txt_raw)
            mins = _numero(_val(row, c_min))
            mins_txt = _minutos(txt) if txt is not None else None
            if mins is None:
                mins = mins_txt
            elif mins_txt is not None and mins_txt != mins:
                avisos.append(f'{nome}: fermentação final com "Tempo (min)" = '
                              f'{mins} diferente do escrito ("{txt}" = {mins_txt} '
                              f'min) — vale o número; confira a ficha '
                              f'({_origem(ws_final, n)}).')
            if mins is None and txt is None and temp is None:
                continue        # '–' ou em branco: o "Local" sozinho não é dado
            slot = _slot(nome, ws_final, n)
            if mins is None:
                if txt is not None:
                    avisos.append(f'{nome}: fermentação final com tempo ilegível '
                                  f'("{txt}"; {_origem(ws_final, n)}).')
                else:
                    avisos.append(f'{nome}: fermentação final sem tempo na ficha '
                                  f'({_origem(ws_final, n)}).')
            else:
                mins = _duracao_lida(int(round(mins)), nome, 'fermentação final',
                                     _origem(ws_final, n), avisos)
            metodo = {'local': local, 'temp_c': temp, 'tempo_como_escrito': txt,
                      'tempo_min': mins, 'origem': _origem(ws_final, n),
                      'incerto': [k for k, c in (('local', c_local),
                                                 ('temp_c', c_temp),
                                                 ('tempo_como_escrito', c_txt),
                                                 ('tempo_min', c_min))
                                  if _incerto(_cel(row, c))]}
            lista = metodos.setdefault(normalizar_nome(nome), [])
            if any(normalizar_nome(m['local'] or '') == normalizar_nome(local or '')
                   for m in lista):
                # segunda linha do MESMO método (mesmo local): vale a 1ª
                avisos.append(f'{nome}: linha repetida em "{ws_final.title}" '
                              f'({_origem(ws_final, n)}, método '
                              f'"{local or "sem local"}") — só a primeira vale.')
                continue
            lista.append(metodo)
            slot['final']            # garante o slot (criado acima)
        for chave, lista in metodos.items():
            com_tempo = [m for m in lista if m['tempo_min'] is not None]
            principal = com_tempo[0] if com_tempo else lista[0]
            alternativas = [m for m in lista if m is not principal]
            cf = (principal['local'] is not None
                  and normalizar_nome(principal['local']).startswith('camara'))
            incerto = [k for k in principal['incerto']
                       if k in ('local', 'temp_c', 'tempo_como_escrito')
                       or k == 'tempo_min']
            for a in alternativas:
                if a['incerto']:
                    # a alternativa não tem chave própria de incerteza (esquema
                    # fechado): marca 'alternativas' no rótulo e avisa com a
                    # linha dela
                    incerto.append('alternativas')
                    avisos.append(f'{grafias[chave]}: alternativa da fermentação '
                                  f'final ("{a["local"] or "sem local"}") com '
                                  f'leitura incerta na ficha (célula amarela: '
                                  f'{rotulo_chaves(a["incerto"])}; '
                                  f'{a["origem"]}).')
            params = _limpar({
                'local': principal['local'], 'temp_c': principal['temp_c'],
                'tempo_como_escrito': principal['tempo_como_escrito'],
                'alternativas': [_limpar({
                    'local': a['local'], 'temp_c': a['temp_c'],
                    'tempo_min': a['tempo_min'],
                    'tempo_como_escrito': a['tempo_como_escrito']})
                    for a in alternativas],
                'origem': principal['origem'],
                'incerto': list(dict.fromkeys(incerto))})
            partes[chave]['final'].append({
                'nome': NOME_FINAL,
                'duracao_min': principal['tempo_min'] or 0,
                'equipamento': 'camara_fria' if cf else None, 'ativa': False,
                'descricao': None, 'parametros': params})

        # 4. Forno
        cab, linhas = _linhas(ws_forno)
        c_forno = _exigir(ws_forno, cab, 'forno')
        c_tempo = _exigir(ws_forno, cab, 'tempo')
        c_teto = _coluna(cab, 'teto', excluir=('unidade',))
        c_lastro = _coluna(cab, 'lastro', excluir=('unidade',))
        c_unid = _coluna(cab, 'unidade')
        c_alvo = _coluna(cab, 'alvo')
        for n, nome, row in linhas:
            _visto(nome)
            _amarelas_vazias(row, [('forno', c_forno), ('tempo', c_tempo),
                                   ('teto', c_teto), ('lastro', c_lastro),
                                   ('unidade', c_unid), ('alvo interno', c_alvo)],
                             nome, _origem(ws_forno, n), avisos)
            forno_c = _numero(_val(row, c_forno))
            tempo = _numero(_val(row, c_tempo))
            teto = _numero(_val(row, c_teto))
            lastro = _numero(_val(row, c_lastro))
            unidade = _texto(_val(row, c_unid))
            alvo = _numero(_val(row, c_alvo))
            if all(x is None for x in (forno_c, tempo, teto, lastro, unidade,
                                       alvo)):
                continue
            slot = _slot(nome, ws_forno, n)
            if _repetida(slot, 'forno', nome, ws_forno, n):
                continue
            if tempo is None:
                avisos.append(f'{nome}: forno sem tempo na ficha '
                              f'({_origem(ws_forno, n)}).')
                dur = 0
            else:
                dur = _duracao_lida(int(round(tempo)), nome, 'tempo de forno',
                                    _origem(ws_forno, n), avisos)
            params = _limpar({
                'forno_c': forno_c, 'teto': teto, 'lastro': lastro,
                'unidade_teto_lastro': unidade, 'alvo_interno_c': alvo,
                'origem': _origem(ws_forno, n)})
            _marcar_incertos(params, row, [
                ('forno_c', c_forno), ('teto', c_teto), ('lastro', c_lastro),
                ('unidade_teto_lastro', c_unid), ('alvo_interno_c', c_alvo)])
            # o tempo do forno é a duração da etapa, não um parâmetro: marca
            # a incerteza na chave que o rótulo conhece
            if tempo is not None and _incerto(_cel(row, c_tempo)):
                params['incerto'] = params.get('incerto', []) + ['tempo_min']
            slot['forno'].append({
                'nome': NOME_FORNO, 'duracao_min': dur or 0,
                'equipamento': 'forno', 'ativa': True, 'descricao': None,
                'parametros': params})

        fichas = {}
        nomes = []
        for chave in ordem:
            if chave not in partes:
                continue                 # só linhas vazias: nem entra na prévia
            p = partes[chave]
            etapas = p['batimento'] + p['caixa'] + p['final'] + p['forno']
            for e in etapas:
                proprias = [k for k in e['parametros'].get('incerto') or []
                            if k != 'alternativas']   # a alternativa já avisou
                if proprias:
                    avisos.append(f'{grafias[chave]}: {e["nome"]} com leitura '
                                  f'incerta na ficha (célula amarela: '
                                  f'{rotulo_chaves(proprias)}; '
                                  f'{e["parametros"]["origem"]}).')
            fichas[grafias[chave]] = etapas
            nomes.append(grafias[chave])
        return {'fichas': fichas, 'ordem': nomes, 'avisos': avisos}
    finally:
        wb.close()


def sha256_de(origem):
    if isinstance(origem, (bytes, bytearray)):
        dados = bytes(origem)
    else:
        dados = Path(origem).read_bytes()
    return hashlib.sha256(dados).hexdigest()


# ── casamento com o cadastro ──────────────────────────────────────────────────

def casar(fichas):
    """Para cada nome da planilha: {'receita_id', 'receita_nome'} ou
    {'erro': 'nao_encontrada'} / {'erro': 'ambigua', 'candidatos': [...]}.
    Match por nome normalizado EXATO entre receitas não arquivadas."""
    por_nome = {}
    for r in Receita.ativas().all():
        por_nome.setdefault(normalizar_nome(r.nome), []).append(r)
    saida = {}
    for nome in fichas:
        recs = por_nome.get(normalizar_nome(nome), [])
        if len(recs) == 1:
            saida[nome] = {'receita_id': recs[0].id, 'receita_nome': recs[0].nome}
        elif not recs:
            saida[nome] = {'erro': 'nao_encontrada'}
        else:
            saida[nome] = {'erro': 'ambigua',
                           'candidatos': [{'id': r.id, 'nome': r.nome}
                                          for r in sorted(recs, key=lambda x: x.id)]}
    return saida


# ── aplicação ─────────────────────────────────────────────────────────────────

def _backup_etapas():
    rows = (ReceitaEtapa.query
            .order_by(ReceitaEtapa.receita_id, ReceitaEtapa.ordem,
                      ReceitaEtapa.id).all())
    return [{'receita_id': e.receita_id, 'ordem': e.ordem, 'nome': e.nome,
             'duracao_min': e.duracao_min, 'equipamento': e.equipamento,
             'ativa': bool(e.ativa), 'descricao': e.descricao,
             'parametros': e.parametros} for e in rows]


def _travar_aplicacao():
    """Serializa dois `aplicar` concorrentes (Postgres): o segundo espera o
    primeiro commitar e então refaz o gesto por cima — nunca etapas em
    dobro. SQLite (dev/testes) é um processo só."""
    if db.engine.dialect.name == 'postgresql':
        db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'),
                           {'k': LOCK_KEY_APLICAR})


def _reescrever_snapshots_abertos(referencia=None):
    """Reescreve `dados['processo']` dos snapshots de batelada das ordens
    que o Gantt ainda lê (hoje e as continuações dos últimos
    `gantt.MAX_LEAD_DIAS` dias, item com falta, não dispensado, não
    encerrado). Quantidades, MP e subs do snapshot ficam intactos — só o
    passo a passo acompanha a ficha nova."""
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.models.producao_batelada import PlanejamentoItemBatelada
    from app.services.bateladas_paes import processo_da_receita
    from app.services.gantt import MAX_LEAD_DIAS

    ref = referencia or hoje()
    desde = ref - timedelta(days=MAX_LEAD_DIAS)
    q = (db.session.query(PlanejamentoItemBatelada)
         .join(PlanejamentoItem,
               PlanejamentoItem.id == PlanejamentoItemBatelada.item_id)
         .join(PlanejamentoProducao,
               PlanejamentoProducao.id == PlanejamentoItem.planejamento_id)
         .filter(PlanejamentoProducao.data >= desde,
                 PlanejamentoItem.dispensada_em.is_(None),
                 PlanejamentoItem.falta_encerrada_em.is_(None)))
    n = 0
    for snap in q.all():
        it = snap.item
        if it is None or it.receita is None:
            continue
        if int(it.produzido_qtd or 0) >= int(it.qtd_alvo or 0):
            continue
        dados = deepcopy(snap.dados or {})
        dados['processo'] = processo_da_receita(it.receita)
        snap.dados = dados          # objeto novo: o ORM enxerga a mudança
        n += 1
    return n


def aplicar(fichas, casamento, *, usuario_id=None, arquivo=None, sha256=None,
            origem='tela', referencia=None):
    """Substitui TODAS as etapas do sistema pelas da planilha. Não commita.

    Levanta ValueError (sem tocar no banco) quando nenhuma receita da
    planilha casa com o cadastro. Devolve o resumo gravado no marcador:
    receitas aplicadas, etapas, as não encontradas/ambíguas, a chave do
    backup e os snapshots reescritos."""
    casadas = [n for n in fichas if (casamento.get(n) or {}).get('receita_id')]
    if not casadas:
        raise ValueError('Nenhuma receita da planilha casa com uma receita '
                         'ativa do cadastro — nada foi apagado.')
    _travar_aplicacao()
    momento = agora()
    backup = _backup_etapas()
    chave_backup = PREFIXO_BACKUP + momento.strftime('%Y%m%d_%H%M%S')
    AppConfig.set(chave_backup, json.dumps(
        {'quando': momento.isoformat(timespec='seconds'), 'etapas': backup},
        ensure_ascii=False))

    ReceitaEtapa.query.delete(synchronize_session=False)
    db.session.flush()

    aplicadas = []
    nao_encontradas = []
    ambiguas = []
    n_etapas = 0
    for nome, etapas in fichas.items():
        alvo = casamento.get(nome) or {'erro': 'nao_encontrada'}
        if alvo.get('erro') == 'ambigua':
            ambiguas.append(nome)
            continue
        if alvo.get('erro'):
            nao_encontradas.append(nome)
            continue
        for i, e in enumerate(etapas):
            db.session.add(ReceitaEtapa(
                receita_id=alvo['receita_id'], ordem=i,
                nome=str(e.get('nome') or '?')[:80],
                duracao_min=duracao_valida(e.get('duracao_min')),
                equipamento=e.get('equipamento'),
                ativa=bool(e.get('ativa', True)),
                descricao=e.get('descricao'),
                parametros=validar_parametros(e.get('parametros'))))
            n_etapas += 1
        aplicadas.append({'planilha': nome, 'receita_id': alvo['receita_id'],
                          'receita': alvo['receita_nome'], 'etapas': len(etapas)})
    db.session.flush()
    # O DELETE em massa não sincroniza a sessão: qualquer `Receita.etapas` já
    # carregada (tela, seed, selectinload) ainda apontaria para as linhas
    # antigas. Tudo está flushed — expira o identity map inteiro para que o
    # reescrever abaixo leia as etapas NOVAS (ou nenhuma) do banco.
    db.session.expire_all()
    snapshots = _reescrever_snapshots_abertos(referencia)

    resumo = {
        'quando': momento.isoformat(timespec='seconds'),
        'usuario_id': usuario_id, 'origem': origem,
        'arquivo': arquivo, 'sha256': sha256,
        'receitas_aplicadas': len(aplicadas), 'etapas': n_etapas,
        'aplicadas': aplicadas, 'nao_encontradas': nao_encontradas,
        'ambiguas': ambiguas, 'backup': chave_backup,
        'etapas_anteriores': len(backup), 'snapshots_reescritos': snapshots,
    }
    AppConfig.set(MARCADOR_IMPORT, json.dumps(resumo, ensure_ascii=False))
    return resumo


def aplicar_arquivo(caminho, *, usuario_id=None, origem='seed', referencia=None):
    """Lê + casa + aplica um xlsx do disco (seed de startup). Não commita."""
    lido = ler_planilha(caminho)
    casamento = casar(lido['fichas'])
    resumo = aplicar(lido['fichas'], casamento, usuario_id=usuario_id,
                     arquivo=Path(caminho).name, sha256=sha256_de(caminho),
                     origem=origem, referencia=referencia)
    resumo['avisos'] = lido['avisos']
    return resumo


def ultima_importacao():
    """O marcador da última importação (dict) ou None."""
    raw = AppConfig.get(MARCADOR_IMPORT)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {'erro': 'marcador ilegível', 'bruto': raw[:200]}


def situacao_receitas():
    """Receitas não arquivadas com as etapas atuais (pra tela e pra sonda)."""
    from sqlalchemy.orm import selectinload

    from app.services.etapas_receita import resumo_parametros
    recs = (Receita.ativas().options(selectinload(Receita.etapas))
            .order_by(Receita.categoria, Receita.nome).all())
    saida = []
    for r in recs:
        saida.append({
            'id': r.id, 'nome': r.nome, 'categoria': r.categoria or '',
            'n_etapas': len(r.etapas),
            'etapas': [{'nome': e.nome, 'duracao_min': e.duracao_min,
                        'equipamento': e.equipamento, 'ativa': bool(e.ativa),
                        'descricao': e.descricao, 'parametros': e.parametros,
                        'parametros_label': resumo_parametros(e.parametros,
                                                              e.duracao_min)}
                       for e in r.etapas]})
    return saida
