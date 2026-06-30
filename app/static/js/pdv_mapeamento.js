/* Widget compartilhado de mapeamento de produto PDV (Seru).
 *
 * Fonte UNICA do fator de composicao + resolucao de alvo + save, para as telas
 * de itens-vendidos, reconciliacao e mapeamentos nao divergirem. Antes cada uma
 * tinha a sua copia de parseFator / calc de fatias / POST /api/mapear, com
 * diferencas sutis — a "zona" que o dono relatou. Comportamento aqui espelha
 * exatamente o que as telas ja faziam (refator de deduplicacao, sem mudar UX).
 *
 * Exposto como window.PdvMap.
 */
(function () {
  'use strict';

  // Fator: 1 venda consome X unidades do alvo. Aceita virgula PT-BR ("0,2").
  // Invalido / <= 0 -> 1 (clamp no cliente). O SERVIDOR rejeita invalido de
  // verdade (parse_fator_composicao) — aqui e so a normalizacao da UI.
  function parseFator(raw) {
    var f = parseFloat(String(raw == null ? '1' : raw).trim().replace(',', '.'));
    if (!isFinite(f) || f <= 0) return 1;
    return f;
  }

  // Texto de ajuda do fator (mesma mensagem das telas). Retorna {text, tone}
  // com tone in ('muted', 'info').
  function fatorHelp(f) {
    if (Math.abs(f - 1) < 0.001) {
      return { text: '1 venda = 1 unidade (default)', tone: 'muted' };
    }
    if (f < 1) {
      return {
        text: '1 venda = ' + f + ' unidade (composto: ' + Math.round(1 / f) +
              ' vendas baixam 1 inteiro)',
        tone: 'info'
      };
    }
    return { text: '1 venda = ' + f + ' unidades', tone: 'info' };
  }

  // "X fatias de Y" -> string do fator X/Y (vazio se invalido). Ex: (2, 10) -> "0.2".
  function fatiasParaFator(x, y) {
    x = parseFloat(String(x == null ? '' : x).replace(',', '.'));
    y = parseFloat(String(y == null ? '' : y).replace(',', '.'));
    if (!(x > 0 && y > 0)) return '';
    return (x / y).toFixed(4).replace(/\.?0+$/, '');
  }

  // Indice de alvos para datalist/resolucao: rotulo UNICO -> "tipo:id", mais
  // fallback por nome puro (so quando o nome e unico, pra nao baixar no alvo
  // errado). produtos/receitas = [{id, nome}]. Quando dois itens tem o mesmo
  // rotulo, sufixa " #id".
  function construirIndiceAlvo(produtos, receitas) {
    var ents = [];
    (produtos || []).forEach(function (p) {
      ents.push({ base: p.nome + ' — produto', plain: p.nome, tok: 'produto:' + p.id });
    });
    (receitas || []).forEach(function (r) {
      ents.push({ base: r.nome + ' — receita', plain: r.nome, tok: 'receita:' + r.id });
    });
    var cont = {}, plainCont = {}, tokens = {}, byName = {}, labels = [];
    ents.forEach(function (e) {
      cont[e.base] = (cont[e.base] || 0) + 1;
      var pk = e.plain.toLowerCase();
      plainCont[pk] = (plainCont[pk] || 0) + 1;
    });
    ents.forEach(function (e) {
      var chave = cont[e.base] > 1 ? (e.base + ' #' + e.tok.split(':')[1]) : e.base;
      tokens[chave] = e.tok;
      labels.push(chave);
      var pk = e.plain.toLowerCase();
      if (plainCont[pk] === 1) byName[pk] = e.tok;
    });
    return { tokens: tokens, byName: byName, labels: labels };
  }

  // Popula um <datalist> com os rotulos do indice.
  function popularDatalist(datalistEl, indice) {
    if (!datalistEl) return;
    datalistEl.innerHTML = '';
    indice.labels.forEach(function (label) {
      var opt = document.createElement('option');
      opt.value = label;
      datalistEl.appendChild(opt);
    });
  }

  // Texto digitado -> "tipo:id" (ou null). Aceita o rotulo exato OU o nome puro
  // quando unico (deixa o usuario digitar so o nome e Vincular).
  function resolverAlvo(texto, indice) {
    var v = (texto || '').trim();
    if (indice.tokens[v]) return indice.tokens[v];
    var plain = v.replace(/ — (produto|receita)$/, '').trim().toLowerCase();
    return indice.byName[plain] || null;
  }

  // POST /pdv/api/mapear. dados = {seru_nome, acao, alvo_tipo, alvo_id, fator}.
  // (alvo_*/fator so usados quando acao === 'vincular'.) Retorna Promise<{ok,...}>.
  function salvar(dados) {
    var fd = new FormData();
    fd.append('csrf_token', window.CSRF_TOKEN || '');
    fd.append('seru_nome', dados.seru_nome);
    fd.append('acao', dados.acao);
    if (dados.acao === 'vincular') {
      fd.append('alvo_tipo', dados.alvo_tipo);
      fd.append('alvo_id', dados.alvo_id);
      fd.append('fator', String(dados.fator == null ? 1 : dados.fator));
    }
    return fetch('/pdv/api/mapear', {
      method: 'POST', body: fd, credentials: 'same-origin'
    }).then(function (r) {
      if (!r.ok && r.status !== 200) {
        return r.text().then(function (t) {
          throw new Error('HTTP ' + r.status + ': ' + (t || '').slice(0, 200));
        });
      }
      return r.json();
    });
  }

  window.PdvMap = {
    parseFator: parseFator,
    fatorHelp: fatorHelp,
    fatiasParaFator: fatiasParaFator,
    construirIndiceAlvo: construirIndiceAlvo,
    popularDatalist: popularDatalist,
    resolverAlvo: resolverAlvo,
    salvar: salvar
  };
})();
