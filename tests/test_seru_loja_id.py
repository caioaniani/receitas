"""Vínculo Seru→Loja ancorado no company.id (incidente 06-07/07/2026).

Renomearam as lojas no Seru e o vínculo por NOME quebrou em silêncio: a
Ribeiro ficou 2 semanas sem baixar e as vendas dela caíram na Anesio (o nome
novo colidiu com o mapa confirmado da outra loja). Agora o id resolve
primeiro; renome só atualiza o rótulo ("traz a atualização junto").
"""
from app.extensions import db
from app.models import Loja, SeruLojaMap
from app.services.seru_sync import _resolver_loja
from app.utils import agora


def _loja(nome):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    return lj


def test_renome_atualiza_rotulo_e_mantem_loja(app):
    ribeiro = _loja('Loja Ribeiro do Vale')
    db.session.add(SeruLojaMap(seru_company_name='OPAO PADARIA',
                               seru_company_id='uuid-rib',
                               loja_id=ribeiro.id, confirmado_em=agora()))
    db.session.commit()
    loja, m = _resolver_loja('O PAO RIBEIRO NOVO', [ribeiro], 'uuid-rib')
    assert loja.id == ribeiro.id                     # resolveu pelo id
    assert m.seru_company_name == 'O PAO RIBEIRO NOVO'   # rótulo acompanhou
    assert m.confirmado_em is not None               # confirmação preservada


def test_colisao_de_nome_resolve_pelo_id_sem_quebrar(app):
    """O caso do incidente: nome novo já pertence a OUTRO mapa. A resolução
    pelo id continua certa; o rótulo velho fica (nunca viola o unique)."""
    anesio = _loja('Loja Anesio')
    ribeiro = _loja('Loja Ribeiro')
    db.session.add_all([
        SeruLojaMap(seru_company_name='O PAO PADARIA', seru_company_id='uuid-ane',
                    loja_id=anesio.id, confirmado_em=agora()),
        SeruLojaMap(seru_company_name='OPAO PADARIA', seru_company_id='uuid-rib',
                    loja_id=ribeiro.id, confirmado_em=agora()),
    ])
    db.session.commit()
    loja, m = _resolver_loja('O PAO PADARIA', [anesio, ribeiro], 'uuid-rib')
    assert loja.id == ribeiro.id                     # id vence o nome colidido
    assert m.seru_company_name == 'OPAO PADARIA'     # rótulo velho preservado


def test_backfill_do_id_no_mapa_antigo(app):
    nebraska = _loja('Loja Nebraska')
    db.session.add(SeruLojaMap(seru_company_name='O PAO NEBRASKA',
                               loja_id=nebraska.id, confirmado_em=agora()))
    db.session.commit()
    loja, m = _resolver_loja('O PAO NEBRASKA', [nebraska], 'uuid-neb')
    assert loja.id == nebraska.id
    assert m.seru_company_id == 'uuid-neb'           # id gravado na 1ª venda


def test_company_nova_cria_pendente_com_id(app):
    lj = _loja('Loja Sem Match XYZ')
    loja, m = _resolver_loja('NOME DESCONHECIDO QQQ', [lj], 'uuid-novo')
    assert m.seru_company_id == 'uuid-novo'
    assert m.confirmado_em is None                   # pendente até confirmar


def test_backfill_do_cnpj_e_formatacao(app):
    lj = _loja('Loja CNPJ')
    db.session.add(SeruLojaMap(seru_company_name='O PAO PADARIA',
                               loja_id=lj.id, confirmado_em=agora()))
    db.session.commit()
    _, m = _resolver_loja('O PAO PADARIA', [lj], 'uuid-doc',
                          '40646899000139')
    assert m.seru_company_document == '40646899000139'
    assert m.cnpj_fmt == '40.646.899/0001-39'
