/* Checkout da loja online (Fase 3).
 *
 * Lê o carrinho (window.Carrinho, de carrinho.js, carregado antes), renderiza
 * o resumo, alterna os blocos por modo de entrega, cota o frete via
 * /loja/api/frete e monta o itens_json no submit. A AUTORIDADE de preço e
 * frete é do servidor (loja_checkout.criar_pedido) — aqui é só UI.
 */
(function () {
  'use strict';

  function fmtBRL(v) {
    return 'R$ ' + (Number(v) || 0).toFixed(2).replace('.', ',');
  }

  function $(sel) { return document.querySelector(sel); }

  document.addEventListener('DOMContentLoaded', function () {
    var form = document.getElementById('checkout-form');
    if (!form || !window.Carrinho) return;

    var dados = {};
    try {
      dados = JSON.parse(document.getElementById('checkout-dados').textContent);
    } catch (e) { dados = { janelas_entrega: [], janelas_retirada: [], expressOk: false }; }

    var itens = Carrinho.ler();
    if (!itens.length) {
      form.style.display = 'none';
      var vazio = document.getElementById('checkout-vazio');
      if (vazio) vazio.style.display = 'block';
      return;
    }
    form.style.display = 'block';

    // ── Resumo do pedido ───────────────────────────────────────────────
    var subtotal = 0;
    var resumoHtml = '<ul class="checkout-itens-lista">';
    itens.forEach(function (it) {
      var sub = (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
      subtotal += sub;
      resumoHtml += '<li><span>' + (parseInt(it.qtd, 10) || 0) + '× ' +
        escapeHtml(it.nome) + '</span><span>' + fmtBRL(sub) + '</span></li>';
    });
    resumoHtml += '</ul>';
    $('#checkout-resumo').innerHTML = resumoHtml;

    // Cartinha só aparece se houver uma CESTA no carrinho. Regra: categoria
    // contém "cesta" (pega 'Cestas' e 'Cestas Personalizadas'). Pães/itens
    // avulsos não levam cartinha de presente.
    var temCesta = itens.some(function (it) {
      return (it.categoria || '').toLowerCase().indexOf('cesta') >= 0;
    });
    var blocoCart = document.getElementById('bloco-cartinha');
    if (blocoCart) blocoCart.style.display = temCesta ? 'block' : 'none';

    var freteAtual = null;  // null = ainda não cotado (entrega/express)

    function modoSelecionado() {
      var r = form.querySelector('input[name="modo_entrega"]:checked');
      return r ? r.value : 'agendada';
    }

    function popularJanelas(modo) {
      var sel = document.getElementById('janela_entrega');
      if (!sel) return;
      // Janelas de 1h (08:00–09:00 … 17:00–18:00) — mesma lista pros modos
      // com data (agendada/retirada). Express não usa este bloco.
      var lista = dados.janelas;
      var preferida = sel.getAttribute('data-sel') || '';
      sel.innerHTML = '';
      (lista || []).forEach(function (j) {
        var opt = document.createElement('option');
        opt.value = j; opt.textContent = j;
        if (j === preferida) opt.selected = true;
        sel.appendChild(opt);
      });
    }

    function atualizarTotais() {
      $('#t-subtotal').textContent = fmtBRL(subtotal);
      var modo = modoSelecionado();
      var freteTxt, total;
      if (modo === 'retirada') {
        freteTxt = 'Grátis (retirada)';
        total = subtotal;
      } else if (freteAtual === null) {
        freteTxt = 'calcule pelo endereço';
        total = subtotal;
      } else {
        freteTxt = fmtBRL(freteAtual);
        total = subtotal + freteAtual;
      }
      $('#t-frete').textContent = freteTxt;
      $('#t-total').textContent = fmtBRL(total);
    }

    function aplicarModo() {
      var modo = modoSelecionado();
      var ehEntrega = (modo === 'agendada' || modo === 'express');
      document.getElementById('bloco-endereco').style.display =
        ehEntrega ? 'block' : 'none';
      document.getElementById('bloco-loja').style.display =
        modo === 'retirada' ? 'block' : 'none';
      document.getElementById('bloco-data').style.display =
        (modo === 'express') ? 'none' : 'block';
      document.getElementById('bloco-express').style.display =
        (modo === 'express') ? 'block' : 'none';
      // Retirada não tem frete; express começa sem cotação.
      if (modo === 'retirada') freteAtual = 0;
      else freteAtual = null;
      popularJanelas(modo);
      atualizarTotais();
    }

    form.querySelectorAll('input[name="modo_entrega"]').forEach(function (r) {
      r.addEventListener('change', aplicarModo);
    });

    // ── Cotação de frete ───────────────────────────────────────────────
    var btnFrete = document.getElementById('btn-frete');
    if (btnFrete) {
      btnFrete.addEventListener('click', function () {
        var endereco = (document.getElementById('endereco').value || '').trim();
        var cep = (document.getElementById('cep').value || '').trim();
        var out = document.getElementById('frete-resultado');
        if (!endereco && !cep) {
          out.textContent = 'Informe o endereço ou o CEP.';
          out.className = 'frete-resultado erro';
          return;
        }
        out.textContent = 'Calculando…';
        out.className = 'frete-resultado';
        btnFrete.disabled = true;
        var meta = document.querySelector('meta[name="csrf-token"]');
        fetch('/loja/api/frete', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': meta ? meta.getAttribute('content') : '',
          },
          body: JSON.stringify({ endereco: endereco, cep: cep }),
        }).then(function (r) { return r.json(); }).then(function (data) {
          btnFrete.disabled = false;
          if (!data.ok) {
            freteAtual = null;
            out.textContent = data.erro || 'Não consegui calcular o frete.';
            out.className = 'frete-resultado erro';
          } else if (data.fora_area) {
            freteAtual = null;
            out.textContent = 'Endereço fora da área de entrega (até 15 km).';
            out.className = 'frete-resultado erro';
          } else {
            freteAtual = Number(data.valor) || 0;
            var dist = data.distancia_km
              ? ' (' + Number(data.distancia_km).toFixed(1).replace('.', ',') + ' km)'
              : '';
            var base = data.gratis ? 'Frete grátis' : ('Frete: ' + fmtBRL(freteAtual));
            var extra = (modoSelecionado() === 'express')
              ? ' — estimativa, a equipe confirma' : '';
            out.textContent = base + dist + extra;
            out.className = 'frete-resultado ok';
          }
          atualizarTotais();
        }).catch(function () {
          btnFrete.disabled = false;
          freteAtual = null;
          out.textContent = 'Erro de conexão ao calcular o frete.';
          out.className = 'frete-resultado erro';
          atualizarTotais();
        });
      });
    }

    // ── Submit ─────────────────────────────────────────────────────────
    form.addEventListener('submit', function (e) {
      var atual = Carrinho.ler();
      if (!atual.length) {
        e.preventDefault();
        return;
      }
      document.getElementById('itens_json').value = JSON.stringify(
        atual.map(function (it) {
          return { kind: it.kind, id: it.id, qtd: it.qtd };
        }));
      // Servidor valida tudo; deixa enviar.
    });

    function escapeHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    aplicarModo();  // estado inicial (respeita o modo já marcado)
  });
})();
