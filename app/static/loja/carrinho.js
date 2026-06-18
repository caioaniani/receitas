/* Carrinho da loja online — estado client-side em localStorage (Fase 3).
 *
 * O carrinho vive 100% no navegador até o checkout (quando vira um
 * PedidoOnline no banco). Sem dependência de framework: vanilla JS.
 *
 * Item no carrinho: { kind, id, nome, preco, imagem, qtd }
 *   - kind: 'receita' | 'produto'  (junto com id, identifica o item)
 *   - preco: número (BRL), preço unitário no momento em que foi adicionado
 */
(function () {
  'use strict';

  var CHAVE = 'opao_carrinho_v1';

  var Carrinho = {
    ler: function () {
      try {
        var raw = localStorage.getItem(CHAVE);
        var arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr : [];
      } catch (e) {
        return [];
      }
    },

    salvar: function (itens) {
      try {
        localStorage.setItem(CHAVE, JSON.stringify(itens));
      } catch (e) { /* localStorage cheio/indisponível — ignora */ }
      this.atualizarBadge();
    },

    _chaveItem: function (kind, id) {
      return kind + ':' + id;
    },

    adicionar: function (item, qtd) {
      qtd = parseInt(qtd, 10) || 1;
      if (qtd < 1) qtd = 1;
      var antesQtd = this.contar();
      var itens = this.ler();
      var k = this._chaveItem(item.kind, item.id);
      var achou = false;
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id) === k) {
          itens[i].qtd += qtd;
          achou = true;
          break;
        }
      }
      if (!achou) {
        itens.push({
          kind: item.kind, id: item.id, nome: item.nome,
          preco: Number(item.preco) || 0, imagem: item.imagem || '',
          categoria: item.categoria || '', qtd: qtd,
        });
      }
      this.salvar(itens);
      // Primeiro item do carrinho (vazio -> com 1+ item): abre drawer
      // automaticamente pra mostrar pro cliente que pode seguir pro
      // checkout ou continuar comprando.
      if (antesQtd === 0 && this.contar() > 0) {
        abrirDrawer();
      }
    },

    mudarQtd: function (kind, id, qtd) {
      qtd = parseInt(qtd, 10) || 0;
      var itens = this.ler();
      var k = this._chaveItem(kind, id);
      var out = [];
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id) === k) {
          if (qtd > 0) { itens[i].qtd = qtd; out.push(itens[i]); }
          // qtd <= 0 remove (não adiciona ao out)
        } else {
          out.push(itens[i]);
        }
      }
      this.salvar(out);
    },

    remover: function (kind, id) {
      this.mudarQtd(kind, id, 0);
    },

    contar: function () {
      return this.ler().reduce(function (n, it) {
        return n + (parseInt(it.qtd, 10) || 0);
      }, 0);
    },

    total: function () {
      return this.ler().reduce(function (s, it) {
        return s + (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
      }, 0);
    },

    atualizarBadge: function () {
      var badge = document.getElementById('cart-badge');
      if (badge) {
        var n = this.contar();
        badge.textContent = n;
        badge.style.display = n > 0 ? 'inline-flex' : 'none';
      }
      renderCardAdds();
    },

    qtdDe: function (kind, id) {
      var itens = this.ler();
      var k = this._chaveItem(kind, id);
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id) === k) {
          return parseInt(itens[i].qtd, 10) || 0;
        }
      }
      return 0;
    },
  };

  // Exposto pra a página do carrinho e testes manuais no console.
  window.Carrinho = Carrinho;

  function fmtBRL(v) {
    return 'R$ ' + (Number(v) || 0).toFixed(2).replace('.', ',');
  }

  // ── Cards na vitrine: botão "Adicionar" ↔ stepper (− N +) ───────────
  // Cada `.card-add[data-kind][data-id]…` é renderizado conforme a qtd no
  // carrinho. Clique em "Adicionar" → vira stepper. Stepper sincroniza o
  // carrinho e some quando qtd cai pra 0.
  function renderCardAdds() {
    var addsEls = document.querySelectorAll('.card-add[data-kind][data-id]');
    addsEls.forEach(function (el) {
      var kind = el.getAttribute('data-kind');
      var id = el.getAttribute('data-id');
      var qtd = Carrinho.qtdDe(kind, id);
      if (qtd > 0) {
        el.innerHTML =
          '<div class="stepper">' +
          '<button type="button" data-acao="menos" aria-label="Diminuir">−</button>' +
          '<div class="qtd">' + qtd + '</div>' +
          '<button type="button" data-acao="mais" aria-label="Aumentar">+</button>' +
          '</div>';
      } else {
        el.innerHTML =
          '<button type="button" data-acao="add">+ Adicionar</button>';
      }
    });
  }

  function lerItemDoCardEl(el) {
    return {
      kind: el.getAttribute('data-kind'),
      id: el.getAttribute('data-id'),
      nome: el.getAttribute('data-nome'),
      preco: el.getAttribute('data-preco'),
      categoria: el.getAttribute('data-categoria') || '',
      imagem: el.getAttribute('data-imagem') || '',
    };
  }

  function ligarCardAdds() {
    // Event delegation: 1 listener no document trata add/+/− de todos os
    // cards (rebatem em qualquer re-render sem perder handlers).
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.card-add button[data-acao]');
      if (!btn) return;
      var el = btn.closest('.card-add');
      if (!el) return;
      e.preventDefault();
      e.stopPropagation();   // não navega pro href do card
      var acao = btn.getAttribute('data-acao');
      var item = lerItemDoCardEl(el);
      if (acao === 'add') {
        Carrinho.adicionar(item, 1);
      } else if (acao === 'mais') {
        Carrinho.mudarQtd(item.kind, item.id,
                          Carrinho.qtdDe(item.kind, item.id) + 1);
      } else if (acao === 'menos') {
        Carrinho.mudarQtd(item.kind, item.id,
                          Carrinho.qtdDe(item.kind, item.id) - 1);
      }
    });
  }

  // ── Wire dos botões "adicionar ao carrinho" (página de produto) ──────
  function ligarBotoesAdd() {
    var botoes = document.querySelectorAll('[data-add-carrinho]');
    botoes.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var qtdInput = document.getElementById('qtd-add');
        var qtd = qtdInput ? qtdInput.value : 1;
        Carrinho.adicionar({
          kind: btn.getAttribute('data-kind'),
          id: btn.getAttribute('data-id'),
          nome: btn.getAttribute('data-nome'),
          preco: btn.getAttribute('data-preco'),
          categoria: btn.getAttribute('data-categoria'),
          imagem: btn.getAttribute('data-imagem'),
        }, qtd);
        // Feedback rápido
        var antes = btn.textContent;
        btn.textContent = '✓ Adicionado ao carrinho';
        btn.classList.add('add-ok');
        setTimeout(function () {
          btn.textContent = antes;
          btn.classList.remove('add-ok');
        }, 1600);
      });
    });
  }

  // ── Render da página do carrinho ─────────────────────────────────────
  function renderCarrinho() {
    var app = document.getElementById('carrinho-app');
    if (!app) return;

    var itens = Carrinho.ler();
    if (!itens.length) {
      app.innerHTML =
        '<div class="carrinho-vazio">' +
        '<p>Seu carrinho está vazio.</p>' +
        '<a class="btn-loja" href="/loja">Ver produtos</a>' +
        '</div>';
      return;
    }

    var html = '<div class="carrinho-itens">';
    itens.forEach(function (it) {
      var sub = (Number(it.preco) || 0) * (parseInt(it.qtd, 10) || 0);
      var foto = it.imagem
        ? '<img src="' + it.imagem + '" alt="">'
        : '<div class="card-foto-placeholder">🥐</div>';
      html +=
        '<div class="carrinho-linha" data-kind="' + it.kind +
        '" data-id="' + it.id + '">' +
        '<div class="carrinho-foto">' + foto + '</div>' +
        '<div class="carrinho-desc">' +
        '<div class="carrinho-nome">' + escapeHtml(it.nome) + '</div>' +
        '<div class="carrinho-preco-un">' + fmtBRL(it.preco) + ' cada</div>' +
        '</div>' +
        '<div class="carrinho-qtd">' +
        '<button class="qtd-btn" data-acao="menos" aria-label="Diminuir">−</button>' +
        '<input class="qtd-in" type="number" min="0" value="' + it.qtd + '">' +
        '<button class="qtd-btn" data-acao="mais" aria-label="Aumentar">+</button>' +
        '</div>' +
        '<div class="carrinho-sub">' + fmtBRL(sub) + '</div>' +
        '<button class="carrinho-remover" data-acao="remover" ' +
        'aria-label="Remover">🗑</button>' +
        '</div>';
    });
    html += '</div>';
    html +=
      '<div class="carrinho-rodape">' +
      '<div class="carrinho-total">Subtotal: <strong>' +
      fmtBRL(Carrinho.total()) + '</strong></div>' +
      '<p class="carrinho-aviso">O frete é calculado no checkout, conforme o ' +
      'endereço e o tipo de entrega.</p>' +
      '<div class="carrinho-acoes">' +
      '<a class="btn-loja-secundario" href="/loja">← Continuar comprando</a>' +
      '<a class="btn-loja" href="/loja/checkout">Ir para o checkout →</a>' +
      '</div></div>';
    app.innerHTML = html;

    // Wire dos controles de cada linha
    app.querySelectorAll('.carrinho-linha').forEach(function (linha) {
      var kind = linha.getAttribute('data-kind');
      var id = linha.getAttribute('data-id');
      linha.querySelector('[data-acao="menos"]').addEventListener('click',
        function () { ajustar(kind, id, -1); });
      linha.querySelector('[data-acao="mais"]').addEventListener('click',
        function () { ajustar(kind, id, +1); });
      linha.querySelector('[data-acao="remover"]').addEventListener('click',
        function () { Carrinho.remover(kind, id); renderCarrinho(); });
      linha.querySelector('.qtd-in').addEventListener('change',
        function (e) {
          Carrinho.mudarQtd(kind, id, e.target.value); renderCarrinho();
        });
    });
  }

  function ajustar(kind, id, delta) {
    var itens = Carrinho.ler();
    for (var i = 0; i < itens.length; i++) {
      if (itens[i].kind === kind && String(itens[i].id) === String(id)) {
        Carrinho.mudarQtd(kind, id, (parseInt(itens[i].qtd, 10) || 0) + delta);
        break;
      }
    }
    renderCarrinho();
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Página de confirmação: pedido criado no servidor -> esvazia o carrinho.
    if (document.getElementById('limpar-carrinho')) {
      Carrinho.salvar([]);  // salvar() já atualiza o badge
    }
    ligarCardAdds();           // event delegation — registra UMA vez
    Carrinho.atualizarBadge(); // renderiza cards iniciais
    ligarBotoesAdd();
    renderCarrinho();
  });
})();
