"""Conferência em lote (balanço de loja): cola a contagem, o sistema SETA cada
item pro valor contado e registra ajuste_conferencia. Regras do dono:
- qtd preenchida > 0 → seta; qtd 0 → zera; em branco/unidade → não mexe;
- item ausente da contagem fica intacto; nome sem match vira pendente.
"""
from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, Produto, Receita
from app.services import estoque_loja_lote as svc


def _receita(nome, categoria='Paes'):
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Ribeiro do Vale'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _estoque(loja, receita, qtd):
    e = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(e)
    db.session.commit()
    return e


# ── parser ────────────────────────────────────────────────────────────────
def test_parser_aceita_zero_e_marca_branco_e_unidade():
    linhas = ('Croissant: 347\n'          # normal
              'Choconana: 0\n'            # zero é válido (zera)
              'Brioche\n'                 # sem qtd → em_branco
              'Sourdough:\n'              # qtd vazia → em_branco
              'Nozes Caramelizadas: 100 g\n'  # unidade → não chuta
              'Pain au Chocolat\t98\n'    # separador TAB (Excel)
              'Pão Francês   170')        # separador 2+ espaços
    out = {p.get('nome'): p for p in svc.parsear_conferencia(linhas)}
    assert out['Croissant']['quantidade'] == 347
    assert out['Choconana']['quantidade'] == 0            # ZERO válido, sem erro
    assert 'quantidade' not in out['Choconana'] or out['Choconana'].get('erro') is None
    assert out['Brioche']['erro'] == 'em_branco'
    assert out['Sourdough']['erro'] == 'em_branco'
    assert out['Nozes Caramelizadas']['erro'] == 'unidade'
    assert out['Pain au Chocolat']['quantidade'] == 98    # TAB
    assert out['Pão Francês']['quantidade'] == 170        # espaços


# ── resolver (SET, diff) ────────────────────────────────────────────────────
def test_resolver_calcula_diff_absoluto(app):
    loja = _loja()
    r = _receita('Croissant Tradicional')
    _estoque(loja, r, 500)
    parse = svc.parsear_conferencia('Croissant Tradicional: 347')
    res = svc.resolver_conferencia(parse, loja.id)[0]
    assert res['estoque_atual'] == 500
    assert res['novo'] == 347                 # SETA, não soma
    assert res['diff'] == -153                # 347 - 500


# ── aplicar ─────────────────────────────────────────────────────────────────
def test_aplicar_seta_valor_contado_e_registra_ajuste(app, admin_user):
    loja = _loja()
    r = _receita('Croissant Tradicional')
    e = _estoque(loja, r, 500)
    parse = svc.parsear_conferencia('Croissant Tradicional: 347')
    resolvidos = svc.resolver_conferencia(parse, loja.id)
    out = svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    assert len(out['aplicados']) == 1 and out['aplicados'][0]['diff'] == -153
    assert db.session.get(EstoqueLoja, e.id).quantidade == 347        # SETADO
    mov = MovEstoqueLoja.query.filter_by(estoque_loja_id=e.id,
                                         tipo='ajuste_conferencia').first()
    assert mov is not None and mov.quantidade == -153                 # baixa assinada


def test_aplicar_zero_zera_o_item(app, admin_user):
    loja = _loja()
    r = _receita('Danish de Maçã')
    e = _estoque(loja, r, 11)
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Danish de Maçã: 0'), loja.id)
    svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    assert db.session.get(EstoqueLoja, e.id).quantidade == 0          # zerado


def test_aplicar_em_branco_nao_mexe(app, admin_user):
    loja = _loja()
    r = _receita('Brioche')
    e = _estoque(loja, r, 14)
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Brioche'), loja.id)               # sem qtd
    out = svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    assert db.session.get(EstoqueLoja, e.id).quantidade == 14         # intacto
    assert out['ignorados'][0]['motivo'] == 'em_branco'


def test_aplicar_item_ausente_fica_intacto(app, admin_user):
    """Item que NÃO veio na contagem não é tocado."""
    loja = _loja()
    r1 = _receita('Croissant Tradicional')
    r2 = _receita('Pão Francês')
    e1 = _estoque(loja, r1, 500)
    e2 = _estoque(loja, r2, 170)
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Croissant Tradicional: 347'), loja.id)
    svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    assert db.session.get(EstoqueLoja, e1.id).quantidade == 347       # conferido
    assert db.session.get(EstoqueLoja, e2.id).quantidade == 170       # intacto


def test_aplicar_nome_sem_match_vira_pendente(app, admin_user):
    loja = _loja()
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Produto Inexistente XPTO: 5'), loja.id)
    svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    ep = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     nome_pendente='Produto Inexistente XPTO').first()
    assert ep is not None and ep.quantidade == 5


def test_aplicar_zero_sem_estoque_e_noop(app, admin_user):
    """Nome sem match (ou item novo) com 0 não cria linha nem movimento."""
    loja = _loja()
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Coisa Que Nao Existe: 0'), loja.id)
    out = svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    assert EstoqueLoja.query.filter_by(loja_id=loja.id).count() == 0
    assert out['ignorados'][0]['motivo'] == 'zero_sem_estoque'


def test_aplicar_produto_novo_na_loja_cria_linha(app, admin_user):
    """Item que casa com o catálogo mas ainda não tinha estoque na loja: cria."""
    loja = _loja()
    p = Produto(nome='Granola 500g', ativo=True)
    db.session.add(p)
    db.session.commit()
    resolvidos = svc.resolver_conferencia(
        svc.parsear_conferencia('Granola 500g: 15'), loja.id)
    svc.aplicar_conferencia(resolvidos, loja.id, admin_user)
    ep = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=p.id).first()
    assert ep is not None and ep.quantidade == 15


# ── rotas ───────────────────────────────────────────────────────────────────
def _login(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_rota_preview_renderiza(app, admin_user):
    loja = _loja()
    r = _receita('Croissant Tradicional')
    _estoque(loja, r, 500)
    c = _login(app, admin_user)
    resp = c.post('/pedidos/estoque-loja/conferencia-lote',
                  data={'loja_id': loja.id, 'texto': 'Croissant Tradicional: 347'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Croissant Tradicional' in body
    assert '-153' in body                     # o ajuste aparece no preview


def test_rota_aplicar_seta_estoque(app, admin_user):
    loja = _loja()
    r = _receita('Croissant Tradicional')
    e = _estoque(loja, r, 500)
    c = _login(app, admin_user)
    resp = c.post('/pedidos/estoque-loja/conferencia-lote/aplicar',
                  data={'loja_id': loja.id, 'texto': 'Croissant Tradicional: 347'})
    assert resp.status_code in (302, 303)
    assert db.session.get(EstoqueLoja, e.id).quantidade == 347


def test_rota_exige_admin(app):
    c = app.test_client()
    resp = c.post('/pedidos/estoque-loja/conferencia-lote/aplicar',
                  data={'loja_id': 1, 'texto': 'X: 1'})
    assert resp.status_code in (301, 302, 403)
