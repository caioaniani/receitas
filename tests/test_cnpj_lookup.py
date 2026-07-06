"""Busca de CNPJ na base pública da Receita (06/07/2026) — alimenta o
botão "Buscar" do cadastro de cliente B2B (preenche razão social +
endereço fiscal, igual ao Tiny). Provedores mockados.
"""
from unittest.mock import patch

from app.services import cnpj as cnpj_svc


class _Resp:
    def __init__(self, status=200, corpo=None):
        self.status_code = status
        self._corpo = corpo or {}

    def json(self):
        return self._corpo


_BRASILAPI = {
    'razao_social': 'PADARIA EXEMPLO LTDA',
    'nome_fantasia': 'Padaria Exemplo',
    'email': 'FISCAL@EXEMPLO.COM.BR',
    'ddd_telefone_1': '(11) 4002-8922',
    'descricao_tipo_de_logradouro': 'AVENIDA',
    'logradouro': 'PAULISTA',
    'numero': '1000',
    'complemento': 'SALA 2',
    'bairro': 'BELA VISTA',
    'cep': 1310100,                       # int — zero à esquerda some
    'municipio': 'SAO PAULO',
    'uf': 'sp',
    'descricao_situacao_cadastral': 'ATIVA',
}


def test_consultar_normaliza_campos(app):
    with app.app_context(), \
         patch('app.services.cnpj.requests.get',
               return_value=_Resp(200, _BRASILAPI)) as get:
        d = cnpj_svc.consultar('11.222.333/0001-44')
    assert 'brasilapi.com.br' in get.call_args[0][0]
    assert d['razao_social'] == 'PADARIA EXEMPLO LTDA'
    assert d['logradouro'] == 'AVENIDA PAULISTA'   # tipo + logradouro
    assert d['numero'] == '1000'
    assert d['bairro'] == 'BELA VISTA'
    assert d['cep'] == '01310100'                  # zfill do CEP numérico
    assert d['cidade'] == 'SAO PAULO'
    assert d['uf'] == 'SP'
    assert d['email'] == 'fiscal@exemplo.com.br'
    assert d['telefone'] == '1140028922'
    assert d['cnpj'] == '11222333000144'


def test_consultar_cai_pro_fallback_quando_brasilapi_falha(app):
    """BrasilAPI fora (500) → minhareceita responde. Não confiamos em
    provedor único (incidente do frete/CEP em 05/07/2026)."""
    respostas = [_Resp(500), _Resp(200, _BRASILAPI)]
    with app.app_context(), \
         patch('app.services.cnpj.requests.get',
               side_effect=respostas) as get:
        d = cnpj_svc.consultar('11222333000144')
    assert d['razao_social'] == 'PADARIA EXEMPLO LTDA'
    assert 'minhareceita.org' in get.call_args[0][0]


def test_consultar_404_nos_dois_e_nao_encontrado(app):
    with app.app_context(), \
         patch('app.services.cnpj.requests.get', return_value=_Resp(404)):
        d = cnpj_svc.consultar('11222333000144')
    assert 'não encontrado' in d['erro']


def test_consultar_cnpj_invalido(app):
    with app.app_context():
        assert '14 dígitos' in cnpj_svc.consultar('123')['erro']
        assert '14 dígitos' in cnpj_svc.consultar('')['erro']


def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def test_rota_api_cnpj(app, admin_user):
    c = app.test_client()
    _login(c, admin_user.id)
    with patch('app.services.cnpj.requests.get',
               return_value=_Resp(200, _BRASILAPI)):
        r = c.get('/b2b/api/cnpj/11222333000144')
    assert r.status_code == 200
    assert r.get_json()['razao_social'] == 'PADARIA EXEMPLO LTDA'
    r2 = c.get('/b2b/api/cnpj/123')
    assert r2.status_code == 400
    with patch('app.services.cnpj.requests.get', return_value=_Resp(404)):
        r3 = c.get('/b2b/api/cnpj/11222333000144')
    assert r3.status_code == 404
