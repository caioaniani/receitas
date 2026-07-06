"""Emissão de NF-e via Tiny pra vendas B2B (06/07/2026).

Espelha o fluxo da loja online (motor comum `tiny_nf.emitir_nf_generico`):
nota.fiscal.incluir com cabeçalho fiscal explícito + cliente PJ com
endereço estruturado + itens por SKU → nota.fiscal.emitir. Tiny mockado.
"""
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import ClienteB2B, Produto, VendaB2B, VendaB2BItem


def _cliente_completo():
    c = ClienteB2B(
        nome='Restaurante Bom Prato', cnpj_cpf='11.222.333/0001-44',
        email='compras@bomprato.com.br', telefone='11 4002-8922',
        endereco_logradouro='Rua das Laranjeiras',
        endereco_numero='100', endereco_complemento='sala 2',
        endereco_bairro='Centro', endereco_cep='04568-001',
        endereco_cidade='São Paulo', endereco_uf='sp', ativo=True)
    db.session.add(c)
    db.session.commit()
    return c


def _venda(cliente, sku=None, desconto=0.0):
    from app.services import tiny_nf
    p = Produto(nome='Pao Frances Congelado', ativo=True)
    db.session.add(p)
    db.session.flush()
    if sku:
        tiny_nf.definir_sku('produto', p.id, sku)
    v = VendaB2B(cliente_id=cliente.id if cliente else None,
                 cliente_nome=None if cliente else 'Avulso',
                 valor_total=Decimal('100.00'))
    db.session.add(v)
    db.session.flush()
    db.session.add(VendaB2BItem(
        venda_id=v.id, produto_id=p.id, quantidade=10,
        preco_unitario=Decimal('10.00'), desconto_percentual=desconto))
    db.session.commit()
    return v


def test_emitir_nf_payload_pj_endereco_e_sku(app):
    """Payload leva tipo_pessoa J (CNPJ), endereço estruturado, SKU do
    mapeamento compartilhado com o site e cabeçalho fiscal explícito."""
    from app.services import tiny_nf_b2b
    with app.app_context():
        v = _venda(_cliente_completo(), sku='SKU-B2B')
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-77',
                                 'numero': '11500'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}) as emi:
            res = tiny_nf_b2b.emitir_nf(v)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-77'
        nota = inc.call_args[0][0]
        assert nota['tipo'] == 'S'
        assert nota['serie'] == '1'
        assert nota['natureza_operacao'] == 'Venda de mercadorias'
        assert nota['frete_por_conta'] == 'R'
        cli = nota['cliente']
        assert cli['tipo_pessoa'] == 'J'
        assert cli['cpf_cnpj'] == '11222333000144'   # só dígitos
        assert cli['endereco'] == 'Rua das Laranjeiras'
        assert cli['numero'] == '100'
        assert cli['bairro'] == 'Centro'
        assert cli['uf'] == 'SP'                     # normalizado
        item = nota['itens'][0]['item']
        assert item['codigo'] == 'SKU-B2B'
        assert item['quantidade'] == 10.0
        assert item['valor_unitario'] == 10.0
        emi.assert_called_once_with('nf-77')
        db.session.refresh(v)
        assert v.tiny_nota_fiscal_id == 'nf-77'
        assert v.nf_emitida_em is not None
        assert v.nf_numero == '11500'                # numero vindo do Tiny


def test_desconto_do_item_vai_no_valor_unitario(app):
    """A NF sai com o preço EFETIVO (desconto aplicado) — mesma conta em
    Decimal do `VendaB2BItem.valor_total`."""
    from app.services import tiny_nf_b2b
    with app.app_context():
        v = _venda(_cliente_completo(), sku='SKU-D', desconto=10.0)
        with patch('app.services.tiny.incluir_nota_fiscal',
                   return_value={'ok': True, 'id': 'nf-1'}) as inc, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}):
            assert tiny_nf_b2b.emitir_nf(v)['ok']
        item = inc.call_args[0][0]['itens'][0]['item']
        assert item['valor_unitario'] == 9.0         # 10,00 − 10%


def test_sem_sku_mapeado_aborta_sem_chamar_tiny(app):
    from app.services import tiny_nf_b2b
    with app.app_context():
        v = _venda(_cliente_completo(), sku=None)
        with patch('app.services.tiny.incluir_nota_fiscal') as inc:
            res = tiny_nf_b2b.emitir_nf(v)
        assert not res['ok']
        assert 'SKU' in res['msg']
        inc.assert_not_called()


def test_cliente_sem_endereco_estruturado_aborta(app):
    from app.services import tiny_nf_b2b
    with app.app_context():
        cli = ClienteB2B(nome='Sem Endereco', cnpj_cpf='11222333000144',
                         ativo=True)
        db.session.add(cli)
        db.session.commit()
        v = _venda(cli, sku='SKU-X')
        with patch('app.services.tiny.incluir_nota_fiscal') as inc:
            res = tiny_nf_b2b.emitir_nf(v)
        assert not res['ok']
        assert 'Endereço' in res['msg']
        inc.assert_not_called()


def test_venda_avulsa_e_cancelada_nao_emitem(app):
    from app.services import tiny_nf_b2b
    with app.app_context():
        avulsa = _venda(None, sku='SKU-A')
        res = tiny_nf_b2b.emitir_nf(avulsa)
        assert not res['ok'] and 'avulsa' in res['msg'].lower()
        v = _venda(_cliente_completo(), sku='SKU-C')
        v.status = 'cancelada'
        db.session.commit()
        res2 = tiny_nf_b2b.emitir_nf(v)
        assert not res2['ok'] and 'cancelada' in res2['msg'].lower()


def test_idempotente_nao_reemite(app):
    from app.services import tiny_nf_b2b
    from app.utils import agora
    with app.app_context():
        v = _venda(_cliente_completo(), sku='SKU-I')
        v.tiny_nota_fiscal_id = 'nf-ja'
        v.nf_emitida_em = agora()
        db.session.commit()
        with patch('app.services.tiny.incluir_nota_fiscal') as inc, \
             patch('app.services.tiny.emitir_nota_fiscal') as emi:
            res = tiny_nf_b2b.emitir_nf(v)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-ja'
        inc.assert_not_called()
        emi.assert_not_called()


def test_item_b2b_aparece_na_tela_de_mapeamento(app):
    """Item vendável só no B2B (preço de atacado, SEM preco_site) e item já
    vendido em VendaB2B aparecem em `itens_para_mapear` com origem 'b2b' —
    sem isso não haveria como mapear o SKU e a NF do B2B ficava travada."""
    from app.models import Receita
    from app.services import tiny_nf
    with app.app_context():
        # Receita de atacado, fora do site
        r = Receita(nome='Pao de Atacado', categoria='Paes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0, preco_venda=8.0)
        db.session.add(r)
        db.session.commit()
        # Produto sem preço nenhum, mas com venda B2B registrada
        v = _venda(_cliente_completo())          # cria Produto sem SKU
        prod_id = v.itens[0].produto_id
        itens = tiny_nf.itens_para_mapear()
        por_chave = {(i['kind'], i['id']): i for i in itens}
        assert por_chave[('receita', r.id)]['origem'] == 'b2b'
        assert por_chave[('produto', prod_id)]['origem'] == 'b2b'


def test_sync_fuzzy_mapeia_item_b2b(app):
    """O match por nome (planilha/API do Tiny) também cobre os itens B2B."""
    from app.models import Receita
    from app.services import tiny_nf
    with app.app_context():
        r = Receita(nome='Pao de Atacado', categoria='Paes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0, preco_venda=8.0)
        db.session.add(r)
        db.session.commit()
        res = tiny_nf._aplicar_pares([('Pao de Atacado', 'SKU-ATAC')])
        assert res['exatos'] == 1
        assert tiny_nf.sku_do_item('receita', r.id) == 'SKU-ATAC'


# ── rotas ──────────────────────────────────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


# Testes separados por usuário: o app context compartilhado do conftest faz
# o flask-login cachear o usuário em `g._login_user` — misturar admin e dono
# no MESMO teste vaza a identidade do primeiro request pros seguintes.

def test_rota_emitir_nf_admin_comum_403(app, admin_user):
    with app.app_context():
        v = _venda(_cliente_completo(), sku='SKU-R')
        vid = v.id
    c = app.test_client()
    _login(c, admin_user.id)               # admin comum (não-owner): 403
    assert c.post(f'/b2b/vendas/{vid}/emitir-nf').status_code == 403


def test_rota_emitir_nf_owner_emite(app, owner_user):
    with app.app_context():
        v = _venda(_cliente_completo(), sku='SKU-R')
        vid = v.id
    c = app.test_client()
    _login(c, owner_user.id)               # dono: emite
    with patch('app.services.tiny.incluir_nota_fiscal',
               return_value={'ok': True, 'id': 'nf-rt'}), \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        r = c.post(f'/b2b/vendas/{vid}/emitir-nf', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        v = db.session.get(VendaB2B, vid)
        assert v.tiny_nota_fiscal_id == 'nf-rt'
        assert v.nf_emitida_em is not None
