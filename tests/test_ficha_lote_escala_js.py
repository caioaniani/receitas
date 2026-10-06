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
  'rendimento-qtd':String(cfg.rendimento || 19), 'peso-unitario':'100',
  'modo-lancamento':cfg.modo || 'farinha',
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
                   base: elements['peso-base'].value,
                   qtds: rows.map(r => r.f['.pct-input'].value),
                   textos: rows.map(r => r.f['.pct-texto'].textContent)});
}
foto('carregou');
for (const passo of cfg.passos) {
  for (const b of (passo.digitar || [])) {          // tecla a tecla
    elements['peso-base'].value = String(b);
    elements['peso-base'].handlers.input();
  }
  if (passo.base !== undefined) {
    elements['peso-base'].value = String(passo.base);
    elements['peso-base'].handlers.input();
  }
  if (passo.rendimento !== undefined) {
    elements['rendimento-qtd'].value = String(passo.rendimento);
    elements['rendimento-qtd'].handlers.input();
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


def _rodar(linhas, base, passos, **extra):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para executar o cálculo da ficha')
    script = Path(__file__).resolve().parents[1] / 'app/static/js/app.js'
    cfg = {'linhas': linhas, 'base': base, 'passos': passos, **extra}
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


def test_bate_com_o_motor_das_bateladas(app):
    """A calculadora e o MOTOR da produção (`bateladas_paes.padrao_receita`)
    dão o mesmo rendimento e o mesmo levain para 12 kg."""
    from app.extensions import db
    from app.models import Receita, ReceitaIngrediente
    from app.services.bateladas_paes import padrao_receita
    levain = Receita(nome='Levain (pé)', categoria='Insumos', peso_base=1000,
                     rendimento_qtd=1, rendimento_unidade='g', peso_unitario=1,
                     sub_na_amassadeira=True)
    pao = Receita(nome='Pão Francês Motor', categoria='Pães', peso_base=1000,
                  rendimento_qtd=19, rendimento_unidade='un', peso_unitario=100)
    for nome, tipo, qtd in PAO_FRANCES:
        pao.ingredientes.append(ReceitaIngrediente(
            tipo=tipo, ingrediente_nome=nome, porcentagem=float(qtd),
            eh_base=nome.startswith('Farinha'),
            sub_receita=levain if tipo == 'receita' else None))
    db.session.add_all([levain, pao])
    db.session.commit()
    motor = padrao_receita(pao, farinha_g=12000, resolver_estoque=False)

    passos = _rodar(PAO_FRANCES, 1000, [{'rotulo': '12kg', 'base': 12000}])
    assert passos['12kg']['unidades'] == motor['unidades'] == 237
    levain_motor = next(i['qtd'] for i in motor['ingredientes'] if i['nome'] == 'Levain (pé)')
    assert float(passos['12kg']['qtds'][2]) == levain_motor == 2400


@pytest.mark.parametrize('digitar', [
    ['', '1', '12', '120', '1200', '12000'],     # apagou e digitou de novo
    ['100', '10', '1', '', '2', '20', '200', '2000', '12000'],   # backspace
])
def test_apagar_e_redigitar_o_peso_base_nao_corrompe(digitar):
    """Peso base vazio ou pequeno no meio da digitação não mexe na proporção
    (achado da revisão: virava 2.400.000 ou caía pela metade)."""
    passos = _rodar(PAO_FRANCES, 1000, [{'rotulo': 'fim', 'digitar': digitar}])
    assert passos['fim']['qtds'][2] == '2400'
    assert passos['fim']['unidades'] == 237


def test_valor_pequeno_que_arredonda_para_zero_volta_depois():
    linhas = PAO_FRANCES[:2] + [['Batom', 'mp_un', '1']]
    passos = _rodar(linhas, 1000, [{'rotulo': 'fim', 'digitar': ['1', '12', '1000']}])
    assert passos['fim']['qtds'][2] == '1'


def test_modo_quantidade_acha_o_peso_base_exato():
    """190 pães pedem a massa de 190 pães, contando o levain (antes ignorava
    o levain e a ficha pedia ingredientes para ~211)."""
    passos = _rodar(PAO_FRANCES, 1000, [{'rotulo': '190', 'rendimento': 190}],
                    modo='quantidade', rendimento=19)
    base = int(passos['190']['base'])
    levain = float(passos['190']['qtds'][2])
    assert levain == pytest.approx(base * 0.2, abs=0.01)
    massa = base * (100 + 75 + 2 + 0.5) / 100 + levain
    assert massa / 100 == pytest.approx(190, abs=0.05)


def test_texto_da_calculadora_do_padeiro_em_formato_brasileiro():
    passos = _rodar(PAO_FRANCES, 1000, [{'rotulo': '12kg', 'base': 12000}])
    assert passos['carregou']['textos'][2] == '200'
    assert passos['12kg']['textos'][2] == '2400'


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
