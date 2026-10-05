"""Fermentação: conferência do envio e aviso ao dono (03/10/2026).

Caso real: o aplicativo não estava em #fermentadora-filial, o Slack recusou a
lista da Anésio todo dia desde 27/09 (not_in_channel) e a recusa ficou só no
registro. No mesmo dia a lista de domingo da Ribeiro saiu bloqueada por um
produto do PDV sem vínculo ("CROISSANT AZUL"). A conferência das 12:10/16:00
lê o estado GRAVADO, reenvia o que nem foi tentado e avisa o dono.
Slack e WhatsApp são sempre substituídos; nenhuma chamada sai para a rede.
"""
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

import pytest

from app.extensions import db
from app.models import (
    AppConfig,
    FermentacaoEnvioLoja,
    Loja,
    Receita,
    SeruLojaMap,
    VendaMapa,
    VendaSeruDiaBreakdown,
    VendaSeruDiaLoja,
    VendaSeruDiaria,
)
from app.services import fermentacao, slack_entrega

# Função real capturada no import, antes do substituto do fixture autouse.
_INFO_CANAL_REAL = slack_entrega.info_canal

ALVO = date(2026, 9, 26)
SABADOS = [date(2026, 8, 8), date(2026, 8, 15), date(2026, 8, 22),
           date(2026, 8, 29), date(2026, 9, 5), date(2026, 9, 12),
           date(2026, 9, 19)]
CANAL_RIBEIRO = 'C0RIBEIRO'
CANAL_ANESIO = 'C0ANESIO'
NOMES_CANAIS = {CANAL_RIBEIRO: 'fermentadora-matriz',
                CANAL_ANESIO: 'fermentadora-filial'}
NUMERO_DONO = '5511999990000'
TOKEN = 'token-de-teste-bem-longo-123'
CLAIM = 'fermentacao_alerta_dono'
CLAIM_RIBEIRO = 'fermentacao_alerta_dono_ribeiro_do_vale'
CLAIM_ANESIO = 'fermentacao_alerta_dono_anesio_pinto_rosa'


@pytest.fixture(autouse=True)
def _sem_rede_e_relogio_fixo(app, monkeypatch, congela_hoje):
    congela_hoje(2026, 9, 25, 11)

    def proibido(*args, **kwargs):
        pytest.fail('Teste tentou falar com Slack/WhatsApp sem mock explícito.')

    monkeypatch.setattr('app.services.slack.post_message', proibido)
    monkeypatch.setattr('app.services.slack.update_message', proibido)
    monkeypatch.setattr('app.services.zapi.enviar_texto', proibido)
    monkeypatch.setattr('app.services.slack_entrega.info_canal',
                        lambda canal: {'nome': NOMES_CANAIS.get(canal),
                                       'is_member': canal == CANAL_RIBEIRO})
    monkeypatch.setattr('app.services.instancia.pode_falar_com_o_mundo',
                        lambda *a, **kw: True)
    app.config['SLACK_CANAL_FERMENTACAO_RIBEIRO'] = CANAL_RIBEIRO
    app.config['SLACK_CANAL_FERMENTACAO_ANESIO'] = CANAL_ANESIO
    app.config['SLACK_BOT_TOKEN'] = 'token-falso-apenas-teste'
    app.config['ZAPI_NUMERO_DESTINO'] = NUMERO_DONO
    app.config['ZAPI_BOT_DONO_NUMERO'] = ''
    app.config['APP_BASE_URL'] = 'https://gestao.exemplo.test'


def _receita(nome):
    receita = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add(receita)
    db.session.flush()
    return receita


def _loja(nome):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.flush()
    db.session.add(SeruLojaMap(
        seru_company_name=nome, loja_id=loja.id, ignorar=False,
        confirmado_em=datetime(2026, 8, 1, 9)))
    return loja


def _linha(loja, dia, nome, qtd):
    db.session.add(VendaSeruDiaria(
        data=dia, loja_seru=loja.nome, loja_id=loja.id, seru_nome=nome,
        qtd=qtd, atualizado_em=datetime.combine(dia + timedelta(days=1), time(3))))


def _dia(loja, dia, croissant, pain):
    db.session.add(VendaSeruDiaLoja(
        data=dia, loja_seru=loja.nome, loja_id=loja.id, n_pedidos=1,
        atualizado_em=datetime.combine(dia + timedelta(days=1), time(3))))
    _linha(loja, dia, 'Croissant Francês', croissant)
    _linha(loja, dia, 'Pain au Chocolat', pain)


@pytest.fixture
def cenario(app):
    croissant = _receita('Croissant Tradicional')
    pain = _receita('Pain au Chocolat')
    db.session.add_all([
        VendaMapa(canal='seru', nome_externo='Croissant Francês',
                  receita_id=croissant.id, fator_quantidade=1),
        VendaMapa(canal='seru', nome_externo='Pain au Chocolat',
                  receita_id=pain.id, fator_quantidade=1),
    ])
    ribeiro = _loja('Ribeiro do Vale')
    anesio = _loja('Anésio Pinto Rosa')
    for dia, cro, pa in zip(SABADOS, [39, 42, 47, 51, 52, 56, 60],
                           [2, 9, 4, 8, 3, 6, 5]):
        _dia(ribeiro, dia, cro, pa)
    for dia, cro, pa in zip(SABADOS[-3:], [4, 7, 9], [1, 2, 4]):
        _dia(anesio, dia, cro, pa)
    db.session.commit()
    return {'ribeiro': ribeiro, 'anesio': anesio,
            'croissant': croissant, 'pain': pain}


def _slack(monkeypatch, recusar_anesio=False, resposta_anesio=None):
    chamadas = []

    def publicar(canal, texto, **kwargs):
        chamadas.append(canal)
        if canal == CANAL_ANESIO and resposta_anesio is not None:
            return dict(resposta_anesio)
        if recusar_anesio and canal == CANAL_ANESIO:
            return {'ok': False, 'erro': 'not_in_channel',
                    'codigo_erro': 'not_in_channel', 'rejeitado': True}
        return {'ok': True, 'ts': f'{len(chamadas)}.000100', 'channel': canal}

    monkeypatch.setattr('app.services.slack.post_message', publicar)
    return chamadas


def _whatsapp(monkeypatch, *respostas):
    enviados = []
    fila = list(respostas)

    def enviar(numero, texto, **kwargs):
        enviados.append((numero, texto, kwargs))
        return dict(fila.pop(0) if fila else {'ok': True, 'zaap_id': 'z1'})

    monkeypatch.setattr('app.services.zapi.enviar_texto', enviar)
    return enviados


def _item(resultado, loja):
    return next(item for item in resultado['lojas'] if item['loja'] == loja)


# ── enviar_amanha: detalhe por loja ───────────────────────────────────────

def test_envio_detalha_cada_loja_com_o_nome_na_mensagem(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    resultado = fermentacao.enviar_amanha()
    assert resultado['estado'] == 'parcial'
    assert resultado['data_alvo'] == ALVO.isoformat()
    assert [item['loja'] for item in resultado['lojas']] == ['Ribeiro do Vale', 'Anésio Pinto Rosa']
    assert _item(resultado, 'Anésio Pinto Rosa')['estado'] == 'falhou'
    assert 'Ribeiro do Vale: Lista confirmada no Slack.' in resultado['mensagem']
    assert ('Anésio Pinto Rosa: O aplicativo da padaria não está neste canal'
            in resultado['mensagem'])
    segundo = fermentacao.enviar_amanha()
    assert 'Anésio Pinto Rosa: tentativa já registrada.' in segundo['mensagem']


def test_erro_inesperado_no_calculo_de_uma_loja_nao_impede_a_outra(cenario, monkeypatch):
    original = fermentacao.calcular

    def calcular(data_alvo=None, nome_loja=None):
        if nome_loja == 'Ribeiro do Vale':
            raise RuntimeError('falha simulada')
        return original(data_alvo, nome_loja=nome_loja)

    monkeypatch.setattr(fermentacao, 'calcular', calcular)
    chamadas = _slack(monkeypatch)
    resultado = fermentacao.enviar_amanha()
    assert _item(resultado, 'Ribeiro do Vale') == {
        'loja': 'Ribeiro do Vale', 'estado': 'indisponivel',
        'mensagem': 'erro interno no cálculo; nada foi enviado.'}
    assert _item(resultado, 'Anésio Pinto Rosa')['estado'] == 'enviado'
    assert chamadas == [CANAL_ANESIO]
    assert db.session.get(FermentacaoEnvioLoja, (ALVO, 'Ribeiro do Vale')) is None


# ── conferência: nada a avisar / reenvio ──────────────────────────────────

def test_conferencia_quieta_quando_as_duas_listas_estao_confirmadas(cenario, monkeypatch):
    chamadas = _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida == {'rodou': True, 'pendentes': [], 'reenvio': None}
    assert len(chamadas) == 2
    assert enviados == []


def test_job_das_12h_perdido_a_conferencia_envia_sem_avisar(cenario, monkeypatch):
    chamadas = _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['reenvio'] == 'enviado'
    assert saida['pendentes'] == []
    assert chamadas == [CANAL_RIBEIRO, CANAL_ANESIO]
    assert enviados == []


def test_erro_no_calculo_que_passou_e_reenviado_na_conferencia(cenario, monkeypatch):
    original = fermentacao.calcular
    estado = {'quebrado': True}

    def calcular(data_alvo=None, nome_loja=None):
        if nome_loja == 'Ribeiro do Vale' and estado['quebrado']:
            raise RuntimeError('falha simulada')
        return original(data_alvo, nome_loja=nome_loja)

    monkeypatch.setattr(fermentacao, 'calcular', calcular)
    chamadas = _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    estado['quebrado'] = False
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['pendentes'] == []
    assert chamadas == [CANAL_ANESIO, CANAL_RIBEIRO]
    assert enviados == []


# ── conferência: avisos ───────────────────────────────────────────────────

def test_recusa_not_in_channel_vira_um_aviso_com_o_gesto(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['pendentes'] == ['Anésio Pinto Rosa']
    assert saida['aviso']['enviado'] is True
    assert len(enviados) == 1
    numero, texto, kwargs = enviados[0]
    assert numero == NUMERO_DONO
    assert kwargs == {'critico': True}
    assert texto.startswith('⚠️ Fermentação de amanhã — 26/09 (sábado)')
    assert ('Anésio Pinto Rosa — lista NÃO publicada: o Slack recusou o envio no '
            'canal #fermentadora-filial.') in texto
    assert 'O aplicativo da padaria não está neste canal' in texto
    assert '(not_in_channel)' in texto
    assert '→ Depois: “Verificar e recuperar envio”.' in texto
    assert 'Conferir: https://gestao.exemplo.test/admin/slack/fermentacao' in texto
    assert 'valem até a meia-noite' in texto
    assert 'Ribeiro do Vale —' not in texto
    assert 'O cálculo também está bloqueado' not in texto

    # Rodada das 16h: o mesmo problema não reavisa no mesmo dia.
    assert fermentacao.verificar_envio_de_amanha(ultima=True)['aviso'] == {
        'enviado': False, 'motivo': 'duplicata'}
    assert len(enviados) == 1


def test_recusa_com_calculo_tambem_bloqueado_avisa_os_dois(cenario, monkeypatch):
    _linha(cenario['anesio'], SABADOS[-1], 'CROISSANT AZUL', 3)
    db.session.commit()
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert 'Anésio Pinto Rosa — lista NÃO publicada' in texto
    assert 'O cálculo também está bloqueado (a recuperação só publica a lista completa):' in texto
    assert ('• Anésio Pinto Rosa, 19/09: CROISSANT AZUL: vínculo do PDV ausente, '
            'pendente ou ignorado.') in texto
    assert '“Ignorar” não destrava a lista' in texto
    assert '→ Depois de resolver a recusa e o cálculo: “Verificar e recuperar envio”.' in texto


def test_croissant_sem_vinculo_bloqueia_e_aviso_explica(cenario, monkeypatch):
    _linha(cenario['ribeiro'], SABADOS[-1], 'CROISSANT AZUL', 5)
    db.session.commit()
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['pendentes'] == ['Ribeiro do Vale']
    texto = enviados[0][1]
    assert 'Ribeiro do Vale — aviso publicado no canal, SEM quantidades. O cálculo parou em:' in texto
    assert ('• Ribeiro do Vale, 19/09: CROISSANT AZUL: vínculo do PDV ausente, '
            'pendente ou ignorado.') in texto
    assert '→ Vincule o produto em Mapeamentos do PDV (“Ignorar” não destrava a lista).' in texto
    assert '“Corrigir a mensagem no Slack” e avise a equipe' in texto
    assert 'Anésio Pinto Rosa —' not in texto


def test_venda_sem_itens_nao_promete_correcao_pelo_sistema(cenario, monkeypatch):
    db.session.add(VendaSeruDiaBreakdown(
        data=SABADOS[2], loja_seru='Ribeiro do Vale', dimensao='sem_itens',
        chave='pdv-facil', valor=35))
    db.session.commit()
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert '• Ribeiro do Vale, 22/08: há vendas sem produtos detalhados.' in texto
    assert 'não há o que vincular. Combine a quantidade com a equipe.' in texto
    assert 'Corrigir a mensagem no Slack' not in texto
    assert ('→ Para amanhã, combine a quantidade com a equipe: corrigir o cadastro '
            'não destrava esta lista.') in texto


def test_vinculo_duplicado_tem_dica_propria(cenario, monkeypatch):
    _linha(cenario['ribeiro'], SABADOS[-1], 'croissant azul', 5)
    db.session.add_all([
        VendaMapa(canal='seru', nome_externo='CROISSANT AZUL',
                  receita_id=cenario['croissant'].id, fator_quantidade=1),
        VendaMapa(canal='seru', nome_externo='Croissant Azul',
                  receita_id=cenario['croissant'].id, fator_quantidade=1),
    ])
    db.session.commit()
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert 'croissant azul: mais de um vínculo do PDV para conferir.' in texto
    assert 'Há mais de um vínculo com esse nome em Mapeamentos do PDV' in texto
    assert '“Ignorar” não destrava' not in texto


def test_ignorar_croissant_continua_bloqueando_e_vincular_destrava(cenario):
    """Orientação dada ao dono: só VINCULAR destrava; ignorar não."""
    _linha(cenario['ribeiro'], SABADOS[-1], 'CROISSANT AZUL', 5)
    mapa = VendaMapa(canal='seru', nome_externo='CROISSANT AZUL', ignorar=True,
                     confirmado_em=datetime(2026, 9, 28, 10))
    db.session.add(mapa)
    db.session.commit()
    bloqueado = fermentacao.calcular(ALVO, nome_loja='Ribeiro do Vale')
    assert not bloqueado['ok']
    assert any('CROISSANT AZUL: vínculo do PDV ausente, pendente ou ignorado.' in erro
               for erro in bloqueado['erros'])
    mapa.ignorar = False
    mapa.receita_id = cenario['croissant'].id
    db.session.commit()
    liberado = fermentacao.calcular(ALVO, nome_loja='Ribeiro do Vale')
    assert liberado['ok'], liberado['erros']
    # 19/09 passa de 60 para 65: (65 + 51) / 2 = 58.
    assert liberado['lojas'][0]['croissant'] == 58


def test_envio_nao_confirmado_e_correcao_nao_confirmada(cenario, monkeypatch):
    _slack(monkeypatch, resposta_anesio={'ok': False, 'erro': 'timeout'})
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    assert db.session.get(FermentacaoEnvioLoja, (ALVO, 'Anésio Pinto Rosa')).estado == 'incerto'
    ribeiro = db.session.get(FermentacaoEnvioLoja, (ALVO, 'Ribeiro do Vale'))
    ribeiro.estado = 'correcao_incerta'
    db.session.commit()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert ('Anésio Pinto Rosa — o Slack não confirmou o envio no canal '
            '#fermentadora-filial: a lista pode não ter chegado.') in texto
    assert '→ Espere 5 minutos após a tentativa, confira o canal' in texto
    assert 'não duplica' in texto
    assert 'Ribeiro do Vale — o Slack não confirmou a correção da mensagem.' in texto


def test_canal_nao_configurado_cita_a_variavel(cenario, app, monkeypatch):
    app.config['SLACK_CANAL_FERMENTACAO_ANESIO'] = ''
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert 'Anésio Pinto Rosa — lista NÃO enviada: canal não configurado.' in texto
    assert 'Configure SLACK_CANAL_FERMENTACAO_ANESIO no Railway' in texto


def test_bot_sem_token_avisa_as_duas_lojas(cenario, app, monkeypatch):
    app.config['SLACK_BOT_TOKEN'] = ''
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['reenvio'] == 'indisponivel'
    texto = enviados[0][1]
    assert 'Ribeiro do Vale — lista NÃO enviada: Canal ou bot do Slack não configurado.' in texto
    assert 'Anésio Pinto Rosa — lista NÃO enviada: Canal ou bot do Slack não configurado.' in texto
    assert 'Configure SLACK_BOT_TOKEN no Railway' in texto


def test_erro_no_calculo_que_persiste_vira_aviso(cenario, monkeypatch):
    original = fermentacao.calcular

    def calcular(data_alvo=None, nome_loja=None):
        if nome_loja == 'Ribeiro do Vale':
            raise RuntimeError('falha simulada')
        return original(data_alvo, nome_loja=nome_loja)

    monkeypatch.setattr(fermentacao, 'calcular', calcular)
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    texto = enviados[0][1]
    assert ('Ribeiro do Vale — lista NÃO enviada: erro interno no cálculo; '
            'nada foi enviado.') in texto
    assert '“Enviar mensagens pendentes de amanhã”' in texto
    assert 'Se o erro repetir, é falha do sistema' in texto


def test_envio_travado_so_avisa_na_ultima_rodada(cenario, monkeypatch):
    monkeypatch.setattr(fermentacao, 'enviar_amanha', lambda: {
        'estado': 'ocupado', 'mensagem': fermentacao.MSG_OCUPADO})
    enviados = _whatsapp(monkeypatch)
    assert fermentacao.verificar_envio_de_amanha() == {
        'rodou': True, 'pendentes': [], 'reenvio': 'ocupado',
        'em_andamento': ['Ribeiro do Vale', 'Anésio Pinto Rosa']}
    assert enviados == []
    saida = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert saida['aviso']['enviado'] is True
    texto = enviados[0][1]
    assert 'lista NÃO enviada: O envio já está em andamento.' in texto
    assert 'a trava ficou presa: reinicie o serviço no Railway' in texto


def test_copia_de_homologacao_nao_reenvia_nem_avisa(cenario, monkeypatch):
    monkeypatch.setattr('app.services.instancia.pode_falar_com_o_mundo',
                        lambda *a, **kw: False)
    assert fermentacao.verificar_envio_de_amanha() == {'rodou': False, 'motivo': 'instancia'}
    assert FermentacaoEnvioLoja.query.count() == 0


def test_whatsapp_fora_devolve_a_data_e_a_tarde_tenta_de_novo(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch, {'ok': False, 'desconectado': True,
                                       'erro': 'Z-API desconectado'})
    fermentacao.enviar_amanha()
    assert fermentacao.verificar_envio_de_amanha()['aviso'] == {
        'enviado': False, 'motivo': 'falha_envio'}
    assert AppConfig.get(CLAIM_ANESIO) is None
    assert fermentacao.verificar_envio_de_amanha(ultima=True)['aviso']['enviado'] is True
    assert AppConfig.get(CLAIM_ANESIO).startswith(ALVO.isoformat() + ':')
    assert len(enviados) == 2


def test_sem_numero_do_dono_nao_gasta_a_data(cenario, app, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    app.config['ZAPI_NUMERO_DESTINO'] = ''
    assert fermentacao.verificar_envio_de_amanha()['aviso'] == {
        'enviado': False, 'motivo': 'sem_numero'}
    assert AppConfig.get(CLAIM_ANESIO) is None


def test_nome_do_canal_indisponivel_usa_o_id(cenario, monkeypatch):
    monkeypatch.setattr('app.services.slack_entrega.info_canal',
                        lambda canal: {'erro': 'falha_consulta_slack'})
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    assert 'o Slack recusou o envio no canal C0ANESIO.' in enviados[0][1]


# ── cron, tela e agenda ───────────────────────────────────────────────────

def test_jobs_de_envio_e_conferencia(cenario, app, monkeypatch):
    from app.services import seru_cron

    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    seru_cron._run_fermentacao(app)
    assert enviados == []  # o envio das 12h não avisa; a conferência avisa
    seru_cron._run_fermentacao_conferencia(app, ultima=False)
    assert len(enviados) == 1
    assert 'Anésio Pinto Rosa — lista NÃO publicada' in enviados[0][1]


def test_agenda_tem_conferencia_as_12h10_e_as_16h(app, monkeypatch):
    from zoneinfo import ZoneInfo

    from apscheduler.schedulers.background import BackgroundScheduler

    from app.services import seru_cron

    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self: None)
    monkeypatch.setattr(seru_cron, '_scheduler', None)
    monkeypatch.setenv('SERU_AUTO_SYNC', '1')
    seru_cron.iniciar(app)
    inicio = datetime(2026, 10, 3, tzinfo=ZoneInfo('America/Sao_Paulo'))

    def proximo(job_id):
        atual = seru_cron._scheduler.get_job(job_id).trigger.get_next_fire_time(None, inicio)
        return atual.day, atual.hour, atual.minute

    assert proximo('slack-fermentacao') == (3, 12, 0)
    assert proximo('slack-fermentacao-conferencia') == (3, 12, 10)
    assert proximo('slack-fermentacao-conferencia-tarde') == (3, 16, 0)


def _cliente_dono(app, owner_user):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    return cliente


def test_tela_manual_nao_manda_whatsapp(cenario, app, owner_user, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    resposta = _cliente_dono(app, owner_user).post('/admin/slack/fermentacao', data={})
    assert resposta.status_code in (302, 303)
    assert db.session.get(FermentacaoEnvioLoja, (ALVO, 'Anésio Pinto Rosa')).estado == 'falhou'
    assert enviados == []


def test_tela_mostra_erro_do_calculo_em_vez_de_quebrar(cenario, app, owner_user, monkeypatch):
    def quebra(*args, **kwargs):
        raise RuntimeError('falha simulada')

    monkeypatch.setattr(fermentacao, 'calcular', quebra)
    resposta = _cliente_dono(app, owner_user).get('/admin/slack/fermentacao')
    assert resposta.status_code == 200
    assert 'erro interno no cálculo' in resposta.get_data(as_text=True)


# ── sonda /api/claude/fermentacao ─────────────────────────────────────────

def _sonda(app, consulta=''):
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    return app.test_client().get(f'/api/claude/fermentacao{consulta}',
                                 headers={'Authorization': f'Bearer {TOKEN}'})


def test_sonda_exige_token(app):
    app.config['CLAUDE_API_TOKEN'] = ''
    assert app.test_client().get('/api/claude/fermentacao').status_code == 503
    app.config['CLAUDE_API_TOKEN'] = TOKEN
    resposta = app.test_client().get('/api/claude/fermentacao',
                                     headers={'Authorization': 'Bearer errado'})
    assert resposta.status_code == 401


def test_sonda_recusa_data_invalida_ou_fora_do_intervalo(app):
    assert _sonda(app, '?data=26-09-2026').status_code == 400
    assert _sonda(app, '?data=0001-01-01').status_code == 400
    assert _sonda(app, '?data=2099-01-01').status_code == 400


def test_sonda_mostra_recusa_gravada_e_calculo_sem_fontes(cenario, app, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    dados = _sonda(app, '?data=2026-09-26').get_json()
    assert dados['ok'] is True
    assert dados['destinos'] == {'Ribeiro do Vale': CANAL_RIBEIRO,
                                 'Anésio Pinto Rosa': CANAL_ANESIO}
    envios = {item['loja']: item for item in dados['envios']}
    assert envios['Anésio Pinto Rosa']['estado'] == 'falhou'
    assert envios['Anésio Pinto Rosa']['codigo_erro'] == 'not_in_channel'
    assert envios['Anésio Pinto Rosa']['rejeitado'] is True
    assert envios['Ribeiro do Vale']['estado'] == 'enviado'
    assert len(dados['historico']) == 2
    calculo = dados['calculo']['Ribeiro do Vale']
    assert calculo['ok'] is True
    assert calculo['lojas'][0]['croissant'] == 56
    assert all('fontes' not in dia for dia in calculo['lojas'][0]['dias'])
    assert 'slack' not in dados
    com_fontes = _sonda(app, '?data=2026-09-26&fontes=1').get_json()
    assert all('fontes' in dia
               for dia in com_fontes['calculo']['Ribeiro do Vale']['lojas'][0]['dias'])


def test_sonda_consulta_se_o_aplicativo_e_membro_dos_canais(cenario, app, monkeypatch):
    def info(canal):
        if canal == CANAL_ANESIO:
            return {'nome': 'fermentadora-filial', 'is_member': False,
                    'is_archived': False, 'is_private': False}
        return {'erro': 'falha_consulta_slack'}

    monkeypatch.setattr('app.services.slack_entrega.info_canal', info)
    dados = _sonda(app, '?slack=1').get_json()
    assert dados['slack']['Anésio Pinto Rosa'] == {
        'canal': CANAL_ANESIO, 'nome': 'fermentadora-filial', 'is_member': False,
        'is_archived': False, 'is_private': False}
    assert dados['slack']['Ribeiro do Vale'] == {'canal': CANAL_RIBEIRO,
                                                 'erro': 'falha_consulta_slack'}


def test_info_canal_le_o_slack_sem_levantar(app, monkeypatch):
    class Cliente:
        def conversations_info(self, channel):
            if channel == 'C0QUEBRA':
                raise RuntimeError('sem rede')
            if channel == 'C0ESTRANHO':
                return {'ok': False}
            return {'ok': True, 'channel': {'name': 'fermentadora-filial',
                                            'is_member': False}}

    monkeypatch.setattr(slack_entrega, '_cliente_leitura', lambda: Cliente())
    assert _INFO_CANAL_REAL('C0ANESIO') == {
        'nome': 'fermentadora-filial', 'is_member': False,
        'is_archived': None, 'is_private': None}
    assert _INFO_CANAL_REAL('C0QUEBRA') == {'erro': 'falha_consulta_slack'}
    assert _INFO_CANAL_REAL('C0ESTRANHO') == {'erro': 'resposta_slack_invalida'}
    assert _INFO_CANAL_REAL('canal inválido') == {'erro': 'canal_invalido'}


# ── revisão rodada 2: em andamento, um aviso por problema, falhas ─────────

AGORA = datetime(2026, 9, 25, 11)


def _gravar(loja, estado, minutos_atras=30, calculo=None, slack_ts=None, **entrega):
    canal = CANAL_ANESIO if loja == 'Anésio Pinto Rosa' else CANAL_RIBEIRO
    dados = dict(calculo or {'ok': True, 'erros': []})
    dados['_entrega'] = {'ultima_tentativa_em':
                         (AGORA - timedelta(minutes=minutos_atras)).isoformat(),
                         **entrega}
    envio = FermentacaoEnvioLoja(data_alvo=ALVO, loja=loja, canal=canal, estado=estado,
                                 texto='Lista gravada', calculo=dados, slack_ts=slack_ts,
                                 criado_em=AGORA - timedelta(minutes=minutos_atras))
    db.session.add(envio)
    db.session.commit()
    return envio


def test_envio_em_curso_espera_a_rodada_seguinte(cenario, monkeypatch):
    """N1: `enviando` de menos de 5 minutos ainda pode estar no POST; antes da
    última rodada não vira "não confirmou" (nem gasta o aviso do dia)."""
    _gravar('Ribeiro do Vale', 'enviado')
    _gravar('Anésio Pinto Rosa', 'enviando', minutos_atras=2)
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida == {'rodou': True, 'pendentes': [], 'reenvio': None,
                     'em_andamento': ['Anésio Pinto Rosa']}
    assert enviados == []
    assert AppConfig.get(CLAIM_ANESIO) is None
    final = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert final['pendentes'] == ['Anésio Pinto Rosa']
    assert 'Anésio Pinto Rosa — o Slack não confirmou o envio' in enviados[0][1]


def test_envio_parado_ha_mais_de_5_minutos_avisa_ja_ao_meio_dia(cenario, monkeypatch):
    _gravar('Ribeiro do Vale', 'enviado')
    _gravar('Anésio Pinto Rosa', 'enviando', minutos_atras=6)
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['pendentes'] == ['Anésio Pinto Rosa']
    assert 'em_andamento' not in saida
    assert len(enviados) == 1


def test_tentativa_em_curso_so_vale_para_enviando_recente():
    def envio(estado, bruto):
        return FermentacaoEnvioLoja(estado=estado, calculo={'_entrega': {
            'ultima_tentativa_em': bruto}}, criado_em=AGORA - timedelta(hours=1))

    assert fermentacao._tentativa_em_curso(
        envio('enviando', (AGORA - timedelta(minutes=4)).isoformat()))
    assert not fermentacao._tentativa_em_curso(
        envio('enviando', (AGORA - timedelta(minutes=6)).isoformat()))
    assert not fermentacao._tentativa_em_curso(
        envio('incerto', (AGORA - timedelta(minutes=1)).isoformat()))
    assert not fermentacao._tentativa_em_curso(envio('enviando', 'horário torto'))
    assert not fermentacao._tentativa_em_curso(
        envio('enviando', '2026-09-25T10:58:00+00:00'))
    # Sem horário gravado vale o criado_em (1 h atrás): não está em curso.
    assert not fermentacao._tentativa_em_curso(envio('enviando', None))


def test_trava_ocupada_ao_meio_dia_nao_cala_a_outra_loja(cenario, monkeypatch):
    """N2: Ribeiro sem registro e trava ocupada; a Anésio já gravada como
    recusada é avisada na mesma rodada."""
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha(nome_loja='Anésio Pinto Rosa')
    monkeypatch.setattr(fermentacao, 'enviar_amanha', lambda: {
        'estado': 'ocupado', 'mensagem': fermentacao.MSG_OCUPADO})
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha()
    assert saida['em_andamento'] == ['Ribeiro do Vale']
    assert saida['pendentes'] == ['Anésio Pinto Rosa']
    texto = enviados[0][1]
    assert 'Anésio Pinto Rosa — lista NÃO publicada' in texto
    assert 'Ribeiro do Vale —' not in texto


def test_tarde_avisa_so_o_problema_novo(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    assert len(enviados) == 1
    ribeiro = db.session.get(FermentacaoEnvioLoja, (ALVO, 'Ribeiro do Vale'))
    ribeiro.estado = 'correcao_incerta'
    db.session.commit()
    saida = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert saida['pendentes'] == ['Ribeiro do Vale', 'Anésio Pinto Rosa']
    assert saida['aviso']['enviado'] is True
    texto = enviados[1][1]
    assert 'Ribeiro do Vale — o Slack não confirmou a correção da mensagem.' in texto
    assert 'Anésio Pinto Rosa —' not in texto


def test_mesmo_problema_com_outro_estado_avisa_de_novo(cenario, monkeypatch):
    _slack(monkeypatch, resposta_anesio={'ok': False, 'erro': 'timeout'})
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha()
    fermentacao.verificar_envio_de_amanha()
    anesio = db.session.get(FermentacaoEnvioLoja, (ALVO, 'Anésio Pinto Rosa'))
    calculo = dict(anesio.calculo)
    calculo['_entrega'] = dict(calculo['_entrega'], codigo_erro='not_in_channel',
                               rejeitado=True)
    anesio.calculo = calculo
    anesio.estado = 'falhou'
    db.session.commit()
    fermentacao.verificar_envio_de_amanha(ultima=True)
    assert len(enviados) == 2
    assert 'não confirmou o envio' in enviados[0][1]
    assert 'o Slack recusou o envio' in enviados[1][1]


def test_lista_que_so_saiu_na_rodada_da_tarde_e_avisada(cenario, monkeypatch):
    """N9: às 16:00, lista enviada pela própria conferência (sem tentativa ao
    meio-dia) vira aviso informativo; às 12:10 o atraso não é avisado."""
    chamadas = _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    saida = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert chamadas == [CANAL_RIBEIRO, CANAL_ANESIO]
    assert saida['pendentes'] == []
    assert saida['atrasadas'] == ['Ribeiro do Vale', 'Anésio Pinto Rosa']
    texto = enviados[0][1]
    assert ('Ribeiro do Vale — lista publicada com atraso, às 11:00: não houve '
            'tentativa ao meio-dia.') in texto
    assert '→ Confirme que a equipe viu a mensagem no canal.' in texto
    # A loja já gravada como enviada não vira "atrasada" numa rodada posterior.
    assert 'atrasadas' not in fermentacao.verificar_envio_de_amanha(ultima=True)


def test_loja_enviada_ao_meio_dia_nao_vira_atrasada(cenario, monkeypatch):
    _slack(monkeypatch)
    enviados = _whatsapp(monkeypatch)
    fermentacao.enviar_amanha(nome_loja='Ribeiro do Vale')
    saida = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert saida['atrasadas'] == ['Anésio Pinto Rosa']
    assert 'Ribeiro do Vale —' not in enviados[0][1]


def test_claim_indisponivel_nao_envia(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    monkeypatch.setattr('app.services.whatsapp.claim_envio', lambda chave, tick: ('erro', None))
    enviados = _whatsapp(monkeypatch)
    assert fermentacao.verificar_envio_de_amanha()['aviso'] == {
        'enviado': False, 'motivo': 'erro'}
    assert enviados == []


def test_erro_dentro_da_conferencia_avisa_so_na_ultima_rodada(cenario, monkeypatch):
    def quebra(alvo, ultima):
        raise RuntimeError('falha simulada no banco')

    monkeypatch.setattr(fermentacao, '_conferir', quebra)
    enviados = _whatsapp(monkeypatch)
    assert fermentacao.verificar_envio_de_amanha() == {'rodou': True, 'motivo': 'erro_interno'}
    assert enviados == []
    saida = fermentacao.verificar_envio_de_amanha(ultima=True)
    assert saida['motivo'] == 'erro_interno'
    assert saida['aviso']['enviado'] is True
    assert 'Não foi possível conferir o envio da lista: erro interno' in enviados[0][1]
    assert AppConfig.get(CLAIM) == f'{ALVO.isoformat()}:erro_interno'
    # Mesmo problema no mesmo dia não repete.
    assert fermentacao.verificar_envio_de_amanha(ultima=True)['aviso'] == {
        'enviado': False, 'motivo': 'duplicata'}


def test_whatsapp_que_levanta_devolve_o_claim(cenario, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()

    def levanta(*args, **kwargs):
        raise RuntimeError('rede')

    monkeypatch.setattr('app.services.zapi.enviar_texto', levanta)
    assert fermentacao.verificar_envio_de_amanha()['aviso'] == {
        'enviado': False, 'motivo': 'falha_envio'}
    assert AppConfig.get(CLAIM_ANESIO) is None


# ── N4: corrigir/recuperar e tela com erro interno no cálculo ─────────────

def _quebrar_calculo(monkeypatch):
    def quebra(*args, **kwargs):
        raise RuntimeError('falha simulada')

    monkeypatch.setattr(fermentacao, 'calcular', quebra)


def test_corrigir_com_erro_no_calculo_nao_toca_no_slack(cenario, monkeypatch):
    _slack(monkeypatch)
    fermentacao.enviar_amanha()
    antes = db.session.get(FermentacaoEnvioLoja, (ALVO, 'Ribeiro do Vale'))
    texto_antes = antes.texto
    _quebrar_calculo(monkeypatch)
    resultado = fermentacao.enviar_amanha(corrigir=True, nome_loja='Ribeiro do Vale')
    assert resultado['lojas'] == [{
        'loja': 'Ribeiro do Vale', 'estado': 'enviado',
        'mensagem': ('Erro interno no cálculo: a mensagem do Slack não foi alterada '
                     '(detalhe no log do sistema).')}]
    depois = db.session.get(FermentacaoEnvioLoja, (ALVO, 'Ribeiro do Vale'))
    assert (depois.estado, depois.texto) == ('enviado', texto_antes)
    assert 'correcao_pendente' not in depois.calculo


def test_recuperar_com_erro_no_calculo_nao_reenvia(cenario, monkeypatch):
    chamadas = _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    _quebrar_calculo(monkeypatch)
    resultado = fermentacao.enviar_amanha(recuperar=True, nome_loja='Anésio Pinto Rosa')
    assert resultado['lojas'] == [{
        'loja': 'Anésio Pinto Rosa', 'estado': 'indisponivel',
        'mensagem': ('Erro interno no cálculo atual (detalhe no log do sistema). '
                     'Nenhuma lista foi reenviada.')}]
    assert chamadas == [CANAL_RIBEIRO, CANAL_ANESIO]
    assert db.session.get(FermentacaoEnvioLoja, (ALVO, 'Anésio Pinto Rosa')).estado == 'falhou'


def test_botoes_da_tela_com_erro_no_calculo_nao_dao_500(cenario, app, owner_user, monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    _quebrar_calculo(monkeypatch)
    cliente = _cliente_dono(app, owner_user)
    corrigir = cliente.post('/admin/slack/fermentacao',
                            data={'acao': 'corrigir', 'loja': 'Ribeiro do Vale'})
    recuperar = cliente.post('/admin/slack/fermentacao',
                             data={'acao': 'recuperar', 'loja': 'Anésio Pinto Rosa',
                                   'data_alvo': ALVO.isoformat()})
    assert corrigir.status_code in (302, 303)
    assert recuperar.status_code in (302, 303)


def test_tela_nao_oferece_correcao_feita_do_erro_interno(cenario, app, owner_user,
                                                        monkeypatch):
    _slack(monkeypatch, recusar_anesio=True)
    fermentacao.enviar_amanha()
    _quebrar_calculo(monkeypatch)
    pagina = _cliente_dono(app, owner_user).get('/admin/slack/fermentacao').get_data(as_text=True)
    assert 'Correção indisponível: o cálculo atual falhou por um erro interno' in pagina
    assert 'Corrigir a mensagem no Slack</button>' not in pagina
    assert 'Prévia do cálculo atualizado' not in pagina
    assert 'A verificação ainda procura a mensagem original' in pagina
    assert 'Verificar e recuperar envio</button>' in pagina


def test_calcular_seguro_sobrevive_ao_calendario_e_ao_texto(cenario, monkeypatch):
    _quebrar_calculo(monkeypatch)
    monkeypatch.setattr(fermentacao, 'feriado', lambda data: 1 / 0)
    resultado = fermentacao.calcular_seguro(ALVO, nome_loja='Ribeiro do Vale')
    assert resultado['erro_interno'] is True
    assert resultado['ok'] is False
    assert resultado['feriado_alvo'] is None
    assert 'erro interno no cálculo' in resultado['texto']
    monkeypatch.setattr(fermentacao, 'formatar', lambda *a: 1 / 0)
    assert fermentacao.calcular_seguro(ALVO)['texto'] == (
        'Não foi possível calcular a lista com segurança: erro interno no cálculo.')


# ── N6: dicas por tipo de erro ────────────────────────────────────────────

@pytest.mark.parametrize('erro, dica', [
    ('Ribeiro do Vale, 19/09: Combo X: menu configurável exige composição efetivamente vendida.',
     'vincule o item do PDV a um produto de composição fixa ou a uma receita'),
    ('Ribeiro do Vale, 19/09: CROISSANT: fator do PDV: quantidade inválida.',
     'Corrija o fator do vínculo em Mapeamentos do PDV.'),
    ('Ribeiro do Vale, 19/09: CROISSANT KG: quantidade de vendas em unidades deve ser inteira.',
     'Venda fracionada de item contado em unidades'),
    ('Ribeiro do Vale, 19/09: Croissant Tradicional: rendimento: quantidade inválida.',
     'Corrija o rendimento da receita no cadastro.'),
    ('Ribeiro do Vale, 19/09: Croissant Tradicional / Massa para folhar: quantidade inválida.',
     'Corrija a quantidade do componente na ficha ou na composição citada.'),
    ('Ribeiro do Vale, 19/09: Cesta: tipo de componente inválido em Fita.',
     'Corrija a composição (ficha da receita ou itens do produto) no cadastro.'),
    ('Ribeiro do Vale, 19/09: histórico fechado indisponível.',
     'se a loja não abriu nesse dia, combine a quantidade com a equipe'),
    ('Ribeiro do Vale, 19/09: Croissant Tradicional: quantidade inválida.',
     'Confira o número citado no cadastro'),
])
def test_cada_erro_recebe_a_sua_dica(erro, dica):
    linhas, corrigivel = fermentacao._linhas_calculo({'ok': False, 'erros': [erro]})
    dicas = [linha for linha in linhas if linha.startswith('→ ')]
    assert len(dicas) == 1
    assert dica in dicas[0]
    assert corrigivel is True


def test_dica_repetida_aparece_uma_vez_e_erro_sem_correcao_vence():
    erros = ['Ribeiro do Vale, 19/09: A: vínculo do PDV ausente, pendente ou ignorado.',
             'Ribeiro do Vale, 12/09: B: vínculo do PDV ausente, pendente ou ignorado.',
             'Ribeiro do Vale, 22/08: há vendas sem produtos detalhados.',
             'Ribeiro do Vale, 15/08: algo novo que ninguém mapeou.']
    linhas, corrigivel = fermentacao._linhas_calculo({'ok': False, 'erros': erros})
    dicas = [linha for linha in linhas if linha.startswith('→ ')]
    assert len(dicas) == 2
    assert dicas[0].startswith('→ Vincule o produto em Mapeamentos do PDV')
    assert 'Combine a quantidade com a equipe' in dicas[1]
    assert corrigivel is False
    assert fermentacao._linhas_calculo({'ok': True, 'erros': []}) == ([], False)


def test_soltar_trava_descarta_a_conexao_se_o_unlock_falha():
    conexao = MagicMock()
    conexao.execute.side_effect = RuntimeError('conexão caiu')
    fermentacao._soltar_trava(conexao, True)
    conexao.invalidate.assert_called_once()
    conexao.close.assert_called_once()
    livre = MagicMock()
    fermentacao._soltar_trava(livre, False)
    livre.execute.assert_not_called()
    livre.invalidate.assert_not_called()
    livre.close.assert_called_once()
