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

    // Funil (GA4): cliente chegou no checkout com itens no carrinho.
    if (window.lojaGA) {
      window.lojaGA('begin_checkout', {
        currency: 'BRL',
        value: itens.reduce(function (s, it) {
          return s + (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
        }, 0),
        items: itens.map(function (it) {
          return {
            item_id: it.kind + '_' + it.id, item_name: it.nome,
            price: Number(it.preco) || 0, quantity: parseInt(it.qtd, 10) || 1,
          };
        }),
      });
    }

    // ── Resumo do pedido ───────────────────────────────────────────────
    // Fatiado é grátis e o toggle preserva a qtd total (merge de linhas),
    // então o subtotal NÃO muda ao marcar/desmarcar — calculado uma vez.
    var subtotal = itens.reduce(function (s, it) {
      return s + (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
    }, 0);

    function pintarResumo() {
      var lista = Carrinho.ler();
      var h = '<ul class="checkout-itens-lista">';
      lista.forEach(function (it) {
        var sub = (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
        // Sourdough: checkbox 'fatiado' na própria linha do checkout.
        var fat = it.fatiavel
          ? '<label class="linha-fatiado"><input type="checkbox"' +
            ' data-acao="fatiado" data-kind="' + it.kind + '"' +
            ' data-id="' + it.id + '" data-fatiado="' +
            (it.fatiado ? '1' : '') + '"' + (it.fatiado ? ' checked' : '') +
            '> 🔪 fatiado</label>'
          : (it.fatiado ? ' <em>(fatiado)</em>' : '');
        // Menu configurável: mostra O QUE o cliente montou — é a última
        // tela antes de pagar, tem que dar pra conferir (26/07/2026).
        var montado = '';
        if (it.comp_resumo && it.comp_resumo.length) {
          montado = '<small class="linha-comp">' + it.comp_resumo.map(
            function (c) { return escapeHtml(c.qtd + 'x ' + c.nome); }
          ).join(' · ') + '</small>';
        }
        h += '<li><span>' + (parseInt(it.qtd, 10) || 0) + '× ' +
          escapeHtml(it.nome) + fat + montado + '</span><span>' +
          fmtBRL(sub) + '</span></li>';
      });
      h += '</ul>';
      $('#checkout-resumo').innerHTML = h;
    }
    pintarResumo();
    var resumoEl = $('#checkout-resumo');
    if (resumoEl) resumoEl.addEventListener('change', function (e) {
      var chk = e.target.closest('input[data-acao="fatiado"]');
      if (!chk) return;
      Carrinho.alternarFatiado(chk.getAttribute('data-kind'),
                               chk.getAttribute('data-id'),
                               chk.getAttribute('data-fatiado') === '1');
      pintarResumo();
    });

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

    // Express longe (>=expressLongeKm): vira "em até 2 horas" (motoboy
    // percorre mais — decisão do dono 23/06/2026). Servidor grava a janela
    // certa; isso aqui é só o aviso visual.
    function atualizarExpressTempo() {
      var alvo = document.getElementById('express-tempo');
      var aviso = document.getElementById('express-aviso-longe');
      if (!alvo) return;
      var limite = dados.expressLongeKm;
      var longe = (limite != null && ultimaDistKm != null
                   && ultimaDistKm >= limite);
      alvo.textContent = longe
        ? 'hoje, por volta de 2 horas'
        : 'hoje, por volta de 1 hora';
      if (aviso) aviso.style.display = longe ? 'block' : 'none';
    }

    function popularJanelas(modo) {
      var sel = document.getElementById('janela_entrega');
      if (!sel) return;
      // Janelas de 1h. Se a data escolhida é HOJE, remove as que já
      // passaram (usa a hora do servidor: minHoraHoje = hora_atual + lead).
      var dataEl = document.getElementById('data_entrega');
      var dataVal = dataEl ? dataEl.value : '';
      // HORÁRIO ESPECIAL DA DATA (27/07/2026): dia cadastrado pelo dono
      // (Dia dos Pais = 06:00–10:00) SUBSTITUI a lista normal — não soma.
      // Lista vazia = dia fechado, e por isso o teste é `in`, não
      // `especiais[data] || padrão`: um `[]` cairia de volta no horário
      // normal e transformaria "fechado" em "aberto o dia inteiro".
      var especiais = dados.janelasPorData || {};
      var temEspecial = dataVal && Object.prototype.hasOwnProperty.call(
        especiais, dataVal);
      var lista = (temEspecial ? especiais[dataVal]
                               : (dados.janelas || [])).slice();
      if (dataVal && dataVal === dados.hojeIso) {
        lista = lista.filter(function (j) {
          return parseInt(j.slice(0, 2), 10) >= (dados.minHoraHoje || 0);
        });
      }
      // Corte por distância (>= corteKm tira a 1ª janela da manhã). NÃO se
      // aplica a dia especial: aquelas janelas foram escolhidas a dedo pro
      // dia e cortá-las poderia zerar o dia inteiro pra quem mora longe
      // (espelha loja_checkout.janelas_disponiveis).
      var corte = dados.corteKm;
      var janelasCortadas = dados.janelasCortadasLonge || [];
      if (!temEspecial && modo === 'agendada' && corte != null
          && ultimaDistKm != null
          && ultimaDistKm >= corte && janelasCortadas.length) {
        lista = lista.filter(function (j) {
          return janelasCortadas.indexOf(j) === -1;
        });
      }
      var preferida = sel.getAttribute('data-sel') || sel.value || '';
      sel.innerHTML = '';
      if (!lista.length) {
        var vazio = document.createElement('option');
        vazio.value = '';
        // Dia FECHADO pelo dono ≠ "as janelas de hoje já passaram". Dizer
        // "sem horário" num dia fechado faz o cliente ficar trocando de
        // horário atrás de um que não existe.
        vazio.textContent = (temEspecial && !especiais[dataVal].length)
          ? 'Não entregamos nesse dia — escolha outra data'
          : 'Sem horário disponível neste dia';
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
      var ehRetirada = (modo === 'retirada');
      // O bloco de ENDEREÇO aparece nos DOIS casos (dono 20/07/2026): a
      // retirada também precisa do endereço pra emitir a NF-e. Só as partes
      // de entrega (quem recebe / calcular frete) somem na retirada.
      document.getElementById('bloco-entrega').style.display =
        (ehEntrega || ehRetirada) ? 'block' : 'none';
      var quem = document.getElementById('entrega-quem');
      var freteBox = document.getElementById('entrega-frete');
      var aviso = document.getElementById('retirada-nf-aviso');
      var titulo = document.getElementById('entrega-titulo');
      if (quem) quem.style.display = ehEntrega ? 'block' : 'none';
      if (freteBox) freteBox.style.display = ehEntrega ? 'block' : 'none';
      if (aviso) aviso.style.display = ehRetirada ? 'block' : 'none';
      if (titulo) {
        titulo.textContent = ehRetirada
          ? 'Seu endereço (para a nota fiscal)' : 'Quem recebe e onde';
      }
      document.getElementById('bloco-loja').style.display =
        ehRetirada ? 'block' : 'none';
      document.getElementById('bloco-data').style.display =
        (modo === 'express') ? 'none' : 'block';
      document.getElementById('bloco-express').style.display =
        (modo === 'express') ? 'block' : 'none';
      // Retirada não tem frete; express começa sem cotação.
      if (ehRetirada) freteAtual = 0;
      else freteAtual = null;
      popularJanelas(modo);
      atualizarTotais();
      conferirEndereco();
    }

    // ── Conferência do endereço (dono 09/08/2026, pós-Dia dos Pais:
    // número/complemento errados em massa). Resumo VIVO acima do Concluir,
    // com número e complemento em destaque; só nos modos de ENTREGA. O
    // campo número aceita SÓ DÍGITOS (máscara + servidor valida igual). ──
    function esconderTexto(s) {
      return String(s || '').replace(/[&<>"']/g, function (c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
      });
    }
    function conferirEndereco() {
      var box = document.getElementById('confere-endereco');
      if (!box) return;
      var modo = modoSelecionado();
      var v = function (id) {
        var el = form.querySelector('#' + id) || form.querySelector('[name="' + id + '"]');
        return el ? el.value.trim() : '';
      };
      var rua = v('logradouro'), num = v('numero'), comp = v('complemento');
      var bairro = v('bairro'), cidade = v('cidade');
      if (modo === 'retirada' || !rua || !num) { box.hidden = true; return; }
      var txt = esconderTexto(rua) + ', <strong>' + esconderTexto(num) + '</strong>';
      if (comp) txt += ', <strong>' + esconderTexto(comp) + '</strong>';
      if (bairro) txt += ' — ' + esconderTexto(bairro);
      if (cidade) txt += ', ' + esconderTexto(cidade);
      document.getElementById('confere-endereco-texto').innerHTML = txt;
      box.hidden = false;
    }
    var numeroEl = document.getElementById('numero');
    if (numeroEl) numeroEl.addEventListener('input', function () {
      var so = this.value.replace(/\D/g, '');
      if (so !== this.value) this.value = so;   // só reatribui se mudou (cursor)
    });
    ['logradouro', 'numero', 'bairro', 'cidade'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('input', conferirEndereco);
    });
    var compEl = form.querySelector('[name="complemento"]');
    if (compEl) compEl.addEventListener('input', conferirEndereco);
    conferirEndereco();

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

    // ── CEP-first (17/07/2026, caso Mirelle): o endereço NASCE do CEP ──
    // Rua/bairro/cidade/UF ficam TRAVADOS (readonly — readonly submete;
    // disabled não) até o CEP resolver, então o cliente não digita nome de
    // rua errado ("Rua Cândido de Azevedo Marques" sem o "Joaquim" barrou
    // venda real). FAIL-OPEN obrigatório: API de CEP fora do ar ou CEP sem
    // rua cadastrada DESTRAVA os campos — venda nunca fica presa por infra.
    var cepEl = document.getElementById('cep');
    var ultimoCep = '';
    var cepEmVoo = false;      // lookup em andamento (guarda o btn-frete)
    var freteAposCep = false;  // clicou "Calcular frete" durante o lookup
    var CAMPOS_CEP = ['logradouro', 'bairro', 'cidade', 'uf'];
    var cepStatus = document.getElementById('cep-status');
    var cepCorrigir = document.getElementById('cep-corrigir');

    function travarEndereco(travar) {
      CAMPOS_CEP.forEach(function (k) {
        var el = document.getElementById(k);
        if (!el) return;
        el.readOnly = travar;
        el.classList.toggle('campo-cep-travado', travar);
      });
    }
    function statusCep(msg, tipo) {
      if (!cepStatus) return;
      cepStatus.textContent = msg || '';
      cepStatus.hidden = !msg;
      cepStatus.className = 'cep-status' + (tipo ? ' ' + tipo : '');
    }
    function mostrarCorrigir(mostrar) {
      if (cepCorrigir) cepCorrigir.hidden = !mostrar;
    }
    function retomarFrete() {
      if (!freteAposCep) return;
      freteAposCep = false;
      var b = document.getElementById('btn-frete');
      if (b) b.click();
    }

    function reconferirCep(dBuscado) {
      // Corrida (revisão 17/07): o cliente pode ter CORRIGIDO o CEP
      // enquanto o lookup anterior estava em voo (input/blur retornam
      // cedo por cepEmVoo) — a resposta velha preencheria o endereço do
      // CEP antigo com o campo já mostrando o novo. Ao terminar QUALQUER
      // lookup, reconfere o campo e re-busca se divergiu. `!== dBuscado`
      // evita loop de retry do MESMO CEP que acabou de falhar.
      var atual = (cepEl.value || '').replace(/\D/g, '');
      if (atual.length === 8 && atual !== dBuscado && atual !== ultimoCep) {
        buscarCep();
      }
    }

    function buscarCep() {
      var d = (cepEl.value || '').replace(/\D/g, '');
      if (d.length !== 8) return;
      // máscara visual — só reatribui se mudou (reatribuir joga o cursor
      // pro fim, atrapalha edição no meio do campo).
      var mascarado = d.slice(0, 5) + '-' + d.slice(5);
      if (cepEl.value !== mascarado) cepEl.value = mascarado;
      if (d === ultimoCep || cepEmVoo) return;   // mesmo CEP / já buscando
      cepEmVoo = true;
      // CEP MUDOU: invalida o frete já calculado (força recalcular com o
      // endereço novo) — sem isso o total ficava com o frete do CEP antigo.
      freteAtual = null;
      var outF = document.getElementById('frete-resultado');
      if (outF) { outF.textContent = ''; outF.className = 'frete-resultado'; }
      atualizarTotais();
      statusCep('Buscando o endereço pelo CEP…', '');
      fetch('/loja/api/cep/' + d)
        .then(function (r) {
          return r.json().then(function (j) { return { st: r.status, j: j }; });
        })
        .then(function (resp) {
          cepEmVoo = false;
          var j = resp.j || {};
          if (j.ok) {
            ultimoCep = d;
            // SOBRESCREVE os campos (trocar o CEP atualiza o endereço).
            CAMPOS_CEP.forEach(function (k) {
              var el = document.getElementById(k);
              if (el) el.value = j[k] || '';
            });
            if ((j.logradouro || '').trim()) {
              travarEndereco(true);
              statusCep('Endereço preenchido pelo CEP — confira e informe o número.', 'ok');
              mostrarCorrigir(true);
            } else {
              // CEP "geral" (cidade pequena, sem rua na base): destrava
              // rua/bairro pra digitação — cidade/UF ficam do CEP.
              travarEndereco(false);
              statusCep('Esse CEP não tem rua cadastrada — digite a rua e o bairro.', '');
              mostrarCorrigir(false);
            }
            var num = document.getElementById('numero');
            // Só rouba o foco se o cliente ainda estiver no campo CEP —
            // no meio de outra digitação seria sequestro de cursor.
            if (num && document.activeElement === cepEl) num.focus();
          } else if (resp.st === 404) {
            // CEP NÃO EXISTE: o gesto certo é corrigir o CEP (a Mirelle
            // digitou os dígitos invertidos). Campos seguem travados; a
            // saída de emergência fica visível.
            statusCep('CEP não encontrado — confira o número digitado.', 'erro');
            mostrarCorrigir(true);
          } else {
            // INFRA fora (502/timeout): FAIL-OPEN — destrava tudo.
            travarEndereco(false);
            statusCep('Não consegui consultar o CEP — pode digitar o endereço.', 'erro');
            mostrarCorrigir(false);
          }
          retomarFrete();
          reconferirCep(d);
        })
        .catch(function () {
          cepEmVoo = false;
          travarEndereco(false);
          statusCep('Não consegui consultar o CEP — pode digitar o endereço.', 'erro');
          mostrarCorrigir(false);
          retomarFrete();
          reconferirCep(d);
        });
    }

    if (cepEl) {
      // Estado inicial: trava só quando o endereço está VAZIO. Endereço já
      // preenchido (conta com endereço salvo / re-render pós-erro do POST)
      // fica livre — o cliente pode só conferir e seguir; a dica "digite o
      // CEP primeiro" (conteúdo estático do #cep-status) não se aplica.
      var logEl = document.getElementById('logradouro');
      if (!logEl || !(logEl.value || '').trim()) {
        travarEndereco(true);
        // Re-render pós-erro do POST pode voltar com o CEP já digitado e o
        // endereço vazio (borda da revisão): resolve o estado na hora —
        // com <8 dígitos o buscarCep é no-op.
        buscarCep();
      } else {
        statusCep('', '');
      }
      // 'input' pega o CEP completo na hora (inclusive autofill do
      // navegador, que nem sempre dispara blur); blur fica de rede de
      // segurança e re-tenta depois de uma falha.
      cepEl.addEventListener('input', buscarCep);
      cepEl.addEventListener('blur', buscarCep);
    }
    if (cepCorrigir) {
      cepCorrigir.addEventListener('click', function () {
        // Saída de emergência: base de CEP com dado errado/desatualizado.
        travarEndereco(false);
        mostrarCorrigir(false);
        statusCep('', '');
        var el = document.getElementById('logradouro');
        if (el) el.focus();
      });
    }

    // Máscara de CPF (XXX.XXX.XXX-XX) ou CNPJ (XX.XXX.XXX/XXXX-XX) — o
    // campo aceita os dois; com 12+ dígitos a máscara vira CNPJ.
    var cpfEl = document.getElementById('cpf');
    if (cpfEl) {
      cpfEl.addEventListener('input', function () {
        var d = (cpfEl.value || '').replace(/\D/g, '').slice(0, 14);
        var out = d;
        if (d.length > 11) {
          out = d.slice(0, 2) + '.' + d.slice(2, 5) + '.' + d.slice(5, 8) + '/' + d.slice(8, 12);
          if (d.length > 12) out += '-' + d.slice(12);
        } else if (d.length > 9) {
          out = d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6, 9) + '-' + d.slice(9);
        } else if (d.length > 6) {
          out = d.slice(0, 3) + '.' + d.slice(3, 6) + '.' + d.slice(6);
        } else if (d.length > 3) {
          out = d.slice(0, 3) + '.' + d.slice(3);
        }
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

    // Monta o endereço estruturado em uma linha pra cotar o frete. SEM o
    // complemento de propósito: apto/bloco/nome de prédio ('Ape 502 Positano')
    // não ajuda o geocoder e ATRAPALHA (Google devolve partial_match, Nominatim
    // erra) — barrava venda de endereço válido (caso Mooca 11/07/2026). O
    // servidor é autoritativo e também geocoda sem complemento (loja_checkout).
    function enderecoMontado() {
      var ids = ['logradouro', 'numero', 'bairro', 'cidade', 'uf'];
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
        if (cepEmVoo) {
          // Lookup do CEP em andamento: espera ele terminar (o endereço
          // pode mudar) e re-dispara a cotação sozinho.
          freteAposCep = true;
          out.textContent = 'Buscando o endereço pelo CEP…';
          out.className = 'frete-resultado';
          return;
        }
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
            atualizarExpressTempo();
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
          return { kind: it.kind, id: it.id, qtd: it.qtd,
                   fatiado: !!it.fatiado,
                   // Menu configurável: a escolha tem que ir junto no
                   // fallback do form (a sessão é a fonte primária).
                   comp: (it.comp && it.comp.length) ? it.comp : null };
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
