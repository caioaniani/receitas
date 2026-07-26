/* Carrinho da loja online — estado client-side em localStorage (Fase 3).
 *
 * O carrinho vive 100% no navegador até o checkout (quando vira um
 * PedidoOnline no banco). Sem dependência de framework: vanilla JS.
 *
 * Item no carrinho: { kind, id, nome, preco, imagem, qtd, fatiado }
 *   - kind: 'receita' | 'produto'  (junto com id e fatiado, identifica o item)
 *   - preco: número (BRL), preço unitário no momento em que foi adicionado
 *   - fatiado: bool — só sourdough (16/07/2026). Preferência de corte, sem
 *     custo. Fatiado e inteiro do MESMO pão são LINHAS SEPARADAS (a chave de
 *     deduplicação inclui fatiado), então não somam qtd um no outro.
 */
(function () {
  'use strict';

  var CHAVE = 'opao_carrinho_v1';

  // Espelho em memória do carrinho. A FONTE DE VERDADE é a SESSÃO do servidor
  // (injetada em #carrinho-sessao e sincronizada via POST /loja/api/carrinho),
  // que não some quando o navegador descarta o storage. O localStorage vira só
  // cache local (resiliência offline + migração do modelo antigo).
  var _mirror = [];

  var Carrinho = {
    ler: function () {
      try { return JSON.parse(JSON.stringify(_mirror)); } catch (e) { return []; }
    },

    salvar: function (itens) {
      _mirror = Array.isArray(itens) ? itens : [];
      try { localStorage.setItem(CHAVE, JSON.stringify(_mirror)); } catch (e) { /* cache best-effort */ }
      _sincronizarServidor(_mirror);   // grava na sessão (fonte de verdade)
      this.atualizarBadge();
    },

    // Assinatura da composição de um MENU CONFIGURÁVEL (26/07/2026):
    // [[pi_id, qtd], ...] → "12:5,13:7". Ordena por pi_id (numérico) pra a
    // mesma escolha gerar sempre a mesma string. Espelha
    // `loja_menu.chave` no servidor — a ordenação não precisa ser a MESMA
    // string dos dois lados, mas as classes de equivalência sim (dois
    // carrinhos idênticos têm que casar tanto aqui quanto lá).
    _chaveComp: function (comp) {
      if (!comp || !comp.length) return '';
      return comp.slice()
        .sort(function (a, b) { return (a[0] | 0) - (b[0] | 0); })
        .map(function (p) { return (p[0] | 0) + ':' + (p[1] | 0); })
        .join(',');
    },

    // Chave de identidade da LINHA. `fatiado` entra na chave: fatiado e
    // inteiro do mesmo pão são linhas distintas (não somam qtd). A
    // composição do menu também: dois menus montados DIFERENTE não podem
    // somar quantidade na mesma linha (o cliente receberia outra coisa).
    _chaveItem: function (kind, id, fatiado, comp) {
      return kind + ':' + id + ':' + (fatiado ? 'f' : '') + ':' +
        this._chaveComp(comp);
    },

    adicionar: function (item, qtd) {
      qtd = parseInt(qtd, 10) || 1;
      if (qtd < 1) qtd = 1;
      var fatiado = !!item.fatiado;
      var comp = (item.comp && item.comp.length) ? item.comp : null;
      var antesQtd = this.contar();
      var itens = this.ler();
      var k = this._chaveItem(item.kind, item.id, fatiado, comp);
      var achou = false;
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id,
                            itens[i].fatiado, itens[i].comp) === k) {
          itens[i].qtd += qtd;
          achou = true;
          break;
        }
      }
      if (!achou) {
        itens.push({
          kind: item.kind, id: item.id, nome: item.nome,
          preco: Number(item.preco) || 0, imagem: item.imagem || '',
          categoria: item.categoria || '', qtd: qtd, fatiado: fatiado,
          fatiavel: !!item.fatiavel,   // mostra o checkbox na linha do carrinho
          // Menu configurável: a escolha do cliente ([[pi_id, qtd], ...]) e
          // o resumo legível pra linha do carrinho. O servidor re-sanitiza.
          comp: comp,
          comp_resumo: item.comp_resumo || null,
        });
      }
      this.salvar(itens);
      lojaGA('add_to_cart', {
        currency: 'BRL', value: (Number(item.preco) || 0) * qtd,
        items: [{
          item_id: item.kind + '_' + item.id, item_name: item.nome,
          price: Number(item.preco) || 0, quantity: qtd,
        }],
      });
      // Primeiro item do carrinho (vazio -> com 1+ item): abre drawer
      // automaticamente pra mostrar pro cliente que pode seguir pro
      // checkout ou continuar comprando.
      if (antesQtd === 0 && this.contar() > 0) {
        abrirDrawer();
      }
    },

    mudarQtd: function (kind, id, qtd, fatiado, comp) {
      qtd = parseInt(qtd, 10) || 0;
      var itens = this.ler();
      var k = this._chaveItem(kind, id, fatiado, comp);
      var out = [];
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id,
                            itens[i].fatiado, itens[i].comp) === k) {
          if (qtd > 0) { itens[i].qtd = qtd; out.push(itens[i]); }
          // qtd <= 0 remove (não adiciona ao out)
        } else {
          out.push(itens[i]);
        }
      }
      this.salvar(out);
    },

    remover: function (kind, id, fatiado, comp) {
      this.mudarQtd(kind, id, 0, fatiado, comp);
    },

    // Liga/desliga "fatiado" de uma LINHA já no carrinho (checkbox da linha
    // no drawer/carrinho/checkout). Fatiado muda a identidade da linha:
    // - sem linha do outro estado → flip NO LUGAR (a linha não muda de
    //   posição, senão ela "pula" ao marcar/desmarcar);
    // - com linha do outro estado → SOMA nela e remove a origem (ex: tinha
    //   1 fatiado + 2 inteiro, marca o inteiro → 3 fatiado). Só item fatiável.
    alternarFatiado: function (kind, id, deFatiado, comp) {
      var itens = this.ler();
      var kDe = this._chaveItem(kind, id, deFatiado, comp);
      var novoFat = !deFatiado;
      var kPara = this._chaveItem(kind, id, novoFat, comp);
      var iOrigem = -1, iDest = -1;
      for (var i = 0; i < itens.length; i++) {
        var k = this._chaveItem(itens[i].kind, itens[i].id, itens[i].fatiado,
                                itens[i].comp);
        if (iOrigem < 0 && k === kDe) iOrigem = i;
        else if (iDest < 0 && k === kPara) iDest = i;
      }
      if (iOrigem < 0 || !itens[iOrigem].fatiavel) return;
      if (iDest >= 0) {
        itens[iDest].qtd = Math.min(99, itens[iDest].qtd + itens[iOrigem].qtd);
        itens.splice(iOrigem, 1);          // destino mantém a posição
      } else {
        itens[iOrigem].fatiado = novoFat;  // flip no lugar
      }
      this.salvar(itens);
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

    qtdDe: function (kind, id, fatiado, comp) {
      var itens = this.ler();
      var k = this._chaveItem(kind, id, fatiado, comp);
      for (var i = 0; i < itens.length; i++) {
        if (this._chaveItem(itens[i].kind, itens[i].id,
                            itens[i].fatiado, itens[i].comp) === k) {
          return parseInt(itens[i].qtd, 10) || 0;
        }
      }
      return 0;
    },
  };

  // Exposto pra a página do carrinho e testes manuais no console.
  window.Carrinho = Carrinho;

  // Eventos de funil pro GA4 (add_to_cart, begin_checkout, purchase). Guarda:
  // só dispara se o gtag existir (GA4 configurado + cookies aceitos). Sem isso
  // o GA4 só media visitas — não dava pra ver ONDE o cliente desiste.
  function lojaGA(evento, params) {
    try {
      if (typeof window.gtag === 'function') window.gtag('event', evento, params);
    } catch (e) { /* analytics nunca quebra a loja */ }
  }
  window.lojaGA = lojaGA;   // reusado no checkout.js e na confirmação do pedido

  function fmtBRL(v) {
    return 'R$ ' + (Number(v) || 0).toFixed(2).replace('.', ',');
  }

  // Controle "fatiado" da LINHA do carrinho: checkbox interativo p/ item
  // fatiável (sourdough), selo estático p/ item antigo sem `fatiavel` no
  // cache. O change é tratado em ligarDrawer/renderCarrinho.
  function fatiadoControle(it) {
    if (it.fatiavel) {
      return '<label class="linha-fatiado">' +
        '<input type="checkbox" data-acao="fatiado"' +
        (it.fatiado ? ' checked' : '') + '> 🔪 fatiado</label>';
    }
    return it.fatiado ? '<div class="fatiado-tag">🔪 fatiado</div>' : '';
  }

  // "O que você montou" na linha do carrinho de um MENU CONFIGURÁVEL
  // (26/07/2026). Sem isso, dois menus montados diferente ficam
  // indistinguíveis na tela — o cliente não confere o que escolheu.
  function compResumoHtml(it) {
    if (!it.comp_resumo || !it.comp_resumo.length) return '';
    var partes = it.comp_resumo.map(function (c) {
      return escapeHtml(c.qtd + 'x ' + c.nome);
    });
    return '<div class="linha-comp">' + partes.join(' · ') + '</div>';
  }

  // `data-comp` da linha: a composição precisa voltar pro Carrinho nos
  // steppers/remover, senão a chave não casa e o clique mexe na linha errada.
  function compAttr(it) {
    if (!it.comp || !it.comp.length) return '';
    return ' data-comp="' + escapeHtml(JSON.stringify(it.comp)) + '"';
  }

  function lerCompDaLinha(linha) {
    var raw = linha.getAttribute('data-comp');
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (e) { return null; }
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
      // Sourdough: mesmo no quick-add da vitrine o item nasce "fatiável"
      // (inteiro por padrão) pra o checkbox aparecer na linha do carrinho.
      fatiavel: !!el.getAttribute('data-fatiavel'),
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

  // ── Drawer (bottom sheet) do carrinho ───────────────────────────────
  // Permanente no _base.html, escondido por padrão. Abre quando o cliente
  // adiciona o primeiro item OU clica no ícone do carrinho no header.

  function drawerEl() { return document.getElementById('cart-drawer'); }
  function drawerCorpo() { return document.getElementById('cart-drawer-corpo'); }

  function renderDrawer() {
    var corpo = drawerCorpo();
    if (!corpo) return;   // página sem drawer (ex: confirmação)
    var itens = Carrinho.ler();
    if (!itens.length) {
      corpo.innerHTML = '<div class="cart-drawer-vazio">' +
        'Seu carrinho está vazio.</div>';
    } else {
      var html = '';
      itens.forEach(function (it) {
        var foto = it.imagem
          ? '<img src="' + it.imagem + '" alt="">'
          : '<div class="ph">🥐</div>';
        var fatCtrl = fatiadoControle(it);
        html +=
          '<div class="cart-drawer-linha" data-kind="' + it.kind +
          '" data-id="' + it.id + '" data-fatiado="' +
          (it.fatiado ? '1' : '') + '"' + compAttr(it) + '>' + foto +
          '<div><div class="nome">' + escapeHtml(it.nome) + '</div>' + fatCtrl +
          compResumoHtml(it) +
          '<div class="preco-un">' + fmtBRL(it.preco) + ' cada</div></div>' +
          '<div class="stepper">' +
          '<button type="button" data-acao="menos" aria-label="Diminuir">−</button>' +
          '<div class="qtd">' + it.qtd + '</div>' +
          '<button type="button" data-acao="mais" aria-label="Aumentar">+</button>' +
          '</div></div>';
      });
      corpo.innerHTML = html;
    }
    var totEl = document.getElementById('cart-drawer-total-valor');
    if (totEl) totEl.textContent = fmtBRL(Carrinho.total());
  }

  function abrirDrawer() {
    var dr = drawerEl();
    if (!dr) return;
    renderDrawer();
    dr.hidden = false;
    // Próximo frame: força reflow antes de aplicar a classe pra a transição
    // de translate rodar (sem isso o painel "aparece" sem deslizar).
    requestAnimationFrame(function () {
      dr.classList.add('aberto');
      dr.setAttribute('aria-hidden', 'false');
    });
    document.body.style.overflow = 'hidden'; // evita scroll do fundo
  }

  function fecharDrawer() {
    var dr = drawerEl();
    if (!dr) return;
    dr.classList.remove('aberto');
    dr.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    // Espera a transição terminar antes de esconder (mantém animação).
    setTimeout(function () {
      if (!dr.classList.contains('aberto')) dr.hidden = true;
    }, 320);
  }

  function ligarDrawer() {
    var dr = drawerEl();
    if (!dr) return;
    // Fecha: overlay, X, "continuar comprando" — qualquer [data-acao="fechar"]
    dr.addEventListener('click', function (e) {
      var fechar = e.target.closest('[data-acao="fechar"]');
      if (fechar) {
        e.preventDefault();
        fecharDrawer();
        return;
      }
      // Steppers nas linhas
      var btn = e.target.closest('.cart-drawer-linha button[data-acao]');
      if (!btn) return;
      var linha = btn.closest('.cart-drawer-linha');
      var kind = linha.getAttribute('data-kind');
      var id = linha.getAttribute('data-id');
      var fatiado = linha.getAttribute('data-fatiado') === '1';
      var atual = Carrinho.qtdDe(kind, id, fatiado);
      var acao = btn.getAttribute('data-acao');
      if (acao === 'mais') Carrinho.mudarQtd(kind, id, atual + 1, fatiado);
      else if (acao === 'menos') Carrinho.mudarQtd(kind, id, atual - 1, fatiado);
      renderDrawer();
    });
    // Checkbox "fatiado" da linha (change, não click).
    dr.addEventListener('change', function (e) {
      var chk = e.target.closest('input[data-acao="fatiado"]');
      if (!chk) return;
      var linha = chk.closest('.cart-drawer-linha');
      var fatiado = linha.getAttribute('data-fatiado') === '1';
      Carrinho.alternarFatiado(linha.getAttribute('data-kind'),
                               linha.getAttribute('data-id'), fatiado);
      renderDrawer();
    });
    // ESC fecha
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && dr.classList.contains('aberto')) {
        fecharDrawer();
      }
    });
    // Ícone do carrinho no header abre o drawer (em vez de navegar pra
    // página dedicada — UX clássico de e-commerce).
    var iconeCarrinho = document.querySelector('.topo-carrinho');
    if (iconeCarrinho) {
      iconeCarrinho.addEventListener('click', function (e) {
        // Página dedicada /loja/carrinho continua existindo. Só abre o
        // drawer se NÃO já estamos lá (senão fica circular).
        if (window.location.pathname === '/loja/carrinho') return;
        e.preventDefault();
        abrirDrawer();
      });
    }
  }

  // Expõe pra testes/console + pra outros scripts disparem se quiserem.
  window.Carrinho.abrirDrawer = abrirDrawer;
  window.Carrinho.fecharDrawer = fecharDrawer;

  // Re-renderiza o drawer toda vez que o carrinho muda (mantém em sincronia
  // com qualquer alteração — stepper do card, página do produto, etc).
  var _salvarOriginal = Carrinho.salvar.bind(Carrinho);
  Carrinho.salvar = function (itens) {
    _salvarOriginal(itens);
    if (drawerEl() && !drawerEl().hidden) renderDrawer();
  };

  // ── Wire dos botões "adicionar ao carrinho" (página de produto) ──────
  function ligarBotoesAdd() {
    var botoes = document.querySelectorAll('[data-add-carrinho]');
    botoes.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var qtdInput = document.getElementById('qtd-add');
        var qtd = qtdInput ? qtdInput.value : 1;
        // "Fatiado?" só existe pra sourdough (data-fatiavel no botão).
        var chkFat = document.getElementById('quer-fatiado');
        var fatiado = !!(btn.getAttribute('data-fatiavel') && chkFat
                         && chkFat.checked);
        Carrinho.adicionar({
          kind: btn.getAttribute('data-kind'),
          id: btn.getAttribute('data-id'),
          nome: btn.getAttribute('data-nome'),
          preco: btn.getAttribute('data-preco'),
          categoria: btn.getAttribute('data-categoria'),
          imagem: btn.getAttribute('data-imagem'),
          fatiado: fatiado,
          fatiavel: !!btn.getAttribute('data-fatiavel'),
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
      var fatCtrl = fatiadoControle(it);
      html +=
        '<div class="carrinho-linha" data-kind="' + it.kind +
        '" data-id="' + it.id + '" data-fatiado="' +
        (it.fatiado ? '1' : '') + '">' +
        '<div class="carrinho-foto">' + foto + '</div>' +
        '<div class="carrinho-desc">' +
        '<div class="carrinho-nome">' + escapeHtml(it.nome) + '</div>' + fatCtrl +
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
      var fatiado = linha.getAttribute('data-fatiado') === '1';
      linha.querySelector('[data-acao="menos"]').addEventListener('click',
        function () { ajustar(kind, id, -1, fatiado); });
      linha.querySelector('[data-acao="mais"]').addEventListener('click',
        function () { ajustar(kind, id, +1, fatiado); });
      linha.querySelector('[data-acao="remover"]').addEventListener('click',
        function () { Carrinho.remover(kind, id, fatiado); renderCarrinho(); });
      linha.querySelector('.qtd-in').addEventListener('change',
        function (e) {
          Carrinho.mudarQtd(kind, id, e.target.value, fatiado);
          renderCarrinho();
        });
      var chkFat = linha.querySelector('input[data-acao="fatiado"]');
      if (chkFat) chkFat.addEventListener('change', function () {
        Carrinho.alternarFatiado(kind, id, fatiado); renderCarrinho();
      });
    });
  }

  function ajustar(kind, id, delta, fatiado) {
    var atual = Carrinho.qtdDe(kind, id, fatiado);
    Carrinho.mudarQtd(kind, id, atual + delta, fatiado);
    renderCarrinho();
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Grava o carrinho na SESSÃO do servidor (fonte de verdade). Fire-and-forget:
  // a UI já atualizou pelo espelho; se a rede falhar, a próxima ação re-sincroniza
  // (e o localStorage segura o cache até lá).
  function _sincronizarServidor(itens) {
    try {
      var meta = document.querySelector('meta[name="csrf-token"]');
      fetch('/loja/api/carrinho', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': meta ? meta.getAttribute('content') : '',
        },
        credentials: 'same-origin',
        body: JSON.stringify({
          itens: (itens || []).map(function (it) {
            return { kind: it.kind, id: it.id, qtd: it.qtd,
                     fatiado: !!it.fatiado,
                     // Menu configurável: sem `comp` aqui a escolha morre
                     // antes do checkout (a SESSÃO é a fonte de verdade).
                     comp: (it.comp && it.comp.length) ? it.comp : null };
          }),
        }),
      }).catch(function () {});
    } catch (e) { /* sem fetch/rede — sincroniza depois */ }
  }

  // Inicializa o espelho a partir do carrinho da SESSÃO (injetado pelo servidor).
  // Migração: se a sessão está vazia mas há um carrinho antigo no localStorage,
  // sobe ele pro servidor uma vez (clientes que estavam no modelo localStorage).
  function inicializarMirror() {
    var sessao = [];
    var el = document.getElementById('carrinho-sessao');
    if (el) { try { sessao = JSON.parse(el.textContent || '[]'); } catch (e) { sessao = []; } }
    if (!Array.isArray(sessao)) sessao = [];
    if (sessao.length) {
      _mirror = sessao;
      try { localStorage.setItem(CHAVE, JSON.stringify(_mirror)); } catch (e) { /* cache */ }
      return;
    }
    var local = [];
    try { var raw = localStorage.getItem(CHAVE); local = raw ? JSON.parse(raw) : []; } catch (e) { local = []; }
    if (Array.isArray(local) && local.length) {
      _mirror = local;
      _sincronizarServidor(_mirror);   // migra pro servidor (vira fonte de verdade)
    } else {
      _mirror = [];
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('limpar-carrinho')) {
      // Pedido criado: o servidor já zerou a sessão. Limpa o cache local também
      // (NÃO migra — senão o carrinho "voltaria" depois de comprar).
      _mirror = [];
      try { localStorage.removeItem(CHAVE); } catch (e) { /* ok */ }
    } else {
      inicializarMirror();     // fonte: sessão do servidor (+ migra antigo 1x)
    }
    ligarCardAdds();           // event delegation — registra UMA vez
    ligarDrawer();             // handlers do drawer (1x por página)
    Carrinho.atualizarBadge(); // renderiza cards iniciais
    ligarBotoesAdd();
    renderCarrinho();
  });
})();
