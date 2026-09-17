"""Consulta externa: uma loja, somente recebimentos, HTML/PDF/XLSX/fotos."""
import io

import pytest

from app.extensions import db
from app.models import FotoRecebimento, Loja, PedidoItem, PedidoLoja, Receita, Usuario
from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


@pytest.fixture
def consulta(app):
    nebraska = Loja(nome='Nebraska', ativa=True)
    outra = Loja(nome='Outra loja restrita', ativa=True)
    db.session.add_all([nebraska, outra])
    db.session.flush()
    user = Usuario(nome='Thiago Oliveira', login='thiago@example.com',
                   email='thiago@example.com', papel='relatorio_loja',
                   loja_id=nebraska.id)
    user.set_senha('Senha-teste-123')
    db.session.add(user)
    pedidos, fotos = [], []
    for loja, status, nome in (
        (nebraska, 'recebido', 'Produto permitido'),
        (outra, 'entregue', 'Produto secreto'),
        (nebraska, 'confirmado', 'Produto não recebido'),
    ):
        receita = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                          rendimento_unidade='un', peso_base=100, preco_interno=2)
        pedido = PedidoLoja(loja_id=loja.id, status=status, data_entrega=hoje())
        db.session.add_all([receita, pedido])
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                                 quantidade=3))
        foto = FotoRecebimento(pedido_id=pedido.id, imagem=b'foto-teste',
                               mimetype='image/jpeg')
        db.session.add(foto)
        pedidos.append(pedido)
        fotos.append(foto)
    db.session.commit()
    client = app.test_client()
    _login(client, user)
    return client, user, nebraska, outra, pedidos, fotos


@pytest.mark.parametrize('classico', [False, True])
def test_html_somente_loja_e_navegacao_restrita(consulta, classico):
    client, _, nebraska, _, _, _ = consulta
    if classico:
        client.set_cookie('ui_classic', '1')
    response = client.get('/pedidos/relatorio')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'Nebraska' in html
    assert 'Produto permitido' in html
    assert 'Somente leitura' in html
    for texto in ('Outra loja restrita', 'Produto secreto', 'Produto não recebido',
                  'Treinamento', '/area/', '/pedidos/observador', '/receitas/precos',
                  'href="/pedidos/"', 'name="loja" class="form-select'):
        assert texto not in html
    assert f'name="loja" value="{nebraska.id}"' in html
    assert client.get('/').location.endswith('/pedidos/relatorio')


@pytest.mark.parametrize('formato', ['html', 'pdf', 'xlsx'])
@pytest.mark.parametrize('parametro', ['outra', 'zero', 'invalido', 'repetido', 'vazio'])
def test_parametro_loja_adulterado_bloqueado(consulta, formato, parametro):
    client, _, nebraska, outra, _, _ = consulta
    valor = {'outra': str(outra.id), 'zero': '0', 'invalido': 'abc',
             'repetido': f'{nebraska.id}&loja={outra.id}', 'vazio': ''}[parametro]
    resp = client.get(f'/pedidos/relatorio?formato={formato}&loja={valor}')
    assert resp.status_code == 403
    assert b'Produto secreto' not in resp.data


@pytest.mark.parametrize('formato', ['pdf', 'xlsx'])
def test_exportador_recebe_apenas_dados_da_loja(consulta, monkeypatch, formato):
    client, _, _, _, pedidos, _ = consulta
    from app.services import relatorio

    def exportar(loja_nome, de, ate, rows, totais, por_item, **kwargs):
        assert loja_nome == 'Nebraska'
        assert [row['p'].id for row in rows] == [pedidos[0].id]
        assert set(por_item) == {'Produto permitido'}
        assert totais['qtd_pedidos'] == 1
        return io.BytesIO(b'exportacao-restrita')

    monkeypatch.setattr(relatorio, f'gerar_{formato}_pedidos', exportar)
    resp = client.get(f'/pedidos/relatorio?formato={formato}&fotos=1')
    assert resp.status_code == 200
    assert resp.data == b'exportacao-restrita'
    assert 'Nebraska' in resp.headers['Content-Disposition']


def test_fotos_apenas_da_loja_e_pedidos_recebidos(consulta):
    client, _, _, _, _, fotos = consulta
    assert client.get(f'/pedidos/foto/{fotos[0].id}').data == b'foto-teste'
    for foto in fotos[1:]:
        assert client.get(f'/pedidos/foto/{foto.id}').status_code == 403


@pytest.mark.parametrize('url', [
    '/pedidos/', '/pedidos/consulta', '/pedidos/observador',
    '/pedidos/estoque-loja', '/auth/usuarios', '/auth/minhas-fichas',
    '/treino/', '/rh/funcionarios', '/area/vendas', '/padeiro/',
    '/telaindustriateste/', '/admin/loja-online', '/api/claude/cronograma',
])
def test_outras_areas_negadas(consulta, url):
    client = consulta[0]
    assert client.get(url).status_code == 403


def test_escrita_negada_sem_mudar_pedido(consulta):
    client, _, _, _, pedidos, _ = consulta
    pedido = pedidos[0]
    assert client.post(f'/pedidos/{pedido.id}/cancelar').status_code == 403
    assert db.session.get(PedidoLoja, pedido.id).status == 'recebido'
    assert client.post('/auth/usuarios/novo', data={'nome': 'Invasor'}).status_code == 403


@pytest.mark.parametrize('estado', ['sem-loja', 'inativa', 'industria'])
def test_loja_invalida_falha_fechada(consulta, estado):
    client, user, loja, _, _, fotos = consulta
    if estado == 'sem-loja':
        user.loja_id = None
    elif estado == 'inativa':
        loja.ativa = False
    else:
        loja.nome = 'Industria'
    db.session.commit()
    assert client.get('/pedidos/relatorio').status_code == 403
    assert client.get(f'/pedidos/foto/{fotos[0].id}').status_code == 403


def test_sem_tools_copilot_ou_checklist(consulta):
    from app.services import copilot
    user = consulta[1]
    assert copilot.papel_efetivo(user) == 'relatorio_loja'
    assert not any(copilot.pode_usar(tool, user) for tool in copilot.PAPEIS_POR_TOOL)
    assert not user.pode_checklist()


@pytest.mark.parametrize('url', ['/pedidos/relatorio', '/auth/minha-senha', '/auth/usuarios'])
def test_custos_e_catalogo_nao_embutidos_no_html(consulta, url):
    from app.models import MateriaPrima, Produto
    from app.services.busca_navegacao import itens_para_usuario
    client, user, _, _, _, _ = consulta
    db.session.add_all([
        MateriaPrima(nome='MP sigilosa', unidade='kg', custo_por_kg=123.45),
        Produto(nome='Produto fora do relatório', ativo=True),
    ])
    db.session.commit()
    html = client.get(url).get_data(as_text=True)
    assert 'const MP_DATA = {}' in html
    for texto in ('MP sigilosa', 'Produto secreto', 'Produto fora do relatório',
                  'custo_por_kg'):
        assert texto not in html
    with client.application.test_request_context():
        assert {i['url'] for i in itens_para_usuario(user, {})} == {
            '/auth/minha-senha', '/auth/logout', '/pedidos/relatorio'}


def test_login_e_troca_senha_redirecionam_para_relatorio(consulta, app):
    _, user, _, _, _, _ = consulta
    user.senha_provisoria = True
    db.session.commit()
    client = app.test_client()
    resp = client.post('/auth/login?next=/auth/usuarios', data={
        'login': user.email, 'senha': 'Senha-teste-123'})
    assert resp.location.endswith('/auth/minha-senha')
    assert client.get('/pedidos/relatorio').location.endswith('/auth/minha-senha')
    troca = client.post('/auth/minha-senha', data={
        'senha_atual': 'Senha-teste-123', 'nova_senha': 'Nova-senha-123',
        'confirma_senha': 'Nova-senha-123'})
    assert troca.location.endswith('/pedidos/relatorio')
    assert client.get(troca.location).status_code == 200
    client.get('/auth/logout')
    resp = client.post('/auth/login?next=/auth/usuarios', data={
        'login': user.email, 'senha': 'Nova-senha-123'})
    assert resp.location.endswith('/pedidos/relatorio')


def test_cadastro_com_escopo_e_email_sem_chatwoot(app, admin_user, loja, monkeypatch):
    from app.services import email
    enviados = []
    monkeypatch.setattr(email, 'enviar_boas_vindas',
                        lambda *a, **kw: enviados.append((a, kw)) or {'ok': True})
    client = app.test_client()
    _login(client, admin_user)
    response = client.post('/auth/usuarios/novo', data={
        'nome': 'Thiago', 'login': 'thiago@example.com', 'email': 'thiago@example.com',
        'papel': 'relatorio_loja', 'loja_id': loja.id,
    })
    assert response.status_code == 302
    user = Usuario.query.filter_by(login='thiago@example.com').one()
    assert user.loja_id == loja.id and user.is_relatorio_loja()
    assert user.senha_provisoria and not user.somente_treino
    assert enviados[0][1] == {'com_chatwoot': False}
    assert enviados[0][0][0] == user.email
    assert user.check_senha(enviados[0][0][3])


@pytest.mark.parametrize('alteracao', ['sem-loja', 'inativa', 'treino', 'duplicado'])
def test_cadastro_invalido_nao_cria_nem_envia(consulta, admin_user, monkeypatch, alteracao):
    from app.services import email
    client, _, loja, _, _, _ = consulta
    _login(client, admin_user)
    enviados = []
    monkeypatch.setattr(email, 'enviar_boas_vindas', lambda *a, **kw: enviados.append(a))
    data = {'nome': 'Novo', 'login': 'novo@example.com', 'email': 'novo@example.com',
            'papel': 'relatorio_loja', 'loja_id': loja.id}
    if alteracao == 'sem-loja':
        data.pop('loja_id')
    elif alteracao == 'inativa':
        loja.ativa = False
        db.session.commit()
    elif alteracao == 'treino':
        data['somente_treino'] = '1'
    else:
        data['email'] = 'THIAGO@example.com'
    client.post('/auth/usuarios/novo', data=data)
    assert not Usuario.query.filter_by(login='novo@example.com').first()
    assert enviados == []


def test_alteracao_exige_loja_e_nao_afeta_demais_perfis(consulta, admin_user):
    client, user, loja, outra, _, _ = consulta
    _login(client, admin_user)
    client.post(f'/auth/usuarios/{user.id}/papel', data={'papel': 'relatorio_loja'})
    assert db.session.get(Usuario, user.id).loja_id == loja.id
    client.post(f'/auth/usuarios/{user.id}/papel', data={
        'papel': 'relatorio_loja', 'loja_id': outra.id})
    assert db.session.get(Usuario, user.id).loja_id == outra.id
    client.post(f'/auth/usuarios/{user.id}/somente-treino')
    assert not db.session.get(Usuario, user.id).somente_treino
    # Admin mantém todos os relatórios, sem a restrição do novo perfil.
    resp = client.get(f'/pedidos/relatorio?loja={outra.id}')
    assert resp.status_code == 200
    assert 'Produto secreto' in resp.get_data(as_text=True)
