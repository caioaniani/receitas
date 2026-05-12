/* Copilot UI — painel lateral com chat e preview de ações.
   Backend: POST /copilot/api/interpretar → preview → POST /api/<id>/aprovar */
(function() {
    var fab = document.getElementById('copilot-fab');
    var panel = document.getElementById('copilot-panel');
    var overlay = document.getElementById('copilot-overlay');
    var closeBtn = document.getElementById('copilot-close');
    var history = document.getElementById('copilot-history');
    var form = document.getElementById('copilot-form');
    var input = document.getElementById('copilot-input');
    var sendBtn = document.getElementById('copilot-send');
    if (!fab || !panel) return;  // não está logado / não admin

    function abrir() {
        panel.classList.remove('d-none');
        overlay.classList.remove('d-none');
        setTimeout(function() { input && input.focus(); }, 60);
    }
    function fechar() {
        panel.classList.add('d-none');
        overlay.classList.add('d-none');
    }
    fab.addEventListener('click', abrir);
    closeBtn.addEventListener('click', fechar);
    overlay.addEventListener('click', fechar);

    // Cmd/Ctrl+K abre; Esc fecha
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
        div.innerHTML = '<div class="bubble">' + html + '</div>' +
            '<div class="timestamp">' + nowHHMM() + '</div>';
        history.appendChild(div);
        history.scrollTop = history.scrollHeight;
        return div;
    }

    function renderPreviewPedido(conversaId, params) {
        var loja = params.loja_nome ? escape(params.loja_nome) : '<span class="warn">— escolha uma loja —</span>';
        var data = params.data_entrega || '—';
        var obs = params.observacao ? escape(params.observacao) : '';

        var itensHtml = '';
        (params.itens || []).forEach(function(item, idx) {
            var resolvido = item.resolvido;
            var matches = item.matches || [];
            var optHtml = '<option value="">— escolha —</option>';
            matches.forEach(function(m) {
                var sel = (resolvido && m.tipo === resolvido.tipo && m.id === resolvido.id) ? ' selected' : '';
                optHtml += '<option value="' + m.tipo + ':' + m.id + '"' + sel + '>' +
                    escape(m.nome) + ' (' + m.tipo + ', ' + m.match + ')</option>';
            });
            var warn = !resolvido ? '<div class="warn">não achei "' + escape(item.nome_original) + '" — corrija ou remova</div>' : '';
            itensHtml += '<div class="copilot-preview-item" data-idx="' + idx + '">' +
                '<span class="qty">' + item.quantidade + '×</span>' +
                '<select class="form-control form-control-sm copilot-item-sel">' + optHtml + '</select>' +
                '<button type="button" class="btn-circle copilot-item-remove" title="Remover" style="width:28px;height:28px;"><i class="bi bi-x"></i></button>' +
                '</div>' + warn;
        });

        return '<div class="copilot-preview" data-conversa="' + conversaId + '">' +
            '<div class="copilot-preview-header">criar pedido</div>' +
            '<div class="copilot-preview-row"><span class="label">loja</span><strong>' + loja + '</strong></div>' +
            '<div class="copilot-preview-row"><span class="label">data de entrega</span><strong>' + data + '</strong></div>' +
            (obs ? '<div class="copilot-preview-row"><span class="label">observação</span><span>' + obs + '</span></div>' : '') +
            '<div class="copilot-preview-row"><span class="label">itens</span></div>' +
            '<div class="copilot-items">' + itensHtml + '</div>' +
            '<div class="copilot-preview-actions">' +
            '<button type="button" class="btn-pill btn-pill-outline copilot-cancel">cancelar</button>' +
            '<button type="button" class="btn-pill btn-pill-primary copilot-approve">criar pedido</button>' +
            '</div></div>';
    }

    function enviar(prompt) {
        sendBtn.disabled = true;
        addMsg('user', escape(prompt));
        var loading = addMsg('bot', '<i class="bi bi-arrow-repeat"></i> pensando…');

        fetch('/copilot/api/interpretar', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN || ''},
            credentials: 'same-origin',
            body: JSON.stringify({prompt: prompt}),
        }).then(function(r) { return r.json(); }).then(function(d) {
            loading.remove();
            sendBtn.disabled = false;
            if (!d.ok) {
                addMsg('bot', '<span style="color:var(--cor-vermelho);">erro: ' + escape(d.erro || 'desconhecido') + '</span>');
                return;
            }
            var conteudo = escape(d.explicacao || '');
            if (d.tipo === 'criar_pedido' && d.params) {
                conteudo += renderPreviewPedido(d.conversa_id, d.params);
            }
            addMsg('bot', conteudo);
        }).catch(function(e) {
            loading.remove();
            sendBtn.disabled = false;
            addMsg('bot', '<span style="color:var(--cor-vermelho);">falha de rede: ' + escape(String(e)) + '</span>');
        });
    }

    form.addEventListener('submit', function(e) {
        e.preventDefault();
        var v = (input.value || '').trim();
        if (!v) return;
        input.value = '';
        enviar(v);
    });

    // Handler clicks dentro do history (preview actions)
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
                headers: {'X-CSRFToken': window.CSRF_TOKEN || ''},
                credentials: 'same-origin',
            });
            preview.innerHTML = '<div class="copilot-preview-header" style="color:var(--text-muted);">— cancelado —</div>';
            return;
        }

        if (btnAprovar) {
            // Coleta itens editados do preview
            var itens = [];
            preview.querySelectorAll('.copilot-preview-item').forEach(function(div) {
                var sel = div.querySelector('.copilot-item-sel');
                var qtdSpan = div.querySelector('.qty');
                var qtd = parseInt((qtdSpan.textContent || '').replace('×', '').trim(), 10);
                if (!sel.value || qtd <= 0) return;
                var parts = sel.value.split(':');
                var tipo = parts[0], id = parseInt(parts[1], 10);
                var nomeOpcao = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : '';
                itens.push({
                    quantidade: qtd,
                    resolvido: {tipo: tipo, id: id, nome: nomeOpcao},
                });
            });

            if (itens.length === 0) {
                alert('Adicione pelo menos 1 item antes de aprovar.');
                return;
            }

            // Reconstroi params editados (pega loja/data do preview original — não permitimos editar via UI ainda)
            // Pega da string visualmente — pra simplificar, vamos buscar de novo do server o estado original?
            // Mais simples: backend usa params salvos na conversa SE não passar params; vamos passar items editados + os campos read-only originais
            var lojaStr = preview.querySelector('.copilot-preview-row strong');
            var dataStr = preview.querySelectorAll('.copilot-preview-row strong')[1];

            btnAprovar.disabled = true;
            btnAprovar.innerHTML = '<i class="bi bi-arrow-repeat"></i> criando…';

            fetch('/copilot/api/' + conversaId + '/aprovar', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN || ''},
                credentials: 'same-origin',
                body: JSON.stringify({params: {
                    // backend lê loja_id/data_entrega/observacao da conversa salva se faltarem.
                    // Aqui só passamos os itens editados.
                    _itens_editados: itens,
                }}),
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.ok) {
                    preview.innerHTML = '<div class="copilot-preview-header" style="color:var(--cor-verde);">✓ pedido #' + d.pedido_id + ' criado</div>' +
                        '<div style="text-align:center; margin-top:8px;"><a href="/pedidos/' + d.pedido_id + '" target="_blank" class="btn-pill btn-pill-outline">ver pedido →</a></div>';
                } else {
                    btnAprovar.disabled = false;
                    btnAprovar.innerHTML = 'criar pedido';
                    alert('Erro: ' + (d.erro || 'desconhecido'));
                }
            }).catch(function(e) {
                btnAprovar.disabled = false;
                btnAprovar.innerHTML = 'criar pedido';
                alert('Falha de rede: ' + e);
            });
        }
    });
})();
