"""A promoção exige revisar e confirmar a mesma decisão, sem perder vínculos."""
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy.exc import OperationalError

from app.extensions import db
from app.models import Cargo, Funcionario, PlanoCarreiraFaixa, RhMovimentacao, Usuario
from tests.test_rh_equipe import _client, _dados


class _Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name'):
            self.values[attrs['name']] = attrs.get('value', '')


class _Forms(HTMLParser):
    """Lê os campos enviados mesmo quando o botão de submit é desabilitado."""
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.form = None
        self.button = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.form = {'attrs': attrs, 'hidden': {}, 'buttons': []}
        elif self.form is not None:
            if tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name'):
                self.form['hidden'][attrs['name']] = attrs.get('value', '')
            elif tag == 'button':
                self.button = []

    def handle_data(self, data):
        if self.button is not None:
            self.button.append(data)

    def handle_endtag(self, tag):
        if tag == 'button' and self.button is not None:
            self.form['buttons'].append(' '.join(''.join(self.button).split()))
            self.button = None
        elif tag == 'form' and self.form is not None:
            self.forms.append(self.form)
            self.form = None


@pytest.fixture
def cenario(app, congela_hoje):
    congela_hoje(2026, 9, 10)
    pessoa, inativa, lider, loja = _dados()
    usuario = Usuario(nome=pessoa.nome, login='ana-promocao',
                      senha_hash='fixture', papel='funcionario', somente_treino=True)
    db.session.add(usuario)
    pessoa.usuario = usuario
    pessoa.salario_base = 1900  # Cache legado: a prévia deve usar o cargo real.
    pessoa.funcao = pessoa.cargo.nome
    pessoa.premiacao = 500
    pessoa.tem_cargo_confianca = True
    pessoa.cargo_confianca = 19
    pessoa.hora_extra_pct = 60
    pessoa.horas_extras = 4
    pessoa.vt_dia = 10.8
    pessoa.vr_dia = 22
    pessoa.dias_trabalhados = 25
    pessoa.email = 'ana@example.test'
    pessoa.telefone = '11911112222'
    pessoa.observacao = 'Observação do cadastro'
    pessoa.funcao_operacional = 'Atendente chefe'
    faixa = PlanoCarreiraFaixa.query.one()
    faixa.salario_nivel = 2500
    faixa.equivalente_mensal = 2500
    faixa.complemento_funcao = 300
    faixa.total_alvo = 2800
    pessoa.enquadramento_carreira.nivel = 1
    pessoa.enquadramento_carreira.total_alvo = 2330.4
    db.session.commit()
    return {
        'pessoa': pessoa, 'inativa': inativa, 'lider': lider, 'loja': loja,
        'cargo': pessoa.cargo, 'destino': inativa.cargo, 'faixa': faixa,
        'url': f'/rh/funcionarios/{pessoa.id}/promover',
        'payload': {'etapa': 'revisar', 'cargo_id': str(inativa.cargo_id),
                    'data_efetiva': '2026-08-20', 'observacao': 'Decisão do dono'},
    }


def _token(resposta):
    assert resposta.status_code == 200
    token = _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    assert token, 'A prévia precisa oferecer uma confirmação assinada.'
    return token


def _preservados(pessoa):
    campos = ('nome', 'cpf', 'ativo', 'data_admissao', 'data_demissao',
              'usuario_id', 'lider_id', 'periodo', 'funcao_operacional',
              'premiacao', 'tem_cargo_confianca', 'cargo_confianca',
              'hora_extra_pct', 'horas_extras', 'vt_dia', 'vr_dia',
              'dias_trabalhados', 'email', 'telefone', 'observacao')
    return {
        **{campo: getattr(pessoa, campo) for campo in campos},
        'lojas': tuple(sorted(loja.id for loja in pessoa.lojas)),
        'acesso': (pessoa.usuario.papel, pessoa.usuario.somente_treino,
                   pessoa.usuario.senha_hash),
    }


def test_acesso_direto_na_equipe_e_historico_somente_para_ativos(app, owner_user, cenario):
    client = _client(app, owner_user)
    html = client.get('/rh/equipe?ativos=0').get_data(as_text=True)
    assert f'href="{cenario["url"]}"' in html
    assert f'href="/rh/funcionarios/{cenario["inativa"].id}/promover"' not in html
    assert 'Promover' in html
    historico = client.get(f'/rh/funcionarios/{cenario["pessoa"].id}/carreira')
    assert f'href="{cenario["url"]}"' in historico.get_data(as_text=True)
    assert client.get(cenario['url']).status_code == 200
    assert RhMovimentacao.query.count() == 0


def test_previa_exibe_valores_e_nao_altera_funcionario_ou_plano(app, owner_user, cenario):
    client = _client(app, owner_user)
    pessoa = cenario['pessoa']
    antes = _preservados(pessoa)
    cargo_id = pessoa.cargo_id
    plano = pessoa.enquadramento_carreira
    antes_plano = (plano.nivel, plano.total_alvo, plano.decisao)
    resposta = client.post(cenario['url'], data=cenario['payload'])
    _token(resposta)
    html = resposta.get_data(as_text=True)
    for valor in ('2.330,40', '2.500,00', '500,00', '932,16', '1.000,00',
                  '3.762,56', '4.000,00'):
        assert valor in html
    assert 'Confirmar promoção' in html
    db.session.expire_all()
    assert pessoa.cargo_id == cargo_id and pessoa.salario_base == 1900
    assert _preservados(pessoa) == antes
    assert (plano.nivel, plano.total_alvo, plano.decisao) == antes_plano
    assert RhMovimentacao.query.count() == 0


def test_confirmar_atualiza_vinculos_historico_e_preserva_demais_dados(app, owner_user, cenario):
    client = _client(app, owner_user)
    pessoa, destino = cenario['pessoa'], cenario['destino']
    antes = _preservados(pessoa)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    resposta = client.post(cenario['url'], data={
        'etapa': 'confirmar', 'confirmacao': token,
        # Um campo inesperado não pode alterar o que foi revisado.
        'cargo_id': str(cenario['cargo'].id), 'data_efetiva': '2020-01-02',
        'observacao': 'Texto adulterado', 'premiacao': '0', 'ativo': '0',
    })
    assert resposta.status_code == 302
    retorno = urlsplit(resposta.location)
    assert retorno.path == '/rh/equipe'
    assert parse_qs(retorno.query)['q'] == [pessoa.nome]
    db.session.expire_all()
    assert pessoa.cargo_id == destino.id
    assert pessoa.cargo is destino and pessoa.funcao == destino.nome
    assert pessoa.salario_base == 2500 and pessoa.salario_efetivo() == 2500
    assert _preservados(pessoa) == antes
    assert cenario['cargo'].salario_base == 2330.4
    plano = pessoa.enquadramento_carreira
    assert (plano.familia, plano.nivel, plano.cargo_proposto, plano.decisao) == (
        'Atendimento', 2, 'Atendente 2', 'Aprovado')
    assert (plano.salario_base_alvo, plano.complemento_funcao_alvo, plano.total_alvo) == (
        2500, 300, 2800)
    registro = RhMovimentacao.query.one()
    assert registro.funcionario_id == pessoa.id
    assert registro.tipo == 'promocao' and registro.origem == 'promocao_equipe'
    assert registro.cargo_anterior_id == cenario['cargo'].id
    assert registro.cargo_novo_id == destino.id
    assert (registro.nivel_anterior, registro.nivel_novo) == (1, 2)
    assert registro.data_efetiva == date(2026, 8, 20)
    assert registro.registrado_por_id == owner_user.id
    assert registro.observacao == 'Decisão do dono'
    html = client.get(resposta.location).get_data(as_text=True)
    assert '20/08/2026' in html and 'Atendente 2' in html
    assert client.post(cenario['url'], data={
        'etapa': 'confirmar', 'confirmacao': token}).status_code == 400
    assert RhMovimentacao.query.count() == 1


@pytest.mark.parametrize('etapa', ('revisar', 'confirmar'))
def test_promocao_restrita_ao_dono(app, owner_user, admin_user, cenario, etapa):
    dono = _client(app, owner_user)
    token = _token(dono.post(cenario['url'], data=cenario['payload']))
    admin = _client(app, admin_user)
    assert admin.get(cenario['url']).status_code == 403
    payload = cenario['payload'] | {'etapa': etapa, 'confirmacao': token}
    assert admin.post(cenario['url'], data=payload).status_code == 403
    assert _client(app).get(cenario['url']).status_code in (302, 401)
    assert RhMovimentacao.query.count() == 0


def test_inativo_redireciona_sem_oferecer_ou_aplicar_promocao(app, owner_user, cenario):
    client = _client(app, owner_user)
    pessoa = cenario['inativa']
    url = f'/rh/funcionarios/{pessoa.id}/promover'
    resposta = client.get(url)
    assert resposta.status_code == 302
    assert urlsplit(resposta.location).path == f'/rh/funcionarios/{pessoa.id}/carreira'
    assert client.post(url, data=cenario['payload']).status_code in (302, 400)
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('alteracao', (
    {'cargo_id': ''}, {'cargo_id': 'invalido'}, {'cargo_id': '999999'},
    {'data_efetiva': ''}, {'data_efetiva': '2026-02-30'},
    {'data_efetiva': '2999-01-01'}, {'data_efetiva': '2019-12-31'},
    {'observacao': 'x' * 2001},
))
def test_dados_invalidos_nao_geram_confirmacao_ou_mudancas(app, owner_user, cenario, alteracao):
    pessoa = cenario['pessoa']
    antes = (pessoa.cargo_id, pessoa.salario_base, pessoa.enquadramento_carreira.decisao)
    resposta = _client(app, owner_user).post(cenario['url'], data=cenario['payload'] | alteracao)
    assert resposta.status_code == 400
    assert not _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    assert (pessoa.cargo_id, pessoa.salario_base, pessoa.enquadramento_carreira.decisao) == antes
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('token', ('', 'token-invalido'))
def test_confirmacao_ausente_ou_adulterada_nao_aplica(app, owner_user, cenario, token):
    resposta = _client(app, owner_user).post(cenario['url'], data={
        'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert cenario['pessoa'].cargo_id == cenario['cargo'].id
    assert RhMovimentacao.query.count() == 0


def test_confirmacao_expira_apos_trinta_minutos(app, owner_user, cenario, monkeypatch):
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    futuro = TimestampSigner.get_timestamp(None) + 1801
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: futuro)
    resposta = client.post(cenario['url'], data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert cenario['pessoa'].cargo_id == cenario['cargo'].id
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('outro', ('funcionario', 'autor'))
def test_token_vinculado_ao_funcionario_e_ao_dono_da_previa(app, owner_user, cenario, outro):
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    url = cenario['url']
    if outro == 'funcionario':
        url = f'/rh/funcionarios/{cenario["lider"].id}/promover'
    else:
        segundo_dono = Usuario(nome='Outro dono', login='outro-dono', senha_hash='fixture',
                               papel='admin', is_owner=True)
        db.session.add(segundo_dono)
        db.session.commit()
        client = _client(app, segundo_dono)
    resposta = client.post(url, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert cenario['pessoa'].cargo_id == cenario['cargo'].id
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('mudanca', (
    'salario_destino', 'salario_atual', 'cargo_funcionario', 'premiacao',
    'confianca', 'enquadramento', 'faixa', 'cargo_inativado', 'pessoa_inativada',
))
def test_estado_alterado_exige_nova_revisao(app, owner_user, cenario, mudanca):
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    pessoa = cenario['pessoa']
    if mudanca == 'salario_destino':
        cenario['destino'].salario_base = 2700
    elif mudanca == 'salario_atual':
        cenario['cargo'].salario_base = 2400
    elif mudanca == 'cargo_funcionario':
        cargo = Cargo(nome='Outro cargo', salario_base=2450)
        db.session.add(cargo)
        pessoa.cargo = cargo
    elif mudanca == 'premiacao':
        pessoa.premiacao = 900
    elif mudanca == 'confianca':
        pessoa.tem_cargo_confianca = False
    elif mudanca == 'enquadramento':
        pessoa.enquadramento_carreira.nivel = 3
        pessoa.enquadramento_carreira.total_alvo = 3300
    elif mudanca == 'faixa':
        cenario['faixa'].equivalente_mensal = 2700
    elif mudanca == 'cargo_inativado':
        cenario['destino'].ativo = False
    elif mudanca == 'pessoa_inativada':
        pessoa.ativo = False
    db.session.commit()
    cargo_antes = pessoa.cargo_id
    estado_antes = _preservados(pessoa)
    resposta = client.post(cenario['url'], data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400 or (
        mudanca == 'pessoa_inativada' and resposta.status_code == 302)
    db.session.expire_all()
    assert pessoa.cargo_id == cargo_antes
    assert _preservados(pessoa) == estado_antes
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('etapa', ('revisar', 'confirmar'))
def test_csrf_obrigatorio_na_previa_e_na_confirmacao(app, owner_user, cenario, etapa):
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = client.post(cenario['url'], data=cenario['payload'] | {
        'etapa': etapa, 'confirmacao': token})
    assert resposta.status_code == 302 and resposta.location == '/'
    with client.session_transaction() as sessao:
        assert any('token-ausente' in texto for _, texto in sessao['_flashes'])
    assert cenario['pessoa'].cargo_id == cenario['cargo'].id
    assert RhMovimentacao.query.count() == 0


def test_falha_no_historico_reverte_cargo_e_plano(app, owner_user, cenario, monkeypatch):
    from app.services import rh_movimentacao
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    pessoa = cenario['pessoa']
    antes = (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base,
             pessoa.enquadramento_carreira.nivel, pessoa.enquadramento_carreira.decisao)

    def falhar(*args, **kwargs):
        raise ValueError('Não foi possível registrar o histórico.')

    monkeypatch.setattr(rh_movimentacao, 'registrar_mudanca', falhar)
    resposta = client.post(cenario['url'], data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    db.session.expire_all()
    pessoa = db.session.get(Funcionario, pessoa.id)
    assert (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base,
            pessoa.enquadramento_carreira.nivel, pessoa.enquadramento_carreira.decisao) == antes
    assert RhMovimentacao.query.count() == 0


def test_formulario_real_confirma_apenas_com_campos_hidden(app, owner_user, cenario):
    app.config['WTF_CSRF_ENABLED'] = True
    client = _client(app, owner_user)
    escolha = client.get(cenario['url'])
    csrf = _Inputs(escolha.get_data(as_text=True)).values['csrf_token']
    previa = client.post(cenario['url'], data=cenario['payload'] | {'csrf_token': csrf})
    assert previa.status_code == 200
    formularios = _Forms(previa.get_data(as_text=True)).forms
    confirmar = next(form for form in formularios if 'Confirmar promoção' in form['buttons'])
    assert confirmar['attrs']['method'].lower() == 'post'
    assert confirmar['hidden']['etapa'] == 'confirmar'
    assert confirmar['hidden']['confirmacao']
    assert confirmar['hidden']['csrf_token']
    # O loading global desabilita o botão; ele deixa de ser um controle enviado.
    resposta = client.post(confirmar['attrs'].get('action') or cenario['url'],
                           data=confirmar['hidden'])
    assert resposta.status_code == 302
    assert cenario['pessoa'].cargo_id == cenario['destino'].id
    assert RhMovimentacao.query.one().origem == 'promocao_equipe'


@pytest.mark.parametrize(('atributo', 'codigo'), (
    ('pgcode', '40P01'), ('sqlstate', '40001'),
))
def test_conflito_transacional_reverte_e_pede_nova_revisao(
        app, owner_user, cenario, monkeypatch, atributo, codigo):
    from app.services import rh_promocao
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    pessoa = cenario['pessoa']
    plano = pessoa.enquadramento_carreira
    antes = (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base,
             plano.nivel, plano.decisao, plano.total_alvo, plano.salario_base_alvo)
    aplicar = rh_promocao.aplicar
    origem = Exception('Conflito transacional simulado')
    setattr(origem, atributo, codigo)

    def aplicar_e_falhar(*args, **kwargs):
        aplicar(*args, **kwargs)
        db.session.flush()
        assert RhMovimentacao.query.count() == 1
        raise OperationalError(None, None, origem)

    monkeypatch.setattr(rh_promocao, 'aplicar', aplicar_e_falhar)
    resposta = client.post(cenario['url'], data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    html = resposta.get_data(as_text=True)
    assert 'Outro cadastro foi atualizado ao mesmo tempo. Revise e confirme a promoção novamente.' in html
    assert not _Inputs(html).values.get('confirmacao')
    db.session.expire_all()
    assert (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base,
            plano.nivel, plano.decisao, plano.total_alvo, plano.salario_base_alvo) == antes
    assert RhMovimentacao.query.count() == 0


def test_erro_de_banco_nao_relacionado_a_concorrencia_continua_visivel(
        app, owner_user, cenario, monkeypatch):
    from app.services import rh_promocao
    client = _client(app, owner_user)
    token = _token(client.post(cenario['url'], data=cenario['payload']))
    origem = Exception('Conexão indisponível')
    origem.pgcode = '08006'
    erro = OperationalError(None, None, origem)

    def falhar(*args, **kwargs):
        raise erro

    monkeypatch.setattr(rh_promocao, 'aplicar', falhar)
    with pytest.raises(OperationalError) as recebido:
        client.post(cenario['url'], data={'etapa': 'confirmar', 'confirmacao': token})
    assert recebido.value is erro
    db.session.rollback()
    assert cenario['pessoa'].cargo_id == cenario['cargo'].id
    assert RhMovimentacao.query.count() == 0
