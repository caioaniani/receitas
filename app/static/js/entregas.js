(function() {
    var STORAGE_KEY = 'entregas_status';
    function isAdmin() {
        var t = document.getElementById('tab-atribuidos');
        return !!(t && t.dataset.isAdmin === '1');
    }
    var pedidos = [];
    var filtroAtual = 'todos';
    var calAno, calMes;
    var rotasUltimoResultado = null;
    var ROTA_CORES = ['#e6194b','#3cb44b','#4363d8','#f58231','#911eb4','#46f0f0','#f032e6','#bcf60c','#fabebe','#008080','#9a6324','#800000','#aaffc3','#808000','#000075','#808080'];
    var MAPS_MAX_PARADAS = 9;  // Google Maps deeplink: ate 9 waypoints + destino
    var rotasMapaLeaflet = null;
    var rotasMapaLayers = null;

    // ── localStorage status ──

    function getStatusStore() {
        try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch(e) { return {}; }
    }

    function saveStatusStore(store) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
    }

    function getStatus(dataStr, code) {
        return getStatusStore()[dataStr + ':' + code] || 'pendente';
    }

    function setStatus(dataStr, code, status) {
        var store = getStatusStore();
        store[dataStr + ':' + code] = status;
        saveStatusStore(store);
    }

    function limparAntigos() {
        var store = getStatusStore();
        var limite = new Date();
        limite.setDate(limite.getDate() - 7);
        var limiteStr = limite.toISOString().slice(0, 10);
        var changed = false;
        for (var key in store) {
            var datepart = key.split(':')[0];
            if (datepart < limiteStr) {
                delete store[key];
                changed = true;
            }
        }
        if (changed) saveStatusStore(store);
    }

    // ── Carregar pedidos ──

    window.carregarPedidos = function() {
        var dataStr = document.getElementById('data-entrega').value;
        var loading = document.getElementById('loading');
        var container = document.getElementById('pedidos-container');
        var msg = document.getElementById('msg-container');

        loading.classList.remove('d-none');
        container.innerHTML = '';
        msg.innerHTML = '';

        fetch('/entregas/api/pedidos?data=' + dataStr, {
            headers: {'X-CSRFToken': CSRF_TOKEN}
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            loading.classList.add('d-none');
            if (data.erro) {
                msg.innerHTML = '<div class="alert alert-warning">' + data.erro + '</div>';
                pedidos = [];
            } else {
                pedidos = data.pedidos || [];
            }
            if (pedidos.length === 0 && !data.erro) {
                var extra = data.total_janela ? ' (' + data.total_janela + ' pedidos na janela, nenhum com entrega nesta data)' : '';
                msg.innerHTML = '<div class="alert alert-info">Nenhum pedido para esta data.' + extra + '</div>';
            }
            renderPedidos();
        })
        .catch(function(err) {
            loading.classList.add('d-none');
            msg.innerHTML = '<div class="alert alert-danger">Erro ao carregar pedidos.</div>';
            pedidos = [];
            renderPedidos();
        });
    };

    // ── Render ──

    function renderPedidos() {
        var container = document.getElementById('pedidos-container');
        var dataStr = document.getElementById('data-entrega').value;
        var busca = (document.getElementById('busca-pedido').value || '').toLowerCase();
        var counts = {todos: 0, pendente: 0, separado: 0, entregue: 0};
        var totalDia = 0;

        var html = '';
        for (var i = 0; i < pedidos.length; i++) {
            var p = pedidos[i];
            var status = getStatus(dataStr, p.code);
            counts.todos++;
            counts[status] = (counts[status] || 0) + 1;
            totalDia += Number(p.total) || 0;

            if (filtroAtual !== 'todos' && status !== filtroAtual) continue;
            var buscaTexto = (p.destinatario + ' ' + p.comprador + ' ' + p.code).toLowerCase();
            if (busca && buscaTexto.indexOf(busca) === -1) continue;

            var statusBadge = '';
            if (status === 'pendente') statusBadge = '<span class="status-badge status-pendente">Pendente</span>';
            else if (status === 'separado') statusBadge = '<span class="status-badge status-separado">Separado</span>';
            else if (status === 'entregue') statusBadge = '<span class="status-badge status-entregue">Entregue</span>';

            var itensHtml = '';
            for (var j = 0; j < p.itens.length; j++) {
                var it = p.itens[j];
                itensHtml += '<span class="me-3">' + it.quantidade + 'x ' + escapeHtml(it.nome) + '</span>';
            }

            var cartinhaClass = (p.cartinha || p.tem_customizacao) ? 'has-text' : '';
            var cartinhaBadge = p.tem_customizacao ? ' <span class="badge bg-warning text-dark" style="font-size:10px;"><i class="bi bi-envelope-heart"></i> Cartinha</span>' : '';

            var compradorLine = '';
            if (p.comprador && p.comprador !== p.destinatario) {
                compradorLine = '<div class="text-muted" style="font-size:11px;"><i class="bi bi-person"></i> Comprador: ' + escapeHtml(p.comprador) + '</div>';
            }

            html += '<div class="card mb-2">' +
                '<div class="card-body py-2 px-3">' +
                    '<div class="d-flex justify-content-between align-items-start">' +
                        '<div>' +
                            '<a href="https://www.padariaartesanalonline.com.br/admin/pedido?id=' + encodeURIComponent(p.code) + '" target="_blank" rel="noopener" class="text-decoration-none" title="Abrir no VNDA" style="color: var(--accent);">' +
                                '<strong style="font-size:13px; color: var(--accent);">[' + escapeHtml(p.code) + '] <i class="bi bi-box-arrow-up-right" style="font-size:11px;"></i></strong>' +
                            '</a> ' +
                            '<span class="fw-semibold"><i class="bi bi-person-fill"></i> ' + escapeHtml(p.destinatario) + '</span>' +
                            cartinhaBadge +
                            (p.data_override ? ' <span class="badge bg-warning text-dark" title="Data alterada — original: ' + escapeHtml(p.data_entrega_original_fmt || '') + (p.override_motivo ? ' · Motivo: ' + escapeHtml(p.override_motivo) : '') + (p.override_autor ? ' · Por: ' + escapeHtml(p.override_autor) : '') + '" style="font-size:10px;"><i class="bi bi-pencil-square"></i> Data alterada</span>' : '') +
                            (p.driver ? ' <span class="badge text-white" style="font-size:10px;background:' + (p.driver.cor || '#3cb44b') + ';" title="Driver atribuído"><i class="bi bi-person-badge"></i> ' + escapeHtml(p.driver.nome) + '</span>' : '') +
                        '</div>' +
                        '<div class="d-flex align-items-center gap-2">' +
                            (p.periodo ? '<span class="badge bg-light text-dark" style="font-size:11px;"><i class="bi bi-clock"></i> ' + escapeHtml(p.periodo) + '</span>' : '') +
                            statusBadge +
                        '</div>' +
                    '</div>' +
                    compradorLine +
                    '<div class="text-muted" style="font-size:12px;">' +
                        (p.endereco ? '<i class="bi bi-geo-alt"></i> ' + escapeHtml(p.endereco) : '') +
                        (p.telefone ? ' &nbsp;<i class="bi bi-telephone"></i> ' + escapeHtml(p.telefone) : '') +
                    '</div>' +
                    '<div class="mt-1" style="font-size:13px;">' + itensHtml + '</div>' +
                    '<div class="d-flex justify-content-between align-items-center mt-1">' +
                        '<strong>R$ ' + formatMoney(p.total) + '</strong>' +
                        '<div class="d-print-none d-flex gap-1">' +
                            (status !== 'separado' ? '<button class="btn btn-sm btn-outline-info" onclick="marcarStatus(\'' + p.code + '\',\'separado\')"><i class="bi bi-check"></i> Separar</button>' : '') +
                            (status !== 'entregue' ? '<button class="btn btn-sm btn-outline-success" onclick="marcarStatus(\'' + p.code + '\',\'entregue\')"><i class="bi bi-check-all"></i> Entregar</button>' : '') +
                            (status !== 'pendente' ? '<button class="btn btn-sm btn-outline-secondary" onclick="marcarStatus(\'' + p.code + '\',\'pendente\')"><i class="bi bi-arrow-counterclockwise"></i></button>' : '') +
                            '<button class="btn btn-sm btn-outline-warning" onclick="editarData(\'' + p.code + '\',\'' + (p.data_entrega || '') + '\',' + (p.data_override ? 'true' : 'false') + ')" title="Mudar data de entrega"><i class="bi bi-calendar-event"></i></button>' +
                            (p.tem_customizacao ? '<button class="btn btn-sm btn-outline-warning" onclick="toggleCartinha(\'' + p.code + '\')"><i class="bi bi-envelope-heart"></i></button>' : '') +
                            '<a class="btn btn-sm btn-outline-dark" href="https://www.padariaartesanalonline.com.br/admin/pedido?id=' + encodeURIComponent(p.code) + '" target="_blank" rel="noopener" title="Abrir pedido no VNDA"><i class="bi bi-box-arrow-up-right"></i> VNDA</a>' +
                        '</div>' +
                    '</div>' +
                    '<div class="cartinha-area ' + cartinhaClass + ' mt-2 d-none" id="cartinha-' + p.code + '">' +
                        (p.cartinha_origem === 'vnda' ? '<small class="text-muted d-block mb-1"><i class="bi bi-magic"></i> Cartinha automática do VNDA — edite se quiser sobrescrever</small>' : '') +
                        '<textarea class="form-control form-control-sm mb-1" rows="3" id="cartinha-txt-' + p.code + '" placeholder="Cole a cartinha do admin Vnda...">' + escapeHtml(p.cartinha || '') + '</textarea>' +
                        '<button class="btn btn-sm btn-warning d-print-none" onclick="salvarCartinha(\'' + p.code + '\')"><i class="bi bi-save"></i> Salvar</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        }

        container.innerHTML = html;

        document.getElementById('cnt-todos').textContent = counts.todos;
        document.getElementById('cnt-pendente').textContent = counts.pendente;
        document.getElementById('cnt-separado').textContent = counts.separado;
        document.getElementById('cnt-entregue').textContent = counts.entregue;
        document.getElementById('total-dia-valor').textContent = formatMoney(totalDia);
    }

    // ── Acoes ──

    window.marcarStatus = function(code, status) {
        var dataStr = document.getElementById('data-entrega').value;
        setStatus(dataStr, code, status);
        renderPedidos();
    };

    window.toggleCartinha = function(code) {
        var el = document.getElementById('cartinha-' + code);
        if (el) el.classList.toggle('d-none');
    };

    window.salvarCartinha = function(code) {
        var texto = document.getElementById('cartinha-txt-' + code).value;
        fetch('/entregas/cartinha/' + code, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': CSRF_TOKEN
            },
            body: JSON.stringify({texto: texto})
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                for (var i = 0; i < pedidos.length; i++) {
                    if (pedidos[i].code === code) pedidos[i].cartinha = texto;
                }
                var btn = document.querySelector('#cartinha-' + code + ' .btn');
                if (btn) { btn.textContent = ' Salvo!'; setTimeout(function(){ btn.innerHTML = '<i class="bi bi-save"></i> Salvar'; }, 1500); }
            }
        });
    };

    window.editarData = function(code, dataAtual, jaTemOverride) {
        var pedido = null;
        for (var i = 0; i < pedidos.length; i++) {
            if (pedidos[i].code === code) { pedido = pedidos[i]; break; }
        }
        if (!pedido) return;

        document.getElementById('md-code').value = code;
        document.getElementById('md-info-code').textContent = code;
        document.getElementById('md-info-original').textContent =
            pedido.data_entrega_original_fmt || pedido.data_entrega_fmt || '—';
        document.getElementById('md-data').value = dataAtual || '';
        document.getElementById('md-motivo').value = pedido.override_motivo || '';

        var btnRemover = document.getElementById('md-remover');
        var hist = document.getElementById('md-historico');
        if (jaTemOverride) {
            btnRemover.classList.remove('d-none');
            var quando = pedido.override_em ? new Date(pedido.override_em).toLocaleString('pt-BR') : '—';
            hist.classList.remove('d-none');
            hist.innerHTML = '<i class="bi bi-clock-history"></i> Override criado por <strong>' +
                escapeHtml(pedido.override_autor || '—') + '</strong> em ' + escapeHtml(quando) +
                (pedido.override_motivo ? '<br><i class="bi bi-chat-text"></i> ' + escapeHtml(pedido.override_motivo) : '');
        } else {
            btnRemover.classList.add('d-none');
            hist.classList.add('d-none');
            hist.innerHTML = '';
        }

        var modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-alterar-data'));
        modal.show();
    };

    document.addEventListener('DOMContentLoaded', function() {
        var btnSalvar = document.getElementById('md-salvar');
        var btnRemover = document.getElementById('md-remover');
        if (!btnSalvar) return;

        btnSalvar.addEventListener('click', function() {
            var code = document.getElementById('md-code').value;
            var nova = document.getElementById('md-data').value;
            var motivo = document.getElementById('md-motivo').value.trim();
            if (!nova) { alert('Selecione uma data.'); return; }

            fetch('/entregas/data/' + code, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                body: JSON.stringify({data: nova, motivo: motivo})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    bootstrap.Modal.getInstance(document.getElementById('modal-alterar-data')).hide();
                    carregarPedidos();
                    if (typeof window.opRecarregar === 'function') window.opRecarregar();
                } else {
                    alert('Erro: ' + (d.erro || 'desconhecido'));
                }
            });
        });

        btnRemover.addEventListener('click', function() {
            var code = document.getElementById('md-code').value;
            if (!confirm('Remover o override e voltar à data original do VNDA?')) return;
            fetch('/entregas/data/' + code, {
                method: 'DELETE',
                headers: {'X-CSRFToken': CSRF_TOKEN}
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    bootstrap.Modal.getInstance(document.getElementById('modal-alterar-data')).hide();
                    carregarPedidos();
                    if (typeof window.opRecarregar === 'function') window.opRecarregar();
                }
            });
        });
    });

    window.imprimirPedidos = function() {
        window.print();
    };

    // ── Filtros ──

    document.querySelectorAll('[data-filtro]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            document.querySelectorAll('[data-filtro]').forEach(function(b) { b.classList.remove('active'); });
            btn.classList.add('active');
            filtroAtual = btn.getAttribute('data-filtro');
            renderPedidos();
        });
    });

    document.getElementById('busca-pedido').addEventListener('input', function() {
        renderPedidos();
    });

    document.getElementById('data-entrega').addEventListener('change', function() {
        carregarPedidos();
    });

    // ── Calendario ──

    var mesesNome = ['Janeiro','Fevereiro','Marco','Abril','Maio','Junho','Julho','Agosto','Setembro','Outubro','Novembro','Dezembro'];
    var diasSemana = ['Seg','Ter','Qua','Qui','Sex','Sab','Dom'];

    function initCalendario() {
        var hoje = new Date();
        calAno = hoje.getFullYear();
        calMes = hoje.getMonth() + 1;
    }

    window.calMesAnterior = function() {
        calMes--;
        if (calMes < 1) { calMes = 12; calAno--; }
        carregarCalendario();
    };

    window.calMesProximo = function() {
        calMes++;
        if (calMes > 12) { calMes = 1; calAno++; }
        carregarCalendario();
    };

    function carregarCalendario() {
        var mesStr = calAno + '-' + String(calMes).padStart(2, '0');
        document.getElementById('cal-titulo').textContent = mesesNome[calMes - 1] + ' ' + calAno;

        fetch('/entregas/api/calendario?mes=' + mesStr, {
            headers: {'X-CSRFToken': CSRF_TOKEN}
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            renderCalendario(data.dias || {});
        });
    }

    function renderCalendario(dias) {
        var grid = document.getElementById('calendario-grid');
        var html = '<div class="cal-grid">';

        for (var d = 0; d < 7; d++) {
            html += '<div class="cal-header">' + diasSemana[d] + '</div>';
        }

        var primeiro = new Date(calAno, calMes - 1, 1);
        var ultimoDia = new Date(calAno, calMes, 0).getDate();
        var diaSemana = primeiro.getDay();
        diaSemana = diaSemana === 0 ? 6 : diaSemana - 1;

        for (var e = 0; e < diaSemana; e++) {
            html += '<div class="cal-day cal-day-empty"></div>';
        }

        for (var dia = 1; dia <= ultimoDia; dia++) {
            var dateStr = calAno + '-' + String(calMes).padStart(2, '0') + '-' + String(dia).padStart(2, '0');
            var count = dias[dateStr] || 0;
            html += '<div class="cal-day" onclick="selecionarDia(\'' + dateStr + '\')">';
            html += '<div class="cal-day-num">' + dia + '</div>';
            if (count > 0) {
                html += '<span class="badge bg-primary" style="font-size:11px;">' + count + ' pedido' + (count > 1 ? 's' : '') + '</span>';
            }
            html += '</div>';
        }

        html += '</div>';
        grid.innerHTML = html;
    }

    window.selecionarDia = function(dateStr) {
        document.getElementById('data-entrega').value = dateStr;
        var tabBtn = document.querySelector('[data-bs-target="#tab-pedidos"]');
        if (tabBtn) new bootstrap.Tab(tabBtn).show();
        carregarPedidos();
    };

    document.getElementById('btn-tab-cal').addEventListener('shown.bs.tab', function() {
        if (!calAno) initCalendario();
        carregarCalendario();
    });

    // ── Helpers ──

    function escapeHtml(str) {
        if (!str) return '';
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function formatMoney(val) {
        if (!val && val !== 0) return '0,00';
        return Number(val).toFixed(2).replace('.', ',');
    }

    // ──────────────────────────────────────────
    // ── Rotas (drivers nominais + Google Maps) ──
    // ──────────────────────────────────────────

    function getJanelas(prefix) {
        return Array.from(document.querySelectorAll('#' + prefix + '-janelas-cb input[type=checkbox]:checked'))
            .map(function(cb) { return cb.value; });
    }

    function renderCheckboxesJanela(prefix, periodos, atuais) {
        var area = document.getElementById(prefix + '-janelas-area');
        var cont = document.getElementById(prefix + '-janelas-cb');
        if (!area || !cont) return;
        if (!periodos || periodos.length === 0) {
            area.style.display = 'none';
            cont.innerHTML = '';
            return;
        }
        area.style = '';
        var html = '';
        for (var i = 0; i < periodos.length; i++) {
            var per = periodos[i];
            var checked = atuais.indexOf(per) !== -1 ? 'checked' : '';
            html += '<label class="form-check form-check-inline mb-0">' +
                '<input class="form-check-input cb-janela-' + prefix + '" type="checkbox" value="' + escapeHtml(per) + '" ' + checked + '>' +
                '<span class="form-check-label small">' + escapeHtml(per) + '</span>' +
            '</label>';
        }
        cont.innerHTML = html;
    }

    function gerarRotas(salvar) {
        var data = document.getElementById('rotas-data').value;
        var janelas = getJanelas('rotas');
        var loading = document.getElementById('rotas-loading');
        var msg = document.getElementById('rotas-msg');
        var container = document.getElementById('rotas-container');
        var sc = document.getElementById('rotas-sem-cep');

        loading.classList.remove('d-none');
        msg.innerHTML = '';
        container.innerHTML = '';
        sc.innerHTML = '';

        var url = '/entregas/api/rotas?data=' + encodeURIComponent(data) +
                  janelas.map(function(j) { return '&janela=' + encodeURIComponent(j); }).join('');

        fetch(url, {credentials: 'same-origin'})
            .then(function(r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function(d) {
                loading.classList.add('d-none');
                if (d.erro) {
                    msg.innerHTML = '<div class="alert alert-warning py-2 small">' + escapeHtml(d.erro) + '</div>';
                    return;
                }
                rotasUltimoResultado = d;
                renderCheckboxesJanela('rotas', d.periodos_disponiveis || [], d.janelas || []);
                if (!d.drivers_disponiveis || d.drivers_disponiveis.length === 0) {
                    msg.innerHTML = '<div class="alert alert-info py-2 small">' +
                        '<i class="bi bi-info-circle"></i> Nenhum driver cadastrado. Clique em <strong>Drivers</strong> pra adicionar.' +
                        '</div>';
                    return;
                }
                try {
                    renderRotas(d);
                } catch (renderErr) {
                    msg.innerHTML = '<div class="alert alert-danger py-2 small">' +
                        '<strong>Erro ao renderizar rotas:</strong> ' +
                        escapeHtml(renderErr.message || String(renderErr)) +
                        '<br><small style="font-family:monospace;">' + escapeHtml((renderErr.stack || '').slice(0, 500)) + '</small>' +
                        '</div>';
                    if (window.console) console.error('Erro renderRotas:', renderErr);
                }
                if (salvar) salvarAtribuicoesGeradas(d, msg);
            })
            .catch(function(err) {
                loading.classList.add('d-none');
                msg.innerHTML = '<div class="alert alert-danger py-2 small">' +
                    '<strong>Falha ao buscar rotas:</strong> ' +
                    escapeHtml(err && err.message ? err.message : String(err)) +
                    '</div>';
                if (window.console) console.error('Erro fetch rotas:', err);
            });
    }

    // Constroi 1 ou mais URLs do Google Maps com waypoints (ate 9 paradas + destino).
    // Origem: matriz (se configurada) ou primeira parada do lote.
    function gerarLinksMaps(matriz, paradas) {
        if (!paradas || !paradas.length) return [];
        var links = [];
        // Cada chunk: ate MAPS_MAX_PARADAS+1 paradas (matriz + 9 waypoints + destino seria 11, mas Google
        // parece aceitar 10 efetivos. Vou cortar em 10 paradas por link, ultima vira destination, resto waypoints).
        var chunkSize = 10;
        for (var i = 0; i < paradas.length; i += chunkSize) {
            var chunk = paradas.slice(i, i + chunkSize);
            var origem = matriz || chunk[0].endereco;
            var destino = chunk[chunk.length - 1].endereco;
            var waypoints = chunk.slice(0, -1).map(function(p) {
                return encodeURIComponent(p.endereco);
            }).join('|');
            // Se matriz nao definida, primeira ja foi origem — pula
            if (!matriz) {
                waypoints = chunk.slice(1, -1).map(function(p) {
                    return encodeURIComponent(p.endereco);
                }).join('|');
            }
            var url = 'https://www.google.com/maps/dir/?api=1' +
                '&origin=' + encodeURIComponent(origem) +
                '&destination=' + encodeURIComponent(destino) +
                (waypoints ? '&waypoints=' + waypoints : '') +
                '&travelmode=driving';
            links.push({
                url: url,
                de: i + 1,
                ate: Math.min(i + chunkSize, paradas.length),
            });
        }
        return links;
    }

    function renderRotas(d) {
        var container = document.getElementById('rotas-container');
        var sc = document.getElementById('rotas-sem-cep');
        var matriz = (d.origem_endereco || '').trim();

        if (!matriz) {
            var msg = document.getElementById('rotas-msg');
            msg.innerHTML = '<div class="alert alert-warning py-2 small mb-3">' +
                '<i class="bi bi-exclamation-triangle"></i> <strong>ROTA_ORIGEM_ENDERECO não configurado.</strong> ' +
                'O link do Maps vai sair da primeira parada em vez da matriz.' +
                '</div>';
        }

        // Limpa pinos do mapa de buscas anteriores
        var mapaEl = document.getElementById('rotas-mapa');
        if (rotasMapaLayers) rotasMapaLayers.clearLayers();

        if (!d.rotas || d.rotas.length === 0) {
            if (mapaEl) mapaEl.style.display = 'none';
            var qtdSemCep = (d.sem_cep || []).length;
            var qtdTotal = d.total_pedidos || 0;
            var diag = '';
            if (qtdTotal > 0 && qtdSemCep === qtdTotal) {
                diag = ' Nenhum dos ' + qtdTotal + ' pedidos foi geocodado — pode ser cota do Google Maps esgotada ou GOOGLE_MAPS_API_KEY ausente.';
            } else if (qtdTotal > 0) {
                diag = ' (' + qtdTotal + ' pedidos no VNDA, ' + qtdSemCep + ' sem CEP/geocode)';
            }
            container.innerHTML = '<div class="alert alert-info py-2 small">Nenhum pedido para esses filtros.' + escapeHtml(diag) + '</div>';
            return;
        }

        var html = '<div class="row g-2">';
        for (var i = 0; i < d.rotas.length; i++) {
            var r = d.rotas[i];
            var drv = r.driver;
            var cor = drv.cor || ROTA_CORES[i % ROTA_CORES.length];

            var detalhe = r.qtd_paradas + ' paradas';
            if (r.km != null) detalhe += ' · ' + r.km + ' km';
            if (r.minutos != null) detalhe += ' · ' + Math.floor(r.minutos / 60) + 'h' + (r.minutos % 60).toString().padStart(2, '0') + 'min';
            html += '<div class="col-md-6 col-lg-4 mb-2">' +
                    '<div class="card">' +
                    '<div class="card-header py-2 d-flex justify-content-between align-items-center" style="border-left:4px solid ' + cor + ';">' +
                        '<strong style="color:' + cor + ';"><i class="bi bi-person-badge"></i> ' + escapeHtml(drv.nome) + '</strong>' +
                        '<span class="text-muted small qtd-paradas">' + detalhe + '</span>' +
                    '</div>' +
                    '<div class="card-body py-2 px-3 border-bottom maps-area" data-cor="' + cor + '"></div>' +
                    '<div class="card-body p-0">' +
                        '<ol class="list-group list-group-numbered list-group-flush rotas-paradas" ' +
                          'data-driver-id="' + drv.id + '" data-driver-nome="' + escapeHtml(drv.nome) + '">';
            for (var j = 0; j < r.paradas.length; j++) {
                var p = r.paradas[j];
                html += '<li class="list-group-item d-flex justify-content-between align-items-start" style="cursor:grab;" data-code="' + p.code + '" data-endereco="' + escapeHtml(p.endereco || '') + '">' +
                        '<div class="ms-1" style="flex:1; min-width:0;">' +
                        '<div class="fw-semibold small">' + escapeHtml(p.destinatario || '—') + '</div>' +
                        '<div class="text-muted" style="font-size:11px;">' + escapeHtml(p.endereco || '') + '</div>' +
                        (p.periodo ? '<span class="badge bg-light text-dark" style="font-size:10px;"><i class="bi bi-clock"></i> ' + escapeHtml(p.periodo) + '</span>' : '') +
                        '</div>' +
                        '<small class="text-muted">[' + escapeHtml(p.code) + ']</small>' +
                        '</li>';
            }
            html += '</ol></div></div></div>';
        }
        html += '</div>';
        container.innerHTML = html;

        // Inicializa cada coluna: gera links Maps (preserva detalhe km/min) e ativa Sortable
        document.querySelectorAll('.rotas-paradas').forEach(function(ol) {
            atualizarMapsDaColuna(ol, false);
            new Sortable(ol, {
                group: 'rotas',
                animation: 150,
                onEnd: function(evt) {
                    // Apos drag: atualiza contagem (perde km/min — precisa Gerar de novo)
                    if (evt.from) atualizarMapsDaColuna(evt.from, true);
                    if (evt.to && evt.to !== evt.from) atualizarMapsDaColuna(evt.to, true);
                    persistirAtribuicoes();
                }
            });
        });

        // Pedidos sem CEP
        if (d.sem_cep && d.sem_cep.length > 0) {
            var s = '<div class="alert alert-warning py-2"><strong><i class="bi bi-geo-alt-fill"></i> ' + d.sem_cep.length + ' pedido(s) sem CEP — não distribuídos automaticamente:</strong><ul class="mb-0 small">';
            for (var k = 0; k < d.sem_cep.length; k++) {
                var x = d.sem_cep[k];
                s += '<li>[' + escapeHtml(x.code) + '] ' + escapeHtml(x.destinatario || '—') + ' — ' + escapeHtml(x.endereco || '(sem endereço)') + '</li>';
            }
            s += '</ul></div>';
            sc.innerHTML = s;
        }

        // Mapa visual com pinos coloridos por driver
        renderMapa(d);
    }

    // ── Mapa Leaflet com pinos coloridos por driver ──

    function inicializarMapaLeaflet() {
        if (rotasMapaLeaflet) return;
        if (typeof L === 'undefined') return;
        rotasMapaLeaflet = L.map('rotas-mapa', {scrollWheelZoom: false}).setView([-23.5505, -46.6333], 11);
        // Tiles via proxy do nosso proprio backend. CDNs externas (unpkg,
        // openstreetmap.org direto, cartocdn) estao bloqueadas no ambiente
        // do usuario.
        L.tileLayer('/entregas/tiles/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap',
            maxZoom: 19,
        }).addTo(rotasMapaLeaflet);
        rotasMapaLayers = L.layerGroup().addTo(rotasMapaLeaflet);
    }

    function renderMapa(d) {
        var mapaEl = document.getElementById('rotas-mapa');
        if (!mapaEl) return;
        if (typeof L === 'undefined') {
            mapaEl.style.display = '';
            mapaEl.style.height = 'auto';
            mapaEl.innerHTML = '<div class="alert alert-warning py-2 small mb-0">' +
                '<i class="bi bi-exclamation-triangle"></i> ' +
                '<strong>Leaflet não carregou.</strong> ' +
                'Verifique sua conexão ou se algum bloqueador está ativo (unpkg.com).' +
                '</div>';
            return;
        }
        // So mostra mapa se tem rotas com coords
        var temCoords = (d.rotas || []).some(function(r) {
            return r.paradas.some(function(p) { return p.lat != null && p.lng != null; });
        });
        if (!temCoords) {
            mapaEl.style.display = 'none';
            return;
        }
        mapaEl.style.display = '';
        mapaEl.style.height = '380px';
        inicializarMapaLeaflet();
        rotasMapaLayers.clearLayers();

        var bounds = [];
        // Marcador da matriz
        if (d.origem && d.origem.lat && d.origem.lng) {
            L.marker([d.origem.lat, d.origem.lng], {
                icon: L.divIcon({
                    className: 'matriz-icon',
                    html: '<div style="background:#000;color:#fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;border:2px solid #fff;box-shadow:0 2px 4px rgba(0,0,0,0.3);">🏠</div>',
                    iconSize: [28, 28],
                }),
            }).addTo(rotasMapaLayers).bindPopup('<b>Loja matriz</b>');
            bounds.push([d.origem.lat, d.origem.lng]);
        }

        // Pinos + linhas por driver
        for (var i = 0; i < d.rotas.length; i++) {
            var r = d.rotas[i];
            var cor = (r.driver && r.driver.cor) || ROTA_CORES[i % ROTA_CORES.length];
            var coords_linha = [];
            if (d.origem) coords_linha.push([d.origem.lat, d.origem.lng]);
            for (var j = 0; j < r.paradas.length; j++) {
                var p = r.paradas[j];
                if (p.lat == null || p.lng == null) continue;
                coords_linha.push([p.lat, p.lng]);
                bounds.push([p.lat, p.lng]);
                L.marker([p.lat, p.lng], {
                    icon: L.divIcon({
                        className: 'parada-icon',
                        html: '<div style="background:' + cor + ';color:#fff;border-radius:50%;width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:bold;border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,0.3);">' + p.ordem + '</div>',
                        iconSize: [24, 24],
                    }),
                }).addTo(rotasMapaLayers).bindPopup('<b>' + (r.driver.nome || '') + ' · #' + p.ordem + '</b><br>' + escapeHtml(p.destinatario || '') + '<br><small>' + escapeHtml(p.endereco || '') + '</small>');
            }
            if (d.origem) coords_linha.push([d.origem.lat, d.origem.lng]);
            if (coords_linha.length > 1) {
                L.polyline(coords_linha, {color: cor, weight: 2, opacity: 0.6, dashArray: '5,8'}).addTo(rotasMapaLayers);
            }
        }

        if (bounds.length > 0) {
            rotasMapaLeaflet.fitBounds(bounds, {padding: [40, 40]});
        }
        setTimeout(function() {
            if (rotasMapaLeaflet) rotasMapaLeaflet.invalidateSize();
        }, 200);
    }

    function atualizarMapsDaColuna(ol, atualizarContagem) {
        var card = ol.closest('.card');
        var paradas = [];
        ol.querySelectorAll('li').forEach(function(li) {
            paradas.push({
                code: li.dataset.code,
                endereco: li.dataset.endereco || '',
            });
        });
        // So atualiza contagem se foi pedido (apos drag) — preserva o "X paradas · Y km · Zmin"
        // que renderRotas montou inicialmente.
        if (atualizarContagem) {
            var hdr = card.querySelector('.qtd-paradas');
            if (hdr) hdr.textContent = paradas.length + ' paradas';
        }

        var matriz = (rotasUltimoResultado && rotasUltimoResultado.origem_endereco) || '';
        var btnArea = card.querySelector('.maps-area');
        if (!btnArea) return;
        var links = gerarLinksMaps(matriz, paradas);
        if (links.length === 0) {
            btnArea.innerHTML = '<small class="text-muted">Sem paradas</small>';
        } else if (links.length === 1) {
            btnArea.innerHTML = '<a href="' + links[0].url + '" target="_blank" rel="noopener" class="btn btn-sm btn-success">' +
                                '<i class="bi bi-geo-alt-fill"></i> Abrir no Google Maps</a>';
        } else {
            var bm = '<div class="dropdown">' +
                '<button class="btn btn-sm btn-success dropdown-toggle" data-bs-toggle="dropdown">' +
                '<i class="bi bi-geo-alt-fill"></i> Maps (' + links.length + ' partes)</button>' +
                '<ul class="dropdown-menu">';
            for (var li2 = 0; li2 < links.length; li2++) {
                bm += '<li><a class="dropdown-item" href="' + links[li2].url + '" target="_blank" rel="noopener">' +
                      'Parte ' + (li2 + 1) + ' (paradas ' + links[li2].de + '–' + links[li2].ate + ')</a></li>';
            }
            bm += '</ul></div>';
            btnArea.innerHTML = bm;
        }
    }

    function persistirAtribuicoes() {
        var data = (rotasUltimoResultado && rotasUltimoResultado.data) || '';
        var items = [];
        // Tambem atualiza estado em memoria pra remapear o mapa em tempo real
        var paradasPorDriver = {};
        document.querySelectorAll('.rotas-paradas').forEach(function(ol) {
            var driverId = parseInt(ol.dataset.driverId, 10);
            paradasPorDriver[driverId] = [];
            ol.querySelectorAll('li').forEach(function(li, idx) {
                items.push({
                    code: li.dataset.code,
                    driver_id: driverId,
                    ordem: idx,
                    data_entrega: data,
                });
                paradasPorDriver[driverId].push(li.dataset.code);
            });
        });
        // Atualiza paradas no rotasUltimoResultado (mantendo lat/lng) e re-renderiza mapa
        if (rotasUltimoResultado && rotasUltimoResultado.rotas) {
            // Indexa todas as paradas existentes por code
            var paradasIndex = {};
            rotasUltimoResultado.rotas.forEach(function(r) {
                r.paradas.forEach(function(p) { paradasIndex[p.code] = p; });
            });
            // Reconstroi rotas
            rotasUltimoResultado.rotas.forEach(function(r) {
                var ids = paradasPorDriver[r.driver.id] || [];
                r.paradas = ids.map(function(code, idx) {
                    var p = paradasIndex[code];
                    if (!p) return null;
                    return Object.assign({}, p, {ordem: idx + 1});
                }).filter(Boolean);
                r.qtd_paradas = r.paradas.length;
            });
            renderMapa(rotasUltimoResultado);
        }
        if (items.length === 0) return;
        fetch('/entregas/api/atribuicao/lote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
            body: JSON.stringify({items: items}),
        }).then(function(r) { return r.json(); })
          .then(function(d) {
              if (!d.ok) {
                  console.error('Erro ao salvar atribuicoes:', d.erro);
              }
          });
    }

    // ── Modal: gerenciar drivers ──
    function carregarDrivers() {
        var lista = document.getElementById('drv-lista');
        if (!lista) return;
        lista.innerHTML = '<div class="text-muted small">Carregando…</div>';
        fetch('/entregas/api/drivers?inativos=1', {credentials: 'same-origin'})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d.drivers || d.drivers.length === 0) {
                    lista.innerHTML = '<div class="text-muted small text-center py-3">Nenhum driver cadastrado ainda.</div>';
                    return;
                }
                var html = '<div class="table-responsive"><table class="table table-sm align-middle"><thead><tr>' +
                    '<th style="width:60px;">Cor</th><th>Nome</th><th>Telefone</th><th style="width:90px;">PIN</th><th style="width:70px;">Ativo</th><th>Acesso driver</th><th></th>' +
                    '</tr></thead><tbody>';
                for (var i = 0; i < d.drivers.length; i++) {
                    var dr = d.drivers[i];
                    var linkDriver = dr.token ? (window.location.origin + '/driver/' + dr.token) : '';
                    html += '<tr data-id="' + dr.id + '">' +
                        '<td><input type="color" class="form-control form-control-color form-control-sm drv-cor-edit" value="' + (dr.cor || '#666666') + '" style="width:36px;height:24px;"></td>' +
                        '<td><input type="text" class="form-control form-control-sm drv-nome-edit" value="' + escapeHtml(dr.nome) + '"></td>' +
                        '<td><input type="text" class="form-control form-control-sm drv-tel-edit" value="' + escapeHtml(dr.telefone || '') + '"></td>' +
                        '<td><input type="text" maxlength="6" inputmode="numeric" class="form-control form-control-sm drv-pin-edit" placeholder="—" value="' + escapeHtml(dr.pin || '') + '"></td>' +
                        '<td class="text-center"><input type="checkbox" class="form-check-input drv-ativo-edit" ' + (dr.ativo ? 'checked' : '') + '></td>' +
                        '<td>' +
                            (linkDriver ?
                                '<button class="btn btn-sm btn-outline-success btn-copiar-link" data-link="' + escapeHtml(linkDriver) + '" title="Copiar link p/ WhatsApp"><i class="bi bi-clipboard"></i> Copiar link</button>' +
                                '<a href="' + escapeHtml(linkDriver) + '" target="_blank" rel="noopener" class="btn btn-sm btn-outline-secondary ms-1" title="Abrir"><i class="bi bi-box-arrow-up-right"></i></a>'
                                : '<span class="text-muted small">salve pra gerar</span>') +
                        '</td>' +
                        '<td class="text-end">' +
                            '<button class="btn btn-sm btn-outline-primary me-1 btn-drv-salvar"><i class="bi bi-check"></i></button>' +
                            '<button class="btn btn-sm btn-outline-danger btn-drv-desativar" title="Desativar"><i class="bi bi-trash"></i></button>' +
                        '</td>' +
                        '</tr>';
                }
                html += '</tbody></table>' +
                    '<div class="form-text small mt-2">' +
                        '<i class="bi bi-info-circle"></i> ' +
                        'O link de acesso fica fixo por driver. PIN é opcional (4-6 dígitos) — deixe vazio pra acesso livre. ' +
                        'Mande o link pelo WhatsApp; o motorista salva nos favoritos do celular.' +
                    '</div></div>';
                lista.innerHTML = html;
            });
    }

    function criarDriver() {
        var nome = (document.getElementById('drv-nome').value || '').trim();
        var tel = (document.getElementById('drv-telefone').value || '').trim();
        var cor = document.getElementById('drv-cor').value;
        var msg = document.getElementById('drv-msg');
        msg.innerHTML = '';
        if (!nome) { msg.innerHTML = '<div class="text-danger">Informe o nome.</div>'; return; }
        fetch('/entregas/api/drivers', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
            body: JSON.stringify({nome: nome, telefone: tel, cor: cor})
        }).then(function(r) { return r.json(); })
          .then(function(d) {
              if (d.ok) {
                  document.getElementById('drv-nome').value = '';
                  document.getElementById('drv-telefone').value = '';
                  carregarDrivers();
              } else {
                  msg.innerHTML = '<div class="text-danger">' + escapeHtml(d.erro || 'Erro ao criar') + '</div>';
              }
          });
    }

    // ──────────────────────────────────────────
    // ── Produtos (resumo de quantidades por SKU) ──
    // ──────────────────────────────────────────

    var prodUltimoResultado = null;

    function carregarProdutos() {
        var data = document.getElementById('prod-data').value;
        var janelas = getJanelas('prod');
        var loading = document.getElementById('prod-loading');
        var msg = document.getElementById('prod-msg');
        var container = document.getElementById('prod-container');
        var resumo = document.getElementById('prod-resumo');

        loading.classList.remove('d-none');
        msg.innerHTML = '';
        container.innerHTML = '';
        resumo.style.display = 'none';

        var url = '/entregas/api/produtos?data=' + encodeURIComponent(data) +
                  janelas.map(function(j) { return '&janela=' + encodeURIComponent(j); }).join('');

        fetch(url, {credentials: 'same-origin'})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                loading.classList.add('d-none');
                if (d.erro) {
                    msg.innerHTML = '<div class="alert alert-warning py-2 small">' + escapeHtml(d.erro) + '</div>';
                    return;
                }
                prodUltimoResultado = d;
                renderCheckboxesJanela('prod', d.periodos_disponiveis || [], d.janelas || []);
                document.getElementById('prod-total-pedidos').textContent = d.total_pedidos || 0;
                resumo.style.display = '';
                renderProdutos(d);
            })
            .catch(function() {
                loading.classList.add('d-none');
                msg.innerHTML = '<div class="alert alert-danger py-2 small">Falha ao carregar produtos.</div>';
            });
    }

    function tabelaProdutos(lista, opts) {
        opts = opts || {};
        var mostrarPreco = !!opts.preco;
        var mostrarUnidade = !!opts.unidade;
        var html = '<div class="table-responsive"><table class="table table-sm table-hover mb-0">' +
            '<thead class="thead-padaria"><tr>' +
                '<th style="width:80px;">SKU</th>' +
                '<th>Produto</th>' +
                '<th class="text-center" style="width:120px;">Qtd</th>' +
                (opts.componente_de ? '<th>Vem de</th>' : '') +
                (mostrarPreco ? '<th class="text-end" style="width:120px;">Preço unit</th><th class="text-end" style="width:140px;">Total</th>' : '') +
            '</tr></thead><tbody>';
        for (var i = 0; i < lista.length; i++) {
            var p = lista[i];
            var qtdHtml = '<strong>' + p.quantidade + '</strong>';
            if (mostrarUnidade && p.unidade) {
                qtdHtml += ' <small class="text-muted">' + escapeHtml(p.unidade) + '</small>';
            }
            html += '<tr>' +
                '<td><small class="text-muted">' + escapeHtml(p.sku || '—') + '</small></td>' +
                '<td>' + escapeHtml(p.nome) + '</td>' +
                '<td class="text-center">' + qtdHtml + '</td>';
            if (opts.componente_de) {
                var origem = (p.componente_de && p.componente_de.length) ?
                    p.componente_de.map(escapeHtml).join(', ') : '<span class="text-muted">—</span>';
                html += '<td><small class="text-muted">' + origem + '</small></td>';
            }
            if (mostrarPreco) {
                html += '<td class="text-end small text-muted">R$ ' + formatMoney(p.preco_unitario) + '</td>' +
                        '<td class="text-end">R$ ' + formatMoney(p.valor_total) + '</td>';
            }
            html += '</tr>';
        }
        html += '</tbody></table></div>';
        return html;
    }

    function renderProdutos(d) {
        var container = document.getElementById('prod-container');
        var nadaVendido = !d.vendidos || d.vendidos.length === 0;
        var nadaProducao = !d.producao || d.producao.length === 0;

        if (nadaVendido && nadaProducao) {
            container.innerHTML = '<div class="alert alert-info py-2 small">Nenhum produto pra essa data e janela.</div>';
            return;
        }

        var html = '';

        // Vendido (como veio do VNDA)
        html += '<div class="card mb-3">' +
            '<div class="card-header py-2 d-flex justify-content-between align-items-center">' +
                '<strong><i class="bi bi-cart-check"></i> Vendidos no dia</strong>' +
                '<div>' +
                    '<small class="text-muted me-2">' + d.total_skus_vendidos + ' SKUs · ' + d.total_itens_vendidos + ' itens · R$ ' + formatMoney(d.valor_total) + '</small>' +
                    '<button class="btn btn-sm btn-outline-success btn-prod-copiar-vendidos d-print-none">' +
                        '<i class="bi bi-clipboard"></i> Copiar' +
                    '</button>' +
                '</div>' +
            '</div>' +
            tabelaProdutos(d.vendidos, {preco: true}) +
            '</div>';

        // Producao (cestas explodidas) — totais separados por unidade
        var totaisStr = '';
        if (d.totais_producao_por_unidade) {
            var partes = [];
            // Ordem preferida: un primeiro, depois g, kg, ml, l, etc
            var ordem = ['un', 'g', 'kg', 'ml', 'l'];
            var unidades = Object.keys(d.totais_producao_por_unidade).sort(function(a, b) {
                var ia = ordem.indexOf(a); if (ia === -1) ia = 99;
                var ib = ordem.indexOf(b); if (ib === -1) ib = 99;
                return ia - ib;
            });
            for (var ui = 0; ui < unidades.length; ui++) {
                var u = unidades[ui];
                partes.push(d.totais_producao_por_unidade[u] + ' ' + u);
            }
            totaisStr = partes.join(' · ');
        }

        html += '<div class="card mb-3">' +
            '<div class="card-header py-2 d-flex justify-content-between align-items-center" style="background:#fff5e6;">' +
                '<strong><i class="bi bi-tools"></i> A produzir <small class="text-muted fw-normal">(cestas explodidas)</small></strong>' +
                '<div>' +
                    '<small class="text-muted me-2">' + d.total_skus_producao + ' itens base · ' + escapeHtml(totaisStr) + '</small>' +
                    '<button class="btn btn-sm btn-outline-success btn-prod-copiar-producao d-print-none">' +
                        '<i class="bi bi-clipboard"></i> Copiar' +
                    '</button>' +
                '</div>' +
            '</div>' +
            tabelaProdutos(d.producao, {componente_de: true, unidade: true}) +
            '</div>';

        container.innerHTML = html;

        // Bind copy buttons
        var btnVend = container.querySelector('.btn-prod-copiar-vendidos');
        if (btnVend) btnVend.addEventListener('click', function() { copiarListaProdutos('vendidos'); });
        var btnProd = container.querySelector('.btn-prod-copiar-producao');
        if (btnProd) btnProd.addEventListener('click', function() { copiarListaProdutos('producao'); });
    }

    function copiarListaProdutos(tipo) {
        if (!prodUltimoResultado) return;
        var d = prodUltimoResultado;
        var lista = (tipo === 'producao') ? d.producao : d.vendidos;
        if (!lista || lista.length === 0) return;
        var dataFmt = d.data ? d.data.split('-').reverse().join('/') : '';
        var titulo = '*' + (tipo === 'producao' ? 'Produção (itens base)' : 'Vendidos') +
                     ' ' + dataFmt + (d.janela ? ' — ' + d.janela : '') + '*';
        var lines = [titulo, ''];
        for (var i = 0; i < lista.length; i++) {
            var p = lista[i];
            var qtd = p.quantidade + (tipo === 'producao' && p.unidade ? ' ' + p.unidade : 'x');
            var line = qtd + ' ' + p.nome + (p.sku ? ' (' + p.sku + ')' : '');
            if (tipo === 'producao' && p.componente_de && p.componente_de.length > 0) {
                line += '  [vem de: ' + p.componente_de.join(', ') + ']';
            }
            lines.push(line);
        }
        lines.push('');
        if (tipo === 'producao' && d.totais_producao_por_unidade) {
            var partes = [];
            var ordem = ['un', 'g', 'kg', 'ml', 'l'];
            var unidades = Object.keys(d.totais_producao_por_unidade).sort(function(a, b) {
                var ia = ordem.indexOf(a); if (ia === -1) ia = 99;
                var ib = ordem.indexOf(b); if (ib === -1) ib = 99;
                return ia - ib;
            });
            for (var ui = 0; ui < unidades.length; ui++) {
                var u = unidades[ui];
                partes.push(d.totais_producao_por_unidade[u] + ' ' + u);
            }
            lines.push('Total: ' + partes.join(' · ') + ' em ' + d.total_pedidos + ' pedidos');
        } else {
            lines.push('Total: ' + (d.total_itens_vendidos || 0) + ' unidades em ' + d.total_pedidos + ' pedidos');
        }
        var texto = lines.join('\n');
        if (navigator.clipboard) {
            navigator.clipboard.writeText(texto).then(function() {
                alert('Lista copiada!');
            }, function() { fallbackCopiar(texto); });
        } else {
            fallbackCopiar(texto);
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        var btnProd = document.getElementById('btn-prod-carregar');
        if (btnProd) btnProd.addEventListener('click', carregarProdutos);
        var tabProd = document.getElementById('btn-tab-produtos');
        if (tabProd) tabProd.addEventListener('shown.bs.tab', function() {
            var dataPedidos = document.getElementById('data-entrega').value;
            if (dataPedidos) document.getElementById('prod-data').value = dataPedidos;
            carregarProdutos();
        });
    });

    var atribUltimoResultado = null;

    function carregarAtribuidos() {
        var data = document.getElementById('atrib-data').value;
        var loading = document.getElementById('atrib-loading');
        var msg = document.getElementById('atrib-msg');
        var container = document.getElementById('atrib-container');
        var resumo = document.getElementById('atrib-resumo');

        loading.classList.remove('d-none');
        msg.innerHTML = '';
        container.innerHTML = '';
        resumo.style.display = 'none';

        fetch('/entregas/api/atribuidos?data=' + encodeURIComponent(data), {credentials: 'same-origin'})
            .then(function(r) { return r.json(); })
            .then(function(d) {
                loading.classList.add('d-none');
                if (d.erro) {
                    msg.innerHTML = '<div class="alert alert-warning py-2 small">' + escapeHtml(d.erro) + '</div>';
                    return;
                }
                atribUltimoResultado = d;
                document.getElementById('atrib-total').textContent = d.total_pedidos || 0;
                document.getElementById('atrib-total-atrib').textContent = d.total_atribuidos || 0;
                resumo.style.display = '';
                renderAtribuidos(d);
            })
            .catch(function() {
                loading.classList.add('d-none');
                msg.innerHTML = '<div class="alert alert-danger py-2 small">Falha ao carregar atribuídos.</div>';
            });
    }

    // Cache de drivers disponiveis (usado pelo bulk)
    var __driversDisp = [];

    function renderAtribuidos(d) {
        var container = document.getElementById('atrib-container');
        var msg = document.getElementById('atrib-msg');
        var html = '';

        if ((!d.drivers || d.drivers.length === 0) && (!d.sem_driver || d.sem_driver.length === 0)) {
            container.innerHTML = '<div class="alert alert-info py-2 small">Nenhum pedido para essa data.</div>';
            __driversDisp = d.drivers_disponiveis || [];
            atualizarBulkDrivers();
            return;
        }

        if (!d.drivers_disponiveis || d.drivers_disponiveis.length === 0) {
            msg.innerHTML = '<div class="alert alert-warning py-2 small">' +
                '<i class="bi bi-exclamation-triangle"></i> Nenhum driver cadastrado. Vai na aba <strong>Rotas</strong> e clica em <strong>Drivers</strong> pra adicionar.' +
                '</div>';
        }

        __driversDisp = d.drivers_disponiveis || [];
        atualizarBulkDrivers();

        // Topo: Sem driver (acao necessaria)
        if (d.sem_driver && d.sem_driver.length > 0) {
            var pseudoDriver = {
                id: null,
                nome: 'Sem driver',
                cor: '#dc3545',
                paradas: d.sem_driver,
                qtd: d.sem_driver.length,
            };
            html += renderSecaoAtribuidos(pseudoDriver, d.drivers_disponiveis, true);
        }

        // Embaixo: por driver
        for (var i = 0; i < d.drivers.length; i++) {
            html += renderSecaoAtribuidos(d.drivers[i], d.drivers_disponiveis, false);
        }

        container.innerHTML = html;
        atualizarBulkBar();
    }

    function atualizarBulkDrivers() {
        var sel = document.getElementById('atrib-bulk-driver');
        if (!sel) return;
        var html = '<option value="">— Sem driver —</option>';
        for (var i = 0; i < __driversDisp.length; i++) {
            html += '<option value="' + __driversDisp[i].id + '">' + escapeHtml(__driversDisp[i].nome) + '</option>';
        }
        sel.innerHTML = html;
    }

    function atualizarBulkBar() {
        var bar = document.getElementById('atrib-bulk-bar');
        var count = document.querySelectorAll('.atrib-check:checked').length;
        if (bar) {
            bar.style.display = count > 0 ? '' : 'none';
            var c = document.getElementById('atrib-bulk-count');
            if (c) c.textContent = count;
        }
    }

    function renderSecaoAtribuidos(driver, driversDisp, isSemDriver) {
        var cor = driver.cor || '#999';
        var bgHeader = isSemDriver ? '#fff5f5' : '';
        var html = '<div class="card mb-3 atrib-secao">' +
            '<div class="card-header py-2 d-flex justify-content-between align-items-center flex-wrap gap-2" style="border-left:4px solid ' + cor + ';' + (bgHeader ? 'background:' + bgHeader + ';' : '') + '">' +
                '<div class="d-flex align-items-center gap-2">' +
                    '<input type="checkbox" class="form-check-input atrib-check-secao d-print-none" data-secao="' + (isSemDriver ? 'sem' : driver.id) + '" title="Selecionar todos desta secao">' +
                    '<strong style="color:' + cor + ';"><i class="bi bi-' + (isSemDriver ? 'exclamation-circle' : 'person-badge') + '"></i> ' + escapeHtml(driver.nome) + '</strong>' +
                    '<span class="text-muted small">' + driver.qtd + ' pedido' + (driver.qtd !== 1 ? 's' : '') + '</span>' +
                    (driver.telefone ? '<small class="text-muted"><i class="bi bi-telephone"></i> ' + escapeHtml(driver.telefone) + '</small>' : '') +
                '</div>';

        if (!isSemDriver) {
            html += '<button class="btn btn-sm btn-outline-success d-print-none btn-copiar-lista" data-driver-id="' + driver.id + '">' +
                    '<i class="bi bi-clipboard"></i> Copiar lista (WhatsApp)</button>';
        }
        html += '</div>';

        html += '<div class="list-group list-group-flush">';
        for (var j = 0; j < driver.paradas.length; j++) {
            var p = driver.paradas[j];
            var st = p.status || 'pendente';
            var stBadge = '';
            if (st === 'entregue') {
                stBadge = '<span class="badge bg-success" style="font-size:10px;"><i class="bi bi-check-circle"></i> Entregue</span>';
            } else if (st === 'nao_entregue') {
                stBadge = '<span class="badge bg-danger" style="font-size:10px;"><i class="bi bi-x-circle"></i> Não entregue</span>';
            }
            var fotosHtml = '';
            if (p.fotos && p.fotos.length > 0) {
                fotosHtml = '<div class="d-flex gap-1 mt-2">';
                for (var fi = 0; fi < p.fotos.length; fi++) {
                    fotosHtml += '<a href="' + escapeHtml(p.fotos[fi].url) + '" target="_blank" rel="noopener">' +
                        '<img src="' + escapeHtml(p.fotos[fi].url) + '" style="width:48px;height:48px;object-fit:cover;border-radius:4px;border:1px solid #e2e8f0;"></a>';
                }
                fotosHtml += '</div>';
            }
            var proofLink = '';
            if (p.proof_hash) {
                proofLink = ' <button class="btn btn-link btn-sm p-0 ms-2 atrib-copiar-proof" data-hash="' + escapeHtml(p.proof_hash) + '" style="font-size:11px;" title="Copiar link do comprovante (cliente)"><i class="bi bi-link-45deg"></i> Comprovante</button>';
            }
            var adminBtns = '';
            var temComprovante = (st === 'entregue' || st === 'nao_entregue' || (p.fotos && p.fotos.length > 0) || p.proof_hash);
            if (isAdmin() && temComprovante) {
                adminBtns =
                    ' <button class="btn btn-link btn-sm p-0 ms-2 atrib-reabrir d-print-none" data-code="' + escapeHtml(p.code) + '" style="font-size:11px;color:#b45309;" title="Voltar pra pendente e apagar fotos"><i class="bi bi-arrow-counterclockwise"></i> Reabrir</button>' +
                    ' <button class="btn btn-link btn-sm p-0 ms-2 atrib-mover d-print-none" data-code="' + escapeHtml(p.code) + '" style="font-size:11px;color:#0369a1;" title="Mover comprovante pra outro pedido"><i class="bi bi-arrow-left-right"></i> Mover</button>';
            }
            html += '<div class="list-group-item">' +
                '<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">' +
                    '<div class="d-flex align-items-start gap-2" style="flex:1; min-width:240px;">' +
                        '<input type="checkbox" class="form-check-input atrib-check d-print-none mt-1" data-code="' + escapeHtml(p.code) + '" data-driver-atual="' + (driver.id || '') + '">' +
                        '<div style="flex:1; min-width:0;">' +
                            '<a href="https://www.padariaartesanalonline.com.br/admin/pedido?id=' + encodeURIComponent(p.code) + '" target="_blank" rel="noopener" class="text-decoration-none small fw-bold" style="color:var(--accent);">' +
                                '[' + escapeHtml(p.code) + '] <i class="bi bi-box-arrow-up-right" style="font-size:10px;"></i>' +
                            '</a> ' +
                            '<span class="fw-semibold"><i class="bi bi-person-fill"></i> ' + escapeHtml(p.destinatario || '—') + '</span>' +
                            (stBadge ? ' ' + stBadge : '') +
                            (p.periodo ? ' <span class="badge bg-light text-dark" style="font-size:10px;"><i class="bi bi-clock"></i> ' + escapeHtml(p.periodo) + '</span>' : '') +
                            proofLink +
                            adminBtns +
                            '<div class="text-muted small mt-1"><i class="bi bi-geo-alt"></i> ' + escapeHtml(p.endereco || '') + '</div>' +
                            (p.telefone ? '<div class="text-muted small"><i class="bi bi-telephone"></i> ' + escapeHtml(p.telefone) + '</div>' : '') +
                            (p.nota_driver ? '<div class="text-muted small fst-italic mt-1"><i class="bi bi-chat-left-quote"></i> ' + escapeHtml(p.nota_driver) + '</div>' : '') +
                            fotosHtml +
                        '</div>' +
                    '</div>' +
                    '<div class="d-print-none d-flex align-items-center gap-1">' +
                        '<select class="form-select form-select-sm atrib-select-driver" data-code="' + escapeHtml(p.code) + '" data-driver-atual="' + (driver.id || '') + '" style="max-width:160px; font-size:12px;">' +
                            '<option value="">— Sem driver —</option>';
            for (var k = 0; k < (driversDisp || []).length; k++) {
                var dd = driversDisp[k];
                var sel = (driver.id && dd.id === driver.id) ? ' selected' : '';
                html += '<option value="' + dd.id + '"' + sel + '>' + escapeHtml(dd.nome) + '</option>';
            }
            html += '</select>' +
                    '</div>' +
                '</div>' +
            '</div>';
        }
        html += '</div></div>';
        return html;
    }

    function copiarListaWhatsApp(driver, dataIso) {
        // Formato do texto: nome do driver + data + lista de paradas
        var dataFmt = dataIso ? dataIso.split('-').reverse().join('/') : '';
        var lines = ['*Entregas ' + dataFmt + ' — ' + driver.nome + '*', ''];
        for (var i = 0; i < driver.paradas.length; i++) {
            var p = driver.paradas[i];
            lines.push((i + 1) + '. *' + (p.destinatario || '—') + '*');
            if (p.endereco) lines.push('   ' + p.endereco);
            if (p.telefone) lines.push('   ☎ ' + p.telefone);
            if (p.periodo) lines.push('   ⏱ ' + p.periodo);
            lines.push('   #' + p.code);
            lines.push('');
        }
        var texto = lines.join('\n');
        if (navigator.clipboard) {
            navigator.clipboard.writeText(texto).then(function() {
                alert('Lista copiada! Cola no WhatsApp do ' + driver.nome + '.');
            }, function() {
                fallbackCopiar(texto);
            });
        } else {
            fallbackCopiar(texto);
        }
    }

    function salvarAtribuicoesGeradas(d, msgEl) {
        var items = [];
        (d.rotas || []).forEach(function(r) {
            (r.paradas || []).forEach(function(p, idx) {
                items.push({
                    code: p.code,
                    driver_id: r.driver.id,
                    ordem: idx,
                    data_entrega: d.data,
                });
            });
        });
        if (items.length === 0) return;
        fetch('/entregas/api/atribuicao/lote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
            body: JSON.stringify({items: items}),
        }).then(function(r) { return r.json(); })
          .then(function(resp) {
              if (!resp.ok) {
                  if (msgEl) msgEl.innerHTML = '<div class="alert alert-warning py-2 small">Distribuição feita mas falhou ao salvar: ' + escapeHtml(resp.erro || '?') + '</div>';
                  return;
              }
              if (msgEl) msgEl.innerHTML = '<div class="alert alert-success py-2 small"><i class="bi bi-check-circle"></i> ' + items.length + ' pedido(s) atribuído(s) e salvo(s).</div>';
          })
          .catch(function() {
              if (msgEl) msgEl.innerHTML = '<div class="alert alert-danger py-2 small">Falha ao salvar atribuições no banco.</div>';
          });
    }

    function fallbackCopiar(texto) {
        var ta = document.createElement('textarea');
        ta.value = texto;
        ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); alert('Lista copiada!'); }
        catch (e) { prompt('Copie manualmente:', texto); }
        document.body.removeChild(ta);
    }

    document.addEventListener('DOMContentLoaded', function() {
        var btn = document.getElementById('btn-gerar-rotas');
        if (btn) btn.addEventListener('click', function() { gerarRotas(true); });
        var tabBtn = document.getElementById('btn-tab-rotas');
        if (tabBtn) tabBtn.addEventListener('shown.bs.tab', function() {
            var dataPedidos = document.getElementById('data-entrega').value;
            if (dataPedidos) document.getElementById('rotas-data').value = dataPedidos;
        });
        // Trocar checkbox de janela re-busca automatico
        var rotasJanCb = document.getElementById('rotas-janelas-cb');
        if (rotasJanCb) rotasJanCb.addEventListener('change', function(e) {
            if (e.target.matches('input[type=checkbox]')) gerarRotas(false);
        });
        // Trocar a data: limpa janelas (podem nao existir no dia novo) e re-busca em modo preview
        var rotasDataInput = document.getElementById('rotas-data');
        if (rotasDataInput) rotasDataInput.addEventListener('change', function() {
            var cont = document.getElementById('rotas-janelas-cb');
            if (cont) cont.querySelectorAll('input[type=checkbox]:checked').forEach(function(cb) { cb.checked = false; });
            gerarRotas(false);
        });
        var prodJanCb = document.getElementById('prod-janelas-cb');
        if (prodJanCb) prodJanCb.addEventListener('change', function(e) {
            if (e.target.matches('input[type=checkbox]')) carregarProdutos();
        });

        // Aba Atribuidos
        var btnAtrib = document.getElementById('btn-atrib-carregar');
        if (btnAtrib) btnAtrib.addEventListener('click', carregarAtribuidos);
        var tabAtrib = document.getElementById('btn-tab-atribuidos');
        if (tabAtrib) tabAtrib.addEventListener('shown.bs.tab', function() {
            var dataPedidos = document.getElementById('data-entrega').value;
            if (dataPedidos) document.getElementById('atrib-data').value = dataPedidos;
            carregarAtribuidos();
        });

        // Trocar driver inline (delegacao)
        var atribContainer = document.getElementById('atrib-container');
        if (atribContainer) {
            atribContainer.addEventListener('change', function(e) {
                var sel = e.target.closest('.atrib-select-driver');
                if (!sel) return;
                var code = sel.dataset.code;
                var novoId = sel.value || null;
                var dataAtual = (atribUltimoResultado && atribUltimoResultado.data) || '';
                var url = '/entregas/api/atribuicao/' + encodeURIComponent(code);
                var body = {data_entrega: dataAtual};
                var fetchPromise;
                if (!novoId) {
                    // Remover atribuicao
                    fetchPromise = fetch(url, {
                        method: 'DELETE',
                        headers: {'X-CSRFToken': CSRF_TOKEN},
                    });
                } else {
                    body.driver_id = parseInt(novoId, 10);
                    fetchPromise = fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                        body: JSON.stringify(body),
                    });
                }
                fetchPromise.then(function(r) { return r.json(); })
                    .then(function(d) {
                        if (d.ok) {
                            // Recarrega tudo pra refletir
                            carregarAtribuidos();
                        } else {
                            alert('Erro: ' + (d.erro || 'desconhecido'));
                        }
                    });
            });
            // Botao copiar lista
            atribContainer.addEventListener('click', function(e) {
                var btn = e.target.closest('.btn-copiar-lista');
                if (!btn) return;
                var did = parseInt(btn.dataset.driverId, 10);
                if (!atribUltimoResultado) return;
                var driver = (atribUltimoResultado.drivers || []).find(function(x) { return x.id === did; });
                if (driver) {
                    copiarListaWhatsApp(driver, atribUltimoResultado.data);
                }
            });

            // Copiar link do comprovante (cliente)
            atribContainer.addEventListener('click', function(e) {
                var b = e.target.closest('.atrib-copiar-proof');
                if (!b) return;
                e.preventDefault();
                var hash = b.dataset.hash;
                var link = window.location.origin + '/entrega/' + hash;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(link).then(function() {
                        var orig = b.innerHTML;
                        b.innerHTML = '<i class="bi bi-check2"></i> Copiado!';
                        setTimeout(function() { b.innerHTML = orig; }, 1500);
                    });
                } else {
                    prompt('Link do comprovante:', link);
                }
            });

            // Admin: reabrir entrega (volta pra pendente + apaga fotos)
            atribContainer.addEventListener('click', function(e) {
                var b = e.target.closest('.atrib-reabrir');
                if (!b) return;
                e.preventDefault();
                var code = b.dataset.code;
                if (!confirm('Reabrir o pedido ' + code + '?\nIsso apaga as fotos e volta pra pendente.')) return;
                b.disabled = true;
                fetch('/entregas/api/entrega/' + encodeURIComponent(code) + '/reset', {
                    method: 'POST',
                    headers: {'X-CSRFToken': CSRF_TOKEN},
                    credentials: 'same-origin',
                }).then(function(r) { return r.json(); })
                  .then(function(d) {
                      if (!d.ok) { alert('Erro: ' + (d.erro || '?')); b.disabled = false; return; }
                      if (d.data) document.getElementById('atrib-data').value = d.data;
                      carregarAtribuidos();
                  })
                  .catch(function() { alert('Falha de rede.'); b.disabled = false; });
            });

            // Admin: mover comprovante pra outro pedido
            atribContainer.addEventListener('click', function(e) {
                var b = e.target.closest('.atrib-mover');
                if (!b) return;
                e.preventDefault();
                var code = b.dataset.code;
                var destino = prompt('Mover comprovante de ' + code + ' para qual pedido?\nDigite o code do pedido destino:');
                if (!destino) return;
                destino = destino.trim();
                if (!destino || destino === code) return;
                b.disabled = true;
                fetch('/entregas/api/entrega/' + encodeURIComponent(code) + '/migrar', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                    credentials: 'same-origin',
                    body: JSON.stringify({destino: destino}),
                }).then(function(r) { return r.json(); })
                  .then(function(d) {
                      if (!d.ok) { alert('Erro: ' + (d.erro || '?')); b.disabled = false; return; }
                      if (d.data) document.getElementById('atrib-data').value = d.data;
                      alert(d.fotos_movidas + ' foto(s) movida(s) para ' + d.destino + '.');
                      carregarAtribuidos();
                  })
                  .catch(function() { alert('Falha de rede.'); b.disabled = false; });
            });

            // Bulk: checkbox individual
            atribContainer.addEventListener('change', function(e) {
                if (e.target.matches('.atrib-check')) {
                    atualizarBulkBar();
                }
                // Checkbox de secao (selecionar todos da secao)
                if (e.target.matches('.atrib-check-secao')) {
                    var card = e.target.closest('.atrib-secao');
                    if (card) {
                        card.querySelectorAll('.atrib-check').forEach(function(cb) {
                            cb.checked = e.target.checked;
                        });
                        atualizarBulkBar();
                    }
                }
            });
        }

        // Bulk bar: aplicar atribuicao em lote
        var btnBulkAplicar = document.getElementById('atrib-bulk-aplicar');
        if (btnBulkAplicar) btnBulkAplicar.addEventListener('click', function() {
            var driverSel = document.getElementById('atrib-bulk-driver');
            var driverId = driverSel.value ? parseInt(driverSel.value, 10) : null;
            var checks = document.querySelectorAll('.atrib-check:checked');
            if (checks.length === 0) return;
            var dataAtual = (atribUltimoResultado && atribUltimoResultado.data) || '';
            var items = [];
            checks.forEach(function(cb, idx) {
                items.push({code: cb.dataset.code, driver_id: driverId, ordem: idx, data_entrega: dataAtual});
            });
            fetch('/entregas/api/atribuicao/lote', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                body: JSON.stringify({items: items}),
            }).then(function(r) { return r.json(); })
              .then(function(d) {
                  if (d.ok) {
                      carregarAtribuidos();
                  } else {
                      alert('Erro: ' + (d.erro || 'desconhecido'));
                  }
              });
        });

        var btnBulkLimpar = document.getElementById('atrib-bulk-limpar');
        if (btnBulkLimpar) btnBulkLimpar.addEventListener('click', function() {
            document.querySelectorAll('.atrib-check, .atrib-check-secao').forEach(function(cb) {
                cb.checked = false;
            });
            atualizarBulkBar();
        });

        // Modal de drivers
        var btnCriar = document.getElementById('btn-criar-driver');
        if (btnCriar) btnCriar.addEventListener('click', criarDriver);

        var modalDrv = document.getElementById('modal-drivers');
        if (modalDrv) {
            modalDrv.addEventListener('show.bs.modal', carregarDrivers);
            // Salvar/desativar
            modalDrv.addEventListener('click', function(e) {
                var row = e.target.closest('tr[data-id]');
                if (!row) return;
                var id = row.dataset.id;
                var btnCopiar = e.target.closest('.btn-copiar-link');
                if (btnCopiar) {
                    var link = btnCopiar.dataset.link;
                    if (link) {
                        if (navigator.clipboard && navigator.clipboard.writeText) {
                            navigator.clipboard.writeText(link).then(function() {
                                btnCopiar.innerHTML = '<i class="bi bi-check2"></i> Copiado!';
                                setTimeout(function() { btnCopiar.innerHTML = '<i class="bi bi-clipboard"></i> Copiar link'; }, 1500);
                            });
                        } else {
                            prompt('Copie o link:', link);
                        }
                    }
                    return;
                }
                if (e.target.closest('.btn-drv-salvar')) {
                    var dados = {
                        nome: row.querySelector('.drv-nome-edit').value,
                        telefone: row.querySelector('.drv-tel-edit').value,
                        cor: row.querySelector('.drv-cor-edit').value,
                        ativo: row.querySelector('.drv-ativo-edit').checked,
                        pin: row.querySelector('.drv-pin-edit').value,
                    };
                    fetch('/entregas/api/drivers/' + id, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                        body: JSON.stringify(dados),
                    }).then(function(r) { return r.json(); })
                      .then(function(d) {
                          if (d.ok) {
                              row.style.background = '#d4edda';
                              setTimeout(function() { row.style.background = ''; carregarDrivers(); }, 700);
                          } else {
                              alert('Erro: ' + (d.erro || 'desconhecido'));
                          }
                      });
                } else if (e.target.closest('.btn-drv-desativar')) {
                    if (!confirm('Excluir esse driver?')) return;
                    fetch('/entregas/api/drivers/' + id, {
                        method: 'DELETE',
                        headers: {'X-CSRFToken': CSRF_TOKEN},
                    }).then(function(r) { return r.json(); })
                      .then(function(d) {
                          if (d.acao === 'excluido') {
                              carregarDrivers();
                          } else if (d.acao === 'desativado') {
                              var msg = 'Esse driver tem ' + d.atribuicoes + ' pedido(s) no histórico. ' +
                                        'Foi apenas DESATIVADO (não aparece mais nas rotas novas).\n\n' +
                                        'Quer apagar o driver E o histórico de atribuições? (irreversível)';
                              if (confirm(msg)) {
                                  fetch('/entregas/api/drivers/' + id + '?force=1', {
                                      method: 'DELETE',
                                      headers: {'X-CSRFToken': CSRF_TOKEN},
                                  }).then(function(r) { return r.json(); })
                                    .then(function() { carregarDrivers(); });
                              } else {
                                  carregarDrivers();
                              }
                          } else {
                              carregarDrivers();
                          }
                      });
                }
            });
        }
    });

    // ── Aba Operação (unificada) ──
    var opUltimoResultado = null;
    var opMapaVisivel = false;
    var opMapa = null;
    var opMapaLayers = null;
    var opJanelaFiltro = new Set();  // filtro local — vazio = todas

    function opData() { return document.getElementById('op-data').value; }
    var opBuscaTexto = '';

    function opCarregar() {
        var data = opData();
        if (!data) return;
        var loading = document.getElementById('op-loading');
        var msg = document.getElementById('op-msg');
        var container = document.getElementById('op-container');
        var resumo = document.getElementById('op-resumo');

        loading.classList.remove('d-none');
        msg.innerHTML = '';

        var pAtrib = fetch('/entregas/api/atribuidos?data=' + encodeURIComponent(data),
            {credentials: 'same-origin'}).then(function(r) { return r.json(); });
        var pRotas = opMapaVisivel
            ? fetch('/entregas/api/rotas?data=' + encodeURIComponent(data),
                {credentials: 'same-origin'}).then(function(r) { return r.json(); })
            : Promise.resolve(null);

        Promise.all([pAtrib, pRotas]).then(function(rs) {
            loading.classList.add('d-none');
            var d = rs[0];
            var dRotas = rs[1];
            if (d.erro) {
                msg.innerHTML = '<div class="alert alert-warning py-2 small">' + escapeHtml(d.erro) + '</div>';
                container.innerHTML = '';
                resumo.textContent = '';
                return;
            }
            opUltimoResultado = d;
            __driversDisp = d.drivers_disponiveis || [];
            opMontarFiltroJanela(d);
            opMontarBulkSelect(d);
            opAtualizarResumo(d);
            opRenderLista(d, container);
            if (opMapaVisivel && dRotas) opRenderMapa(dRotas);
        }).catch(function(e) {
            loading.classList.add('d-none');
            msg.innerHTML = '<div class="alert alert-danger py-2 small">Falha ao carregar: ' + escapeHtml(String(e)) + '</div>';
        });
    }

    function opAtualizarResumo(d) {
        var nDrivers = (d.drivers || []).length;
        var nSem = (d.sem_driver || []).length;
        var nPed = d.total_pedidos || 0;
        var nAtrib = d.total_atribuidos || 0;
        var totalRs = 0;
        (d.drivers || []).forEach(function(dr) {
            (dr.paradas || []).forEach(function(p) { totalRs += (p.total || 0); });
        });
        (d.sem_driver || []).forEach(function(p) { totalRs += (p.total || 0); });
        var totalFmt = totalRs.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        document.getElementById('op-resumo').innerHTML =
            '<strong>' + nPed + '</strong> pedido(s) · <strong>' + nAtrib + '</strong> atribuído(s) em ' +
            nDrivers + ' driver(s) · <strong>' + nSem + '</strong> sem driver · <strong>R$ ' + totalFmt + '</strong> total';
    }

    function opMatchBusca(p) {
        if (!opBuscaTexto) return true;
        var alvo = ((p.destinatario || '') + ' ' + (p.code || '') + ' ' + (p.endereco || '') + ' ' + (p.telefone || '')).toLowerCase();
        return alvo.indexOf(opBuscaTexto.toLowerCase()) !== -1;
    }

    function opMontarFiltroJanela(d) {
        // Filtro de janela foi removido da UI; manter funcao como no-op pra
        // nao precisar mexer nas chamadas existentes.
        var cont = document.getElementById('op-janelas');
        if (!cont) return;
        cont.innerHTML = '';
    }

    function opMontarBulkSelect(d) {
        var sel = document.getElementById('op-bulk-driver');
        var html = '<option value="">— Sem driver —</option>';
        (d.drivers_disponiveis || []).forEach(function(dr) {
            html += '<option value="' + dr.id + '">' + escapeHtml(dr.nome) + '</option>';
        });
        sel.innerHTML = html;
    }

    function opPassaJanela(p) {
        if (opJanelaFiltro.size === 0) return true;
        return opJanelaFiltro.has(p.periodo || '');
    }

    function opRenderLista(d, container) {
        var html = '';
        var sem = (d.sem_driver || []).filter(opPassaJanela).filter(opMatchBusca);
        if (sem.length > 0) {
            html += opRenderSecao({id: '', nome: 'Sem driver', cor: '#94a3b8', paradas: sem, qtd: sem.length}, true, d.drivers_disponiveis);
        }
        (d.drivers || []).forEach(function(dr) {
            var paradas = (dr.paradas || []).filter(opPassaJanela).filter(opMatchBusca);
            if (paradas.length === 0) return;
            html += opRenderSecao({id: dr.id, nome: dr.nome, cor: dr.cor, paradas: paradas, qtd: paradas.length}, false, d.drivers_disponiveis);
        });
        if (!html) {
            html = '<div class="alert alert-info py-2 small">' +
                (opBuscaTexto ? 'Nenhum pedido bate com a busca.' : 'Nenhum pedido para esta data.') + '</div>';
        }
        container.innerHTML = html;
        opAtivarSortable();
    }

    function opRenderSecao(driver, isSemDriver, driversDisp) {
        var corFundo = isSemDriver ? '#f8fafc' : (driver.cor || '#5b8def') + '14';
        var corBorda = isSemDriver ? '#cbd5e1' : (driver.cor || '#5b8def');
        var html = '<div class="op-secao mb-3" data-driver-id="' + (driver.id || '') + '" style="background:' + corFundo + '; border-left:4px solid ' + corBorda + '; border-radius:6px; padding:10px;">';
        html += '<div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">';
        html += '<div class="d-flex align-items-center gap-2">';
        html += '<input type="checkbox" class="form-check-input op-check-secao d-print-none" title="Selecionar todos">';
        html += '<strong style="color:' + corBorda + ';">' + (isSemDriver ? '<i class="bi bi-exclamation-triangle"></i> ' : '<i class="bi bi-person-fill"></i> ') + escapeHtml(driver.nome) + '</strong>';
        html += '<span class="badge bg-light text-dark">' + driver.paradas.length + ' parada(s)</span>';
        html += '</div>';
        if (!isSemDriver) {
            html += '<button class="btn btn-sm btn-outline-success d-print-none op-copiar-lista" data-driver-id="' + driver.id + '">' +
                    '<i class="bi bi-clipboard"></i> Copiar p/ WhatsApp</button>';
        }
        html += '</div>';

        html += '<div class="list-group list-group-flush op-list" data-driver-id="' + (driver.id || '') + '">';
        for (var j = 0; j < driver.paradas.length; j++) {
            html += opRenderItem(driver.paradas[j], driver, driversDisp);
        }
        html += '</div></div>';
        return html;
    }

    function opRenderItem(p, driver, driversDisp) {
        var st = p.status || 'pendente';
        var stBadge = '';
        if (st === 'entregue') {
            stBadge = '<span class="badge bg-success" style="font-size:10px;"><i class="bi bi-check-circle"></i> Entregue</span>';
        } else if (st === 'nao_entregue') {
            stBadge = '<span class="badge bg-danger" style="font-size:10px;"><i class="bi bi-x-circle"></i> Não entregue</span>';
        }
        var fotosHtml = '';
        if (p.fotos && p.fotos.length > 0) {
            fotosHtml = '<div class="d-flex gap-1 mt-2">';
            p.fotos.forEach(function(f) {
                fotosHtml += '<a href="' + escapeHtml(f.url) + '" target="_blank" rel="noopener">' +
                    '<img src="' + escapeHtml(f.url) + '" style="width:48px;height:48px;object-fit:cover;border-radius:4px;border:1px solid #e2e8f0;"></a>';
            });
            fotosHtml += '</div>';
        }
        var proofLink = '';
        if (p.proof_hash) {
            proofLink = ' <button class="btn btn-link btn-sm p-0 ms-2 op-copiar-proof d-print-none" data-hash="' + escapeHtml(p.proof_hash) + '" style="font-size:11px;" title="Copiar link do comprovante (cliente)"><i class="bi bi-link-45deg"></i> Comprovante</button>';
        }
        var adminBtns = '';
        var temComprov = (st === 'entregue' || st === 'nao_entregue' || (p.fotos && p.fotos.length > 0) || p.proof_hash);
        if (isAdmin() && temComprov) {
            adminBtns =
                ' <button class="btn btn-link btn-sm p-0 ms-2 op-reabrir d-print-none" data-code="' + escapeHtml(p.code) + '" style="font-size:11px;color:#b45309;" title="Voltar pra pendente e apagar fotos"><i class="bi bi-arrow-counterclockwise"></i> Reabrir</button>' +
                ' <button class="btn btn-link btn-sm p-0 ms-2 op-mover d-print-none" data-code="' + escapeHtml(p.code) + '" style="font-size:11px;color:#0369a1;" title="Mover comprovante pra outro pedido"><i class="bi bi-arrow-left-right"></i> Mover</button>';
        }

        // Botao 'Mudar data' (abre modal de override que ja existe)
        var btnData = '';
        if (!p.pedido_local) {
            // Override so faz sentido pra pedidos VNDA. Pedido local edita direto via modal.
            btnData = ' <button class="btn btn-link btn-sm p-0 ms-2 op-mudar-data d-print-none" data-code="' + escapeHtml(p.code) + '" style="font-size:11px;color:#7c3aed;" title="Mudar data so no ERP"><i class="bi bi-calendar-event"></i> Mudar data</button>';
        }
        // Badge override + badge pedido local
        var badgeOrigem = '';
        if (p.pedido_local) {
            badgeOrigem = ' <span class="badge" style="background:#16a34a; font-size:10px;"><i class="bi bi-pencil-square"></i> Manual</span>';
        } else if (p.data_override) {
            badgeOrigem = ' <span class="badge bg-warning text-dark" style="font-size:10px;" title="Data alterada no ERP"><i class="bi bi-calendar-event"></i> Data alterada</span>';
        }

        // Total R$
        var totalHtml = '';
        if (p.total) {
            totalHtml = ' <span class="badge bg-light text-dark" style="font-size:10px;">R$ ' + p.total.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</span>';
        }

        // Itens (collapse)
        var itensHtml = '';
        if (p.itens && p.itens.length > 0) {
            var itensId = 'op-itens-' + p.code.replace(/[^a-zA-Z0-9]/g, '');
            itensHtml = '<div class="mt-1"><button class="btn btn-link btn-sm p-0" type="button" data-bs-toggle="collapse" data-bs-target="#' + itensId + '" style="font-size:11px;"><i class="bi bi-list-ul"></i> ' + p.itens.length + ' item(s)</button>' +
                '<div class="collapse" id="' + itensId + '"><ul class="small mb-0 mt-1" style="padding-left:18px; color:#475569;">';
            p.itens.forEach(function(it) {
                itensHtml += '<li>' + (it.quantidade || 1) + 'x ' + escapeHtml(it.nome || '') + (it.preco_unitario ? ' <span class="text-muted">— R$ ' + it.preco_unitario.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</span>' : '') + '</li>';
            });
            itensHtml += '</ul></div></div>';
        }

        // Botao editar pedido local
        var editLocal = '';
        if (p.pedido_local) {
            editLocal = ' <button class="btn btn-link btn-sm p-0 ms-2 op-editar-local d-print-none" data-id="' + p.id + '" style="font-size:11px;color:#0369a1;" title="Editar pedido manual"><i class="bi bi-pencil"></i> Editar</button>';
        }

        var html = '<div class="list-group-item op-item" data-code="' + escapeHtml(p.code) + '" data-driver-atual="' + (driver.id || '') + '">';
        html += '<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">';
        html += '<div class="d-flex align-items-start gap-2" style="flex:1; min-width:240px;">';
        html += '<input type="checkbox" class="form-check-input op-check d-print-none mt-1" data-code="' + escapeHtml(p.code) + '">';
        html += '<div style="flex:1; min-width:0;">';
        html += '<a href="https://www.padariaartesanalonline.com.br/admin/pedido?id=' + encodeURIComponent(p.code) + '" target="_blank" rel="noopener" class="text-decoration-none small fw-bold" style="color:var(--accent);">' +
            '[' + escapeHtml(p.code) + '] <i class="bi bi-box-arrow-up-right" style="font-size:10px;"></i></a> ';
        html += '<span class="fw-semibold"><i class="bi bi-person-fill"></i> ' + escapeHtml(p.destinatario || '—') + '</span>';
        if (stBadge) html += ' ' + stBadge;
        if (badgeOrigem) html += badgeOrigem;
        if (p.periodo) html += ' <span class="badge bg-light text-dark" style="font-size:10px;"><i class="bi bi-clock"></i> ' + escapeHtml(p.periodo) + '</span>';
        html += totalHtml + proofLink + adminBtns + btnData + editLocal;
        html += '<div class="text-muted small mt-1"><i class="bi bi-geo-alt"></i> ' + escapeHtml(p.endereco || '') + '</div>';
        if (p.telefone) html += '<div class="text-muted small"><i class="bi bi-telephone"></i> ' + escapeHtml(p.telefone) + '</div>';
        if (p.nota_driver) html += '<div class="text-muted small fst-italic mt-1"><i class="bi bi-chat-left-quote"></i> ' + escapeHtml(p.nota_driver) + '</div>';
        html += itensHtml;
        html += fotosHtml;
        html += '</div></div>';
        html += '<div class="d-print-none">';
        html += '<select class="form-select form-select-sm op-select-driver" data-code="' + escapeHtml(p.code) + '" data-driver-atual="' + (driver.id || '') + '" style="max-width:160px; font-size:12px;">';
        html += '<option value="">— Sem driver —</option>';
        (driversDisp || []).forEach(function(dd) {
            var sel = (driver.id && dd.id === driver.id) ? ' selected' : '';
            html += '<option value="' + dd.id + '"' + sel + '>' + escapeHtml(dd.nome) + '</option>';
        });
        html += '</select></div></div></div>';
        return html;
    }

    function opAtivarSortable() {
        if (typeof Sortable === 'undefined') return;
        document.querySelectorAll('.op-list').forEach(function(lst) {
            new Sortable(lst, {
                group: 'op-pedidos',
                animation: 150,
                draggable: '.op-item',
                onEnd: function() { opSalvarOrdem(); },
            });
        });
    }

    function opSalvarOrdem() {
        var items = [];
        document.querySelectorAll('.op-secao').forEach(function(sec) {
            var did = sec.dataset.driverId;
            var driverId = did ? parseInt(did, 10) : null;
            sec.querySelectorAll('.op-item').forEach(function(li, idx) {
                items.push({code: li.dataset.code, driver_id: driverId, ordem: idx, data_entrega: opData()});
            });
        });
        if (items.length === 0) return;
        fetch('/entregas/api/atribuicao/lote', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
            body: JSON.stringify({items: items}),
        }).then(function(r) { return r.json(); }).then(function(resp) {
            if (!resp.ok) {
                document.getElementById('op-msg').innerHTML = '<div class="alert alert-warning py-2 small">Falha ao salvar: ' + escapeHtml(resp.erro || '?') + '</div>';
            } else {
                opCarregar();
            }
        });
    }

    function opAtualizarBulkBar() {
        var checks = document.querySelectorAll('.op-check:checked');
        var bar = document.getElementById('op-bulk-bar');
        document.getElementById('op-bulk-count').textContent = checks.length;
        bar.style.display = checks.length > 0 ? '' : 'none';
    }

    function opRenderMapa(d) {
        var el = document.getElementById('op-mapa');
        var temCoords = (d.rotas || []).some(function(r) {
            return r.paradas.some(function(p) { return p.lat != null && p.lng != null; });
        });
        if (!temCoords) { el.style.display = 'none'; return; }
        el.style.display = '';
        if (!opMapa) {
            opMapa = L.map('op-mapa', {scrollWheelZoom: false}).setView([-23.5505, -46.6333], 11);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19, attribution: '© OpenStreetMap',
            }).addTo(opMapa);
            opMapaLayers = L.layerGroup().addTo(opMapa);
        }
        opMapaLayers.clearLayers();
        var bounds = [];
        if (d.origem && d.origem.lat && d.origem.lng) {
            L.marker([d.origem.lat, d.origem.lng], {
                icon: L.divIcon({
                    className: 'matriz-icon',
                    html: '<div style="background:#000;color:#fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:14px;border:2px solid #fff;">🏠</div>',
                    iconSize: [28, 28],
                }),
            }).addTo(opMapaLayers).bindPopup('<b>Loja matriz</b>');
            bounds.push([d.origem.lat, d.origem.lng]);
        }
        (d.rotas || []).forEach(function(r, i) {
            var cor = (r.driver && r.driver.cor) || ROTA_CORES[i % ROTA_CORES.length];
            (r.paradas || []).forEach(function(p) {
                if (p.lat == null || p.lng == null) return;
                bounds.push([p.lat, p.lng]);
                L.marker([p.lat, p.lng], {
                    icon: L.divIcon({
                        className: 'parada-icon',
                        html: '<div style="background:' + cor + ';color:#fff;border-radius:50%;width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:bold;border:2px solid #fff;">' + p.ordem + '</div>',
                        iconSize: [24, 24],
                    }),
                }).addTo(opMapaLayers).bindPopup('<b>' + escapeHtml(r.driver.nome) + ' · #' + p.ordem + '</b><br>' + escapeHtml(p.destinatario || ''));
            });
        });
        if (bounds.length > 0) opMapa.fitBounds(bounds, {padding: [40, 40]});
        setTimeout(function() { if (opMapa) opMapa.invalidateSize(); }, 80);
    }

    function opAutoDistribuir() {
        var data = opData();
        if (!data) return;
        var msg = document.getElementById('op-msg');
        msg.innerHTML = '<div class="alert alert-info py-2 small"><i class="bi bi-hourglass-split"></i> Distribuindo…</div>';
        fetch('/entregas/api/rotas?data=' + encodeURIComponent(data),
            {credentials: 'same-origin'}).then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.erro) { msg.innerHTML = '<div class="alert alert-warning py-2 small">' + escapeHtml(d.erro) + '</div>'; return; }
                var items = [];
                (d.rotas || []).forEach(function(r) {
                    (r.paradas || []).forEach(function(p, idx) {
                        items.push({code: p.code, driver_id: r.driver.id, ordem: idx, data_entrega: data});
                    });
                });
                if (items.length === 0) {
                    var nSem = (d.sem_cep || []).length;
                    var nT = d.total_pedidos || 0;
                    msg.innerHTML = '<div class="alert alert-warning py-2 small">Nada a distribuir (' + nT + ' pedidos, ' + nSem + ' sem geocode).</div>';
                    opCarregar();
                    return;
                }
                fetch('/entregas/api/atribuicao/lote', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                    body: JSON.stringify({items: items}),
                }).then(function(r) { return r.json(); }).then(function(resp) {
                    if (!resp.ok) {
                        msg.innerHTML = '<div class="alert alert-warning py-2 small">Falha ao salvar: ' + escapeHtml(resp.erro || '?') + '</div>';
                        return;
                    }
                    msg.innerHTML = '<div class="alert alert-success py-2 small"><i class="bi bi-check-circle"></i> ' + items.length + ' pedido(s) distribuído(s) e salvo(s).</div>';
                    if (opMapaVisivel) opRenderMapa(d);
                    opCarregar();
                });
            })
            .catch(function() { msg.innerHTML = '<div class="alert alert-danger py-2 small">Falha de rede.</div>'; });
    }

    function opCopiarListaWhatsApp(driverId) {
        var driver = (opUltimoResultado.drivers || []).find(function(x) { return x.id === driverId; });
        if (!driver) return;
        copiarListaWhatsApp(driver, opUltimoResultado.data);
    }

    document.addEventListener('DOMContentLoaded', function() {
        var btnHoje = document.getElementById('op-hoje');
        var btnAuto = document.getElementById('op-auto');
        var btnMapa = document.getElementById('op-mapa-toggle');
        var inpData = document.getElementById('op-data');
        var contJan = document.getElementById('op-janelas');
        var cont = document.getElementById('op-container');
        if (!cont) return;

        if (btnHoje) btnHoje.addEventListener('click', function() {
            inpData.value = new Date().toISOString().slice(0, 10);
            opCarregar();
        });
        if (btnAuto) btnAuto.addEventListener('click', opAutoDistribuir);
        if (btnMapa) btnMapa.addEventListener('click', function() {
            opMapaVisivel = !opMapaVisivel;
            document.getElementById('op-mapa-label').textContent = opMapaVisivel ? 'Esconder mapa' : 'Mostrar mapa';
            if (!opMapaVisivel) document.getElementById('op-mapa').style.display = 'none';
            opCarregar();
        });
        if (inpData) inpData.addEventListener('change', function() { opJanelaFiltro.clear(); opCarregar(); });

        if (contJan) contJan.addEventListener('change', function(e) {
            if (!e.target.matches('.op-cb-janela')) return;
            var v = e.target.value;
            if (e.target.checked) opJanelaFiltro.add(v); else opJanelaFiltro.delete(v);
            opRenderLista(opUltimoResultado, cont);
        });

        // Click handlers (delegados ao container)
        cont.addEventListener('click', function(e) {
            var b;
            if ((b = e.target.closest('.op-copiar-lista'))) {
                opCopiarListaWhatsApp(parseInt(b.dataset.driverId, 10));
                return;
            }
            if ((b = e.target.closest('.op-copiar-proof'))) {
                e.preventDefault();
                var link = window.location.origin + '/entrega/' + b.dataset.hash;
                if (navigator.clipboard) navigator.clipboard.writeText(link).then(function() {
                    var orig = b.innerHTML; b.innerHTML = '<i class="bi bi-check2"></i> Copiado!';
                    setTimeout(function() { b.innerHTML = orig; }, 1500);
                }); else prompt('Link:', link);
                return;
            }
            if ((b = e.target.closest('.op-reabrir'))) {
                e.preventDefault();
                if (!confirm('Reabrir o pedido ' + b.dataset.code + '?\nApaga as fotos e volta pra pendente.')) return;
                b.disabled = true;
                fetch('/entregas/api/entrega/' + encodeURIComponent(b.dataset.code) + '/reset', {
                    method: 'POST', headers: {'X-CSRFToken': CSRF_TOKEN}, credentials: 'same-origin',
                }).then(function(r) { return r.json(); }).then(function(d) {
                    if (!d.ok) { alert('Erro: ' + (d.erro || '?')); b.disabled = false; return; }
                    if (d.data) document.getElementById('op-data').value = d.data;
                    opCarregar();
                });
                return;
            }
            if ((b = e.target.closest('.op-mover'))) {
                e.preventDefault();
                var destino = prompt('Mover comprovante de ' + b.dataset.code + ' pra qual pedido?');
                if (!destino) return;
                destino = destino.trim();
                if (!destino || destino === b.dataset.code) return;
                b.disabled = true;
                fetch('/entregas/api/entrega/' + encodeURIComponent(b.dataset.code) + '/migrar', {
                    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                    credentials: 'same-origin', body: JSON.stringify({destino: destino}),
                }).then(function(r) { return r.json(); }).then(function(d) {
                    if (!d.ok) { alert('Erro: ' + (d.erro || '?')); b.disabled = false; return; }
                    alert(d.fotos_movidas + ' foto(s) movida(s) pra ' + d.destino);
                    if (d.data) document.getElementById('op-data').value = d.data;
                    opCarregar();
                });
                return;
            }
        });

        // Select de driver inline + checkboxes bulk
        cont.addEventListener('change', function(e) {
            if (e.target.matches('.op-select-driver')) {
                var code = e.target.dataset.code;
                var novo = e.target.value;
                var url = '/entregas/api/atribuicao/' + encodeURIComponent(code);
                if (!novo) {
                    fetch(url, {method: 'DELETE', headers: {'X-CSRFToken': CSRF_TOKEN}, credentials: 'same-origin'})
                        .then(function(r) { return r.json(); }).then(function() { opCarregar(); });
                } else {
                    fetch(url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                        credentials: 'same-origin',
                        body: JSON.stringify({driver_id: parseInt(novo, 10), data_entrega: opData()}),
                    }).then(function(r) { return r.json(); }).then(function() { opCarregar(); });
                }
                return;
            }
            if (e.target.matches('.op-check')) { opAtualizarBulkBar(); return; }
            if (e.target.matches('.op-check-secao')) {
                var sec = e.target.closest('.op-secao');
                if (sec) sec.querySelectorAll('.op-check').forEach(function(cb) { cb.checked = e.target.checked; });
                opAtualizarBulkBar();
                return;
            }
        });

        // Bulk: aplicar
        var bulkAplicar = document.getElementById('op-bulk-aplicar');
        if (bulkAplicar) bulkAplicar.addEventListener('click', function() {
            var sel = document.getElementById('op-bulk-driver');
            var driverId = sel.value ? parseInt(sel.value, 10) : null;
            var checks = document.querySelectorAll('.op-check:checked');
            if (checks.length === 0) return;
            var items = [];
            checks.forEach(function(cb, idx) {
                items.push({code: cb.dataset.code, driver_id: driverId, ordem: idx, data_entrega: opData()});
            });
            fetch('/entregas/api/atribuicao/lote', {
                method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                credentials: 'same-origin', body: JSON.stringify({items: items}),
            }).then(function(r) { return r.json(); }).then(function() { opCarregar(); });
        });
        var bulkLimpar = document.getElementById('op-bulk-limpar');
        if (bulkLimpar) bulkLimpar.addEventListener('click', function() {
            document.querySelectorAll('.op-check, .op-check-secao').forEach(function(cb) { cb.checked = false; });
            opAtualizarBulkBar();
        });

        // Busca por cliente/code
        var busca = document.getElementById('op-busca');
        if (busca) busca.addEventListener('input', function() {
            opBuscaTexto = this.value.trim();
            if (opUltimoResultado) opRenderLista(opUltimoResultado, cont);
        });

        // Mudar data (override) — handler usa a funcao window.abrirAlterarData ja existente
        cont.addEventListener('click', function(e) {
            var b = e.target.closest('.op-mudar-data');
            if (!b) return;
            e.preventDefault();
            var code = b.dataset.code;
            // Pega o pedido no resultado pra ler override info
            var pedido = null;
            (opUltimoResultado.drivers || []).forEach(function(dr) {
                (dr.paradas || []).forEach(function(p) { if (p.code === code) pedido = p; });
            });
            (opUltimoResultado.sem_driver || []).forEach(function(p) { if (p.code === code) pedido = p; });
            if (!pedido) return;
            // Reaproveita o modal: precisa popular pedidos[] global pra abrirAlterarData encontrar
            window.pedidos = window.pedidos || [];
            var idx = -1;
            for (var i = 0; i < window.pedidos.length; i++) if (window.pedidos[i].code === code) { idx = i; break; }
            if (idx === -1) window.pedidos.push(pedido);
            else window.pedidos[idx] = pedido;
            var dataAtual = pedido.data_entrega || opData();
            var jaTem = !!pedido.data_override;
            window.abrirAlterarData(code, dataAtual, jaTem);
        });

        // Novo pedido / Editar pedido local
        var btnNovo = document.getElementById('op-novo-pedido');
        if (btnNovo) btnNovo.addEventListener('click', function() { abrirModalPedidoLocal(null); });
        cont.addEventListener('click', function(e) {
            var b = e.target.closest('.op-editar-local');
            if (!b) return;
            e.preventDefault();
            abrirModalPedidoLocal(parseInt(b.dataset.id, 10));
        });

        // Expor recarregar pra outros handlers (modal alterar-data)
        window.opRecarregar = opCarregar;

        // Carrega ao abrir a aba pela primeira vez
        var tabBtn = document.getElementById('btn-tab-operacao');
        var carregada = false;
        function bootOp() { if (!carregada) { carregada = true; opCarregar(); } }
        if (tabBtn) tabBtn.addEventListener('shown.bs.tab', bootOp);
        // Como a aba é a default ativa, carrega já
        bootOp();
    });

    // ── Modal: novo/editar pedido local ──
    function npLimparItens() {
        document.getElementById('np-itens').innerHTML = '';
    }

    function npAdicionarItem(item) {
        item = item || {};
        var div = document.createElement('div');
        div.className = 'np-item d-flex gap-2 mb-2';
        div.innerHTML =
            '<input class="form-control form-control-sm np-i-nome" placeholder="Nome do item" value="' + escapeHtml(item.nome || '') + '" style="flex:2;">' +
            '<input class="form-control form-control-sm np-i-qtd" type="number" min="1" placeholder="Qtd" value="' + (item.quantidade || 1) + '" style="max-width:70px;">' +
            '<input class="form-control form-control-sm np-i-preco" type="number" step="0.01" min="0" placeholder="Preço" value="' + (item.preco_unitario || 0) + '" style="max-width:100px;">' +
            '<button type="button" class="btn btn-sm btn-outline-danger np-i-remover" title="Remover"><i class="bi bi-x"></i></button>';
        document.getElementById('np-itens').appendChild(div);
        npRecalcular();
    }

    function npRecalcular() {
        var total = 0;
        document.querySelectorAll('#np-itens .np-item').forEach(function(div) {
            var q = parseInt(div.querySelector('.np-i-qtd').value, 10) || 0;
            var p = parseFloat(div.querySelector('.np-i-preco').value) || 0;
            total += q * p;
        });
        document.getElementById('np-total').textContent = total.toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }

    function abrirModalPedidoLocal(id) {
        document.getElementById('np-id').value = id || '';
        document.getElementById('np-destinatario').value = '';
        document.getElementById('np-telefone').value = '';
        document.getElementById('np-endereco').value = '';
        document.getElementById('np-data').value = document.getElementById('op-data').value;
        document.getElementById('np-periodo').value = '';
        document.getElementById('np-cartinha').value = '';
        document.getElementById('np-observacao').value = '';
        npLimparItens();
        var btnDel = document.getElementById('np-deletar');
        btnDel.classList.add('d-none');

        if (id) {
            // Carregar do servidor
            fetch('/entregas/api/pedido-local/' + id, {credentials: 'same-origin'})
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (!d.ok) { alert('Erro: ' + (d.erro || '?')); return; }
                    var p = d.pedido;
                    document.getElementById('np-destinatario').value = p.destinatario || '';
                    document.getElementById('np-telefone').value = p.telefone || '';
                    document.getElementById('np-endereco').value = p.endereco || '';
                    document.getElementById('np-data').value = p.data_entrega || '';
                    document.getElementById('np-periodo').value = p.periodo || '';
                    document.getElementById('np-cartinha').value = p.cartinha_vnda || '';
                    document.getElementById('np-observacao').value = p.observacao || '';
                    npLimparItens();
                    (p.itens || []).forEach(npAdicionarItem);
                    if ((p.itens || []).length === 0) npAdicionarItem();
                    btnDel.classList.remove('d-none');
                    bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-novo-pedido')).show();
                });
        } else {
            npAdicionarItem();
            bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-novo-pedido')).show();
        }
    }

    document.addEventListener('DOMContentLoaded', function() {
        var modal = document.getElementById('modal-novo-pedido');
        if (!modal) return;

        document.getElementById('np-add-item').addEventListener('click', function() { npAdicionarItem(); });

        document.getElementById('np-itens').addEventListener('input', npRecalcular);
        document.getElementById('np-itens').addEventListener('click', function(e) {
            var b = e.target.closest('.np-i-remover');
            if (!b) return;
            b.closest('.np-item').remove();
            npRecalcular();
        });

        document.getElementById('np-salvar').addEventListener('click', function() {
            var id = document.getElementById('np-id').value;
            var itens = [];
            document.querySelectorAll('#np-itens .np-item').forEach(function(div) {
                var nome = div.querySelector('.np-i-nome').value.trim();
                if (!nome) return;
                itens.push({
                    nome: nome,
                    quantidade: parseInt(div.querySelector('.np-i-qtd').value, 10) || 1,
                    preco_unitario: parseFloat(div.querySelector('.np-i-preco').value) || 0,
                });
            });
            var body = {
                id: id ? parseInt(id, 10) : null,
                destinatario: document.getElementById('np-destinatario').value.trim(),
                telefone: document.getElementById('np-telefone').value.trim(),
                endereco: document.getElementById('np-endereco').value.trim(),
                data_entrega: document.getElementById('np-data').value,
                periodo: document.getElementById('np-periodo').value.trim(),
                cartinha: document.getElementById('np-cartinha').value.trim(),
                observacao: document.getElementById('np-observacao').value.trim(),
                itens: itens,
            };
            if (!body.destinatario || !body.telefone || !body.endereco || !body.data_entrega) {
                alert('Preencha destinatário, telefone, endereço e data.'); return;
            }
            if (itens.length === 0) { alert('Adicione pelo menos um item.'); return; }
            fetch('/entregas/api/pedido-local', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                credentials: 'same-origin',
                body: JSON.stringify(body),
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (!d.ok) { alert('Erro: ' + (d.erro || '?')); return; }
                bootstrap.Modal.getInstance(modal).hide();
                if (d.pedido && d.pedido.data_entrega) document.getElementById('op-data').value = d.pedido.data_entrega;
                if (typeof window.opRecarregar === 'function') window.opRecarregar();
            });
        });

        document.getElementById('np-deletar').addEventListener('click', function() {
            var id = document.getElementById('np-id').value;
            if (!id) return;
            if (!confirm('Deletar este pedido manual?')) return;
            fetch('/entregas/api/pedido-local/' + id, {
                method: 'DELETE', headers: {'X-CSRFToken': CSRF_TOKEN}, credentials: 'same-origin',
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (!d.ok) { alert('Erro: ' + (d.erro || '?')); return; }
                bootstrap.Modal.getInstance(modal).hide();
                if (typeof window.opRecarregar === 'function') window.opRecarregar();
            });
        });
    });

    // ── Init ──
    limparAntigos();
    carregarPedidos();
})();
