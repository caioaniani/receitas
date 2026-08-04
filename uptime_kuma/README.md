# Uptime Kuma — o vigia que fica FORA do sistema

## Por que existe

Todos os vigias do sistema (Chatwoot, site/frete, custo de IA, PDV, baixas
presas, venda sem item, estorno pendente) rodam **dentro** do app, no
`app/services/seru_cron.py`. O Sentry também: é uma biblioteca que roda no
processo do Flask.

Isso significa que os dois cobrem a mesma classe de falha — *"o app está de
pé e algo deu errado"* — e ficam **mudos** na classe oposta:

| Situação | Sentry | Vigias do app | Uptime Kuma |
|---|---|---|---|
| Erro numa rota | ✅ reporta | — | — |
| Regra de negócio violada | — | ✅ alerta | — |
| App em crashloop / não sobe | ❌ mudo | ❌ mudo | ✅ **alerta** |
| Banco fora, app não inicia | ❌ mudo | ❌ mudo | ✅ **alerta** |
| Deploy travado / Railway fora | ❌ mudo | ❌ mudo | ✅ **alerta** |
| Certificado SSL vencendo | ❌ | ❌ | ✅ avisa antes |

Silêncio total é indistinguível de "está tudo bem" — é esse buraco que o
Uptime Kuma fecha.

## Estado atual (04/08/2026)

**Instalado e rodando** no VPS da Vultr (São Paulo, `216.238.102.67`), em
`/opt/uptime-kuma`, modo HTTP na porta 3001. Container `unless-stopped`
(volta sozinho em qualquer reinício), Docker 29.7.1 sobre Ubuntu 26.04.

Confirmado na instalação:
- A ponte RADIUS do Wi-Fi (`wifi-radius.service`) está **`enabled`** — sobe
  sozinha no boot, então reiniciar o VPS é seguro para o Wi-Fi das lojas.
- Instalar o Docker não afetou a ponte (`active`, porta 1812 escutando).

Pendências opcionais:
- **HTTPS**: hoje o login trafega em texto claro na porta 3001 exposta.
  Criar o DNS `status.opaopadariaartesanal.com.br` → IP do VPS e rodar
  `./setup.sh status.opaopadariaartesanal.com.br` fecha isso (a 3001 sai
  da internet e o Caddy assume 80/443).
- **Vigiar o vigia**: UptimeRobot free apontado para este Kuma.
- O VPS tem reinício pendente (atualizações de kernel) — seguro de fazer.

## Onde roda

No **VPS da Vultr (São Paulo)** que já existe para a ponte RADIUS do Wi-Fi.
Nunca no Railway: um monitor que cai junto com o alvo não monitora nada.

Convive com a ponte RADIUS sem conflito (ela usa `1812/udp`).
⚠️ **Este script não mexe em firewall de propósito.** Se um dia for
configurar UFW nesse VPS, inclua `ufw allow 1812/udp` — sem isso o Wi-Fi das
lojas para de autenticar.

## Instalação

```bash
# no VPS, como root
git clone <repo> /tmp/receitas   # ou copie só esta pasta
cd /tmp/receitas/uptime_kuma
./setup.sh
```

Sobe em `http://<ip-do-vps>:3001`. **Abra imediatamente e crie o usuário
admin** — até isso ser feito, quem abrir a página vira o dono.

### Com HTTPS (recomendado)

Senha em HTTP puro trafega em texto claro. Com um subdomínio, o Caddy
resolve o certificado sozinho:

1. Crie um registro DNS **A**: `status.opaopadariaartesanal.com.br` → IP do VPS
2. `./setup.sh status.opaopadariaartesanal.com.br`

Nesse modo a porta 3001 deixa de ficar exposta (fica só em `127.0.0.1`) e o
acesso passa pelo Caddy em 80/443.

## Configuração POR COMANDO (mais rápido)

`configurar.py` cria os quatro monitores e o alerta de WhatsApp de uma vez.
É **idempotente** (compara por nome; rodar de novo não duplica) e roda num
container Python descartável — não instala nada no VPS:

```bash
cd /opt/uptime-kuma
# baixe/copie o configurar.py pra cá, então:
docker run --rm --network host \
  -e KUMA_USER='admin' -e KUMA_PASS='SUA_SENHA' \
  -e ZAPI_INSTANCE_ID='...' -e ZAPI_TOKEN='...' \
  -e ZAPI_CLIENT_TOKEN='...' -e ZAPI_PHONE='5511999999999' \
  -v /opt/uptime-kuma/configurar.py:/c.py:ro \
  python:3.12-slim sh -c "pip install -q uptime-kuma-api && python /c.py"
```

Sem as variáveis `ZAPI_*` ele cria só os monitores e avisa que ninguém será
notificado. Depois é só clicar em **Test** na notificação, pela tela.

O que segue abaixo é o mesmo, na mão — use se preferir conferir campo a
campo ou se o script falhar.

## Configuração pela tela

### 1. Monitores

Em **Add New Monitor**, crie estes três. Em todos: `Retries = 2`
(evita alarme falso por oscilação de rede) e deixe
**Certificate Expiry Notification** ligado.

| Nome | Tipo | URL | Intervalo | Observação |
|---|---|---|---|---|
| Sistema — gestão | HTTP(s) - Keyword | `https://gestao.opaopadariaartesanal.com.br/health` | 60s | Keyword: `ok` — rota leve que já existe pra isso (`app/__init__.py:298`) |
| Loja online | HTTP(s) | `https://opao.online` | 60s | Site fora num sábado = venda perdida em silêncio |
| Atendimento (Chatwoot) | HTTP(s) | `https://atendimento.opaopadariaartesanal.com.br` | 120s | Projeto separado no Railway |

Por que **Keyword** no primeiro: o Railway pode devolver uma página de erro
com status 200. Exigir a palavra `ok` no corpo garante que quem respondeu foi
o app, não o proxy. (Confirmado na instalação: `200 - OK, keyword is found`.)

### ⚠️ Não monitore a ponte RADIUS como "TCP Port"

Erro cometido em 04/08/2026 — gerou alarme falso já no primeiro ciclo. A
ponte escuta em **UDP**/1812 (`ss -ulnp` mostra `UNCONN`) e o monitor de
porta do Kuma testa **TCP**: `ECONNREFUSED` garantido. Somado a isso, o
container tem rede própria, então `127.0.0.1` lá dentro é o container e não
o VPS.

Para monitorar a ponte de verdade existe `MonitorType.RADIUS`, que faz
autenticação real e cobre a cadeia inteira (Kuma → ponte → endpoint
`/api/wifi/radius-check` no gestão → banco). Exige `radiusSecret`
(`WIFI_RADIUS_SECRET`), `radiusUsername`/`radiusPassword` de uma conta de
cliente **dedicada a teste**, e o hostname alcançável de dentro do container
(o IP do VPS). Decisão do dono pendente — sem isso, a ponte fica coberta
pelo systemd (`Restart` automático, já se recuperou sozinha em 01/08).

### 2. Alerta no WhatsApp (Z-API)

Em **Settings → Notifications → Setup Notification**:

- **Notification Type**: `Webhook`
- **Post URL**:
  ```
  https://api.z-api.io/instances/SEU_INSTANCE_ID/token/SEU_TOKEN/send-text
  ```
  (`ZAPI_INSTANCE_ID` e `ZAPI_TOKEN` estão nas variáveis do Railway)
- **Request Body**: `Custom Body`
- **Custom Body** — copie exatamente, trocando só o telefone:
  ```
  {
    "phone": "5511999999999",
    "message": {{ msg | prepend: "🚨 O Pão — " | json }}
  }
  ```
- **Additional Headers** (marque a caixa) — o `Content-Type` é
  **obrigatório**, ver o porquê abaixo:
  ```json
  {
    "Content-Type": "application/json",
    "Client-Token": "SEU_ZAPI_CLIENT_TOKEN"
  }
  ```
- Marque **Default enabled** e **Apply on all existing monitors**.
- Clique em **Test** — a mensagem tem que chegar no WhatsApp na hora.

#### ⚠️ Por que `{{ msg | json }}` e não `"{{ msg }}"`

O exemplo que o próprio Uptime Kuma mostra na tela usa a mensagem entre
aspas. **Isso quebra.** O corpo é renderizado por LiquidJS e o resultado
precisa ser JSON válido — e mensagens de queda frequentemente contêm aspas
ou quebras de linha:

```
[Sistema] [🔴 Down] getaddrinfo ENOTFOUND "gestao"
```

Com `"{{ msg }}"` isso vira JSON inválido e **o alerta não sai justamente na
hora em que o sistema caiu**. O filtro `| json` já produz a string com as
aspas e os escapes corretos — por isso ele vai **sem** aspas em volta.
Testado com erro contendo aspas, multilinha, barra invertida e mensagem de
recuperação.

Nota: este caminho fala com a Z-API **direto**, sem passar pelo
`app/services/zapi.py` — logo não está sujeito ao whitelist nem ao teto/hora
do sistema. É o desejado: alerta de queda nunca pode ser suprimido por
throttle.

### 3. Vigiar o vigia

Se o VPS cair, o monitor cai com ele. Cubra com um serviço externo gratuito
(UptimeRobot, plano free) apontando para o próprio Uptime Kuma — um monitor
só, que avisa se o vigia emudecer.

## Operação

```bash
cd /opt/uptime-kuma
docker compose ps                 # estado
docker compose logs -f            # logs
./setup.sh                        # atualizar (preserva os dados)
tar czf kuma-backup.tgz data      # backup (SQLite em data/kuma.db)
```

## O que ele NÃO faz

Não substitui os vigias do sistema. Ele responde *"o app respondeu?"* — não
sabe se a previsão de produção está errada, se uma venda ficou sem estorno ou
se o custo de IA estourou. As duas camadas são complementares.
