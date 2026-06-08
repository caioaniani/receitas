import os

# Banco de dados: PostgreSQL em produção, SQLite local
DB_DIR = os.path.join(os.path.expanduser('~'), '.padaria')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'padaria.db')

DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DB_PATH}')
# Railway usa 'postgres://' mas SQLAlchemy precisa de 'postgresql://'
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)


class Config:
    _env_secret = os.environ.get('SECRET_KEY')
    if _env_secret:
        SECRET_KEY = _env_secret
    elif DATABASE_URL.startswith('postgresql'):
        raise RuntimeError(
            'SECRET_KEY obrigatoria em producao (Postgres detectado). '
            "Gere com: python3 -c 'import secrets; print(secrets.token_hex(32))' "
            'e defina como env var no Railway.'
        )
    else:
        import secrets as _secrets
        SECRET_KEY = _secrets.token_hex(32)  # so dev/SQLite local
    SQLALCHEMY_DATABASE_URI = DATABASE_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB upload (atestados)
    VNDA_API_TOKEN = os.environ.get('VNDA_API_TOKEN', '')
    # Token dedicado ao catalogo (/products). O VNDA_API_TOKEN pode nao ter o
    # escopo "Produtos" habilitado (so pedidos -> 403 no /products). Se setado,
    # o bot usa este pro catalogo; senao, cai no VNDA_API_TOKEN.
    VNDA_PRODUTOS_TOKEN = os.environ.get('VNDA_PRODUTOS_TOKEN', '')
    VNDA_SHOP_HOST = os.environ.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    # Vigia do chatbot: IA supervisora que assiste cada conversa e alerta via
    # Z-API quando detecta problema (cliente irritado, bot errou produto, perda
    # de venda). Liga/desliga sem mexer em codigo. Destino do alerta:
    # CHATBOT_VIGIA_NUMERO ou, na ausencia, ZAPI_NUMERO_DESTINO.
    CHATBOT_VIGIA = os.environ.get('CHATBOT_VIGIA', '1') == '1'
    CHATBOT_VIGIA_NUMERO = os.environ.get('CHATBOT_VIGIA_NUMERO', '')
    # Detector de abandono: minutos sem resposta na conversa pra acionar o vigia
    # (default 15). Roda no cron de 5 em 5 min via seru_cron.
    CHATBOT_VIGIA_ABANDONO_MIN = int(os.environ.get('CHATBOT_VIGIA_ABANDONO_MIN', '15') or '15')
    # Tiny ERP — token da API v2 (gera em Painel Tiny -> Configuracoes -> API).
    # Sem token, o bot recusa NF gentilmente e passa pro humano.
    TINY_API_TOKEN = os.environ.get('TINY_API_TOKEN', '')
    # Coordenadas da loja matriz — origem das rotas de entrega
    ROTA_ORIGEM_LAT = os.environ.get('ROTA_ORIGEM_LAT', '')
    ROTA_ORIGEM_LNG = os.environ.get('ROTA_ORIGEM_LNG', '')
    # Endereco textual da matriz — usado como origem dos links do Google Maps
    ROTA_ORIGEM_ENDERECO = os.environ.get('ROTA_ORIGEM_ENDERECO', '')
    # Chave da API do Google Maps Platform (Geocoding + Directions)
    GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

    # Seru (PDV) — credenciais OAuth2 client_credentials
    # Documentacao: https://integration.plataformaseru.com.br/v1/docs
    SERU_CLIENT_ID = os.environ.get('SERU_CLIENT_ID', '')
    SERU_CLIENT_SECRET = os.environ.get('SERU_CLIENT_SECRET', '')

    # Token para integracao com bots externos (n8n / WhatsApp).
    # Gere com: python -c "import secrets; print(secrets.token_urlsafe(32))"
    BOT_API_TOKEN = os.environ.get('BOT_API_TOKEN', '')
    # Telefones autorizados a consultar o bot (CSV, so digitos opcional).
    # Ex: '5511999999999,5511888888888'. Se vazio, qualquer telefone passa.
    BOT_ALLOWED_PHONES = os.environ.get('BOT_ALLOWED_PHONES', '')

    # Dropbox para fotos de comprovante de entrega
    # Token: https://www.dropbox.com/developers/apps -> escopo files.content.write + sharing.write
    DROPBOX_ACCESS_TOKEN = os.environ.get('DROPBOX_ACCESS_TOKEN', '')
    DROPBOX_PASTA_BASE = os.environ.get('DROPBOX_PASTA_BASE', '/Apps/Receitas-Entregas')
    # Refresh token flow (recomendado: token nao expira)
    # Setup: /entregas/dropbox/setup (apenas admin)
    DROPBOX_APP_KEY = os.environ.get('DROPBOX_APP_KEY', '')
    DROPBOX_APP_SECRET = os.environ.get('DROPBOX_APP_SECRET', '')
    DROPBOX_REFRESH_TOKEN = os.environ.get('DROPBOX_REFRESH_TOKEN', '')

    # Slack bot — copilot via DM/@mention.
    # Setup do app: https://api.slack.com/apps -> Create New App
    # Scopes do bot: chat:write, im:history, im:write, app_mentions:read, users:read
    # Event subscriptions: app_mention, message.im
    # Interactivity URL: <host>/slack/interact
    # Events URL: <host>/slack/events
    SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')  # xoxb-...
    SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')
    # Canais publicos onde o bot responde a @mention (CSV de IDs C123...).
    # Vazio = so DM.
    SLACK_CANAIS_PERMITIDOS = os.environ.get('SLACK_CANAIS_PERMITIDOS', '')
    # Canais de recebimento de mercadoria (NF/boleto). O bot SO LE: cada
    # imagem/PDF postada vira uma Conta a Pagar (IA extrai os dados). Nunca
    # responde nesses canais. CSV de IDs C123... Vazio = desligado.
    SLACK_CANAIS_NF = os.environ.get('SLACK_CANAIS_NF', '')
    # Cada canal de NF = uma loja. Mapa "id=Nome da Loja" separado por ';'
    # (usado pra filtrar Contas a Pagar por loja). Configuravel por env.
    SLACK_CANAIS_NF_NOMES = os.environ.get(
        'SLACK_CANAIS_NF_NOMES',
        'C09BJCYGQ4U=Matriz - Ribeiro do Vale;'
        'C09BTQ58QLR=Filial - Anésio Pinto Rosa;'
        'C0AMH3SQYFP=Filial - Nebraska')
    # Canal onde o bot posta o resumo diario as 04:00 BRT de pedidos pra
    # entregar hoje. Vazio = desligado. Ex: C0ABC1234.
    SLACK_CANAL_RESUMO_DIARIO = os.environ.get('SLACK_CANAL_RESUMO_DIARIO', '')
    # Canal #pedidos — recebe lembretes 9h/12h/16h/19h sobre lojas
    # que nao fizeram pedido pra amanha. Vazio = desligado.
    SLACK_CANAL_PEDIDOS = os.environ.get('SLACK_CANAL_PEDIDOS', '')
    # Canal #copilot — recebe lembretes 20:10/15/20/25 BRT de lojas que ainda
    # nao lancaram desperdicio (escalada antes do WhatsApp). Vazio = desligado.
    SLACK_CANAL_COPILOT = os.environ.get('SLACK_CANAL_COPILOT', '')

    # Z-API (WhatsApp) — envia digest diario de tarefas.
    # Cadastro: https://z-api.io/ → cria instancia → copia ID + token.
    ZAPI_INSTANCE_ID = os.environ.get('ZAPI_INSTANCE_ID', '')
    ZAPI_TOKEN = os.environ.get('ZAPI_TOKEN', '')
    # Header de seguranca opcional da Z-API (Account Settings → Token de seguranca)
    ZAPI_CLIENT_TOKEN = os.environ.get('ZAPI_CLIENT_TOKEN', '')
    # Numero destino do digest (formato 5511999999999, so digitos)
    ZAPI_NUMERO_DESTINO = os.environ.get('ZAPI_NUMERO_DESTINO', '')
    # Whitelist de numeros que o sistema pode enviar mensagem (CSV).
    # SEGURANCA: o servico recusa enviar pra numeros fora dessa lista.
    # Se vazio, refuse-all (nada e enviado). Inclui o ZAPI_NUMERO_DESTINO.
    ZAPI_NUMEROS_PERMITIDOS = os.environ.get('ZAPI_NUMEROS_PERMITIDOS', '')

    # ── Chatwoot (atendimento omnichannel WhatsApp/IG/FB/site) ──
    # Chatwoot roda como app self-hosted separado (Railway). O sistema da
    # padaria so INTEGRA: serve o "card do cliente" (iframe Dashboard App) e
    # faz backup do banco do Chatwoot junto com o seu.
    # URL base da instancia Chatwoot (ex: https://atendimento.opaopadaria...).
    CHATWOOT_URL = os.environ.get('CHATWOOT_URL', '')
    # API access token (Profile Settings -> Access Token) pra enriquecer contatos.
    CHATWOOT_API_TOKEN = os.environ.get('CHATWOOT_API_TOKEN', '')
    # ID da conta no Chatwoot (numero na URL: /app/accounts/<id>/...).
    CHATWOOT_ACCOUNT_ID = os.environ.get('CHATWOOT_ACCOUNT_ID', '')
    # Token compartilhado embutido na URL do Dashboard App; valida o iframe do
    # card. Gerar random longo (secrets.token_urlsafe(32)). Vazio = card 503.
    CHATWOOT_CARD_TOKEN = os.environ.get('CHATWOOT_CARD_TOKEN', '')
    # URL do Postgres do Chatwoot (banco SEPARADO) pro job de backup diario.
    # Vazio = backup do Chatwoot desligado.
    CHATWOOT_DATABASE_URL = os.environ.get('CHATWOOT_DATABASE_URL', '')
    # Bot de atendimento (Agent Bot do Chatwoot). Token de acesso do Agent Bot
    # (criado em Settings -> Bots) — o bot usa pra enviar mensagem e passar a
    # conversa pro humano. Vazio = bot desligado.
    CHATWOOT_BOT_TOKEN = os.environ.get('CHATWOOT_BOT_TOKEN', '')
    # Segredo na URL do webhook do bot (/crm/bot?k=...). Valida que o evento
    # veio do nosso Agent Bot. Gerar random longo. Vazio = webhook recusa tudo.
    CHATWOOT_BOT_SECRET = os.environ.get('CHATWOOT_BOT_SECRET', '')
