"""Recuperação explícita de ordens: autorização, preservação e diagnóstico."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app.extensions import db
from app.models import AppConfig, PlanejamentoItem, PlanejamentoProducao, Receita, Usuario
from app.services.auto_pedidos import STATUS_ENVIO_PLANO_KEY
from app.utils import hoje


def _cliente(app, usuario=None):
    # A fixture mantém um app_context entre requests; cada cliente tem identidade própria.
    g.pop('_login_user', None)
    cliente = app.test_client()
    if usuario:
        with cliente.session_transaction() as sessao:
            sessao['_user_id'] = str(usuario.id)
            sessao['_fresh'] = True
    return cliente


def test_recuperar_exige_admin_e_post(app, admin_user, monkeypatch):
    chamadas = []
    monkeypatch.setattr('app.services.auto_pedidos.enviar_ordens_da_semana',
                        lambda **kw: chamadas.append(kw))
    url = '/telaindustriateste/recuperar-ordens'
    assert _cliente(app).post(url).status_code == 302
    usuario = Usuario(nome='Padeiro', login='padeiro-recuperar', papel='padeiro')
    usuario.set_senha('teste-local')
    db.session.add(usuario)
    db.session.commit()
    assert _cliente(app, usuario).post(url).status_code == 403
    assert _cliente(app, admin_user).get(url).status_code == 405
    assert chamadas == []


def test_recuperar_exige_csrf(app, admin_user, monkeypatch):
    app.config['WTF_CSRF_ENABLED'] = True
    chamadas = []
    monkeypatch.setattr('app.services.auto_pedidos.enviar_ordens_da_semana',
                        lambda **kw: chamadas.append(kw))
    assert _cliente(app, admin_user).post(
        '/telaindustriateste/recuperar-ordens', json={}).status_code == 400
    assert chamadas == []


def test_recuperar_preserva_enviados_e_rascunho_humano(
        app, admin_user, monkeypatch, congela_hoje):
    congela_hoje()
    rec = Receita(nome='Cookie de recuperação', rendimento_qtd=1,
                  rendimento_unidade='un', peso_base=100)
    db.session.add(rec)
    db.session.flush()
    preservados = []
    for offset, enviado, autor in [(0, True, None), (1, True, None),
                                    (2, False, admin_user.id)]:
        plano = PlanejamentoProducao(
            data=hoje() + timedelta(days=offset), origem='cronograma',
            enviado_ao_padeiro=enviado, criado_por=autor, status='aprovado')
        plano.itens.append(PlanejamentoItem(receita=rec, qtd_alvo=7))
        db.session.add(plano)
        preservados.append(plano)
    db.session.commit()
    monkeypatch.setattr('app.services.previsao_producao.cronograma_producao',
                        lambda **kw: {'receitas': [{'receita_id': rec.id, 'por_dia': [
                            {'data': (hoje() + timedelta(days=i)).isoformat(), 'qtd': 3}
                            for i in range(7)]}]})
    resposta = _cliente(app, admin_user).post(
        '/telaindustriateste/recuperar-ordens', data={'legacy': '1'})
    assert resposta.status_code == 302
    assert 'legacy=1' in resposta.location
    db.session.expire_all()
    assert [p.itens[0].qtd_alvo for p in preservados] == [7, 7, 7]
    assert preservados[2].enviado_ao_padeiro is False
    nova = PlanejamentoProducao.query.filter_by(data=hoje() + timedelta(days=3)).one()
    assert nova.enviado_ao_padeiro and nova.itens[0].qtd_alvo == 3


@pytest.mark.parametrize('visao', ['?v2=1', '?legacy=1'])
def test_falha_visivel_escapada_com_ficha_exata(app, admin_user, visao):
    rec = Receita(nome='Pão do teste', rendimento_qtd=1,
                  rendimento_unidade='un', peso_base=100)
    db.session.add(rec)
    db.session.flush()
    status = {'falhas': [
        {'data': hoje().isoformat(), 'erro': rec.nome + ': <script>erro</script>'},
        {'data': (hoje() - timedelta(days=1)).isoformat(), 'erro': 'Erro antigo oculto'},
    ]}
    AppConfig.set(STATUS_ENVIO_PLANO_KEY, json.dumps(status))
    db.session.commit()
    antes = AppConfig.get(STATUS_ENVIO_PLANO_KEY)
    resposta = _cliente(app, admin_user).get('/telaindustriateste/' + visao)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert 'Enviar ordens pendentes' in html and 'Envio bloqueado' in html
    assert '&lt;script&gt;erro&lt;/script&gt;' in html
    assert '<script>erro</script>' not in html
    assert f'href="/receitas/{rec.id}"' in html
    assert 'Erro antigo oculto' not in html
    assert AppConfig.get(STATUS_ENVIO_PLANO_KEY) == antes


def test_envio_humano_limpa_apenas_falha_resolvida(app, admin_user, monkeypatch):
    status = {'falhas': [
        {'data': hoje().isoformat(), 'erro': 'Falha resolvida'},
        {'data': (hoje() + timedelta(days=1)).isoformat(), 'erro': 'Outra falha'},
    ]}
    AppConfig.set(STATUS_ENVIO_PLANO_KEY, json.dumps(status))
    db.session.commit()
    monkeypatch.setattr('app.services.producao.enviar_plano_do_dia',
                        lambda *args, **kw: SimpleNamespace(itens=[1]))
    resposta = _cliente(app, admin_user).post('/telaindustriateste/enviar',
                                             data={'data': hoje().isoformat()})
    assert resposta.status_code == 302
    salvo = json.loads(AppConfig.get(STATUS_ENVIO_PLANO_KEY))
    assert salvo['falhas'] == [status['falhas'][1]]


@pytest.mark.parametrize('obtida', [True, False])
def test_trava_pg_libera_mesma_conexao_apos_erro(app, monkeypatch, obtida):
    from app.blueprints.industria_teste import routes

    chamadas = []

    class Conexao:
        def execute(self, comando, parametros):
            chamadas.append((str(comando), parametros))
            return SimpleNamespace(scalar=lambda: obtida)

        def close(self):
            chamadas.append('close')

    conexao = Conexao()
    engine = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'),
                             connect=lambda: conexao)
    monkeypatch.setattr(routes, 'db', SimpleNamespace(engine=engine))
    with pytest.raises(ValueError, match='controlado'):
        with routes._trava_recuperacao_ordens() as valor:
            assert valor is obtida
            raise ValueError('controlado')
    assert chamadas[0] == ('SELECT pg_try_advisory_lock(:k)', {'k': 7759})
    assert chamadas[-1] == 'close'
    assert len(chamadas) == (3 if obtida else 2)
    if obtida:
        assert chamadas[1] == ('SELECT pg_advisory_unlock(:k)', {'k': 7759})
