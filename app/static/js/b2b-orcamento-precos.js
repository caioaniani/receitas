(function (root) {
  'use strict';

  function escapar(valor) {
    return String(valor == null ? '' : valor).replace(/[&<>"']/g, function (caractere) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[caractere];
    });
  }

  function sugerido(tabelas, cliente, ref) {
    var especificos = tabelas.especificos[String(cliente)] || {};
    if (Object.prototype.hasOwnProperty.call(especificos, ref)) return especificos[ref];
    var desconto = tabelas.descontos[String(cliente)] || '0';
    return (tabelas.por_desconto[desconto] || {})[ref] || null;
  }

  function decimal(valor) {
    var m = String(valor == null || valor === '' ? '0' : valor).replace(',', '.')
      .match(/^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/);
    if (!m || !(m[2] || m[3])) return {n: 0n, escala: 0};
    var escala = (m[3] || '').length - Number(m[4] || 0);
    if (Math.abs(escala) > 100) return {n: 0n, escala: 0};
    var n = BigInt((m[1] === '-' ? '-' : '') + (m[2] || '0') + (m[3] || ''));
    if (escala < 0) { n *= 10n ** BigInt(-escala); escala = 0; }
    return {n: n, escala: escala};
  }

  // Prévia em centavos inteiros, com o mesmo arredondamento por linha do
  // servidor. Nunca calcula um preço líquido unitário intermediário.
  function subtotalCentavos(quantidade, preco, desconto) {
    var q = decimal(quantidade), p = decimal(preco), d = decimal(desconto);
    var fator = 10n ** BigInt(d.escala);
    var numerador = q.n * p.n * (100n * fator - d.n);
    var divisor = 10n ** BigInt(q.escala + p.escala + d.escala);
    var sinal = numerador < 0n ? -1n : 1n;
    var absoluto = numerador * sinal;
    return sinal * (absoluto / divisor + (2n * (absoluto % divisor) >= divisor ? 1n : 0n));
  }

  function moedaCentavos(valor) {
    var sinal = valor < 0n ? '-' : '';
    if (valor < 0n) valor = -valor;
    return 'R$ ' + sinal + String(valor / 100n) + ',' + String(valor % 100n).padStart(2, '0');
  }

  // O formulário usa este controle por linha. Edição manual e valores
  // já salvos ficam preservados até o dono pedir uma nova sugestão.
  function controlar(opcoes) {
    var nome = opcoes.nome, preco = opcoes.preco, desconto = opcoes.desconto;
    var nomeManual = !!opcoes.nomeManual;
    var precoManual = !!opcoes.preservarPreco;

    function mostrar() {
      var sugestao = opcoes.sugestao();
      opcoes.aviso.textContent = precoManual
        ? (preco.value === '' ? 'Informe o preço ou use a sugestão.' : 'Preço e desconto informados preservados.')
        : (!sugestao.selecionado ? '' : (preco.value === ''
          ? 'Sem preço de atacado cadastrado.' : 'Preço sugerido para este cliente.'));
      opcoes.botao.hidden = !precoManual || !sugestao.preco;
    }

    function atualizarPreco() {
      if (!precoManual) {
        var sugestao = opcoes.sugestao();
        preco.value = sugestao.preco;
        desconto.value = sugestao.desconto;
      }
      mostrar();
      opcoes.recalcular();
    }

    nome.addEventListener('input', function () {
      nomeManual = nome.value.trim() !== '';
    });
    [preco, desconto].forEach(function (campo) {
      campo.addEventListener('input', function () {
        precoManual = true;
        mostrar();
      });
    });
    opcoes.botao.addEventListener('click', function () {
      precoManual = false;
      atualizarPreco();
    });
    mostrar();
    return {
      trocarItem: function () {
        if (!nomeManual) nome.value = opcoes.sugestao().nome;
        atualizarPreco();
      },
      trocarCliente: atualizarPreco
    };
  }

  root.B2BOrcamentoPrecos = { sugerido: sugerido, controlar: controlar, escapar: escapar,
    subtotalCentavos: subtotalCentavos, moedaCentavos: moedaCentavos };
})(typeof window === 'undefined' ? globalThis : window);
