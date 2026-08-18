(function () {
    'use strict';

    var root = document.querySelector('.home-v2');
    var corpo = document.getElementById('modalCDCorpo');
    var titulo = document.getElementById('modalCDTitulo');
    var modalEl = document.getElementById('modalCD');
    if (!root || !corpo || !modalEl || typeof bootstrap === 'undefined') return;

    var url = root.getAttribute('data-sales-detail-url');
    var modal = new bootstrap.Modal(modalEl);

    function esc(value) {
        var node = document.createElement('div');
        node.textContent = value === null || value === undefined ? '' : String(value);
        return node.innerHTML;
    }

    function brl(value) {
        return (Number(value) || 0).toLocaleString('pt-BR', {
            style: 'currency',
            currency: 'BRL'
        });
    }

    function cancelamentos(itens) {
        if (!itens.length) return '<p class="text-muted mb-0">Nenhum cancelamento.</p>';
        var rows = itens.map(function (item) {
            return '<tr><td>' + esc(item.hora) + '</td><td class="text-muted">' +
                esc(item.codigo || '—') + '</td><td>' + esc(item.loja) +
                '</td><td class="text-end">' + brl(item.valor) + '</td><td>' +
                esc(item.caixa || '—') + '</td><td>' +
                (item.nf ? '<span class="text-success">sim</span>' : '<span class="text-danger">não</span>') +
                '</td></tr>';
        }).join('');
        return '<div class="table-responsive"><table class="table table-sm mb-0"><thead><tr>' +
            '<th>Hora</th><th>Cód.</th><th>Loja</th><th class="text-end">Valor</th><th>Caixa</th><th>NF</th>' +
            '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    function descontos(itens) {
        if (!itens.length) return '<p class="text-muted mb-0">Nenhum desconto.</p>';
        var rows = itens.map(function (item) {
            return '<tr><td>' + esc(item.hora) + '</td><td class="text-muted">' +
                esc(item.codigo || '—') + '</td><td>' + esc(item.loja) +
                '</td><td class="text-end">' + brl(item.subtotal) +
                '</td><td class="text-end text-danger">− ' + brl(item.desconto) +
                '</td><td class="text-end">' + brl(item.total) + '</td></tr>';
        }).join('');
        return '<div class="table-responsive"><table class="table table-sm mb-0"><thead><tr>' +
            '<th>Hora</th><th>Cód.</th><th>Loja</th><th class="text-end">Subtotal</th>' +
            '<th class="text-end">Desconto</th><th class="text-end">Total</th>' +
            '</tr></thead><tbody>' + rows + '</tbody></table></div>';
    }

    function abrir(dia, rotulo) {
        titulo.textContent = 'Cancelamentos e descontos — ' + rotulo;
        corpo.innerHTML = '<p class="text-muted mb-0">Consultando o Seru ao vivo…</p>';
        modal.show();
        fetch(url + '?dia=' + encodeURIComponent(dia), { headers: { Accept: 'application/json' } })
            .then(function (response) {
                return response.json().catch(function () { return { ok: false }; });
            })
            .then(function (data) {
                if (!data || !data.ok) {
                    corpo.innerHTML = '<p class="text-danger mb-0">' +
                        esc((data && data.erro) || 'Falha ao consultar o Seru.') + '</p>';
                    return;
                }
                corpo.innerHTML = '<h3 class="h6 mb-1">Cancelados <span class="text-muted fw-normal">(' +
                    data.cancelados.length + ' · ' + brl(data.cancelados_valor) + ')</span></h3>' +
                    cancelamentos(data.cancelados) + '<hr class="my-3"><h3 class="h6 mb-1">Descontos ' +
                    '<span class="text-muted fw-normal">(' + data.descontos.length + ' · ' +
                    brl(data.desconto_total) + ')</span></h3>' + descontos(data.descontos);
            })
            .catch(function () {
                corpo.innerHTML = '<p class="text-danger mb-0">Sem conexão com o Seru agora. Tente de novo.</p>';
            });
    }

    document.querySelectorAll('.abrir-cd').forEach(function (button) {
        button.addEventListener('click', function () {
            abrir(button.getAttribute('data-dia'), button.getAttribute('data-rotulo') || '');
        });
    });
})();
