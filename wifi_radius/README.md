# Ponte RADIUS do Wi-Fi das lojas

Trava dura do Wi-Fi por **login** (e-mail + senha da conta do site), sem o
problema de certificado do External Portal. O cliente digita e-mail+senha na
tela do OC200; o controlador pergunta a esta ponte (RADIUS/UDP); a ponte
pergunta ao `gestão.opao` (`/api/wifi/radius-check`) se confere.

```
OC200 (portal RADIUS)  --UDP 1812-->  [bridge.py no VPS]  --HTTPS-->  gestão.opao
```

Roda num **servidorzinho** (VPS) porque o Railway não expõe UDP. É um único
arquivo, **sem dependências** além do Python 3.8+.

## Por que um VPS e não o Railway

O RADIUS é UDP; o Railway só expõe HTTP. O *cérebro* (conferir a senha) fica
no gestão.opao; aqui só mora o tradutor de protocolo. Qualquer VPS pequeno
serve (Vultr/Hetzner/DigitalOcean ~US$5/mês; ou tier gratuito da Oracle
Cloud). Precisa de **IP público fixo** (o OC200 vai apontar pra ele) e a
porta **UDP 1812** liberada no firewall.

## Deploy (Ubuntu/Debian)

1. Copie `bridge.py` pro VPS, ex. `/opt/wifi-radius/bridge.py`.

2. Teste a cripto (não precisa de rede):
   ```bash
   python3 /opt/wifi-radius/bridge.py --selftest
   ```

3. Crie o serviço systemd em `/etc/systemd/system/wifi-radius.service`:
   ```ini
   [Unit]
   Description=Ponte RADIUS Wi-Fi O Pao
   After=network.target

   [Service]
   Environment=WIFI_RADIUS_SECRET=<segredo-RADIUS-forte>
   Environment=WIFI_API_URL=https://gestao.opaopadariaartesanal.com.br/api/wifi
   Environment=WIFI_API_TOKEN=<mesmo valor do WIFI_RADIUS_TOKEN do Railway>
   ExecStart=/usr/bin/python3 /opt/wifi-radius/bridge.py
   Restart=always
   RestartSec=3
   User=nobody

   [Install]
   WantedBy=multi-user.target
   ```

4. Libere a porta e suba:
   ```bash
   ufw allow 1812/udp
   systemctl daemon-reload
   systemctl enable --now wifi-radius
   journalctl -u wifi-radius -f     # acompanhar os logs
   ```

## Os DOIS segredos (não confundir)

- **`WIFI_RADIUS_SECRET`**: entre o **OC200 e a ponte** (o "Shared Secret" do
  perfil RADIUS no Omada). É o que cifra a senha no caminho UDP.
- **`WIFI_API_TOKEN`**: entre a **ponte e o gestão.opao** (Bearer). Tem que
  ser IGUAL ao `WIFI_RADIUS_TOKEN` setado no Railway.

Gere os dois fortes e diferentes (ex.: `openssl rand -hex 24`).

## Config no Omada (OC200)

1. `Network Config → Profile → RADIUS Profile → Create`: nome `radius-opao`,
   Authentication Server = **IP público do VPS**, Auth Port = **1812**,
   Shared Secret = **`WIFI_RADIUS_SECRET`**.
2. `Network Config → Authentication → Portal` (edite "Wi-Fi Clientes"):
   Authentication Type = **RADIUS**, aponte pro perfil `radius-opao`.
3. Deixe a **Welcome Information** com o convite ao cadastro, ex.:
   *"Já é cliente? Entre com seu e-mail e senha. Ainda não?
   Cadastre-se em opao.online/loja/wifi."*
4. **Pre-Authentication Access** (aba Access Control) precisa liberar
   `opao.online` + o WhatsApp, pra quem ainda não tem conta conseguir se
   cadastrar antes de logar.

## Teste de ponta a ponta

1. `journalctl -u wifi-radius -f` no VPS.
2. Um cliente com conta (e-mail+senha em opao.online) conecta e loga na tela
   do Wi-Fi → o log mostra `<email> -> ACCEPT` e a internet libera.
3. Senha errada → `-> REJECT`.

## Segurança

- A senha do cliente NUNCA fica na ponte: é decriptada, repassada por HTTPS
  e descartada.
- Fail-closed: se o gestão.opao não responder, a ponte devolve **Reject**
  (não libera às cegas).
- O endpoint `/api/wifi/radius-check` é anti-enumeração (mesma resposta pra
  senha errada e conta inexistente) e tem rate limit.
