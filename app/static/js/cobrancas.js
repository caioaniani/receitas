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
