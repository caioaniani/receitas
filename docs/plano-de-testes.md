# Plano de testes — Caixa próprio + Clover + servidor local

Objetivo: validar o caixa novo em produção controlada, loja por loja,
**sem desligar a Seru** (a nota fiscal continua nela até o módulo fiscal
próprio existir). Cada fase tem critério de saída — só avance quando bater.

---

## Fase 0 — Subir pra produção (30 min, sem risco)

As mudanças são aditivas: tabelas novas nascem vazias e, sem as variáveis
de ambiente, nada muda no comportamento atual do sistema.

- [ ] Merge do PR `claude/upbeat-davinci-io19yq` → `claude/bakery-recipe-cost-system-N4ieR` (deploy automático)
- [ ] No Railway, definir:
  - `CLOVER_MODE=simulado` (pra treinar o fluxo de cartão sem maquininha)
  - `SYNC_API_TOKEN=<gerar>` → `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- [ ] Conferir deploy: abrir `/pdv/caixa`, badge "Clover: conectada (simulada)"
- [ ] Em ⚙ (config do caixa): **atribuir o setor de cada item** (chapa,
      cafe, cozinha, viagem) — isso desce pras lojas via sync depois
- [ ] Conferir preços: item sem preço de loja não aparece no caixa —
      revisar os que faltarem

**Critério de saída**: venda de teste completa na nuvem (dinheiro +
cartão simulado) aparecendo em "Vendas de hoje".

## Fase 1 — Treino do fluxo (2–3 dias, no caixa da nuvem)

Quem: você + 1 operador de confiança. Roteiro, cada um fazendo tudo:

- [ ] Venda em dinheiro com troco (conferir troco na tela final)
- [ ] Venda PIX
- [ ] Venda cartão (simulado): aguardar maquininha → aprovação
- [ ] Cancelar uma cobrança no meio (botão "Cancelar cobrança")
- [ ] Pagamento dividido (metade dinheiro, metade cartão)
- [ ] Desconto + item avulso (encomenda)
- [ ] Cancelar venda aberta
- [ ] Errar de propósito: cobrar sem itens, trocar de loja no meio

**Critério de saída**: operador fecha uma venda em menos de 30 segundos
sem ajuda.

## Fase 2 — Servidor local na loja piloto (semana 1)

Compras/infra:
- [ ] Mini PC x86 (4 GB RAM, SSD — ex: Beelink/NUC, ~R$ 1.200–1.800) + nobreak pequeno
- [ ] Rede da loja com IPs fixos pro mini PC e impressoras (reserva DHCP no roteador)

Instalação (detalhe em `docs/servidor-local.md`):
- [ ] Python 3.11+, `git clone`, `pip install -r requirements.txt`
- [ ] Env vars: `SYNC_NUVEM_URL`, `SYNC_API_TOKEN` (o mesmo da nuvem),
      `SYNC_LOJA_ID` (id da loja na nuvem), `ADMIN_PASSWORD` forte
- [ ] `python run.py` e depois configurar como serviço (systemd/NSSM)
- [ ] Criar usuários operadores locais (papel funcionario + loja)

Validação:
- [ ] Tablet/PC da loja abre `http://IP-do-mini-pc:2000/pdv/caixa`
- [ ] Catálogo desceu (itens e preços iguais aos da nuvem, setores certos)
- [ ] Badge "Nuvem: sincronizada" verde
- [ ] **Teste de queda**: desligar a internet da loja → fazer 2–3 vendas
      (badge vermelho "sem internet", caixa segue) → religar → badge verde
      e vendas aparecendo na nuvem em até 1 min

**Critério de saída**: vendas offline subiram sem perda nem duplicação.

## Fase 3 — Impressoras Jetway por setor (semana 1–2)

- [ ] Identificar o modelo e a interface de cada Jetway (Ethernet? USB?)
  - Ethernet/Wi-Fi: IP fixo e pronto
  - Só USB: ligar no mini PC e compartilhar como impressora RAW de rede
- [ ] Cadastrar em ⚙ (loja, setor, IP, porta 9100, largura 80mm=48 col)
- [ ] Botão "testar" em cada uma → saiu "TESTE OK"?
- [ ] Venda com 1 item de cada setor → cada comanda saiu na térmica certa,
      só com os itens do setor, com número/hora grandes e observação
- [ ] Simular falha: tirar o papel/cabo → vender → botão vermelho nas
      vendas do dia → reimprimir funcionou
- [ ] (Opcional) térmica no balcão como setor `caixa` pro cupom de conferência

**Critério de saída**: 20 vendas seguidas com 100% das comandas no setor
certo; recuperação por reimpressão funcionando.

## Fase 4 — Clover real (trilha paralela — começar JÁ, depende da Fiserv)

- [ ] Enviar hoje o pedido de credenciamento: **dvrel@clover.com** (cc:
      gerente da conta Fiserv/adquirente). Texto sugerido:

  > Somos a Padaria Opão (CNPJ X), clientes Clover Mini no Brasil.
  > Temos um PDV próprio (web, Flask) e queremos credenciar a
  > **integração externa** do nosso PDV com a Clover Mini para envio de
  > cobrança da venda (semi-integração / REST Pay Display ou o fluxo
  > equivalente Clover-SiTef do Brasil). Solicitamos a documentação,
  > acesso de desenvolvedor e credenciais de sandbox/produção.

- [ ] Enquanto não vem: cartão como **captura manual** (digita o valor na
      Mini, registra no caixa) — operação não trava
- [ ] Credenciais em mãos: no servidor da loja `CLOVER_MODE=local`,
      `CLOVER_API_BASE=https://IP-da-mini:12346`, token/serial/RAID
      (`docs/clover-pdv.md`); se o formato BR divergir, o ajuste é só em
      `app/services/clover.py` (me chama que eu adapto)
- [ ] Transação real de valor baixo + estorno na Mini
- [ ] Teste 4G: desligar a internet fixa → cobrança via caixa → Mini
      autoriza pelo chip

**Critério de saída**: cobrança disparada do caixa, aprovada na Mini,
status correto no caixa (aprovado/negado/cancelado).

## Fase 5 — Piloto de operação em paralelo com a Seru (semanas 2–4)

A nota continua saindo pela Seru — dupla digitação durante o piloto, então
limite a janelas (ex: 2h/dia no movimento médio).

- [ ] 1ª semana: caixa novo em paralelo nas janelas definidas
- [ ] Fechamento do dia: comparar "Vendas de hoje" (por forma de
      pagamento) × relatório da Seru — bater valor a valor
- [ ] Registrar TODA fricção num grupo/nota (tela, fluxo, comanda, troco)
- [ ] Ajustes (me traga a lista) → repetir até rodar 1 semana limpa

**Métricas de go/no-go pra expandir às outras lojas:**
- 0 vendas perdidas ou duplicadas na sincronização (1 semana)
- < 1% de comandas precisando reimpressão
- Fechamento batendo com a Seru todos os dias
- Operadores preferindo o caixa novo (teste honesto)

## Fase 6 — Expansão + fiscal

- [ ] Replicar o kit (mini PC + térmicas) nas demais lojas
- [ ] Módulo NFC-e próprio com contingência offline (eu desenvolvo —
      precisarei de: certificado A1 (.pfx + senha), CSC/ID Token da SEFAZ-SP,
      IE e dados fiscais dos itens: NCM, CFOP, CSOSN/CST)
- [ ] Homologação SEFAZ → rodar fiscal em paralelo → desligar a Seru

---

## Riscos e plano B

| Risco | Mitigação |
|---|---|
| Fiserv demorar no credenciamento | Captura manual segura a operação; cobrar semanalmente |
| Formato BR da API divergir do implementado | Resposta crua fica salva em `venda_pagamento.clover_resposta`; ajuste isolado em `clover.py` |
| Jetway só USB | Compartilhar RAW pelo mini PC (print server) |
| Mini PC morrer | A nuvem tem as vendas já sincronizadas; trocar a máquina e refazer setup (≈30 min); manter 1 mini PC reserva |
| Dupla digitação cansar a equipe | Janelas curtas de piloto; fiscal próprio é o que elimina isso |
