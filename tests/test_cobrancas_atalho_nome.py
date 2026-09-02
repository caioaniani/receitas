"""Atalho sem envio e arquivos identificados pela empresa, entrega, pedido e NF."""
import json
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import EnvioCobranca
from app.services.cobrancas_download import nome_arquivo_cobranca
from tests.test_b2b_email_docs import _cenario, _preparar_nf
from tests.test_central_cobrancas import _client


def _referencia(tipo='parcela', quantidade=1):
    vendas = [SimpleNamespace(id=32 + i, data_entrega=date(2026, 8, 3) + timedelta(days=i), parcelas=[object()])
              for i in range(quantidade)]
    documento = vendas[0] if tipo == 'parcela' else SimpleNamespace(vendas=vendas, codigo='FAT00002')
    documento.nf_numero = '0011853'
    return SimpleNamespace(tipo=tipo, id=2, documento=documento,
                           cliente='UNITED COFFEE & COMPANY LTDA',
                           vencimento=date(2026, 9, 9),
                           cobranca=SimpleNamespace(parcela=SimpleNamespace(numero=1)))


@pytest.mark.parametrize('formato', ['pdf', 'zip'])
def test_nome_usa_empresa_entrega_pedido_e_nf_com_zeros(formato):
    r = _referencia()
    assert nome_arquivo_cobranca(r, formato) == (
        f'UNITED COFFEE & COMPANY LTDA - Entrega 03-08-2026 - Pedido 32 - NF 0011853.{formato}')


def test_nome_fatura_inclui_datas_e_todos_pedidos():
    r = _referencia('fatura', 3)
    assert nome_arquivo_cobranca(r) == (
        'UNITED COFFEE & COMPANY LTDA - Entregas 03-08-2026_04-08-2026_05-08-2026'
        ' - Pedidos 32_33_34 - NF 0011853.pdf')


def test_nome_nao_inventa_data_ou_numero_da_nf():
    r = _referencia()
    r.documento.data_entrega = None
    r.documento.nf_numero = None
    nome = nome_arquivo_cobranca(r)
    assert 'Entrega sem data' in nome and 'NF sem numero' in nome
    assert r.vencimento.strftime('%d-%m-%Y') not in nome


def test_fatura_datas_repetidas_e_entrega_faltante():
    r = _referencia('fatura', 3)
    r.documento.vendas[1].data_entrega = r.documento.vendas[0].data_entrega
    r.documento.vendas[2].data_entrega = None
    nome = nome_arquivo_cobranca(r)
    assert 'Entrega 03-08-2026 e sem data' in nome
    assert nome.count('03-08-2026') == 1
    assert 'Pedidos 32_33_34' in nome


def test_venda_com_varias_parcelas_nao_gera_nomes_iguais():
    r = _referencia()
    r.documento.parcelas.append(object())
    primeiro = nome_arquivo_cobranca(r)
    r.cobranca.parcela.numero = 2
    segundo = nome_arquivo_cobranca(r)
    assert 'Pedido 32 parcela 1' in primeiro
    assert 'Pedido 32 parcela 2' in segundo
    assert primeiro != segundo


def test_nome_seguro_no_header_e_no_sistema_de_arquivos():
    r = _referencia()
    r.cliente = '../Pão "&" Açúcar\\: Teste\r\nX-Header: falso/ 🚀'
    r.documento.nf_numero = '../../00123\r\n'
    nome = nome_arquivo_cobranca(r)
    assert nome.startswith('Pao & Acucar Teste X-Header falso - Entrega')
    assert 'NF 00123.pdf' in nome
    assert nome.isascii()
    assert all(c not in nome for c in '/\\\r\n:<>"|?*')


def test_fatura_grande_nome_limitado_sem_perder_identificacao():
    r = _referencia('fatura', 200)
    r.cliente = 'Empresa com nome extremamente extenso ' * 10
    r.documento.nf_numero = '0' * 40 + '1234567890'
    nome = nome_arquivo_cobranca(r)
    assert len(nome.encode()) <= 240
    assert 'FAT00002 - 200 pedidos' in nome
    assert 'Entregas 03-08-2026 a 18-02-2027' in nome
    assert nome.endswith(r.documento.nf_numero + '.pdf')


@pytest.mark.parametrize('situacao', ['registrada', 'remessa', 'pendente', 'paga', 'sem_cobranca'])
def test_atalho_ao_lado_do_envio_respeita_bloqueios_sem_baixar_no_get(app, admin_user, situacao):
    _, venda, parcela, cob = _cenario()
    _preparar_nf(venda)
    if situacao == 'sem_cobranca':
        venda.dispensa_cobranca = {'motivo': 'Divulgacao autorizada'}
    elif situacao == 'paga':
        parcela.valor_pago = parcela.valor
    else:
        cob.status = situacao
    db.session.commit()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as nf, \
            patch('app.services.email.enviar') as enviar:
        response = _client(app, admin_user).get('/cobrancas/?situacao=todas')
    assert response.status_code == 200
    grupo = response.text.split('<div class="cob-row-actions">')[1].split('</div>')[0]
    assert f'/cobrancas/parcela/{parcela.id}/documentos' in grupo
    assert ('Baixar PDF<br>cobrança completa' in grupo) == (situacao in ('registrada', 'remessa'))
    if situacao in ('registrada', 'remessa'):
        assert f'/cobrancas/parcela/{parcela.id}/baixar?formato=pdf' in grupo
        assert ('data-cob-confirmar-banco="1"' in grupo) == (situacao == 'remessa')
        assert 'banco_confirmado=1' not in grupo
    nf.assert_not_called()
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('confirmado', [True, False])
def test_atalho_remessa_so_acrescenta_confirmacao_apos_aceite(confirmado):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para o teste de interação')
    arquivo = Path(__file__).resolve().parents[1] / 'app/static/js/cobrancas.js'
    script = r'''
const fs = require('fs'), vm = require('vm');
const destino = [], mensagens = [];
let handler, prevented = false;
const link = {href: 'https://erp.example/cobrancas/parcela/7/baixar?formato=pdf',
    addEventListener: (tipo, fn) => { if (tipo === 'click') handler = fn; }};
const sandbox = {URL, document: {getElementById: () => null,
    querySelectorAll: () => [link]}, window: {
    confirm: texto => {mensagens.push(texto); return process.argv[2] === 'true';},
    location: {href: 'https://erp.example/cobrancas/', assign: url => destino.push(url)}}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
handler({preventDefault: () => {prevented = true;}});
console.log(JSON.stringify({destino, mensagens, prevented, href:link.href}));
'''
    resultado = subprocess.run([node, '-e', script, str(arquivo), json.dumps(confirmado)],
                               capture_output=True, text=True, check=True, timeout=15)
    dados = json.loads(resultado.stdout)
    assert dados['prevented'] and len(dados['mensagens']) == 1
    assert 'Sicredi' in dados['mensagens'][0]
    assert 'banco_confirmado' not in dados['href']
    assert dados['destino'] == (['https://erp.example/cobrancas/parcela/7/baixar?formato=pdf&banco_confirmado=1'] if confirmado else [])
