/* ═══════════════════════════════════════════
   Padaria — JavaScript Principal
   ═══════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', function () {

    // ═══ MENU MOBILE (HAMBURGER) ═══
    var btnToggle = document.getElementById('btn-sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebar-overlay');

    if (btnToggle && sidebar && overlay) {
        btnToggle.addEventListener('click', function () {
            sidebar.classList.toggle('open');
            overlay.classList.toggle('active');
        });
        overlay.addEventListener('click', function () {
            sidebar.classList.remove('open');
            overlay.classList.remove('active');
        });
        // Fechar sidebar ao clicar em um link (navegação mobile)
        sidebar.querySelectorAll('a.sidebar-link').forEach(function (link) {
            link.addEventListener('click', function () {
                sidebar.classList.remove('open');
                overlay.classList.remove('active');
            });
        });
    }

    // ═══ BUSCA NA SIDEBAR ═══
    var sidebarBusca = document.getElementById('sidebar-busca');
    if (sidebarBusca) {
        sidebarBusca.addEventListener('input', function () {
            var termo = this.value.toLowerCase().trim();
            var receitas = document.querySelectorAll('[data-recipe]');
            var cats = document.querySelectorAll('[data-cat]');

            if (!termo) {
                receitas.forEach(function (r) { r.style.display = ''; });
                cats.forEach(function (c) { c.style.display = ''; });
                return;
            }

            cats.forEach(function (c) { c.style.display = 'none'; });

            receitas.forEach(function (r) {
                var nome = r.textContent.toLowerCase().trim();
                if (nome.indexOf(termo) !== -1) {
                    r.style.display = '';
                    var prev = r.previousElementSibling;
                    while (prev) {
                        if (prev.hasAttribute('data-cat')) {
                            prev.style.display = '';
                            break;
                        }
                        prev = prev.previousElementSibling;
                    }
                } else {
                    r.style.display = 'none';
                }
            });
        });
    }


    // ═══ FICHA TÉCNICA ═══
    var fichaBody = document.getElementById('ficha-body');
    var pesoBaseInput = document.getElementById('peso-base');
    var rendimentoInput = document.getElementById('rendimento-qtd');
    var pesoUnitarioInput = document.getElementById('peso-unitario');
    var modoSelect = document.getElementById('modo-lancamento');
    var ingTemplate = document.getElementById('ing-row-template');
    var btnAddIng = document.getElementById('btn-add-ing');
    var multiplicadorInput = document.getElementById('multiplicador');
    var perdaInput = document.getElementById('perda-percentual');
    var precoVendaInput = document.getElementById('preco-venda');
    var precoLojaInput = document.getElementById('preco-loja');
    var precoSiteInput = document.getElementById('preco-site');
    var custoEmbalagemInput = document.getElementById('custo-embalagem');

    if (fichaBody && pesoBaseInput) {

        // ── Aviso de alterações não salvas ──
        var fichaForm = document.getElementById('ficha-form');
        var formAlterado = false;

        if (fichaForm) {
            fichaForm.addEventListener('input', function () {
                formAlterado = true;
            });

            fichaForm.addEventListener('submit', function () {
                formAlterado = false;
            });

            window.addEventListener('beforeunload', function (e) {
                if (formAlterado) {
                    e.preventDefault();
                    e.returnValue = '';
                }
            });
        }

        // Eventos de input
        pesoBaseInput.addEventListener('input', recalcularTudo);
        rendimentoInput.addEventListener('input', recalcularTudo);
        if (pesoUnitarioInput) pesoUnitarioInput.addEventListener('input', recalcularTudo);
        if (multiplicadorInput) multiplicadorInput.addEventListener('input', recalcularTudo);
        if (perdaInput) perdaInput.addEventListener('input', recalcularTudo);
        if (precoVendaInput) precoVendaInput.addEventListener('input', recalcularTudo);
        if (precoLojaInput) precoLojaInput.addEventListener('input', recalcularTudo);
        if (precoSiteInput) precoSiteInput.addEventListener('input', recalcularTudo);
        if (custoEmbalagemInput) custoEmbalagemInput.addEventListener('input', recalcularTudo);

        // Trocar modo
        if (modoSelect) {
            modoSelect.addEventListener('change', function () {
                aplicarModo();
                recalcularTudo();
            });
            aplicarModo();
        }

        // Delegação de eventos para inputs dinâmicos
        fichaBody.addEventListener('input', function (e) {
            if (e.target.classList.contains('pct-input') || e.target.classList.contains('nome-input')) {
                recalcularTudo();
            }
        });

        // Trocar tipo de ingrediente (MP ↔ Receita): alternar datalist e recalcular
        fichaBody.addEventListener('change', function (e) {
            if (e.target.classList.contains('ing-tipo')) {
                var row = e.target.closest('.ingrediente-row');
                var nomeInput = row.querySelector('.nome-input');
                nomeInput.setAttribute('list', e.target.value === 'receita' ? 'receita-list' : 'mp-list');
                nomeInput.value = '';
                recalcularTudo();
            }
        });

        // Remover ingrediente
        fichaBody.addEventListener('click', function (e) {
            var btn = e.target.closest('.btn-remove-ing');
            if (btn) {
                btn.closest('.ingrediente-row').remove();
                recalcularTudo();
            }
        });

        // Adicionar ingrediente
        if (btnAddIng && ingTemplate) {
            btnAddIng.addEventListener('click', function () {
                var clone = ingTemplate.content.cloneNode(true);
                var pctInput = clone.querySelector('.pct-input');
                if (pctInput && !pctTravado) {
                    pctInput.readOnly = false;
                    pctInput.classList.remove('pct-locked');
                    pctInput.classList.add('pct-unlocked');
                }
                fichaBody.appendChild(clone);
            });
        }

        // Cadeado de % Padeiro
        var pctTravado = true;
        var btnLock = document.getElementById('btn-lock-pct');
        if (btnLock) {
            btnLock.addEventListener('click', function () {
                pctTravado = !pctTravado;
                var icon = btnLock.querySelector('i');
                if (pctTravado) {
                    icon.className = 'bi bi-lock-fill';
                    btnLock.classList.remove('unlocked');
                    btnLock.title = 'Destravar para editar receita';
                } else {
                    icon.className = 'bi bi-unlock-fill';
                    btnLock.classList.add('unlocked');
                    btnLock.title = 'Travar receita';
                }
                document.querySelectorAll('.pct-input').forEach(function (input) {
                    input.readOnly = pctTravado;
                    if (pctTravado) {
                        input.classList.add('pct-locked');
                        input.classList.remove('pct-unlocked');
                    } else {
                        input.classList.remove('pct-locked');
                        input.classList.add('pct-unlocked');
                    }
                });
            });
        }

        // Modal: cadastrar nova MP sem sair da ficha
        var btnSalvarMPFicha = document.getElementById('btn-salvar-nova-mp-ficha');
        if (btnSalvarMPFicha) {
            btnSalvarMPFicha.addEventListener('click', function () {
                var nome = document.getElementById('nova-mp-nome').value.trim();
                var custo = document.getElementById('nova-mp-custo').value.trim();
                var erroEl = document.getElementById('nova-mp-erro');
                var okEl = document.getElementById('nova-mp-ok');

                erroEl.style.display = 'none';
                okEl.style.display = 'none';

                if (!nome || !custo) {
                    erroEl.textContent = 'Preencha nome e custo.';
                    erroEl.style.display = 'block';
                    return;
                }

                var formData = new FormData();
                formData.append('mp_nome', nome);
                formData.append('mp_custo', custo);
                formData.append('csrf_token', CSRF_TOKEN);

                fetch('/receitas/api/nova-mp', { method: 'POST', body: formData })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.success) {
                            MP_DATA[data.nome] = { custo_por_kg: data.custo, unidade: 'g' };

                            var mpList = document.getElementById('mp-list');
                            var opt = document.createElement('option');
                            opt.value = data.nome;
                            mpList.appendChild(opt);

                            okEl.textContent = '"' + data.nome + '" cadastrado! Ja pode usar na ficha.';
                            okEl.style.display = 'block';

                            document.getElementById('nova-mp-nome').value = '';
                            document.getElementById('nova-mp-custo').value = '';

                            recalcularTudo();
                        } else {
                            erroEl.textContent = data.error;
                            erroEl.style.display = 'block';
                        }
                    })
                    .catch(function () {
                        erroEl.textContent = 'Erro ao salvar. Tente novamente.';
                        erroEl.style.display = 'block';
                    });
            });
        }

        // Calcular ao carregar
        recalcularTudo();
    }

    function aplicarModo() {
        var modo = modoSelect ? modoSelect.value : 'farinha';
        var boxFarinha = document.getElementById('box-farinha');
        var boxQtd = document.getElementById('box-quantidade');

        if (modo === 'farinha') {
            pesoBaseInput.readOnly = false;
            pesoBaseInput.classList.remove('calc-readonly');
            rendimentoInput.readOnly = true;
            rendimentoInput.classList.add('calc-readonly');
            if (boxFarinha) boxFarinha.style.order = '1';
            if (boxQtd) boxQtd.style.order = '2';
        } else {
            pesoBaseInput.readOnly = true;
            pesoBaseInput.classList.add('calc-readonly');
            rendimentoInput.readOnly = false;
            rendimentoInput.classList.remove('calc-readonly');
            if (boxFarinha) boxFarinha.style.order = '2';
            if (boxQtd) boxQtd.style.order = '1';
        }
    }

    function _custoPorGrama(mp) {
        // Converte custo da MP em "R$ por grama" considerando como ela foi cadastrada.
        // - g/ml: custo_por_kg / 1000
        // - un com peso_unidade preenchido: custo_por_un / peso_unidade
        // - un sem peso_unidade: 0 (nao da pra converter)
        if (!mp) return 0;
        if (mp.unidade === 'g' || mp.unidade === 'ml') {
            return (mp.custo_por_kg || 0) / 1000;
        }
        if (mp.unidade === 'un' && mp.peso_unidade && mp.peso_unidade > 0) {
            return (mp.custo_por_kg || 0) / mp.peso_unidade;
        }
        return 0;
    }

    function recalcularTudo() {
        var modo = modoSelect ? modoSelect.value : 'farinha';
        var pesoUnit = pesoUnitarioInput ? (parseFloat(pesoUnitarioInput.value) || 0) : 0;
        var multiplicador = multiplicadorInput ? (parseInt(multiplicadorInput.value) || 1) : 1;
        if (multiplicador < 1) multiplicador = 1;
        var perda = perdaInput ? (parseFloat(perdaInput.value) || 0) : 0;
        if (perda < 0) perda = 0;
        if (perda > 50) perda = 50;

        // Passo 1: Soma das porcentagens (só MP % contribuem para peso/% padeiro)
        var sumPct = 0;
        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            var tipoSel = row.querySelector('.ing-tipo');
            var tipo = tipoSel ? tipoSel.value : 'mp';
            if (tipo === 'mp') {
                var pctInput = row.querySelector('.pct-input');
                sumPct += parseFloat(pctInput ? pctInput.value : 0) || 0;
            }
        });

        // Passo 2: Determinar peso_base e rendimento conforme o modo
        var pesoBase, rendimento;

        if (modo === 'quantidade' && pesoUnit > 0 && sumPct > 0) {
            rendimento = parseFloat(rendimentoInput.value) || 0;
            pesoBase = rendimento * pesoUnit * 100 / sumPct;
            pesoBaseInput.value = Math.round(pesoBase);
        } else {
            pesoBase = parseFloat(pesoBaseInput.value) || 0;
            rendimento = parseFloat(rendimentoInput.value) || 1;
        }

        // Passo 3: Calcular ingredientes (para 1 fornada)
        var totalPct = 0;
        var totalQtd = 0;
        var totalCusto = 0;

        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            var nomeInput = row.querySelector('.nome-input');
            var pctInput = row.querySelector('.pct-input');
            var qtdCell = row.querySelector('.qtd-calc');
            var custoKgCell = row.querySelector('.custo-kg-calc');
            var custoRsCell = row.querySelector('.custo-rs-calc');
            var tipoSel = row.querySelector('.ing-tipo');

            var nome = nomeInput ? nomeInput.value.trim() : '';
            var pct = parseFloat(pctInput ? pctInput.value : 0) || 0;
            var tipo = tipoSel ? tipoSel.value : 'mp';

            var qtd, custoRs, custoKg;

            if (tipo === 'receita') {
                // Sub-receita: porcentagem = quantidade de unidades
                var custoUnitReceita = (typeof RECEITA_CUSTOS !== 'undefined' && RECEITA_CUSTOS[nome]) || 0;
                var pesoUnitReceita = (typeof RECEITA_PESOS !== 'undefined' && RECEITA_PESOS[nome]) || 0;
                qtd = pct * pesoUnitReceita;  // peso total = unidades × peso unitário
                custoRs = custoUnitReceita * pct;

                var qtdExibir = qtd * multiplicador;
                var custoExibir = custoRs * multiplicador;

                qtdCell.textContent = qtdExibir > 0 ? formatNum(qtdExibir, 1) + 'g' : pct > 0 ? pct + ' un' : '-';
                custoKgCell.textContent = custoUnitReceita > 0 ? formatBRL(custoUnitReceita) + '/un' : '-';
                custoKgCell.className = custoUnitReceita > 0 ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
                custoRsCell.textContent = custoExibir > 0 ? formatBRL(custoExibir) : '-';

                // Sub-receitas não contribuem para % padeiro, mas contribuem para peso e custo
                totalQtd += qtd;
                totalCusto += custoRs;
            } else if (tipo === 'mp_direto') {
                // MP com quantidade em gramas direto (não usa % padeiro)
                qtd = pct;  // pct é na verdade gramas
                var mp = MP_DATA[nome];
                custoKg = mp ? mp.custo_por_kg : 0;
                custoRs = qtd * _custoPorGrama(mp);

                var qtdExibir = qtd * multiplicador;
                var custoExibir = custoRs * multiplicador;

                qtdCell.textContent = qtdExibir > 0 ? formatNum(qtdExibir, 1) : '-';
                custoKgCell.textContent = mp ? formatBRL(custoKg) : '-';
                custoKgCell.className = mp ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
                custoRsCell.textContent = custoExibir > 0 ? formatBRL(custoExibir) : '-';

                // MP direto não contribui para % padeiro, mas contribui para peso e custo
                totalQtd += qtd;
                totalCusto += custoRs;
            } else {
                // MP normal: % padeiro
                qtd = pesoBase * pct / 100;
                var mp = MP_DATA[nome];
                custoKg = mp ? mp.custo_por_kg : 0;
                custoRs = qtd * _custoPorGrama(mp);

                var qtdExibir = qtd * multiplicador;
                var custoExibir = custoRs * multiplicador;

                qtdCell.textContent = qtdExibir > 0 ? formatNum(qtdExibir, 1) : '-';
                custoKgCell.textContent = mp ? formatBRL(custoKg) : '-';
                custoKgCell.className = mp ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
                custoRsCell.textContent = custoExibir > 0 ? formatBRL(custoExibir) : '-';

                totalPct += pct;
                totalQtd += qtd;
                totalCusto += custoRs;
            }
        });

        // Aplicar multiplicador aos totais
        var totalQtdMult = totalQtd * multiplicador;
        var totalCustoMult = totalCusto * multiplicador;

        // Aplicar perda de rendimento ao peso
        var pesoAposPerda = totalQtdMult * (1 - perda / 100);

        // Passo 4: Se modo farinha e peso_unitario preenchido, calcular rendimento
        if (modo === 'farinha' && pesoUnit > 0 && pesoAposPerda > 0) {
            rendimento = Math.floor(pesoAposPerda / pesoUnit);
            rendimentoInput.value = rendimento;
        } else {
            rendimento = rendimento * multiplicador;
        }

        // Totais na tabela
        var elPct = document.getElementById('total-pct');
        var elQtd = document.getElementById('total-qtd');
        var elCusto = document.getElementById('total-custo');
        if (elPct) elPct.textContent = formatNum(totalPct, 1) + '%';
        if (elQtd) elQtd.textContent = formatNum(totalQtdMult, 0) + 'g';
        if (elCusto) elCusto.textContent = formatBRL(totalCustoMult);

        // Resumo (custo producao + embalagem por unidade)
        var custoEmbalagem = parseFloat((document.getElementById('custo-embalagem') || {}).value) || 0;
        var custoUn = rendimento > 0 ? (totalCustoMult / rendimento) + custoEmbalagem : 0;

        var rPeso = document.getElementById('resumo-peso');
        var rCusto = document.getElementById('resumo-custo');
        var rUn = document.getElementById('resumo-unidades');
        var rCustoUn = document.getElementById('resumo-custo-un');
        if (rPeso) {
            if (perda > 0) {
                rPeso.textContent = formatNum(pesoAposPerda, 0) + 'g';
                rPeso.title = 'Massa: ' + formatNum(totalQtdMult, 0) + 'g - Perda ' + perda + '%';
            } else {
                rPeso.textContent = formatNum(totalQtdMult, 0) + 'g';
                rPeso.title = '';
            }
        }
        if (rCusto) rCusto.textContent = formatBRL(totalCustoMult);
        if (rUn) rUn.textContent = rendimento;
        if (rCustoUn) rCustoUn.textContent = rendimento > 0 ? formatBRL(custoUn) : '-';

        // Rentabilidade — função auxiliar
        function calcRent(preco, sufixo) {
            var el = document.getElementById('resumo-preco-' + sufixo);
            var elM = document.getElementById('resumo-margem-' + sufixo);
            var elL = document.getElementById('resumo-lucro-un-' + sufixo);
            var elT = document.getElementById('resumo-lucro-total-' + sufixo);

            if (el) el.textContent = preco > 0 ? formatBRL(preco) : '-';

            if (preco > 0 && custoUn > 0) {
                var lucro = preco - custoUn;
                var marg = (lucro / preco) * 100;
                var lucroT = lucro * rendimento;

                if (elM) {
                    elM.textContent = formatNum(marg, 1) + '%';
                    elM.className = marg >= 50 ? 'resumo-valor text-success' : marg >= 20 ? 'resumo-valor text-warning' : 'resumo-valor text-danger';
                }
                if (elL) {
                    elL.textContent = formatBRL(lucro);
                    elL.className = lucro >= 0 ? 'resumo-valor text-success' : 'resumo-valor text-danger';
                }
                if (elT) {
                    elT.textContent = formatBRL(lucroT);
                    elT.className = lucroT >= 0 ? 'resumo-valor text-success' : 'resumo-valor text-danger';
                }
            } else {
                if (elM) { elM.textContent = '-'; elM.className = 'resumo-valor'; }
                if (elL) { elL.textContent = '-'; elL.className = 'resumo-valor'; }
                if (elT) { elT.textContent = '-'; elT.className = 'resumo-valor'; }
            }
        }

        var precoVenda = precoVendaInput ? (parseFloat(precoVendaInput.value) || 0) : 0;
        var precoLoja = precoLojaInput ? (parseFloat(precoLojaInput.value) || 0) : 0;
        var precoSite = precoSiteInput ? (parseFloat(precoSiteInput.value) || 0) : 0;
        calcRent(precoVenda, 'venda');
        calcRent(precoLoja, 'loja');
        calcRent(precoSite, 'site');
    }


    // ═══ BANCO DE MP ═══
    var mpBody = document.getElementById('mp-body');
    var mpTemplate = document.getElementById('mp-row-template');
    var btnAddMp = document.getElementById('btn-add-mp');

    if (mpBody) {
        if (btnAddMp && mpTemplate) {
            btnAddMp.addEventListener('click', function () {
                var clone = mpTemplate.content.cloneNode(true);
                mpBody.appendChild(clone);
            });
        }

        mpBody.addEventListener('click', function (e) {
            var btn = e.target.closest('.btn-del-mp');
            if (btn) {
                var mpId = btn.dataset.id;
                if (confirm('Excluir esta materia-prima?')) {
                    var form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '/materias-primas/excluir/' + mpId;
                    var csrf = document.createElement('input');
                    csrf.type = 'hidden';
                    csrf.name = 'csrf_token';
                    csrf.value = CSRF_TOKEN;
                    form.appendChild(csrf);
                    document.body.appendChild(form);
                    form.submit();
                }
            }

            var btnNew = e.target.closest('.btn-del-new');
            if (btnNew) {
                btnNew.closest('tr').remove();
            }
        });
    }


    // ═══ LISTA DE PRODUTOS (exclusão) ═══
    document.addEventListener('click', function (e) {
        var btn = e.target.closest('.btn-del-prod');
        if (btn) {
            var prodId = btn.dataset.id;
            if (confirm('Excluir este produto?')) {
                var form = document.createElement('form');
                form.method = 'POST';
                form.action = '/produtos/excluir/' + prodId;
                var csrf = document.createElement('input');
                csrf.type = 'hidden';
                csrf.name = 'csrf_token';
                csrf.value = CSRF_TOKEN;
                form.appendChild(csrf);
                document.body.appendChild(form);
                form.submit();
            }
        }
    });


    // ═══ DETALHE CESTA ═══
    var cestaBody = document.getElementById('cesta-body');
    var cestaTemplate = document.getElementById('item-row-template');
    var btnAddItem = document.getElementById('btn-add-item');

    if (cestaBody) {

        function _findMp(nome) {
            if (typeof MP_DATA === 'undefined') return null;
            if (MP_DATA[nome]) return MP_DATA[nome];
            var alvo = (nome || '').trim().toLowerCase();
            if (!alvo) return null;
            for (var k in MP_DATA) {
                if (k.trim().toLowerCase() === alvo) return MP_DATA[k];
            }
            return null;
        }

        function getCustoItem(tipo, nome) {
            if (tipo === 'receita') {
                return (typeof RECEITA_CUSTOS !== 'undefined' && RECEITA_CUSTOS[nome]) || 0;
            } else {
                var mp = _findMp(nome);
                if (!mp) return 0;
                if (mp.unidade === 'g' || mp.unidade === 'ml') {
                    return mp.custo_por_kg / 1000;
                }
                return mp.custo_por_kg;
            }
        }

        function formatBrl(val) {
            return 'R$ ' + val.toFixed(2).replace('.', ',');
        }

        function recalcularCesta() {
            var rows = cestaBody.querySelectorAll('tr');
            var custoTotal = 0;

            rows.forEach(function (row) {
                var tipo = row.querySelector('.item-tipo');
                var nome = row.querySelector('.item-nome');
                var qtd = row.querySelector('.item-qtd');
                var custoUnCell = row.querySelector('.item-custo-un');
                var custoTotalCell = row.querySelector('.item-custo-total');

                if (!tipo || !nome || !qtd) return;

                var custoUn = getCustoItem(tipo.value, nome.value);
                var quantidade = parseFloat(qtd.value) || 0;
                var custoLinha = custoUn * quantidade;
                custoTotal += custoLinha;

                // Mostrar custo/kg para itens em gramas, custo/un para unitários
                if (tipo.value === 'mp') {
                    var mpData = _findMp(nome.value);
                    if (mpData && (mpData.unidade === 'g' || mpData.unidade === 'ml')) {
                        custoUnCell.textContent = mpData.custo_por_kg > 0 ? formatBrl(mpData.custo_por_kg) + '/kg' : '-';
                    } else {
                        custoUnCell.textContent = custoUn > 0 ? formatBrl(custoUn) : '-';
                    }
                } else {
                    custoUnCell.textContent = custoUn > 0 ? formatBrl(custoUn) : '-';
                }
                custoTotalCell.textContent = custoLinha > 0 ? formatBrl(custoLinha) : '-';
            });

            // Se não tem composição, usar custo direto
            if (custoTotal === 0) {
                var custoDireto = parseFloat((document.getElementById('custo_direto') || {}).value) || 0;
                custoTotal = custoDireto;
            }

            // Somar embalagem
            var custoEmbalagem = parseFloat((document.getElementById('custo_embalagem') || {}).value) || 0;
            custoTotal += custoEmbalagem;

            document.getElementById('custo-total-cesta').textContent = formatBrl(custoTotal);

            // Resumo financeiro
            var canais = [
                { input: 'preco_atacado', el: 'resumo-atacado' },
                { input: 'preco_loja', el: 'resumo-loja' },
                { input: 'preco_site', el: 'resumo-site' },
            ];

            canais.forEach(function (c) {
                var preco = parseFloat(document.getElementById(c.input).value) || 0;
                var el = document.getElementById(c.el);
                if (preco > 0 && custoTotal > 0) {
                    var lucro = preco - custoTotal;
                    var margem = (lucro / preco * 100).toFixed(1);
                    var cor = lucro >= 0 ? '#2e7d32' : '#c62828';
                    el.innerHTML = formatBrl(preco) + ' &mdash; Lucro: <span style="color:' + cor + '">' + formatBrl(lucro) + '</span> (' + margem + '%)';
                } else {
                    el.textContent = '-';
                }
            });
        }

        // Adicionar item
        if (btnAddItem && cestaTemplate) {
            btnAddItem.addEventListener('click', function () {
                var clone = cestaTemplate.content.cloneNode(true);
                cestaBody.appendChild(clone);
                recalcularCesta();
            });
        }

        // Delegação de eventos na tabela
        cestaBody.addEventListener('click', function (e) {
            var btn = e.target.closest('.btn-remove-item');
            if (btn) {
                btn.closest('tr').remove();
                recalcularCesta();
            }
        });

        cestaBody.addEventListener('change', function (e) {
            // Alternar datalist quando tipo muda
            if (e.target.classList.contains('item-tipo')) {
                var row = e.target.closest('tr');
                var nomeInput = row.querySelector('.item-nome');
                nomeInput.setAttribute('list', e.target.value === 'receita' ? 'receita-list' : 'mp-list');
                nomeInput.value = '';
                recalcularCesta();
            }
            // Recalcular ao mudar nome ou quantidade
            if (e.target.classList.contains('item-nome') || e.target.classList.contains('item-qtd')) {
                recalcularCesta();
            }
        });

        cestaBody.addEventListener('input', function (e) {
            if (e.target.classList.contains('item-nome') || e.target.classList.contains('item-qtd')) {
                recalcularCesta();
            }
        });

        // Recalcular ao mudar precos
        ['preco_atacado', 'preco_loja', 'preco_site', 'custo_direto', 'custo_embalagem'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('input', recalcularCesta);
        });

        // Excluir cesta
        var btnExcluir = document.getElementById('btn-excluir-cesta');
        if (btnExcluir) {
            btnExcluir.addEventListener('click', function () {
                if (confirm('Excluir esta cesta? Isso nao pode ser desfeito.')) {
                    var form = document.createElement('form');
                    form.method = 'POST';
                    form.action = '/produtos/excluir/' + window.location.pathname.split('/').pop();
                    var csrf = document.createElement('input');
                    csrf.type = 'hidden';
                    csrf.name = 'csrf_token';
                    csrf.value = CSRF_TOKEN;
                    form.appendChild(csrf);
                    document.body.appendChild(form);
                    form.submit();
                }
            });
        }

        // Modal: cadastrar nova MP sem sair da página
        var btnSalvarMP = document.getElementById('btn-salvar-nova-mp');
        if (btnSalvarMP) {
            btnSalvarMP.addEventListener('click', function () {
                var nome = document.getElementById('nova-mp-nome').value.trim();
                var custo = document.getElementById('nova-mp-custo').value.trim();
                var erroEl = document.getElementById('nova-mp-erro');
                var okEl = document.getElementById('nova-mp-ok');

                erroEl.style.display = 'none';
                okEl.style.display = 'none';

                if (!nome || !custo) {
                    erroEl.textContent = 'Preencha nome e custo.';
                    erroEl.style.display = 'block';
                    return;
                }

                var formData = new FormData();
                formData.append('mp_nome', nome);
                formData.append('mp_custo', custo);
                formData.append('csrf_token', CSRF_TOKEN);

                fetch('/produtos/api/nova-mp', { method: 'POST', body: formData })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.success) {
                            // Atualizar MP_DATA e datalist
                            MP_DATA[data.nome] = { custo_por_kg: data.custo, unidade: 'un' };

                            var mpList = document.getElementById('mp-list');
                            var opt = document.createElement('option');
                            opt.value = data.nome;
                            mpList.appendChild(opt);

                            okEl.textContent = '"' + data.nome + '" cadastrado! Ja pode usar na cesta.';
                            okEl.style.display = 'block';

                            // Limpar campos
                            document.getElementById('nova-mp-nome').value = '';
                            document.getElementById('nova-mp-custo').value = '';

                            recalcularCesta();
                        } else {
                            erroEl.textContent = data.error;
                            erroEl.style.display = 'block';
                        }
                    })
                    .catch(function () {
                        erroEl.textContent = 'Erro ao salvar. Tente novamente.';
                        erroEl.style.display = 'block';
                    });
            });
        }

        // Calcular ao carregar
        recalcularCesta();
    }


    // ═══ IMPORTAR JSON ═══
    var importInput = document.getElementById('import-file');
    if (importInput) {
        importInput.addEventListener('change', function () {
            var file = this.files[0];
            if (!file) return;

            if (!confirm('Isso vai SUBSTITUIR todos os dados atuais pelo conteudo do arquivo. Continuar?')) {
                this.value = '';
                return;
            }

            var formData = new FormData();
            formData.append('file', file);

            fetch('/api/importar', {
                method: 'POST',
                body: formData,
                headers: { 'X-CSRFToken': CSRF_TOKEN }
            })
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.success) {
                    alert('Dados importados com sucesso!');
                    window.location.href = '/';
                } else {
                    alert('Erro: ' + (data.error || 'Falha na importacao'));
                }
            })
            .catch(function () {
                alert('Erro de conexao ao importar.');
            });

            this.value = '';
        });
    }


    // ═══ HELPERS ═══
    function formatBRL(value) {
        return 'R$ ' + value.toFixed(2).replace('.', ',');
    }

    function formatNum(value, decimals) {
        return value.toFixed(decimals).replace('.', ',');
    }


    // ═══ UX: LOADING STATES ═══
    document.addEventListener('submit', function(e) {
        var btn = e.target.querySelector('[type="submit"]');
        if (btn && !btn.dataset.noLoading) {
            btn.disabled = true;
            btn.dataset.originalText = btn.innerHTML;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Salvando...';
            setTimeout(function() {
                btn.disabled = false;
                btn.innerHTML = btn.dataset.originalText;
            }, 10000);
        }
    });


    // ═══ UX: CTRL+S PARA SALVAR ═══
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            e.preventDefault();
            var form = document.querySelector('form[data-autosave], form.main-form, form[action*="salvar"]');
            if (form) {
                form.requestSubmit ? form.requestSubmit() : form.submit();
            }
        }
    });


    // ═══ UX: AUTO-SAVE DEBOUNCED ═══
    (function() {
        var forms = document.querySelectorAll('form[data-autosave]');
        if (!forms.length) return;

        forms.forEach(function(form) {
            var timer = null;
            var indicator = document.createElement('span');
            indicator.className = 'ms-2 small';
            indicator.style.display = 'none';
            var submitBtn = form.querySelector('[type="submit"]');
            if (submitBtn) submitBtn.parentElement.appendChild(indicator);

            function showStatus(text, color) {
                indicator.textContent = text;
                indicator.style.color = color;
                indicator.style.display = '';
                setTimeout(function() { indicator.style.display = 'none'; }, 3000);
            }

            function doAutoSave() {
                var formData = new FormData(form);
                indicator.textContent = 'Salvando...';
                indicator.style.color = '#666';
                indicator.style.display = '';

                fetch(form.action, {
                    method: 'POST',
                    body: formData,
                    headers: {'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': CSRF_TOKEN}
                }).then(function(r) {
                    if (r.ok) showStatus('Salvo', '#28a745');
                    else showStatus('Erro ao salvar', '#dc3545');
                }).catch(function() {
                    showStatus('Erro de conexão', '#dc3545');
                });
            }

            form.addEventListener('input', function() {
                clearTimeout(timer);
                timer = setTimeout(doAutoSave, 3000);
            });
        });
    })();


    // ═══ UX: VALIDAÇÃO CLIENT-SIDE ═══
    document.querySelectorAll('form[data-validate]').forEach(function(form) {
        form.addEventListener('submit', function(e) {
            if (!form.checkValidity()) {
                e.preventDefault();
                e.stopPropagation();
            }
            form.classList.add('was-validated');
        });
    });
});
