"""A capa pública reflete a curadoria do plano sem alterar sua composição."""
import pytest
from test_kits_checkout import ambiente as ambiente
from test_kits_cliente_imagens import FOTOS, _pagina
from test_kits_opcoes_cliente import _chave
from test_kits_opcoes_cliente import cenario as cenario
from test_kits_opcoes_cliente import plano as plano
from test_kits_sucos import _comprar, _form
from test_kits_sucos_owner import cliente_owner

from app.extensions import db
from app.models import (
    CompraKit,
    EntregaKit,
    EstoqueLoja,
    EstoqueSitePlano,
    KitCafeFoto,
    PedidoOnline,
    PedidoOnlineItem,
    Receita,
)
from app.services import kits_cafe

pytestmark = pytest.mark.loja_host


@pytest.fixture
def plano_fotos(plano):
    kit, laranja, verde, almond, nutella, tradicional, integral, _ = plano
    pao = db.session.get(Receita, kit.itens[0].receita_id)
    for indice, item in enumerate([pao, laranja, verde, almond, nutella, tradicional, integral]):
        item.imagem_url = f'{FOTOS}{indice}.jpg'
    db.session.commit()
    return {'kit': kit, 'pao': pao, 'laranja': laranja, 'verde': verde,
            'almond': almond, 'nutella': nutella, 'tradicional': tradicional,
            'integral': integral}


@pytest.fixture(params=['/loja/', '/loja/kits-cafe/{id}'])
def caminho(request, plano_fotos):
    return request.param.format(id=plano_fotos['kit'].id)


def _selecionar(kit, itens):
    kit.fotos = []
    db.session.flush()
    for ordem, item in enumerate(itens, start=1):
        kind = _chave(item).split(':')[0]
        kit.fotos.append(KitCafeFoto(ordem=ordem, kind=kind, **{f'{kind}_id': item.id}))
    db.session.commit()


def _estoques():
    return [(e.id, e.quantidade, e.quantidade_reservada)
            for e in EstoqueLoja.query.order_by(EstoqueLoja.id)]


def test_publico_respeita_ordem_das_tres_fotos_de_grupos_e_suco(
        app, plano_fotos, caminho):
    cenario = plano_fotos
    kit = cenario['kit']
    escolhidos = [cenario['integral'], cenario['nutella'], cenario['verde']]
    cenario['integral'].imagem_dropbox_url = FOTOS + 'sourdough-atualizado.jpg'
    antes = (kits_cafe.preco_kit(kit), kits_cafe.itens_do_kit(kit), _estoques())
    _selecionar(kit, escolhidos)
    _, fotos = _pagina(app, caminho)
    assert [(foto['src'], foto['alt']) for foto in fotos] == [
        (item.imagem_dropbox_url or item.imagem_url, item.nome) for item in escolhidos]
    assert (kits_cafe.preco_kit(kit), kits_cafe.itens_do_kit(kit), _estoques()) == antes
    assert CompraKit.query.count() == PedidoOnline.query.count() == 0


@pytest.mark.parametrize('quantidade', [1, 2])
def test_uma_ou_duas_fotos_nao_sao_completadas_com_capa_automatica(
        app, plano_fotos, caminho, quantidade):
    escolhidos = [plano_fotos['tradicional'], plano_fotos['laranja']][:quantidade]
    _selecionar(plano_fotos['kit'], escolhidos)
    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [item.imagem_url for item in escolhidos]
    assert plano_fotos['pao'].imagem_url not in [foto['src'] for foto in fotos]


def test_sem_curadoria_preserva_fallback_atual_dos_itens_fixos(app, plano_fotos, caminho):
    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [plano_fotos['pao'].imagem_url]


def test_foto_apagada_do_catalogo_e_ignorada_sem_quebrar_kit(app, plano_fotos, caminho):
    kit = plano_fotos['kit']
    pao, integral = plano_fotos['pao'], plano_fotos['integral']
    _selecionar(kit, [integral, pao])
    integral.imagem_url = integral.imagem_dropbox_url = None
    db.session.commit()
    html, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [pao.imagem_url]
    assert kit.nome in html and kit in kits_cafe.publicados()


def test_nenhuma_foto_manual_valida_retorna_ao_fallback(app, plano_fotos, caminho):
    integral = plano_fotos['integral']
    _selecionar(plano_fotos['kit'], [integral])
    integral.imagem_url = integral.imagem_dropbox_url = None
    db.session.commit()
    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [plano_fotos['pao'].imagem_url]


def test_foto_de_opcao_removida_da_composicao_nao_e_exibida(app, plano_fotos, caminho):
    kit = plano_fotos['kit']
    _selecionar(kit, [plano_fotos['integral'], plano_fotos['laranja']])
    kit.opcoes = [opcao for opcao in kit.opcoes if opcao.grupo != 'sourdough']
    db.session.commit()
    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [plano_fotos['laranja'].imagem_url]


def test_urls_que_passaram_a_ser_iguais_nao_repetem_imagem(app, plano_fotos, caminho):
    integral, verde = plano_fotos['integral'], plano_fotos['verde']
    _selecionar(plano_fotos['kit'], [integral, verde])
    verde.imagem_dropbox_url = integral.imagem_url
    db.session.commit()
    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [integral.imagem_url]


def test_trocar_capa_nao_reescreve_pedidos_existentes_nem_reservas(
        app, owner_user, plano_fotos, monkeypatch):
    cenario = plano_fotos
    kit = cenario['kit']
    compra, erros = _comprar(kit, _form(
        suco_id=str(cenario['verde'].id), escolha_croissant=_chave(cenario['nutella']),
        escolha_sourdough=_chave(cenario['integral'])))
    assert compra is not None and not erros

    def snapshot():
        modelos = [CompraKit, EntregaKit, PedidoOnline, PedidoOnlineItem,
                   EstoqueLoja, EstoqueSitePlano]
        return {modelo.__tablename__: [tuple(row) for row in db.session.execute(
            modelo.__table__.select().order_by(*modelo.__table__.primary_key.columns))]
            for modelo in modelos}

    antes = snapshot()
    form = {'kit_id': str(kit.id), 'nome': kit.nome, 'descricao': kit.descricao,
            'acao': 'publicar', 'configurar_fotos': '1',
            'foto_1': _chave(cenario['tradicional']), 'foto_2': _chave(cenario['almond']),
            'foto_3': _chave(cenario['laranja']),
            **{f'qtd_{item.kind}_{item.receita_id or item.produto_id}': str(item.quantidade)
               for item in kit.itens}}
    # O POST administrativo ocorre no domínio interno, não no host público.
    monkeypatch.setitem(app.config, 'LOJA_HOSTS', 'opao.online')
    resposta = cliente_owner(app, owner_user).post('/admin/kits-cafe/salvar', data=form)
    assert resposta.status_code == 302, resposta.get_data(as_text=True)
    db.session.expire_all()
    assert snapshot() == antes
    assert len(kit.fotos) == 3
