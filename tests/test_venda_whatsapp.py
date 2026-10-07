"""Venda pelo WhatsApp com link de carrinho (decisão do dono, 07/10/2026).

O bot atende quem quer comprar, monta o carrinho — minis, fatiado e cartinha
incluídos — e manda o link; o cliente confere e paga no site. Travas:
- o servidor valida o carrinho ao CRIAR o link e de novo ao ABRIR;
- a composição dos minis é guardada pelo mini (alvo), não pelo `pi_id`, e
  sobrevive à edição do menu no admin;
- reclamação, pedido de atendente, falha de entrega, anexo e conversa já com
  a equipe nunca chegam ao modelo de vendas;
- link que não veio de ferramenta não sai para o cliente.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from conftest import _make_receita

# ── Cenário ─────────────────────────────────────────────────────────────


def _loja_site(db):
    from app.models import AppConfig, Loja
    loja = Loja(nome='Loja do Site', ativa=True, endereco='Rua Site, 1')
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.commit()
    return loja


def _catalogo(db):
    from app.models import Produto, ProdutoItem
    _loja_site(db)
    sour = _make_receita('Sourdough Tradicional', categoria='Paes')
    sour.preco_site = Decimal('38.00')
    sour.familia = 'pao_sourdough'
    croi = _make_receita('Croissant Tradicional', categoria='Viennoiserie')
    croi.preco_site = Decimal('22.50')
    menu = Produto(nome='Menu Degustação dos Minis', categoria='Minis',
                   preco_site=Decimal('1'), ativo=True,
                   menu_configuravel=True, menu_total_unidades=10,
                   menu_max_por_item=6)
    db.session.add_all([sour, croi, menu])
    db.session.flush()
    minis = []
    for i, preco in enumerate((Decimal('3'), Decimal('4')), start=1):
        r = _make_receita(f'Mini {i}', categoria='Paes')
        db.session.add(r)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=menu.id, tipo='receita',
                                   receita_id=r.id, item_nome=r.nome,
                                   quantidade=5, preco_menu=preco))
        minis.append(r)
    db.session.commit()
    return {'sour': sour, 'croi': croi, 'menu': menu, 'minis': minis}


def _pis(menu):
    return [pi.id for pi in sorted(menu.itens, key=lambda x: x.id)]


def _comp(menu, *qtds):
    return [{'pi_id': p, 'quantidade': q} for p, q in zip(_pis(menu), qtds)]


# ── carrinho_link.criar ─────────────────────────────────────────────────

def test_criar_link_com_fatiado_minis_e_cartinha(app):
    from app.extensions import db
    from app.models import CarrinhoLink
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        out = carrinho_link.criar([
            {'kind': 'receita', 'id': c['sour'].id, 'quantidade': 2,
             'fatiado': True},
            {'kind': 'produto', 'id': c['menu'].id, 'quantidade': 1,
             'composicao': _comp(c['menu'], 6, 4)},
        ], cartinha='Feliz aniversário, mãe!', conversa_id=2402)
        assert '/loja/carrinho?link=' in out['url']
        assert out['total_itens'] == pytest.approx(2 * 38 + 6 * 3 + 4 * 4)
        assert out['itens'][0]['fatiado'] is True
        assert out['itens'][1]['composicao'] == ['6x Mini 1', '4x Mini 2']
        reg = CarrinhoLink.query.filter_by(token=out['token']).one()
        assert reg.cartinha == 'Feliz aniversário, mãe!'
        assert reg.conversa_id == '2402'
        # Composição guardada pelo MINI (alvo), nunca pelo pi_id.
        menu_linha = reg.itens[1]
        assert menu_linha['comp'] == [['receita_id', c['minis'][0].id, 6],
                                      ['receita_id', c['minis'][1].id, 4]]


@pytest.mark.parametrize('ajuste, trecho', [
    ({'composicao': None}, 'menu montável'),
    ({'composicao': 'total errado'}, 'exatamente 10'),
    ({'composicao': 'intruso'}, 'não fazem parte'),
])
def test_criar_recusa_menu_mal_montado(app, ajuste, trecho):
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        linha = {'kind': 'produto', 'id': c['menu'].id, 'quantidade': 1}
        if ajuste['composicao'] == 'total errado':
            linha['composicao'] = _comp(c['menu'], 3, 3)
        elif ajuste['composicao'] == 'intruso':
            linha['composicao'] = [{'pi_id': 999999, 'quantidade': 10}]
        with pytest.raises(carrinho_link.LinkInvalido) as exc:
            carrinho_link.criar([linha])
        assert trecho in str(exc.value)


def test_criar_recusa_fatiado_em_item_que_nao_fatia(app):
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        with pytest.raises(carrinho_link.LinkInvalido, match='fatiado'):
            carrinho_link.criar([{'kind': 'receita', 'id': c['croi'].id,
                                  'quantidade': 1, 'fatiado': True}])


def test_criar_recusa_cartinha_longa_item_fora_do_site_e_esgotado(app):
    from app.extensions import db
    from app.services import carrinho_link, loja_plano_dia
    from app.utils import hoje
    with app.app_context():
        c = _catalogo(db)
        item = {'kind': 'receita', 'id': c['croi'].id, 'quantidade': 1}
        with pytest.raises(carrinho_link.LinkInvalido, match='máximo'):
            carrinho_link.criar([item], cartinha='x' * 400)
        with pytest.raises(carrinho_link.LinkInvalido, match='não está à venda'):
            carrinho_link.criar([{'kind': 'receita', 'id': 987654,
                                  'quantidade': 1}])
        loja_plano_dia.replicar_para_proximos_dias(
            'receita', c['croi'].id, 0, data_inicio=hoje(), dias=14)
        with pytest.raises(carrinho_link.LinkInvalido, match='esgotado'):
            carrinho_link.criar([item])


# ── carrinho_link.resgatar ──────────────────────────────────────────────

def test_resgatar_sobrevive_a_edicao_do_menu(app):
    """`salvar_composicao` apaga e recria os ProdutoItem: o pi_id muda, o
    mini não. O link segue valendo com os pi_id NOVOS."""
    from app.extensions import db
    from app.models import ProdutoItem
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        out = carrinho_link.criar([{'kind': 'produto', 'id': c['menu'].id,
                                    'quantidade': 1,
                                    'composicao': _comp(c['menu'], 6, 4)}])
        for pi in list(c['menu'].itens):
            db.session.delete(pi)
        db.session.flush()
        for r, preco in zip(c['minis'], (Decimal('3'), Decimal('4'))):
            db.session.add(ProdutoItem(produto_id=c['menu'].id, tipo='receita',
                                       receita_id=r.id, item_nome=r.nome,
                                       quantidade=5, preco_menu=preco))
        db.session.commit()
        db.session.refresh(c['menu'])
        novos = _pis(c['menu'])
        dados = carrinho_link.resgatar(out['token'])
        assert dados['avisos'] == []
        assert dados['itens'][0]['comp'] == [[novos[0], 6], [novos[1], 4]]


def test_resgatar_menu_que_deixou_de_fechar_vira_aviso(app):
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        out = carrinho_link.criar([{'kind': 'produto', 'id': c['menu'].id,
                                    'quantidade': 1,
                                    'composicao': _comp(c['menu'], 6, 4)}])
        c['menu'].menu_max_por_item = 5     # 6 do Mini 1 deixa de caber
        db.session.commit()
        dados = carrinho_link.resgatar(out['token'])
        assert dados['itens'] == []
        assert 'monte de novo' in dados['avisos'][0]


def test_resgatar_link_vencido_ou_inexistente(app):
    from app.extensions import db
    from app.models import CarrinhoLink
    from app.services import carrinho_link
    from app.utils import agora
    with app.app_context():
        c = _catalogo(db)
        out = carrinho_link.criar([{'kind': 'receita', 'id': c['croi'].id,
                                    'quantidade': 1}])
        reg = CarrinhoLink.query.filter_by(token=out['token']).one()
        reg.expira_em = agora() - timedelta(minutes=1)
        db.session.commit()
        assert carrinho_link.resgatar(out['token']) is None
        assert carrinho_link.resgatar('naoexiste') is None


# ── Rota /loja/carrinho?link= e checkout ───────────────────────────────

@pytest.mark.loja_host
def test_rota_do_link_enche_o_carrinho_uma_vez_e_preenche_a_cartinha(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    client = app.test_client()
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        out = carrinho_link.criar([
            {'kind': 'receita', 'id': c['sour'].id, 'quantidade': 2,
             'fatiado': True},
            {'kind': 'produto', 'id': c['menu'].id, 'quantidade': 1,
             'composicao': _comp(c['menu'], 6, 4)},
        ], cartinha='Com carinho, Ana')
        pis = _pis(c['menu'])
        sour_id = c['sour'].id
    r = client.get(f'/loja/carrinho?link={out["token"]}')
    assert r.status_code == 302
    client.get(f'/loja/carrinho?link={out["token"]}')     # segundo toque
    with client.session_transaction() as s:
        carrinho = s['carrinho']
    assert len(carrinho) == 2                               # não duplicou
    sour = next(i for i in carrinho if i['kind'] == 'receita')
    assert sour['id'] == sour_id and sour['qtd'] == 2 and sour['fatiado']
    menu = next(i for i in carrinho if i['kind'] == 'produto')
    assert menu['comp'] == [[pis[0], 6], [pis[1], 4]]
    html = client.get('/loja/checkout').get_data(as_text=True)
    assert 'Com carinho, Ana' in html


@pytest.mark.loja_host
def test_rota_do_link_vencido_avisa(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    client = app.test_client()
    r = client.get('/loja/carrinho?link=naoexiste', follow_redirects=True)
    assert 'venceu ou não existe' in r.get_data(as_text=True)


def test_cartinha_aparece_para_minis_e_quando_preenchida():
    js = open('app/static/loja/checkout.js', encoding='utf-8').read()
    assert 'it.comp && it.comp.length > 0' in js
    assert 'temCesta || preenchida' in js


# ── Roteamento no atendimento restrito ─────────────────────────────────

def _u(texto, **kw):
    return {'role': 'user', 'content': texto, **kw}


@pytest.fixture
def venda_ligada(app, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'teste')
    monkeypatch.setenv('BOT_VENDA', '1')
    monkeypatch.setattr('app.services.chatbot._fora_horario_chat', lambda: False)


def test_compra_vai_para_a_venda(app, venda_ligada):
    from app.services import atendimento_restrito
    with app.app_context(), patch(
            'app.services.atendimento_venda.responder',
            return_value={'acao': 'responder', 'texto': 'ok'}) as venda:
        out = atendimento_restrito.responder([_u('Quero 2 sourdough')],
                                             conversa_id=10)
    venda.assert_called_once()
    assert out['acao'] == 'responder'


@pytest.mark.parametrize('hist', [
    [_u('Quero 2 sourdough', handoff_em='2026-10-07T10:00:00')],
    [_u('Quero 2 sourdough', imagens=['data:x'])],
    [_u('Quero 2 sourdough'), {'role': 'assistant', 'content': 'Oi, aqui é a '
                               'Ana da equipe', 'humano': True},
     _u('e o croissant?')],
])
def test_equipe_na_conversa_nunca_vai_para_a_venda(app, venda_ligada, hist):
    from app.services import atendimento_restrito
    with app.app_context(), patch(
            'app.services.atendimento_venda.responder') as venda:
        out = atendimento_restrito.responder(hist)
    venda.assert_not_called()
    assert out['acao'] == 'handoff'


def test_venda_desligada_volta_ao_restrito(app, venda_ligada, monkeypatch):
    from app.services import atendimento_restrito
    monkeypatch.setenv('BOT_VENDA', '0')
    with app.app_context(), patch(
            'app.services.atendimento_venda.responder') as venda:
        out = atendimento_restrito.responder([_u('Quero 2 sourdough')])
    venda.assert_not_called()
    assert out['acao'] == 'handoff'


def test_faq_continua_sem_modelo(app, venda_ligada):
    from app.services import atendimento_restrito
    with app.app_context(), patch(
            'app.services.atendimento_venda.responder') as venda:
        out = atendimento_restrito.responder([_u('Oi')])
    venda.assert_not_called()
    assert out['acao'] == 'responder'


# ── Turno de venda (modelo mockado) ────────────────────────────────────

def _resp(*blocos, stop='end_turn'):
    return SimpleNamespace(content=list(blocos), stop_reason=stop,
                           stop_details=None, usage=None)


def _tool(nome, inp, i='t1'):
    return SimpleNamespace(type='tool_use', name=nome, input=inp, id=i)


def _txt(t):
    return SimpleNamespace(type='text', text=t)


@pytest.mark.parametrize('frase', [
    'Quero falar com um atendente',
    'Meu pedido não chegou até agora',
    'Veio tudo errado, péssimo',
])
def test_travas_antes_do_modelo(app, venda_ligada, frase):
    from app.services import atendimento_venda
    with app.app_context(), patch(
            'app.services.chatbot._chamar_com_retry_sobrecarga') as api:
        out = atendimento_venda.responder([_u(frase)])
    api.assert_not_called()
    assert out['acao'] == 'handoff'
    assert out['politica_atendimento']


def test_turno_completo_monta_o_link(app, venda_ligada):
    from app.extensions import db
    from app.services import atendimento_restrito, atendimento_venda
    with app.app_context():
        c = _catalogo(db)
        sour = c['sour'].id
        respostas = iter([
            _resp(_tool('consultar_produtos', {'busca': 'sourdough'}),
                  stop='tool_use'),
            _resp(_tool('montar_link_carrinho', {'itens': [
                {'kind': 'receita', 'id': sour, 'quantidade': 2,
                 'fatiado': True}]}, 't2'), stop='tool_use'),
        ])
        estado = {}

        def api(client, **kw):
            try:
                return next(respostas)
            except StopIteration:
                ultimo = kw['messages'][-1]['content'][0]['content']
                import json
                estado['url'] = json.loads(ultimo)['url']
                return _resp(_txt(f'Pronto! Seu carrinho: {estado["url"]}'))

        with patch('anthropic.Anthropic'), patch(
                'app.services.chatbot._chamar_com_retry_sobrecarga',
                side_effect=api):
            out = atendimento_venda.responder(
                [_u('Quero 2 sourdough fatiados')], conversa_id=7)
    assert out['acao'] == 'responder'
    assert estado['url'] in out['texto']
    assert out['tools_usadas'] == ['consultar_produtos', 'montar_link_carrinho']
    assert out['politica_atendimento'] == atendimento_restrito.POLITICA


def test_consultar_produtos_da_venda_mostra_minis_e_fatiavel(app):
    from app.extensions import db
    from app.services import atendimento_venda
    with app.app_context():
        c = _catalogo(db)
        res = atendimento_venda.consultar_produtos('menu degustação')
        menu = next(p for p in res['produtos'] if p['id'] == c['menu'].id)
        assert menu['menu']['total'] == 10 and menu['menu']['max_por_item'] == 6
        assert [m['pi_id'] for m in menu['menu']['minis']] == _pis(c['menu'])
        res = atendimento_venda.consultar_produtos('sourdough')
        assert next(p for p in res['produtos']
                    if p['id'] == c['sour'].id)['fatiavel'] is True


def test_link_inventado_vai_para_a_equipe(app, venda_ligada):
    from app.services import atendimento_venda
    with app.app_context(), patch('anthropic.Anthropic'), patch(
            'app.services.chatbot._chamar_com_retry_sobrecarga',
            return_value=_resp(_txt('Compre aqui: https://golpe.example/x'))):
        out = atendimento_venda.responder([_u('Quero um croissant')])
    assert out['acao'] == 'handoff'
    assert 'link' in out['motivo']


def test_transferir_e_recusa_vao_para_a_equipe(app, venda_ligada):
    from app.services import atendimento_venda
    with app.app_context(), patch('anthropic.Anthropic'):
        with patch('app.services.chatbot._chamar_com_retry_sobrecarga',
                   return_value=_resp(_tool('transferir_para_humano',
                                            {'motivo': 'evento para 200'}),
                                      stop='tool_use')):
            out = atendimento_venda.responder([_u('Quero 200 croissants pra festa')])
        assert out['acao'] == 'handoff' and 'evento para 200' in out['motivo']
        with patch('app.services.chatbot._chamar_com_retry_sobrecarga',
                   return_value=_resp(stop='refusal')):
            out = atendimento_venda.responder([_u('Quero um croissant')])
        assert out['acao'] == 'handoff'


def test_link_do_proprio_bot_em_turno_anterior_pode_ser_repetido(app, venda_ligada):
    from app.services import atendimento_venda
    url = 'https://opao.online/loja/carrinho?link=abc123'
    hist = [_u('Quero um croissant'),
            {'role': 'assistant', 'content': f'Seu carrinho: {url}'},
            _u('me manda de novo o link?')]
    with app.app_context(), patch('anthropic.Anthropic'), patch(
            'app.services.chatbot._chamar_com_retry_sobrecarga',
            return_value=_resp(_txt(f'Claro: {url}'))):
        out = atendimento_venda.responder(hist)
    assert out['acao'] == 'responder'


# ── Correções da revisão (07/10/2026) ──────────────────────────────────

@pytest.mark.loja_host
def test_segundo_link_substitui_o_carrinho_nao_soma(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        sour = c['sour'].id
        a = carrinho_link.criar([{'kind': 'receita', 'id': sour,
                                  'quantidade': 2}], cartinha='Oi mãe')
        b = carrinho_link.criar([{'kind': 'receita', 'id': sour,
                                  'quantidade': 3}])
    client = app.test_client()
    client.get(f'/loja/carrinho?link={a["token"]}')
    r = client.get(f'/loja/carrinho?link={b["token"]}', follow_redirects=True)
    with client.session_transaction() as s:
        carrinho = s['carrinho']
        assert '_checkout_cartinha' not in s     # link B não tem cartinha
    assert [(i['id'], i['qtd']) for i in carrinho] == [(sour, 3)]
    assert 'trocado pelo que você combinou' in r.get_data(as_text=True)


def test_criar_recusa_mini_repetido_e_acima_do_teto(app):
    from app.extensions import db
    from app.services import carrinho_link
    with app.app_context():
        c = _catalogo(db)
        a, _b = _pis(c['menu'])
        base = {'kind': 'produto', 'id': c['menu'].id, 'quantidade': 1}
        with pytest.raises(carrinho_link.LinkInvalido, match='repete'):
            carrinho_link.criar([dict(base, composicao=[
                {'pi_id': a, 'quantidade': 5}, {'pi_id': a, 'quantidade': 5}])])
        with pytest.raises(carrinho_link.LinkInvalido, match='no máximo 6'):
            carrinho_link.criar([dict(base, composicao=[
                {'pi_id': a, 'quantidade': 10}])])


@pytest.mark.parametrize('texto, inventado', [
    ('Seu carrinho: *{url}*', False),
    ('Seu carrinho: _{url}_', False),
    ('Seu carrinho: `{url}`.', False),
    ('Link: [{url}]', False),
    ('Qualquer dúvida: contato@opao.online', False),
    ('Valor: R$ 38.00, 2 un. Obrigada!', False),
    ('Paga aqui: www.golpe.com/pix', True),
    ('Acesse bit.ly/xyz', True),
    ('Veja https://opao.online/loja/outra-coisa', True),
])
def test_deteccao_de_link(texto, inventado):
    from app.services import atendimento_venda as v
    url = 'https://opao.online/loja/carrinho?link=Ab_9-x'
    permitidos = {v._normalizar_url(url)}
    achados = v._links_inventados(texto.format(url=url), permitidos,
                                  host_loja='opao.online')
    assert bool(achados) is inventado, achados


def test_prometer_equipe_sem_ferramenta_vira_passagem(app, venda_ligada):
    from app.services import atendimento_venda
    with app.app_context(), patch('anthropic.Anthropic'), patch(
            'app.services.chatbot._chamar_com_retry_sobrecarga',
            return_value=_resp(_txt('Vou passar para a nossa equipe!'))):
        out = atendimento_venda.responder([_u('Quero 300 pães')])
    assert out['acao'] == 'handoff'
    assert 'prometeu' in out['motivo']


def test_alergia_vai_para_a_equipe_sem_modelo(app, venda_ligada):
    from app.services import atendimento_venda
    with app.app_context(), patch(
            'app.services.chatbot._chamar_com_retry_sobrecarga') as api:
        out = atendimento_venda.responder(
            [_u('Meu filho tem alergia a castanha, o croissant tem?')])
    api.assert_not_called()
    assert 'alergia' in out['motivo']


def test_resposta_publica_da_equipe_no_chatwoot_cala_a_venda(app, venda_ligada):
    from app.services import atendimento_venda
    hist_cw = [{'role': 'user', 'content': 'quero pão'},
               {'role': 'assistant', 'content': 'Oi, sou a Ana', 'humano': True}]
    with app.app_context(), \
            patch('app.services.chatwoot.disponivel', return_value=True), \
            patch('app.services.chatwoot.buscar_historico',
                  return_value=hist_cw), \
            patch('app.services.chatbot._chamar_com_retry_sobrecarga') as api:
        out = atendimento_venda.responder([_u('quero pão')], conversa_id=55)
    api.assert_not_called()
    assert out['acao'] == 'handoff'


def test_encaminhamento_resolvido_nao_prende_fora_da_venda(app, venda_ligada):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.services import atendimento_restrito
    from app.utils import agora
    hist = [_u('oi', handoff_em='2026-10-01T10:00:00'),
            {'role': 'assistant', 'content': 'Obrigada pelo contato.',
             'handoff_em': '2026-10-01T10:00:00'},
            _u('Quero 2 sourdough')]
    with app.app_context():
        db.session.add(EsperaAtendimento(conversa_id='31', inicio_em=agora(),
                                         estado='resolvido',
                                         resolvido_em=agora()))
        db.session.commit()
        with patch('app.services.atendimento_venda.responder',
                   return_value={'acao': 'responder', 'texto': 'ok'}) as venda:
            atendimento_restrito.responder(hist, conversa_id='31')
            venda.assert_called_once()
        db.session.query(EsperaAtendimento).update({'estado': 'aguardando'})
        db.session.commit()
        with patch('app.services.atendimento_venda.responder') as venda:
            out = atendimento_restrito.responder(hist, conversa_id='31')
            venda.assert_not_called()
    assert out['acao'] == 'handoff'


def test_webhook_entrega_a_resposta_de_venda_sem_mudar_status(app, venda_ligada):
    """Ponta a ponta: a resposta de venda sai pelo gateway com a política do
    atendimento e a conversa continua com o bot (sem `open`, sem fila)."""
    from app.models import EsperaAtendimento

    class _Sync:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            self.target()

    app.config.update(CHATWOOT_BOT_SECRET='seg', CHATWOOT_URL='https://cw.test',
                      CHATWOOT_ACCOUNT_ID='1', CHATWOOT_BOT_TOKEN='bot')
    venda = {'acao': 'responder', 'texto': 'Seu carrinho: https://opao.online/x',
             'motivo': 'venda pelo WhatsApp', 'tools_usadas': [],
             'politica_atendimento': __import__(
                 'app.services.atendimento_restrito',
                 fromlist=['POLITICA']).POLITICA}
    with patch('threading.Thread', _Sync), \
            patch('app.services.chatwoot.buscar_historico', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa',
                  return_value={'status': 'pending'}), \
            patch('app.services.chatwoot.enviar_mensagem',
                  return_value={'ok': True}) as enviar, \
            patch('app.services.chatwoot.definir_status',
                  return_value={'ok': True}) as status, \
            patch('app.services.chatbot_vigia.disponivel', return_value=False), \
            patch('app.services.atendimento_venda.responder',
                  return_value=venda):
        r = app.test_client().post('/crm/bot?k=seg', json={
            'event': 'message_created', 'message_type': 'incoming',
            'conversation': {'id': 4242, 'status': 'pending'},
            'sender': {'name': 'Cliente'}, 'content': 'Quero 2 sourdough'})
    assert r.status_code == 200
    enviar.assert_called_once()
    assert enviar.call_args.args[1] == venda['texto']
    assert enviar.call_args.kwargs['politica_atendimento'] == venda['politica_atendimento']
    status.assert_not_called()
    with app.app_context():
        assert EsperaAtendimento.query.count() == 0
