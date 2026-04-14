/* ═══════════════════════════════════════════
   Padaria — JavaScript Principal
   ═══════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', function () {

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

    if (fichaBody && pesoBaseInput) {

        // ── Aviso de alterações não salvas ──
        var fichaForm = document.getElementById('ficha-form');
        var formAlterado = false;

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

        // Eventos de input
        pesoBaseInput.addEventListener('input', recalcularTudo);
        rendimentoInput.addEventListener('input', recalcularTudo);
        if (pesoUnitarioInput) pesoUnitarioInput.addEventListener('input', recalcularTudo);
        if (multiplicadorInput) multiplicadorInput.addEventListener('input', recalcularTudo);
        if (perdaInput) perdaInput.addEventListener('input', recalcularTudo);
        if (precoVendaInput) precoVendaInput.addEventListener('input', recalcularTudo);
        if (precoLojaInput) precoLojaInput.addEventListener('input', recalcularTudo);
        if (precoSiteInput) precoSiteInput.addEventListener('input', recalcularTudo);

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

    function recalcularTudo() {
        var modo = modoSelect ? modoSelect.value : 'farinha';
        var pesoUnit = pesoUnitarioInput ? (parseFloat(pesoUnitarioInput.value) || 0) : 0;
        var multiplicador = multiplicadorInput ? (parseInt(multiplicadorInput.value) || 1) : 1;
        if (multiplicador < 1) multiplicador = 1;
        var perda = perdaInput ? (parseFloat(perdaInput.value) || 0) : 0;
        if (perda < 0) perda = 0;
        if (perda > 50) perda = 50;

        // Passo 1: Soma das porcentagens
        var sumPct = 0;
        document.querySelectorAll('.ingrediente-row').forEach(function (row) {
            var pctInput = row.querySelector('.pct-input');
            sumPct += parseFloat(pctInput ? pctInput.value : 0) || 0;
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

            var nome = nomeInput ? nomeInput.value.trim() : '';
            var pct = parseFloat(pctInput ? pctInput.value : 0) || 0;

            var qtd = pesoBase * pct / 100;
            var mp = MP_DATA[nome];
            var custoKg = mp ? mp.custo_por_kg : 0;
            var custoRs = qtd / 1000 * custoKg;

            // Mostrar com multiplicador
            var qtdExibir = qtd * multiplicador;
            var custoExibir = custoRs * multiplicador;

            qtdCell.textContent = qtdExibir > 0 ? formatNum(qtdExibir, 1) : '-';
            custoKgCell.textContent = mp ? formatBRL(custoKg) : '-';
            custoKgCell.className = mp ? 'custo-kg-calc valor-mp text-end' : 'custo-kg-calc text-end text-muted';
            custoRsCell.textContent = custoExibir > 0 ? formatBRL(custoExibir) : '-';

            totalPct += pct;
            totalQtd += qtd;
            totalCusto += custoRs;
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

        // Resumo
        var custoUn = rendimento > 0 ? totalCustoMult / rendimento : 0;

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


    // ═══ PRODUTOS/CESTAS ═══
    var prodBody = document.getElementById('prod-body');
    var prodTemplate = document.getElementById('prod-row-template');
    var btnAddProd = document.getElementById('btn-add-prod');

    if (prodBody) {
        if (btnAddProd && prodTemplate) {
            btnAddProd.addEventListener('click', function () {
                var clone = prodTemplate.content.cloneNode(true);
                prodBody.appendChild(clone);
            });
        }

        prodBody.addEventListener('click', function (e) {
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

            var btnNew = e.target.closest('.btn-del-new');
            if (btnNew) {
                btnNew.closest('tr').remove();
            }
        });
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
});
