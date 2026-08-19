"""Alerta WhatsApp de desperdicio nao lancado (cron 20:10 BRT): lista as lojas
operacionais sem Desperdicio no dia e avisa o dono; nao envia se todas lancaram."""


def test_lojas_sem_desperdicio_lista_quem_falta(app, admin_user, catalogo):
    from app.extensions import db
    from app.models import Desperdicio, Loja
    from app.services.desperdicio_alerta import lojas_sem_desperdicio
    from app.utils import hoje
    l1 = Loja(nome='Ribeiro do Vale', ativa=True)
    l2 = Loja(nome='Nebraska', ativa=True)
    l3 = Loja(nome='Industria', ativa=True)   # nao operacional -> fora
    l4 = Loja(nome='Anesio', ativa=False)      # inativa -> fora
    db.session.add_all([l1, l2, l3, l4])
    db.session.commit()
    db.session.add(Desperdicio(loja_id=l1.id, receita_id=catalogo['receita'].id,
                               quantidade=2, data=hoje()))
    db.session.commit()
    assert [lj.nome for lj in lojas_sem_desperdicio()] == ['Nebraska']


def test_alerta_envia_quando_falta_loja(app, admin_user, monkeypatch):
    from app.extensions import db
    from app.models import Loja
    from app.services import desperdicio_alerta, zapi
    db.session.add(Loja(nome='Nebraska', ativa=True))
    db.session.commit()
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    enviados = {}
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda numero, msg: enviados.update(numero=numero, msg=msg) or {'ok': True})
    desperdicio_alerta.enviar_alerta_desperdicio()
    assert enviados.get('numero') == '5511999999999'
    assert 'Nebraska' in enviados.get('msg', '')


def test_alerta_nao_envia_se_todas_lancaram(app, admin_user, catalogo, monkeypatch):
    from app.extensions import db
    from app.models import Desperdicio, Loja
    from app.services import desperdicio_alerta, zapi
    from app.utils import hoje
    lj = Loja(nome='Nebraska', ativa=True)
    db.session.add(lj)
    db.session.commit()
    db.session.add(Desperdicio(loja_id=lj.id, receita_id=catalogo['receita'].id,
                               quantidade=1, data=hoje()))
    db.session.commit()
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    chamou = {'n': 0}
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda numero, msg: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    desperdicio_alerta.enviar_alerta_desperdicio()
    assert chamou['n'] == 0


def test_mensagem_pendentes_formato(app):
    """Helper de texto: lista os nomes em bullets + cabecalho explicativo.
    Contrato atualizado em 01/08/2026 (cobranca POR ITEM): o titulo virou
    "Sobras de hoje — pendencias" porque a mensagem passou a cobrir DUAS
    pendencias (loja sem nada + itens nominais)."""
    from app.extensions import db
    from app.models import Loja
    from app.services.desperdicio_alerta import mensagem_pendentes
    a = Loja(nome='Nebraska', ativa=True)
    b = Loja(nome='Anesio', ativa=True)
    db.session.add_all([a, b])
    db.session.commit()
    msg = mensagem_pendentes([a, b])
    assert 'Nebraska' in msg
    assert 'Anesio' in msg
    assert 'sobras' in msg.lower()
    assert 'não lançaram' in msg.lower()


def test_alertar_slack_pendentes_envia_quando_tem_pendencias(app, admin_user, monkeypatch):
    """Slack: ha loja pendente + canal configurado -> post_message chamado."""
    from app.extensions import db
    from app.models import Loja
    from app.services import desperdicio_alerta, slack
    db.session.add(Loja(nome='Nebraska', ativa=True))
    db.session.commit()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    enviados = {}
    monkeypatch.setattr(slack, 'post_message',
                        lambda canal, msg: enviados.update(canal=canal, msg=msg) or {'ok': True, 'ts': '1'})
    res = desperdicio_alerta.alertar_slack_pendentes()
    assert res['enviado'] is True
    assert res['pendentes'] == 1
    assert enviados.get('canal') == 'C0COPILOT'
    assert 'Nebraska' in enviados.get('msg', '')


def test_alertar_slack_pendentes_skip_quando_vazio(app, admin_user, catalogo, monkeypatch):
    """Slack: nenhuma loja pendente -> NAO chama post_message."""
    from app.extensions import db
    from app.models import Desperdicio, Loja
    from app.services import desperdicio_alerta, slack
    from app.utils import hoje
    lj = Loja(nome='Nebraska', ativa=True)
    db.session.add(lj)
    db.session.commit()
    db.session.add(Desperdicio(loja_id=lj.id, receita_id=catalogo['receita'].id,
                               quantidade=1, data=hoje()))
    db.session.commit()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda canal, msg: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    res = desperdicio_alerta.alertar_slack_pendentes()
    assert res == {'enviado': False, 'motivo': 'sem_pendencias'}
    assert chamou['n'] == 0


def test_alertar_slack_pendentes_skip_sem_canal(app, admin_user, monkeypatch):
    """Slack: ha loja pendente mas canal nao configurado -> skip + warning."""
    from app.extensions import db
    from app.models import Loja
    from app.services import desperdicio_alerta, slack
    db.session.add(Loja(nome='Nebraska', ativa=True))
    db.session.commit()
    app.config['SLACK_CANAL_COPILOT'] = ''
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda canal, msg: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    res = desperdicio_alerta.alertar_slack_pendentes()
    assert res['enviado'] is False
    assert res['motivo'] == 'sem_canal_configurado'
    assert chamou['n'] == 0


# ── Anti-duplicata (19/08/2026, dono: "Continua duplicando") ─────────────
# Overlap de deploy = container velho + novo AMBOS disparam o cron do
# minuto; o advisory lock só serializa execuções simultâneas. O claim
# persistente em AppConfig (commitado ANTES do envio) segura a segunda.

def _pendencia(nome='Nebraska'):
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome=nome, ativa=True))
    db.session.commit()


def test_whatsapp_dono_nao_duplica_no_mesmo_dia(app, admin_user, monkeypatch):
    """Caso real 19/08/2026: duas mensagens de sobras às 20:30. O 2º
    disparo do MESMO dia é suprimido pelo claim."""
    from app.services import desperdicio_alerta, zapi
    _pendencia()
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    chamou = {'n': 0}
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    r1 = desperdicio_alerta.enviar_alerta_desperdicio()
    r2 = desperdicio_alerta.enviar_alerta_desperdicio()
    assert r1['enviado'] is True
    assert r2 == {'enviado': False, 'motivo': 'claim_duplicata'}
    assert chamou['n'] == 1


def test_whatsapp_dono_envio_falho_devolve_o_claim(app, admin_user, monkeypatch):
    """Z-API fora não pode queimar o claim do dia: o job seguinte (ou um
    re-disparo manual) ainda consegue avisar."""
    from app.services import desperdicio_alerta, zapi
    _pendencia()
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    monkeypatch.setattr(zapi, 'enviar_texto', lambda n, m: {'ok': False})
    r1 = desperdicio_alerta.enviar_alerta_desperdicio()
    assert r1['enviado'] is False and r1['motivo'] == 'erro_envio'
    chamou = {'n': 0}
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    r2 = desperdicio_alerta.enviar_alerta_desperdicio()
    assert r2['enviado'] is True and chamou['n'] == 1


def test_slack_mesmo_minuto_nao_duplica(app, admin_user, monkeypatch):
    """Dois containers no MESMO tick do cron (20:10) = 1 post só."""
    from datetime import datetime

    from app.services import desperdicio_alerta, slack
    _pendencia()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    monkeypatch.setattr(desperdicio_alerta, 'agora',
                        lambda: datetime(2026, 8, 19, 20, 10, 3))
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda c, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    r1 = desperdicio_alerta.alertar_slack_pendentes()
    r2 = desperdicio_alerta.alertar_slack_pendentes()
    assert r1['enviado'] is True
    assert r2 == {'enviado': False, 'motivo': 'claim_duplicata'}
    assert chamou['n'] == 1


def test_slack_ticks_diferentes_seguem_enviando(app, admin_user, monkeypatch):
    """A escalada 20:10/15/20/25 NÃO pode ser afetada: tick novo = post
    novo (o claim é por minuto, não por dia)."""
    from datetime import datetime

    from app.services import desperdicio_alerta, slack
    _pendencia()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda c, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    for minuto in (10, 15, 20, 25):
        monkeypatch.setattr(desperdicio_alerta, 'agora',
                            lambda m=minuto: datetime(2026, 8, 19, 20, m, 2))
        res = desperdicio_alerta.alertar_slack_pendentes()
        assert res['enviado'] is True
    assert chamou['n'] == 4


def test_slack_claim_false_reenvia(app, admin_user, monkeypatch):
    """O botão manual do /admin/slack-diagnostico (claim=False) re-envia
    mesmo depois de um tick do cron no mesmo minuto."""
    from datetime import datetime

    from app.services import desperdicio_alerta, slack
    _pendencia()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    monkeypatch.setattr(desperdicio_alerta, 'agora',
                        lambda: datetime(2026, 8, 19, 20, 10, 3))
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda c, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    assert desperdicio_alerta.alertar_slack_pendentes()['enviado'] is True
    assert desperdicio_alerta.alertar_slack_pendentes(claim=False)['enviado'] is True
    assert chamou['n'] == 2


def test_slack_envio_falho_devolve_o_claim(app, admin_user, monkeypatch):
    """Slack fora não queima o tick: a retentativa no mesmo minuto ainda
    consegue postar."""
    from datetime import datetime

    from app.services import desperdicio_alerta, slack
    _pendencia()
    app.config['SLACK_CANAL_COPILOT'] = 'C0COPILOT'
    monkeypatch.setattr(desperdicio_alerta, 'agora',
                        lambda: datetime(2026, 8, 19, 20, 15, 1))
    monkeypatch.setattr(slack, 'post_message',
                        lambda c, m: {'ok': False, 'erro': 'down'})
    r1 = desperdicio_alerta.alertar_slack_pendentes()
    assert r1['enviado'] is False
    chamou = {'n': 0}
    monkeypatch.setattr(slack, 'post_message',
                        lambda c, m: chamou.update(n=chamou['n'] + 1) or {'ok': True})
    r2 = desperdicio_alerta.alertar_slack_pendentes()
    assert r2['enviado'] is True and chamou['n'] == 1
