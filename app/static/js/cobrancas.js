/* Confirma o destinatário efetivamente digitado, nunca o e-mail antigo. */
(() => {
    const form = document.getElementById('cob-send-form');
    if (!form) return;
    let enviando = false;
    form.addEventListener('submit', event => {
        if (enviando) { event.preventDefault(); return; }
        const email = form.elements.email.value.trim();
        if (!window.confirm(`Enviar a NF e o boleto, juntos em um único e-mail, para ${email}?`)) {
            event.preventDefault(); return;
        }
        enviando = true;
        const button = document.getElementById('cob-send-button');
        button.disabled = true;
        button.textContent = 'Enviando os dois documentos…';
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) window.location.reload();
    });
})();

/* Download direto da lista. Confirmar registro não altera o status no banco. */
(() => {
    document.querySelectorAll('a[data-cob-confirmar-banco]').forEach(link => {
        link.addEventListener('click', event => {
            event.preventDefault();
            if (!window.confirm('Você conferiu no Sicredi que este boleto já foi registrado? O ERP ainda só registra a geração da remessa.')) return;
            const destino = new URL(link.href, window.location.href);
            destino.searchParams.set('banco_confirmado', '1');
            window.location.assign(destino.href);
        });
    });
})();
