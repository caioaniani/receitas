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

    # A janela NORMAL não existe mais nesse dia.
    _, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, '09:00–10:00', loja_id=loja.id), itens, base=base)
    assert any('janela' in e.lower() for e in erros)

    # A especial passa.
    pedido, erros = loja_checkout.criar_pedido(
        _form(DIA_DOS_PAIS, JANELA_PAIS, loja_id=loja.id), itens, base=base)
    assert erros == [] and pedido is not None
    assert pedido.janela_entrega == JANELA_PAIS
    assert pedido.data_entrega == DIA_DOS_PAIS


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


def test_post_sem_marcar_express_desbloqueia(app):
    """Checkbox ausente no POST = desmarcado (comportamento de HTML)."""
    from app.models import LojaDataEspecial
    c = _owner(app)
    c.post('/admin/loja-online/horarios-especiais/salvar',
           data={'data': '2026-08-09', 'janelas': '06:00-10:00'},
           follow_redirects=True)
    assert LojaDataEspecial.query.first().express_bloqueado is False


def test_remover_pela_tela(app):
    from app.models import LojaDataEspecial
    c = _owner(app)
    _definir()
    c.post('/admin/loja-online/horarios-especiais/remover',
           data={'data': '2026-08-09'}, follow_redirects=True)
    assert LojaDataEspecial.query.count() == 0
