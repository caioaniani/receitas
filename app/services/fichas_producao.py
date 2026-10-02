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
  teto/lastro...). Célula em branco NÃO vira parâmetro nem tempo inventado:
  o que a ficha não diz fica fora e aparece em `avisos`.
- `casar(fichas)` liga cada nome da planilha a UMA receita não arquivada por
  nome normalizado EXATO ("Sourdough Tradicional" nunca casa "Mini Sourdough
  Tradicional"); ausente ou ambígua fica de fora, listada.
- `aplicar(fichas, casamento, ...)` SUBSTITUI todas as `ReceitaEtapa` do
  sistema (de TODAS as receitas, como o dono pediu) pelas da planilha, depois
  de guardar um backup JSON em `AppConfig` (o passo a passo escrito pelo
  padeiro nunca se perde sem volta), reescreve o `processo` congelado das
  ordens ainda abertas (o Gantt lê o snapshot de batelada, não a ficha viva)
  e grava o marcador `fichas_producao_import`. Não commita — o chamador
  (rota ou seed de startup) fecha a transação.

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
from datetime import timedelta
from pathlib import Path

from app.extensions import db
from app.models import AppConfig, Receita, ReceitaEtapa
from app.utils import agora, hoje

ARQUIVO_SEED = Path(__file__).resolve().parents[1] / 'seeds_data' / 'fichas_producao_2026_10.xlsx'
MARCADOR_IMPORT = 'fichas_producao_import'
PREFIXO_BACKUP = 'fichas_producao_backup_'
# Ordens cujo snapshot de batelada é reescrito: as que o Gantt ainda lê —
# hoje e as continuações dos últimos MAX_LEAD_DIAS dias (gantt.MAX_LEAD_DIAS).
JANELA_SNAPSHOT_DIAS = 3

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
    return v is None or (isinstance(v, str) and not v.strip())


def _numero(v):
    """Número da célula (int quando inteiro), ou None se não for número."""
    if isinstance(v, bool):
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
    """Minutos a partir de número, timedelta, datetime.time ou texto
    ('1:30', '12 h', '43 min', '24h'). None quando não dá pra ler."""
    if _vazio(v):
        return None
    if isinstance(v, timedelta):
        return int(round(v.total_seconds() / 60))
    if hasattr(v, 'hour') and hasattr(v, 'minute') and not hasattr(v, 'date'):
        return int(v.hour) * 60 + int(v.minute)       # datetime.time
    n = _numero(v)
    if n is not None:
        return int(round(n))
    t = str(v).strip().lower().replace(',', '.')
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', t)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(h|hora|horas|hr|hrs)', t)
    if m:
        return int(round(float(m.group(1)) * 60))
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(min|minutos|m)', t)
    if m:
        return int(round(float(m.group(1))))
    return None


def _local(v):
    t = _texto(v)
    if t is None:
        return None
    return _LOCAL_LABEL.get(normalizar_nome(t), t)


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
        return openpyxl.load_workbook(fonte, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl levanta vários tipos
        raise PlanilhaInvalida(f'Não foi possível abrir a planilha: {exc}') from exc


def _aba(wb, *termos, excluir=()):
    for ws in wb.worksheets:
        t = normalizar_nome(ws.title)
        if all(x in t for x in termos) and not any(x in t for x in excluir):
            return ws
    raise PlanilhaInvalida('Aba "%s" não encontrada na planilha.' % ' '.join(termos))


def _linhas(ws):
    """(numero_linha, cabecalho_normalizado->indice, celulas) das linhas de
    dados: do cabeçalho ("Receita" na 1ª coluna) até a legenda ou o fim."""
    cab = None
    saida = []
    for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not row:
            continue
        primeiro = _texto(row[0])
        if cab is None:
            if primeiro and normalizar_nome(primeiro) == 'receita':
                cab = {normalizar_nome(c): i for i, c in enumerate(row)
                       if not _vazio(c)}
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


def _col(cab, row, *termos):
    """Valor da coluna cujo cabeçalho normalizado contém TODOS os termos."""
    for chave, idx in cab.items():
        if all(t in chave for t in termos):
            return row[idx] if idx < len(row) else None
    return None


def _origem(ws, n):
    return f'{ws.title} · linha {n}'


def _limpar(d):
    return {k: v for k, v in d.items() if v is not None and v != [] and v != ''}


def ler_planilha(origem):
    """Lê o xlsx e devolve
    {'fichas': {nome_planilha: [etapa, ...]}, 'avisos': [...],
     'ordem': [nomes na ordem em que aparecem]}.
    Cada etapa: nome, duracao_min, equipamento, ativa, descricao=None,
    parametros (dict, sempre com 'origem')."""
    wb = _carregar(origem)
    try:
        ws_bat = _aba(wb, 'batimento')
        ws_caixa = _aba(wb, 'fermentacao', excluir=('final',))
        ws_final = _aba(wb, 'fermentacao', 'final')
        ws_forno = _aba(wb, 'forno')
        fichas = {}
        ordem = []
        avisos = []
        partes = {}      # nome -> {'batimento': [...], 'caixa': [...], ...}

        def _slot(nome):
            if nome not in partes:
                partes[nome] = {'batimento': [], 'caixa': [], 'final': [],
                                'forno': []}
                ordem.append(nome)
            return partes[nome]

        # 1. Batimento
        cab, linhas = _linhas(ws_bat)
        for n, nome, row in linhas:
            slot = _slot(nome)
            v1 = _numero(_col(cab, row, 'velocidade 1'))
            v2 = _numero(_col(cab, row, 'velocidade 2'))
            ponto = _texto(_col(cab, row, 'ponto'))
            t_amb = _numero(_col(cab, row, 'ambiente'))
            liq_raw = _col(cab, row, 'liquido')
            t_liq = _numero(liq_raw)
            t_liq_txt = None if t_liq is not None else _texto(liq_raw)
            t_fim = _numero(_col(cab, row, 'apos'))
            if all(x is None for x in (v1, v2, ponto, t_amb, t_liq,
                                       t_liq_txt, t_fim)):
                continue
            dur = int(round((v1 or 0) + (v2 or 0)))
            if v1 is None and v2 is None:
                avisos.append(f'{nome}: batimento sem tempo de velocidade na '
                              f'ficha ({_origem(ws_bat, n)}).')
            slot['batimento'].append({
                'nome': NOME_BATIMENTO, 'duracao_min': dur,
                'equipamento': 'amassadeira', 'ativa': True, 'descricao': None,
                'parametros': _limpar({
                    'velocidade_1_min': v1, 'velocidade_2_min': v2,
                    'ponto_massa': ponto, 'temp_ambiente_c': t_amb,
                    'temp_liquido_c': t_liq, 'temp_liquido_txt': t_liq_txt,
                    'temp_massa_final_c': t_fim,
                    'origem': _origem(ws_bat, n)})})

        # 2. Fermentação em caixa (+ mesa)
        cab, linhas = _linhas(ws_caixa)
        for n, nome, row in linhas:
            slot = _slot(nome)
            local = _local(_col(cab, row, 'local'))
            temp = _numero(_col(cab, row, 'temperatura'))
            dur_h = _numero(_col(cab, row, 'duracao'))
            dobras = [_numero(_col(cab, row, f'dobra {i}')) for i in (1, 2, 3)]
            dobras = [int(round(d)) for d in dobras if d is not None]
            mesa_raw = _col(cab, row, 'mesa')
            mesa_min = _minutos(mesa_raw)
            if all(x is None for x in (local, temp, dur_h)) and not dobras \
                    and mesa_min is None:
                continue
            if dur_h is not None:
                dur = int(round(float(dur_h) * 60))
                duracao_origem = 'ficha'
            elif dobras:
                dur = sum(dobras)                 # até a última dobra
                duracao_origem = 'ate_ultima_dobra'
                avisos.append(f'{nome}: a ficha não diz a duração total da '
                              f'fermentação em caixa — o fluxograma usa o tempo '
                              f'até a última dobra ({dur} min; '
                              f'{_origem(ws_caixa, n)}).')
            else:
                dur = 0
                duracao_origem = 'ausente'
                avisos.append(f'{nome}: fermentação em caixa sem tempo nem '
                              f'dobras na ficha ({_origem(ws_caixa, n)}).')
            cf = local is not None and normalizar_nome(local).startswith('camara')
            slot['caixa'].append({
                'nome': NOME_CAIXA, 'duracao_min': dur,
                'equipamento': 'camara_fria' if cf else None, 'ativa': False,
                'descricao': None,
                'parametros': _limpar({
                    'local': local, 'temp_c': temp,
                    'duracao_h': dur_h, 'dobras_min': dobras,
                    'duracao_origem': duracao_origem,
                    'origem': _origem(ws_caixa, n)})})
            if mesa_min is not None:
                slot['caixa'].append({
                    'nome': NOME_MESA, 'duracao_min': mesa_min,
                    'equipamento': None, 'ativa': False, 'descricao': None,
                    'parametros': _limpar({
                        'tempo_como_escrito': _como_escrito(mesa_raw),
                        'origem': _origem(ws_caixa, n)})})

        # 3. Fermentação final (uma linha por método; a 1ª usável manda)
        cab, linhas = _linhas(ws_final)
        metodos = {}
        for n, nome, row in linhas:
            _slot(nome)
            local = _local(_col(cab, row, 'local'))
            temp = _numero(_col(cab, row, 'temperatura'))
            txt = _texto(_col(cab, row, 'como escrito'))
            mins = _numero(_col(cab, row, '(min)'))
            if mins is None and txt is not None:
                mins = _minutos(txt)
            if mins is None and txt is None and temp is None:
                continue                                   # '–' ou em branco
            if mins is None:
                avisos.append(f'{nome}: fermentação final sem tempo legível '
                              f'("{txt}"; {_origem(ws_final, n)}).')
            metodos.setdefault(nome, []).append({
                'local': local, 'temp_c': temp, 'tempo_como_escrito': txt,
                'tempo_min': int(round(mins)) if mins is not None else None,
                'origem': _origem(ws_final, n)})
        for nome, lista in metodos.items():
            principal, alternativas = lista[0], lista[1:]
            cf = (principal['local'] is not None
                  and normalizar_nome(principal['local']).startswith('camara'))
            partes[nome]['final'].append({
                'nome': NOME_FINAL,
                'duracao_min': principal['tempo_min'] or 0,
                'equipamento': 'camara_fria' if cf else None, 'ativa': False,
                'descricao': None,
                'parametros': _limpar({
                    'local': principal['local'], 'temp_c': principal['temp_c'],
                    'tempo_como_escrito': principal['tempo_como_escrito'],
                    'alternativas': [_limpar({
                        'local': a['local'], 'temp_c': a['temp_c'],
                        'tempo_min': a['tempo_min'],
                        'tempo_como_escrito': a['tempo_como_escrito']})
                        for a in alternativas],
                    'origem': principal['origem']})})

        # 4. Forno
        cab, linhas = _linhas(ws_forno)
        for n, nome, row in linhas:
            slot = _slot(nome)
            forno_c = _numero(_col(cab, row, 'forno'))
            tempo = _numero(_col(cab, row, 'tempo'))
            teto = _numero(_col(cab, row, 'teto'))
            lastro = _numero(_col(cab, row, 'lastro'))
            unidade = _texto(_col(cab, row, 'unidade'))
            alvo = _numero(_col(cab, row, 'alvo'))
            if all(x is None for x in (forno_c, tempo, teto, lastro, unidade,
                                       alvo)):
                continue
            if tempo is None:
                avisos.append(f'{nome}: forno sem tempo na ficha '
                              f'({_origem(ws_forno, n)}).')
            slot['forno'].append({
                'nome': NOME_FORNO,
                'duracao_min': int(round(tempo)) if tempo is not None else 0,
                'equipamento': 'forno', 'ativa': True, 'descricao': None,
                'parametros': _limpar({
                    'forno_c': forno_c, 'teto': teto, 'lastro': lastro,
                    'unidade_teto_lastro': unidade, 'alvo_interno_c': alvo,
                    'origem': _origem(ws_forno, n)})})

        for nome in ordem:
            p = partes[nome]
            fichas[nome] = p['batimento'] + p['caixa'] + p['final'] + p['forno']
        return {'fichas': fichas, 'ordem': ordem, 'avisos': avisos}
    finally:
        wb.close()


def _como_escrito(v):
    if isinstance(v, timedelta):
        total = int(round(v.total_seconds() / 60))
        return '%d:%02d' % divmod(total, 60)
    if hasattr(v, 'hour') and hasattr(v, 'minute') and not hasattr(v, 'date'):
        return '%d:%02d' % (v.hour, v.minute)
    return _texto(v)


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


def _reescrever_snapshots_abertos(referencia=None):
    """Reescreve `dados['processo']` dos snapshots de batelada das ordens
    que o Gantt ainda lê (hoje e os últimos JANELA_SNAPSHOT_DIAS dias, item
    com falta, não dispensado, não encerrado). Quantidades, MP e subs do
    snapshot ficam intactos — só o passo a passo acompanha a ficha nova."""
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.models.producao_batelada import PlanejamentoItemBatelada
    from app.services.bateladas_paes import processo_da_receita

    ref = referencia or hoje()
    desde = ref - timedelta(days=JANELA_SNAPSHOT_DIAS)
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

    Devolve o resumo gravado no marcador: receitas aplicadas, etapas, as não
    encontradas/ambíguas, a chave do backup e os snapshots reescritos."""
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
                receita_id=alvo['receita_id'], ordem=i, nome=e['nome'][:80],
                duracao_min=int(e.get('duracao_min') or 0),
                equipamento=e.get('equipamento'),
                ativa=bool(e.get('ativa', True)),
                descricao=e.get('descricao'),
                parametros=e.get('parametros') or None))
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
                        'parametros_label': resumo_parametros(e.parametros)}
                       for e in r.etapas]})
    return saida
