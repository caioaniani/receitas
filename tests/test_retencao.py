"""Retenção de dados + drill de restore (auditoria 2026-06-09).

Regras travadas aqui:
- Retenção apaga só o que passou do prazo; dry-run não toca em nada.
- Backups no Dropbox: >90 dias somem, mas o MAIS RECENTE sobrevive sempre.
- Drill: reporta dump ausente/corrompido sem explodir.
"""
from datetime import timedelta
from unittest.mock import patch

from app.utils import agora


def _owner_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _semear(db):
    """1 registro VELHO + 1 NOVO por alvo (+ NFLog velho, que NUNCA deve ser
    apagado). Retorna ids relevantes."""
    from app.models import ChatbotConversa, NFLog, ZapiBotEventoProcessado
    velho = agora() - timedelta(days=400)
    recente = agora() - timedelta(days=1)
    nf_v = NFLog(criado_em=velho, resultado='enviada')
    nf_n = NFLog(criado_em=recente, resultado='enviada')
    cv_v = ChatbotConversa(conv_id='v1', mensagens_json='[]',
                           ultima_msg_em=agora() - timedelta(days=200))
    cv_n = ChatbotConversa(conv_id='n1', mensagens_json='[]',
                           ultima_msg_em=recente)
    ev_v = ZapiBotEventoProcessado(message_id='ev-velho',
                                   processado_em=agora() - timedelta(days=8))
    ev_n = ZapiBotEventoProcessado(message_id='ev-novo', processado_em=recente)
    db.session.add_all([nf_v, nf_n, cv_v, cv_n, ev_v, ev_n])
    db.session.commit()
    return {'nf_velho': nf_v.id, 'nf_novo': nf_n.id, 'conv_nova': cv_n.id}


def test_limpeza_apaga_velhos_preserva_novos(app):
    from app.extensions import db
    from app.models import ChatbotConversa, ZapiBotEventoProcessado
    from app.services import retencao
    ids = _semear(db)
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        rel = retencao.executar_limpeza()
    assert rel['chatbot_conversa'] == 1
    assert rel['zapi_bot_evento_processado'] == 1
    # novos sobreviveram
    assert ChatbotConversa.query.get(ids['conv_nova']) is not None
    assert ZapiBotEventoProcessado.query.get('ev-novo') is not None
    assert ZapiBotEventoProcessado.query.get('ev-velho') is None


def test_nflog_nunca_e_apagado(app):
    """Decisao do dono (2026-06-10): auditoria de NF fica PRA SEMPRE — nem
    registro de 400 dias entra na limpeza, e o relatorio nem lista o alvo."""
    from app.extensions import db
    from app.models import NFLog
    from app.services import retencao
    ids = _semear(db)
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        rel = retencao.executar_limpeza()
    assert 'nf_log' not in rel                              # fora dos alvos
    assert NFLog.query.get(ids['nf_velho']) is not None     # velho preservado
    assert NFLog.query.get(ids['nf_novo']) is not None


def test_dry_run_conta_sem_apagar(app):
    from app.extensions import db
    from app.models import ChatbotConversa
    from app.services import retencao
    _semear(db)
    antes = ChatbotConversa.query.count()
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        rel = retencao.executar_limpeza(dry_run=True)
    assert rel['dry_run'] is True
    assert rel['chatbot_conversa'] == 1            # contou a velha
    assert ChatbotConversa.query.count() == antes  # mas NAO apagou


def test_backups_dropbox_90d_preserva_mais_recente(app):
    """Mesmo que TODOS os dumps tenham >90 dias, o mais recente fica —
    um backup velho ainda é melhor que nenhum."""
    from app.services import retencao
    antigo = (agora() - timedelta(days=200)).strftime('%Y-%m-%d')
    medio = (agora() - timedelta(days=120)).strftime('%Y-%m-%d')
    arquivos = [
        {'path': f'/backups-postgres/padaria_{antigo}_0400.dump.gz',
         'nome': f'padaria_{antigo}_0400.dump.gz', 'tamanho': 1, 'modificado': ''},
        {'path': f'/backups-postgres/padaria_{medio}_0400.dump.gz',
         'nome': f'padaria_{medio}_0400.dump.gz', 'tamanho': 1, 'modificado': ''},
    ]

    def _listar(pasta):
        return arquivos if pasta == '/backups-postgres' else []

    deletados = []
    with patch('app.services.dropbox_storage.disponivel', return_value=True), \
         patch('app.services.dropbox_storage.listar_pasta', side_effect=_listar), \
         patch('app.services.dropbox_storage.deletar',
               side_effect=lambda p: deletados.append(p) or True):
        n = retencao._limpar_backups_dropbox(90)
    assert n == 1                                  # so o de 200 dias caiu
    assert antigo in deletados[0]                  # caiu o mais velho
    assert all(medio not in p for p in deletados)  # o mais recente ficou


def test_drill_sem_dump_reporta(app):
    from app.services import backup
    with patch('app.services.dropbox_storage.listar_pasta', return_value=[]):
        rel = backup._executar_drill(full=False)
    assert rel['ok'] is False
    assert 'nenhum' in rel['motivo']


def test_drill_dump_corrompido_reporta(app):
    from app.services import backup
    arqs = [{'path': '/backups-postgres/padaria_2026-06-09_0400.dump.gz',
             'nome': 'padaria_2026-06-09_0400.dump.gz',
             'tamanho': 10, 'modificado': ''}]
    with patch('app.services.dropbox_storage.listar_pasta', return_value=arqs), \
         patch('app.services.dropbox_storage.baixar',
               return_value=b'nao-e-gzip'):
        rel = backup._executar_drill(full=False)
    assert rel['ok'] is False
    assert 'gunzip' in rel['motivo']


def test_rota_retencao_dry_run_default(app):
    """GET /admin/retencao sem params é dry-run: mostra prazos e não apaga."""
    from app.extensions import db
    from app.models import NFLog
    c = _owner_logado(app)
    _semear(db)
    antes = NFLog.query.count()
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        r = c.get('/admin/retencao')
    data = r.get_json()
    assert data['dry_run'] is True
    assert data['prazos_dias']['backups'] == 90
    assert NFLog.query.count() == antes


def test_rota_debug_sentry_sem_dsn_instrui(app):
    c = _owner_logado(app)
    with patch.dict('os.environ', {'SENTRY_DSN': ''}):
        r = c.get('/admin/debug-sentry')
    data = r.get_json()
    assert data['dsn_configurado'] is False
    assert 'como_ativar' in data


def test_drill_status_compartilhado_entre_processos(app, tmp_path):
    """Status do drill persiste em ARQUIVO: qualquer worker gunicorn responde
    o mesmo estado (fix da loteria de worker vista em prod 2026-06-09)."""
    from unittest.mock import patch as _patch

    from app.services import backup
    arq = str(tmp_path / 'drill_status.json')
    with _patch.object(backup, '_DRILL_STATUS_PATH', arq):
        # sem arquivo = estado zerado
        assert backup.drill_status() == {'rodando': False, 'iniciado_em': None,
                                         'resultado': None}
        # grava como worker A; le como worker B (mesmo arquivo)
        backup._drill_salvar({'rodando': False, 'iniciado_em': 'x',
                              'resultado': {'ok': True}})
        st = backup.drill_status()
        assert st['resultado'] == {'ok': True}


def test_drill_abandonado_destrava(app, tmp_path):
    """'rodando: true' órfão (worker morreu no meio) expira após o timeout —
    senão bloquearia novos drills pra sempre."""
    from datetime import timedelta as _td
    from unittest.mock import patch as _patch

    from app.services import backup
    from app.utils import agora as _agora
    arq = str(tmp_path / 'drill_status.json')
    velho = (_agora() - _td(minutes=backup._DRILL_TIMEOUT_MIN + 5)).isoformat()
    with _patch.object(backup, '_DRILL_STATUS_PATH', arq):
        backup._drill_salvar({'rodando': True, 'iniciado_em': velho,
                              'resultado': None})
        st = backup.drill_status()
    assert st['rodando'] is False
    assert 'abandonado' in st['resultado']['motivo']
