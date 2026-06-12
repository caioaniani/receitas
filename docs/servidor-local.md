# Servidor local por loja (offline-first)

Cada loja roda **o mesmo código** deste repositório num mini PC, com seu
banco próprio. Se a internet fixa cair, o caixa, as comandas das térmicas
e a Clover (na rede local / 4G da Mini) continuam funcionando — as vendas
ficam na fila e sobem pra nuvem quando o link volta.

```
NUVEM (Railway) — dona dos cadastros e dos relatórios
   ▲ vendas finalizadas          │ catálogo (receitas, produtos,
   │ (POST /pdv/api/sync/vendas) │  preços, setores, lojas)
   │                             ▼ (GET /pdv/api/sync/catalogo)
LOJA — mini PC com Flask + SQLite
   ├─ caixa nos tablets/PCs da loja (http://IP-do-mini-pc:2000/pdv/caixa)
   ├─ Clover Mini via rede local (CLOVER_MODE=local)
   └─ térmicas Jetway por setor (chapa, café, cozinha, viagem)
```

## Como funciona a sincronização

- **Catálogo desce** (a cada ~10 min e no boot): a nuvem é a fonte da
  verdade; os IDs locais espelham os da nuvem. Receitas descem só com o
  necessário pra vender (nome, categoria, setor, preços) — ficha técnica
  completa fica na nuvem. `PrecoLojaReceita` é substituída por inteiro.
- **Vendas sobem** (a cada 60s): só as finalizadas (paga/cancelada). A
  identidade global é `Venda.uuid` — reenviar após queda de internet não
  duplica nada (a nuvem responde "já tenho"). O `code` leva o prefixo da
  loja (`V2-20260612-001`) e, se ainda assim colidir, a nuvem ajusta com
  sufixo.
- O badge **"Nuvem"** no topo do caixa mostra o estado: verde
  (sincronizada), amarelo (vendas na fila), vermelho (sem internet — siga
  vendendo, sobe depois). Admin pode forçar um ciclo em
  `POST /pdv/caixa/api/sync/agora`.

## Setup do servidor da loja

1. **Token compartilhado** (uma vez): gere com
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` e
   defina `SYNC_API_TOKEN` no Railway (nuvem) e em cada loja (mesmo valor).
2. **No mini PC** (Linux ou Windows com Python 3.11+):
   ```bash
   git clone <repo> && cd receitas
   pip install -r requirements.txt
   export SYNC_NUVEM_URL=https://SEU-APP.up.railway.app
   export SYNC_API_TOKEN=<o token>
   export SYNC_LOJA_ID=2            # id da loja na nuvem
   export CLOVER_MODE=local         # quando a integração Clover BR estiver credenciada
   export CLOVER_API_BASE=https://IP-DA-MINI:12346
   export ADMIN_PASSWORD=<senha forte>
   python run.py                    # porta 2000
   ```
   `SYNC_NUVEM_URL` definido = **modo loja**: o app pula os seeds (o
   catálogo vem da nuvem), liga o loop de sincronização e o caixa
   pré-seleciona a loja do `SYNC_LOJA_ID`.
3. **Tablets/PCs da loja** acessam `http://IP-do-mini-pc:2000`. Crie os
   usuários operadores localmente (usuários não são sincronizados — o
   nome de quem vendeu sobe junto com a venda no campo `operador`).
4. **Impressoras**: cadastre em Caixa → ⚙ com os IPs reais e use o botão
   de teste (docs/comandas-setores.md).
5. Pra rodar como serviço (boot automático): systemd no Linux ou NSSM no
   Windows, apontando pro `python run.py` com as env vars.

## Limites desta fase (de propósito)

- Sincroniza **catálogo ↓ e vendas ↑**. Estoque de loja, pedidos e RH
  continuam direto na nuvem (não fazem parte do fluxo offline do caixa).
- Receita/produto **excluído** na nuvem não some da loja sozinho — zere o
  preço (item sem preço não aparece no catálogo do caixa) ou desative o
  produto.
- Vendas abertas abandonadas não sobem (cancele-as no caixa).
- NFC-e: continua pela Seru/Clover. O módulo fiscal próprio (emissão com
  contingência offline) é a próxima fase e é o que permitirá desligar a
  Seru.
