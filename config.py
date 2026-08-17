import os
from datetime import timedelta

# Banco de dados: PostgreSQL em produção, SQLite local
DB_DIR = os.path.join(os.path.expanduser('~'), '.padaria')
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, 'padaria.db')

DATABASE_URL = os.environ.get('DATABASE_URL', f'sqlite:///{DB_PATH}')
# Railway usa 'postgres://' mas SQLAlchemy precisa de 'postgresql://'
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)


class Config:
    # Ambiente visual de homologacao. Ativa o shell v2 e recursos de preview
    # sem mudar a experiencia do ambiente de producao.
    PREVIEW_MODE = os.environ.get('PREVIEW_MODE', '0') == '1'
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
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB (uploads de foto/atestado;
    # subiu de 10 → 25 MB em 16/06/2026 porque foto de celular passa de 10
    # fácil. Defesa em profundidade — a tela de catalogo (loja_online)
    # comprime client-side antes do upload, mas se algo escapar o servidor
    # ainda aceita. comprimir_imagem (utils.py) reduz pra ~150KB no banco.

    # Treinamento (self-host de vídeo, decisão do dono 24/07/2026): os vídeos
    # ficam no VOLUME do Railway montado em /data (o app grava/serve de lá).
    # Em dev/local (SQLite) cai numa pasta do ~/.padaria. Env
    # TREINAMENTO_MEDIA_DIR sobrepõe (Flask só absorve env declarada aqui).
    TREINAMENTO_MEDIA_DIR = os.environ.get(
        'TREINAMENTO_MEDIA_DIR',
        '/data/treinamento' if DATABASE_URL.startswith('postgresql')
        else os.path.join(DB_DIR, 'treinamento'))
    # Teto de upload SÓ da rota de vídeo (o global MAX_CONTENT_LENGTH segue
    # 25 MB pras fotos). Default 1 GB — sobe via env se precisar de aula longa.
    TREINAMENTO_MAX_VIDEO = int(
        os.environ.get('TREINAMENTO_MAX_VIDEO_MB', '1024')) * 1024 * 1024

    # Cloudflare Stream (decisão do dono 24/07/2026 após o self-host no volume
    # /data esbarrar em permissão do Railway): o vídeo do treinamento é ENVIADO
    # DIRETO do navegador pro Cloudflare (nunca passa pelo nosso servidor — sem
    # teto de 25 MB, sem volume, sem timeout de worker) e TOCA embutido num
    # iframe na NOSSA página (o funcionário não sai do site). Sem estas envs o
    # upload por Stream fica desligado e a tela avisa "não configurado" (mesmo
    # padrão do Spotify) — nada quebra.
    CLOUDFLARE_ACCOUNT_ID = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
    CLOUDFLARE_STREAM_TOKEN = os.environ.get('CLOUDFLARE_STREAM_TOKEN', '')
    # Subdomínio de entrega (customer-XXXX.cloudflarestream.com). Opcional: se
    # vazio, o serviço descobre sozinho pela API na 1ª vez e cacheia em
    # AppConfig. Pode colar só o código (customer-XXXX) ou o host inteiro.
    CLOUDFLARE_STREAM_SUBDOMAIN = os.environ.get(
        'CLOUDFLARE_STREAM_SUBDOMAIN', '')

    # Cookies de sessao — defesa em profundidade contra roubo de sessao
    # (12 atendentes logados ao dia + admins; sessao roubada = acesso
    # total a pedidos/clientes/dinheiro). Auditado em 12/06/2026: zero
    # uso de `document.cookie` no JS, todo polling e same-origin, iframe
    # do Chatwoot autentica via token na URL — flags nao quebram nada.
    # - HTTPONLY: JS nao le o cookie (se algum script malicioso entrar
    #   por CSP frouxa/extensao, nao captura sessao).
    # - SAMESITE=Lax: outro site nao consegue forcar requests no nosso
    #   em nome do usuario logado (CSRF estrutural).
    # - SECURE: cookie so via HTTPS. Condicional: prod (postgresql) liga;
    #   dev local (sqlite) deixa desligado pra http://localhost funcionar.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = DATABASE_URL.startswith('postgresql')
    # Token CSRF vale a SESSAO inteira (default do Flask-WTF = 1h). A protecao
    # CSRF vem do token ser secreto e amarrado a sessao (cookie HTTPONLY +
    # SameSite=Lax acima), nao do TTL — e o uso real aqui e aba aberta o dia
    # todo (cronograma, mapeamentos, painel): com o limite de 1h, TODO form
    # HTML clicado depois disso morria em "400 The CSRF token has expired"
    # (caso real 02/07/2026 no /telaindustriateste/enviar; antes ja tinha
    # acontecido no autosave via fetch, que ganhou retry via /auth/csrf-token).
    WTF_CSRF_TIME_LIMIT = None
    # Sessao PERMANENTE (30 dias, rolando a cada request). Sem isso o cookie de
    # sessao e "de navegador": morre ao fechar/reciclar a aba (celular mata a
    # aba com frequencia). O login SOBREVIVE (remember-me de ~1 ano), mas o
    # Flask-Login restaura o usuario numa sessao NOVA e VAZIA -> o token CSRF da
    # pagina ja aberta nao bate mais -> "Sessao de seguranca expirada" ao
    # enviar o form (caso real 25/07/2026: subir foto na ficha da receita).
    # NAO afrouxa o acesso: o remember-me ja mantinha a pessoa logada por 1 ano;
    # isto so faz o cookie de sessao (e o CSRF) durarem o suficiente pra nao
    # quebrar o form no meio do uso.
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
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
    # NF-e da loja online (Fase 5): cabecalho fiscal mandado DIRETO no
    # nota.fiscal.incluir (Plano B). A natureza DEVE bater EXATAMENTE com uma
    # natureza cadastrada no Tiny. A SERIE segue a do VNDA (1) pra a numeracao
    # ficar em ordem; trocar pra serie dedicada da loja e' so mudar a env.
    # CFOP/NCM/CST continuam vindo do cadastro do produto (via SKU). Ajustaveis
    # por env sem deploy.
    NF_NATUREZA_OPERACAO = os.environ.get(
        'NF_NATUREZA_OPERACAO', 'Venda de mercadorias')
    # NF de transferencia industria→loja (20/07/2026): texto EXATO da
    # natureza cadastrada no Tiny (informado pelo dono).
    NF_NATUREZA_TRANSFERENCIA = os.environ.get(
        'NF_NATUREZA_TRANSFERENCIA',
        'TRANSFERÊNCIA DE PRODUÇÃO DO ESTABELECIMENTO')
    NF_SERIE = os.environ.get('NF_SERIE', '1')
    # Modalidade do frete pro Tiny (codigo de LETRA, nao numerico: a API e
    # PHP e trata "0" como vazio -> "deve ser informado"). Valores Tiny:
    # 'R' = Remetente/emitente (CIF, a padaria contrata a entrega),
    # 'D' = Destinatario (FOB), 'T' = Terceiros, 'S' = sem frete.
    # Ajustavel por env se o contador pedir outro.
    NF_FRETE_POR_CONTA = os.environ.get('NF_FRETE_POR_CONTA', 'R')
    # Bot WhatsApp privado do dono (copilot read-only via Z-API).
    # ZAPI_BOT_DONO_NUMERO    = 55DDDNUMERO (so digitos) do WhatsApp do dono
    # ZAPI_BOT_WEBHOOK_TOKEN  = segredo na URL do webhook (?k=...)
    ZAPI_BOT_DONO_NUMERO = os.environ.get('ZAPI_BOT_DONO_NUMERO', '')
    ZAPI_BOT_WEBHOOK_TOKEN = os.environ.get('ZAPI_BOT_WEBHOOK_TOKEN', '')
    # Aviso no WhatsApp do dono quando pedido eh recebido na loja (com link da
    # pasta de fotos no Dropbox). Desligar: ZAPI_BOT_AVISO_RECEBIMENTO=0.
    ZAPI_BOT_AVISO_RECEBIMENTO = (
        os.environ.get('ZAPI_BOT_AVISO_RECEBIMENTO', '1') != '0')
    # Radar de saude (digest 07:30 + /admin/saude): margem abaixo disso (em %)
    # entra como critica no alerta de receitas.
    SAUDE_MARGEM_MINIMA = float(os.environ.get('SAUDE_MARGEM_MINIMA', '30') or '30')
    # Retencao de dados (LGPD + custo). Roda no cron diario APOS o backup OK
    # (nunca apaga dado que nao esteja no dump do dia). RETENCAO_AUTO=0 desliga.
    RETENCAO_AUTO = os.environ.get('RETENCAO_AUTO', '1') != '0'
    RETENCAO_LOGS_DIAS = int(os.environ.get('RETENCAO_LOGS_DIAS', '365') or '365')
    RETENCAO_CONVERSAS_DIAS = int(os.environ.get('RETENCAO_CONVERSAS_DIAS', '180') or '180')
    RETENCAO_EVENTOS_DIAS = int(os.environ.get('RETENCAO_EVENTOS_DIAS', '7') or '7')
    RETENCAO_BACKUPS_DIAS = int(os.environ.get('RETENCAO_BACKUPS_DIAS', '90') or '90')
    # Sensor do frete (PII: endereço/contato do cliente) — poda por LGPD.
    RETENCAO_FRETE_SENSOR_DIAS = int(os.environ.get('RETENCAO_FRETE_SENSOR_DIAS', '90') or '90')
    # Coordenadas da loja matriz — origem das rotas de entrega
    ROTA_ORIGEM_LAT = os.environ.get('ROTA_ORIGEM_LAT', '')
    ROTA_ORIGEM_LNG = os.environ.get('ROTA_ORIGEM_LNG', '')
    # Endereco textual da matriz — usado como origem dos links do Google Maps
    ROTA_ORIGEM_ENDERECO = os.environ.get('ROTA_ORIGEM_ENDERECO', '')
    # Chave da API do Google Maps Platform (Geocoding + Directions)
    GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

    # Avaliacoes do Google (Business Profile) — ver+responder+alerta (12/07/2026).
    # OAuth do dono (business.manage). Vazio = integracao dormente (503/no-op).
    # Precisa tambem de acesso APROVADO a Business Profile API no Google Cloud.
    GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
    GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
    # Numero do WhatsApp pra alerta de review nova (fallback ZAPI_BOT_DONO_NUMERO).
    GOOGLE_REVIEWS_NUMERO = os.environ.get('GOOGLE_REVIEWS_NUMERO', '')

    # Seru (PDV) — credenciais OAuth2 client_credentials
    # Documentacao: https://integration.plataformaseru.com.br/v1/docs
    SERU_CLIENT_ID = os.environ.get('SERU_CLIENT_ID', '')
    SERU_CLIENT_SECRET = os.environ.get('SERU_CLIENT_SECRET', '')

    # Lalamove (entregador sob demanda chamado do painel do dia).
    # Producao: https://rest.lalamove.com | sandbox: https://rest.sandbox.lalamove.com
    LALAMOVE_API_KEY = os.environ.get('LALAMOVE_API_KEY', '')
    LALAMOVE_API_SECRET = os.environ.get('LALAMOVE_API_SECRET', '')
    LALAMOVE_MARKET = os.environ.get('LALAMOVE_MARKET', 'BR')
    LALAMOVE_BASE_URL = os.environ.get('LALAMOVE_BASE_URL', '')
    LALAMOVE_ORIGEM_ENDERECO = os.environ.get('LALAMOVE_ORIGEM_ENDERECO', '')
    LALAMOVE_ORIGEM_LATLNG = os.environ.get('LALAMOVE_ORIGEM_LATLNG', '')
    LALAMOVE_REMETENTE_NOME = os.environ.get('LALAMOVE_REMETENTE_NOME', '')
    LALAMOVE_REMETENTE_FONE = os.environ.get('LALAMOVE_REMETENTE_FONE', '')

    # Modelo do bot WhatsApp do dono (fork por canal; Slack usa o default
    # Sonnet do copilot). Vazio = Opus (MODELO_WHATSAPP_DEFAULT).
    ZAPI_BOT_MODELO = os.environ.get('ZAPI_BOT_MODELO', '')

    # Token para integracao com bots externos (n8n / WhatsApp).
    # Gere com: python -c "import secrets; print(secrets.token_urlsafe(32))"
    BOT_API_TOKEN = os.environ.get('BOT_API_TOKEN', '')
    # Token da API read-only do assistente (Claude Code) — /api/claude/*.
    # Vazio = rotas desligadas (503). Gere com o mesmo comando acima.
    CLAUDE_API_TOKEN = os.environ.get('CLAUDE_API_TOKEN', '')
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
    # Numeros que o bot IGNORA (CSV): bots externos que entram em loop com o
    # nosso (ex: gov.br +556132073332, 03/07/2026 — 6 alertas ALTA sem cliente
    # real). Conversa desses numeros e resolvida em silencio, sem gastar
    # Claude e sem encher a fila humana. Formato livre (a chave canonica de
    # telefone normaliza +55/9o digito).
    CHATBOT_NUMEROS_IGNORADOS = os.environ.get('CHATBOT_NUMEROS_IGNORADOS', '')
    # Token de USUARIO do agente "Painel" (Profile Settings -> Access Token).
    # Usado pra RESPONDER conversas pelo /entregas/painel-testes — mensagens
    # aparecem como esse agente, distinto do bot. Vazio = botao Enviar do
    # painel recusa (chatwoot.painel_disponivel() = False).
    CHATWOOT_PAINEL_TOKEN = os.environ.get('CHATWOOT_PAINEL_TOKEN', '')
    # WhatsApp: iniciar conversa com o cliente pelo painel de entregas
    # (botao "Chamar cliente pelo WhatsApp" — 11/07/2026). Fora da janela de
    # 24h a Meta so deixa a EMPRESA iniciar com TEMPLATE aprovado; por isso
    # precisa do id da inbox do WhatsApp + o nome do template de utilidade
    # aprovado. Vazio em qualquer um = botao desligado (rota devolve aviso,
    # nunca quebra o painel). O id da inbox aparece na URL da inbox no
    # Chatwoot (/app/accounts/<acc>/inbox/<id>) ou via
    # GET /entregas/api/atendimento/chatwoot-inboxes (owner).
    CHATWOOT_WHATSAPP_INBOX_ID = os.environ.get('CHATWOOT_WHATSAPP_INBOX_ID', '')
    # Nome do template de utilidade aprovado na Meta (ex: 'duvida_pedido').
    # Estrutura esperada: 2 variaveis, {{1}} = nome do cliente, {{2}} =
    # codigo do pedido. Ajustar o mapeamento em chatwoot.iniciar_conversa_
    # whatsapp se o template tiver outra ordem.
    CHATWOOT_WHATSAPP_TEMPLATE = os.environ.get('CHATWOOT_WHATSAPP_TEMPLATE', '')
    # Locale do template aprovado (o codigo de idioma DA META, ex: pt_BR).
    CHATWOOT_WHATSAPP_TEMPLATE_LANG = os.environ.get(
        'CHATWOOT_WHATSAPP_TEMPLATE_LANG', 'pt_BR')
    # Corpo do template com {{1}}/{{2}} — SO pra exibicao na thread do
    # Chatwoot (a Meta manda o template aprovado de verdade). Ajustar aqui se
    # o texto aprovado na Meta for outro, senao a thread mostra um texto
    # diferente do que o cliente recebeu.
    CHATWOOT_WHATSAPP_TEMPLATE_CORPO = os.environ.get(
        'CHATWOOT_WHATSAPP_TEMPLATE_CORPO',
        'Olá {{1}}, aqui é da Opão. Ficamos com uma dúvida sobre o seu '
        'pedido {{2}}. Pode nos responder por aqui?')
    # Template do MOTOBOY Lalamove (14/07/2026, botao "Chamar motoboy" no
    # painel de entregas). OPCIONAIS: vazios = usa o template padrao acima
    # ({{1}} = nome do motorista, {{2}} = codigo do pedido). Aprovar um
    # template dedicado na Meta deixa o texto mais adequado ("sou da padaria
    # O Pao, sobre a entrega X") — dai preencher os dois.
    CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY = os.environ.get(
        'CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY', '')
    CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY_CORPO = os.environ.get(
        'CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY_CORPO', '')

    # ── Portal Wi-Fi das lojas (11/07/2026, Ribeiro do Vale) ──────────
    # Numero do WhatsApp da padaria (so digitos, com 55) pro botao wa.me do
    # portal — o cliente valida a posse do numero mandando o codigo
    # WIFI-XXXXXX pra ca (mesmo numero do atendimento/Chatwoot). Vazio =
    # portal mostra aviso de configuracao pendente.
    WIFI_PORTAL_WHATSAPP = os.environ.get('WIFI_PORTAL_WHATSAPP', '')
    # Open API do Omada (autorizar o aparelho no controlador via nuvem
    # TP-Link). Vazios = autorizacao fica pendente (o cadastro/login
    # funcionam; o enforcement do Wi-Fi e ligado depois).
    OMADA_API_URL = os.environ.get('OMADA_API_URL', '')
    OMADA_CLIENT_ID = os.environ.get('OMADA_CLIENT_ID', '')
    OMADA_CLIENT_SECRET = os.environ.get('OMADA_CLIENT_SECRET', '')
    OMADA_OMADAC_ID = os.environ.get('OMADA_OMADAC_ID', '')
    OMADA_SITE_ID = os.environ.get('OMADA_SITE_ID', '')
    # Trava dura por VOUCHER (12/07/2026 — OC200 nao fala com a Open API
    # da nuvem): abaixo deste estoque livre, avisa o dono no WhatsApp
    # (dedup 24h) pra gerar lote novo no Hotspot Manager.
    WIFI_VOUCHER_AVISO_MIN = int(
        os.environ.get('WIFI_VOUCHER_AVISO_MIN', '50'))
    # Trava dura por LOGIN via RADIUS (13/07/2026): segredo compartilhado
    # entre a ponte RADIUS (wifi_radius/bridge.py, roda num VPS) e o
    # endpoint /api/wifi/radius-check. Vazio = endpoint responde 503
    # (login por RADIUS desligado). NUNCA e o mesmo secret do RADIUS<->OC200.
    WIFI_RADIUS_TOKEN = os.environ.get('WIFI_RADIUS_TOKEN', '')

    # ── Email transacional (Postmark) — 17/06/2026 ────────────────────
    # Envio de senha/convite pra novos usuarios do gestao.*. Vazio =
    # email desligado (cadastro mostra a senha na tela como fallback).
    # Trocado do Resend pro Postmark porque o Resend exige MX em
    # subdominio pra verificar o dominio, e o Wix (host do DNS de
    # opao.online) so permite MX na raiz. Postmark valida com CNAME, que
    # o Wix aceita — sem precisar mover o DNS.
    POSTMARK_SERVER_TOKEN = os.environ.get('POSTMARK_SERVER_TOKEN', '')
    # ── Listmonk (e-mail marketing, 05/08/2026) ──
    # Roda no VPS da Vultr atrás de HTTPS. NUNCA apontar pra http:// — o
    # token vai em BasicAuth e trafegaria em claro (o service recusa).
    # Vazio = módulo dormente, nada quebra.
    LISTMONK_URL = os.environ.get(
        'LISTMONK_URL', 'https://mkt.opaopadariaartesanal.com.br')
    LISTMONK_API_USER = os.environ.get('LISTMONK_API_USER', 'api_padaria')
    LISTMONK_API_TOKEN = os.environ.get('LISTMONK_API_TOKEN', '')
    # Remetente — precisa de Sender Signature verificada no Postmark
    # (DKIM TXT na raiz + Return-Path CNAME em subdominio).
    EMAIL_REMETENTE = os.environ.get('EMAIL_REMETENTE', 'noreply@opao.online')
    EMAIL_REMETENTE_NOME = os.environ.get(
        'EMAIL_REMETENTE_NOME', 'O Pão Padaria Artesanal')
    # URL base do sistema pro link de login no email (sem barra final).
    APP_BASE_URL = os.environ.get(
        'APP_BASE_URL', 'https://gestao.opaopadariaartesanal.com.br')
    # URL pública da LOJA (e-commerce) — usada nos links dos e-mails que vão
    # pro CLIENTE (pedido, pagamento, NF, reset de senha). Separada do
    # APP_BASE_URL (admin/gestão) porque a loja vive em opao.online e o admin
    # em gestao.opaopadariaartesanal.com.br (18/06/2026).
    LOJA_BASE_URL = os.environ.get('LOJA_BASE_URL', 'https://opao.online')
    # Alerta IMEDIATO ao dono (WhatsApp) quando um cliente e BARRADO no
    # checkout por item esgotado no plano-do-dia — vira venda perdida em sinal
    # acionavel + contato do cliente. '0' desliga. Numero: LOJA_ALERTA_NUMERO
    # ou, na ausencia, ZAPI_NUMERO_DESTINO (mesmo padrao dos outros alertas).
    LOJA_ALERTA_TRAVA = os.environ.get('LOJA_ALERTA_TRAVA', '1')
    LOJA_ALERTA_NUMERO = os.environ.get('LOJA_ALERTA_NUMERO', '')
    # Hosts que servem SOMENTE a loja (admin/gestão viram 404 neles, raiz
    # redireciona pra /loja/). CSV. gestao.* NÃO entra aqui — continua full.
    LOJA_HOSTS = os.environ.get(
        'LOJA_HOSTS', 'opao.online,www.opao.online')
    # Cutover: hosts que apenas REDIRECIONAM (302) pro site novo — ex: o
    # domínio antigo do VNDA. CSV, vazio = desligado (chave liga/desliga sem
    # deploy: setar/limpar no Railway). 302 (temporário) de propósito — pra
    # poder CORTAR o redirecionamento sem ficar preso em cache de navegador
    # (301 grudaria). Destino = SITE_REDIRECT_DESTINO.
    SITE_REDIRECT_HOSTS = os.environ.get('SITE_REDIRECT_HOSTS', '')
    SITE_REDIRECT_DESTINO = os.environ.get(
        'SITE_REDIRECT_DESTINO', 'https://opao.online')
    # URL do Chatwoot pra instruir o atendente (reusa CHATWOOT_URL se setado).
    CHATWOOT_PUBLIC_URL = os.environ.get(
        'CHATWOOT_PUBLIC_URL', 'https://atendimento.opaopadariaartesanal.com.br')
    # Token PÚBLICO da "Website Inbox" do Chatwoot (Settings > Inboxes >
    # <inbox> > Configuration > "Website Token"). Vai no HTML do site, pode
    # ser commitado sem risco (não é segredo — qualquer um vê no fonte da
    # página). Default = inbox do site da padaria (19/06/2026), pra o chat
    # ligar SEM depender de env var no Railway. Pra trocar/desligar, setar
    # CHATWOOT_WEBSITE_TOKEN no Railway ('' desliga o widget — fail-open).
    CHATWOOT_WEBSITE_TOKEN = os.environ.get(
        'CHATWOOT_WEBSITE_TOKEN', 'GP6SHfZfjsCqEH1ZnPybdUpf')

    # ── Pagamento (Pagar.me / Stone) — Fase 4 loja online (17/06/2026) ──
    # API v5 (https://api.pagar.me/core/v5), Basic auth com a SECRET KEY
    # (sk_test_… sandbox / sk_live_… producao) como usuario e senha vazia.
    # Segredos NUNCA vem pelo chat — o dono cadastra no Railway > Variables.
    PAGARME_API_KEY = os.environ.get('PAGARME_API_KEY', '')        # sk_test_/sk_live_
    PAGARME_PUBLIC_KEY = os.environ.get('PAGARME_PUBLIC_KEY', '')  # pk_… (tokenizacao no front)
    # Segredo NOSSO no ?k= da URL do webhook (geramos e registramos no painel
    # Pagar.me). Independe da assinatura interna deles — mesmo padrao do
    # Chatwoot/Slack/Zapi. Vazio = webhook recusa tudo.
    PAGARME_WEBHOOK_SECRET = os.environ.get('PAGARME_WEBHOOK_SECRET', '')

    # ── Analytics e remarketing (Fase 8 — cutover VNDA → loja propria) ──
    # Ambos opt-in por env var. Sem env, o template NAO injeta nada (zero
    # impacto LGPD e zero requisicao a terceiro). Carregamento condicionado
    # ao aceite no banner de cookies (`cookies_consent=aceitar`); sem aceite,
    # so o cookie de sessao e o de carrinho ficam ativos.
    # GA4: cria propriedade em https://analytics.google.com -> ID 'G-XXXXXXXXXX'.
    GA4_ID = os.environ.get('GA4_ID', '')
    # Meta Pixel: Business Manager -> Events Manager -> ID de 15 digitos.
    META_PIXEL_ID = os.environ.get('META_PIXEL_ID', '')
    # Purchase server-side (13/07/2026 — Pix pago no app do banco sem voltar
    # a pagina nao disparava o purchase do navegador). Ver
    # app/services/analytics_server.py. Sem os segredos = no-op logado.
    # GA4 Admin -> Data Streams -> Measurement Protocol API secrets:
    GA4_API_SECRET = os.environ.get('GA4_API_SECRET', '')
    # Events Manager -> pixel -> Conversions API -> gerar token:
    META_CAPI_TOKEN = os.environ.get('META_CAPI_TOKEN', '')
    # Kill-switch do reporte server-side ('0' desliga):
    ANALYTICS_SERVER = os.environ.get('ANALYTICS_SERVER', '1')

    # ── Spotify (widget 🎵 da tela do padeiro, 15/07/2026) ──
    # App criado pelo dono em developer.spotify.com; o servidor controla o
    # aparelho de som via Spotify Connect (app/services/spotify.py). Flask
    # NAO absorve env var sozinho — sem estas linhas o app nunca ve as
    # variaveis do Railway (bug real do primeiro deploy da feature).
    SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
    SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')
    # Opcional: fixa a redirect URI (sem ela, deriva da URL publica).
    SPOTIFY_REDIRECT_URI = os.environ.get('SPOTIFY_REDIRECT_URI', '')
