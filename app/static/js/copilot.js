/* Copilot UI — chat com preview de ações.
   Tools: criar_pedido, receber_mp, ajuste_estoque (write — preview obrigatório)
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
            itensHtml += '<div class="copilot-preview-item" data-idx="' + idx + '">' +
                '<input type="number" min="1" class="form-control form-control-sm qty-input" value="' + item.quantidade + '" style="max-width:60px; text-align:right;">' +
                '<span style="margin:0 4px;">×</span>' +
                '<select class="form-control form-control-sm copilot-item-sel">' + optHtml + '</select>' +
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

    function renderResposta(d) {
        var html = texto2html(d.explicacao || '');
        if (d.resultado && d.resultado.texto) {
            html += '<div class="copilot-result-text">' + texto2html(d.resultado.texto) + '</div>';
        }
        if (d.resultado && d.resultado.erro) {
            html += '<div class="warn">erro: ' + escape(d.resultado.erro) + '</div>';
        }
        if (d.requer_aprovacao && d.params) {
            if (d.tipo === 'criar_pedido') html += previewCriarPedido(d.conversa_id, d.params);
            else if (d.tipo === 'receber_mp') html += previewReceberMP(d.conversa_id, d.params);
            else if (d.tipo === 'ajuste_estoque') html += previewAjusteEstoque(d.conversa_id, d.params);
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

    form.addEventListener('submit', function(e) {
        e.preventDefault();
        var v = (input.value || '').trim();
        if (!v) return;
        input.value = '';
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
                itens.push({
                    quantidade: qtd,
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
