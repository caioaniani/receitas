"""A carga inicial conserva dados sem data e a origem de cada medida."""
import csv
import io

from app.extensions import db
from app.models import ProducaoDiarioLote, ProducaoDiarioReferencia, Usuario


def _login(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def test_consulta_nao_importa_e_carga_preserva_dez_originais(app, admin_user):
    client = _login(app, admin_user)
    resposta = client.get('/padeiro/diario/referencias')
    assert resposta.status_code == 200
    assert ProducaoDiarioReferencia.query.count() == 0
    assert client.post('/padeiro/diario/referencias/importar').status_code == 302
    assert ProducaoDiarioReferencia.query.count() == 10
    assert ProducaoDiarioLote.query.count() == 0
    assert all(r.importado_por_id == admin_user.id for r in ProducaoDiarioReferencia.query.all())
    html = client.get('/padeiro/diario/referencias').get_data(as_text=True)
    assert 'Tradicional' in html and 'Integral' in html and 'Nozes' in html
    assert 'calculad' in html.lower() and 'extrapolad' in html.lower()
    assert client.post('/padeiro/diario/referencias/importar').status_code == 302
    assert ProducaoDiarioReferencia.query.count() == 10
    # Os registros sem data continuam acessíveis mesmo fora do filtro de dias.
    html = client.get('/padeiro/diario/historico?inicio=2025-01-01&fim=2025-01-07').get_data(as_text=True)
    assert '/padeiro/diario/referencias' in html


def test_csv_preserva_lacunas_duracoes_percentuais_e_origem(app, admin_user):
    client = _login(app, admin_user)
    client.post('/padeiro/diario/referencias/importar')
    resposta = client.get('/padeiro/diario/referencias/exportar.csv')
    assert resposta.status_code == 200
    dados = list(csv.DictReader(io.StringIO(resposta.get_data(as_text=True).lstrip('\ufeff')), delimiter=';'))
    assert len(dados) == 10
    por_id = {r['ID na planilha']: r for r in dados}
    assert por_id['1']['Hidratação (%)'] in ('80', '80,0')
    assert por_id['1']['Água calculada (kg) — origem'] == 'calculado'
    assert por_id['1']['Dobras (qtd) — origem'] == 'extrapolado'
    assert por_id['8']['Velocidade 1 (min)'] == ''
    assert por_id['8']['Velocidade 2 (min)'] == ''
    assert por_id['9']['Farinha (kg)'] == ''
    assert por_id['9']['Velocidade 1 (min)'] == '24'
    assert all(r['Data do lote'] == '' for r in dados)
    assert all(r['Aba'] == 'Registros' for r in dados)


def test_padeiro_consulta_historico_mas_carga_e_administrativa(app, admin_user):
    user = Usuario(nome='Padeiro do histórico', login='padeiro-historico', papel='padeiro')
    user.set_senha('teste')
    db.session.add(user)
    db.session.commit()
    client = _login(app, user)
    assert client.get('/padeiro/diario/referencias').status_code == 200
    assert client.post('/padeiro/diario/referencias/importar').status_code == 403
    assert ProducaoDiarioReferencia.query.count() == 0
    _login(app, admin_user).post('/padeiro/diario/referencias/importar')
    assert client.get('/padeiro/diario/referencias').status_code == 200
    assert client.get('/padeiro/diario/referencias/exportar.csv').status_code == 200


def test_carga_exige_csrf(app, admin_user):
    client = _login(app, admin_user)
    app.config['WTF_CSRF_ENABLED'] = True
    assert client.post('/padeiro/diario/referencias/importar').status_code == 302
    assert ProducaoDiarioReferencia.query.count() == 0
