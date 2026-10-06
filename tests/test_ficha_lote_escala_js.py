"""Calculadora da ficha: linhas em gramas/unidades acompanham o peso base.

Caso real (06/10/2026, pão francês): a ficha tem 200 g de levain para
1.000 g de farinha (20%). A ordem de produção (motor das bateladas) calcula
12 kg de farinha com 2,4 kg de levain e 237 pães de 100 g; a calculadora da
ficha mantinha os 200 g e mostrava 215. O padeiro conferia pela ficha e
achava que o robô errava. Agora a ficha escala essas linhas como o motor.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cfg = JSON.parse(process.argv[2]);
function el(value = '') { return {value, textContent:'', className:'', style:{}, handlers:{},
  classList:{add(){},remove(){},contains(){return false;}},
  addEventListener(n, fn){this.handlers[n] = fn;}, querySelectorAll(){return [];} }; }
const elements = {};
for (const [id, value] of Object.entries({'ficha-body':'', 'peso-base':String(cfg.base),
  'rendimento-qtd':'19', 'peso-unitario':'100', 'modo-lancamento':'farinha',
  'multiplicador':'1', 'perda-percentual':'0', 'preco-venda':'0',
  'total-custo':'', 'resumo-custo':'', 'resumo-custo-un':'', 'resumo-peso':'',
  'resumo-unidades':'', 'resumo-margem-venda':'', 'resumo-lucro-un-venda':''})) {
  elements[id] = el(value);
}
const rows = cfg.linhas.map(([nome, tipo, qtd]) => {
  const f = {'.nome-input':el(nome), '.ing-tipo':el(tipo), '.pct-input':el(qtd),
             '.qtd-calc':el(), '.custo-kg-calc':el(), '.custo-rs-calc':el(),
             '.pct-texto':el()};
  return {dataset:{}, f, querySelector:s => f[s] || null};
});
const sandbox = {
  document:{getElementById:id => elements[id] || null,
    querySelectorAll:s => s === '.ingrediente-row' ? rows : [],
    querySelector:() => null, addEventListener(n, fn){if(n === 'DOMContentLoaded') fn();}},
  window:{addEventListener(){}, CARGA_IMPOSTOS:0},
  MP_DATA:{}, RECEITA_CUSTOS:{'Levain (pé)':0.01}, RECEITA_PESOS:{'Levain (pé)':1}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const out = {passos: []};
function foto(rotulo) {
  out.passos.push({rotulo, unidades: Number(elements['resumo-unidades'].textContent),
                   qtds: rows.map(r => r.f['.pct-input'].value)});
}
foto('carregou');
for (const passo of cfg.passos) {
  if (passo.base !== undefined) {
    elements['peso-base'].value = String(passo.base);
    elements['peso-base'].handlers.input();
  }
  if (passo.linha !== undefined) {
    rows[passo.linha].f['.pct-input'].value = String(passo.valor);
    elements['ficha-body'].handlers.input({target:{classList:{contains:c => c === 'pct-input'}}});
  }
  foto(passo.rotulo);
}
console.log(JSON.stringify(out));
'''

PAO_FRANCES = [['FarinhaT65', 'mp', '100.0'], ['Agua(1L)', 'mp', '75.0'],
               ['Levain (pé)', 'receita', '200.0'], ['Sal', 'mp', '2.0'],
               ['Fermento', 'mp', '0.5']]


def _rodar(linhas, base, passos):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para executar o cálculo da ficha')
    script = Path(__file__).resolve().parents[1] / 'app/static/js/app.js'
    cfg = {'linhas': linhas, 'base': base, 'passos': passos}
    res = subprocess.run([node, '-e', _HARNESS, str(script), json.dumps(cfg)],
                         capture_output=True, text=True, timeout=15, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    return {p['rotulo']: p for p in json.loads(res.stdout)['passos']}


def test_pao_frances_12kg_da_237_como_a_ordem():
    passos = _rodar(PAO_FRANCES, 1000, [
        {'rotulo': '12kg', 'base': 12000},
        {'rotulo': 'volta', 'base': 1000}])
    assert passos['carregou']['unidades'] == 19
    assert passos['carregou']['qtds'][2] == '200.0'      # carregar não mexe no campo
    assert passos['12kg']['unidades'] == 237              # mesmo número da ordem
    assert passos['12kg']['qtds'][2] == '2400'            # 20% da farinha
    assert passos['12kg']['qtds'][0] == '100.0'           # linhas em % não mudam
    assert passos['volta']['qtds'][2] == '200'
    assert passos['volta']['unidades'] == 19


def test_bate_com_o_motor_das_bateladas():
    """A calculadora e o motor da produção dão o mesmo rendimento."""
    passos = _rodar(PAO_FRANCES, 1000, [{'rotulo': '12kg', 'base': 12000}])
    massa = 12000 * (100 + 75 + 2 + 0.5) / 100 + 12000 * 200 / 1000
    assert passos['12kg']['unidades'] == int(massa // 100) == 237


def test_edicao_manual_vira_a_nova_proporcao():
    passos = _rodar(PAO_FRANCES, 1000, [
        {'rotulo': '12kg', 'base': 12000},
        {'rotulo': 'editou', 'linha': 2, 'valor': 1200},
        {'rotulo': '6kg', 'base': 6000}])
    assert passos['editou']['qtds'][2] == '1200'
    assert passos['6kg']['qtds'][2] == '600'               # 10% mantidos


def test_receita_montada_nao_escala():
    """Sem linha em %, as quantidades são absolutas: peso base não mexe."""
    linhas = [['Levain (pé)', 'receita', '200'], ['Recheio', 'mp_direto', '50']]
    passos = _rodar(linhas, 1000, [{'rotulo': 'mudou', 'base': 12000}])
    assert passos['mudou']['qtds'] == ['200', '50']


def test_tela_do_padeiro_mostra_a_quantidade_escalada(app, admin_user):
    """A calculadora do padeiro tem o texto da linha para acompanhar."""
    from app.extensions import db
    from app.models import Receita, ReceitaIngrediente
    sub = Receita(nome='Levain Teste', categoria='Insumos', peso_base=1000,
                  rendimento_qtd=1, rendimento_unidade='g', peso_unitario=1)
    rec = Receita(nome='Pão Teste', categoria='Pães', peso_base=1000,
                  rendimento_qtd=19, rendimento_unidade='un', peso_unitario=100)
    rec.ingredientes.append(ReceitaIngrediente(tipo='mp', ingrediente_nome='Farinha',
                                               porcentagem=100, eh_base=True))
    rec.ingredientes.append(ReceitaIngrediente(tipo='receita', ingrediente_nome=sub.nome,
                                               sub_receita=sub, porcentagem=200))
    db.session.add_all([sub, rec])
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    html = c.get(f'/receitas/{rec.id}/padeiro').get_data(as_text=True)
    assert 'class="pct-texto"' in html
