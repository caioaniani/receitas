"""Cliente da API do Listmonk (05/08/2026) — o `requests` é sempre mockado.

Trava o contrato com a API: nenhum teste pode bater na instância real do VPS
nem disparar campanha.
"""
import json
from unittest.mock import patch

import pytest


class _Resp:
    def __init__(self, payload=None, texto='{}'):
        self._payload = payload if payload is not None else {}
        self.text = texto

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _cfg(app, url='https://mkt.example.com'):
    app.config['LISTMONK_URL'] = url
    app.config['LISTMONK_API_TOKEN'] = 'tok'
    app.config['LISTMONK_API_USER'] = 'api_padaria'


def test_indisponivel_sem_config(app):
    from app.services import listmonk
    app.config['LISTMONK_URL'] = ''
    app.config['LISTMONK_API_TOKEN'] = ''
    assert listmonk.disponivel() is False


def test_http_puro_e_recusado(app):
    """O token vai em BasicAuth — em HTTP puro trafegaria em claro."""
    from app.services import listmonk
    _cfg(app, url='http://mkt.example.com')
    with patch('requests.request') as req:
        with pytest.raises(ValueError, match='https'):
            listmonk.listas()
    req.assert_not_called()


def test_garantir_lista_e_idempotente_pelo_nome(app):
    from app.services import listmonk
    _cfg(app)
    existe = _Resp({'data': {'results': [{'id': 4, 'name': 'Wi-Fi das lojas'}]}})
    with patch('requests.request', return_value=existe) as req:
        assert listmonk.garantir_lista('Wi-Fi das lojas') == 4
    # só o GET das listas — não tentou criar de novo
    assert [c.args[0] for c in req.call_args_list] == ['GET']


def test_importar_fatia_em_lotes_e_nao_sobrescreve(app):
    """`overwrite` ligado reescreveria quem já existe — e re-inscreveria quem
    descadastrou. Default tem que ser False."""
    from app.services import listmonk
    _cfg(app)
    contatos = [{'email': f'{i}@x.com', 'nome': 'N', 'attribs_json': '{}'}
                for i in range(listmonk.LOTE + 3)]
    with patch('requests.request', return_value=_Resp()) as req:
        out = listmonk.importar([1], contatos)
    assert out == {'importados': listmonk.LOTE + 3, 'lotes': 2}
    params = json.loads(req.call_args_list[0].kwargs['data']['params'])
    assert params['overwrite'] is False
    assert params['mode'] == 'subscribe'
    assert params['lists'] == [1]


def test_importar_vazio_nao_bate_na_api(app):
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request') as req:
        assert listmonk.importar([1], [])['importados'] == 0
    req.assert_not_called()


def test_csv_leva_o_aniversario_nos_attribs(app):
    from app.services import listmonk
    linhas = listmonk._csv_de([
        {'email': 'a@x.com', 'nome': 'Ana',
         'attribs_json': '{"aniv_dia": 9, "aniv_mes": 8}'}]).splitlines()
    assert linhas[0] == 'email,name,attributes'
    assert 'aniv_dia' in linhas[1] and 'a@x.com' in linhas[1]


def test_descadastrados_pagina_e_devolve_id(app):
    """O id é o que permite propagar o descadastro pras outras listas sem
    montar SQL com o e-mail."""
    from app.services import listmonk
    _cfg(app)
    p1 = _Resp({'data': {'results': [{'id': i, 'email': f'{i}@X.com'}
                                     for i in range(500)]}})
    p2 = _Resp({'data': {'results': [{'id': 900, 'email': 'Z@X.com'}]}})
    with patch('requests.request', side_effect=[p1, p2]):
        out = listmonk.descadastrados(3)
    assert len(out) == 501
    assert out['z@x.com'] == 900          # normaliza pra minúsculo


def test_mudar_listas_por_id_manda_status_so_no_add(app):
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request', return_value=_Resp()) as req:
        listmonk.mudar_listas([7], 'unsubscribe', [1, 2])
        corpo = req.call_args.kwargs['json']
        assert corpo == {'ids': [7], 'action': 'unsubscribe',
                         'target_list_ids': [1, 2]}
        listmonk.mudar_listas([7], 'add', [9])
        assert req.call_args.kwargs['json']['status'] == 'confirmed'


def test_mudar_listas_sem_ids_nao_bate_na_api(app):
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request') as req:
        assert listmonk.mudar_listas([], 'remove', [1]) == 0
    req.assert_not_called()


def test_query_limita_o_universo_as_listas_de_origem(app):
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request', return_value=_Resp()) as req:
        listmonk.mudar_listas_por_query("subscribers.id > 0", 'add', [9],
                                        listas_origem=[1, 2])
    corpo = req.call_args.kwargs['json']
    assert corpo['list_ids'] == [1, 2]
    assert corpo['subscription_status'] == 'confirmed'
    assert corpo['target_list_ids'] == [9]


def test_criar_campanha_nasce_em_rascunho(app):
    """Criar NÃO envia — o disparo é o `iniciar_campanha`, separado."""
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request', return_value=_Resp({'data': {'id': 12}})) as req:
        assert listmonk.criar_campanha('C', 'Assunto', '<p>oi</p>', [9]) == 12
    metodo, url = req.call_args.args
    assert metodo == 'POST' and url.endswith('/api/campaigns')
    corpo = req.call_args.kwargs['json']
    assert corpo['lists'] == [9] and corpo['type'] == 'regular'
    assert 'status' not in corpo


def test_assinante_que_ja_existe_nao_e_erro(app):
    """409 do Listmonk = "já é assinante". Levantar aqui faria o botão de
    teste falhar na segunda vez."""
    from app.services import listmonk
    _cfg(app)

    class _R409:
        status_code = 409
        text = ''

        def raise_for_status(self):
            raise AssertionError('não deveria levantar em 409')

    with patch('requests.request', return_value=_R409()):
        listmonk.garantir_assinante('a@x.com', None, [8])


def test_enviar_teste_reenvia_o_corpo_da_campanha(app):
    """O endpoint de teste do Listmonk quer os mesmos campos da criação."""
    from app.services import listmonk
    _cfg(app)
    payload = listmonk.montar_campanha('C', 'Assunto', '<p>x</p>', [8],
                                       content_type='html')
    with patch('requests.request', return_value=_Resp()) as req:
        listmonk.enviar_teste(44, payload, ['a@x.com'])
    metodo, url = req.call_args.args
    assert metodo == 'POST' and url.endswith('/api/campaigns/44/test')
    corpo = req.call_args.kwargs['json']
    assert corpo['subscribers'] == ['a@x.com']
    assert corpo['subject'] == 'Assunto' and corpo['body'] == '<p>x</p>'
    assert payload.get('subscribers') is None   # não muta o original


def test_iniciar_campanha_poe_pra_rodar(app):
    from app.services import listmonk
    _cfg(app)
    with patch('requests.request', return_value=_Resp()) as req:
        listmonk.iniciar_campanha(12)
    metodo, url = req.call_args.args
    assert metodo == 'PUT' and url.endswith('/api/campaigns/12/status')
    assert req.call_args.kwargs['json'] == {'status': 'running'}
