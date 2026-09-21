"""Cantina participa do resumo e dos filtros, sem duplicar ao trocar a loja."""
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import AppConfig, Loja
from app.utils import hoje
from tests.test_briefing_dono import _venda_tiny
from tests.test_pdv_vendas_inclui_site import _login


@pytest.mark.parametrize('ao_vivo', [False, True])
def test_api_tiny_respeita_periodo_cancelamento_e_loja_original(app, admin_user, ao_vivo):
    dia = hoje()
    cantina = _venda_tiny(dia, valor='125.50', pid='valida')
    _venda_tiny(dia, valor=900, pid='cancelada', cancelada=True)
    _venda_tiny(dia - timedelta(days=1), valor=800, pid='fora')
    _venda_tiny(None, valor=700, pid='sem-data')
    outra = Loja(nome='Outra loja', ativa=True)
    db.session.add(outra)
    db.session.flush()
    AppConfig.set('tiny_pdv_loja_id', outra.id)
    db.session.commit()
    c = app.test_client()
    _login(c, admin_user)
    with patch('app.services.seru.listar_pedidos_completo', return_value=[]), \
            patch('app.services.vendas_diarias.garantir_capturado'):
        r = c.get(f'/pdv/api/vendas?inicio={dia}&fim={dia}&ao_vivo={int(ao_vivo)}')
    assert r.status_code == 200
    assert r.json['tiny']['lojas'] == [
        {'loja_id': cantina.id, 'loja': 'Cantina', 'total': 125.5, 'n_pedidos': 1}]


def test_resumo_js_tiny_todas_filtro_ao_vivo_sem_duplicacao():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node necessário para executar o resumo JavaScript')
    template = Path('app/templates/pdv/index.html').read_text(encoding='utf-8')
    # Executa as funções reais da página, com apenas o DOM substituído.
    funcoes = template[template.index('    function popularFiltroLoja'):template.index('    function renderResumo')]
    script = r'''
const assert = require('node:assert/strict');
let filtroLoja = '', sel = {};
const document = {getElementById: () => sel};
function escapeHtml(v) {return v;}
function s(v) {return typeof v === 'string' ? v : v.name;}
''' + funcoes + r'''
const r = {total_valor:100, total_pedidos:2, cancelados:0,
  por_loja:{Seru:100}, por_loja_detalhe:{Seru:{total:100,n_pedidos:2}},
  site:{total:50,n_pedidos:1}, tiny:{lojas:[{loja:'Cantina',total:125.5,n_pedidos:1}]},
  pedidos:[{total:100,company:{name:'Seru'},payments:[],salesChannel:'Balcão'}]};
popularFiltroLoja(r);
assert.ok(sel.innerHTML.includes('Cantina'));
assert.ok(sel.innerHTML.includes('Todas (2)'));
for (const fn of [viewDoBanco, viewAoVivo]) {
  for (const filtro of ['', 'Cantina', 'Seru', '']) {
    filtroLoja = filtro;
    const v = fn(r); incluirTiny(v,r);
    assert.equal(v.total_valor, filtro === 'Cantina' ? 125.5 : filtro === 'Seru' ? 100 : 275.5);
    if (filtro === 'Cantina') {
      assert.equal(v.total_pedidos,1);
      assert.equal(v.total_valor/v.total_pedidos,125.5);
      assert.equal(v.site_incluido,undefined);
    }
    if (!filtro) {
      assert.equal(v.por_loja.Cantina,125.5);
      assert.equal(v.total_pedidos,fn === viewDoBanco ? 4 : 3);
    }
  }
}
assert.deepEqual(r.por_loja,{Seru:100});
assert.equal(r.por_loja_detalhe.Seru.por_canal,undefined);
// Se uma loja tem ambos os PDVs, soma na mesma linha sem perder a fonte.
r.tiny.lojas.push({loja:'Seru',total:20,n_pedidos:1});
filtroLoja='Seru';
for (const fn of [viewDoBanco,viewAoVivo]) {
  const v=fn(r); incluirTiny(v,r);
  assert.equal(v.total_valor,120);
  assert.equal(v.por_loja.Seru,120);
}
filtroLoja='Cantina'; popularFiltroLoja({por_loja:{Seru:10}});
assert.equal(filtroLoja,'');
'''
    subprocess.run([node, '-e', script], check=True, capture_output=True, text=True)
