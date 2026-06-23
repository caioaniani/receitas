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

  // Campos de nome: bloqueia dígitos enquanto digita (cliente colocava o CPF
  // no nome — 23/06/2026). O servidor também valida (autoridade), isso é só UX.
  function _blindarNomes() {
    var campos = document.querySelectorAll('[data-nome-field]');
    campos.forEach(function (el) {
      el.addEventListener('input', function () {
        var limpo = el.value.replace(/[0-9]/g, '');
        if (limpo !== el.value) {
          var delta = el.value.length - limpo.length;
          var pos = Math.max(0, (el.selectionStart || limpo.length) - delta);
          el.value = limpo;
          try { el.setSelectionRange(pos, pos); } catch (e) { /* noop */ }
        }
      });
    });
  }

  // Contador da cartinha (limite 250 — clientes empolgam). maxlength já
  // bloqueia no input; isso aqui é só feedback visual.
  function _cartinhaContador() {
    var ta = document.getElementById('cartinha');
    var out = document.getElementById('cartinha-contador');
    if (!ta || !out) return;
    var limite = parseInt(ta.getAttribute('maxlength') || '250', 10);
    function atualizar() {
      var n = (ta.value || '').length;
      out.textContent = n + '/' + limite + ' caracteres';
      out.style.color = (n >= limite) ? '#c92a2a'
                      : (n >= limite * 0.85) ? '#a06200' : '';
    }
    ta.addEventListener('input', atualizar);
    atualizar();
  }

  document.addEventListener('DOMContentLoaded', function () {
    _blindarNomes();
    _cartinhaContador();
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

    // Última distância cotada — corta a 1ª janela quando o cliente está
    // longe (>=corteKm). Repopular ao cotar frete. Servidor é autoridade.
    var ultimaDistKm = null;

    function popularJanelas(modo) {
      var sel = document.getElementById('janela_entrega');
      if (!sel) return;
      // Janelas de 1h. Se a data escolhida é HOJE, remove as que já
      // passaram (usa a hora do servidor: minHoraHoje = hora_atual + lead).
      var lista = (dados.janelas || []).slice();
      var dataEl = document.getElementById('data_entrega');
      var dataVal = dataEl ? dataEl.value : '';
      if (dataVal && dataVal === dados.hojeIso) {
        lista = lista.filter(function (j) {
          return parseInt(j.slice(0, 2), 10) >= (dados.minHoraHoje || 0);
        });
      }
      // Corte por distância (>= corteKm tira a 1ª janela da manhã).
      var corte = dados.corteKm;
      var janelasCortadas = dados.janelasCortadasLonge || [];
      if (modo === 'agendada' && corte != null && ultimaDistKm != null
          && ultimaDistKm >= corte && janelasCortadas.length) {
        lista = lista.filter(function (j) {
          return janelasCortadas.indexOf(j) === -1;
        });
      }
      var preferida = sel.getAttribute('data-sel') || sel.value || '';
      sel.innerHTML = '';
      if (!lista.length) {
        var vazio = document.createElement('option');
        vazio.value = ''; vazio.textContent = 'Sem horário disponível neste dia';
        sel.appendChild(vazio);
        return;
      }
      lista.forEach(function (j) {
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
      document.getElementById('bloco-entrega').style.display =
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

    // Toggle "é um presente" (mostra campos do destinatário)
    var chkPresente = document.getElementById('e_presente');
    var blocoDest = document.getElementById('bloco-destinatario');
    function aplicarPresente() {
      if (blocoDest) blocoDest.style.display = chkPresente.checked ? 'block' : 'none';
    }
    if (chkPresente) {
      chkPresente.addEventListener('change', aplicarPresente);
      aplicarPresente();
    }

    // CEP -> BrasilAPI: preenche logradouro/bairro/cidade/UF
    var cepEl = document.getElementById('cep');
    var ultimoCep = '';
    if (cepEl) {
      cepEl.addEventListener('blur', function () {
        var d = (cepEl.value || '').replace(/\D/g, '');
        if (d.length !== 8) return;
        // máscara visual
        cepEl.value = d.slice(0, 5) + '-' + d.slice(5);
        if (d === ultimoCep) return;   // mesmo CEP, não re-busca
        // CEP MUDOU: invalida o frete já calculado (força recalcular com o
        // endereço novo) — sem isso o total ficava com o frete do CEP antigo.
        freteAtual = null;
        var outF = document.getElementById('frete-resultado');
        if (outF) { outF.textContent = ''; outF.className = 'frete-resultado'; }
        atualizarTotais();
        fetch('/loja/api/cep/' + d).then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) return;
            ultimoCep = d;
            // SOBRESCREVE os campos do endereço (antes só preenchia se vazio,
            // então trocar o CEP não atualizava o endereço já inserido).
            ['logradouro', 'bairro', 'cidade', 'uf'].forEach(function (k) {
              var el = document.getElementById(k);
              if (el) el.value = j[k] || '';
            });
            var num = document.getElementById('numero');
            if (num) num.focus();
          }).catch(function () {});
      });
    }

    // Máscara simples de CPF (XXX.XXX.XXX-XX)
    var cpfEl = document.getElementById('cpf');
    if (cpfEl) {
      cpfEl.addEventListener('input', function () {
        var d = (cpfEl.value || '').replace(/\D/g, '').slice(0, 11);
        var out = d;
        if (d.length > 9) out = d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6, 9) + '-' + d.slice(9);
        else if (d.length > 6) out = d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6);
        else if (d.length > 3) out = d.slice(0, 3) + '.' + d.slice(3);
        cpfEl.value = out;
      });
    }

    form.querySelectorAll('input[name="modo_entrega"]').forEach(function (r) {
      r.addEventListener('change', aplicarModo);
    });

    // Mudou a data -> repopular janelas (filtra passadas se for hoje) +
    // checar disponibilidade de cada item do carrinho pra essa data.
    // Decisao do dono 23/06/2026: cliente precisa SABER no momento da
    // escolha qual item nao tem saldo pra aquela data (em vez de descobrir
    // so ao submeter), com opcoes pra trocar data ou remover do carrinho.
    var dataEl = document.getElementById('data_entrega');
    if (dataEl) {
      dataEl.addEventListener('change', function () {
        popularJanelas(modoSelecionado());
        checarDisponibilidadeData();
      });
      checarDisponibilidadeData();  // estado inicial
    }

    function checarDisponibilidadeData() {
      var aviso = document.getElementById('checkout-disponibilidade');
      if (!aviso) return;
      var data = dataEl.value;
      if (!data) { aviso.style.display = 'none'; return; }
      var itensCart = Carrinho.ler().map(function (it) {
        return { kind: it.kind, id: it.id };
      });
      if (!itensCart.length) { aviso.style.display = 'none'; return; }
      aviso.style.display = 'block';
      aviso.className = 'dispon-checkout verificando';
      aviso.innerHTML = '<em>verificando disponibilidade…</em>';
      var meta = document.querySelector('meta[name="csrf-token"]');
      fetch('/loja/api/disponibilidade-checkout', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': meta ? meta.getAttribute('content') : '',
        },
        body: JSON.stringify({ data: data, itens: itensCart })
      }).then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) {
            aviso.className = 'dispon-checkout ko';
            aviso.textContent = 'Não consegui verificar — tente outra data.';
            travarSubmit(true);
            return;
          }
          if (!j.esgotados || !j.esgotados.length) {
            aviso.className = 'dispon-checkout ok';
            aviso.innerHTML = '✓ Todos os itens disponíveis pra essa data.';
            travarSubmit(false);
            setTimeout(function () { aviso.style.display = 'none'; }, 2500);
            return;
          }
          // Tem item(ns) esgotado(s) — mostra lista + acoes.
          var html = '<strong>⚠ Itens sem disponibilidade pra essa data:</strong><ul style="margin:8px 0 10px 18px;">';
          j.esgotados.forEach(function (it) {
            html += '<li>' + escapeHtml(it.nome) +
              ' <button type="button" class="btn-link-vermelho" ' +
              'data-remover-kind="' + escapeHtml(it.kind) +
              '" data-remover-id="' + escapeHtml(String(it.id)) +
              '">remover do carrinho</button></li>';
          });
          html += '</ul>';
          if (j.proxima_disponivel) {
            var d = j.proxima_disponivel;
            var br = d.slice(8, 10) + '/' + d.slice(5, 7) + '/' + d.slice(0, 4);
            html += '<button type="button" class="btn-trocar-data" ' +
              'data-data="' + escapeHtml(d) + '">' +
              'Trocar pra ' + br + ' (todos disponíveis)</button>';
          } else {
            html += '<p style="margin:6px 0 0; font-size:13px;">' +
              'Nenhuma data nos próximos 30 dias tem todos os itens — ' +
              'tire o esgotado ou tente uma data específica.</p>';
          }
          aviso.className = 'dispon-checkout ko';
          aviso.innerHTML = html;
          travarSubmit(true);
        })
        .catch(function () {
          aviso.className = 'dispon-checkout ko';
          aviso.textContent = 'Erro ao verificar — tente outra data.';
          travarSubmit(true);
        });
    }

    function travarSubmit(travar) {
      var btn = form.querySelector('button[type="submit"]');
      if (!btn) return;
      btn.disabled = !!travar;
      btn.style.opacity = travar ? '0.55' : '';
      btn.title = travar ? 'Resolva os itens esgotados antes de continuar' : '';
    }

    // Cliques nos botoes do aviso (remover do carrinho / trocar data).
    document.addEventListener('click', function (e) {
      var btnRm = e.target.closest && e.target.closest('[data-remover-kind]');
      if (btnRm) {
        var k = btnRm.getAttribute('data-remover-kind');
        var id = btnRm.getAttribute('data-remover-id');
        Carrinho.remover(k, id);
        // Re-renderiza o resumo e re-checa a data.
        window.location.reload();
        return;
      }
      var btnTr = e.target.closest && e.target.closest('[data-data]');
      if (btnTr && dataEl) {
        dataEl.value = btnTr.getAttribute('data-data');
        dataEl.dispatchEvent(new Event('change'));
      }
    });

    // Monta o endereço estruturado em uma linha pra cotar o frete.
    function enderecoMontado() {
      var ids = ['logradouro', 'numero', 'complemento', 'bairro', 'cidade', 'uf'];
      var partes = ids.map(function (k) {
        var el = document.getElementById(k);
        return el ? (el.value || '').trim() : '';
      }).filter(Boolean);
      return partes.join(', ');
    }

    // ── Cotação de frete ───────────────────────────────────────────────
    var btnFrete = document.getElementById('btn-frete');
    if (btnFrete) {
      btnFrete.addEventListener('click', function () {
        var endereco = enderecoMontado();
        var cepEl2 = document.getElementById('cep');
        var cep = cepEl2 ? (cepEl2.value || '').trim() : '';
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
            out.textContent = 'Endereço fora da nossa área de entrega.';
            out.className = 'frete-resultado erro';
          } else {
            freteAtual = Number(data.valor) || 0;
            var dist = data.distancia_km
              ? ' (' + Number(data.distancia_km).toFixed(1).replace('.', ',') + ' km)'
              : '';
            var base = data.gratis ? 'Frete grátis' : ('Frete: ' + fmtBRL(freteAtual));
            out.textContent = base + dist;
            out.className = 'frete-resultado ok';
            // Atualiza distância e repopula janelas (corta 1ª manhã se longe).
            ultimaDistKm = data.distancia_km != null
              ? Number(data.distancia_km) : null;
            popularJanelas(modoSelecionado());
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
    // Trava de duplo-envio: sem isso, um toque duplo (comum no celular com
    // rede lenta) ou um Enter repetido manda o POST várias vezes e cria
    // PEDIDOS DUPLICADOS. Em sucesso o servidor redireciona (página nova,
    // botão volta a habilitar); em erro ele re-renderiza o form (idem).
    var enviando = false;
    form.addEventListener('submit', function (e) {
      if (enviando) { e.preventDefault(); return; }
      var atual = Carrinho.ler();
      if (!atual.length) {
        e.preventDefault();
        return;
      }
      document.getElementById('itens_json').value = JSON.stringify(
        atual.map(function (it) {
          return { kind: it.kind, id: it.id, qtd: it.qtd };
        }));
      enviando = true;
      var btn = document.getElementById('btn-finalizar');
      if (btn) { btn.disabled = true; btn.textContent = 'Enviando…'; }
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
