"""Fichas de produção (planilha do dono, 02/10/2026) → etapas do fluxograma.

"cadastrar e usar essas infos no gantt/fluxograma de producao; apagar todos
os registros que o gantt usa hoje e trocar por esses dessa planilha".
Serviço app/services/fichas_producao.py; tela /receitas/fichas-producao;
seed de startup _seed_fichas_producao_2026_10; sonda /api/claude/fichas-producao.
A planilha REAL embarcada em app/seeds_data é a fixture.
"""
import json
import re
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
    Usuario,
)
from app.models.producao_batelada import PlanejamentoItemBatelada
from app.services import fichas_producao as svc
from app.services.etapas_receita import resumo_parametros
from app.utils import hoje

NOMES_PLANILHA = [
    'Sourdough Tradicional', 'Sourdough Integral', 'Sourdough 7 Grãos',
    'Sourdough Nozes e Azeitonas', 'Pão Francês Fermentado', 'Brioche',
    'Massa para folhar', 'Pão de Forma Integral',
    'Pão de Forma Integral com Grãos', 'Pão de Cranberry',
    'Croissant Tradicional', 'Pain au Chocolat',
]


def _receita(nome, categoria='Pães', **kw):
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=4,
                rendimento_unidade='un', peso_base=1000.0,
                capacidade_amassadeira_g=50000, **kw)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.commit()
    return r


def _etapa_velha(r, nome='Velha', descricao='texto do padeiro'):
    e = ReceitaEtapa(receita_id=r.id, ordem=0, nome=nome, duracao_min=99,
                     equipamento=None, ativa=True, descricao=descricao)
    db.session.add(e)
    db.session.commit()
    return e


def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'},
           follow_redirects=True)
    return c


@pytest.fixture
def lido():
    return svc.ler_planilha(svc.ARQUIVO_SEED)


# ── leitura da planilha real ─────────────────────────────────────────────────

def test_planilha_embarcada_existe_e_tem_as_12_receitas(lido):
    assert svc.ARQUIVO_SEED.exists()
    assert lido['ordem'] == NOMES_PLANILHA
    assert set(lido['fichas']) == set(NOMES_PLANILHA)


def test_sourdough_tradicional_cinco_etapas_na_ordem_da_ficha(lido):
    etapas = lido['fichas']['Sourdough Tradicional']
    assert [(e['nome'], e['duracao_min'], e['equipamento'], e['ativa'])
            for e in etapas] == [
        (svc.NOME_BATIMENTO, 15, 'amassadeira', True),      # V1 14 + V2 1
        (svc.NOME_CAIXA, 85, 'camara_fria', False),         # 30+30+25 (dobras)
        (svc.NOME_MESA, 90, None, False),                   # 1:30
        (svc.NOME_FINAL, 720, 'camara_fria', False),        # CF 12 h
        (svc.NOME_FORNO, 23, 'forno', True),
    ]
    bat = etapas[0]['parametros']
    assert bat['velocidade_1_min'] == 14 and bat['velocidade_2_min'] == 1
    assert bat['temp_ambiente_c'] == 20 and bat['temp_liquido_c'] == 5.4
    assert bat['temp_massa_final_c'] == 23
    assert 'ponto_massa' not in bat                 # em branco na ficha: fora
    assert re.fullmatch(r'1\. Batimento · linha \d+', bat['origem'])
    caixa = etapas[1]['parametros']
    assert caixa['local'] == 'Câmara fria' and caixa['temp_c'] == 21
    assert caixa['dobras_min'] == [30, 30, 25]
    assert caixa['duracao_origem'] == 'ate_ultima_dobra'
    assert 'duracao_h' not in caixa
    assert etapas[2]['parametros']['tempo_como_escrito'] == '1:30'
    final = etapas[3]['parametros']
    assert final['local'] == 'Câmara fria' and final['temp_c'] == 3
    assert final['tempo_como_escrito'] == '12 h'
    assert final['alternativas'] == [{'local': 'Ambiente', 'temp_c': 21,
                                      'tempo_min': 360,
                                      'tempo_como_escrito': '6 h'}]
    forno = etapas[4]['parametros']
    assert forno.pop('origem').startswith('4. Forno · linha ')
    assert forno == {'forno_c': 235, 'teto': 100, 'lastro': 100}


def test_pao_frances_sem_terceira_dobra_sem_mesa_e_cf_tracinho_ignorado(lido):
    etapas = lido['fichas']['Pão Francês Fermentado']
    assert [e['nome'] for e in etapas] == [
        svc.NOME_BATIMENTO, svc.NOME_CAIXA, svc.NOME_FINAL, svc.NOME_FORNO]
    assert etapas[0]['duracao_min'] == 13                 # 8 + 5
    assert etapas[0]['parametros']['temp_liquido_c'] == 5.9
    caixa = etapas[1]
    assert caixa['duracao_min'] == 60 and caixa['equipamento'] is None
    assert caixa['parametros']['local'] == 'Ambiente'
    assert caixa['parametros']['dobras_min'] == [30, 30]
    final = etapas[2]
    assert final['duracao_min'] == 43 and final['equipamento'] is None
    assert final['parametros']['local'] == 'Ambiente'
    assert final['parametros']['temp_c'] == 20
    assert 'alternativas' not in final['parametros']    # a linha CF era '–'
    assert etapas[3]['duracao_min'] == 13


def test_brioche_v2_zero_fica_registrado_como_zero(lido):
    etapas = lido['fichas']['Brioche']
    assert etapas[0]['duracao_min'] == 30
    assert etapas[0]['parametros']['velocidade_2_min'] == 0
    assert etapas[0]['parametros']['temp_liquido_c'] == 20
    assert etapas[0]['parametros']['temp_massa_final_c'] == 27
    assert etapas[1]['parametros']['dobras_min'] == [20, 20]
    assert etapas[1]['duracao_min'] == 40
    assert etapas[2]['duracao_min'] == 30 and etapas[2]['equipamento'] is None
    forno = dict(etapas[3]['parametros'])
    assert forno.pop('origem').startswith('4. Forno · linha ')
    assert forno == {'forno_c': 170, 'teto': 80, 'lastro': 100}


def test_massa_para_folhar_so_batimento_com_liquido_em_texto(lido):
    etapas = lido['fichas']['Massa para folhar']
    assert len(etapas) == 1
    assert etapas[0]['duracao_min'] == 16                  # 5 + 11
    p = etapas[0]['parametros']
    assert p['temp_liquido_txt'] == 'Água 4,5 / Leite 5,2 / Ovos 7,5'
    assert 'temp_liquido_c' not in p
    assert p['temp_massa_final_c'] == 28.6


def test_croissant_24h_literal_e_pain_so_forno(lido):
    cro = lido['fichas']['Croissant Tradicional']
    assert [(e['nome'], e['duracao_min'], e['equipamento']) for e in cro] == [
        (svc.NOME_FINAL, 1440, None), (svc.NOME_FORNO, 18, 'forno')]
    assert cro[0]['parametros']['temp_c'] == 28
    assert cro[0]['parametros']['tempo_como_escrito'] == '24h'
    assert 'local' not in cro[0]['parametros']
    assert cro[1]['parametros']['teto'] == 80 and cro[1]['parametros']['lastro'] == 20
    pain = lido['fichas']['Pain au Chocolat']
    assert [(e['nome'], e['duracao_min']) for e in pain] == [(svc.NOME_FORNO, 18)]


def test_paes_de_forma_e_cranberry_so_forno(lido):
    for nome in ('Pão de Forma Integral', 'Pão de Forma Integral com Grãos',
                 'Pão de Cranberry'):
        etapas = lido['fichas'][nome]
        assert [(e['nome'], e['duracao_min']) for e in etapas] == [(svc.NOME_FORNO, 25)]
        assert etapas[0]['parametros']['forno_c'] == 180


def test_avisos_dizem_o_que_a_ficha_nao_informa(lido):
    # 6 receitas com fermentação em caixa sem "Duração (h)": o fluxograma usa
    # o tempo até a última dobra e avisa — nada é inventado em silêncio.
    caixa = [a for a in lido['avisos'] if 'fermentação em caixa' in a]
    assert len(caixa) == 6
    assert any(a.startswith('Sourdough Tradicional') and '85 min' in a for a in caixa)
    assert len(lido['avisos']) == 6


def test_planilha_invalida_levanta_erro_legivel(tmp_path):
    with pytest.raises(svc.PlanilhaInvalida):
        svc.ler_planilha(b'isto nao e um xlsx')
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.title = 'Outra coisa'
    caminho = tmp_path / 'x.xlsx'
    wb.save(caminho)
    with pytest.raises(svc.PlanilhaInvalida, match='batimento'):
        svc.ler_planilha(caminho)


def test_minutos_de_texto_e_celulas():
    from datetime import time
    assert svc._minutos('12 h') == 720
    assert svc._minutos('43 min') == 43
    assert svc._minutos('24h') == 1440
    assert svc._minutos('1:30') == 90
    assert svc._minutos(timedelta(hours=1, minutes=30)) == 90
    assert svc._minutos(time(1, 30)) == 90
    assert svc._minutos(45) == 45
    assert svc._minutos('–') is None
    assert svc._minutos(None) is None


# ── rótulo dos parâmetros (fonte única) ──────────────────────────────────────

def test_resumo_parametros_formata_em_portugues(lido):
    sd = lido['fichas']['Sourdough Tradicional']
    assert resumo_parametros(sd[0]['parametros'], 15) == \
        'V1 14 min · V2 1 min · líquido 5,4 °C · ambiente 20 °C · massa ao fim 23 °C'
    assert resumo_parametros(sd[1]['parametros'], 85) == \
        'Câmara fria · 21 °C · dobras aos 30, 30 e 25 min'
    assert resumo_parametros(sd[2]['parametros'], 90) == ''   # só o tempo
    assert resumo_parametros(sd[3]['parametros'], 720) == \
        'Câmara fria · 3 °C · alternativa: Ambiente · 21 °C · 6 h'
    assert resumo_parametros(sd[4]['parametros'], 23) == '235 °C · teto 100 · lastro 100'
    mf = lido['fichas']['Massa para folhar'][0]['parametros']
    assert resumo_parametros(mf, 16) == (
        'V1 5 min · V2 11 min · líquido: Água 4,5 / Leite 5,2 / Ovos 7,5 · '
        'ambiente 20 °C · massa ao fim 28,6 °C')
    pf = lido['fichas']['Pão Francês Fermentado'][1]['parametros']
    assert resumo_parametros(pf, 60) == 'Ambiente · 21 °C · dobras aos 30 e 30 min'


def test_resumo_parametros_casos_de_borda():
    assert resumo_parametros(None) == ''
    assert resumo_parametros({}) == ''
    assert resumo_parametros('lixo') == ''
    assert resumo_parametros({'chave_desconhecida': 1, 'origem': 'x'}) == ''
    # duração ilegível: o texto da ficha aparece
    assert resumo_parametros({'tempo_como_escrito': '24h', 'temp_c': 28}, 0) == \
        '28 °C · na ficha: 24h'
    assert resumo_parametros({'dobras_min': [20]}) == 'dobra aos 20 min'


# ── casamento por nome exato ─────────────────────────────────────────────────

def test_casar_por_nome_normalizado_exato(app):
    sd = _receita('Sourdough Tradicional')
    _receita('Mini Sourdough Tradicional')
    pf = _receita('Pao  frances FERMENTADO')          # acento/caixa/espaços
    arquivado = _receita('Brioche')
    arquivado.arquivada_em = hoje()
    db.session.commit()
    _receita('Pão de Cranberry', categoria='Fornadas Especiais')
    _receita('Pão de Cranberry', categoria='Fornadas Especiais')
    fichas = {n: [] for n in ('Sourdough Tradicional', 'Pão Francês Fermentado',
                              'Brioche', 'Pão de Cranberry', 'Pain au Chocolat')}
    c = svc.casar(fichas)
    assert c['Sourdough Tradicional'] == {'receita_id': sd.id,
                                          'receita_nome': 'Sourdough Tradicional'}
    assert c['Pão Francês Fermentado']['receita_id'] == pf.id
    assert c['Brioche'] == {'erro': 'nao_encontrada'}      # só a arquivada
    assert c['Pão de Cranberry']['erro'] == 'ambigua'
    assert len(c['Pão de Cranberry']['candidatos']) == 2
    assert c['Pain au Chocolat'] == {'erro': 'nao_encontrada'}


# ── aplicar: substitui TUDO, com backup e marcador ───────────────────────────

def test_aplicar_substitui_todas_as_etapas_do_sistema_com_backup(app, lido):
    sd = _receita('Sourdough Tradicional')
    bri = _receita('Brioche')
    fora = _receita('Cookie Callebaut', categoria='Doces')   # não está na planilha
    arquivada = _receita('Pão de Cranberry', categoria='Fornadas Especiais')
    arquivada.arquivada_em = hoje()
    _etapa_velha(sd, 'Velha do sourdough', 'passo escrito pelo padeiro')
    _etapa_velha(fora, 'Velha do cookie', 'texto do cookie')
    _etapa_velha(arquivada, 'Velha da arquivada')
    casamento = svc.casar(lido['fichas'])
    resumo = svc.aplicar(lido['fichas'], casamento, usuario_id=7,
                         arquivo='fichas.xlsx', sha256='abc')
    db.session.commit()

    # universo: TODAS as etapas antigas sumiram (inclusive de receita fora da
    # planilha e de arquivada — que nunca recebe as novas: só ativas casam)
    assert ReceitaEtapa.query.filter_by(receita_id=fora.id).count() == 0
    assert ReceitaEtapa.query.filter_by(receita_id=arquivada.id).count() == 0
    assert ReceitaEtapa.query.filter(ReceitaEtapa.nome.like('Velha%')).count() == 0
    db.session.refresh(sd)
    assert [e.nome for e in sd.etapas] == [
        svc.NOME_BATIMENTO, svc.NOME_CAIXA, svc.NOME_MESA, svc.NOME_FINAL,
        svc.NOME_FORNO]
    assert sd.etapas[0].parametros['velocidade_1_min'] == 14     # JSON round-trip
    assert sd.etapas[3].parametros['alternativas'][0]['tempo_min'] == 360
    db.session.refresh(bri)
    assert len(bri.etapas) == 4
    # backup recuperável: as duas etapas antigas, com o texto do padeiro
    bk = json.loads(AppConfig.get(resumo['backup']))
    nomes_bk = {e['nome']: e for e in bk['etapas']}
    assert nomes_bk['Velha do sourdough']['descricao'] == 'passo escrito pelo padeiro'
    assert nomes_bk['Velha do cookie']['receita_id'] == fora.id
    # marcador com o resumo
    marc = json.loads(AppConfig.get(svc.MARCADOR_IMPORT))
    assert marc['receitas_aplicadas'] == 2 and marc['etapas'] == 9
    assert marc['etapas_anteriores'] == 3 and marc['usuario_id'] == 7
    assert marc['arquivo'] == 'fichas.xlsx' and marc['sha256'] == 'abc'
    assert set(marc['nao_encontradas']) == set(NOMES_PLANILHA) - {
        'Sourdough Tradicional', 'Brioche'}
    assert marc['ambiguas'] == []
    assert svc.ultima_importacao()['backup'] == resumo['backup']


def test_aplicar_reescreve_o_processo_das_ordens_abertas_e_so_delas(app, lido):
    sd = _receita('Sourdough Tradicional')
    _etapa_velha(sd, 'Amassamento velho')
    processo_velho = [{'nome': 'Amassamento velho', 'duracao': '99 min',
                       'duracao_min': 99, 'equipamento': None, 'ativa': True,
                       'descricao': None}]

    def _ordem(dia, produzido=0, alvo=8, **kw):
        pl = PlanejamentoProducao(data=dia, origem='cronograma')
        db.session.add(pl)
        db.session.flush()
        it = PlanejamentoItem(planejamento_id=pl.id, receita_id=sd.id,
                              multiplicador=1, qtd_alvo=alvo,
                              produzido_qtd=produzido, **kw)
        db.session.add(it)
        db.session.flush()
        snap = PlanejamentoItemBatelada(
            item_id=it.id, bateladas=2,
            dados={'unidades': 4, 'mp': [{'id': 1, 'quantidade': 500.0}],
                   'processo': processo_velho})
        db.session.add(snap)
        db.session.commit()
        return it

    hj = hoje()
    aberta = _ordem(hj)
    continuacao = _ordem(hj - timedelta(days=3))          # ainda na janela
    antiga = _ordem(hj - timedelta(days=4))               # fora da janela
    concluida = _ordem(hj, produzido=8)
    dispensada = _ordem(hj, dispensada_em=db.func.now())
    encerrada = _ordem(hj, falta_encerrada_em=db.func.now())

    resumo = svc.aplicar(lido['fichas'], svc.casar(lido['fichas']))
    db.session.commit()
    assert resumo['snapshots_reescritos'] == 2

    def _proc(it):
        snap = db.session.get(PlanejamentoItemBatelada, it.id)
        db.session.refresh(snap)
        return snap.dados['processo'], snap.dados

    proc, dados = _proc(aberta)
    assert [p['nome'] for p in proc] == [
        svc.NOME_BATIMENTO, svc.NOME_CAIXA, svc.NOME_MESA, svc.NOME_FINAL,
        svc.NOME_FORNO]
    assert proc[0]['parametros_label'].startswith('V1 14 min')
    assert dados['mp'] == [{'id': 1, 'quantidade': 500.0}]   # pesagem intacta
    assert dados['unidades'] == 4
    assert _proc(continuacao)[0][0]['nome'] == svc.NOME_BATIMENTO
    for it in (antiga, concluida, dispensada, encerrada):
        assert _proc(it)[0] == processo_velho


# ── Gantt e TV usam as etapas e os parâmetros novos ──────────────────────────

def _plano_enviado(dia, rec, alvo=8):
    pl = PlanejamentoProducao(data=dia, origem='cronograma')
    db.session.add(pl)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=pl.id, receita_id=rec.id,
                                    multiplicador=1, qtd_alvo=alvo,
                                    produzido_qtd=0))
    db.session.commit()
    return pl


def test_gantt_mostra_parametros_no_passo_no_destino_e_na_continuacao(app, lido):
    from app.services.gantt import montar_gantt
    sd = _receita('Sourdough Tradicional', dias_producao=1)
    svc.aplicar(lido['fichas'], svc.casar(lido['fichas']))
    db.session.commit()
    hj = hoje()
    _plano_enviado(hj, sd)
    g = montar_gantt(hj)
    prod = next(p for p in g['produtos'] if p['nome'] == 'Sourdough Tradicional')
    etapas = [t['etapa'] for t in prod['tarefas']]
    assert etapas == [svc.NOME_BATIMENTO, svc.NOME_CAIXA, svc.NOME_MESA]
    assert prod['tarefas'][0]['parametros_label'].startswith('V1 14 min · V2 1 min')
    assert prod['tarefas'][1]['parametros_label'] == \
        'Câmara fria · 21 °C · dobras aos 30, 30 e 25 min'
    assert prod['destino_etapa'] == svc.NOME_FINAL
    assert prod['destino_parametros_label'] == \
        'Câmara fria · 3 °C · alternativa: Ambiente · 21 °C · 6 h'
    # continuação no dia seguinte: só o forno, com os parâmetros do forno
    g2 = montar_gantt(hj + timedelta(days=1))
    cont = next(p for p in g2['produtos'] if p['tipo'] == 'continuacao')
    assert [t['etapa'] for t in cont['tarefas']] == [svc.NOME_FORNO]
    assert cont['tarefas'][0]['parametros_label'] == '235 °C · teto 100 · lastro 100'


def test_rota_gantt_renderiza_parametros(app, admin_user, lido):
    sd = _receita('Sourdough Tradicional', dias_producao=1)
    svc.aplicar(lido['fichas'], svc.casar(lido['fichas']))
    db.session.commit()
    _plano_enviado(hoje(), sd)
    c = _login(app, admin_user)
    html = c.get('/padeiro/gantt').get_data(as_text=True)
    assert 'class="step-params"' in html
    assert 'V1 14 min · V2 1 min · líquido 5,4 °C' in html
    assert 'Câmara fria · 3 °C · alternativa: Ambiente · 21 °C · 6 h' in html
    assert 'padeiro-sequencia.css?v=20261002a' in html


def test_tv_mise_en_place_traz_parametros_da_ficha_e_do_snapshot(app, admin_user, lido):
    from app.services.bateladas_paes import processo_da_receita
    sd = _receita('Sourdough Tradicional')
    svc.aplicar(lido['fichas'], svc.casar(lido['fichas']))
    db.session.commit()
    c = _login(app, admin_user)
    d = c.get(f'/padeiro/receita/{sd.id}.json?unidades=8').get_json()
    assert d['processo'][0]['parametros_label'].startswith('V1 14 min')
    forno = dict(d['processo'][4]['parametros'])
    assert forno.pop('origem').startswith('4. Forno · linha ')
    assert forno == {'forno_c': 235, 'teto': 100, 'lastro': 100}
    # mesma forma do processo congelado (fonte única)
    assert [p['nome'] for p in processo_da_receita(sd)] == \
        [p['nome'] for p in d['processo']]


# ── seed de startup ──────────────────────────────────────────────────────────

def test_seed_de_startup_aplica_uma_vez_com_contagens_no_marker(app):
    from app.migrations_legacy import _seed_fichas_producao_2026_10
    sd = _receita('Sourdough Tradicional')
    _receita('Brioche')
    fora = _receita('Cookie', categoria='Doces')
    _etapa_velha(fora)
    _seed_fichas_producao_2026_10(app)
    marker = AppConfig.get('seed_fichas_producao_2026_10')
    assert marker.startswith('receitas=2 etapas=9 anteriores=1 snapshots=0 '
                             'nao_encontradas=10 ambiguas=0 sha256=4b99b0bb17ee')
    db.session.refresh(sd)
    assert len(sd.etapas) == 5
    assert ReceitaEtapa.query.filter_by(receita_id=fora.id).count() == 0
    assert svc.ultima_importacao()['origem'] == 'seed'
    # segunda chamada: no-op (o dono pode ter editado depois)
    _etapa_velha(fora, 'Editada pelo dono')
    _seed_fichas_producao_2026_10(app)
    assert ReceitaEtapa.query.filter_by(receita_id=fora.id).count() == 1
    assert AppConfig.get('seed_fichas_producao_2026_10') == marker


# ── tela /receitas/fichas-producao ───────────────────────────────────────────

def _upload(c, url='/receitas/fichas-producao/previa'):
    import io
    dados = svc.ARQUIVO_SEED.read_bytes()
    return c.post(url, data={'planilha': (io.BytesIO(dados), 'fichas.xlsx')},
                  content_type='multipart/form-data')


def test_tela_estado_lista_receitas_e_as_sem_etapas(app, admin_user):
    sd = _receita('Sourdough Tradicional')
    db.session.add(ReceitaEtapa(receita_id=sd.id, ordem=0, nome='Forno',
                                duracao_min=23, equipamento='forno',
                                parametros={'forno_c': 235, 'teto': 100}))
    _receita('Cookie', categoria='Doces')
    db.session.commit()
    c = _login(app, admin_user)
    html = c.get('/receitas/fichas-producao').get_data(as_text=True)
    assert 'Sourdough Tradicional' in html
    assert '⚙ 235 °C · teto 100' in html
    assert '1 receita(s) sem etapas' in html and 'Cookie' in html
    assert 'Nenhuma importação registrada' in html


def test_previa_mostra_casamento_e_nao_grava(app, admin_user):
    """Admin vê a prévia completa mas não o botão de aplicar (a troca é
    global — só o dono)."""
    sd = _receita('Sourdough Tradicional')
    _etapa_velha(sd)
    c = _login(app, admin_user)
    resp = _upload(c)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Prévia' in html
    assert f'#{sd.id} Sourdough Tradicional' in html
    assert html.count('não encontrada') == 11
    assert 'V1 14 min · V2 1 min' in html
    assert 'Só o proprietário aplica' in html
    assert 'name="token"' not in html
    assert ReceitaEtapa.query.filter_by(receita_id=sd.id, nome='Velha').count() == 1


def test_dono_ve_o_token_na_previa_e_aplica_pela_tela(app, owner_user):
    import re
    sd = _receita('Sourdough Tradicional')
    _etapa_velha(sd)
    c = _login(app, owner_user)
    html = _upload(c).get_data(as_text=True)
    assert 'Só o proprietário aplica' not in html
    token = re.search(r'name="token" value="([^"]+)"', html).group(1)
    resp = c.post('/receitas/fichas-producao/aplicar', data={'token': token},
                  follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Fichas aplicadas: 1 receita(s), 5 etapa(s)' in body
    assert '1 etapa(s) anteriores guardadas' in body
    db.session.refresh(sd)
    assert [e.nome for e in sd.etapas][0] == svc.NOME_BATIMENTO
    marc = svc.ultima_importacao()
    assert marc['origem'] == 'tela' and marc['usuario_id'] == owner_user.id
    assert marc['arquivo'] == 'fichas.xlsx'
    assert 'Última importação' in body


def test_aplicar_pela_tela_exige_dono(app, admin_user):
    """Admin comum com um token válido na mão: 403 e nada gravado."""
    from app.blueprints.receitas.routes import _fichas_serializer
    sd = _receita('Sourdough Tradicional')
    _etapa_velha(sd)
    lido = svc.ler_planilha(svc.ARQUIVO_SEED)
    token = _fichas_serializer().dumps({'fichas': lido['fichas'],
                                        'arquivo': 'x.xlsx', 'sha256': 'abc'})
    c = _login(app, admin_user)
    resp = c.post('/receitas/fichas-producao/aplicar', data={'token': token})
    assert resp.status_code == 403
    assert ReceitaEtapa.query.filter_by(receita_id=sd.id, nome='Velha').count() == 1


def test_aplicar_com_token_invalido_ou_vencido_nao_grava(app, owner_user):
    sd = _receita('Sourdough Tradicional')
    _etapa_velha(sd)
    c = _login(app, owner_user)
    resp = c.post('/receitas/fichas-producao/aplicar', data={'token': 'lixo'},
                  follow_redirects=True)
    assert 'prévia expirou' in resp.get_data(as_text=True)
    assert ReceitaEtapa.query.filter_by(receita_id=sd.id).count() == 1


def test_previa_sem_arquivo_ou_invalido_avisa(app, admin_user):
    import io
    c = _login(app, admin_user)
    resp = c.post('/receitas/fichas-producao/previa', data={},
                  follow_redirects=True)
    assert 'Envie a planilha' in resp.get_data(as_text=True)
    resp = c.post('/receitas/fichas-producao/previa',
                  data={'planilha': (io.BytesIO(b'nao e xlsx'), 'x.xlsx')},
                  content_type='multipart/form-data', follow_redirects=True)
    assert 'Não foi possível abrir a planilha' in resp.get_data(as_text=True)


def test_tela_exige_admin(app):
    u = Usuario(nome='func', login='funcfp', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    c = _login(app, u)
    assert c.get('/receitas/fichas-producao').status_code == 403
    assert _upload(c).status_code == 403


def test_menu_do_catalogo_tem_o_link(app, admin_user):
    c = _login(app, admin_user)
    html = c.get('/area/catalogo').get_data(as_text=True)
    assert '/receitas/fichas-producao' in html


# ── sonda read-only ──────────────────────────────────────────────────────────

TOKEN = 'token-de-teste-bem-longo-123'


def test_sonda_fichas_producao_e_receita_expoem_etapas(app, lido):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    sd = _receita('Sourdough Tradicional')
    _receita('Cookie', categoria='Doces')
    svc.aplicar(lido['fichas'], svc.casar(lido['fichas']))
    db.session.commit()
    c = app.test_client()
    h = {'Authorization': 'Bearer ' + TOKEN}
    d = c.get('/api/claude/fichas-producao', headers=h).get_json()
    assert d['ok'] and d['total_receitas'] == 2 and d['com_etapas'] == 1
    assert [r['nome'] for r in d['sem_etapas']] == ['Cookie']
    assert d['importacao']['receitas_aplicadas'] == 1
    rec = d['receitas'][0]
    assert rec['nome'] == 'Sourdough Tradicional' and rec['n_etapas'] == 5
    assert rec['etapas'][4]['parametros_label'] == '235 °C · teto 100 · lastro 100'
    d2 = c.get(f'/api/claude/receita?id={sd.id}', headers=h).get_json()
    assert [e['nome'] for e in d2['receita']['etapas']][0] == svc.NOME_BATIMENTO
    assert d2['receita']['etapas'][0]['parametros']['velocidade_1_min'] == 14
    assert c.get('/api/claude/fichas-producao').status_code == 401


def test_duplicar_receita_copia_os_parametros(app, admin_user):
    sd = _receita('Sourdough Tradicional')
    db.session.add(ReceitaEtapa(receita_id=sd.id, ordem=0, nome='Forno',
                                duracao_min=23, equipamento='forno',
                                parametros={'forno_c': 235, 'teto': 100}))
    db.session.commit()
    c = _login(app, admin_user)
    c.post(f'/receitas/{sd.id}/duplicar', follow_redirects=True)
    copia = Receita.query.filter_by(nome='Cópia de Sourdough Tradicional').first()
    assert copia is not None
    assert copia.etapas[0].parametros == {'forno_c': 235, 'teto': 100}
