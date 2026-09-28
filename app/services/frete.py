"""Faixas de frete por distância até a padaria (Brooklin).

Fonte: mapa "Fretes O pão" do My Maps, exportado em KML pelo dono em
10/06/2026 ("Faixas de frete O Pão sem sobreposição"). São anéis
concêntricos de 1 km a partir da loja do Brooklin (Rua Ribeiro do Vale,
455): grátis até 1 km, e a cada km adicional soma R$5, até o limite de
25 km (R$120). Além de 25 km = fora da área de entrega do site.

O valor daqui é ESTIMATIVA pro atendimento (bot/equipe) — o valor que
vale é o do checkout do site. Se o dono redesenhar o mapa, atualizar as
constantes abaixo (e o teste de faixas).

Geocodificação (ordem completa em `_geocodificar_impl`):
  1. Google (preciso, com teto diário POR CANAL e cache — o preview anônimo
     tem sub-teto próprio);
  2. BrasilAPI v2 pelo CEP — só os metadados oficiais (logradouro, bairro,
     cidade) pra re-tentar o Google e validar o Nominatim; a coordenada do
     CEP é só o ÚLTIMO recurso, marcada imprecisa;
  3. Nominatim (OpenStreetMap) com guards de homônimo.
"""
import logging
import re
import unicodedata
from math import asin, ceil, cos, radians, sin, sqrt

import requests

logger = logging.getLogger(__name__)


def _norm_cidade(s):
    """minúsculo, sem acento, sem pontuação — pra comparar nome de cidade
    entre a BrasilAPI (Correios) e o OSM sem tropeçar em acento/caixa."""
    s = unicodedata.normalize('NFKD', (s or '')).encode('ascii', 'ignore').decode()
    return ' '.join(re.sub(r'[^\w\s]', ' ', s).lower().split())


# Códigos de erro do consultar_frete (máquina) -> mensagem pro cliente. Fonte
# ÚNICA: o /loja/api/frete e o POST do checkout traduzem pelos mesmos textos
# (antes o AJAX mostrava o código cru "nao_encontrado" pro cliente).
_MENSAGENS_ERRO = {
    'endereco_vazio': 'Informe o endereço ou o CEP.',
    'nao_encontrado': 'Não consegui localizar esse endereço. '
                      'Confira o endereço ou o CEP.',
}


def mensagem_erro(codigo):
    """Mensagem amigável pro código de erro do consultar_frete."""
    return _MENSAGENS_ERRO.get(
        codigo, 'Não consegui calcular o frete. Tente de novo.')

# Centro dos anéis (centroide do KML) = padaria do Brooklin.
CENTRO_LAT = -23.598678
CENTRO_LNG = -46.693661
KM_GRATIS = 1.0          # até aqui, frete grátis
VALOR_POR_KM = 5.0       # cada km adicional (anel de 1 km) soma R$5
RAIO_MAX_KM = 25.0       # além disso, fora da área de entrega do site
# Fora-da-área: o painel registra TODOS, mas o WhatsApp do dono só dispara pra
# quem ficou PERTO da borda (até aqui além do limite = "quase comprou", vale
# chamar). Muito além = cliente de outra cidade, não é venda perdida real.
MARGEM_ALERTA_FORA_KM = 5.0

_TIMEOUT = 8
# Nominatim exige User-Agent identificável (politica de uso do OSM).
_UA = {'User-Agent': 'opao-padaria-atendimento/1.0 (gestao.opaopadariaartesanal.com.br)'}


def distancia_km(lat, lng):
    """Haversine até o centro dos anéis, em km."""
    dlat = radians(lat - CENTRO_LAT)
    dlng = radians(lng - CENTRO_LNG)
    a = (sin(dlat / 2) ** 2
         + cos(radians(CENTRO_LAT)) * cos(radians(lat)) * sin(dlng / 2) ** 2)
    return 2 * 6371.0 * asin(sqrt(a))


def valor_para_distancia(km):
    """Valor do frete pro anel onde a distância cai. None = fora da área.

    Limites batem com o KML: cada anel fecha no km cheio (faixa R$5 vai de
    1 a 2 km — 2.0 km ainda é R$5)."""
    if km is None or km < 0 or km > RAIO_MAX_KM:
        return None
    if km <= KM_GRATIS:
        return 0.0
    return VALOR_POR_KM * (ceil(km) - 1)


def _extrair_cep(texto):
    m = re.search(r'(\d{5})[\s.-]?(\d{3})', texto or '')
    return f'{m.group(1)}{m.group(2)}' if m else None


def _formatar_cep(cep):
    """'04561000' -> '04561-000' (forma que o Nominatim geocodifica melhor)."""
    d = re.sub(r'\D', '', cep or '')
    return f'{d[:5]}-{d[5:]}' if len(d) == 8 else (cep or '')


def _geocodificar_cep(cep):
    """BrasilAPI v2: CEP -> (lat, lng, rótulo, ref) ou None (sem coords/erro).

    `ref` = {'cidade', 'bairro', 'rua'} resolvidos pelo Correios — sinal de
    sanidade MAIS confiável que o postcode do OSM (que às vezes vem errado no
    nó certo). 'cidade' valida o candidato do Nominatim; 'rua' (logradouro
    oficial) alimenta a retentativa do Google."""
    try:
        r = requests.get(f'https://brasilapi.com.br/api/cep/v2/{cep}',
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        d = r.json()
        coords = ((d.get('location') or {}).get('coordinates') or {})
        lat, lng = coords.get('latitude'), coords.get('longitude')
        cidade, bairro = d.get('city'), d.get('neighborhood')
        rotulo = ', '.join(x for x in (d.get('street'), bairro, cidade) if x)
        # 'rua' = logradouro OFICIAL dos Correios — usado pra re-tentar o
        # Google com o nome certo quando o cliente digitou o nome errado
        # (caso Mirelle 17/07/2026: "Rua Cândido de Azevedo Marques" sem o
        # "Joaquim" → nenhum geocoder achava; o nome oficial resolve).
        ref = {'cidade': cidade, 'bairro': bairro, 'rua': d.get('street')}
        if lat and lng:
            return float(lat), float(lng), rotulo or f'CEP {cep}', ref
        # Sem coordenadas: devolve rótulo + ref pro fallback geocodificar.
        return (None, None, rotulo, ref) if rotulo else None
    except (requests.RequestException, ValueError):
        logger.warning('BrasilAPI falhou pro CEP %s', cep)
        return None


def _geocodificar_texto(texto, ref=None, cep_ref=None, postcode_estrito=False,
                        falhas=None):
    """Nominatim (OSM): endereço livre -> (lat, lng, rótulo) ou None.

    Sanidade contra homônimo (05/07/2026; revisto 09/07/2026), dois sinais
    por candidato, nesta ordem:
      1. CIDADE — quando o candidato traz cidade no addressdetails E `ref`
         tem a cidade resolvida pela BrasilAPI: REJEITA se divergir. É o
         sinal FORTE. O postcode do OSM às vezes vem ERRADO no nó certo
         (Alameda Porcelana, São Caetano do Sul / CEP 09531 vinha etiquetada
         08671 no OSM) — rejeitar por ele barrava endereço válido do ABC.
         Cidade batendo, aceita mesmo com postcode divergente.
      2. POSTCODE (`cep_ref`) — fallback quando o candidato NÃO traz cidade:
         rejeita se o prefixo de 4 dígitos diverge. Pega homônimo de outro
         distrito quando o OSM não dá cidade ("Rua Nova York" Brooklin×Grajaú,
         "Rua Martins Fontes" Centro×Arujá, 05/07/2026).
    Só rejeita em divergência POSITIVA (sem o dado, aceita). Até 3 candidatos.

    `falhas` (lista): recebe 'rede' quando o Nominatim não respondeu (erro de
    conexão, timeout, HTTP != 200) — diferente de "não achou". A cadeia usa
    isso para parar: insistir num serviço fora custava 5 × 8 s por cotação
    (revisão 28/09/2026).
    """
    consulta = texto.strip()
    if 'são paulo' not in consulta.lower() and 'sao paulo' not in consulta.lower():
        consulta += ', São Paulo, Brasil'
    ref_cidade = _norm_cidade((ref or {}).get('cidade'))
    cep_pref = re.sub(r'\D', '', cep_ref or '')[:4]
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
                         params={'q': consulta, 'format': 'json', 'limit': 3,
                                 'addressdetails': 1, 'countrycodes': 'br'},
                         headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            if falhas is not None:
                falhas.append('rede')
            return None
        candidatos = r.json()
        if not isinstance(candidatos, list):
            # Resposta de erro em JSON (objeto) no lugar da lista: iterar as
            # chaves estourava AttributeError fora do except — o checkout
            # virava 500 em vez de seguir pra próxima tentativa da cadeia.
            logger.warning('Nominatim devolveu resposta inesperada pra %r',
                           texto[:80])
            return None
        for h in candidatos:
            if not isinstance(h, dict):
                continue
            addr = h.get('address') or {}
            nome = (h.get('display_name') or '')[:120]
            cand_cidade = _norm_cidade(
                addr.get('city') or addr.get('town') or addr.get('municipality')
                or addr.get('village') or addr.get('city_district'))
            if ref_cidade and cand_cidade:
                # Sinal forte: cidade. Ignora o postcode do OSM (frouxo).
                if cand_cidade != ref_cidade:
                    logger.warning('geocode descartado (cidade diverge): '
                                   'pedimos %r, candidato %r (%r)',
                                   ref_cidade, cand_cidade, nome)
                    continue
            elif cep_pref:
                # Sem cidade no candidato: cai no guard de postcode.
                pc = re.sub(r'\D', '', addr.get('postcode') or '')
                if postcode_estrito:
                    # Exige match POSITIVO: sem postcode batendo, NÃO aceita.
                    # Usado na tentativa "rua+cidade" (homônimo da MESMA cidade,
                    # ex: Guararapes Brooklin×Lapa) — sem isso, candidato sem
                    # postcode passava e cobrava frete errado.
                    if not (pc and pc[:4] == cep_pref):
                        logger.warning('geocode descartado (sem CEP p/ confirmar '
                                       'distrito): pedimos %s (%r)', cep_pref, nome)
                        continue
                elif pc and pc[:4] != cep_pref:
                    logger.warning('geocode descartado (CEP diverge): pedimos '
                                   '%s, candidato %s (%r)', cep_pref, pc, nome)
                    continue
            try:
                return (float(h['lat']), float(h['lon']),
                        h.get('display_name', consulta))
            except (KeyError, TypeError, ValueError):
                continue          # candidato sem coordenada legível
        return None
    except requests.RequestException:
        if falhas is not None:
            falhas.append('rede')
        logger.warning('Nominatim falhou pra %r', texto)
        return None
    except ValueError:
        logger.warning('Nominatim respondeu JSON ilegível pra %r', texto)
        return None


def _extrair_numero(texto):
    """Número da casa a partir do endereço em uma linha: a 1ª parte (após a
    rua) que COMEÇA com dígitos. Ignora o CEP (removido antes) pra ele não
    ser confundido com número. '' quando não achar."""
    t = re.sub(r'\d{5}[\s.-]?\d{3}', '', texto or '')
    partes = [p.strip() for p in t.split(',') if p.strip()]
    for p in partes[1:3]:
        m = re.match(r'^(\d+)\b', p)
        if m:
            return m.group(1)
    return ''


def simplificar_endereco(texto):
    """Reduz um endereço completo pra 'rua, numero, cidade' — complemento
    (apto/bloco), bairro, estado e CEP costumam DERRUBAR o Nominatim.
    'Rua X, 123, apto 45, Moema, São Paulo, SP, 04500-000'
    -> 'Rua X, 123, São Paulo'."""
    t = re.sub(r'\d{5}[\s.-]?\d{3}', '', texto or '')
    partes = [p.strip() for p in t.split(',') if p.strip()]
    if not partes:
        return None
    rua = partes[0]
    numero = _extrair_numero(texto)
    base = f'{rua}, {numero}' if numero else rua
    return f'{base}, São Paulo'


# ── Google Maps como fonte PRECISA (opt-in, com teto e kill-switch) ─────────
#
# Contexto (09/07/2026): a cadeia grátis (BrasilAPI+Nominatim) erra homônimo e
# não tem coordenada de muitos CEPs — barra venda e, PIOR, manda a Lalamove pro
# lugar errado. O Google (já usado no sistema pra rotas de entrega) é preciso a
# nível de porta. Mas o /api/frete é PÚBLICO: por isso o Google entra com
# TETO DIÁRIO (custo/abuso não pode disparar) + kill-switch + cache permanente
# (paga 1x por endereço) + FALLBACK pra cadeia grátis se faltar/cair.
#
# Cota POR CANAL (auditoria do checkout, 27/09/2026): o teto era um contador
# único, e o preview ANÔNIMO (/loja/api/frete, qualquer texto) podia esgotá-lo
# sozinho — aí o geocode preciso sumia até a meia-noite também do POST do
# checkout e do despacho da Lalamove. Agora o preview tem um SUB-TETO
# (`FRETE_GOOGLE_MAX_DIA_PREVIEW`, padrão 60% do teto) e checkout/despacho
# usam o restante do teto total. O teto total continua sendo o limite de
# custo do dia (o preview nunca passa do sub-teto; o checkout pode usar o que
# o preview não usou).

CANAL_CHECKOUT = 'checkout'   # POST do checkout (autoritativo) — padrão
CANAL_PREVIEW = 'preview'     # cotação anônima (/loja/api/frete)
CANAL_DESPACHO = 'despacho'   # ponto de entrega da Lalamove/painel
CANAIS = (CANAL_CHECKOUT, CANAL_PREVIEW, CANAL_DESPACHO)

_TETO_PADRAO = 500
_PREVIEW_PADRAO_PCT = 60      # limite do preview anônimo, em % do teto total
# Contador do dia em AppConfig: 'AAAA-MM-DD|total|preview'. O formato antigo
# ('AAAA-MM-DD|total') é lido como preview=0 — o total do dia sobrevive ao
# deploy que trouxe o sub-teto.
_CHAVE_USO_GOOGLE = 'frete_google_dia'
_TENTATIVAS_RESERVA = 5


def _google_frete_ativo():
    from flask import current_app
    return str(current_app.config.get('FRETE_GOOGLE', '1')).strip().lower() \
        not in ('0', 'false', 'no', '')


def _normalizar_canal(canal):
    """Canal válido; desconhecido = checkout (fail-safe) com ERROR no log —
    é erro de programação, mas não pode derrubar a cotação do cliente."""
    if canal in CANAIS:
        return canal
    logger.error('frete: canal desconhecido %r — tratado como checkout', canal)
    return CANAL_CHECKOUT


def _int_config(nome, padrao):
    """Inteiro >= 0 do app.config. Vazio = padrão; ilegível = padrão com
    WARNING (config torta não pode sumir em silêncio)."""
    from flask import current_app
    bruto = current_app.config.get(nome)
    if bruto is None or str(bruto).strip() == '':
        return padrao
    try:
        return max(0, int(str(bruto).strip()))
    except ValueError:
        logger.warning('frete: %s=%r ilegível — usando %s', nome, bruto, padrao)
        return padrao


def tetos_google():
    """(teto_total, teto_preview) de chamadas REMOTAS ao Google por dia.

    `FRETE_GOOGLE_MAX_DIA` = teto total (padrão 500). 0 = nenhuma chamada
    remota (cache continua valendo); pra desligar o Google de vez, use o
    kill-switch `FRETE_GOOGLE=0`.
    `FRETE_GOOGLE_MAX_DIA_PREVIEW` = sub-teto do preview anônimo: número
    absoluto ('200') ou percentual do total ('60%'). Vazio = 60% do total.
    Nunca passa do total."""
    from flask import current_app
    teto = _int_config('FRETE_GOOGLE_MAX_DIA', _TETO_PADRAO)
    padrao = teto * _PREVIEW_PADRAO_PCT // 100
    bruto = str(current_app.config.get('FRETE_GOOGLE_MAX_DIA_PREVIEW')
                or '').strip()
    preview = padrao
    if bruto:
        try:
            if bruto.endswith('%'):
                pct = float(bruto[:-1].strip().replace(',', '.'))
                preview = int(teto * pct / 100)
            else:
                preview = int(bruto)
        except (ValueError, OverflowError):
            logger.warning('frete: FRETE_GOOGLE_MAX_DIA_PREVIEW=%r ilegível — '
                           'usando %s%% do teto', bruto, _PREVIEW_PADRAO_PCT)
            preview = padrao
    return teto, max(0, min(preview, teto))


def _ler_uso(bruto, hoje_iso):
    """'AAAA-MM-DD|total[|preview]' -> (total, preview) de HOJE; outro dia ou
    vazio = (0, 0)."""
    partes = (bruto or '').split('|')
    if len(partes) < 2 or partes[0] != hoje_iso:
        return 0, 0
    total = int(partes[1]) if partes[1].isdigit() else 0
    preview = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else 0
    return total, preview


def uso_google_hoje():
    """Uso do teto hoje, pro painel do dono: {'total', 'preview', 'teto',
    'teto_preview'}."""
    from app.models import AppConfig
    from app.utils import hoje
    total, preview = _ler_uso(AppConfig.get(_CHAVE_USO_GOOGLE), hoje().isoformat())
    teto, teto_preview = tetos_google()
    return {'total': total, 'preview': preview, 'teto': teto,
            'teto_preview': teto_preview}


# Aviso de teto esgotado: 1 linha por (dia, canal) por processo — antes saía
# um WARNING a cada cotação depois de esgotar (ruído proporcional ao tráfego
# anônimo).
_TETO_AVISADO = set()


def _reservar_vaga_google(canal):
    """Reserva 1 vaga do teto DIÁRIO de chamadas REMOTAS ao Google. True se
    coube. Preview precisa caber no sub-teto E no total; checkout/despacho,
    só no total.

    Atômico entre workers: compare-and-swap no valor do AppConfig (UPDATE ...
    WHERE value = <lido>); perdeu a corrida, relê e tenta de novo.

    Conexão PRÓPRIA (`db.engine.begin()`, como a consulta de CNPJ): o contador
    nunca commita, descarta nem solta as travas da transação do CHAMADOR —
    a versão em savepoint fazia `commit` da sessão do checkout/kit no meio da
    compra (revisão 28/09/2026). Erro de banco = sem Google (a cadeia grátis
    segue)."""
    from sqlalchemy import insert, select, update
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError

    from app.extensions import db
    from app.models import AppConfig
    from app.utils import hoje

    tabela = AppConfig.__table__
    teto, teto_preview = tetos_google()
    hoje_iso = hoje().isoformat()
    for _ in range(_TENTATIVAS_RESERVA):
        esgotado = None
        gravou = False
        try:
            with db.engine.begin() as conn:
                linha = conn.execute(
                    select(tabela.c.value)
                    .where(tabela.c.key == _CHAVE_USO_GOOGLE)).first()
                bruto = linha[0] if linha else None
                total, preview = _ler_uso(bruto, hoje_iso)
                if total >= teto:
                    esgotado = (f'teto diário do Google esgotado '
                                f'({total}/{teto}, canal {canal})')
                elif canal == CANAL_PREVIEW and preview >= teto_preview:
                    esgotado = (f'sub-teto do preview do Google esgotado '
                                f'({preview}/{teto_preview}) — checkout/'
                                'despacho seguem com o restante')
                else:
                    novo = (f'{hoje_iso}|{total + 1}|'
                            f'{preview + (1 if canal == CANAL_PREVIEW else 0)}')
                    if linha is None:
                        # Unique da chave: outro worker criou a linha agora →
                        # IntegrityError → relê.
                        conn.execute(insert(tabela).values(
                            key=_CHAVE_USO_GOOGLE, value=novo))
                        gravou = True
                    else:
                        atual = (tabela.c.value.is_(None) if bruto is None
                                 else tabela.c.value == bruto)
                        res = conn.execute(
                            update(tabela)
                            .where(tabela.c.key == _CHAVE_USO_GOOGLE, atual)
                            .values(value=novo))
                        gravou = res.rowcount == 1
        except IntegrityError:
            continue                  # outro worker criou a linha: relê
        except SQLAlchemyError as exc:
            logger.warning('frete: contador do teto do Google indisponível '
                           '(%s) — sem Google nesta cotação', type(exc).__name__)
            return False
        if esgotado:
            marca = (hoje_iso, canal, esgotado.split(' (')[0])
            if marca not in _TETO_AVISADO:
                _TETO_AVISADO.add(marca)
                logger.warning('frete: %s', esgotado)
            return False
        if gravou:
            return True
        # CAS perdeu (outro worker gravou entre a leitura e o UPDATE): relê.
    logger.warning('frete: reserva no teto do Google perdeu %s corridas '
                   'seguidas — sem Google nesta cotação', _TENTATIVAS_RESERVA)
    return False


def _google_geocode(texto, numero_entrega=None, canal=CANAL_CHECKOUT):
    """Google (cacheado) pro frete. (lat, lng) ou None. Cache (positivo OU
    negativo — `google_maps.cache_preciso`) não consome o teto (custo zero);
    só a chamada REMOTA reserva vaga, e só se houver chave configurada.
    `canal` decide de qual cota sai a vaga (ver `_reservar_vaga_google`).
    Nunca levanta — fora de app context (thread do bot) ou sem chave, retorna
    None e cai na cadeia grátis."""
    if not texto:
        return None
    try:
        if not _google_frete_ativo():
            return None
        from app.services import google_maps
        estado, coords = google_maps.cache_preciso(texto, numero_entrega)
        if estado == 'preciso':
            return coords                     # hit PRECISO: sem custo/teto
        if estado == 'negativo':
            return None                       # aproximado/ZERO_RESULTS: grátis
        if not google_maps.tem_chave():
            return None                       # sem chave não há chamada remota
        if not _reservar_vaga_google(_normalizar_canal(canal)):
            return None
        # geocode_preciso: só devolve quando o Google achou o ENDEREÇO (não o
        # centroide da cidade) — senão None e cai na cadeia grátis (com guards).
        if numero_entrega is not None:
            return google_maps.geocode_preciso(texto, numero_entrega=numero_entrega)
        return google_maps.geocode_preciso(texto)
    except Exception:  # noqa: BLE001 — geocode nunca pode quebrar o frete
        logger.exception('frete: geocode Google falhou pra %r', texto[:80])
        return None


def _chave_geocode(texto):
    """Chave do cache de geocode (mesma normalização do google_maps)."""
    from app.services import google_maps
    return google_maps._normalizar_chave(texto)


def _geocodificar_impl(endereco_ou_cep, canal=CANAL_CHECKOUT):
    """Núcleo do geocode. Devolve `(geo, impreciso, fonte)`:
    - `geo` = (lat, lng, rotulo) ou None;
    - `impreciso` = True quando o endereço não foi achado na porta: só o CEP
      resolveu (centroide do distrito ou a coordenada da BrasilAPI) ou o
      cliente deu número e só a RUA foi achada;
    - `fonte` in {'latlng','google','gratis','rua_sem_numero',
      'cep_centroide','brasilapi'} — pro sensor.

    Ordem: 0. "lat,lng" colado; 1. GOOGLE com o texto (preciso, se ativo);
    2. BrasilAPI pelo CEP — só METADADOS (logradouro/bairro/cidade oficiais):
       2a. GOOGLE com o logradouro OFICIAL; depois a cadeia do Nominatim:
    COM número do cliente — 3. texto; 4. simplificado; 5. rótulo oficial sem
    número (IMPRECISO); SEM número — o rótulo oficial vem primeiro (é o
    melhor ponto possível) e depois texto/simplificado; 6. rua+cidade
    (postcode estrito); 7. só o CEP (centroide — IMPRECISO); 8. ÚLTIMO
    recurso: a coordenada do CEP na BrasilAPI (IMPRECISO). Nominatim fora
    (rede/timeout/HTTP) encerra a cadeia na hora — com a coordenada da
    BrasilAPI, ela sai imprecisa; sem ela, 'nao_encontrado'.

    A coordenada da BrasilAPI NÃO é confiável: devolveu o MESMO centro de São
    Paulo para CEPs diferentes (corridas 373/375/370, 03/09/2026). Até a
    auditoria de 27/09/2026 ela era aceita como PRECISA e pulava a retentativa
    do Google — frete cotado num ponto que pode estar a km do endereço, sem
    alerta ao dono. Agora ela só resgata a venda quando nada mais resolveu, e
    marcada como imprecisa (o chamador alerta o dono e registra no sensor)."""
    texto = (endereco_ou_cep or '').strip()
    if not texto:
        return None, False, None
    m = re.match(r'^(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)$', texto)
    if m:
        return (float(m.group(1)), float(m.group(2)), texto), False, 'latlng'
    # Google primeiro (preciso a nível de porta; conserta homônimo e Lalamove).
    g = _google_geocode(texto, canal=canal)
    if g:
        return (g[0], g[1], texto), False, 'google'
    geo = None
    ref = None
    coord_brasilapi = None
    cep = _extrair_cep(texto)
    if cep:
        cep_geo = _geocodificar_cep(cep)
        if cep_geo:
            ref = cep_geo[3]             # {'cidade','bairro','rua'} do Correios
            if cep_geo[0] is not None:
                coord_brasilapi = cep_geo[:3]   # só como ÚLTIMO recurso
            # BrasilAPI conhece o logradouro: ANTES da cadeia grátis, re-tenta
            # o GOOGLE com o logradouro OFICIAL dos Correios — com ou sem
            # coordenada do CEP (ela não é confiável; ver docstring). O passo
            # 1 (texto cru) falha quando o cliente digitou o nome da rua
            # errado/incompleto, mas o nome oficial resolve (caso Mirelle
            # 17/07/2026: "Rua Cândido de Azevedo Marques" sem o "Joaquim" →
            # nao_encontrado; com o oficial → Google 1,9km, R$5). Mesmo teto/
            # cache/kill-switch/canal do passo 1; falhou → cadeia grátis.
            if (ref or {}).get('rua'):
                numero = _extrair_numero(texto)
                canonico = ', '.join(x for x in (
                    ref['rua'], numero, ref.get('bairro'), ref.get('cidade'),
                    _formatar_cep(cep)) if x)
                # Mesmo texto do passo 1 = mesma chave de cache: não paga 2x.
                if _chave_geocode(canonico) != _chave_geocode(texto):
                    g2 = _google_geocode(canonico, canal=canal)
                    if g2:
                        return (g2[0], g2[1], canonico), False, 'google'
    falhas = []

    def _parar():
        """Nominatim fora (rede/timeout/HTTP): as próximas etapas também usam
        ele — insistir custava até 5 × 8 s por cotação, travando as threads
        do app, e piorava o bloqueio por excesso de chamadas. Com a
        coordenada da BrasilAPI em mãos, ela resgata a venda (imprecisa)."""
        return bool(falhas)

    def _saida_sem_nominatim():
        if coord_brasilapi:
            logger.warning('frete: Nominatim fora — coordenada do CEP na '
                           'BrasilAPI para %r (imprecisa)', texto[:200])
            return coord_brasilapi, True, 'brasilapi'
        logger.warning('frete: Nominatim fora e sem outra fonte para %r',
                       texto[:200])
        return None, False, None

    numero_cliente = _extrair_numero(texto)
    rotulo_oficial = cep_geo[2] if (cep and cep_geo) else None

    def _rotulo():
        """Rótulo oficial dos Correios (rua + bairro + cidade, SEM número),
        validado por CIDADE (barra Arujá; o postcode frouxo do OSM não
        derruba — caso ABC)."""
        return _geocodificar_texto(rotulo_oficial, ref=ref, cep_ref=cep,
                                   falhas=falhas)

    sem_numero = False
    # Sem número do cliente, o rótulo oficial é o melhor ponto possível e vem
    # primeiro, como antes. COM número, o texto do cliente vem antes: o rótulo
    # sem número devolve um ponto qualquer da rua (a km do endereço numa
    # avenida longa) e era aceito como preciso antes mesmo de tentar o número
    # (revisão 28/09/2026).
    if rotulo_oficial and not numero_cliente:
        geo = _rotulo()
        if _parar():
            return _saida_sem_nominatim()
    if not geo or geo[0] is None:
        geo = _geocodificar_texto(texto, ref=ref, cep_ref=cep, falhas=falhas)
        if _parar():
            return _saida_sem_nominatim()
    if not geo or geo[0] is None:
        simples = simplificar_endereco(texto)
        if simples and simples.lower() != texto.lower():
            # O simplificado perde o BAIRRO E a cidade real (vira "São Paulo"):
            # aqui NÃO dá pra validar por cidade, então usa só o guard de
            # postcode — é o que barra a "Rua Nova York" do Grajaú vs Brooklin
            # (05/07/2026). Endereço de fora da capital que só resolve aqui é
            # limitação conhecida do último fallback.
            geo = _geocodificar_texto(simples, cep_ref=cep, falhas=falhas)
            if _parar():
                return _saida_sem_nominatim()
    if (not geo or geo[0] is None) and rotulo_oficial and numero_cliente:
        # O número não foi localizado: o rótulo oficial acha a RUA, não a
        # porta — cotação marcada imprecisa (o chamador alerta o dono).
        geo = _rotulo()
        if _parar():
            return _saida_sem_nominatim()
        sem_numero = bool(geo and geo[0] is not None)
    if (not geo or geo[0] is None) and cep:
        # RUA + cidade (sem número/bairro/UF): a string cheia às vezes derruba
        # o Nominatim e o simplificado-com-número cai no HOMÔNIMO (ex: "Rua
        # Guararapes" existe no Brooklin E na Lapa). Sem cidade de referência
        # aqui (homônimo é MESMA cidade), o guard de postcode barra o de outro
        # distrito. Caso real 09/07/2026.
        rua = texto.split(',')[0].strip()
        cidade = (ref or {}).get('cidade') or 'São Paulo'
        if rua:
            # postcode ESTRITO: só aceita se o OSM confirmar o distrito (match
            # positivo de CEP) — senão é seguro cair no CEP-só abaixo, em vez
            # de arriscar o homônimo da mesma cidade.
            geo = _geocodificar_texto(f'{rua}, {cidade}', cep_ref=cep,
                                      postcode_estrito=True, falhas=falhas)
            if _parar():
                return _saida_sem_nominatim()
            sem_numero = bool(numero_cliente and geo and geo[0] is not None)
    if (not geo or geo[0] is None) and cep:
        # ÚLTIMO RECURSO: geocodifica só o CEP (centroide do distrito). Menos
        # preciso — pode super OU subestimar o frete e, na borda de um CEP
        # grande, inverter o "fora da área" — mas RESGATA a venda quando a
        # BrasilAPI não tem coordenada e nenhuma variante do endereço resolve.
        # Marca IMPRECISO pro caller alertar o dono (decisão do dono 09/07).
        geo = _geocodificar_texto(_formatar_cep(cep), cep_ref=cep,
                                  falhas=falhas)
        if _parar():
            return _saida_sem_nominatim()
        if geo and geo[0] is not None:
            return geo, True, 'cep_centroide'
    if (not geo or geo[0] is None) and coord_brasilapi:
        # ÚLTIMO RECURSO MESMO: a coordenada do CEP na BrasilAPI. Mesma classe
        # do centroide acima (IMPRECISO → o chamador alerta o dono e registra
        # no sensor), com fonte própria pra dar pra distinguir no painel.
        logger.warning('frete: só a coordenada do CEP na BrasilAPI resolveu '
                       '%r — cotação imprecisa', texto[:200])
        return coord_brasilapi, True, 'brasilapi'
    if not geo or geo[0] is None:
        logger.warning('geocodificacao falhou em todas as tentativas: %r',
                       texto[:200])
        return None, False, None
    if sem_numero:
        logger.warning('frete: número não localizado, cotado pela rua %r — '
                       'cotação imprecisa', texto[:200])
        return geo, True, 'rua_sem_numero'
    return geo, False, 'gratis'


def geocodificar_entrega(endereco):
    """Ponto para despacho: endereço/número validado ou coordenada explícita.

    A BrasilAPI devolveu o MESMO centro de São Paulo para CEPs diferentes
    nas corridas 373/375/370 (03/09/2026). Aqui só usamos seus metadados
    postais para limpar o endereço; nunca as coordenadas do CEP. A cadeia
    de estimativa de frete não pode decidir para onde enviar um motorista.
    """
    texto = (endereco or '').strip()
    m = re.fullmatch(r'(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)', texto)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        return (lat, lng, texto) if -90 <= lat <= 90 and -180 <= lng <= 180 else None
    numero = _extrair_numero(texto)
    if not numero:
        return None
    g = _google_geocode(texto, numero_entrega=numero, canal=CANAL_DESPACHO)
    if g:
        return g[0], g[1], texto
    cep = _extrair_cep(texto)
    postal = _geocodificar_cep(cep) if cep else None
    ref = postal[3] if postal else {}
    if ref.get('rua') and ref.get('cidade'):
        canonico = ', '.join(x for x in (
            ref['rua'], numero, ref.get('bairro'), ref['cidade'],
            _formatar_cep(cep)) if x)
        if _chave_geocode(canonico) != _chave_geocode(texto):
            g = _google_geocode(canonico, numero_entrega=numero,
                                canal=CANAL_DESPACHO)
            if g:
                return g[0], g[1], canonico
    logger.warning('Endereço sem ponto validado para despacho: %r', texto[:200])
    return None


def geocodificar(endereco_ou_cep, *, canal=CANAL_CHECKOUT):
    """(lat, lng, rotulo) pra um CEP ou endereço livre, ou None. Wrapper
    compatível para estimativas. Despacho usa geocodificar_entrega()."""
    geo, _impreciso, _fonte = _geocodificar_impl(endereco_ou_cep, canal=canal)
    return geo


def consultar_frete(endereco_ou_cep, *, canal=CANAL_CHECKOUT):
    """Estimativa de frete pra um CEP ou endereço.

    `canal` decide de qual cota do Google sai a chamada remota (cache é
    grátis): CANAL_PREVIEW pra cotação ANÔNIMA (/loja/api/frete — sub-teto,
    não consegue esgotar o Google do checkout), CANAL_CHECKOUT (padrão —
    fail-safe: quem não informa é tratado como checkout).

    Retorna:
      {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
       'distancia_km': 3.4, 'endereco': 'Rua X, Moema, São Paulo',
       'aviso': 'valor estimado — o definitivo é o do checkout'}
      {'ok': True, 'fora_area': True, ...}  -> além de RAIO_MAX_KM
      {'ok': False, 'erro': 'endereco_vazio'|'nao_encontrado'}
    """
    if not (endereco_ou_cep or '').strip():
        return {'ok': False, 'erro': 'endereco_vazio'}
    geo, impreciso, fonte = _geocodificar_impl(endereco_ou_cep, canal=canal)
    if not geo:
        return {'ok': False, 'erro': 'nao_encontrado'}

    lat, lng, rotulo = geo
    km = distancia_km(lat, lng)
    valor = valor_para_distancia(km)
    # `impreciso` = resolveu SÓ pelo CEP (centroide no Nominatim ou coordenada
    # da BrasilAPI — frete é chute grosseiro). `fonte` = de onde veio a
    # coordenada (google/gratis/cep_centroide/brasilapi) — pro sensor. O
    # caller (checkout) alerta o dono nos casos de risco (dono 09/07).
    if valor is None:
        return {'ok': True, 'fora_area': True, 'distancia_km': round(km, 1),
                'endereco': rotulo, 'impreciso': impreciso, 'fonte': fonte,
                'aviso': f'fora do raio de {int(RAIO_MAX_KM)} km — '
                         'confirmar com a equipe'}
    return {'ok': True, 'fora_area': False, 'valor': valor,
            'gratis': valor == 0.0, 'distancia_km': round(km, 1),
            'endereco': rotulo, 'impreciso': impreciso, 'fonte': fonte,
            'aviso': 'valor estimado — o definitivo é o do checkout do site'}
