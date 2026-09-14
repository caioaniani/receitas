(function () {
  'use strict';
  const source = document.getElementById('kits-config');
  if (!source) return;
  const cfg = JSON.parse(source.textContent);
  const form = document.getElementById('kit-form');
  const agenda = document.getElementById('kit-agenda');
  const template = document.getElementById('kit-dia-template');
  const continuar = document.getElementById('kit-continuar');
  const aviso = document.getElementById('frete-aviso');
  const money = value => (value / 100).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
  const campo = name => form.elements.namedItem(name);
  const suco = document.getElementById('kit-suco');
  const precoKit = document.getElementById('kit-preco');
  const precoInicial = precoKit ? precoKit.textContent : '';
  let frete = null;
  let distancia = null;
  let versaoEndereco = 0;
  let enviando = false;

  function linhas() { return Array.from(agenda.querySelectorAll('.kit-dia')); }
  function configuracao() { return suco ? (cfg.sucos[suco.value] || null) : cfg; }
  function horarios(row, escolhido) {
    const atual = configuracao();
    const input = row.querySelector('.kit-data');
    const data = input.value;
    input.min = atual ? atual.dataMin : '';
    input.max = atual ? atual.dataMax : '';
    input.disabled = !atual;
    const select = row.querySelector('.kit-janela');
    select.disabled = !atual;
    const anterior = escolhido || select.value;
    select.replaceChildren();
    const mapa = atual ? (distancia !== null && distancia >= cfg.corteKm ? atual.janelasLonge : atual.janelas) : {};
    const opcoes = mapa[data] || [];
    const inicial = new Option(opcoes.length ? 'Escolha o horário' : 'Sem horários nesta data', '');
    select.add(inicial);
    opcoes.forEach(h => select.add(new Option(h, h)));
    select.value = opcoes.includes(anterior) ? anterior : '';
  }
  function atualizar() {
    const atual = configuracao();
    const lista = linhas();
    const dias = lista.map(r => r.querySelector('.kit-data').value);
    lista.forEach(r => {
      const data = r.querySelector('.kit-data');
      data.setCustomValidity(data.value && dias.filter(d => d === data.value).length > 1
        ? 'Cada dia corresponde a um kit. Escolha uma data diferente.'
        : (data.value && atual && !atual.janelas[data.value]
          ? 'Escolha uma data disponível para este kit e suco.' : ''));
    });
    const quantidade = lista.length;
    const valorProdutos = atual ? atual.precoCentavos * quantidade : null;
    if (precoKit) precoKit.textContent = atual ? money(atual.precoCentavos) + ' por kit' : precoInicial;
    document.getElementById('kits-quantidade').textContent = quantidade + (quantidade === 1 ? ' kit' : ' kits');
    document.getElementById('kits-subtotal').textContent = atual ? money(valorProdutos) : 'Escolha o suco';
    document.getElementById('kits-fretes').textContent = frete === null ? 'Calcule acima' : money(frete * quantidade);
    document.getElementById('kits-total').textContent = frete === null || !atual ? '—' : money(valorProdutos + frete * quantidade);
    document.getElementById('adicionar-data').disabled = !atual || quantidade >= 31;
    continuar.disabled = enviando || !atual || frete === null || quantidade === 0;
    document.getElementById('agenda-json').value = JSON.stringify(lista.map(r => ({
      data: r.querySelector('.kit-data').value, janela: r.querySelector('.kit-janela').value
    })));
  }
  function adicionar(data, janela) {
    if (linhas().length >= 31) return;
    const row = template.content.firstElementChild.cloneNode(true);
    row.querySelector('.kit-data').value = data || '';
    row.querySelector('.kit-data').addEventListener('change', () => { horarios(row); atualizar(); });
    row.querySelector('.kit-janela').addEventListener('change', atualizar);
    row.querySelector('.kit-remover').addEventListener('click', () => { row.remove(); atualizar(); });
    agenda.appendChild(row);
    horarios(row, janela);
    atualizar();
  }
  document.getElementById('adicionar-data').addEventListener('click', () => {
    const atual = configuracao();
    if (!atual) return;
    const lista = linhas();
    const ultima = lista.length ? lista[lista.length - 1].querySelector('.kit-data').value : '';
    let proxima = '';
    if (ultima) {
      const d = new Date(ultima + 'T12:00:00Z');
      d.setUTCDate(d.getUTCDate() + 7);
      const sugestao = d.toISOString().slice(0, 10);
      if (atual.janelas[sugestao]) proxima = sugestao;
    }
    adicionar(proxima);
    linhas().at(-1).querySelector('.kit-data').focus();
  });
  if (suco) suco.addEventListener('change', () => {
    linhas().forEach(r => horarios(r));
    atualizar();
  });
  const enderecoCampos = ['cep', 'logradouro', 'numero', 'complemento', 'bairro', 'cidade', 'uf'];
  function invalidarFrete() {
    versaoEndereco += 1;
    frete = null;
    distancia = null;
    aviso.textContent = 'Calcule os fretes para este endereço.';
    linhas().forEach(r => horarios(r));
    atualizar();
  }
  enderecoCampos.forEach(name => campo(name).addEventListener('input', invalidarFrete));
  campo('cep').addEventListener('blur', async () => {
    const cep = campo('cep').value.replace(/\D/g, '');
    if (cep.length !== 8) return;
    const msg = document.getElementById('cep-aviso');
    msg.textContent = 'Consultando CEP…';
    try {
      const res = await fetch(cfg.cepUrl.replace('00000000', cep));
      const body = await res.json();
      if (campo('cep').value.replace(/\D/g, '') !== cep) return;
      if (!res.ok || !body.ok) throw new Error(body.erro || 'Confira o endereço manualmente.');
      ['logradouro', 'bairro', 'cidade', 'uf'].forEach(name => { campo(name).value = body[name] || ''; });
      msg.textContent = 'Confira o endereço e informe o número e o complemento.';
      invalidarFrete();
    } catch (err) { msg.textContent = err.message || 'Não foi possível consultar. Preencha o endereço.'; }
  });
  document.getElementById('kit-calcular-frete').addEventListener('click', async function () {
    for (const name of enderecoCampos) {
      if (!campo(name).reportValidity()) return;
    }
    const versao = versaoEndereco;
    const botao = this;
    botao.disabled = true;
    aviso.textContent = 'Calculando os fretes…';
    try {
      const endereco = ['logradouro', 'numero', 'bairro', 'cidade', 'uf'].map(n => campo(n).value).join(', ');
      const res = await fetch(cfg.freteUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json',
          'X-CSRFToken': campo('csrf_token').value},
        body: JSON.stringify({endereco: endereco, cep: campo('cep').value})
      });
      const body = await res.json();
      if (versao !== versaoEndereco) return;
      if (!res.ok || !body.ok || body.fora_area || body.valor == null
          || !Number.isFinite(Number(body.valor))) {
        throw new Error(body.erro || 'Este endereço está fora da nossa área de entrega.');
      }
      frete = Math.round(Number(body.valor) * 100);
      if (frete < 0) throw new Error('Não foi possível calcular o frete.');
      distancia = Number(body.distancia_km);
      aviso.textContent = money(frete) + ' por entrega. O resumo soma os fretes de todas as datas.';
      linhas().forEach(r => horarios(r));
    } catch (err) {
      frete = null;
      aviso.textContent = err.message || 'Não foi possível calcular os fretes. Tente novamente.';
    } finally { botao.disabled = false; atualizar(); }
  });
  form.addEventListener('submit', event => {
    atualizar();
    if (enviando || !configuracao() || frete === null || !linhas().length || !form.reportValidity()) {
      event.preventDefault();
      return;
    }
    enviando = true;
    continuar.disabled = true;
    continuar.textContent = 'Preparando sua compra…';
  });
  (Array.isArray(cfg.agenda) && cfg.agenda.length ? cfg.agenda : [{data: suco ? '' : (Object.keys(cfg.janelas)[0] || ''), janela: ''}])
    .filter(a => a && typeof a === 'object').forEach(a => adicionar(a.data, a.janela));
  atualizar();
})();
