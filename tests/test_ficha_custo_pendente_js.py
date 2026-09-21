"""O cálculo real da ficha respeita nomes legados e custo ainda desconhecido."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente

_HARNESS = r'''
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const cfg = JSON.parse(process.argv[2]);
function el(value = '') { return {value, textContent:'', className:'', style:{}, handlers:{},
  classList:{add(){},remove(){},contains(){return false;}},
  addEventListener(n, fn){this.handlers[n] = fn;}, querySelectorAll(){return [];} }; }
const elements = {};
for (const [id, value] of Object.entries({'ficha-body':'', 'peso-base':'1000',
  'rendimento-qtd':'1', 'peso-unitario':'100', 'modo-lancamento':'farinha',
  'multiplicador':'1', 'perda-percentual':'0', 'preco-venda':'100',
  'total-custo':'', 'resumo-custo':'', 'resumo-custo-un':'', 'resumo-peso':'',
  'resumo-unidades':'', 'resumo-margem-venda':'', 'resumo-lucro-un-venda':''})) {
  elements[id] = el(value);
}
const fields = {'.nome-input':el('  aZEITONAS  '), '.ing-tipo':el(cfg.tipo),
  '.pct-input':el(String(cfg.qtd)), '.qtd-calc':el(), '.custo-kg-calc':el(), '.custo-rs-calc':el()};
const row = {querySelector:s => fields[s] || null};
const sandbox = {
  document:{getElementById:id => elements[id] || null,
    querySelectorAll:s => s === '.ingrediente-row' ? [row] : [],
    querySelector:() => null, addEventListener(n, fn){if(n === 'DOMContentLoaded') fn();}},
  window:{addEventListener(){}, CARGA_IMPOSTOS:0},
  MP_DATA:{Azeitonas:{custo_por_kg:cfg.custo, unidade:cfg.tipo === 'mp_un' ? 'un' : 'g', peso_unidade:50}},
  RECEITA_CUSTOS:{Azeitonas:cfg.custo}, RECEITA_PESOS:{Azeitonas:100}
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
assert.equal(elements['resumo-peso'].textContent, '100g', 'Quantidade/peso usam o mesmo nome normalizado');
assert.equal(elements['resumo-unidades'].textContent, 1);
if (cfg.custo === null) {
  for (const id of ['total-custo','resumo-custo','resumo-custo-un',
                   'resumo-margem-venda','resumo-lucro-un-venda']) {
    assert.equal(elements[id].textContent, 'Custo pendente', id);
  }
  assert.equal(fields['.custo-rs-calc'].textContent, 'Custo pendente');
} else {
  assert.equal(elements['total-custo'].textContent, cfg.esperado, 'Preço conhecido também usa o nome normalizado');
  assert.notEqual(elements['resumo-margem-venda'].textContent, 'Custo pendente');
}
'''


@pytest.mark.parametrize(('tipo', 'qtd', 'total'), [
    ('mp', 10, 2), ('mp_direto', 100, 2), ('mp_un', 2, 40),
    ('receita', 1, 20), ('sub_pct', .1, 20),
])
@pytest.mark.parametrize('custo', [None, 0, 20])
def test_ficha_js_normaliza_nome_sem_perder_pendencia_ou_custo(tipo, qtd, total, custo):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node indisponível para executar o cálculo da ficha')
    script = Path(__file__).resolve().parents[1] / 'app/static/js/app.js'
    cfg = {'tipo': tipo, 'qtd': qtd, 'custo': custo,
           'esperado': f'R$ {total if custo else 0:.2f}'.replace('.', ',')}
    result = subprocess.run([node, '-e', _HARNESS, str(script), json.dumps(cfg)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ficha_renderiza_nome_atual_da_subreceita_vinculada(app, admin_user):
    mp = MateriaPrima(nome='Azeitonas', custo_por_kg=None, unidade='g')
    sub = Receita(nome='Pasta de Azeitonas', categoria='Insumos', peso_base=1000,
                  rendimento_qtd=1, rendimento_unidade='un', peso_unitario=100)
    sub.ingredientes.append(ReceitaIngrediente(tipo='mp_direto',
                                               ingrediente_nome=mp.nome, porcentagem=100))
    rec = Receita(nome='Pão com pasta', categoria='Pães', peso_base=1000,
                  rendimento_qtd=1, rendimento_unidade='un', peso_unitario=100)
    rec.ingredientes.append(ReceitaIngrediente(tipo='receita',
                                               ingrediente_nome='Nome antigo',
                                               sub_receita=sub, porcentagem=1))
    db.session.add_all([mp, sub, rec])
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_user.id)
        session['_fresh'] = True
    response = client.get(f'/receitas/{rec.id}')
    assert response.status_code == 200
    assert 'name="ingrediente_nome[]" value="Pasta de Azeitonas"' in response.text
    assert 'name="ingrediente_nome[]" value="Nome antigo"' not in response.text
    assert '"Pasta de Azeitonas": null' in response.text
