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
    """Helper de texto: lista os nomes em bullets + cabecalho explicativo."""
    from app.extensions import db
    from app.models import Loja
    from app.services.desperdicio_alerta import mensagem_pendentes
    with app.app_context():
        a = Loja(nome='Nebraska', ativa=True)
        b = Loja(nome='Anesio', ativa=True)
        db.session.add_all([a, b])
        db.session.commit()
        msg = mensagem_pendentes([a, b])
        assert 'Nebraska' in msg
        assert 'Anesio' in msg
        assert 'desperdício' in msg.lower()


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
