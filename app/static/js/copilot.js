/* Copilot UI — chat com preview de ações.
   Tools: criar_pedido, receber_mp, ajuste_estoque, registrar_desperdicio (write — preview obrigatório)
          consultar_pedido, consultar_estoque (read — resultado direto). */
(function() {
    var fab = document.getElementById('copilot-fab');
    var panel = document.getElementById('copilot-panel');
    var overlay = document.getElementById('copilot-overlay');
    var closeBtn = document.getElementById('copilot-close');
    var history = document.getElementById('copilot-history');
    var form = document.getElementById('copilot-form');
    var input = document.getElementById('copilot-input');
    var sendBtn = document.getElementById('copilot-send');
    if (!fab || !panel) return;

    // Lojas pra dropdown (carregada sob demanda)
    var __lojasCache = null;

    // Historico de conversa em memoria pra dar contexto ao Claude
    // ('ah entendi, foi aqui' depois de uma resposta).
    // [{role: 'user'|'assistant', content: 'texto'}]
    var copilotHistorico = [];

    function carregarLojas() {
        if (__lojasCache) return Promise.resolve(__lojasCache);
        return fetch('/copilot/api/lojas', {credentials: 'same-origin'})
            .then(function(r) { return r.ok ? r.json() : {lojas: []}; })
            .catch(function() { return {lojas: []}; })
            .then(function(d) { __lojasCache = d.lojas || []; return __lojasCache; });
    }

    function abrir() {
        panel.classList.remove('d-none');
        overlay.classList.remove('d-none');
        setTimeout(function() { input && input.focus(); }, 60);
        carregarLojas();
    }
    function fechar() {
        panel.classList.add('d-none');
        overlay.classList.add('d-none');
    }
    fab.addEventListener('click', abrir);
    closeBtn.addEventListener('click', fechar);
    overlay.addEventListener('click', fechar);
    document.addEventListener('keydown', function(e) {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            panel.classList.contains('d-none') ? abrir() : fechar();
        } else if (e.key === 'Escape' && !panel.classList.contains('d-none')) {
            fechar();
        }
    });

    function escape(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
            return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
        });
    }
    function nowHHMM() {
        var d = new Date();
        return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }
    function addMsg(role, html) {
        var div = document.createElement('div');
        div.className = 'copilot-msg ' + role;
        div.innerHTML = '<div class="bubble">' + html + '</div><div class="timestamp">' + nowHHMM() + '</div>';
        history.appendChild(div);
        history.scrollTop = history.scrollHeight;
        return div;
    }
    function texto2html(t) {
        // markdown bem leve: **bold**, quebras de linha
        return escape(t).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
    }

    // ── Previews por tipo de ação ─────────────────────────────

    function previewCriarPedido(conversaId, params) {
        var lojaSel = '<select class="form-control form-control-sm copilot-loja-sel">';
        lojaSel += '<option value="">— escolha a loja —</option>';
        (__lojasCache || []).forEach(function(l) {
            var sel = (params.loja_id === l.id) ? ' selected' : '';
            lojaSel += '<option value="' + l.id + '"' + sel + '>' + escape(l.nome) + '</option>';
        });
        lojaSel += '</select>';

        var dataInput = '<input type="date" class="form-control form-control-sm copilot-data-input" value="' + escape(params.data_entrega || '') + '">';
        var obsInput = '<input type="text" class="form-control form-control-sm copilot-obs-input" placeholder="opcional" value="' + escape(params.observacao || '') + '">';

        var itensHtml = '';
        (params.itens || []).forEach(function(item, idx) {
            var optHtml = '<option value="">— escolha —</option>';
            (item.matches || []).forEach(function(m) {
                var sel = (item.resolvido && m.tipo === item.resolvido.tipo && m.id === item.resolvido.id) ? ' selected' : '';
                optHtml += '<option value="' + m.tipo + ':' + m.id + '"' + sel + '>' + escape(m.nome) + '</option>';
            });
            var warn = !item.resolvido ? '<div class="warn">não achei "' + escape(item.nome_original) + '"</div>' : '';
            var obsVal = item.observacao || '';
            itensHtml += '<div class="copilot-preview-item" data-idx="' + idx + '">' +
                '<input type="number" min="1" class="form-control form-control-sm qty-input" value="' + item.quantidade + '" style="max-width:60px; text-align:right;">' +
                '<span style="margin:0 4px;">×</span>' +
                '<select class="form-control form-control-sm copilot-item-sel">' + optHtml + '</select>' +
                '<input type="text" class="form-control form-control-sm copilot-item-obs" placeholder="obs (ex: backup)" maxlength="200" value="' + escape(obsVal) + '" style="max-width:140px; margin-left:6px;">' +
                '<button type="button" class="btn-circle copilot-item-remove" title="Remover" style="width:28px;height:28px;"><i class="bi bi-x"></i></button>' +
                '</div>' + warn;
        });

        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="criar_pedido">' +
            '<div class="copilot-preview-header">criar pedido</div>' +
            '<div class="copilot-preview-row"><span class="label">loja</span></div>' + lojaSel +
            '<div class="copilot-preview-row"><span class="label">data</span></div>' + dataInput +
            '<div class="copilot-preview-row"><span class="label">observação</span></div>' + obsInput +
            '<div class="copilot-preview-row"><span class="label">itens</span></div>' +
            '<div class="copilot-items">' + itensHtml + '</div>' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve">criar pedido</button>' +
            '</div></div>';
    }

    function previewReceberMP(conversaId, params) {
        var mp = params.mp_resolvida;
        var mpInfo = mp ? escape(mp.nome) + ' (' + escape(mp.unidade || '') + ')' : '<span class="warn">MP não identificada: ' + escape(params.mp_nome) + '</span>';
        var precoTotal = params.preco_total || '';
        var ref = params.referencia || '';
        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="receber_mp">' +
            '<div class="copilot-preview-header">receber MP</div>' +
            '<div class="copilot-preview-row"><span class="label">MP</span><strong>' + mpInfo + '</strong></div>' +
            '<div class="copilot-preview-row"><span class="label">quantidade</span></div>' +
            '<input type="number" step="0.01" min="0.01" class="form-control form-control-sm copilot-mp-qtd" value="' + (params.quantidade || '') + '">' +
            '<div class="copilot-preview-row"><span class="label">preço total (R$)</span></div>' +
            '<input type="number" step="0.01" min="0" class="form-control form-control-sm copilot-mp-preco" value="' + precoTotal + '" placeholder="opcional">' +
            '<div class="copilot-preview-row"><span class="label">referência (NF/fornecedor)</span></div>' +
            '<input type="text" class="form-control form-control-sm copilot-mp-ref" value="' + escape(ref) + '" placeholder="opcional">' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve">registrar entrada</button>' +
            '</div></div>';
    }

    function previewAjusteEstoque(conversaId, params) {
        var mp = params.mp_resolvida;
        var mpInfo = mp ? escape(mp.nome) + ' (' + escape(mp.unidade || '') + ')' : '<span class="warn">MP não identificada</span>';
        var tipoSel = '<select class="form-control form-control-sm copilot-ajuste-tipo">' +
            '<option value="entrada"' + (params.tipo === 'entrada' ? ' selected' : '') + '>entrada (+)</option>' +
            '<option value="saida"' + (params.tipo === 'saida' ? ' selected' : '') + '>saída (−)</option></select>';
        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="ajuste_estoque">' +
            '<div class="copilot-preview-header">ajuste de estoque</div>' +
            '<div class="copilot-preview-row"><span class="label">MP</span><strong>' + mpInfo + '</strong></div>' +
            '<div class="copilot-preview-row"><span class="label">tipo</span></div>' + tipoSel +
            '<div class="copilot-preview-row"><span class="label">quantidade</span></div>' +
            '<input type="number" step="0.01" min="0.01" class="form-control form-control-sm copilot-ajuste-qtd" value="' + (params.quantidade || '') + '">' +
            '<div class="copilot-preview-row"><span class="label">motivo</span></div>' +
            '<input type="text" class="form-control form-control-sm copilot-ajuste-motivo" value="' + escape(params.motivo || '') + '" placeholder="ex: quebra por umidade">' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve">registrar ajuste</button>' +
            '</div></div>';
    }

    // Preview generico: lista os params como tabela read-only + botoes
    // aprovar/cancelar. Usado pra tools simples sem UI customizada.
    function previewBalancoCongelados(conversaId, params) {
        var itens = params.itens || [];
        var totais = params.totais || {};
        var ref = params.referencia ? escape(params.referencia) : '(sem referência)';
        var n_ok = totais.resolvidos || 0;
        var n_nao = totais.nao_resolvidos || 0;
        var delta = totais.delta_total;

        var n_aplicaveis = n_ok + n_nao;
        var resumo = '<div class="copilot-preview-row" style="font-size:12px;">' +
            '<span class="badge bg-success" style="margin-right:4px;">' + n_ok + ' com match</span>' +
            (n_nao ? '<span class="badge bg-warning text-dark" style="margin-right:4px;">' + n_nao + ' pendente(s)</span>' : '') +
            (delta !== undefined && delta !== null
                ? '<span class="badge bg-info text-dark" style="margin-right:4px;">delta ' + (delta >= 0 ? '+' : '') + delta + '</span>'
                : '') +
            '<span style="color:var(--text-muted);">ref: ' + ref + '</span>' +
            '</div>';

        var rows = '';
        itens.forEach(function(it, idx) {
            var nome = escape(it.nome || '?');
            var qtd = it.quantidade !== undefined ? it.quantidade : '';
            var atual = (it.estoque_atual !== undefined && !it.erro) ? it.estoque_atual : '—';
            var d = it.delta;
            var deltaCell = '—';
            if (!it.erro && d !== null && d !== undefined) {
                if (d > 0) deltaCell = '<span style="color:#198754;">+' + d + '</span>';
                else if (d < 0) deltaCell = '<span style="color:#dc3545;">' + d + '</span>';
                else deltaCell = '<span style="color:#888;">0</span>';
            }
            var match;
            if (it.erro) {
                match = '<span style="color:#dc3545;">⚠ ' + escape(it.erro) + '</span>';
            } else if (it.resolvido) {
                var tag = it.resolvido.match === 'fuzzy' ? ' <small style="color:#888;">(fuzzy)</small>' : '';
                match = escape(it.resolvido.nome) + tag;
            } else {
                match = '<span style="color:#cc7700;" title="Vai entrar como pendente — vincule a uma receita depois em /pedidos/congelados">↪ pendente</span>';
            }
            rows += '<tr>' +
                '<td style="color:#888;">' + (idx + 1) + '</td>' +
                '<td>' + nome + '</td>' +
                '<td>' + match + '</td>' +
                '<td style="text-align:right;">' + atual + '</td>' +
                '<td style="text-align:right; font-weight:600;">' + qtd + '</td>' +
                '<td style="text-align:right; font-weight:600;">' + deltaCell + '</td>' +
                '</tr>';
        });

        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="balanco_congelados">' +
            '<div class="copilot-preview-header">balanço de congelados</div>' +
            resumo +
            '<div style="max-height:280px; overflow-y:auto; margin-top:6px;">' +
            '<table class="table table-sm table-bordered mb-0" style="font-size:11.5px;">' +
            '<thead><tr><th>#</th><th>ditado</th><th>match</th>' +
            '<th style="text-align:right;">atual</th><th style="text-align:right;">novo</th>' +
            '<th style="text-align:right;">Δ</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table></div>' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve"' +
            (n_aplicaveis === 0 ? ' disabled' : '') + '>' +
            'aplicar ' + n_aplicaveis + ' item(s)</button>' +
            '</div></div>';
    }

    function previewEntradaLoteLoja(conversaId, params) {
        var itens = params.itens || [];
        var totais = params.totais || {};
        var lojaNome = params.loja_nome ? escape(params.loja_nome) : '(sem loja)';
        var ref = params.referencia ? escape(params.referencia) : '(sem referência)';
        var n_ok = totais.resolvidos || 0;
        var n_nao = totais.nao_resolvidos || 0;
        var delta = totais.delta_total;
        var n_aplicaveis = n_ok + n_nao;

        var resumo = '<div class="copilot-preview-row" style="font-size:12px;">' +
            '<span class="badge bg-primary" style="margin-right:4px;">loja: ' + lojaNome + '</span>' +
            '<span class="badge bg-success" style="margin-right:4px;">' + n_ok + ' com match</span>' +
            (n_nao ? '<span class="badge bg-warning text-dark" style="margin-right:4px;">' + n_nao + ' pendente(s)</span>' : '') +
            (delta !== undefined && delta !== null
                ? '<span class="badge bg-info text-dark" style="margin-right:4px;">soma total +' + delta + '</span>'
                : '') +
            '<span style="color:var(--text-muted);">ref: ' + ref + '</span>' +
            '</div>';

        var rows = '';
        itens.forEach(function(it, idx) {
            var nome = escape(it.nome || '?');
            var qtd = it.quantidade !== undefined ? it.quantidade : '';
            var atual = (it.estoque_atual !== undefined && !it.erro) ? it.estoque_atual : '—';
            var novo = (it.novo !== undefined && !it.erro) ? it.novo : '—';
            var match;
            if (it.erro) {
                match = '<span style="color:#dc3545;">⚠ ' + escape(it.erro) + '</span>';
            } else if (it.resolvido) {
                var tag = it.resolvido.match === 'fuzzy' ? ' <small style="color:#888;">(fuzzy)</small>' : '';
                match = escape(it.resolvido.nome) + tag;
            } else {
                match = '<span style="color:#cc7700;" title="Vai entrar como pendente — vincule a uma receita/produto/MP depois">↪ pendente</span>';
            }
            rows += '<tr>' +
                '<td style="color:#888;">' + (idx + 1) + '</td>' +
                '<td>' + nome + '</td>' +
                '<td>' + match + '</td>' +
                '<td style="text-align:right;">' + atual + '</td>' +
                '<td style="text-align:right; color:#198754; font-weight:600;">+' + qtd + '</td>' +
                '<td style="text-align:right; font-weight:600;">' + novo + '</td>' +
                '</tr>';
        });

        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="entrada_lote_loja">' +
            '<div class="copilot-preview-header">entrada em lote no estoque de loja</div>' +
            resumo +
            '<div style="max-height:280px; overflow-y:auto; margin-top:6px;">' +
            '<table class="table table-sm table-bordered mb-0" style="font-size:11.5px;">' +
            '<thead><tr><th>#</th><th>ditado</th><th>match</th>' +
            '<th style="text-align:right;">atual</th><th style="text-align:right;">soma</th>' +
            '<th style="text-align:right;">novo</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table></div>' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve"' +
            (n_aplicaveis === 0 ? ' disabled' : '') + '>' +
            'aplicar ' + n_aplicaveis + ' item(s)</button>' +
            '</div></div>';
    }

    function previewRegistrarDesperdicioLote(conversaId, params) {
        var itens = params.itens || [];
        var totais = params.totais || {};
        var lojaNome = params.loja_nome ? escape(params.loja_nome) : '<span style="color:#dc3545;">(sem loja)</span>';
        var motivo = escape(params.motivo || 'vencido');
        var obs = params.observacao ? escape(params.observacao) : '';
        var n_ok = totais.resolvidos || 0;
        var n_nao = totais.nao_resolvidos || 0;
        var n_err = totais.erros || 0;
        var totalQtd = totais.delta_total;

        var resumo = '<div class="copilot-preview-row" style="font-size:12px;">' +
            '<span class="badge bg-primary" style="margin-right:4px;">loja: ' + lojaNome + '</span>' +
            '<span class="badge bg-danger" style="margin-right:4px;">' + motivo + '</span>' +
            '<span class="badge bg-success" style="margin-right:4px;">' + n_ok + ' com match</span>' +
            (n_nao ? '<span class="badge bg-warning text-dark" style="margin-right:4px;">' + n_nao + ' nao encontrado(s)</span>' : '') +
            (n_err ? '<span class="badge bg-secondary" style="margin-right:4px;">' + n_err + ' invalido(s)</span>' : '') +
            (totalQtd !== undefined && totalQtd !== null
                ? '<span class="badge bg-info text-dark" style="margin-right:4px;">total -' + totalQtd + '</span>'
                : '') +
            (obs ? '<span style="color:var(--text-muted);">obs: ' + obs + '</span>' : '') +
            '</div>';

        var rows = '';
        itens.forEach(function(it, idx) {
            var nome = escape(it.nome || '?');
            var qtd = it.quantidade !== undefined ? it.quantidade : '';
            var atual = (it.estoque_atual !== undefined && it.estoque_atual !== null) ? it.estoque_atual : '—';
            var obsItem = it.observacao ? ' <small style="color:#888;">' + escape(it.observacao) + '</small>' : '';
            var match;
            if (it.erro) {
                match = '<span style="color:#dc3545;">⚠ ' + escape(it.erro) + '</span>';
            } else if (it.resolvido) {
                match = escape(it.resolvido.nome) + obsItem;
            } else {
                match = '<span style="color:#cc7700;" title="Item nao encontrado no cadastro — sera ignorado">⚠ nao encontrado</span>';
            }
            rows += '<tr>' +
                '<td style="color:#888;">' + (idx + 1) + '</td>' +
                '<td>' + nome + '</td>' +
                '<td>' + match + '</td>' +
                '<td style="text-align:right;">' + atual + '</td>' +
                '<td style="text-align:right; color:#dc3545; font-weight:600;">-' + qtd + '</td>' +
                '</tr>';
        });

        var podeAplicar = n_ok > 0 && params.loja_id;
        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="registrar_desperdicio_lote">' +
            '<div class="copilot-preview-header">registrar desperdicio em lote</div>' +
            resumo +
            '<div style="max-height:280px; overflow-y:auto; margin-top:6px;">' +
            '<table class="table table-sm table-bordered mb-0" style="font-size:11.5px;">' +
            '<thead><tr><th>#</th><th>ditado</th><th>match</th>' +
            '<th style="text-align:right;">atual</th><th style="text-align:right;">baixa</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table></div>' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve"' +
            (podeAplicar ? '' : ' disabled') + '>' +
            'registrar ' + n_ok + ' item(s)</button>' +
            '</div></div>';
    }

    function previewCriarVendaB2B(conversaId, params) {
        var itens = params.itens || [];
        var parcelas = params.parcelas || [];
        var cliBadge;
        if (params.cliente_avulso) {
            cliBadge = '<span class="badge bg-warning text-dark">avulso</span> ' + escape(params.cliente_nome_resolvido || params.cliente_nome || '?');
        } else {
            cliBadge = '<span class="badge bg-info text-dark">cadastrado</span> ' + escape(params.cliente_nome_resolvido || '?');
            if (params.cliente_desconto) cliBadge += ' <small class="text-muted">(' + params.cliente_desconto + '% desc)</small>';
        }
        var data = params.data_venda || 'hoje';
        var nf = params.nf_numero ? ' · NF ' + escape(params.nf_numero) : '';

        var rows = '';
        itens.forEach(function(it, idx) {
            var nome = it.resolvido ? escape(it.resolvido.nome) : '<span class="text-danger">' + escape(it.nome_original) + ' (não achei)</span>';
            var atual = (it.estoque_atual !== null && it.estoque_atual !== undefined) ? it.estoque_atual : '—';
            var preco = (it.preco_unitario !== undefined) ? Number(it.preco_unitario).toFixed(2) : '0,00';
            var subt = (it.subtotal !== undefined) ? Number(it.subtotal).toFixed(2) : '0,00';
            rows += '<tr>' +
                '<td>' + nome + '</td>' +
                '<td class="text-end" style="color:#888;">' + atual + '</td>' +
                '<td class="text-end">' + it.quantidade + '</td>' +
                '<td class="text-end">R$ ' + preco + '</td>' +
                '<td class="text-end fw-bold">R$ ' + subt + '</td>' +
                '</tr>';
        });

        var parcelasHtml = '';
        if (parcelas.length) {
            parcelasHtml = '<div class="copilot-preview-row mt-2"><span class="label">parcelas</span></div><ul style="font-size:11.5px; margin-bottom:0;">';
            parcelas.forEach(function(p) {
                parcelasHtml += '<li>' + escape(p.vencimento || '?') + ' · R$ ' + Number(p.valor || 0).toFixed(2) + (p.forma_pagamento ? ' (' + escape(p.forma_pagamento) + ')' : '') + '</li>';
            });
            parcelasHtml += '</ul>';
        } else {
            parcelasHtml = '<div class="copilot-preview-row mt-2"><small class="text-muted">sem parcelas — 1 parcela única no dia da venda</small></div>';
        }

        var nResolv = itens.filter(function(it) { return it.resolvido; }).length;
        var podeCriar = nResolv > 0 && (params.cliente_nome_resolvido || '').trim();

        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="criar_venda_b2b">' +
            '<div class="copilot-preview-header">criar venda B2B</div>' +
            '<div class="copilot-preview-row"><span class="label">cliente</span> ' + cliBadge + '</div>' +
            '<div class="copilot-preview-row"><span class="label">data</span> ' + escape(data) + nf + '</div>' +
            '<div style="max-height:280px; overflow-y:auto; margin-top:6px;">' +
            '<table class="table table-sm table-bordered mb-0" style="font-size:11.5px;">' +
            '<thead><tr><th>item</th><th class="text-end">freezer</th><th class="text-end">qtd</th><th class="text-end">preço</th><th class="text-end">subt.</th></tr></thead>' +
            '<tbody>' + rows + '</tbody>' +
            '<tfoot><tr><td colspan="4" class="text-end fw-bold">total</td><td class="text-end fw-bold">R$ ' + Number(params.total || 0).toFixed(2) + '</td></tr></tfoot>' +
            '</table></div>' +
            parcelasHtml +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve"' +
            (podeCriar ? '' : ' disabled') + '>criar venda</button>' +
            '</div></div>';
    }

    function previewGenerico(conversaId, tipo, params, titulo, labelAprovar) {
        var rows = '';
        for (var k in params) {
            if (!params.hasOwnProperty(k)) continue;
            if (k.indexOf('_') === 0) continue;  // skip _privados
            var v = params[k];
            if (v === null || v === undefined || v === '') continue;
            if (typeof v === 'object') v = JSON.stringify(v);
            rows += '<div class="copilot-preview-row"><span class="label">' + escape(k) + '</span>: <strong>' + escape(String(v)) + '</strong></div>';
        }
        return '<div class="copilot-preview" data-conversa="' + conversaId + '" data-tipo="' + tipo + '">' +
            '<div class="copilot-preview-header">' + escape(titulo) + '</div>' +
            rows +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve">' + escape(labelAprovar) + '</button>' +
            '</div></div>';
    }

    function renderResposta(d) {
        var html = texto2html(d.explicacao || '');
        if (d.resultado && d.resultado.texto) {
            html += '<div class="copilot-result-text">' + texto2html(d.resultado.texto) + '</div>';
        }
        if (d.resultado && d.resultado.erro) {
            html += '<div class="warn">erro: ' + escape(d.resultado.erro) + '</div>';
        }
        if (d.requer_aprovacao && d.params) {
            // Previews customizados (com campos editaveis)
            if (d.tipo === 'criar_pedido') html += previewCriarPedido(d.conversa_id, d.params);
            else if (d.tipo === 'receber_mp') html += previewReceberMP(d.conversa_id, d.params);
            else if (d.tipo === 'ajuste_estoque') html += previewAjusteEstoque(d.conversa_id, d.params);
            // Previews genericos pras tools novas (sem edicao inline ainda)
            else if (d.tipo === 'mudar_status_pedido') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'mudar status do pedido', 'aplicar');
            else if (d.tipo === 'criar_fornecedor') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'criar fornecedor', 'cadastrar');
            else if (d.tipo === 'marcar_ponto') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'marcar ponto', 'registrar');
            else if (d.tipo === 'criar_tarefa') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'criar tarefa', 'criar');
            else if (d.tipo === 'balanco_congelados') html += previewBalancoCongelados(d.conversa_id, d.params);
            else if (d.tipo === 'entrada_lote_loja') html += previewEntradaLoteLoja(d.conversa_id, d.params);
            else if (d.tipo === 'registrar_desperdicio') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'registrar desperdicio', 'registrar');
            else if (d.tipo === 'registrar_desperdicio_lote') html += previewRegistrarDesperdicioLote(d.conversa_id, d.params);
            else if (d.tipo === 'criar_venda_b2b') html += previewCriarVendaB2B(d.conversa_id, d.params);
            else if (d.tipo === 'criar_cliente_b2b') html += previewGenerico(d.conversa_id, d.tipo, d.params, 'cadastrar cliente B2B', 'cadastrar');
        }
        return html;
    }

    function fetchJson(url, opts) {
        return fetch(url, opts).then(function(r) {
            return r.text().then(function(body) {
                var data;
                try {
                    data = JSON.parse(body);
                } catch (e) {
                    // Backend devolveu HTML (erro 500/404 do Flask)
                    throw new Error('Servidor retornou HTTP ' + r.status + '. Provável erro do backend.');
                }
                // Se status nao-ok mas JSON valido, propaga como erro com mensagem clara
                if (!r.ok && data && data.erro) {
                    var err = new Error(data.erro);
                    err.detalhe = data.traceback || '';
                    throw err;
                }
                return data;
            });
        });
    }

    function enviar(prompt) {
        sendBtn.disabled = true;
        addMsg('user', escape(prompt));
        var loading = addMsg('bot', '<i class="bi bi-arrow-repeat"></i> pensando…');

        // Envia historico ANTES do prompt atual (Claude ja recebe o prompt como
        // 'messages[-1]' no backend)
        var historicoParaEnvio = copilotHistorico.slice();
        copilotHistorico.push({role: 'user', content: prompt});

        fetchJson('/copilot/api/interpretar', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': (typeof CSRF_TOKEN !== 'undefined' ? CSRF_TOKEN : '')},
            credentials: 'same-origin',
            body: JSON.stringify({prompt: prompt, historico: historicoParaEnvio}),
        }).then(function(d) {
            loading.remove();
            sendBtn.disabled = false;
            if (!d.ok) {
                addMsg('bot', '<span style="color:var(--cor-vermelho);">erro: ' + escape(d.erro || 'desconhecido') + '</span>');
                return;
            }
            // Compoe texto da resposta pra historico (sem o preview HTML)
            var respostaTexto = d.explicacao || '';
            if (d.resultado && d.resultado.texto) respostaTexto += '\n' + d.resultado.texto;
            if (respostaTexto.trim()) {
                copilotHistorico.push({role: 'assistant', content: respostaTexto.trim()});
            }
            // Limita a 20 mensagens (mantem ultimas)
            if (copilotHistorico.length > 20) copilotHistorico = copilotHistorico.slice(-20);

            addMsg('bot', renderResposta(d));
        }).catch(function(e) {
            loading.remove();
            sendBtn.disabled = false;
            // Remove a user msg que adicionei otimisticamente
            copilotHistorico.pop();
            addMsg('bot', '<span style="color:var(--cor-vermelho);">' + escape(String(e.message || e)) + '</span>');
        });
    }

    // Auto-resize do textarea conforme digita (max 5 linhas / 140px).
    function autosizeInput() {
        if (!input) return;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 140) + 'px';
    }
    if (input) {
        input.addEventListener('input', autosizeInput);
        // Enter envia, Shift+Enter pula linha (igual home).
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                if (form.requestSubmit) form.requestSubmit();
                else form.dispatchEvent(new Event('submit', {cancelable: true}));
            }
        });
    }

    form.addEventListener('submit', function(e) {
        e.preventDefault();
        var v = (input.value || '').trim();
        if (!v) return;
        input.value = '';
        autosizeInput();
        enviar(v);
    });

    // Coleta params do preview baseado no tipo
    function coletarParams(preview) {
        var tipo = preview.dataset.tipo;
        if (tipo === 'criar_pedido') {
            var itens = [];
            preview.querySelectorAll('.copilot-preview-item').forEach(function(div) {
                var sel = div.querySelector('.copilot-item-sel');
                var qtd = parseInt(div.querySelector('.qty-input').value, 10);
                if (!sel.value || qtd <= 0) return;
                var parts = sel.value.split(':');
                var nomeOpc = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '';
                var obsInp = div.querySelector('.copilot-item-obs');
                var obsVal = obsInp ? (obsInp.value || '').trim() : '';
                itens.push({
                    quantidade: qtd,
                    observacao: obsVal || null,
                    resolvido: {tipo: parts[0], id: parseInt(parts[1], 10), nome: nomeOpc},
                });
            });
            var lojaSel = preview.querySelector('.copilot-loja-sel');
            var loja_id = lojaSel && lojaSel.value ? parseInt(lojaSel.value, 10) : null;
            var dataInp = preview.querySelector('.copilot-data-input');
            var obsInp = preview.querySelector('.copilot-obs-input');
            return {
                _itens_editados: itens,
                loja_id: loja_id,
                data_entrega: dataInp ? dataInp.value : null,
                observacao: obsInp ? obsInp.value : null,
            };
        }
        if (tipo === 'receber_mp') {
            return {
                quantidade: parseFloat(preview.querySelector('.copilot-mp-qtd').value),
                preco_total: parseFloat(preview.querySelector('.copilot-mp-preco').value) || null,
                referencia: preview.querySelector('.copilot-mp-ref').value || null,
            };
        }
        if (tipo === 'ajuste_estoque') {
            return {
                tipo: preview.querySelector('.copilot-ajuste-tipo').value,
                quantidade: parseFloat(preview.querySelector('.copilot-ajuste-qtd').value),
                motivo: preview.querySelector('.copilot-ajuste-motivo').value,
            };
        }
        return {};
    }

    history.addEventListener('click', function(e) {
        var btnAprovar = e.target.closest('.copilot-approve');
        var btnCancelar = e.target.closest('.copilot-cancel');
        var btnRemoveItem = e.target.closest('.copilot-item-remove');
        var preview = e.target.closest('.copilot-preview');
        if (!preview) return;
        var conversaId = preview.dataset.conversa;

        if (btnRemoveItem) {
            var item = btnRemoveItem.closest('.copilot-preview-item');
            if (item) item.remove();
            return;
        }
        if (btnCancelar) {
            fetch('/copilot/api/' + conversaId + '/cancelar', {
                method: 'POST',
                headers: {'X-CSRFToken': (typeof CSRF_TOKEN !== 'undefined' ? CSRF_TOKEN : '')},
                credentials: 'same-origin',
            });
            preview.innerHTML = '<div class="copilot-preview-header" style="color:var(--text-muted);">— cancelado —</div>';
            return;
        }
        if (btnAprovar) {
            var params = coletarParams(preview);
            btnAprovar.disabled = true;
            btnAprovar.innerHTML = '<i class="bi bi-arrow-repeat"></i> aplicando…';
            fetchJson('/copilot/api/' + conversaId + '/aprovar', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': (typeof CSRF_TOKEN !== 'undefined' ? CSRF_TOKEN : '')},
                credentials: 'same-origin',
                body: JSON.stringify({params: params}),
            }).then(function(d) {
                if (d.ok) {
                    var link = d.url ? '<a href="' + d.url + '" target="_blank" class="btn-pill btn-pill-outline mt-2">abrir →</a>' : '';
                    preview.innerHTML = '<div class="copilot-preview-header" style="color:var(--cor-verde);">✓ feito</div>' + link;
                } else {
                    btnAprovar.disabled = false;
                    btnAprovar.innerHTML = 'aplicar';
                    alert('Erro: ' + (d.erro || 'desconhecido'));
                }
            }).catch(function(e) {
                btnAprovar.disabled = false;
                btnAprovar.innerHTML = 'aplicar';
                alert('Falha: ' + (e.message || e));
            });
        }
    });
})();
