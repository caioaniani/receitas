"""Horário de entrega ESPECIAL por data (27/07/2026).

Pedido do dono: no Dia dos Pais (09/08/2026) o site só pode oferecer UMA
janela de entrega — 06:00 às 10:00. Escolhas dele (AskUserQuestion):
express BLOQUEADO nesse dia e retirada TAMBÉM restrita à mesma faixa; e a
data vira CADASTRO numa tela, não constante no código.

O que estes testes travam:
- a janela especial SUBSTITUI as normais (não soma) nos dois modos;
- express fica fora do ar no dia;
- lista vazia = dia FECHADO, e NUNCA cai de volta no horário normal;
- o checkout aceita a janela especial e recusa a normal (e vice-versa).
"""
from datetime import date, datetime, timedelta

DIA_DOS_PAIS = date(2026, 8, 9)
JANELA_PAIS = '06:00–10:00'          # EN-DASH, igual JANELAS_HORARIAS


def _definir(**kw):
    from app.services import loja_data_especial
    kw.setdefault('data', DIA_DOS_PAIS)
    kw.setdefault('janelas', JANELA_PAIS)
    return loja_data_especial.definir(**kw)


def _daqui(dias):
    """Data relativa a HOJE — estes testes não podem depender de o relógio
    do CI estar antes ou depois de 09/08/2026."""
    from app.utils import hoje
    return hoje() + timedelta(days=dias)


# ── Normalização do que o dono digita ────────────────────────────────────

def test_hifen_do_teclado_vira_en_dash(app):
    """O dono digita no celular, onde en-dash não existe. Guardar o hífen
    cru criaria uma janela que a tela mostra e o checkout recusa."""
    from app.services.loja_data_especial import normalizar_janela
    assert normalizar_janela('06:00-10:00') == JANELA_PAIS
    assert normalizar_janela(' 6:00 — 10:00 ') == JANELA_PAIS
    assert normalizar_janela('06:00–10:00') == JANELA_PAIS


def test_horario_torto_nao_grava_nada(app):
    """Cadastro pela metade em horário de entrega é pior que recusar."""
    import pytest

    from app.models import LojaDataEspecial
    from app.services import loja_data_especial
    with pytest.raises(loja_data_especial.JanelaInvalida):
        _definir(janelas='06:00-10:00\nmeio-dia')
    assert LojaDataEspecial.query.count() == 0


def test_fim_antes_do_comeco_e_recusado(app):
    import pytest

    from app.services import loja_data_especial
    with pytest.raises(loja_data_especial.JanelaInvalida):
        _definir(janelas='10:00-06:00')


def test_janela_repetida_nao_duplica(app):
    from app.services.loja_data_especial import normalizar_lista
    assert normalizar_lista('06:00-10:00\n6:00–10:00') == [JANELA_PAIS]


# ── A regra no checkout ──────────────────────────────────────────────────

def test_dia_dos_pais_tem_so_a_janela_do_dono(app):
    """O caso do pedido: 09/08 oferece 06:00–10:00 e NADA das normais."""
    from app.services import loja_checkout
    _definir()
    base = datetime(2026, 8, 3, 10, 0)          # uma semana antes
    js = loja_checkout.janelas_disponiveis('agendada', DIA_DOS_PAIS, base=base)
    assert js == [JANELA_PAIS]
    assert '08:00–09:00' not in js and '17:00–18:00' not in js


def test_retirada_tambem_fica_restrita(app):
    """Decisão do dono: a restrição vale pras duas pontas."""
    from app.services import loja_checkout
    _definir()
    base = datetime(2026, 8, 3, 10, 0)
    assert loja_checkout.janelas_disponiveis(
        'retirada', DIA_DOS_PAIS, base=base) == [JANELA_PAIS]


def test_os_outros_dias_seguem_normais(app):
    """A regra é DAQUELE dia — não pode vazar pro resto do calendário."""
    from app.services import loja_checkout
    _definir()
    base = datetime(2026, 8, 3, 10, 0)
    js = loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS + timedelta(days=1), base=base)
    assert js == list(loja_checkout.JANELAS_HORARIAS)


def test_express_bloqueado_no_dia(app):
    """Sem isto "só uma janela" seria mentira: o cliente pediria entrega
    imediata às 15h e alguém sairia pra rua fora da leva única."""
    from app.services import loja_checkout
    _definir()
    meio_dia = datetime(2026, 8, 9, 12, 0)      # dentro do horário normal
    assert loja_checkout.express_disponivel(base=meio_dia) is False
    assert loja_checkout.datas_disponiveis('express', base=meio_dia) == []
    assert loja_checkout.janelas_disponiveis(
        'express', DIA_DOS_PAIS, base=meio_dia) == []


def test_express_segue_normal_na_vespera(app):
    from app.services import loja_checkout
    _definir()
    vespera = datetime(2026, 8, 8, 12, 0)
    assert loja_checkout.express_disponivel(base=vespera) is True


def test_express_liberado_se_o_dono_desmarcar(app):
    from app.services import loja_checkout
    _definir(express_bloqueado=False)
    assert loja_checkout.express_disponivel(
        base=datetime(2026, 8, 9, 12, 0)) is True


def test_janela_especial_passada_some_no_proprio_dia(app):
    """Mesma regra de sempre (LEAD_HORAS): às 9h a faixa que começa 06:00 já
    passou. O cliente não pode comprar pra uma leva que já saiu."""
    from app.services import loja_checkout
    _definir()
    assert loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 9, 9, 0)) == []
    assert loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS,
        base=datetime(2026, 8, 9, 3, 0)) == [JANELA_PAIS]


def test_distancia_nao_come_a_janela_especial(app):
    """O corte da 1ª janela da manhã por distância vale pro 08:00–09:00
    normal. Aplicá-lo aqui zeraria o dia inteiro pra quem mora longe."""
    from app.services import loja_checkout
    _definir()
    js = loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 3, 10, 0),
        distancia_km=22.0)
    assert js == [JANELA_PAIS]


# ── Dia fechado ──────────────────────────────────────────────────────────

def test_dia_sem_janela_e_fechado_e_nao_cai_no_normal(app):
    """Cair no horário normal transformaria "fechado" em "aberto o dia
    inteiro" — o pior erro possível aqui."""
    from app.services import loja_checkout, loja_data_especial
    _definir(janelas='')
    assert loja_data_especial.dia_fechado(DIA_DOS_PAIS) is True
    assert loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 3, 10, 0)) == []


def test_dia_fechado_some_do_calendario(app):
    """Senão o cliente escolhe a data e o seletor de horário vem vazio —
    beco sem saída no checkout."""
    from app.services import loja_checkout
    base = datetime(2026, 8, 3, 10, 0)
    antes = loja_checkout.datas_disponiveis('agendada', base=base)
    assert DIA_DOS_PAIS in antes
    _definir(janelas='')
    depois = loja_checkout.datas_disponiveis('agendada', base=base)
    assert DIA_DOS_PAIS not in depois
    assert len(depois) == len(antes) - 1


def test_dia_fechado_some_tambem_com_lead_de_encomenda(app):
    """O ramo D+2 (sob encomenda) monta o calendário por outro caminho."""
    from app.services import loja_checkout
    _definir(janelas='')
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=datetime(2026, 8, 3, 10, 0), lead_dias=2)
    assert DIA_DOS_PAIS not in datas


def test_dia_fechado_bloqueia_express_mesmo_desmarcado(app):
    """O manual promete que a data "some do site e ninguém consegue comprar
    pra ela". O express não olha a lista de janelas — sem esta trava, dia
    fechado com a caixa desmarcada continuaria vendendo."""
    from app.services import loja_checkout
    _definir(janelas='', express_bloqueado=False)
    meio_dia = datetime(2026, 8, 9, 12, 0)
    assert loja_checkout.express_disponivel(base=meio_dia) is False
    assert loja_checkout.datas_disponiveis('express', base=meio_dia) == []


def test_janela_ilegivel_nao_derruba_o_site(app):
    """A coluna é texto e só o cadastro pela tela normaliza. Uma linha
    escrita por fora ('6:00-10:00') fazia `int('6:')` estourar DENTRO do
    render do checkout — o site inteiro em 500."""
    from app.extensions import db
    from app.models import LojaDataEspecial
    from app.services import loja_checkout
    db.session.add(LojaDataEspecial(data=DIA_DOS_PAIS,
                                    janelas='6:00-10:00'))
    db.session.commit()
    js = loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 9, 3, 0))
    assert js == ['6:00-10:00']          # mantida, não explodiu


def test_periodo_resolve_em_uma_query(app):
    """15 datas por render viravam 15 SELECTs (e 15 logger.exception com o
    banco intermitente)."""
    from app.services import loja_data_especial
    _definir()
    datas = [DIA_DOS_PAIS + timedelta(days=i) for i in range(-3, 4)]
    regras = loja_data_especial.regras_do_periodo(datas)
    assert list(regras) == [DIA_DOS_PAIS]
    assert regras[DIA_DOS_PAIS].lista_janelas() == [JANELA_PAIS]


def test_pedido_ja_pago_fora_do_horario_aparece(app):
    """A agenda do site é de 14 dias: pode haver venda ANTERIOR ao cadastro
    marcada num horário que não existe mais. Ninguém descobriria até o dia."""
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_data_especial
    regra = _definir()
    db.session.add_all([
        PedidoOnline(codigo='VELHO1', nome_cliente='A', email_cliente='a@x.com',
                     status='pago', modo_entrega='agendada', data_entrega=DIA_DOS_PAIS,
                     janela_entrega='15:00–16:00', valor_total=10),
        PedidoOnline(codigo='NOVO1', nome_cliente='B', email_cliente='b@x.com',
                     status='pago', modo_entrega='agendada', data_entrega=DIA_DOS_PAIS,
                     janela_entrega=JANELA_PAIS, valor_total=10),
        PedidoOnline(codigo='CANC1', nome_cliente='C', email_cliente='c@x.com',
                     status='cancelado', modo_entrega='agendada',
                     data_entrega=DIA_DOS_PAIS,
                     janela_entrega='15:00–16:00', valor_total=10),
    ])
    db.session.commit()
    fora = loja_data_especial.pedidos_fora_do_horario([regra])
    codigos = [c for c, _ in fora['2026-08-09']]
    assert codigos == ['VELHO1']          # o certo e o cancelado ficam fora


# ── Cadastro (upsert / remoção) ──────────────────────────────────────────

def test_regravar_a_mesma_data_atualiza_em_vez_de_duplicar(app):
    from app.models import LojaDataEspecial
    _definir(rotulo='Dia dos Pais')
    _definir(janelas='07:00-11:00', rotulo='Dia dos Pais (mudou)')
    assert LojaDataEspecial.query.count() == 1
    r = LojaDataEspecial.query.first()
    assert r.lista_janelas() == ['07:00–11:00']
    assert r.rotulo == 'Dia dos Pais (mudou)'


def test_remover_devolve_o_horario_normal(app):
    from app.services import loja_checkout, loja_data_especial
    _definir()
    assert loja_data_especial.remover(DIA_DOS_PAIS) is True
    assert loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 3, 10, 0)
    ) == list(loja_checkout.JANELAS_HORARIAS)


def test_erro_de_banco_nao_derruba_o_checkout(app, monkeypatch):
    """Pior caso aceitável = "ofereceu o horário de sempre"; jamais
    "ninguém consegue comprar"."""
    from app.services import loja_checkout, loja_data_especial

    class Explode:
        def filter_by(self, **kw):
            raise RuntimeError('banco fora')
    monkeypatch.setattr(loja_data_especial.LojaDataEspecial, 'query', Explode())
    js = loja_checkout.janelas_disponiveis(
        'agendada', DIA_DOS_PAIS, base=datetime(2026, 8, 3, 10, 0))
    assert js == list(loja_checkout.JANELAS_HORARIAS)


# ── Ponta a ponta: o POST do checkout ────────────────────────────────────

def _carrinho(db):
    from app.models import Produto
    p = Produto(nome='Cesta Pais', categoria='Cestas', preco_site=90.0,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return [{'kind': 'produto', 'id': p.id, 'qtd': 1}]


# Endereço da NF-e: a retirada também o exige desde 20/07/2026 (a SEFAZ
# rejeita destinatário em branco).
_END_NF = {'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
           'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}


def _form(data, janela, loja_id):
    return {
        'modo_entrega': 'retirada', 'nome': 'Fulano de Tal',
        'email': 'f@x.com', 'telefone': '11999998888',
        'cpf': '529.982.247-25', 'aceite_lgpd': '1',
        'loja_id': str(loja_id),
        'data_entrega': data.isoformat(), 'janela_entrega': janela,
        **_END_NF,
    }


def test_checkout_aceita_a_janela_especial_e_recusa_a_normal(app):
    from app.extensions import db
    from app.models import AppConfig, EstoqueLoja, Loja
    from app.services import loja_checkout
    loja = Loja(nome='Brooklin', endereco='Rua X, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    itens = _carrinho(db)
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=itens[0]['id'],
                               quantidade=99))
    db.session.commit()
    _definir()
    base = datetime(2026, 8, 3, 10, 0)

    # A janela NORMAL não existe mais nesse dia — e a recusa diz QUAL é o
    # horário, em vez do genérico "o horário escolhido já passou" (que seria
    # mentira: o horário nem existe nesse dia).
    _, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, '09:00–10:00', loja_id=loja.id), itens, base=base)
    assert any(JANELA_PAIS in e for e in erros), erros
    assert not any('já passou' in e for e in erros)

    # A especial passa.
    pedido, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, JANELA_PAIS, loja_id=loja.id), itens, base=base)
    assert erros == [] and pedido is not None
    assert pedido.janela_entrega == JANELA_PAIS
    assert pedido.data_entrega == DIA_DOS_PAIS


def test_recusa_em_dia_fechado_manda_trocar_de_data(app):
    """"Escolha outro horário" num dia fechado faz o cliente ficar tentando
    horário atrás de horário que não existe."""
    from app.extensions import db
    from app.models import AppConfig, Loja
    from app.services import loja_checkout
    loja = Loja(nome='Brooklin', endereco='Rua X, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    itens = _carrinho(db)
    _definir(janelas='')
    _, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, '08:00–09:00', loja_id=loja.id), itens,
        base=datetime(2026, 8, 3, 10, 0))
    assert any('Não entregamos nesse dia' in e or 'data de entrega' in e
               for e in erros), erros


def test_pdf_do_motorista_nao_imprime_interrogacao(app):
    """O papel que vai com o motorista saía "08:00?09:00" — latin-1 não
    conhece en-dash (defeito antigo, agora com a janela do Dia dos Pais na
    jogada)."""
    from app.services.pdf import _latin1
    assert _latin1(JANELA_PAIS) == '06:00-10:00'
    assert _latin1('08:00–09:00') == '08:00-09:00'
    assert '?' not in _latin1(JANELA_PAIS)


def test_janela_especial_cabe_na_coluna(app):
    """`PedidoOnline.janela_entrega` é String(40)."""
    from app.models import PedidoOnline
    assert len(JANELA_PAIS) <= PedidoOnline.__table__.c.janela_entrega.type.length


# ── A tela do dono ───────────────────────────────────────────────────────

def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_tela_exige_owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm2', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    assert c.get('/admin/loja-online/horarios-especiais').status_code == 403


def test_tela_lista_o_dia_cadastrado(app):
    c = _owner(app)
    _definir(rotulo='Dia dos Pais')
    html = c.get('/admin/loja-online/horarios-especiais').data.decode()
    assert '09/08/2026' in html and JANELA_PAIS in html
    assert 'Dia dos Pais' in html


def test_post_salva_e_normaliza(app):
    from app.models import LojaDataEspecial
    c = _owner(app)
    r = c.post('/admin/loja-online/horarios-especiais/salvar',
               data={'data': '2026-08-09', 'rotulo': 'Dia dos Pais',
                     'janelas': '06:00-10:00', 'express_bloqueado': '1'},
               follow_redirects=True)
    assert r.status_code == 200
    regra = LojaDataEspecial.query.filter_by(data=DIA_DOS_PAIS).first()
    assert regra.lista_janelas() == [JANELA_PAIS]   # hífen virou en-dash
    assert regra.express_bloqueado is True


def test_post_com_horario_torto_avisa_e_nao_grava(app):
    from app.models import LojaDataEspecial
    c = _owner(app)
    r = c.post('/admin/loja-online/horarios-especiais/salvar',
               data={'data': '2026-08-09', 'janelas': 'de manhã'},
               follow_redirects=True)
    assert 'não parece um horário' in r.data.decode()
    assert LojaDataEspecial.query.count() == 0


def test_post_sem_horario_recusa_em_vez_de_fechar_o_dia(app):
    """O formulário nasce vazio e `definir` é upsert: reabrir a tela só pra
    corrigir o rótulo NÃO pode fechar o site no Dia dos Pais."""
    from app.models import LojaDataEspecial
    c = _owner(app)
    _definir(rotulo='Dia dos Pais')
    r = c.post('/admin/loja-online/horarios-especiais/salvar',
               data={'data': '2026-08-09', 'rotulo': 'Dia dos Pais 2026',
                     'janelas': '', 'express_bloqueado': '1'},
               follow_redirects=True)
    assert 'Informe pelo menos um horário' in r.data.decode()
    # E o cadastro anterior segue intacto — o dia NÃO fechou.
    assert LojaDataEspecial.query.first().lista_janelas() == [JANELA_PAIS]


def test_fechar_o_dia_e_gesto_explicito(app):
    from app.models import LojaDataEspecial
    c = _owner(app)
    c.post('/admin/loja-online/horarios-especiais/salvar',
           data={'data': '2026-08-09', 'janelas': '', 'fechar_dia': '1'},
           follow_redirects=True)
    assert LojaDataEspecial.query.first().fechado is True


def test_tela_avisa_de_pedido_ja_agendado_fora_do_horario(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _owner(app)
    _definir()
    db.session.add(PedidoOnline(
        codigo='VELHO9', nome_cliente='A', email_cliente='a@x.com',
        status='pago', modo_entrega='agendada', data_entrega=DIA_DOS_PAIS,
        janela_entrega='15:00–16:00', valor_total=10))
    db.session.commit()
    html = c.get('/admin/loja-online/horarios-especiais').data.decode()
    assert 'VELHO9' in html and 'já pago' in html


def test_post_sem_marcar_express_desbloqueia(app):
    """Checkbox ausente no POST = desmarcado (comportamento de HTML)."""
    from app.models import LojaDataEspecial
    c = _owner(app)
    c.post('/admin/loja-online/horarios-especiais/salvar',
           data={'data': '2026-08-09', 'janelas': '06:00-10:00'},
           follow_redirects=True)
    assert LojaDataEspecial.query.first().express_bloqueado is False


# ── O que o CLIENTE vê no checkout ───────────────────────────────────────
#
# O seletor de horário é montado no navegador a partir de uma lista que vem
# no HTML. Sem o mapa por data, o site mostraria 08:00–18:00 no Dia dos Pais
# e só o POST recusaria — com a mensagem errada. Estes testes travam isso.

def test_payload_do_checkout_leva_a_janela_do_dia(app):
    from datetime import datetime as _dt

    from app.services import loja_checkout
    _definir()
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=_dt(2026, 8, 3, 10, 0))
    mapa = loja_checkout.janelas_especiais_do_periodo(datas)
    assert mapa['2026-08-09'] == [JANELA_PAIS]


def test_payload_nao_carrega_dia_normal(app):
    """Só data COM regra entra — dia normal usa a lista global."""
    from datetime import datetime as _dt

    from app.services import loja_checkout
    _definir()
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=_dt(2026, 8, 3, 10, 0))
    mapa = loja_checkout.janelas_especiais_do_periodo(datas)
    assert list(mapa) == ['2026-08-09']


def test_payload_inclui_dia_fechado_mesmo_fora_da_lista(app):
    """O calendário do checkout é um intervalo contíguo (min/max), então o
    dia fechado continua clicável mesmo tendo saído de `datas_disponiveis`.
    Ele PRECISA vir no mapa (com []) pra a tela dizer "não entregamos nesse
    dia" em vez de mostrar o horário normal."""
    from datetime import datetime as _dt

    from app.services import loja_checkout
    _definir(janelas='')
    base = _dt(2026, 8, 3, 10, 0)
    datas = loja_checkout.datas_disponiveis('agendada', base=base)
    assert DIA_DOS_PAIS not in datas
    mapa = loja_checkout.janelas_especiais_do_periodo(datas, base=base)
    assert mapa['2026-08-09'] == []


def test_checkout_renderiza_a_janela_especial(app):
    """Ponta a ponta: o HTML entregue ao cliente carrega a janela do dia."""
    import json

    from app.extensions import db
    from app.models import AppConfig, Loja
    loja = Loja(nome='Brooklin', endereco='Rua X, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    # Data RELATIVA a hoje: o payload cobre só os próximos 14 dias, então
    # cravar 09/08/2026 deixaria a suíte vermelha a partir de 10/08/2026 —
    # e com "Wait for CI" ligado, CI vermelho trava TODO deploy, inclusive
    # hotfix (achado de revisão 27/07/2026).
    dia = _daqui(4)
    _definir(data=dia)
    itens = _carrinho(db)
    c = _owner(app)          # no host de gestão a loja só responde a staff
    with c.session_transaction() as s:
        s['carrinho'] = [{'kind': 'produto', 'id': itens[0]['id'], 'qtd': 1}]
    html = c.get('/loja/checkout').data.decode()
    assert '"janelasPorData"' in html
    bruto = html.split('id="checkout-dados" type="application/json">')[1]
    dados = json.loads(bruto.split('</script>')[0])
    assert dados['janelasPorData'][dia.isoformat()] == [JANELA_PAIS]
    # A lista normal continua lá, pros outros dias.
    assert '08:00–09:00' in dados['janelas']


def test_janelas_do_modo_nao_existe_mais(app):
    """Era código morto que devolvia a lista GLOBAL ignorando a data — quem
    o usasse furaria o dia especial em silêncio."""
    from app.services import loja_checkout
    assert not hasattr(loja_checkout, 'janelas_do_modo')


# ── O bot de atendimento ─────────────────────────────────────────────────

def test_bot_sabe_do_horario_especial(app):
    """O prompt crava "todos os dias das 8h às 18h" — no Dia dos Pais o bot
    afirmaria o horário errado no dia de maior movimento."""
    from app.services import chatbot
    dia = _daqui(5)
    _definir(data=dia, rotulo='Dia dos Pais')
    txt = chatbot._horarios_especiais_texto()
    assert dia.strftime('%d/%m') in txt and JANELA_PAIS in txt
    assert 'Dia dos Pais' in txt
    assert 'sem entrega expressa' in txt


def test_bot_nao_gasta_token_sem_data_especial(app):
    """Sem data cadastrada o bloco some — não infla prompt nem mexe no
    cache do prompt."""
    from app.services import chatbot
    assert chatbot._horarios_especiais_texto() == ''


def test_bot_avisa_dia_fechado(app):
    from app.services import chatbot
    _definir(data=_daqui(3), janelas='')
    assert 'NAO entregamos' in chatbot._horarios_especiais_texto()


def test_bot_ignora_data_fora_da_janela(app):
    """Data daqui a meses não interessa — o cliente nem consegue escolher."""
    from app.services import chatbot
    _definir(data=_daqui(90))
    assert chatbot._horarios_especiais_texto() == ''


def test_bot_ignora_data_que_ja_passou(app):
    from app.services import chatbot
    _definir(data=_daqui(-3))
    assert chatbot._horarios_especiais_texto() == ''


def test_remover_pela_tela(app):
    from app.models import LojaDataEspecial
    c = _owner(app)
    _definir()
    c.post('/admin/loja-online/horarios-especiais/remover',
           data={'data': '2026-08-09'}, follow_redirects=True)
    assert LojaDataEspecial.query.count() == 0


def test_bot_explica_que_nao_ha_hora_individual_na_faixa(app):
    """Dono 01/08/2026: "não tem um horário definido por conta da alta
    demanda" — o bot precisa gerenciar a expectativa DENTRO da faixa e
    apontar o acompanhamento ao vivo, nunca prometer hora exata."""
    from app.services import chatbot
    _definir(data=_daqui(5), rotulo='Dia dos Pais')
    txt = chatbot._horarios_especiais_texto()
    assert 'NAO existe horario individual' in txt
    assert 'NUNCA prometa hora exata' in txt
    assert 'acompanha a entrega ao vivo' in txt


def test_bot_dia_fechado_nao_ganha_papo_de_faixa(app):
    """Dia FECHADO não tem entrega — explicar "dentro da faixa" seria
    confuso. O aviso só entra quando há dia especial aberto."""
    from app.services import chatbot
    _definir(data=_daqui(3), janelas='')
    txt = chatbot._horarios_especiais_texto()
    assert 'NAO entregamos' in txt
    assert 'horario individual' not in txt


# ── Bloqueio de ITENS por data especial (07/08/2026) ─────────────────────
# Caso real: "Caixa de Mini" (categoria Mini Pães) vendida pra entrega no
# Dia dos Pais — dono: "os clientes nao poderiam comprar os minis para o
# dia 9". A data especial ganhou `bloquear_itens` (uma regra por linha:
# categoria ou nome de item); o checkout barra com a mensagem do cardápio
# especial. Vazio = sem restrição (comportamento de sempre).

def _produto_mini(db):
    from app.models import Produto
    p = Produto(nome='Caixa de Mini', categoria='Mini Pães',
                preco_site=300.0, imagem_dropbox_url='https://x/m.jpg',
                ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def test_itens_bloqueados_por_categoria_sem_acento(app):
    """'mini paes' digitado sem acento/caixa casa a categoria 'Mini Pães'."""
    from app.extensions import db
    from app.services import loja_data_especial
    p = _produto_mini(db)
    _definir(bloquear_itens='mini paes')
    itens = [{'kind': 'produto', 'id': p.id, 'nome': p.nome}]
    assert loja_data_especial.itens_bloqueados(DIA_DOS_PAIS, itens) == \
        ['Caixa de Mini']
    # Dia normal: nada barrado.
    assert loja_data_especial.itens_bloqueados(
        DIA_DOS_PAIS + timedelta(days=1), itens) == []


def test_itens_bloqueados_por_nome_do_item(app):
    from app.extensions import db
    from app.services import loja_data_especial
    p = _produto_mini(db)
    _definir(bloquear_itens='caixa de mini')
    itens = [{'kind': 'produto', 'id': p.id, 'nome': p.nome}]
    assert loja_data_especial.itens_bloqueados(DIA_DOS_PAIS, itens) == \
        ['Caixa de Mini']


def test_sem_bloqueio_nada_barrado_e_none_preserva(app):
    """Regra sem bloqueio não barra nada; `definir(bloquear_itens=None)`
    (chamador antigo, ex. seed) NÃO apaga o que está gravado; '' limpa."""
    from app.extensions import db
    from app.services import loja_data_especial
    p = _produto_mini(db)
    _definir()                              # sem bloqueio
    itens = [{'kind': 'produto', 'id': p.id, 'nome': p.nome}]
    assert loja_data_especial.itens_bloqueados(DIA_DOS_PAIS, itens) == []

    _definir(bloquear_itens='Mini Pães')
    regra = _definir()                      # None: preserva
    assert regra.lista_bloqueios() == ['Mini Pães']
    regra = _definir(bloquear_itens='')     # '': limpa de propósito
    assert regra.lista_bloqueios() == []


def test_checkout_barra_item_bloqueado_na_data(app):
    """Checkout recusa o item barrado pra data especial, citando o rótulo
    ('cardápio especial'); pra OUTRA data o mesmo carrinho passa."""
    from app.extensions import db
    from app.models import AppConfig, Loja
    from app.services import loja_checkout
    loja = Loja(nome='Brooklin', endereco='Rua X, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    p = _produto_mini(db)
    itens = [{'kind': 'produto', 'id': p.id, 'qtd': 1}]
    _definir(rotulo='Dia dos Pais', bloquear_itens='Mini Pães')
    base = datetime(2026, 8, 3, 10, 0)

    _, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, JANELA_PAIS, loja_id=loja.id), itens, base=base)
    assert any('Caixa de Mini' in e and 'cardápio especial' in e
               for e in erros), erros

    # Mesmo carrinho, dia normal (D+2, sem regra): passa.
    outra = date(2026, 8, 5)
    pedido, erros = loja_checkout.criar_pedido(
        _form(outra, '10:00–11:00', loja_id=loja.id), itens, base=base)
    assert erros == [] and pedido is not None


def test_checkout_nao_barra_item_fora_da_regra(app):
    """Categoria diferente não é afetada — o bloqueio é curadoria pontual,
    não fechamento do dia."""
    from app.extensions import db
    from app.models import AppConfig, Loja
    from app.services import loja_checkout
    loja = Loja(nome='Brooklin', endereco='Rua X, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    itens = _carrinho(db)                   # 'Cesta Pais', categoria Cestas
    _definir(bloquear_itens='Mini Pães')
    pedido, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, JANELA_PAIS, loja_id=loja.id), itens,
        base=datetime(2026, 8, 3, 10, 0))
    assert erros == [] and pedido is not None


def test_tela_salva_edita_e_preserva_bloqueios(app, owner_user):
    """POST da tela grava; o botão Editar carrega o valor (data-bloqueios);
    salvar de novo com o campo intacto não apaga."""
    from app.models import LojaDataEspecial
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    r = c.post('/admin/loja-online/horarios-especiais/salvar', data={
        'data': DIA_DOS_PAIS.isoformat(), 'rotulo': 'Dia dos Pais',
        'janelas': '06:00-10:00', 'express_bloqueado': '1',
        'bloquear_itens': 'Mini Pães\nCaixa de Mini',
    })
    assert r.status_code in (302, 303)
    regra = LojaDataEspecial.query.filter_by(data=DIA_DOS_PAIS).first()
    assert regra.lista_bloqueios() == ['Mini Pães', 'Caixa de Mini']

    body = c.get('/admin/loja-online/horarios-especiais').get_data(
        as_text=True)
    assert 'name="bloquear_itens"' in body
    assert 'data-bloqueios="Mini Pães' in body
    assert '🚫 Mini Pães' in body


# ── Faixa larga corta pelo FIM, não pelo início (09/08/2026, caso real) ──

def test_faixa_larga_vale_ate_o_lead_do_fim(app):
    """Caso Roberta, 07:29 do Dia dos Pais: o corte de janela passada
    comparava o INÍCIO — a faixa 06:00–10:00 "passava" às ~4h da manhã e o
    dia inteiro sumia do calendário ("entrega só amanhã") com 6h de janela
    pela frente. Viável = FIM além de agora + LEAD_HORAS."""
    from app.services import loja_checkout
    _definir()
    as_729 = datetime(2026, 8, 9, 7, 29)
    js = loja_checkout.janelas_disponiveis('agendada', DIA_DOS_PAIS,
                                           base=as_729)
    assert js == [JANELA_PAIS]                  # ainda vendável
    assert DIA_DOS_PAIS in loja_checkout.datas_disponiveis(
        'agendada', base=as_729)                # hoje volta pro calendário
    # Às 08:00 o lead de 2h encosta no fim (10h): aí sim fecha.
    as_8 = datetime(2026, 8, 9, 8, 0)
    assert loja_checkout.janelas_disponiveis('agendada', DIA_DOS_PAIS,
                                             base=as_8) == []
    assert DIA_DOS_PAIS not in loja_checkout.datas_disponiveis(
        'agendada', base=as_8)


def test_janela_de_1h_mantem_o_corte_historico(app):
    """O corte pelo fim NÃO afrouxa dia normal: às 07:29, a 08:00–09:00
    continua fora (fim 9 <= 7+2) e a 09:00–10:00 continua dentro."""
    from app.services import loja_checkout
    from app.utils import hoje as _hoje
    dia = _hoje()
    base = datetime(dia.year, dia.month, dia.day, 7, 29)
    js = loja_checkout.janelas_disponiveis('agendada', dia, base=base)
    assert '08:00–09:00' not in js
    assert '09:00–10:00' in js
