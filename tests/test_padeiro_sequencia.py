"""Consulta da sequência na TV leva ao registro canônico, sem produzir ao navegar."""
import re
from datetime import date, timedelta
from html import escape, unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MassaBase,
    MassaBaseItem,
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
    Usuario,
)
from app.utils import agora

DIA = date(2026, 9, 10)
ETAPAS = [
    ('Amassamento', 15, 'amassadeira', True),
    ('Modelagem', 10, 'bancada', True),
    ('Assar', 20, 'forno', True),
]


@pytest.fixture(autouse=True)
def _dia_fixo(congela_hoje):
    congela_hoje(2026, 9, 10, hora=2)


def _receita(nome, etapas=ETAPAS, agua=70, lead=0):
    rec = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                  rendimento_unidade='un', peso_base=1000,
                  capacidade_amassadeira_g=50000, dias_producao=lead)
    db.session.add(rec)
    db.session.flush()
    for nome_ingrediente, percentual in [('Farinha', 100), ('Água', agua)]:
        db.session.add(ReceitaIngrediente(
            receita_id=rec.id, tipo='mp', ingrediente_nome=nome_ingrediente,
            porcentagem=percentual))
    for ordem, (nome_etapa, duracao, equipamento, ativa) in enumerate(etapas):
        db.session.add(ReceitaEtapa(
            receita_id=rec.id, ordem=ordem, nome=nome_etapa,
            duracao_min=duracao, equipamento=equipamento, ativa=ativa))
    db.session.commit()
    return rec


def _plano(dia, receitas, enviado=True, produzido=3):
    plano = PlanejamentoProducao(
        data=dia, origem='cronograma', status='aprovado',
        enviado_ao_padeiro=enviado)
    db.session.add(plano)
    db.session.flush()
    itens = [PlanejamentoItem(
        planejamento_id=plano.id, receita_id=rec.id, multiplicador=1,
        qtd_alvo=10, produzido_qtd=produzido) for rec in receitas]
    db.session.add_all(itens)
    db.session.commit()
    return plano, itens


def _login(app, usuario):
    cliente = app.test_client()
    resposta = cliente.post('/auth/login', data={
        'login': usuario.login, 'senha': '123'})
    assert resposta.status_code == 302
    return cliente


def _sequencia(cliente, dia=DIA):
    resposta = cliente.get(f'/padeiro/gantt?data={dia.isoformat()}')
    assert resposta.status_code == 200
    return resposta.get_data(as_text=True)


def _paineis(html, nome):
    paineis = re.findall(
        r'<section\b[^>]*class="product-panel"[^>]*>.*?</section>',
        html, flags=re.DOTALL)
    return [painel for painel in paineis
            if re.search(r'<h2\b[^>]*>' + re.escape(escape(nome)) + r'</h2>',
                         painel)]


def _link(html, texto):
    encontrados = re.findall(
        r'<a\b[^>]*href="([^"]+)"[^>]*>' + re.escape(texto) + r'</a>', html)
    assert len(encontrados) == 1, f'Esperado um link {texto!r}'
    return unescape(encontrados[0])


def _confere_destino(cliente, href, item, dia):
    destino = urlsplit(href)
    assert destino.path == '/padeiro/'
    assert parse_qs(destino.query) == {'data': [dia.isoformat()]}
    assert destino.fragment == f'producao-item-{item.id}'
    resposta = cliente.get(f'{destino.path}?{destino.query}')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert f'id="{destino.fragment}"' in html
    assert f'action="/padeiro/produzir-plano/{item.id}"' in html


def test_registro_leva_ao_item_e_dia_selecionados(app, admin_user):
    receita = _receita('Brioche da ordem')
    dia_selecionado = DIA + timedelta(days=1)
    _, itens = _plano(dia_selecionado, [receita])
    cliente = _login(app, admin_user)

    painel, = _paineis(_sequencia(cliente, dia_selecionado), receita.nome)
    _confere_destino(cliente, _link(painel, 'Registrar produção'),
                     itens[0], dia_selecionado)


def test_sequencia_mostra_instrucao_da_ficha_sem_executar_html(app, admin_user):
    receita = _receita('Pão com instrução')
    receita.etapas[0].descricao = 'Bater na velocidade 1 por 18 minutos.\nConferir <massa> antes de seguir.'
    db.session.commit()
    _plano(DIA, [receita])
    cliente = _login(app, admin_user)
    html = _sequencia(cliente)
    assert 'Bater na velocidade 1 por 18 minutos.\nConferir &lt;massa&gt; antes de seguir.' in html
    assert 'Conferir <massa>' not in html


def test_continuacao_registra_item_da_origem_mesmo_com_receita_repetida_hoje(
        app, admin_user):
    receita = _receita('Sourdough da madrugada', etapas=[
        ('Amassar', 20, 'amassadeira', True),
        ('Fermentação', 1440, 'camara_fria', False),
        ('Assar', 30, 'forno', True),
    ], lead=1)
    ontem = DIA - timedelta(days=1)
    _, itens_ontem = _plano(ontem, [receita])
    _, itens_hoje = _plano(DIA, [receita], produzido=0)
    cliente = _login(app, admin_user)

    paineis = _paineis(_sequencia(cliente), receita.nome)
    assert len(paineis) == 2
    continuacao, = [p for p in paineis if 'Finalização prevista' in p]
    novo_preparo, = [p for p in paineis if 'Finalização prevista' not in p]
    _confere_destino(cliente, _link(continuacao, 'Registrar produção'),
                     itens_ontem[0], ontem)
    _confere_destino(cliente, _link(novo_preparo, 'Registrar produção'),
                     itens_hoje[0], DIA)


@pytest.mark.parametrize('com_outro_preparo', [False, True])
def test_item_sem_etapas_continua_acessivel_para_registro(
        app, admin_user, com_outro_preparo):
    receita = _receita('Preparo sem ficha', etapas=[])
    receitas = [receita]
    if com_outro_preparo:
        receitas.append(_receita('Pão com ficha'))
    _, itens = _plano(DIA, receitas)
    cliente = _login(app, admin_user)

    html = _sequencia(cliente)
    painel, = _paineis(html, receita.nome)
    painel_id = re.search(r'\bid="([^"]+)"', painel).group(1)
    assert f'href="#{painel_id}"' in html
    _confere_destino(cliente, _link(painel, 'Registrar produção'), itens[0], DIA)


def test_base_abre_preparo_e_so_derivados_tem_registro(app, admin_user):
    receitas = [_receita('Pão Francês da base'),
                _receita('Pão Integral da base', agua=80)]
    base = MassaBase(nome='Base da ordem')
    db.session.add(base)
    db.session.flush()
    for ordem, rec in enumerate(receitas):
        db.session.add(MassaBaseItem(
            massa_base_id=base.id, receita_id=rec.id, ordem=ordem))
    db.session.commit()
    dia_selecionado = DIA - timedelta(days=1)
    _, itens = _plano(dia_selecionado, receitas)
    cliente = _login(app, admin_user)

    html = _sequencia(cliente, dia_selecionado)
    painel_base, = _paineis(html, f'Massa base: {base.nome}')
    assert 'Registrar produção' not in painel_base
    destino = urlsplit(_link(painel_base, 'Ver preparo da base'))
    assert destino.path == '/padeiro/'
    assert parse_qs(destino.query) == {'data': [dia_selecionado.isoformat()]}
    assert destino.fragment == f'producao-base-{base.id}-{dia_selecionado}'
    painel_html = cliente.get(f'{destino.path}?{destino.query}').get_data(as_text=True)
    assert f'id="{destino.fragment}"' in painel_html
    for receita, item in zip(receitas, itens):
        painel, = _paineis(html, receita.nome)
        _confere_destino(cliente, _link(painel, 'Registrar produção'),
                         item, dia_selecionado)


@pytest.mark.parametrize(('estado', 'offset_tela', 'mostra_madrugada'), [
    ('aberta', 0, True),
    ('produzida', 0, False),
    ('dispensada', 0, False),
    ('encerrada', 0, False),
    ('rascunho', 0, False),
    ('aberta', -1, False),
    ('aberta', 1, False),
    ('antiga', 0, False),
])
def test_madrugada_so_oferece_ordem_de_ontem_aberta_na_visao_de_hoje(
        app, admin_user, estado, offset_tela, mostra_madrugada):
    receita = _receita('Pão da virada')
    origem = DIA - timedelta(days=2 if estado == 'antiga' else 1)
    _, itens = _plano(origem, [receita], enviado=estado != 'rascunho',
                      produzido=10 if estado == 'produzida' else 3)
    if estado == 'dispensada':
        itens[0].dispensada_em = agora()
    elif estado == 'encerrada':
        itens[0].falta_encerrada_em = agora()
    db.session.commit()
    cliente = _login(app, admin_user)

    html = _sequencia(cliente, DIA + timedelta(days=offset_tela))
    assert ('Ver ordem da madrugada' in html) is mostra_madrugada
    if mostra_madrugada:
        href = _link(html, 'Ver ordem da madrugada')
        assert href == f'/padeiro/gantt?data={origem.isoformat()}'
        painel, = _paineis(cliente.get(href).get_data(as_text=True), receita.nome)
        _confere_destino(cliente, _link(painel, 'Registrar produção'), itens[0], origem)


@pytest.mark.parametrize('papel', ['admin', 'padeiro'])
def test_edicao_da_ordem_e_das_etapas_restrita_ao_admin(app, admin_user, papel):
    usuario = admin_user
    if papel == 'padeiro':
        usuario = Usuario(nome='Padeiro da TV', login='padeiro-tv', papel=papel)
        usuario.set_senha('123')
        db.session.add(usuario)
        db.session.commit()
    receita = _receita('Pão de acesso')
    _, itens = _plano(DIA, [receita])
    cliente = _login(app, usuario)

    html = _sequencia(cliente)
    assert ('Editar ordem' in html) is (papel == 'admin')
    assert (f'/receitas/{receita.id}/etapas' in html) is (papel == 'admin')
    painel, = _paineis(html, receita.nome)
    _confere_destino(cliente, _link(painel, 'Registrar produção'), itens[0], DIA)


def test_consultar_sequencia_e_destinos_nao_altera_producao_nem_estoque(
        app, admin_user):
    from app.services.estoque_congelados import entrada_producao

    receitas = [_receita('Pão em preparo'), _receita('Pão sem etapas', etapas=[])]
    _plano(DIA, receitas)
    entrada_producao(receita_id=receitas[0].id, quantidade=3,
                     usuario_id=admin_user.id)
    mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=5)
    db.session.add(mp)
    db.session.flush()
    db.session.add(MovimentacaoEstoque(
        materia_prima_id=mp.id, tipo='entrada', quantidade=20,
        usuario_id=admin_user.id))
    db.session.commit()
    cliente = _login(app, admin_user)

    modelos = (PlanejamentoProducao, PlanejamentoItem, EstoqueProducao,
               MovEstoqueProducao, MateriaPrima, MovimentacaoEstoque, PreBaixaMP)

    def estado():
        return {modelo.__tablename__: db.session.execute(
            modelo.__table__.select().order_by(modelo.id)).all()
                for modelo in modelos}

    antes = estado()
    html = _sequencia(cliente)
    assert '<form' not in html
    for receita in receitas:
        painel, = _paineis(html, receita.nome)
        href = _link(painel, 'Registrar produção')
        assert cliente.get(href).status_code == 200
    # Voltar à sequência também deve ser apenas leitura, sem avanço automático.
    _sequencia(cliente)
    db.session.expire_all()
    assert estado() == antes


def test_preparo_em_gramas_tem_mesma_unidade_na_sequencia_registro_e_modal(app, admin_user):
    receita = _receita('Granola pesada', etapas=[])
    receita.peso_unitario = 1
    receita.rendimento_unidade = 'g'
    _, itens = _plano(DIA, [receita], produzido=338)
    item = itens[0]
    item.qtd_alvo = 17338
    db.session.commit()
    cliente = _login(app, admin_user)

    html = _sequencia(cliente)
    painel, = _paineis(html, receita.nome)
    assert '17000 <small>g</small>' in painel
    destino = _link(painel, 'Registrar produção')
    registro = cliente.get(destino).get_data(as_text=True)
    assert 'data-unidade="g"' in registro
    assert '>17000</b> g' in registro
    assert 'name="unidades" min="1" value="17000"' in registro
    modal = cliente.get(f'/padeiro/receita/{receita.id}.json?unidades=17000').get_json()
    assert modal['unidades'] == 17000
    assert modal['unidade_producao'] == 'g'

    resposta = cliente.post(f'/padeiro/produzir-plano/{item.id}', data={
        'unidades': '1000'}, follow_redirects=True)
    assert resposta.status_code == 200
    assert 'Produzido 1000 g' in resposta.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(PlanejamentoItem, item.id).produzido_qtd == 1338
    estoque = EstoqueProducao.query.filter_by(receita_id=receita.id).one()
    assert estoque.quantidade == 1000
