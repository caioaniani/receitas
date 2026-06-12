/* Frente de caixa (PDV próprio) com captura de cartão na Clover Mini.
 *
 * Estado: carrinho local até o primeiro pagamento; aí a venda é criada no
 * servidor e o carrinho trava. Cartão com Clover ativa entra em modo
 * "aguardando maquininha" com polling do status da venda a cada 2s.
 */
(function () {
    'use strict';
    var CTX = window.CAIXA_CTX || {};
    var CSRF = CTX.csrf || '';

    var catalogo = [];
    var categoriaAtiva = '';
    var carrinho = [];          // [{tipo, id, nome, preco, qtd}]
    var venda = null;           // venda criada no servidor (trava o carrinho)
    var pollTimer = null;
    var pagamentoPendenteId = null;
    var ultimoTroco = 0;

    var $ = function (id) { return document.getElementById(id); };
    var modalDinheiro, modalAvulso, modalClover, modalFim;

    function fmt(n) {
        return 'R$ ' + (Number(n) || 0).toLocaleString('pt-BR',
            { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    function round2(n) { return Math.round((Number(n) || 0) * 100) / 100; }
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function msg(html, tipo) {
        $('cx-msg').innerHTML = html
            ? '<div class="alert alert-' + (tipo || 'danger') + ' py-2">' + html + '</div>' : '';
        if (html) setTimeout(function () { $('cx-msg').innerHTML = ''; }, 8000);
    }

    function api(path, opts) {
        opts = opts || {};
        var init = {
            method: opts.method || 'GET',
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { 'X-CSRFToken': CSRF }
        };
        if (opts.body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.body);
        }
        return fetch(path, init).then(function (r) {
            return r.text().then(function (txt) {
                var body = {};
                try { body = JSON.parse(txt); } catch (e) { body = { ok: false, erro: txt.slice(0, 200) }; }
                if (!r.ok && body.erro === undefined) body.erro = 'HTTP ' + r.status;
                body._status = r.status;
                return body;
            });
        });
    }

    function lojaId() { return parseInt($('cx-loja').value, 10) || null; }

    // ── Catálogo ──

    function carregarCatalogo() {
        var lid = lojaId();
        api('/pdv/caixa/api/catalogo' + (lid ? '?loja_id=' + lid : '')).then(function (r) {
            if (!r.ok) { msg('Erro ao carregar catálogo: ' + esc(r.erro)); return; }
            catalogo = r.itens || [];
            renderCategorias();
            renderCatalogo();
        });
    }

    function categorias() {
        var vistos = {}, lista = [];
        catalogo.forEach(function (i) {
            if (!vistos[i.categoria]) { vistos[i.categoria] = 1; lista.push(i.categoria); }
        });
        return lista;
    }

    function renderCategorias() {
        var html = '<button class="btn btn-sm ' + (categoriaAtiva === '' ? 'btn-dark' : 'btn-outline-dark') +
            '" data-cat="">Todas</button>';
        categorias().forEach(function (c) {
            html += '<button class="btn btn-sm ' + (categoriaAtiva === c ? 'btn-dark' : 'btn-outline-dark') +
                '" data-cat="' + esc(c) + '">' + esc(c) + '</button>';
        });
        $('cx-categorias').innerHTML = html;
        $('cx-categorias').querySelectorAll('button').forEach(function (b) {
            b.addEventListener('click', function () {
                categoriaAtiva = b.getAttribute('data-cat');
                renderCategorias();
                renderCatalogo();
            });
        });
    }

    function renderCatalogo() {
        var busca = ($('cx-busca').value || '').toLowerCase().trim();
        var html = '';
        catalogo.forEach(function (i, idx) {
            if (categoriaAtiva && i.categoria !== categoriaAtiva) return;
            if (busca && i.nome.toLowerCase().indexOf(busca) === -1) return;
            html += '<div class="col-6 col-md-4 col-xl-3">' +
                '<button class="btn btn-outline-primary w-100 h-100 py-2 px-1" data-idx="' + idx + '">' +
                '<div class="small fw-bold" style="line-height:1.15;">' + esc(i.nome) + '</div>' +
                '<div class="small text-success">' + fmt(i.preco) + '</div>' +
                '</button></div>';
        });
        $('cx-catalogo').innerHTML = html ||
            '<div class="text-muted small p-3">Nenhum item com preço de venda encontrado.</div>';
        $('cx-catalogo').querySelectorAll('button[data-idx]').forEach(function (b) {
            b.addEventListener('click', function () {
                addItem(catalogo[parseInt(b.getAttribute('data-idx'), 10)]);
            });
        });
    }

    // ── Carrinho ──

    function vendaTravada() { return venda !== null; }

    function addItem(item, qtd) {
        if (vendaTravada()) { msg('Venda já iniciada — conclua ou cancele antes de mudar itens.', 'warning'); return; }
        var existente = carrinho.find(function (c) {
            return c.tipo === item.tipo && c.id === item.id && item.tipo !== 'avulso';
        });
        if (existente) existente.qtd += (qtd || 1);
        else carrinho.push({ tipo: item.tipo, id: item.id, nome: item.nome, preco: item.preco, qtd: qtd || 1 });
        renderCarrinho();
    }

    function mudarQtd(idx, delta) {
        if (vendaTravada()) return;
        carrinho[idx].qtd += delta;
        if (carrinho[idx].qtd <= 0) carrinho.splice(idx, 1);
        renderCarrinho();
    }

    function subtotal() {
        return round2(carrinho.reduce(function (s, c) { return s + c.preco * c.qtd; }, 0));
    }

    function descontoAtual() {
        var d = round2(parseFloat($('cx-desconto').value) || 0);
        return d > 0 ? d : 0;
    }

    function totalAtual() {
        if (venda) return venda.total;
        return round2(Math.max(subtotal() - descontoAtual(), 0));
    }

    function renderCarrinho() {
        var temItens = venda ? venda.itens.length > 0 : carrinho.length > 0;
        $('cx-carrinho-vazio').style.display = temItens ? 'none' : '';
        $('cx-tabela').style.display = temItens ? '' : 'none';

        var html = '';
        if (venda) {
            venda.itens.forEach(function (i) {
                html += '<tr><td>' + esc(i.descricao) + '</td>' +
                    '<td class="text-center text-nowrap">' + i.quantidade + 'x</td>' +
                    '<td class="text-end text-nowrap">' + fmt(i.subtotal) + '</td></tr>';
            });
        } else {
            carrinho.forEach(function (c, idx) {
                html += '<tr><td>' + esc(c.nome) +
                    '<div class="text-muted" style="font-size:11px;">' + fmt(c.preco) + ' un.</div></td>' +
                    '<td class="text-center text-nowrap">' +
                    '<button class="btn btn-outline-secondary btn-sm py-0 px-1" data-menos="' + idx + '">−</button>' +
                    '<span class="mx-1">' + c.qtd + '</span>' +
                    '<button class="btn btn-outline-secondary btn-sm py-0 px-1" data-mais="' + idx + '">+</button></td>' +
                    '<td class="text-end text-nowrap">' + fmt(c.preco * c.qtd) + '</td></tr>';
            });
        }
        $('cx-itens').innerHTML = html;
        $('cx-itens').querySelectorAll('[data-menos]').forEach(function (b) {
            b.addEventListener('click', function () { mudarQtd(parseInt(b.getAttribute('data-menos'), 10), -1); });
        });
        $('cx-itens').querySelectorAll('[data-mais]').forEach(function (b) {
            b.addEventListener('click', function () { mudarQtd(parseInt(b.getAttribute('data-mais'), 10), +1); });
        });

        $('cx-subtotal').textContent = fmt(venda ? venda.subtotal : subtotal());
        $('cx-total').textContent = fmt(totalAtual());
        $('cx-venda-code').textContent = venda ? venda.code : '';
        $('cx-desconto').disabled = vendaTravada();
        $('cx-cancelar-venda').style.display = venda && venda.status === 'aberta' ? '' : 'none';

        var pago = venda ? venda.total_pago : 0;
        var restante = venda ? venda.restante : totalAtual();
        $('cx-pago-row').style.setProperty('display', pago > 0 ? 'flex' : 'none', 'important');
        $('cx-pago').textContent = fmt(pago);
        var mostraRestante = venda && pago > 0 && restante > 0;
        $('cx-restante-row').style.setProperty('display', mostraRestante ? 'flex' : 'none', 'important');
        $('cx-restante').textContent = fmt(restante);

        var pg = '';
        (venda ? venda.pagamentos : []).forEach(function (p) {
            var cor = { aprovado: 'success', negado: 'danger', erro: 'danger',
                        cancelado: 'secondary', aguardando_clover: 'warning' }[p.status] || 'secondary';
            pg += '<div><span class="badge text-bg-' + cor + '">' + esc(p.metodo) + ' ' + fmt(p.valor) +
                ' — ' + esc(p.status.replace('_clover', ' maquininha')) + '</span>' +
                (p.erro ? ' <span class="text-danger" style="font-size:11px;">' + esc(p.erro) + '</span>' : '') +
                '</div>';
        });
        $('cx-pagamentos-feitos').innerHTML = pg;
    }

    function limpar() {
        carrinho = [];
        venda = null;
        pagamentoPendenteId = null;
        ultimoTroco = 0;
        $('cx-desconto').value = '';
        pararPolling();
        renderCarrinho();
    }

    // ── Venda / pagamento ──

    function garantirVenda() {
        if (venda) return Promise.resolve(venda);
        if (!carrinho.length) { msg('Adicione itens antes de cobrar.', 'warning'); return Promise.reject(); }
        var body = {
            loja_id: lojaId(),
            desconto: descontoAtual(),
            itens: carrinho.map(function (c) {
                var it = { tipo: c.tipo, quantidade: c.qtd };
                if (c.tipo === 'avulso') { it.descricao = c.nome; it.preco_unitario = c.preco; }
                else it.id = c.id;
                return it;
            })
        };
        return api('/pdv/caixa/api/vendas', { method: 'POST', body: body }).then(function (r) {
            if (!r.ok) { msg('Erro ao abrir venda: ' + esc(r.erro)); throw new Error(r.erro); }
            venda = r.venda;
            renderCarrinho();
            return venda;
        });
    }

    function pagar(metodo, valor, valorRecebido) {
        garantirVenda().then(function () {
            var body = { metodo: metodo };
            if (valor != null) body.valor = valor;
            if (valorRecebido != null) body.valor_recebido = valorRecebido;
            return api('/pdv/caixa/api/vendas/' + venda.id + '/pagamentos',
                       { method: 'POST', body: body });
        }).then(function (r) {
            if (!r) return;
            if (!r.ok) { msg('Pagamento: ' + esc(r.erro)); return; }
            venda = r.venda;
            renderCarrinho();
            if (r.aguardando) {
                pagamentoPendenteId = r.pagamento_id;
                var p = venda.pagamentos.find(function (x) { return x.id === r.pagamento_id; });
                $('cx-clover-valor').textContent = p ? fmt(p.valor) : '';
                modalClover.show();
                iniciarPolling();
            } else {
                aposPagamento();
            }
        }).catch(function () { /* mensagem já mostrada */ });
    }

    function aposPagamento() {
        var ultimo = venda.pagamentos[venda.pagamentos.length - 1];
        if (ultimo && ultimo.troco > 0) ultimoTroco = ultimo.troco;
        if (venda.status === 'paga') {
            carregarVendasDia();
            mostrarFim();
        } else if (venda.restante > 0) {
            msg('Pagamento parcial registrado. Restam <b>' + fmt(venda.restante) + '</b>.', 'info');
        }
    }

    function mostrarFim() {
        $('cx-fim-code').textContent = venda.code;
        $('cx-fim-total').textContent = 'Total ' + fmt(venda.total);
        if (ultimoTroco > 0) {
            $('cx-fim-troco').style.display = '';
            $('cx-fim-troco').textContent = 'Troco ' + fmt(ultimoTroco);
        } else {
            $('cx-fim-troco').style.display = 'none';
        }
        modalFim.show();
    }

    // ── Polling do pagamento Clover ──

    function iniciarPolling() {
        pararPolling();
        pollTimer = setInterval(function () {
            if (!venda) { pararPolling(); return; }
            api('/pdv/caixa/api/vendas/' + venda.id).then(function (r) {
                if (!r.ok) return;
                venda = r.venda;
                renderCarrinho();
                var p = venda.pagamentos.find(function (x) { return x.id === pagamentoPendenteId; });
                if (!p || p.status === 'aguardando_clover') return;
                pararPolling();
                modalClover.hide();
                pagamentoPendenteId = null;
                if (p.status === 'aprovado') {
                    aposPagamento();
                } else if (p.status === 'negado') {
                    msg('Cartão não aprovado: ' + esc(p.erro || '') + ' — tente de novo ou outra forma.', 'warning');
                } else if (p.status === 'erro') {
                    msg('Erro na maquininha: ' + esc(p.erro || '') + '. Se o cliente pagou, registre como captura manual.', 'danger');
                }
            });
        }, 2000);
    }

    function pararPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function cancelarClover() {
        if (!venda || !pagamentoPendenteId) { modalClover.hide(); return; }
        api('/pdv/caixa/api/vendas/' + venda.id + '/pagamentos/' + pagamentoPendenteId + '/cancelar',
            { method: 'POST' }).then(function (r) {
            pararPolling();
            modalClover.hide();
            pagamentoPendenteId = null;
            if (r.ok) { venda = r.venda; renderCarrinho(); }
            else msg(esc(r.erro || 'não consegui cancelar'));
        });
    }

    // ── Vendas do dia ──

    function carregarVendasDia() {
        api('/pdv/caixa/api/vendas-dia').then(function (r) {
            if (!r.ok) return;
            var resumo = 'Pagas: <b>' + fmt(r.total_pagas) + '</b>';
            Object.keys(r.por_metodo || {}).forEach(function (m) {
                resumo += ' · ' + esc(m) + ' ' + fmt(r.por_metodo[m]);
            });
            $('cx-dia-resumo').innerHTML = resumo;
            var html = '';
            (r.vendas || []).forEach(function (v) {
                var cor = { paga: 'success', aberta: 'warning', cancelada: 'secondary' }[v.status] || 'secondary';
                var hora = v.criado_em ? new Date(v.criado_em + 'Z').toLocaleTimeString('pt-BR',
                    { hour: '2-digit', minute: '2-digit', timeZone: 'America/Sao_Paulo' }) : '';
                var comandaErro = (v.impressoes || []).some(function (im) { return im.status === 'erro'; });
                var temSetor = (v.itens || []).some(function (i) { return i.setor; });
                var btnImp = (v.status === 'paga' && temSetor)
                    ? ' <button class="btn py-0 px-1 ' + (comandaErro ? 'btn-outline-danger' : 'btn-outline-secondary') +
                      '" data-reimprimir="' + v.id + '" title="' +
                      (comandaErro ? 'Comanda falhou — reimprimir' : 'Reimprimir comandas') + '">' +
                      '<i class="bi bi-printer"></i></button>'
                    : '';
                html += '<div class="d-flex justify-content-between align-items-center border-bottom py-1">' +
                    '<span>' + hora + ' <span class="text-muted">' + esc(v.code) + '</span></span>' +
                    '<span><span class="badge text-bg-' + cor + '">' + esc(v.status) + '</span> ' +
                    fmt(v.total) + btnImp + '</span></div>';
            });
            $('cx-dia-lista').innerHTML = html || '<div class="text-muted py-2">Nenhuma venda hoje.</div>';
            $('cx-dia-lista').querySelectorAll('[data-reimprimir]').forEach(function (b) {
                b.addEventListener('click', function () {
                    b.disabled = true;
                    api('/pdv/caixa/api/vendas/' + b.getAttribute('data-reimprimir') + '/imprimir',
                        { method: 'POST' }).then(function (r2) {
                        b.disabled = false;
                        if (!r2.ok) { msg('Comandas: ' + esc(r2.erro), 'warning'); return; }
                        var falhas = (r2.resultados || []).filter(function (x) { return x.status !== 'ok'; });
                        msg(falhas.length
                            ? 'Comanda falhou: ' + falhas.map(function (x) {
                                return esc(x.setor) + ' — ' + esc(x.erro || '');
                              }).join('; ')
                            : 'Comandas enviadas: ' + (r2.resultados || []).map(function (x) {
                                return esc(x.setor);
                              }).join(', '),
                            falhas.length ? 'warning' : 'success');
                        carregarVendasDia();
                    });
                });
            });
        });
    }

    // ── Status Clover ──

    function statusClover() {
        var badge = $('cx-clover-badge');
        if (!CTX.cloverAtivo) {
            badge.className = 'badge text-bg-secondary';
            badge.innerHTML = '<i class="bi bi-credit-card"></i> Clover: não integrada';
            return;
        }
        api('/pdv/caixa/api/clover/status').then(function (r) {
            var ok = r.ok && r.ping && r.ping.ok;
            badge.className = 'badge text-bg-' + (ok ? 'success' : 'danger');
            badge.innerHTML = '<i class="bi bi-credit-card"></i> Clover: ' +
                (ok ? 'conectada' + (r.modo === 'simulado' ? ' (simulada)' : '') : 'offline');
            badge.title = (r.ping && r.ping.detalhe) || '';
        });
    }

    // ── Bind ──

    document.addEventListener('DOMContentLoaded', function () {
        modalDinheiro = new bootstrap.Modal($('cx-modal-dinheiro'));
        modalAvulso = new bootstrap.Modal($('cx-modal-avulso'));
        modalClover = new bootstrap.Modal($('cx-modal-clover'));
        modalFim = new bootstrap.Modal($('cx-modal-fim'));

        $('cx-busca').addEventListener('input', renderCatalogo);
        $('cx-loja').addEventListener('change', function () {
            if (vendaTravada()) { msg('Conclua a venda antes de trocar de loja.', 'warning'); return; }
            carrinho = [];
            renderCarrinho();
            carregarCatalogo();
        });
        $('cx-desconto').addEventListener('input', renderCarrinho);
        $('cx-limpar').addEventListener('click', function () {
            if (vendaTravada()) { msg('Venda iniciada — use "Cancelar venda".', 'warning'); return; }
            limpar();
        });

        $('cx-cancelar-venda').addEventListener('click', function () {
            if (!venda) return;
            if (!window.confirm('Cancelar a venda ' + venda.code + '?')) return;
            api('/pdv/caixa/api/vendas/' + venda.id + '/cancelar', { method: 'POST' }).then(function (r) {
                if (!r.ok) { msg(esc(r.erro)); return; }
                limpar();
                carregarVendasDia();
            });
        });

        document.querySelectorAll('[data-metodo]').forEach(function (b) {
            b.addEventListener('click', function () {
                var metodo = b.getAttribute('data-metodo');
                var restante = venda ? venda.restante : totalAtual();
                if (restante <= 0 && !venda) { msg('Adicione itens antes de cobrar.', 'warning'); return; }
                if (metodo === 'dinheiro') {
                    $('cx-din-valor').value = restante.toFixed(2);
                    $('cx-din-recebido').value = '';
                    $('cx-din-troco').textContent = fmt(0);
                    modalDinheiro.show();
                    setTimeout(function () { $('cx-din-recebido').focus(); }, 300);
                } else {
                    pagar(metodo);
                }
            });
        });

        function calcTroco() {
            var v = parseFloat($('cx-din-valor').value) || 0;
            var rec = parseFloat($('cx-din-recebido').value) || 0;
            $('cx-din-troco').textContent = fmt(Math.max(rec - v, 0));
        }
        $('cx-din-valor').addEventListener('input', calcTroco);
        $('cx-din-recebido').addEventListener('input', calcTroco);
        $('cx-din-confirmar').addEventListener('click', function () {
            var v = round2(parseFloat($('cx-din-valor').value) || 0);
            var rec = round2(parseFloat($('cx-din-recebido').value) || 0) || v;
            if (v <= 0) { return; }
            if (rec < v) { msg('Valor recebido menor que o valor a pagar.', 'warning'); return; }
            modalDinheiro.hide();
            pagar('dinheiro', v, rec);
        });

        $('cx-avulso').addEventListener('click', function () {
            if (vendaTravada()) { msg('Venda já iniciada.', 'warning'); return; }
            $('cx-av-desc').value = '';
            $('cx-av-preco').value = '';
            $('cx-av-qtd').value = '1';
            modalAvulso.show();
            setTimeout(function () { $('cx-av-desc').focus(); }, 300);
        });
        $('cx-av-confirmar').addEventListener('click', function () {
            var desc = ($('cx-av-desc').value || '').trim();
            var preco = round2(parseFloat($('cx-av-preco').value) || 0);
            var qtd = parseInt($('cx-av-qtd').value, 10) || 1;
            if (!desc || preco <= 0) { return; }
            modalAvulso.hide();
            addItem({ tipo: 'avulso', id: null, nome: desc, preco: preco }, qtd);
        });

        $('cx-clover-cancelar').addEventListener('click', cancelarClover);
        $('cx-fim-nova').addEventListener('click', function () { modalFim.hide(); limpar(); });
        $('cx-modal-fim').addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { modalFim.hide(); limpar(); }
        });
        $('cx-recarregar-dia').addEventListener('click', carregarVendasDia);

        carregarCatalogo();
        carregarVendasDia();
        statusClover();
        setInterval(statusClover, 60000);
        renderCarrinho();
    });
})();
