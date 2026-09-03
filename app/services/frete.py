"""Faixas de frete por distância até a padaria (Brooklin).

Fonte: mapa "Fretes O pão" do My Maps, exportado em KML pelo dono em
10/06/2026 ("Faixas de frete O Pão sem sobreposição"). São anéis
concêntricos de 1 km a partir da loja do Brooklin (Rua Ribeiro do Vale,
455): grátis até 1 km, e a cada km adicional soma R$5, até o limite de
25 km (R$120). Além de 25 km = fora da área de entrega do site.

O valor daqui é ESTIMATIVA pro atendimento (bot/equipe) — o valor que
vale é o do checkout do site. Se o dono redesenhar o mapa, atualizar as
constantes abaixo (e o teste de faixas).

Geocodificação (sem chave de API):
  1. CEP -> BrasilAPI v2 (devolve lat/lng pra maioria dos CEPs urbanos);
  2. fallback/endereço livre -> Nominatim (OpenStreetMap).
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


def _geocodificar_texto(texto, ref=None, cep_ref=None, postcode_estrito=False):
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
            return None
        for h in r.json():
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
            return (float(h['lat']), float(h['lon']),
                    h.get('display_name', consulta))
        return None
    except (requests.RequestException, ValueError, KeyError):
        logger.warning('Nominatim falhou pra %r', texto)
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

def _google_frete_ativo():
    from flask import current_app
    return str(current_app.config.get('FRETE_GOOGLE', '1')).strip().lower() \
        not in ('0', 'false', 'no', '')


def _google_sob_teto():
    """Reserva 1 slot do teto DIÁRIO de chamadas REMOTAS ao Google (cost cap).
    Best-effort via AppConfig (sobrevive a deploy). Devolve True se cabe."""
    from flask import current_app

    from app.extensions import db
    from app.models import AppConfig
    from app.utils import hoje
    try:
        teto = int(current_app.config.get('FRETE_GOOGLE_MAX_DIA') or 500)
    except (TypeError, ValueError):
        teto = 500
    hoje_iso = hoje().isoformat()
    dia, _, n = (AppConfig.get('frete_google_dia') or '').partition('|')
    n = int(n) if n.isdigit() and dia == hoje_iso else 0
    if n >= teto:
        return False
    AppConfig.set('frete_google_dia', f'{hoje_iso}|{n + 1}')
    db.session.commit()
    return True


def _google_geocode(texto, numero_entrega=None):
    """Google (cacheado) pro frete. (lat, lng) ou None. Cache HIT não consome
    o teto (custo zero); só a chamada REMOTA conta. Nunca levanta — fora de app
    context (thread do bot) ou sem chave, retorna None e cai na cadeia grátis."""
    if not texto:
        return None
    try:
        if not _google_frete_ativo():
            return None
        from app.models import GeocodeCache
        from app.services import google_maps
        chave = google_maps._normalizar_chave(texto)
        if chave:
            cache = GeocodeCache.query.filter_by(chave=chave).first()
            if cache and cache.lat is not None:
                if cache.fonte == 'google_entrega' or (cache.fonte == 'google' and numero_entrega is None):
                    return cache.lat, cache.lng   # hit PRECISO: sem custo/teto
                if cache.fonte == 'google_aprox':
                    return None                   # hit aproximado: cai na grátis
        if not _google_sob_teto():
            logger.warning('frete: teto diário de geocode Google atingido')
            return None
        # geocode_preciso: só devolve quando o Google achou o ENDEREÇO (não o
        # centroide da cidade) — senão None e cai na cadeia grátis (com guards).
        if numero_entrega is not None:
            return google_maps.geocode_preciso(texto, numero_entrega=numero_entrega)
        return google_maps.geocode_preciso(texto)
    except Exception:  # noqa: BLE001 — geocode nunca pode quebrar o frete
        logger.exception('frete: geocode Google falhou pra %r', texto[:80])
        return None


def _geocodificar_impl(endereco_ou_cep):
    """Núcleo do geocode. Devolve `(geo, impreciso, fonte)`:
    - `geo` = (lat, lng, rotulo) ou None;
    - `impreciso` = True quando SÓ o CEP resolveu (centroide do distrito);
    - `fonte` in {'latlng','google','gratis','cep_centroide'} — pro sensor.

    Ordem: 0. "lat,lng" colado; 1. GOOGLE (preciso, se ativo); depois a cadeia
    grátis como fallback: 2. BrasilAPI; 3. texto; 4. simplificado; 5. rua+cidade
    (postcode estrito); 6. só o CEP (centroide — IMPRECISO)."""
    texto = (endereco_ou_cep or '').strip()
    if not texto:
        return None, False, None
    m = re.match(r'^(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)$', texto)
    if m:
        return (float(m.group(1)), float(m.group(2)), texto), False, 'latlng'
    # Google primeiro (preciso a nível de porta; conserta homônimo e Lalamove).
    g = _google_geocode(texto)
    if g:
        return (g[0], g[1], texto), False, 'google'
    geo = None
    ref = None
    cep = _extrair_cep(texto)
    if cep:
        cep_geo = _geocodificar_cep(cep)
        if cep_geo:
            ref = cep_geo[3]             # {'cidade','bairro','rua'} do Correios
            if cep_geo[0] is not None:
                return cep_geo[:3], False, 'gratis'   # BrasilAPI tinha coord
            # BrasilAPI conhece o CEP mas nao tem coordenada. ANTES da cadeia
            # grátis, re-tenta o GOOGLE com o logradouro OFICIAL dos Correios:
            # o passo 1 (texto cru) falha quando o cliente digitou o nome da
            # rua errado/incompleto, mas o nome oficial resolve (caso Mirelle
            # 17/07/2026: "Rua Cândido de Azevedo Marques" sem o "Joaquim" →
            # nao_encontrado; com o oficial → Google 1,9km, R$5). Mesmo teto/
            # cache/kill-switch do passo 1; falhou → cadeia grátis intocada.
            if (ref or {}).get('rua'):
                numero = _extrair_numero(texto)
                canonico = ', '.join(x for x in (
                    ref['rua'], numero, ref.get('bairro'), ref.get('cidade'),
                    _formatar_cep(cep)) if x)
                g2 = _google_geocode(canonico)
                if g2:
                    return (g2[0], g2[1], canonico), False, 'google'
            # Cadeia grátis: geocodifica o endereço resolvido (rua + bairro +
            # cidade), mais preciso que o texto cru. Valida por CIDADE (barra
            # Arujá) — o rótulo carrega o bairro, então o postcode frouxo do
            # OSM não derruba (caso ABC).
            geo = _geocodificar_texto(cep_geo[2], ref=ref, cep_ref=cep)
    if not geo or geo[0] is None:
        geo = _geocodificar_texto(texto, ref=ref, cep_ref=cep)
    if not geo or geo[0] is None:
        simples = simplificar_endereco(texto)
        if simples and simples.lower() != texto.lower():
            # O simplificado perde o BAIRRO E a cidade real (vira "São Paulo"):
            # aqui NÃO dá pra validar por cidade, então usa só o guard de
            # postcode — é o que barra a "Rua Nova York" do Grajaú vs Brooklin
            # (05/07/2026). Endereço de fora da capital que só resolve aqui é
            # limitação conhecida do último fallback.
            geo = _geocodificar_texto(simples, cep_ref=cep)
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
                                      postcode_estrito=True)
    if (not geo or geo[0] is None) and cep:
        # ÚLTIMO RECURSO: geocodifica só o CEP (centroide do distrito). Menos
        # preciso — pode super OU subestimar o frete e, na borda de um CEP
        # grande, inverter o "fora da área" — mas RESGATA a venda quando a
        # BrasilAPI não tem coordenada e nenhuma variante do endereço resolve.
        # Marca IMPRECISO pro caller alertar o dono (decisão do dono 09/07).
        geo = _geocodificar_texto(_formatar_cep(cep), cep_ref=cep)
        if geo and geo[0] is not None:
            return geo, True, 'cep_centroide'
    if not geo or geo[0] is None:
        logger.warning('geocodificacao falhou em todas as tentativas: %r',
                       texto[:200])
        return None, False, None
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
    g = _google_geocode(texto, numero_entrega=numero)
    if g:
        return g[0], g[1], texto
    cep = _extrair_cep(texto)
    postal = _geocodificar_cep(cep) if cep else None
    ref = postal[3] if postal else {}
    if ref.get('rua') and ref.get('cidade'):
        canonico = ', '.join(x for x in (
            ref['rua'], numero, ref.get('bairro'), ref['cidade'],
            _formatar_cep(cep)) if x)
        if canonico != texto:
            g = _google_geocode(canonico, numero_entrega=numero)
            if g:
                return g[0], g[1], canonico
    logger.warning('Endereço sem ponto validado para despacho: %r', texto[:200])
    return None


def geocodificar(endereco_ou_cep):
    """(lat, lng, rotulo) pra um CEP ou endereço livre, ou None. Wrapper
    compatível para estimativas. Despacho usa geocodificar_entrega()."""
    geo, _impreciso, _fonte = _geocodificar_impl(endereco_ou_cep)
    return geo


def consultar_frete(endereco_ou_cep):
    """Estimativa de frete pra um CEP ou endereço.

    Retorna:
      {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
       'distancia_km': 3.4, 'endereco': 'Rua X, Moema, São Paulo',
       'aviso': 'valor estimado — o definitivo é o do checkout'}
      {'ok': True, 'fora_area': True, ...}  -> além de RAIO_MAX_KM
      {'ok': False, 'erro': 'endereco_vazio'|'nao_encontrado'}
    """
    if not (endereco_ou_cep or '').strip():
        return {'ok': False, 'erro': 'endereco_vazio'}
    geo, impreciso, fonte = _geocodificar_impl(endereco_ou_cep)
    if not geo:
        return {'ok': False, 'erro': 'nao_encontrado'}

    lat, lng, rotulo = geo
    km = distancia_km(lat, lng)
    valor = valor_para_distancia(km)
    # `impreciso` = resolveu SÓ pelo centroide do CEP (frete é chute grosseiro).
    # `fonte` = de onde veio a coordenada (google/gratis/cep_centroide) — pro
    # sensor. O caller (checkout) alerta o dono nos casos de risco (dono 09/07).
    if valor is None:
        return {'ok': True, 'fora_area': True, 'distancia_km': round(km, 1),
                'endereco': rotulo, 'impreciso': impreciso, 'fonte': fonte,
                'aviso': f'fora do raio de {int(RAIO_MAX_KM)} km — '
                         'confirmar com a equipe'}
    return {'ok': True, 'fora_area': False, 'valor': valor,
            'gratis': valor == 0.0, 'distancia_km': round(km, 1),
            'endereco': rotulo, 'impreciso': impreciso, 'fonte': fonte,
            'aviso': 'valor estimado — o definitivo é o do checkout do site'}
