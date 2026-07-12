"""Tela unificada de mapeamentos (12/07/2026, pedido do dono: "visualizar/
editar o que esta mapeado e o que nao esta — de TUDO"): os dois canais
(seru + lote) na mesma tabela, venda 14d, problemas da auditoria por linha
e vínculo a matéria-prima também no canal seru (paridade com o lote).
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import (
    MateriaPrima,
    Produto,
    Receita,
    VendaMapa,
    VendaSeruDiaria,
)
from app.utils import agora, hoje


def _login(app):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    return c


def test_tela_mostra_os_dois_canais_e_venda_14d(app, admin_user):
    with app.app_context():
        r = Receita(nome='Croissant', categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.flush()
        db.session.add(VendaMapa(canal='seru', nome_externo='CROISSANT PDV',
                                 receita_id=r.id))
        db.session.add(VendaMapa(canal='lote', nome_externo='croassam',
                                 receita_id=r.id))
        db.session.add(VendaSeruDiaria(
            data=hoje() - timedelta(days=1), loja_seru='LOJA',
            seru_nome='CROISSANT PDV', qtd=42, faturamento=420, n_pedidos=1))
        db.session.commit()
    c = _login(app)
    corpo = c.get('/pdv/mapeamentos').get_data(as_text=True)
    assert 'CROISSANT PDV' in corpo
    assert 'croassam' in corpo                      # canal lote na MESMA tela
    assert 'data-canal="lote"' in corpo
    assert 'data-canal="seru"' in corpo
    assert '>42<' in corpo                          # venda 14d
    assert 'Mat-prima</option>' in corpo            # alvo MP no select
    assert 'data-filtro="problema"' in corpo        # filtro de problemas
    # linha compacta: form escondido atras do botao Editar
    assert 'btn-editar-mapa' in corpo
    assert 'mapa-form d-none' in corpo


def test_tela_marca_problema_da_auditoria_na_linha(app, admin_user):
    with app.app_context():
        r_arq = Receita(nome='Antiga', categoria='X', rendimento_qtd=1,
                        rendimento_unidade='un', peso_base=100.0,
                        arquivada_em=agora())
        db.session.add(r_arq)
        db.session.flush()
        db.session.add(VendaMapa(canal='seru', nome_externo='ITEM MORTO',
                                 receita_id=r_arq.id))
        db.session.commit()
    c = _login(app)
    corpo = c.get('/pdv/mapeamentos').get_data(as_text=True)
    assert 'data-problema="1"' in corpo
    assert 'arquivada' in corpo
    # badge de problema e o alvo LINCAM pra ficha (corrigir rápido)
    assert 'abrir receita' in corpo
    assert '/receitas/' in corpo


def test_vincular_mp_pelo_canal_seru(app, admin_user):
    """Paridade com o lote: o handler do pdv aceita alvo_tipo=mp."""
    with app.app_context():
        mp_item = MateriaPrima(nome='Pao de Queijo Congelado', unidade='un',
                               custo_por_kg=10.0)
        db.session.add(mp_item)
        db.session.flush()
        vm = VendaMapa(canal='seru', nome_externo='PAO DE QUEIJO')
        db.session.add(vm)
        db.session.commit()
        vm_id, mp_id = vm.id, mp_item.id
    c = _login(app)
    with patch('app.services.seru_sync.agendar_reprocesso_retroativo'):
        resp = c.post(f'/pdv/mapeamentos/produto/{vm_id}',
                      data={'acao': 'vincular', 'alvo_tipo': 'mp',
                            'alvo_id': str(mp_id), 'fator': '1'})
    assert resp.status_code == 302
    with app.app_context():
        vm = db.session.get(VendaMapa, vm_id)
        assert vm.materia_prima_id == mp_id
        assert vm.receita_id is None and vm.produto_id is None
        assert vm.confirmado_em is not None


def test_vincular_lote_pelo_handler_pdv_sem_reprocesso_seru(app, admin_user):
    """Linha do canal LOTE editada pela tela unificada: vincula normal e NÃO
    dispara o reprocesso retroativo do Seru (é só do canal seru)."""
    with app.app_context():
        p = Produto(nome='Cesta', preco_atacado=10)
        db.session.add(p)
        db.session.flush()
        vm = VendaMapa(canal='lote', nome_externo='cesta digitada')
        db.session.add(vm)
        db.session.commit()
        vm_id, p_id = vm.id, p.id
    c = _login(app)
    with patch('app.services.seru_sync.agendar_reprocesso_retroativo') as ag:
        resp = c.post(f'/pdv/mapeamentos/produto/{vm_id}',
                      data={'acao': 'vincular', 'alvo_tipo': 'produto',
                            'alvo_id': str(p_id), 'fator': '1'})
    assert resp.status_code == 302
    ag.assert_not_called()
    with app.app_context():
        vm = db.session.get(VendaMapa, vm_id)
        assert vm.produto_id == p_id


def test_ignorar_limpa_alvo_mp_tambem(app, admin_user):
    with app.app_context():
        mp_item = MateriaPrima(nome='Nutella', unidade='un',
                               custo_por_kg=10.0)
        db.session.add(mp_item)
        db.session.flush()
        vm = VendaMapa(canal='seru', nome_externo='X',
                       materia_prima_id=mp_item.id)
        db.session.add(vm)
        db.session.commit()
        vm_id = vm.id
    c = _login(app)
    c.post(f'/pdv/mapeamentos/produto/{vm_id}', data={'acao': 'ignorar'})
    with app.app_context():
        vm = db.session.get(VendaMapa, vm_id)
        assert vm.ignorar is True
        assert vm.materia_prima_id is None
