"""A aprovação em lote exige seleção e revisão explícitas, com gravação atômica."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from itsdangerous import TimestampSigner
from sqlalchemy.exc import OperationalError

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    RhMovimentacao,
    Usuario,
)
from tests.test_rh_equipe import _client, _dados
from tests.test_rh_promocao_routes import _Forms, _Inputs, _preservados

URL = '/rh/plano-carreira/aprovar-lote'


class _Pagina(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = []
        self.links = []
        self.forms = []
        self.depth = 0
        self.max_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input':
            self.inputs.append(attrs)
        elif tag == 'a':
            self.links.append(attrs.get('href', ''))
        elif tag == 'form':
            self.forms.append(attrs)
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.depth -= 1

    @property
    def selecionaveis(self):
        return [item for item in self.inputs
                if item.get('name') == 'enquadramento_ids'
                and item.get('type') == 'checkbox' and 'disabled' not in item]


@pytest.fixture
def cenario(app):
    ana, inativa, lider, loja = _dados()
    plano = ana.enquadramento_carreira
    faixa = PlanoCarreiraFaixa.query.one()
    faixa.salario_nivel = 2500
    faixa.equivalente_mensal = 2500
    faixa.total_alvo = 2800
    faixa.complemento_funcao = 300
    plano.salario_base_atual = 2000
    plano.total_atual = 2000
    plano.salario_base_alvo = 2500
    plano.total_alvo = 2800
    plano.complemento_funcao_alvo = 300
    ana.salario_base = 1900
    ana.funcao = ana.cargo.nome
    ana.premiacao = 500
    ana.tem_cargo_confianca = True
    ana.cargo_confianca = 19
    ana.hora_extra_pct = 60
    ana.horas_extras = 4
    ana.vt_dia = 10.8
    ana.vr_dia = 22
    ana.dias_trabalhados = 25
    ana.email = 'ana@example.test'
    ana.telefone = '11911112222'
    ana.observacao = 'Observação preservada'
    ana.funcao_operacional = 'Atendente chefe'
    usuario = Usuario(nome=ana.nome, login='ana-lote', senha_hash='fixture',
                      papel='funcionario', somente_treino=True)
    db.session.add(usuario)
    ana.usuario = usuario

    def adicionar(nome, cpf, *, ativo=True, decisao=None, nivel=2,
                  familia='Atendimento', cargo=None):
        pessoa = Funcionario(nome=nome, cpf=cpf, ativo=ativo,
                             cargo=cargo or ana.cargo, funcao=(cargo or ana.cargo).nome,
                             salario_base=(cargo or ana.cargo).salario_base)
        db.session.add(pessoa)
        db.session.flush()
        item = PlanoCarreiraEnquadramento(
            importacao_id=plano.importacao_id, funcionario=pessoa,
            familia=familia, nivel=nivel, cargo_proposto='Atendente 2',
            salario_base_alvo=2500, total_alvo=2800, decisao=decisao)
        db.session.add(item)
        return item

    mesmo_cargo = adicionar('Bruno do lote', '72000000001', cargo=inativa.cargo)
    fora = adicionar('Carlos fora da trilha', '72000000002', nivel=None,
                     familia='Fora da trilha')
    fora.cargo_proposto = 'Gerente externo'
    fora.salario_base_alvo = 9000
    fora.total_alvo = 9500
    bloqueado = adicionar('Dora inativa', '72000000003', ativo=False)
    aprovado = adicionar('Eva aprovada', '72000000004', decisao='Aprovado')
    sem_faixa = adicionar('Fabio sem faixa', '72000000005', nivel=5)
    db.session.commit()
    return {
        'ana': ana, 'plano': plano, 'mesmo_cargo': mesmo_cargo,
        'fora': fora, 'inativo': bloqueado, 'aprovado': aprovado,
        'sem_faixa': sem_faixa, 'origem': ana.cargo,
        'destino': inativa.cargo, 'faixa': faixa,
        'ids': [str(plano.id), str(mesmo_cargo.id)],
    }


def _revisar(client, ids, **extra):
    return client.post(URL, data={
        'etapa': 'revisar', 'enquadramento_ids': ids, **extra})


def _token(resposta):
    assert resposta.status_code == 200
    token = _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    assert token, 'A revisão precisa oferecer uma confirmação assinada.'
    return token


def _estado(cenario):
    return {
        chave: (item.decisao, item.funcionario.cargo_id,
                item.funcionario.funcao, item.funcionario.salario_base,
                item.familia, item.nivel, item.total_alvo)
        for chave, item in cenario.items()
        if isinstance(item, PlanoCarreiraEnquadramento)
    }


def test_lista_seleciona_apenas_linhas_elegiveis_visiveis_sem_preselecao(
        app, owner_user, cenario):
    client = _client(app, owner_user)
    pagina = _Pagina(client.get('/rh/plano-carreira').get_data(as_text=True))
    assert {item['value'] for item in pagina.selecionaveis} == {
        *cenario['ids'], str(cenario['fora'].id)}
    assert all('checked' not in item for item in pagina.selecionaveis)
    assert all(item.get('form') == 'aprovar-lote-form' for item in pagina.selecionaveis)
    assert pagina.max_depth == 1, 'Os formulários individuais não podem ficar aninhados.'
    assert any(item.get('id') == 'selecionar-visiveis' for item in pagina.inputs)
    filtrada = _Pagina(client.get('/rh/plano-carreira', query_string={
        'q': 'Bruno', 'familia': 'Atendimento'}).get_data(as_text=True))
    assert [item['value'] for item in filtrada.selecionaveis] == [str(cenario['mesmo_cargo'].id)]
    assert RhMovimentacao.query.count() == 0


def test_retorno_restaura_apenas_selecao_valida_dentro_do_filtro(app, owner_user, cenario):
    resposta = _client(app, owner_user).get('/rh/plano-carreira', query_string={
        'q': 'Bruno', 'selecionados': [*cenario['ids'], 'invalido', '999999']})
    pagina = _Pagina(resposta.get_data(as_text=True))
    assert [(item['value'], 'checked' in item) for item in pagina.selecionaveis] == [
        (str(cenario['mesmo_cargo'].id), True)]


def test_previa_nao_grava_e_mostra_pessoas_cargos_e_valores(app, owner_user, cenario):
    client = _client(app, owner_user)
    antes = _estado(cenario)
    extras = _preservados(cenario['ana'])
    resposta = _revisar(client, cenario['ids'])
    _token(resposta)
    html = resposta.get_data(as_text=True)
    for texto in (cenario['ana'].nome, 'Bruno do lote', 'Atendente 2',
                  '2.330,40', '2.500,00'):
        assert texto in html
    db.session.expire_all()
    assert _estado(cenario) == antes
    assert _preservados(cenario['ana']) == extras
    assert RhMovimentacao.query.count() == 0


def test_confirmacao_aplica_apenas_selecao_revisada_preservando_vinculos_e_auditoria(
        app, owner_user, cenario):
    client = _client(app, owner_user)
    extras = _preservados(cenario['ana'])
    token = _token(_revisar(client, cenario['ids'], q='Ana', familia='Atendimento'))
    resposta = client.post(URL, data={
        'etapa': 'confirmar', 'confirmacao': token,
        'enquadramento_ids': [str(cenario['fora'].id)],
        'q': 'Texto adulterado', 'familia': 'Fora da trilha',
        'voltar': 'https://example.test', 'premiacao': '0', 'ativo': '0',
    })
    assert resposta.status_code == 302
    destino = urlsplit(resposta.location)
    assert destino.path == '/rh/plano-carreira'
    assert destino.fragment == 'enquadramento-equipe'
    assert parse_qs(destino.query) == {'q': ['Ana'], 'familia': ['Atendimento']}
    db.session.expire_all()
    assert cenario['plano'].decisao == 'Aprovado'
    assert cenario['mesmo_cargo'].decisao == 'Aprovado'
    assert cenario['fora'].decisao is None
    assert cenario['ana'].cargo_id == cenario['destino'].id
    assert cenario['ana'].funcao == cenario['destino'].nome
    assert cenario['ana'].salario_base == 2500
    assert _preservados(cenario['ana']) == extras
    assert cenario['origem'].salario_base == 2330.4
    registros = RhMovimentacao.query.order_by(RhMovimentacao.id).all()
    assert len(registros) == 2  # Aprovar um cargo já vinculado também deixa histórico.
    assert {r.funcionario_id for r in registros} == {
        cenario['ana'].id, cenario['mesmo_cargo'].funcionario_id}
    assert all(r.tipo == 'aplicacao_plano' and r.origem == 'aprovacao_lote'
               and r.registrado_por_id == owner_user.id for r in registros)
    assert all(r.data_efetiva is None for r in registros)
    assert client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token}).status_code == 400
    assert RhMovimentacao.query.count() == 2


def test_fora_da_trilha_registra_decisao_sem_aplicar_salario_proposto(app, owner_user, cenario):
    client = _client(app, owner_user)
    fora = cenario['fora']
    pessoa = fora.funcionario
    antes = (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base, pessoa.salario_efetivo())
    token = _token(_revisar(client, [str(fora.id)]))
    resposta = client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 302
    db.session.expire_all()
    assert fora.decisao == 'Aprovado'
    assert (pessoa.cargo_id, pessoa.funcao, pessoa.salario_base, pessoa.salario_efetivo()) == antes
    registro = RhMovimentacao.query.one()
    assert registro.tipo == 'aplicacao_plano' and registro.origem == 'aprovacao_lote'
    assert registro.cargo_anterior_id == registro.cargo_novo_id == antes[0]


@pytest.mark.parametrize('etapa', ('revisar', 'confirmar'))
def test_lote_exclusivo_do_dono(app, owner_user, admin_user, cenario, etapa):
    token = _token(_revisar(_client(app, owner_user), cenario['ids']))
    dados = {'etapa': etapa, 'confirmacao': token, 'enquadramento_ids': cenario['ids']}
    assert _client(app, admin_user).post(URL, data=dados).status_code == 403
    assert _client(app).post(URL, data=dados).status_code in (302, 401)
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('ids', ([], [''], ['abc'], ['0'], ['-1'], ['1', '1'],
                                ['999999'], [str(n) for n in range(1, 202)]))
def test_selecao_invalida_recusa_lote_inteiro(app, owner_user, cenario, ids):
    antes = _estado(cenario)
    resposta = _revisar(_client(app, owner_user), ids)
    assert resposta.status_code == 400
    assert not _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    assert _estado(cenario) == antes
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('bloqueado', ('inativo', 'aprovado', 'sem_faixa'))
def test_um_item_inelegivel_impede_previa_do_lote_inteiro(app, owner_user, cenario, bloqueado):
    antes = _estado(cenario)
    resposta = _revisar(_client(app, owner_user), [
        cenario['ids'][0], str(cenario[bloqueado].id)])
    assert resposta.status_code == 400
    assert not _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    assert _estado(cenario) == antes
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('confirmacao', ('', 'token-invalido'))
def test_confirmacao_ausente_ou_adulterada_nao_grava(app, owner_user, cenario, confirmacao):
    antes = _estado(cenario)
    resposta = _client(app, owner_user).post(URL, data={
        'etapa': 'confirmar', 'confirmacao': confirmacao})
    assert resposta.status_code == 400
    assert _estado(cenario) == antes
    assert RhMovimentacao.query.count() == 0


def test_confirmacao_expira_apos_trinta_minutos(app, owner_user, cenario, monkeypatch):
    client = _client(app, owner_user)
    token = _token(_revisar(client, cenario['ids']))
    futuro = TimestampSigner.get_timestamp(None) + 1801
    monkeypatch.setattr(TimestampSigner, 'get_timestamp', lambda self: futuro)
    resposta = client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert RhMovimentacao.query.count() == 0
    assert cenario['plano'].decisao == 'Em avaliação'


def test_confirmacao_vinculada_ao_dono_que_revisou(app, owner_user, cenario):
    token = _token(_revisar(_client(app, owner_user), cenario['ids']))
    outro = Usuario(nome='Outro dono', login='outro-dono-lote', senha_hash='fixture',
                    papel='admin', is_owner=True)
    db.session.add(outro)
    db.session.commit()
    resposta = _client(app, outro).post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('mudanca', ('salario_cargo', 'decisao', 'alvo', 'premiacao',
                                   'inativado', 'vinculo'))
def test_lote_desatualizado_nao_aprova_nem_os_demais_e_preserva_selecao(
        app, owner_user, cenario, mudanca):
    client = _client(app, owner_user)
    token = _token(_revisar(client, cenario['ids'], q='Ana', familia='Atendimento'))
    segundo = cenario['mesmo_cargo']
    if mudanca == 'salario_cargo':
        cenario['destino'].salario_base = 2750
    elif mudanca == 'decisao':
        segundo.decisao = 'Proposta final'
    elif mudanca == 'alvo':
        segundo.total_alvo = 4000
    elif mudanca == 'premiacao':
        segundo.funcionario.premiacao = 600
    elif mudanca == 'inativado':
        segundo.funcionario.ativo = False
    elif mudanca == 'vinculo':
        outro = Cargo(nome='Novo cargo vinculado', salario_base=2600)
        db.session.add(outro)
        cenario['faixa'].cargo_vinculo.cargo = outro
    db.session.commit()
    antes = _estado(cenario)
    resposta = client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    html = resposta.get_data(as_text=True)
    assert not _Inputs(html).values.get('confirmacao')
    retornos = [urlsplit(link) for link in _Pagina(html).links
                if urlsplit(link).path == '/rh/plano-carreira']
    assert any(parse_qs(link.query).get('selecionados') == cenario['ids']
               and parse_qs(link.query).get('q') == ['Ana']
               and parse_qs(link.query).get('familia') == ['Atendimento']
               for link in retornos)
    db.session.expire_all()
    assert _estado(cenario) == antes
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('etapa', ('revisar', 'confirmar'))
def test_previa_e_confirmacao_exigem_csrf(app, owner_user, cenario, etapa):
    client = _client(app, owner_user)
    token = _token(_revisar(client, cenario['ids']))
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = client.post(URL, data={
        'etapa': etapa, 'confirmacao': token, 'enquadramento_ids': cenario['ids']})
    assert resposta.status_code == 302 and resposta.location == '/'
    with client.session_transaction() as sessao:
        assert any('token-ausente' in texto for _, texto in sessao['_flashes'])
    assert RhMovimentacao.query.count() == 0


def test_formularios_reais_funcionam_com_botao_desabilitado_no_envio(app, owner_user, cenario):
    app.config['WTF_CSRF_ENABLED'] = True
    client = _client(app, owner_user)
    lista = client.get('/rh/plano-carreira').get_data(as_text=True)
    formulario = next(form for form in _Forms(lista).forms
                      if form['attrs'].get('id') == 'aprovar-lote-form')
    assert formulario['hidden']['etapa'] == 'revisar'
    assert formulario['hidden']['csrf_token']
    previa = client.post(formulario['attrs']['action'], data={
        **formulario['hidden'], 'enquadramento_ids': cenario['ids']})
    _token(previa)
    confirmar = next(form for form in _Forms(previa.get_data(as_text=True)).forms
                     if form['hidden'].get('confirmacao'))
    assert confirmar['hidden']['etapa'] == 'confirmar'
    assert confirmar['hidden']['csrf_token']
    resposta = client.post(confirmar['attrs'].get('action') or URL,
                           data=confirmar['hidden'])
    assert resposta.status_code == 302
    assert RhMovimentacao.query.count() == 2


@pytest.mark.parametrize(('atributo', 'codigo'), (('pgcode', '40P01'), ('sqlstate', '40001')))
def test_conflito_transacional_reverte_aprovacoes_e_historicos(
        app, owner_user, cenario, monkeypatch, atributo, codigo):
    from app.services import plano_carreira_lote
    client = _client(app, owner_user)
    token = _token(_revisar(client, cenario['ids']))
    antes = _estado(cenario)
    confirmar = plano_carreira_lote.confirmar
    origem = Exception('Conflito transacional simulado')
    setattr(origem, atributo, codigo)

    def confirmar_e_falhar(*args, **kwargs):
        confirmar(*args, **kwargs)
        db.session.flush()
        assert RhMovimentacao.query.count() == 2
        raise OperationalError(None, None, origem)

    monkeypatch.setattr(plano_carreira_lote, 'confirmar', confirmar_e_falhar)
    resposta = client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert resposta.status_code == 400
    assert not _Inputs(resposta.get_data(as_text=True)).values.get('confirmacao')
    db.session.expire_all()
    assert _estado(cenario) == antes
    assert RhMovimentacao.query.count() == 0


def test_erro_de_banco_nao_transitorio_continua_visivel(app, owner_user, cenario, monkeypatch):
    from app.services import plano_carreira_lote
    client = _client(app, owner_user)
    token = _token(_revisar(client, cenario['ids']))
    origem = Exception('Conexão indisponível')
    origem.pgcode = '08006'
    erro = OperationalError(None, None, origem)

    def falhar(*args, **kwargs):
        raise erro

    monkeypatch.setattr(plano_carreira_lote, 'confirmar', falhar)
    with pytest.raises(OperationalError) as recebido:
        client.post(URL, data={'etapa': 'confirmar', 'confirmacao': token})
    assert recebido.value is erro
    db.session.rollback()
    assert RhMovimentacao.query.count() == 0
