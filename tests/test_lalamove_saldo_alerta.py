"""Alerta WhatsApp quando saldo da Lalamove cai abaixo de R$ 200.

Decisao do dono 15/06/2026. Disparado pelo webhook
`WALLET_BALANCE_CHANGED` apos `_salvar_saldo`. Dedupe via AppConfig pra
nao metralhar o WhatsApp em sequencia de debitos.
"""
from decimal import Decimal
from unittest.mock import patch


def _config(app):
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    # Z-API minimalmente configurada (a chamada vai ser mockada)
    app.config['ZAPI_INSTANCE_ID'] = 'inst'
    app.config['ZAPI_TOKEN'] = 'tok'
    app.config['ZAPI_CLIENT_TOKEN'] = 'ctok'


def _enviar_saldo(client, valor):
    return client.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave', 'eventType': 'WALLET_BALANCE_CHANGED',
        'data': {'balance': {'amount': str(valor), 'currency': 'BRL'}}})


def test_alerta_dispara_quando_saldo_cai_abaixo_de_200(app):
    """O caso real do dono: saldo cruza pra baixo dos R$200 → ping no WhatsApp."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '150.00')
    send.assert_called_once()
    numero, msg = send.call_args[0]
    assert numero == '5511999990000'
    assert 'Lalamove' in msg and 'saldo baixo' in msg
    assert 'R$ 150,00' in msg
    assert 'R$ 200,00' in msg  # limite na mensagem
    assert 'lalamove.com/business' in msg


def test_alerta_NAO_dispara_quando_saldo_acima_do_limite(app):
    """Recarga ou pagamento que deixa saldo OK não pode alertar."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '350.00')
    send.assert_not_called()


def test_alerta_NAO_dispara_quando_saldo_no_limite(app):
    """Limite (200) é o piso — saldo igual NÃO alerta (só abaixo)."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '200.00')
    send.assert_not_called()


def test_dedupe_NAO_realerta_em_seguidos_debitos_pequenos(app):
    """Anti-spam: alerta em R$ 180 e depois 5min depois R$ 170 → NÃO realerta
    (queda pequena dentro da janela). Sem isso o dono levaria 10 pings
    seguidos em um dia movimentado."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '180.00')
        _enviar_saldo(c, '170.00')
        _enviar_saldo(c, '160.00')
    # Só 1 alerta — o primeiro
    assert send.call_count == 1
    msg = send.call_args[0][1]
    assert 'R$ 180,00' in msg


def test_dedupe_REALERTA_em_queda_grande(app):
    """Cenário: alertou em R$ 190, sobreveio uma corrida cara, saldo foi pra
    R$ 50. Tem que realertar — não dá pra esperar 12h se o caixa secou."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '190.00')  # 1o alerta
        _enviar_saldo(c, '50.00')   # queda > R$ 50 desde último → realerta
    assert send.call_count == 2


def test_dedupe_NAO_realerta_apos_recarga_que_resolveu_e_caiu_de_novo(app):
    """Se o usuário recarregou (saldo subiu acima do limite) e depois caiu de
    volta, ESSE caso é desejável realertar. Mas o nosso estado guarda só o
    último valor que disparou — se subiu pra R$500 e caiu pra R$190 (delta
    de R$0 desde o último alerta de R$190), nosso dedupe NÃO realerta.

    Trade-off conhecido: priorizamos não-spam. Quem fizer recargas frequentes
    e quiser re-alarme imediato precisa esperar a janela de 12h, OU o saldo
    cair muito mais. Trava esse comportamento explicitamente — se o dono
    pedir pra mudar, sai daqui."""
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '180.00')  # alerta
        _enviar_saldo(c, '500.00')  # recarga
        _enviar_saldo(c, '180.00')  # caiu de novo — mesmo nível
    # Mesma trava: 1 alerta só. O 3o evento tem saldo == ultimo alertado.
    assert send.call_count == 1


def test_alerta_pode_ser_desligado_por_env(app, monkeypatch):
    """Env LALAMOVE_SALDO_ALERTA=0 desliga o alerta — útil em janela de
    manutenção ou se o dono quiser silenciar temporariamente sem deploy."""
    _config(app)
    monkeypatch.setenv('LALAMOVE_SALDO_ALERTA', '0')
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        _enviar_saldo(c, '50.00')
    send.assert_not_called()


def test_saldo_ainda_e_persistido_mesmo_se_zapi_falhar(app):
    """Best-effort do alerta: se Z-API estiver fora, o saldo ainda tem que
    ser persistido (essa é a fonte de verdade do painel)."""
    from app.extensions import db
    from app.models import LalamoveSaldo
    _config(app)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                side_effect=RuntimeError('zapi down')):
        r = _enviar_saldo(c, '120.00')
    assert r.status_code == 200
    with app.app_context():
        s = db.session.get(LalamoveSaldo, 1)
        assert s is not None
        assert s.valor == Decimal('120.00')


def test_alerta_sem_destino_zapi_apenas_loga(app):
    """Sem ZAPI_BOT_DONO_NUMERO nem ZAPI_NUMERO_DESTINO: o servico abandona
    silenciosamente (loga warning), não levanta excecao."""
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    # SEM número de destino
    app.config.pop('ZAPI_BOT_DONO_NUMERO', None)
    app.config.pop('ZAPI_NUMERO_DESTINO', None)
    c = app.test_client()
    with patch('app.services.zapi.enviar_texto',
                return_value={'ok': True}) as send:
        r = _enviar_saldo(c, '50.00')
    assert r.status_code == 200
    send.assert_not_called()


def test_limite_constante_centralizada_em_constants():
    """Trava: o valor de R$200 é constante única em app/constants.py
    (compartilhado entre serviço e mensagem do WhatsApp). Sem isso ele
    duplica e diverge — bug clássico."""
    from app.constants import LALAMOVE_SALDO_MIN_REAIS
    assert LALAMOVE_SALDO_MIN_REAIS == 200
