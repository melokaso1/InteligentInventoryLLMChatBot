# -*- coding: utf-8 -*-
import copy
import logging
import re
import unicodedata
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.schemas import ChatMessageResponse, OperationSummary, ProductOffer
from app.tools import dotnet_tools
from app.utils.json_normalize import pick
from app.utils.measure_units import (
    MEASURE_UNITS_PATTERN,
    QUANTITY_PATTERN,
    VOLUME_UNITS,
    WEIGHT_UNITS,
    extract_quantity_with_unit,
    format_stock,
    is_quantity_reply,
    normalize_unit,
    parse_quantity_text,
    resolve_sale_quantity,
    unit_label,
    unit_short,
)

logger = logging.getLogger(__name__)

TAX_RATE = 0.19
OFFERS_PAGE_SIZE = 12
CATALOG_LOAD_MORE_CHIP = "Ver más productos"
SESSIONS: dict[str, dict[str, Any]] = {}

INTENT_PHRASES: dict[str, tuple[str, ...]] = {
    "consultar_stock": (
        "consultar stock",
        "ver stock",
        "revisar stock",
        "chequear stock",
        "consultar disponibilidad",
    ),
    "buscar_producto": (
        "buscar producto",
        "buscar otro producto",
        "buscar un producto",
        "nueva consulta",
    ),
    "ver_ofertas": (
        "ver ofertas",
        "ver catálogo",
        "ver catalogo",
        "muéstrame el catálogo",
        "muestrame el catalogo",
        "mostrar catálogo",
        "mostrar catalogo",
        "ofertas",
        "ver promociones",
        "promociones",
    ),
    "ver_factura": (
        "ver factura",
        "ver facturas",
        "mis facturas",
        "ver mis facturas",
        "consultar factura",
        "consultar facturas",
    ),
    "cargar_mas_catalogo": (
        "ver más productos",
        "ver mas productos",
        "cargar más",
        "cargar mas",
        "ver todo el catálogo",
        "ver todo el catalogo",
    ),
    "cancelar": (
        "cancelar",
        "cancelar la solicitud",
        "cancelar pedido",
        "cancelar compra",
        "anular",
        "salir",
    ),
    "ayuda": (
        "ayuda",
        "help",
        "como me comunico",
        "cómo me comunico",
        "¿como me comunico?",
        "¿cómo me comunico?",
    ),
}

FLOW_PHASES = frozenset(
    {
        "awaiting_stock_sku",
        "awaiting_product_search",
        "awaiting_quantity",
        "awaiting_confirmation",
        "awaiting_use_saved_address",
        "awaiting_delivery_address",
        "awaiting_save_address",
        "sale_completed",
    }
)

_CANCEL_EXACT_PHRASES = frozenset(
    {
        "cancelar",
        "cancelar la solicitud",
        "cancelar pedido",
        "cancelar compra",
        "cancelo",
        "anular",
        "salir",
        "no",
        "no gracias",
        "nop",
    }
)

_GREETING_PHRASES = frozenset(
    {
        "hola",
        "holi",
        "holaa",
        "hey",
        "hi",
        "hello",
        "buenas",
        "saludos",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "que tal",
        "qué tal",
        "como estas",
        "cómo estás",
        "como esta",
        "cómo está",
        "good morning",
        "good afternoon",
        "good evening",
    }
)

_ALL_INTENT_PHRASES = {phrase for phrases in INTENT_PHRASES.values() for phrase in phrases}


class GraphState(TypedDict):
    session_id: str
    message: str
    phase: str
    product_code: str
    product_name: str
    unit_price: float
    stock: int
    quantity: float
    measure_unit: str
    customer_name: str
    customer_email: str
    response: str
    chips: list[str]
    invoice_number: str
    operation_summary: dict[str, Any]
    offers: list[dict[str, Any]]
    offers_total_count: int


_SESSION_FIELDS = (
    "phase",
    "product_code",
    "product_name",
    "unit_price",
    "stock",
    "quantity",
    "measure_unit",
    "pending_sale",
    "cart",
    "saved_cart",
    "pending_add_queue",
    "pending_order_queue",
    "adding_to_cart",
    "selected_product",
    "last_intent",
    "awaiting_quantity",
    "awaiting_stock_sku",
    "awaiting_product_search",
    "customer_name",
    "customer_email",
    "delivery_address",
    "delivery_city",
    "saved_delivery_address",
    "saved_delivery_city",
    "invoice_number",
    "operation_summary",
    "chat_history",
)


def _export_session_state(session: dict[str, Any]) -> dict[str, Any]:
    return {key: session.get(key) for key in _SESSION_FIELDS}


def _session(session_id: str) -> dict[str, Any]:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {
            "phase": "idle",
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0.0,
            "measure_unit": "unit",
            # Purchase-flow extras (cleared on cancel).
            "pending_sale": False,
            "cart": [],
            "saved_cart": None,
            "pending_add_queue": [],
            "pending_order_queue": [],
            "adding_to_cart": False,
            "selected_product": {},
            "last_intent": "",
            # Kept for backward-compatibility with earlier graph versions.
            "awaiting_quantity": False,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
            "customer_name": "Cliente El Plonsazo",
            "customer_email": "cliente@elplonsazo.com",
            "invoice_number": "",
            "operation_summary": {},
            "chat_history": [],
        }
    return SESSIONS[session_id]


def _apply_customer_context(
    session: dict[str, Any],
    customer_name: str | None = None,
    customer_email: str | None = None,
) -> None:
    if customer_name and customer_name.strip():
        session["customer_name"] = customer_name.strip()
    if customer_email and customer_email.strip():
        session["customer_email"] = customer_email.strip().lower()


def _hydrate_session(
    session_id: str,
    state: dict[str, Any] | None,
    customer_name: str | None = None,
    customer_email: str | None = None,
) -> dict[str, Any]:
    session = _session(session_id)
    if state:
        session.update(state)
        SESSIONS[session_id] = session
    _apply_customer_context(session, customer_name, customer_email)
    return session


def _normalize_message(message: str) -> str:
    return " ".join(message.lower().strip().split())


def _normalize_intent(message: str) -> str | None:
    """Map chip labels and menu phrases to intent keys (case-insensitive)."""
    text = _normalize_message(message).rstrip(".,!?").lstrip("¿").strip()
    for intent, phrases in INTENT_PHRASES.items():
        if text in phrases:
            return intent
    return None


def _is_menu_intent(message: str) -> bool:
    """True when the user tapped a chip or sent a pure menu phrase (not a SKU/query)."""
    return _normalize_intent(message) is not None and _extract_code(message) is None


def _is_intent_phrase(message: str) -> bool:
    return _normalize_message(message).rstrip(".,!?") in _ALL_INTENT_PHRASES


_STOCK_QUERY_MARKERS = (
    "consultar stock",
    "ver stock",
    "revisar stock",
    "chequear stock",
    "consultar disponibilidad",
    "cuanto stock",
    "cuánto stock",
    "cuantas unidades",
    "cuántas unidades",
)

_PRICE_INQUIRY_MARKERS = (
    "cuanto vale",
    "cuánto vale",
    "cuanto cuesta",
    "cuánto cuesta",
    "cuanto cuestan",
    "cuánto cuestan",
    "precio de",
    "precio del",
    "precio de la",
    "valor del gramo",
    "valor del kilo",
    "a cuanto",
    "a cuánto",
    "que precio",
    "qué precio",
    "a que precio",
    "a qué precio",
    "cuanto sale",
    "cuánto sale",
)

_PRICE_INQUIRY_STRIP_PHRASES = (
    "quiero saber",
    "me gustaria saber",
    "me gustaría saber",
    "quisiera saber",
    "necesito saber",
    "cuanto vale el gramo de",
    "cuánto vale el gramo de",
    "cuanto vale el kilo de",
    "cuánto vale el kilo de",
    "valor del gramo de",
    "valor del kilo de",
    "cuanto vale",
    "cuánto vale",
    "cuanto cuesta",
    "cuánto cuesta",
    "cuanto cuestan",
    "cuánto cuestan",
    "precio de la",
    "precio del",
    "precio de",
    "a cuanto esta",
    "a cuánto está",
    "a cuanto",
    "a cuánto",
    "que precio tiene",
    "qué precio tiene",
    "que precio",
    "qué precio",
    "el gramo de",
    "el kilo de",
    "la unidad de",
    "por gramo",
    "por kilo",
    "por unidad",
)


def _is_stock_query(message: str) -> bool:
    """True when the user asks for stock/disponibilidad, optionally with a product."""
    if _normalize_intent(message) == "consultar_stock":
        return True
    text = _normalize_message(message)
    return any(marker in text for marker in _STOCK_QUERY_MARKERS)


def _is_price_inquiry(message: str) -> bool:
    """True when the user asks for a product price (not a purchase)."""
    if _is_menu_intent(message) or _is_intent_phrase(message):
        return False
    text = _normalize_message(message)
    return any(marker in text for marker in _PRICE_INQUIRY_MARKERS)


_UNIT_CONTENT_INQUIRY_MARKERS = (
    "cuanto trae cada unidad",
    "que trae cada unidad",
    "cuantos gramos trae",
    "cuanto pesa cada unidad",
    "contenido por unidad",
    "peso por unidad",
    "que contiene cada unidad",
    "cuanto tiene cada unidad",
    "cuanto contiene cada unidad",
    "cuanto trae",
    "que trae",
    "cuanto pesa",
    "cuanto contiene",
    "cuanto tiene",
)


def _is_unit_content_inquiry(message: str) -> bool:
    """True when the user asks about per-unit content/weight during purchase flow."""
    if _is_menu_intent(message) or is_quantity_reply(message):
        return False
    if _extract_quantity(message) is not None:
        return False
    text = _strip_accents(_normalize_message(message).rstrip(".,!?").lstrip("¿").strip())
    if text.startswith("y "):
        text = text[2:].strip()
    return any(marker in text for marker in _UNIT_CONTENT_INQUIRY_MARKERS)


def _extract_product_from_inquiry(message: str) -> str | None:
    """Strip price-inquiry boilerplate and return product search terms."""
    if not _is_price_inquiry(message):
        return None
    text = _strip_bot_name(_strip_greeting_prefix(message))
    normalized = _normalize_message(text).rstrip(".,!?")
    for phrase in _PRICE_INQUIRY_STRIP_PHRASES:
        normalized = normalized.replace(phrase, " ")
    terms = _extract_search_terms_from_text(normalized)
    if terms:
        return terms
    return _extract_search_terms_from_text(text)


_PURCHASE_INTENT_MARKERS = (
    "quiero comprar",
    "deseo comprar",
    "necesito comprar",
    "quisiera comprar",
    "me gustaria comprar",
    "me gustaría comprar",
)

_VAGUE_PURCHASE_TERMS = frozenset(
    {
        "algo",
        "alguna",
        "alguna cosa",
        "una cosa",
        "un producto",
        "una producto",
        "producto",
        "productos",
        "algun producto",
        "algún producto",
        "cualquier cosa",
        "cualquier producto",
    }
)

_GREETING_PREFIXES = (
    "buenos dias ",
    "buenas tardes ",
    "buenas noches ",
    "good morning ",
    "good afternoon ",
    "good evening ",
    "hola ",
    "hey ",
    "hi ",
)

_PRODUCT_ALIASES: dict[str, str] = {
    # Cocaine / perico slang
    "perico": "cocaina",
    "perica": "cocaina",
    "cocaina": "cocaina",
    "coca": "coca",
    "perla": "cocaina",
    "drogaina": "cocaina",
    "cocaina perlada": "cocaina perlada",
    "perlada": "cocaina perlada",
    "cocaina hcl": "cocaina hcl",
    "pasta base": "pasta base",
    "pastabase": "pasta base",
    # Marihuana / cannabis slang
    "la blanca": "marihuana",
    "blanca": "marihuana",
    "la balnca": "marihuana",
    "balnca": "marihuana",
    "marihuana": "marihuana",
    "marijuana": "marihuana",
    "cannabis": "cannabis",
    "mota": "marihuana",
    "hierba": "marihuana",
    "weed": "marihuana",
    "delta-8": "delta-8",
    "delta 8": "delta-8",
    # Bazuco / paco
    "paco": "bazuco",
    "bazuco": "bazuco",
    # Tussi
    "tusi": "tussi",
    "tussi": "tussi",
    # Hongos
    "hongos": "hongos",
    "champis": "hongos",
    "champi": "hongos",
    "penis envy": "hongos penis envy",
    "golden teacher": "hongos golden teacher",
    # Poppers / nitritos
    "poppers": "poppers",
    "rush": "poppers",
    "nitritos": "poppers",
    # LSD / alucinógenos
    "lsd": "lsd",
    "acido": "lsd",
    "blotter": "lsd blotter",
    "gel tabs": "lsd gel tabs",
    "microdosis": "lsd microdosis",
    "dmt": "dmt",
    "changa": "dmt changa",
    "freebase": "dmt freebase",
    "ayahuasca": "ayahuasca",
    "mescalina": "mescalina",
    "peyote": "mescalina peyote",
    "san pedro": "mescalina san pedro",
    "ibogaina": "ibogaina",
    "nbome": "nbomes",
    "nbomes": "nbomes",
    # MDMA / éxtasis
    "mdma": "mdma",
    "molly": "mdma",
    "extasis": "extasis",
    "ecstasy": "extasis",
    "tesla": "extasis tesla",
    "punisher": "extasis punisher",
    "pink porsche": "extasis pink porsche",
    # Estimulantes
    "meth": "metanfetamina",
    "cristal": "metanfetamina",
    "ice": "metanfetamina",
    "metanfetamina": "metanfetamina",
    "yaba": "metanfetamina yaba",
    "pink ice": "metanfetamina pink ice",
    "anfetamina": "anfetamina",
    "speed": "anfetamina",
    "khat": "khat",
    "flakka": "flakka",
    "catinonas": "catinonas",
    # Ketamina / disociativos
    "ketamina": "ketamina",
    "ketamina liquida": "ketamina liquida",
    "ketamina líquida": "ketamina liquida",
    "ketamina polvo": "ketamina polvo",
    "ketamina nasal": "ketamina nasal",
    "esketamina": "ketamina nasal",
    "keta": "ketamina",
    "pcp": "pcp",
    # Opioides
    "heroina": "heroina",
    "heroin": "heroina",
    "black tar": "heroina black tar",
    "heroina black tar": "heroina black tar",
    "fentanilo": "fentanilo",
    "fenta": "fentanilo",
    "morfina": "morfina",
    "codeina": "codeina",
    "metadona": "metadona",
    "opioides": "opioides",
    "opioide": "opioides",
    "hidrocodona": "hidrocodona",
    "hidromorfona": "hidromorfona",
    "meperidina": "meperidina",
    "oxycodona": "oxycodona",
    "oxycontin": "oxycodona",
    "tramadol": "tramadol",
    # Depresores / sedantes
    "ghb": "ghb",
    "barbituricos": "barbiturico",
    "barbiturico": "barbiturico",
    "benzodiacepinas": "benzodiacepinas",
    "benzos": "benzodiacepinas",
    "benzo": "benzodiacepinas",
    "flunitrazepam": "flunitrazepam",
    "rohypnol": "flunitrazepam",
    "diazepam": "diazepam",
    "valium": "diazepam",
    "alprazolam": "alprazolam",
    "xanax": "alprazolam",
    "lorazepam": "lorazepam",
    "ativan": "lorazepam",
    "clonazepam": "clonazepam",
    # Cannabinoides sintéticos
    "k2": "k2",
    "spice": "k2",
    # Nicotina / vape
    "nicotina": "nicotina",
    "vape": "vape",
    "vapeo": "vape",
    # Esteroides
    "juice": "esteroides",
    "esteroides": "esteroides",
    "anabolicos": "esteroides",
    # Antitusivos / OTC
    "dxm": "dxm",
    "dextrometorfano": "dxm",
    "loperamida": "loperamida",
    "imodium": "loperamida",
    # Otros
    "crack": "crack",
    "piedra": "crack",
    "metilfenidato": "metilfenidato",
    "ritalin": "metilfenidato",
    "gorilla glue": "marihuana gorilla glue",
    "og kush": "marihuana og kush",
    # Cannabis strains
    "blue dream": "marihuana blue dream",
    "marihuana blue": "marihuana blue dream",
    "northern lights": "cannabis northern lights",
    "live rosin": "cannabis live rosin",
}

_COKE_SLANG_TERMS = frozenset(
    {"cocaina", "coca", "perla", "perico", "perica", "drogaina"}
)

_PHRASE_ALIASES_SORTED = tuple(
    sorted(_PRODUCT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True)
)

_PURCHASE_VERB = r"(?:yo\s+)?(?:dame|quiero|necesito)"

_PURCHASE_QUANTITY_PATTERNS = (
    re.compile(
        rf"\b(?:quiero|deseo|necesito|quisiera)\s+comprar\s+(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bcomprar\s+(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^\s*{_PURCHASE_VERB}\s+(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_PURCHASE_VERB}\s+(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_PURCHASE_VERB}\s+(?P<product>.+?)\s+(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})\s+(?P<product>.+)$",
        re.IGNORECASE,
    ),
)

_PURCHASE_PRODUCT_PATTERNS = (
    re.compile(r"\bcomprar\s+(?:la\s+)?(.+)", re.IGNORECASE),
    re.compile(rf"\b{_PURCHASE_VERB}\s+(?:la\s+)?(.+)", re.IGNORECASE),
)

_UNIT_SUFFIX_RE = re.compile(
    rf"\s*{MEASURE_UNITS_PATTERN}\s*$",
    re.IGNORECASE,
)


def _strip_greeting_prefix(message: str) -> str:
    text = _strip_accents(_normalize_message(message).rstrip(".,!?"))
    text = text.lstrip("¿¡").rstrip("?!").strip()
    if text in _GREETING_PHRASES:
        return ""
    for prefix in _GREETING_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text


def _strip_bot_name(text: str) -> str:
    return re.sub(r"^drogui\s+", "", text, flags=re.IGNORECASE).strip()


def _resolve_product_alias(terms: str) -> str:
    """Map Colombian/Latin slang to catalog search terms."""
    normalized = _strip_accents(_normalize_message(terms).rstrip(".,!?").strip())
    if not normalized:
        return terms
    # Compound multi-item strings («cocaina y 5 unidades de … ketamina liquida») must
    # not collapse to the trailing alias or search resolves only the last product.
    if re.search(r"\s+y\s+\d", normalized):
        return terms
    if normalized in _PRODUCT_ALIASES:
        return _PRODUCT_ALIASES[normalized]
    for alias, canonical in _PHRASE_ALIASES_SORTED:
        if normalized == alias or normalized.endswith(f" {alias}"):
            return canonical
    words = normalized.split()
    if len(words) == 1:
        return _PRODUCT_ALIASES.get(words[0], terms)
    resolved = [_PRODUCT_ALIASES.get(word, word) for word in words]
    if resolved != words:
        return " ".join(resolved)
    return terms


def _clean_product_query(query: str) -> str:
    cleaned = _normalize_message(query).rstrip(".,!?").strip()
    cleaned = _UNIT_SUFFIX_RE.sub("", cleaned)
    if cleaned.startswith("la "):
        cleaned = cleaned[3:].strip()
    return _resolve_product_alias(cleaned)


def _is_vague_product_term(product: str) -> bool:
    normalized = _strip_accents(_normalize_message(product).rstrip(".,!?"))
    return normalized in _VAGUE_PURCHASE_TERMS


def _is_vague_purchase_intent(message: str) -> bool:
    """Purchase request without a concrete product (e.g. «quiero comprar algo»)."""
    if _is_price_inquiry(message):
        return False
    text = _normalize_message(message)
    has_marker = any(marker in text for marker in _PURCHASE_INTENT_MARKERS)
    has_comprar = bool(re.search(r"\bcomprar\b", text) and "confirmar" not in text)
    if not has_marker and not has_comprar:
        return False
    _, _, product = _extract_purchase_details(message)
    return product is None


def _extract_purchase_details(message: str) -> tuple[float | None, str | None, str | None]:
    if _is_price_inquiry(message):
        return None, None, None
    if _is_multi_item_order(message):
        return None, None, None
    text = _strip_bot_name(_strip_greeting_prefix(message))
    if not text:
        return None, None, None
    text = _normalize_message(text).rstrip(".,!?")

    for pattern in _PURCHASE_QUANTITY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        quantity = parse_quantity_text(match.group("qty"))
        if quantity is None:
            continue
        unit_raw = match.groupdict().get("unit")
        unit = normalize_unit(unit_raw) if unit_raw else None
        product_raw = match.groupdict().get("product")
        if product_raw:
            product = _clean_product_query(product_raw)
            if product and quantity > 0 and not _is_vague_product_term(product):
                return quantity, unit, product
        elif quantity > 0:
            return quantity, unit, None

    for pattern in _PURCHASE_PRODUCT_PATTERNS:
        match = pattern.search(text)
        if match:
            product_raw = match.group(1)
            leading = _LEADING_QTY_PRODUCT_RE.match(product_raw.strip())
            if leading:
                quantity = parse_quantity_text(leading.group("qty"))
                unit_raw = leading.groupdict().get("unit")
                unit = normalize_unit(unit_raw) if unit_raw else None
                product = _clean_product_query(leading.group("product"))
                if product and quantity and quantity > 0 and not _is_vague_product_term(product):
                    return quantity, unit, product
            product = _clean_product_query(product_raw)
            if product and not re.fullmatch(r"\d+(?:[.,]\d+)?", product):
                if _is_vague_product_term(product):
                    return None, None, None
                trailing_qty, trailing_unit = _extract_quantity_with_unit(product)
                if trailing_qty is not None and str(int(trailing_qty)) in product:
                    product = re.sub(rf"\b{int(trailing_qty)}\b", "", product).strip()
                    product = _clean_product_query(product)
                    if product and not _is_vague_product_term(product):
                        return trailing_qty, trailing_unit, product
                return None, None, product
    return None, None, None


_LEADING_QTY_PRODUCT_RE = re.compile(
    rf"^(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)$",
    re.IGNORECASE,
)

_MULTI_ITEM_SEGMENT_RE = re.compile(
    rf"^(?P<qty>{QUANTITY_PATTERN})\s*(?P<unit>{MEASURE_UNITS_PATTERN})?\s+de\s+(?:la\s+)?(?P<product>.+)$",
    re.IGNORECASE,
)


def _split_multi_item_segments(text: str) -> list[str]:
    stripped = re.sub(
        rf"^(?:{_PURCHASE_VERB}|(?:quiero|deseo|necesito|quisiera)\s+comprar)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    segments: list[str] = []
    for part in re.split(r"\s+y\s+(?=\d)", stripped):
        for piece in re.split(r"\s*,\s*", part):
            piece = piece.strip()
            if piece:
                segments.append(piece)
    return segments


def _parse_multi_item_order(text: str) -> list[dict[str, Any]]:
    """Parse «3 unidades de X y 5 gramos de Y» into line items with qty, unit, product_query."""
    if _is_price_inquiry(text):
        return []
    normalized = _strip_bot_name(_strip_greeting_prefix(text))
    if not normalized:
        return []
    normalized = _normalize_message(normalized).rstrip(".,!?")
    items: list[dict[str, Any]] = []
    for segment in _split_multi_item_segments(normalized):
        match = _MULTI_ITEM_SEGMENT_RE.match(segment.strip())
        if not match:
            continue
        quantity = parse_quantity_text(match.group("qty"))
        if quantity is None or quantity <= 0:
            continue
        unit_raw = match.groupdict().get("unit")
        unit = normalize_unit(unit_raw) if unit_raw else None
        product = _clean_product_query(match.group("product"))
        if not product or _is_vague_product_term(product):
            continue
        items.append(
            {
                "quantity": quantity,
                "unit": unit,
                "product_query": product,
            }
        )
    return items


def _is_multi_item_order(message: str) -> bool:
    return len(_parse_multi_item_order(message)) >= 2


def _has_commerce_intent(message: str) -> bool:
    if _extract_code(message):
        return True
    if _is_price_inquiry(message) and not _is_menu_intent(message):
        return True
    if _is_stock_query(message) and not _is_menu_intent(message):
        return True
    if _normalize_intent(message) in ("consultar_stock", "buscar_producto", "ver_ofertas"):
        return True
    _, _, product_query = _extract_purchase_details(message)
    if product_query:
        return True
    stripped = _strip_bot_name(_strip_greeting_prefix(message))
    if stripped and _extract_search_terms_from_text(stripped):
        return True
    return False


def _is_purchase_intent(message: str) -> bool:
    """New purchase request — must not be treated as confirmation of a pending sale."""
    if _is_price_inquiry(message):
        return False
    if _is_confirm(message):
        return False
    if is_quantity_reply(message):
        return False
    if _is_multi_item_order(message):
        return True
    _, _, product_query = _extract_purchase_details(message)
    if product_query:
        return True
    text = _normalize_message(message)
    if any(marker in text for marker in _PURCHASE_INTENT_MARKERS):
        return True
    if re.search(r"\bcomprar\b", text) and "confirmar" not in text:
        return True
    return False


_CONFIRM_PHRASES = frozenset(
    {
        "confirmar compra",
        "confirmo compra",
        "confirmo la compra",
        "confirmar pedido",
        "confirmo",
        "confirmar",
        "sí confirmo",
        "si confirmo",
        "sí, confirmo",
        "si, confirmo",
        "sí confirmo la compra",
        "si confirmo la compra",
        "sí, confirmo la compra",
        "si, confirmo la compra",
    }
)


def _is_greeting(message: str) -> bool:
    """True for pure salutations — compound greeting + commerce intent is not a greeting."""
    text = _strip_accents(_normalize_message(message).rstrip(".,!?"))
    text = text.lstrip("¿¡").rstrip("?!").strip()
    if text in _GREETING_PHRASES:
        return True
    if any(text.startswith(prefix) for prefix in _GREETING_PREFIXES):
        return not _has_commerce_intent(message)
    return False


def _extract_code(message: str) -> str | None:
    upper = message.upper()
    match = re.search(r"PLZ-[A-Z0-9-]+", upper)
    if match:
        return match.group(0)
    match = re.search(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b", upper)
    return match.group(0) if match else None


def _looks_like_sku(term: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)+", term.strip().upper()))


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


_STOCK_QUERY_PHRASES = (
    "quiero saber el stock de",
    "quiero saber el stock",
    "quiero consultar el stock de",
    "quiero consultar el stock",
    "cuanto stock hay de",
    "cuánto stock hay de",
    "cuanta cantidad hay de",
    "cuánta cantidad hay de",
    "cuantas unidades hay de",
    "cuántas unidades hay de",
    "stock de",
    "disponibilidad de",
    "hay de",
    "tienes de",
    "tienen de",
    "tienes",
    "tienen",
)

_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "algo",
        "algun",
        "alguna",
        "alguno",
        "al",
        "busco",
        "como",
        "con",
        "cual",
        "cuál",
        "cuanto",
        "cuánto",
        "cuanta",
        "cuánta",
        "dame",
        "de",
        "del",
        "disponible",
        "disponibles",
        "drogui",
        "el",
        "en",
        "es",
        "esta",
        "está",
        "estan",
        "están",
        "favor",
        "gramos",
        "gramo",
        "hay",
        "kilos",
        "kilo",
        "kilogramos",
        "kilogramo",
        "la",
        "las",
        "lo",
        "los",
        "me",
        "necesito",
        "o",
        "para",
        "por",
        "producto",
        "productos",
        "que",
        "comprar",
        "consultar",
        "quiero",
        "saber",
        "se",
        "stock",
        "te",
        "tengo",
        "un",
        "una",
        "unidades",
        "unidad",
        "vale",
        "valen",
        "cuesta",
        "cuestan",
        "precio",
        "valor",
        "sale",
        "ver",
        "y",
        "yo",
    }
)


_CONVERSATIONAL_PREFIXES = (
    "quiero ver ",
    "quiero comprar ",
    "quiero ",
    "dame ",
    "necesito ",
    "busco ",
    "tienes ",
    "tienen ",
    "hay ",
)

# Category keyword → catalog search terms (accent-normalized keys).
_CATEGORY_SEARCH_QUERIES: dict[str, tuple[str, ...]] = {
    "liquido": ("líquido", "aceite", "liquido"),
    "liquid": ("líquido", "aceite", "liquido"),
    "aceite": ("aceite", "líquido"),
    "oil": ("aceite", "líquido"),
    "vape": ("vape", "cartucho", "nicotina"),
    "cartucho": ("cartucho", "vape", "k2"),
    "poppers": ("poppers", "nitrito"),
    "rush": ("poppers", "rush"),
    "nitritos": ("poppers", "nitrito"),
    "pastilla": ("extasis", "mdma", "pastilla"),
    "pastillas": ("extasis", "mdma", "pastilla"),
    "flor": ("marihuana", "cannabis"),
    "marihuana": ("marihuana",),
    "mota": ("marihuana",),
    "hierba": ("marihuana",),
    "weed": ("marihuana",),
    "marijuana": ("marihuana", "marijuana"),
    "cannabis": ("cannabis", "marihuana"),
    "delta-8": ("delta-8", "delta"),
    "perlada": ("perlada", "cocaina"),
    "hcl": ("cocaina", "hcl"),
    "pasta": ("pasta base",),
    "base": ("pasta base", "crack"),
    "cocaina": ("cocaina",),
    "coca": ("coca", "cocaina"),
    "perico": ("cocaina",),
    "perica": ("cocaina",),
    "drogaina": ("cocaina",),
    "perla": ("cocaina",),
    "crack": ("crack",),
    "piedra": ("crack",),
    "paco": ("bazuco",),
    "bazuco": ("bazuco",),
    "tusi": ("tussi",),
    "tussi": ("tussi",),
    "hongos": ("hongos",),
    "champis": ("hongos",),
    "champi": ("hongos",),
    "lsd": ("lsd",),
    "acido": ("lsd",),
    "dmt": ("dmt",),
    "ayahuasca": ("ayahuasca",),
    "mescalina": ("mescalina",),
    "ibogaina": ("ibogaina",),
    "nbome": ("nbomes",),
    "nbomes": ("nbomes",),
    "mdma": ("mdma", "extasis"),
    "molly": ("mdma", "extasis"),
    "extasis": ("extasis", "mdma"),
    "ecstasy": ("extasis", "mdma"),
    "meth": ("metanfetamina", "cristalina"),
    "metanfetamina": ("metanfetamina", "cristalina"),
    "anfetamina": ("anfetamina", "speed"),
    "speed": ("anfetamina",),
    "khat": ("khat",),
    "flakka": ("flakka",),
    "catinonas": ("catinonas",),
    "polvo": ("cocaina", "metanfetamina", "ketamina"),
    "ketamina": ("ketamina",),
    "esketamina": ("ketamina", "esketamina"),
    "nasal": ("ketamina", "esketamina"),
    "keta": ("ketamina",),
    "pcp": ("pcp",),
    "heroina": ("heroina",),
    "heroin": ("heroina",),
    "black": ("black tar", "heroina"),
    "tar": ("black tar", "heroina"),
    "tesla": ("extasis", "tesla"),
    "punisher": ("extasis", "punisher"),
    "porsche": ("extasis", "porsche"),
    "yaba": ("metanfetamina", "yaba"),
    "diazepam": ("diazepam", "valium"),
    "valium": ("diazepam", "valium"),
    "alprazolam": ("alprazolam", "xanax"),
    "xanax": ("alprazolam", "xanax"),
    "lorazepam": ("lorazepam", "ativan"),
    "ativan": ("lorazepam", "ativan"),
    "clonazepam": ("clonazepam",),
    "oxycodona": ("oxycodona", "oxycontin"),
    "oxycontin": ("oxycodona", "oxycontin"),
    "tramadol": ("tramadol",),
    "changa": ("dmt", "changa"),
    "freebase": ("dmt", "freebase"),
    "peyote": ("mescalina", "peyote"),
    "penis": ("hongos", "penis envy"),
    "envy": ("hongos", "penis envy"),
    "golden": ("hongos", "golden teacher"),
    "teacher": ("hongos", "golden teacher"),
    "gorilla": ("marihuana", "gorilla glue"),
    "glue": ("marihuana", "gorilla glue"),
    "kush": ("marihuana", "og kush"),
    "northern": ("cannabis", "northern lights"),
    "lights": ("cannabis", "northern lights"),
    "rosin": ("cannabis", "rosin"),
    "blotter": ("lsd", "blotter"),
    "microdosis": ("lsd", "microdosis"),
    "gel": ("lsd", "gel tabs"),
    "fentanilo": ("fentanilo",),
    "fenta": ("fentanilo",),
    "morfina": ("morfina",),
    "codeina": ("codeina",),
    "metadona": ("metadona",),
    "opioides": ("opioides", "morfina", "fentanilo"),
    "opioide": ("opioides",),
    "hidrocodona": ("hidrocodona",),
    "hidromorfona": ("hidromorfona",),
    "meperidina": ("meperidina",),
    "ghb": ("ghb",),
    "barbiturico": ("barbiturico",),
    "barbituricos": ("barbiturico",),
    "benzodiacepinas": ("benzodiacepinas", "clonazepam", "flunitrazepam", "diazepam", "alprazolam"),
    "benzos": ("benzodiacepinas", "clonazepam", "diazepam", "alprazolam"),
    "benzo": ("benzodiacepinas",),
    "flunitrazepam": ("flunitrazepam", "rohypnol"),
    "rohypnol": ("flunitrazepam",),
    "k2": ("k2", "spice"),
    "spice": ("k2",),
    "nicotina": ("nicotina", "vape"),
    "juice": ("juice", "esteroides", "test-e"),
    "esteroides": ("esteroides", "juice", "deca"),
    "anabolicos": ("esteroides",),
    "dxm": ("dxm", "dextrometorfano"),
    "dextrometorfano": ("dxm", "dextrometorfano"),
    "loperamida": ("loperamida", "imodium"),
    "imodium": ("loperamida",),
    "metilfenidato": ("metilfenidato", "ritalin"),
    "ritalin": ("metilfenidato",),
    "aerosol": ("aerosol",),
    "aerosoles": ("aerosol",),
    "inhalantes": ("inhalantes", "aerosol", "poppers"),
    "disolvente": ("disolvente", "tolueno"),
    "gases": ("gas", "n2o"),
    "gas": ("gas", "n2o"),
}

_LIQUID_CATEGORY_KEYS = frozenset(
    {"liquido", "liquid", "aceite", "oil", "vape", "cartucho", "poppers"}
)

_CATEGORY_DISPLAY_LABELS: dict[str, str] = {
    "liquid": "productos líquidos",
    "marijuana": "marihuana",
    "pill": "pastillas",
    "powder": "polvo",
}


def _strip_conversational_prefixes(text: str) -> str:
    normalized = _normalize_message(text).rstrip(".,!?")
    changed = True
    while changed:
        changed = False
        for prefix in _CONVERSATIONAL_PREFIXES:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                changed = True
    return normalized


def _category_key_for_word(word: str) -> str | None:
    accent_word = _strip_accents(word)
    if accent_word in _LIQUID_CATEGORY_KEYS:
        return "liquid"
    if accent_word in {"pastilla", "pastillas"}:
        return "pill"
    if accent_word in {"flor", "marihuana", "hierba", "marijuana"}:
        return "marijuana"
    if accent_word == "polvo":
        return "powder"
    return None


def _resolve_search_queries_from_text(text: str) -> tuple[list[str], str | None]:
    """Return ordered search queries and optional category key for display/fallbacks."""
    normalized = _strip_conversational_prefixes(text)
    for phrase in _STOCK_QUERY_PHRASES:
        normalized = normalized.replace(phrase, " ")

    words = [word for word in normalized.split() if word and word not in _SEARCH_STOPWORDS]
    words = [re.sub(r"^\d+.*$", "", word) for word in words]
    words = [word for word in words if word]
    if not words:
        return [], None

    # Resolve slang aliases before category / free-text search.
    resolved_words: list[str] = []
    for word in words:
        accent_word = _strip_accents(word)
        alias = _PRODUCT_ALIASES.get(accent_word)
        resolved_words.append(alias if alias else word)

    category_queries: list[str] = []
    category: str | None = None
    for word in resolved_words:
        accent_word = _strip_accents(word)
        mapped = _CATEGORY_SEARCH_QUERIES.get(accent_word)
        if mapped:
            category_queries.extend(mapped)
            category = category or _category_key_for_word(word)

    if category_queries:
        return list(dict.fromkeys(category_queries)), category

    meaningful = [word for word in resolved_words if len(word) >= 3]
    terms = " ".join(meaningful or resolved_words)
    if not terms:
        return [], None
    resolved = _resolve_product_alias(terms)
    return [resolved], None


def _extract_search_terms_from_text(text: str) -> str | None:
    queries, _ = _resolve_search_queries_from_text(text)
    return queries[0] if queries else None


def _search_display_label(message: str) -> str:
    stripped = _strip_bot_name(_strip_greeting_prefix(message))
    queries, category = _resolve_search_queries_from_text(stripped or message)
    if category and category in _CATEGORY_DISPLAY_LABELS:
        return _CATEGORY_DISPLAY_LABELS[category]
    if len(queries) > 1:
        return " / ".join(queries)
    if queries:
        return queries[0]
    return message


def _extract_search_queries(message: str) -> list[str]:
    """Ordered catalog queries to try for a natural-language message."""
    if _is_greeting(message):
        return []

    code = _extract_code(message)
    if code:
        return [code]

    _, _, product_query = _extract_purchase_details(message)
    if product_query:
        queries, _ = _resolve_search_queries_from_text(product_query)
        return queries or [product_query]

    stripped = _strip_bot_name(_strip_greeting_prefix(message))
    if stripped:
        queries, _ = _resolve_search_queries_from_text(stripped)
        if queries:
            return queries

    queries, _ = _resolve_search_queries_from_text(message)
    return queries


def _extract_search_terms(message: str) -> str | None:
    """Pull product keywords from a natural-language message (stock or search)."""
    queries = _extract_search_queries(message)
    return queries[0] if queries else None


def _product_fields(product: dict[str, Any]) -> dict[str, Any]:
    sale_unit = normalize_unit(str(pick(product, "saleUnit", "SaleUnit", default="unit")))
    stock = float(pick(product, "stock", "Stock", default=0))
    unit_content_label = pick(
        product, "unitContentLabel", "UnitContentLabel", default=""
    )
    return {
        "code": str(pick(product, "code", "Code", default="")),
        "name": str(pick(product, "name", "Name", default="")),
        "price": float(pick(product, "price", "Price", default=0)),
        "stock": stock,
        "saleUnit": sale_unit,
        "allowsFractional": bool(pick(product, "allowsFractional", "AllowsFractional", default=False)),
        "unitContentLabel": str(unit_content_label or ""),
    }


def _is_weight_measure(measure_unit: str | None) -> bool:
    if not measure_unit:
        return False
    return normalize_unit(measure_unit) in WEIGHT_UNITS


_QUERY_DISCRIMINATOR_WORDS = frozenset(
    {
        "blue",
        "dream",
        "liquida",
        "liquid",
        "perlada",
        "polvo",
        "crack",
        "sativa",
        "indica",
        "hibrida",
        "hibrido",
        "hybrid",
        "microdosis",
        "blotter",
        "premium",
        "indoor",
        "nasal",
        "esketamina",
        "black",
        "tar",
        "tesla",
        "punisher",
        "porsche",
        "yaba",
        "diazepam",
        "alprazolam",
        "lorazepam",
        "clonazepam",
        "valium",
        "xanax",
        "ativan",
        "oxycodona",
        "tramadol",
        "changa",
        "freebase",
        "peyote",
        "penis",
        "envy",
        "golden",
        "teacher",
        "gorilla",
        "glue",
        "kush",
        "northern",
        "lights",
        "rosin",
        "hcl",
        "pasta",
        "base",
        "gel",
    }
)


def _query_match_words(query: str) -> list[str]:
    normalized = _strip_accents(_normalize_message(query))
    return [word for word in normalized.split() if len(word) >= 3]


def _is_specific_product_query(query: str | None) -> bool:
    if not query:
        return False
    words = _query_match_words(query)
    if len(words) >= 2:
        return True
    return bool(words and words[0] in _QUERY_DISCRIMINATOR_WORDS)


def _product_match_score(fields: dict[str, Any], query: str | None) -> int:
    if not query:
        return 0
    normalized_query = _strip_accents(_normalize_message(query))
    name = _strip_accents(fields["name"].lower())
    code = fields["code"].upper()
    score = 0
    if normalized_query in name:
        score += 10
    query_words = _query_match_words(normalized_query)
    matched_words = [word for word in query_words if word in name]
    score += 8 * len(matched_words)
    if query_words and len(matched_words) == len(query_words):
        score += 15
    if "blue" in query_words and "blue" in name:
        score += 20
    if any(word in query_words for word in ("liquida", "liquid")) and (
        "liquida" in name or "liquid" in name
    ):
        score += 15
    if "ketamina" in normalized_query and code.startswith("PLZ-KET"):
        score += 20
        if any(word in query_words for word in ("liquida", "liquid")) and (
            "liquida" in name or "liquid" in name
        ):
            score += 25
        elif "polvo" in query_words and "polvo" in name:
            score += 25
        elif any(word in query_words for word in ("nasal", "esketamina")) and (
            "nasal" in name or "esketamina" in name
        ):
            score += 25
    if "cocaina perlada" in normalized_query and "perlada" in name:
        score += 30
    elif "cocaina hcl" in normalized_query and "hcl" in name:
        score += 30
    elif "pasta base" in normalized_query and "pasta base" in name:
        score += 30
    if normalized_query in _COKE_SLANG_TERMS and code.startswith("PLZ-COC"):
        score += 25
        if fields["saleUnit"] in WEIGHT_UNITS:
            score += 10
        if "perlada" in name or ("polvo" in name and "hcl" not in name):
            score += 15
        if "crack" in name or "base" in name.lower():
            score -= 30
        if "pasta base" in name:
            score -= 25
        if "hoja" in name:
            score -= 20
    elif "crack" in normalized_query and "crack" in name:
        score += 20
    if "heroina black tar" in normalized_query and "black tar" in name:
        score += 30
    elif "heroina" in normalized_query and code.startswith("PLZ-HER"):
        score += 15
        if len(query_words) <= 1 and name == "heroina":
            score += 10
    if normalized_query.replace(" ", "") in name.replace(" ", ""):
        score += 3
    return score


def _pick_product_for_purchase(
    products: list[dict[str, Any]],
    *,
    quantity: float | None,
    measure_unit: str | None,
    product_query: str | None,
    code: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick a product for purchase or return candidates when the user must choose a SKU."""
    if not products:
        return None, []

    if code:
        for product in products:
            if _product_fields(product)["code"] == code:
                return product, []
        return products[0], []

    resolved_query = _resolve_product_alias(product_query) if product_query else None
    search_query = resolved_query or product_query
    specific_query = _is_specific_product_query(search_query)

    def score_product(product: dict[str, Any]) -> int:
        return _product_match_score(_product_fields(product), search_query)

    def pick_clear_winner(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        scored = sorted(candidates, key=score_product, reverse=True)
        top_score = score_product(scored[0])
        second_score = score_product(scored[1]) if len(scored) > 1 else -1
        if top_score <= 0:
            return None
        if top_score > second_score:
            return scored[0]
        if specific_query:
            query_words = _query_match_words(search_query or "")
            top_name = _strip_accents(_product_fields(scored[0])["name"].lower())
            if query_words and all(word in top_name for word in query_words):
                return scored[0]
        return None

    winner = pick_clear_winner(products)
    if winner is not None:
        return winner, []

    filtered = list(products)
    if _is_weight_measure(measure_unit) and not specific_query:
        weight_products = [
            product
            for product in filtered
            if _product_fields(product)["saleUnit"] in WEIGHT_UNITS
        ]
        if weight_products:
            filtered = weight_products

    if measure_unit == "unit" and not specific_query:
        unit_products = [
            product
            for product in filtered
            if _product_fields(product)["saleUnit"] == "unit"
        ]
        if unit_products:
            filtered = unit_products

    if measure_unit in VOLUME_UNITS and not specific_query:
        volume_products = [
            product
            for product in filtered
            if _product_fields(product)["saleUnit"] in VOLUME_UNITS
        ]
        if volume_products:
            filtered = volume_products

    winner = pick_clear_winner(filtered)
    if winner is not None:
        return winner, []

    if len(filtered) == 1:
        return filtered[0], []

    return None, filtered


def _format_sale_unit_description(fields: dict[str, Any], *, plural: bool = True) -> str:
    sale_unit = fields["saleUnit"]
    unit_content = fields.get("unitContentLabel", "")
    if sale_unit == "unit" and unit_content:
        return f"unidades ({unit_content})"
    return unit_label(sale_unit, plural=plural)


def _session_product_fields(session: dict[str, Any]) -> dict[str, Any]:
    selected = session.get("selected_product") or {}
    if selected.get("code"):
        return selected
    return {
        "code": session.get("product_code", ""),
        "name": session.get("product_name", ""),
        "price": float(session.get("unit_price", 0)),
        "stock": float(session.get("stock", 0)),
        "saleUnit": session.get("measure_unit", "unit"),
        "unitContentLabel": str(selected.get("unitContentLabel") or ""),
    }


def _unit_content_label_display(unit_content_label: str) -> str:
    label = unit_content_label.strip()
    suffix = " por unidad"
    if label.lower().endswith(suffix):
        return label[: -len(suffix)].strip()
    return label


def _format_unit_content_answer(fields: dict[str, Any]) -> str:
    sale_unit = fields["saleUnit"]
    product_name = fields["name"]

    if _is_weight_measure(sale_unit):
        unit_name = unit_label(sale_unit, plural=True)
        price_unit = unit_label(sale_unit)
        return (
            f"**{product_name}** se vende por **{unit_name}** "
            f"a **${fields['price']:,.0f} COP** por {price_unit}."
        )

    unit_content = (fields.get("unitContentLabel") or "").strip()
    if unit_content:
        content_display = _unit_content_label_display(unit_content)
        return f"Cada unidad de **{product_name}** trae **{content_display}**."

    return (
        f"No tengo el detalle de contenido por unidad de **{product_name}** "
        "en el catálogo."
    )


def _unit_content_inquiry_response(
    session: dict[str, Any],
    phase: str,
) -> tuple[str, list[str]]:
    fields = _session_product_fields(session)
    answer = _format_unit_content_answer(fields)
    sale_unit = fields["saleUnit"]
    stock = float(fields.get("stock") or session.get("stock", 0))

    if phase == "awaiting_confirmation":
        summary = session.get("operation_summary") or {}
        if summary:
            follow_up = f"\n\n{_format_cart_summary_text(summary)}"
        else:
            follow_up = " ¿Confirmas la compra?"
        chips = _confirmation_chips()
    else:
        sale_description = _format_sale_unit_description(fields)
        follow_up = f" ¿Cuántos {sale_description} necesitas?"
        chips = _quantity_chips(stock, sale_unit) + ["Cancelar"]

    return f"{answer}{follow_up}", chips


def _format_quantity_label(quantity: float, sale_unit: str) -> str:
    label = unit_label(sale_unit, plural=quantity != 1)
    qty_text = f"{quantity:g}"
    return f"{qty_text} {label}"


def _quantity_chip_values(stock: float) -> list[float]:
    """Suggest up to three quantity shortcuts, never above available stock."""
    stock = max(float(stock), 0)
    if stock <= 0:
        return [1.0]
    values: list[float] = [min(50.0, stock)]
    if stock >= 10:
        values.append(min(10.0, stock))
    elif stock >= 2:
        values.append(max(1.0, stock // 2))
    if stock > 10:
        values.append(min(5.0, stock // 2))
    seen: set[float] = set()
    result: list[float] = []
    for value in values:
        capped = min(value, stock)
        if capped > 0 and capped not in seen:
            seen.add(capped)
            result.append(capped)
    return result[:3]


def _quantity_chips(stock: float, sale_unit: str) -> list[str]:
    return [_format_quantity_label(value, sale_unit) for value in _quantity_chip_values(stock)]


MENU_CHIPS = ["¿Cómo me comunico?", "Ver catálogo", "Ver factura", "Consultar stock", "Buscar producto"]

COMMUNICATION_GUIDE = (
    "¡Hola! Soy **Drogui**, tu asistente de ventas en El Plonsazo.\n\n"
    "Puedes escribir en **lenguaje natural** para buscar productos, consultar stock o iniciar una compra.\n\n"
    "**Qué puedo hacer por ti:**\n"
    "• Consultar **stock** y precio de un producto (ej. «consultar stock de PLZ-MJ-001»)\n"
    "• **Buscar** en el catálogo (ej. «buscar marihuana» o «lsd»)\n"
    "• Ver el **catálogo** cuando lo pidas (ej. «ver catálogo»)\n"
    "• Consultar tus **facturas** (ej. «ver factura» o «mis facturas»)\n"
    "• **Comprar** un producto (ej. «quiero comprar cocaina»)\n"
    "• **Cancelar** la operación en curso\n\n"
    "También puedes usar los botones del menú debajo del chat."
)

# Backward-compatible alias for tests and imports
HELP_TEXT = COMMUNICATION_GUIDE


def _idle_welcome() -> tuple[str, list[str]]:
    return (
        "¡Hola! Soy **Drogui**, tu asistente de ventas en El Plonsazo.\n\n"
        "Puedo ayudarte con stock, búsqueda y compras. Escríbeme en **lenguaje natural** "
        "o elige una opción del menú. Para ver el catálogo completo, escribe **«ver catálogo»** "
        "(se muestra en páginas de 5 productos). Para tus facturas, **pídeme ver factura**.\n\n"
        "**Ejemplos:**\n"
        "• «consultar stock de PLZ-MJ-001»\n"
        "• «buscar lsd» o simplemente «lsd»\n"
        "• «ver catálogo»\n"
        "• «quiero comprar cocaina»\n"
        "• «ver factura» o «mis facturas»\n"
        "• «agregar al carrito»\n"
        "• «cancelar»",
        MENU_CHIPS,
    )


def _purchase_prompt() -> tuple[str, list[str]]:
    return (
        "¿Qué producto quieres comprar? Indica el **nombre** o **SKU** "
        "(por ejemplo: marihuana, lsd, PLZ-MJ-001).",
        ["Ver catálogo", "Buscar producto", "Consultar stock", "Cancelar"],
    )


def _cancel_ack() -> tuple[str, list[str]]:
    return (
        "Operación cancelada. ¿En qué más puedo ayudarte?",
        MENU_CHIPS,
    )


def _clear_current_product(session: dict[str, Any]) -> None:
    """Clear in-progress product selection without touching the cart."""
    session.update(
        {
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0.0,
            "measure_unit": "unit",
            "selected_product": {},
            "awaiting_quantity": False,
        }
    )


def _clear_cart_snapshot(session: dict[str, Any]) -> None:
    session["saved_cart"] = None


def _clear_pending_add_queue(session: dict[str, Any]) -> None:
    session["pending_add_queue"] = []


def _clear_pending_order_queue(session: dict[str, Any]) -> None:
    session["pending_order_queue"] = []


def _save_cart_snapshot(session: dict[str, Any]) -> None:
    cart = session.get("cart") or []
    if cart and session.get("saved_cart") is None:
        session["saved_cart"] = copy.deepcopy(cart)


def _restore_cart_snapshot(
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    """Abandon the current add flow and return to the saved cart confirmation."""
    saved = session.get("saved_cart")
    if saved is not None:
        session["cart"] = copy.deepcopy(saved)
    _clear_cart_snapshot(session)
    _clear_pending_add_queue(session)
    _clear_current_product(session)
    session["adding_to_cart"] = False
    session["awaiting_product_search"] = False
    return _enter_awaiting_confirmation(session)


def _abandon_add_flow(
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]] | None:
    """Return to cart confirmation when the user abandons an in-progress add."""
    if session.get("saved_cart") is not None:
        return _restore_cart_snapshot(session)
    if session.get("cart"):
        _clear_cart_snapshot(session)
        _clear_pending_add_queue(session)
        _clear_current_product(session)
        session["adding_to_cart"] = False
        session["awaiting_product_search"] = False
        return _enter_awaiting_confirmation(session)
    return None


def _clear_purchase_flow(session: dict[str, Any]) -> None:
    session.update(
        {
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0.0,
            "measure_unit": "unit",
            "invoice_number": "",
            "operation_summary": {},
            "pending_sale": False,
            "cart": [],
            "saved_cart": None,
            "pending_add_queue": [],
            "pending_order_queue": [],
            "adding_to_cart": False,
            "selected_product": {},
            "awaiting_quantity": False,
            "delivery_address": "",
            "delivery_city": "",
        }
    )


def _reset_flow(session: dict[str, Any]) -> None:
    session.update(
        {
            "phase": "idle",
            "product_code": "",
            "product_name": "",
            "unit_price": 0.0,
            "stock": 0,
            "quantity": 0.0,
            "measure_unit": "unit",
            "invoice_number": "",
            "operation_summary": {},
            "pending_sale": False,
            "cart": [],
            "saved_cart": None,
            "pending_add_queue": [],
            "pending_order_queue": [],
            "selected_product": {},
            "last_intent": "",
            "awaiting_quantity": False,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
            "adding_to_cart": False,
            "delivery_address": "",
            "delivery_city": "",
        }
    )


def _out_of_stock_chips(session: dict[str, Any]) -> list[str]:
    if session.get("cart") or session.get("saved_cart"):
        return _confirmation_chips()
    return ["Buscar otro producto", "Ver catálogo", "Cancelar"]


def _out_of_stock_response(
    fields: dict[str, Any],
    session: dict[str, Any],
    *,
    adding: bool = False,
) -> tuple[str, list[str]]:
    prefix = "Agregando" if adding else "Encontré"
    message = (
        f"{prefix} **{fields['name']}** (`{fields['code']}`), pero "
        "**no tiene stock disponible** en este momento. "
    )
    if session.get("cart") or session.get("saved_cart"):
        message += "Elige otro SKU o continúa con tu carrito actual."
    else:
        message += "Elige otro producto o SKU del catálogo."
    return message, _out_of_stock_chips(session)


def _start_awaiting_quantity(session: dict[str, Any], product: dict[str, Any]) -> None:
    fields = _product_fields(product)
    session.update(
        {
            "phase": "awaiting_quantity",
            "product_code": fields["code"],
            "product_name": fields["name"],
            "unit_price": fields["price"],
            "stock": fields["stock"],
            "measure_unit": fields["saleUnit"],
            "pending_sale": True,
            "selected_product": fields,
            "awaiting_quantity": True,
            "awaiting_stock_sku": False,
            "awaiting_product_search": False,
            "delivery_address": "",
            "delivery_city": "",
        }
    )


def _try_start_awaiting_quantity(
    session: dict[str, Any],
    product: dict[str, Any],
    *,
    adding: bool = False,
) -> tuple[bool, tuple[str, list[str]] | None]:
    fields = _product_fields(product)
    if fields["stock"] <= 0:
        return False, _out_of_stock_response(fields, session, adding=adding)
    _start_awaiting_quantity(session, product)
    return True, None


def _looks_like_product_search(message: str) -> bool:
    if _is_price_inquiry(message):
        return False
    if _is_menu_intent(message) or _is_intent_phrase(message):
        return False
    if _is_greeting(message):
        return False
    if _is_vague_purchase_intent(message):
        return False
    if _extract_code(message):
        return True
    if _is_purchase_intent(message):
        return True
    return _extract_search_terms(message) is not None


def _try_add_item_to_cart(
    session: dict[str, Any],
    product: dict[str, Any],
    quantity: float,
    measure_unit: str | None,
) -> tuple[bool, str | None]:
    """Add a resolved product line to the cart; return (success, warning)."""
    fields = _product_fields(product)
    if fields["stock"] <= 0:
        return False, (
            f"**{fields['name']}** (`{fields['code']}`) no tiene stock disponible."
        )
    try:
        normalized_qty, resolved_unit = resolve_sale_quantity(
            quantity,
            measure_unit,
            fields["saleUnit"],
        )
    except ValueError:
        if measure_unit == "unit" and fields["saleUnit"] != "unit":
            try:
                normalized_qty, resolved_unit = resolve_sale_quantity(
                    quantity,
                    fields["saleUnit"],
                    fields["saleUnit"],
                )
            except ValueError as exc:
                return False, str(exc)
        else:
            return False, (
                f"Este producto se vende por "
                f"{unit_label(fields['saleUnit'], plural=True)}."
            )
    if normalized_qty > fields["stock"]:
        stock_label = _format_quantity_label(fields["stock"], fields["saleUnit"])
        return False, (
            f"Solo hay **{stock_label}** de {fields['name']}. No se agregó al carrito."
        )
    line = {
        "productCode": fields["code"],
        "productName": fields["name"],
        "quantity": normalized_qty,
        "measureUnit": resolved_unit,
        "unitPrice": fields["price"],
        "subtotal": round(fields["price"] * normalized_qty, 2),
    }
    cart = [
        item
        for item in (session.get("cart") or [])
        if item.get("productCode") != line["productCode"]
    ]
    cart.append(line)
    session["cart"] = cart
    return True, None


def _format_order_warnings(warnings: list[str]) -> str:
    if not warnings:
        return ""
    return "\n".join(f"- {warning}" for warning in warnings)


async def _advance_order_queue(
    session: dict[str, Any],
    queue: list[dict[str, Any]],
    warnings: list[str],
) -> tuple[str, list[str], dict[str, Any] | None]:
    remaining = list(queue)
    while remaining:
        item = remaining[0]
        product_query = item["product_query"]
        quantity = float(item["quantity"])
        measure_unit = item.get("unit")

        products = await _find_products(product_query)
        if not products:
            warnings.append(f"No encontré productos para «{product_query}».")
            remaining.pop(0)
            continue

        product, candidates = _pick_product_for_purchase(
            products,
            quantity=quantity,
            measure_unit=measure_unit,
            product_query=product_query,
            code=None,
        )

        if product is None and candidates:
            session["pending_order_queue"] = remaining
            session["pending_sale"] = True
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            lines = _format_product_lines(candidates)
            chips = [
                _product_fields(candidate)["code"]
                for candidate in candidates
                if _product_fields(candidate)["code"]
            ]
            chips.extend(["Cancelar"])
            unit_name = unit_label(measure_unit or "unit", plural=quantity != 1)
            header = (
                f"Para **{_search_display_label(product_query)}** "
                f"({quantity:g} {unit_name}), encontré varios productos:\n\n"
            )
            response = (
                f"{header}{lines}\n\n"
                "Selecciona el **SKU** del producto que quieres."
            )
            prefix = _format_order_warnings(warnings)
            if prefix:
                response = f"{prefix}\n\n{response}"
            return response, chips[:8], None

        if product is None:
            product = products[0]

        _, warn = _try_add_item_to_cart(session, product, quantity, measure_unit)
        if warn:
            warnings.append(warn)
        remaining.pop(0)

    session["pending_order_queue"] = []
    if not session.get("cart"):
        response = "No pude agregar productos al carrito."
        prefix = _format_order_warnings(warnings)
        if prefix:
            response = f"{prefix}\n\n{response}"
        return response, ["Buscar producto", "Ver catálogo", "Cancelar"], None

    session["pending_sale"] = True
    response, chips, summary = _enter_awaiting_confirmation(session)
    prefix = _format_order_warnings(warnings)
    if prefix:
        response = f"{prefix}\n\n{response}"
    return response, chips, summary


async def _resolve_multi_item_order(
    message: str,
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any] | None]:
    items = _parse_multi_item_order(message)
    session.setdefault("cart", [])
    session["pending_order_queue"] = []
    return await _advance_order_queue(session, items, [])


async def _resolve_idle_product_purchase(
    message: str,
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any] | None]:
    """Lookup product from idle; apply quantity from message when present."""
    if _is_multi_item_order(message):
        return await _resolve_multi_item_order(message, session)

    code = _extract_code(message)
    quantity, measure_unit, product_query = _extract_purchase_details(message)
    lookup_message = code or product_query or message
    products = await _find_products(lookup_message)
    if not products:
        label = code or _search_display_label(message)
        return (
            f"No encontré productos para «{label}». "
            "Prueba con otro nombre o SKU del catálogo.",
            ["Buscar producto", "Ver catálogo", "Consultar stock"],
            None,
        )

    product, candidates = _pick_product_for_purchase(
        products,
        quantity=quantity,
        measure_unit=measure_unit,
        product_query=product_query,
        code=code,
    )

    if product is None and candidates:
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        session["pending_sale"] = True
        lines = _format_product_lines(candidates)
        chips = [
            _product_fields(item)["code"]
            for item in candidates
            if _product_fields(item)["code"]
        ]
        chips.extend(["Ver catálogo", "Consultar stock", "Cancelar"])
        if _is_weight_measure(measure_unit) and all(
            _product_fields(item)["saleUnit"] not in WEIGHT_UNITS for item in candidates
        ):
            header = (
                "Encontré productos con ese nombre, pero se venden por **unidades**. "
                "Selecciona el **SKU** si quieres uno de ellos, o busca un producto por peso:\n\n"
            )
        else:
            header = "Encontré varios productos que coinciden:\n\n"
        return (
            f"{header}{lines}\n\n"
            "Selecciona el **SKU** del producto que te interesa.",
            chips[:8],
            None,
        )

    if product is None:
        product = products[0]

    fields = _product_fields(product)
    started, error = _try_start_awaiting_quantity(session, product)
    if not started:
        assert error is not None
        return error[0], error[1], None
    sale_unit = fields["saleUnit"]

    if quantity is not None:
        try:
            normalized_qty, resolved_unit = resolve_sale_quantity(
                quantity,
                measure_unit,
                sale_unit,
            )
        except ValueError as exc:
            return (str(exc), ["Modificar cantidad", "Cancelar"], None)

        if normalized_qty > fields["stock"]:
            stock_label = _format_quantity_label(fields["stock"], sale_unit)
            return (
                f"Solo hay **{stock_label}** de {fields['name']}. "
                "Ajusta la cantidad o elige otro producto.",
                [f"{fields['stock']:g} {unit_label(sale_unit, plural=True)}", "Buscar otro producto", "Cancelar"],
                None,
            )
        session["quantity"] = normalized_qty
        session["measure_unit"] = resolved_unit
        response, chips, summary = _enter_awaiting_confirmation(session)
        return response, chips, summary

    unit_name = unit_label(sale_unit)
    sale_description = _format_sale_unit_description(fields)
    stock_label = _format_quantity_label(fields["stock"], sale_unit)
    return (
        f"Encontré **{fields['name']}** ({fields['code']}). "
        f"Hay **{stock_label}** disponibles a "
        f"**${fields['price']:,.0f} COP** por {unit_name}. "
        f"Se vende por {sale_description}. "
        f"¿Cuántos {sale_description} necesitas?",
        _quantity_chips(fields["stock"], sale_unit) + ["Cancelar"],
        None,
    )


async def _search_liquid_by_sale_unit() -> list[dict[str, Any]]:
    products, _ = await dotnet_tools.search_products_paged("", page_size=50)
    return [
        product
        for product in products
        if _product_fields(product)["saleUnit"] == "milliliter"
    ]


async def _search_with_fallbacks(
    terms: str | list[str],
    *,
    category: str | None = None,
) -> list[dict[str, Any]]:
    queries = [terms] if isinstance(terms, str) else [query for query in terms if query]
    if not queries:
        return []

    primary = queries[0]
    if _looks_like_sku(primary):
        product = await dotnet_tools.get_product_by_code(primary)
        return [product] if product else []

    seen_codes: set[str] = set()
    results: list[dict[str, Any]] = []

    async def add_from_query(query: str) -> None:
        if not query:
            return
        for product in await dotnet_tools.search_products(query):
            fields = _product_fields(product)
            if fields["code"] and fields["code"] not in seen_codes:
                seen_codes.add(fields["code"])
                results.append(product)

    for query in queries:
        await add_from_query(query)

    stripped_queries = [_strip_accents(query) for query in queries]
    for query, stripped in zip(queries, stripped_queries):
        if stripped != query:
            await add_from_query(stripped)

    # Accent mismatch: "cocaina" vs "cocaína" — prefix still matches in catalog.
    for stripped in stripped_queries:
        if len(stripped) >= 4:
            await add_from_query(stripped[:4])
        elif len(stripped) >= 3:
            await add_from_query(stripped[:3])

    if category == "liquid":
        liquid_products = await _search_liquid_by_sale_unit()
        for product in liquid_products:
            fields = _product_fields(product)
            if fields["code"] and fields["code"] not in seen_codes:
                seen_codes.add(fields["code"])
                results.append(product)

    return results


async def _find_products(message: str) -> list[dict[str, Any]]:
    code = _extract_code(message)
    if code:
        product = await dotnet_tools.get_product_by_code(code)
        return [product] if product else []

    stripped = _strip_bot_name(_strip_greeting_prefix(message))
    _, category = _resolve_search_queries_from_text(stripped or message)
    queries = _extract_search_queries(message)
    if not queries:
        return []

    if _looks_like_sku(queries[0]):
        product = await dotnet_tools.get_product_by_code(queries[0])
        return [product] if product else []

    return await _search_with_fallbacks(queries, category=category)


async def _lookup_product(query: str) -> dict[str, Any] | None:
    normalized = query.strip()
    if (
        not normalized
        or _is_greeting(normalized)
        or _is_menu_intent(normalized)
        or _is_intent_phrase(normalized)
    ):
        return None
    products = await _find_products(normalized)
    return products[0] if products else None


def _stock_result_chips() -> list[str]:
    return ["Buscar producto", "Ver catálogo", "Consultar stock"]


def _stock_prompt_chips() -> list[str]:
    return ["PLZ-MJ-001", "busco marihuana", "cancelar"]


def _format_stock_response(stock_info: dict[str, Any]) -> str:
    stock_label = format_stock(stock_info)
    sale_unit = normalize_unit(str(stock_info.get("saleUnit", "unit")))
    unit_name = unit_label(sale_unit)
    return (
        f"Stock de **{stock_info['name']}** (`{stock_info['code']}`): "
        f"**{stock_label}** disponibles a "
        f"**${float(stock_info['price']):,.0f} COP** por {unit_name}."
    )


def _price_result_chips() -> list[str]:
    return ["Consultar stock", "Buscar producto", "Ver catálogo"]


def _format_price_response(fields: dict[str, Any]) -> str:
    sale_unit = fields.get("saleUnit", "unit")
    stock_label = _format_quantity_label(float(fields["stock"]), sale_unit)
    unit_name = unit_label(sale_unit)
    return (
        f"El precio de **{fields['name']}** (`{fields['code']}`) es "
        f"**${fields['price']:,.0f} COP** por {unit_name}. "
        f"Stock disponible: **{stock_label}**."
    )


async def _resolve_price_inquiry(message: str) -> tuple[str, list[str]]:
    """Lookup catalog price/stock for a price-only question; session stays idle."""
    code = _extract_code(message)
    product_name = _extract_product_from_inquiry(message)

    if code:
        try:
            stock_info = await dotnet_tools.check_stock(code)
        except ValueError:
            return (
                f"No encontré el producto **{code}** en el catálogo. "
                "Verifica el SKU o prueba **Buscar producto**.",
                ["Buscar producto", "Ver catálogo", "Cancelar"],
            )
        return _format_price_response(stock_info), _price_result_chips()

    if not product_name:
        return (
            "Indícame el **nombre** o **SKU** del producto cuyo precio quieres consultar "
            "(ej. marihuana, cocaina, `PLZ-MJ-001`).",
            _price_result_chips(),
        )

    queries, category = _resolve_search_queries_from_text(product_name)
    products = await _search_with_fallbacks(queries or [product_name], category=category)
    if not products:
        label = _search_display_label(product_name)
        return (
            f"No encontré productos para «{label}». "
            "Prueba con otro nombre o un SKU como **PLZ-MJ-001**.",
            ["Buscar producto", "Ver catálogo", "Consultar stock"],
        )

    if len(products) == 1:
        return _format_price_response(_product_fields(products[0])), _price_result_chips()

    lines = _format_product_lines(products)
    chips = [_product_fields(product)["code"] for product in products if _product_fields(product)["code"]]
    chips.extend(["Buscar producto", "Cancelar"])
    return (
        "Encontré varios productos que coinciden:\n\n"
        f"{lines}\n\n"
        "Indícame el **SKU** del producto cuyo precio quieres consultar.",
        chips[:8],
    )


async def _resolve_stock_lookup(message: str) -> tuple[str, list[str], bool]:
    """Return (response, chips, found). Keeps session phase unchanged when picking among matches."""
    code = _extract_code(message)
    if code:
        try:
            stock_info = await dotnet_tools.check_stock(code)
        except ValueError:
            return (
                f"No encontré el producto **{code}** en el catálogo. "
                "Verifica el SKU o prueba **Buscar producto**.",
                ["Buscar producto", "Ver catálogo", "Cancelar"],
                False,
            )
        return _format_stock_response(stock_info), _stock_result_chips(), True

    if _is_intent_phrase(message) or not _extract_search_terms(message):
        return (
            "Indícame el **nombre** o **SKU** del producto cuyo stock quieres consultar "
            "(ej. marihuana, cocaina, `PLZ-MJ-001`).",
            _stock_prompt_chips(),
            False,
        )

    products = await _find_products(message)
    if not products:
        label = _search_display_label(message)
        return (
            f"No encontré productos para «{label}». "
            "Prueba con otro nombre, un SKU como **PLZ-MJ-001** o usa **Buscar producto**.",
            ["Buscar producto", "Ver catálogo", "Cancelar"],
            False,
        )

    if len(products) == 1:
        fields = _product_fields(products[0])
        return (
            _format_stock_response(fields),
            _stock_result_chips(),
            True,
        )

    lines = _format_product_lines(products)
    chips = [_product_fields(product)["code"] for product in products if _product_fields(product)["code"]]
    chips.extend(["Buscar producto", "Cancelar"])
    return (
        "Encontré varios productos que coinciden:\n\n"
        f"{lines}\n\n"
        "Selecciona el **SKU** del producto cuyo stock quieres consultar.",
        chips[:8],
        False,
    )


async def _resolve_product_search(
    message: str,
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any] | None]:
    if _is_multi_item_order(message):
        return await _resolve_multi_item_order(message, session)

    pending = session.get("pending_order_queue") or []
    if pending:
        code = _extract_code(message)
        product: dict[str, Any] | None = None
        if code:
            product = await dotnet_tools.get_product_by_code(code)
        if product is None:
            products = await _find_products(message)
            if len(products) == 1:
                product = products[0]
            elif len(products) > 1:
                item = pending[0]
                product, _ = _pick_product_for_purchase(
                    products,
                    quantity=item["quantity"],
                    measure_unit=item.get("unit"),
                    product_query=item["product_query"],
                    code=code,
                )
        if product is not None:
            item = pending[0]
            warnings: list[str] = []
            _, warn = _try_add_item_to_cart(
                session,
                product,
                float(item["quantity"]),
                item.get("unit"),
            )
            if warn:
                warnings.append(warn)
            return await _advance_order_queue(session, pending[1:], warnings)

    products = await _find_products(message)
    if not products:
        label = _search_display_label(message)
        return (
            f"No encontré productos para «{label}». "
            "Prueba con otro nombre o SKU.",
            ["Ver catálogo", "Consultar stock", "Cancelar"],
            None,
        )

    if len(products) == 1:
        fields = _product_fields(products[0])
        started, error = _try_start_awaiting_quantity(
            session,
            products[0],
            adding=bool(session.get("adding_to_cart")),
        )
        if not started:
            assert error is not None
            if session.get("cart"):
                session["phase"] = "awaiting_confirmation"
            return error[0], error[1], None
        fields = _product_fields(products[0])
        stock_label = _format_quantity_label(fields["stock"], fields["saleUnit"])
        prefix = "Agregando" if session.get("adding_to_cart") else "Encontré"
        return (
            f"{prefix} **{fields['name']}** ({fields['code']}). "
            f"Hay **{stock_label}** disponibles a "
            f"**${fields['price']:,.0f} COP** por {unit_label(fields['saleUnit'])}. "
            f"¿Cuántos {unit_label(fields['saleUnit'], plural=True)} necesitas?",
            _quantity_chips(fields["stock"], fields["saleUnit"]) + ["Cancelar"],
            None,
        )

    lines = _format_product_lines(products)
    chips = [_product_fields(product)["code"] for product in products if _product_fields(product)["code"]]
    chips.extend(["Ver catálogo", "Consultar stock", "Cancelar"])
    return (
        "Encontré varios productos que coinciden:\n\n"
        f"{lines}\n\n"
        "Selecciona el **SKU** del producto que te interesa.",
        chips[:8],
        None,
    )


async def _resolve_add_to_cart_product(
    message: str,
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any] | None]:
    """Search a product to append to the existing cart."""
    _save_cart_snapshot(session)
    session["adding_to_cart"] = True

    quantity, measure_unit, product_query = _extract_add_to_cart_details(message)
    if product_query is None:
        queries = _extract_continue_shopping_queries(message)
        if queries:
            session["pending_add_queue"] = queries[1:]
            product_query = queries[0]
        else:
            product_query = _extract_add_to_cart_query(message)

    if product_query is None:
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        session["adding_to_cart"] = True
        return (
            "¿Qué producto quieres agregar al carrito? Indica el **nombre** o **SKU**.",
            ["Ver catálogo", "Cancelar"],
            session.get("operation_summary") or None,
        )

    if quantity is None and product_query:
        parts = _split_product_queries(product_query)
        if len(parts) > 1 and not session.get("pending_add_queue"):
            session["pending_add_queue"] = parts[1:]
            product_query = parts[0]

    code = _extract_code(product_query)
    products = await _find_products(code or product_query)
    if not products:
        session["adding_to_cart"] = bool(session.get("saved_cart"))
        label = code or _search_display_label(product_query)
        return (
            f"No encontré productos para «{label}». "
            "Prueba con otro nombre o SKU del catálogo.",
            _confirmation_chips() if session.get("cart") else ["Buscar producto", "Ver catálogo", "Cancelar"],
            session.get("operation_summary") or None,
        )

    product, candidates = _pick_product_for_purchase(
        products,
        quantity=quantity,
        measure_unit=measure_unit,
        product_query=product_query,
        code=code,
    )
    if product is None and candidates:
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        lines = _format_product_lines(candidates)
        chips = [
            _product_fields(item)["code"]
            for item in candidates
            if _product_fields(item)["code"]
        ]
        chips.extend(_confirmation_chips() if session.get("cart") else ["Ver catálogo", "Cancelar"])
        return (
            "Encontré varios productos que coinciden:\n\n"
            f"{lines}\n\n"
            "Selecciona el **SKU** del producto que quieres agregar.",
            chips[:8],
            session.get("operation_summary") or None,
        )

    if product is None:
        product = products[0]

    if quantity is not None:
        warnings: list[str] = []
        _, warn = _try_add_item_to_cart(session, product, quantity, measure_unit)
        if warn:
            warnings.append(warn)
        pending_add = session.get("pending_add_queue") or []
        if pending_add:
            session["pending_add_queue"] = pending_add[1:]
            next_query = pending_add[0]
            session["adding_to_cart"] = True
            prefix = _format_order_warnings(warnings)
            response, chips, summary = await _resolve_add_to_cart_product(
                f"agrega {next_query}",
                session,
            )
            if prefix:
                response = f"{prefix}\n\n{response}"
            return response, chips, summary
        session["pending_sale"] = True
        response, chips, summary = _enter_awaiting_confirmation(session)
        prefix = _format_order_warnings(warnings)
        if prefix:
            response = f"{prefix}\n\n{response}"
        return response, chips, summary

    fields = _product_fields(product)
    started, error = _try_start_awaiting_quantity(session, product, adding=True)
    if not started:
        assert error is not None
        _clear_current_product(session)
        if session.get("cart"):
            session["phase"] = "awaiting_confirmation"
        return error[0], error[1], session.get("operation_summary") or None

    sale_description = _format_sale_unit_description(fields)
    stock_label = _format_quantity_label(fields["stock"], fields["saleUnit"])
    return (
        f"Agregando **{fields['name']}** ({fields['code']}). "
        f"Hay **{stock_label}** disponibles a "
        f"**${fields['price']:,.0f} COP** por {unit_label(fields['saleUnit'])}. "
        f"¿Cuántos {sale_description} quieres agregar?",
        _quantity_chips(fields["stock"], fields["saleUnit"]) + ["Cancelar"],
        None,
    )


def _format_product_lines(products: list[dict[str, Any]], limit: int = 5) -> str:
    lines: list[str] = []
    for product in products[:limit]:
        fields = _product_fields(product)
        if not fields["code"]:
            continue
        unit_hint = ""
        if fields["saleUnit"] == "unit" and fields.get("unitContentLabel"):
            unit_hint = f" — se vende por unidades ({fields['unitContentLabel']})"
        lines.append(
            f"- **{fields['name']}** (`{fields['code']}`) — "
            f"${fields['price']:,.0f} COP/{unit_short(fields['saleUnit'])} — "
            f"stock: **{_format_quantity_label(fields['stock'], fields['saleUnit'])}**"
            f"{unit_hint}"
        )
    return "\n".join(lines)


def _is_test_product(code: str) -> bool:
    return "COPY-TEST" in code.upper()


def _build_offers(
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    offers: list[dict[str, Any]] = []
    for product in products:
        fields = _product_fields(product)
        if not fields["code"] or _is_test_product(fields["code"]):
            continue
        status = str(pick(product, "status", "Status", default="active")).lower()
        if status in {"inactive", "archived", "out_of_stock"}:
            continue
        if fields["stock"] <= 0:
            continue
        offers.append(
            {
                "productCode": fields["code"],
                "productName": fields["name"],
                "unitPrice": fields["price"],
                "stock": fields["stock"],
                "saleUnit": fields["saleUnit"],
            }
        )
    return offers, len(offers)


def _catalog_chips(session: dict[str, Any]) -> list[str]:
    chips = ["Ver catálogo", "busco marihuana"]
    if not session.get("catalog_fully_loaded"):
        chips.insert(0, CATALOG_LOAD_MORE_CHIP)
    return chips


async def _fetch_catalog_page(query: str, page: int) -> tuple[list[dict[str, Any]], int, str]:
    products, catalog_total = await dotnet_tools.search_products_paged(
        query,
        page=page,
        page_size=OFFERS_PAGE_SIZE,
    )
    if not products and query == "PLZ":
        products, catalog_total = await dotnet_tools.search_products_paged(
            "",
            page=page,
            page_size=OFFERS_PAGE_SIZE,
        )
        query = ""
    return products, catalog_total, query


async def _handle_ver_ofertas(
    session: dict[str, Any],
    *,
    load_more: bool = False,
    load_all: bool = True,
) -> tuple[str, list[str], list[dict[str, Any]], int]:
    if load_all:
        session["catalog_offers"] = []
        session["catalog_page"] = 1
        session["catalog_query"] = "PLZ"
        page = 1
        query = "PLZ"
        catalog_total = 0
        while page <= 50:
            products, catalog_total, query = await _fetch_catalog_page(query, page)
            new_offers, _ = _build_offers(products)
            if not new_offers:
                break
            existing_codes = {offer["productCode"] for offer in session["catalog_offers"]}
            for offer in new_offers:
                if offer["productCode"] not in existing_codes:
                    session["catalog_offers"].append(offer)
                    existing_codes.add(offer["productCode"])
            if len(session["catalog_offers"]) >= catalog_total or len(new_offers) < OFFERS_PAGE_SIZE:
                break
            page += 1
        session["catalog_page"] = page
        session["catalog_query"] = query
        session["catalog_fully_loaded"] = True
    elif load_more and session.get("catalog_offers"):
        page = int(session.get("catalog_page", 1)) + 1
        query = str(session.get("catalog_query", "PLZ"))
        products, catalog_total, query = await _fetch_catalog_page(query, page)
        new_offers, _ = _build_offers(products)
        existing_codes = {offer["productCode"] for offer in session.get("catalog_offers", [])}
        for offer in new_offers:
            if offer["productCode"] not in existing_codes:
                session.setdefault("catalog_offers", []).append(offer)
                existing_codes.add(offer["productCode"])
        session["catalog_page"] = page
        session["catalog_query"] = query
        session["catalog_fully_loaded"] = (
            len(session.get("catalog_offers", [])) >= catalog_total
            or len(new_offers) < OFFERS_PAGE_SIZE
        )
    else:
        page = 1
        query = "PLZ"
        session["catalog_offers"] = []
        session["catalog_page"] = 1
        session["catalog_query"] = "PLZ"
        products, catalog_total, query = await _fetch_catalog_page(query, page)
        new_offers, _ = _build_offers(products)
        existing_codes = {offer["productCode"] for offer in session.get("catalog_offers", [])}
        for offer in new_offers:
            if offer["productCode"] not in existing_codes:
                session.setdefault("catalog_offers", []).append(offer)
                existing_codes.add(offer["productCode"])
        session["catalog_page"] = page
        session["catalog_query"] = query
        session["catalog_fully_loaded"] = (
            len(session.get("catalog_offers", [])) >= catalog_total
            or len(new_offers) < OFFERS_PAGE_SIZE
        )

    offers = list(session.get("catalog_offers", []))
    total = catalog_total if offers else 0
    chips = _catalog_chips(session)

    if offers:
        if session.get("catalog_fully_loaded"):
            response = (
                f"Catálogo completo: **{len(offers)}** productos. "
                "Toca un producto para seleccionarlo o escribe su SKU. "
                "Usa las flechas del carrusel para ver más páginas."
            )
        elif load_more:
            response = (
                f"Mostrando **{len(offers)}** de **{total}** productos. "
                "¿Qué producto te interesa?"
            )
        else:
            response = "Aquí tienes el catálogo. ¿Qué producto te interesa?"
            if len(offers) < total:
                response += f" Hay **{total}** productos en total."
    else:
        response = (
            "No pude cargar el catálogo en este momento. "
            "Prueba con un SKU como **PLZ-MJ-001** o usa **Buscar producto**."
        )
    return response, chips, offers, total


async def _handle_ver_factura(session: dict[str, Any]) -> tuple[str, list[str], list[dict[str, Any]], int]:
    session["phase"] = "idle"
    invoice_number = str(session.get("invoice_number") or "").strip()
    if invoice_number:
        response = (
            f"Tu última factura es **{invoice_number}**. "
            "Puedes ver el detalle y el historial en **Mis facturas** desde el menú de la aplicación."
        )
    else:
        response = (
            "Puedes consultar tus facturas en **Mis facturas** desde el menú lateral. "
            "Si acabas de comprar, también aparecerán ahí."
        )
    return response, MENU_CHIPS, [], 0


async def _handle_menu_intent(
    intent: str,
    session: dict[str, Any],
) -> tuple[str, list[str], list[dict[str, Any]], int]:
    """Handle chip/menu intents — never calls search_products with the label text."""
    if intent == "ayuda":
        session["phase"] = "idle"
        return COMMUNICATION_GUIDE, MENU_CHIPS, [], 0
    if intent == "consultar_stock":
        session["phase"] = "awaiting_stock_sku"
        session["awaiting_stock_sku"] = True
        session["awaiting_quantity"] = False
        return (
            "Para consultar stock, indícame el **nombre** o **SKU** del producto "
            "(por ejemplo: marihuana, cocaina, `PLZ-MJ-001`).",
            ["PLZ-MJ-001", "Buscar producto", "Cancelar"],
            [],
            0,
        )
    if intent == "buscar_producto":
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        session["awaiting_stock_sku"] = False
        session["awaiting_quantity"] = False
        return (
            "¿Qué producto buscas? Escribe el nombre o el SKU "
            "(por ejemplo: marihuana, lsd, PLZ-MJ-001).",
            ["Ver catálogo", "Consultar stock", "Cancelar"],
            [],
            0,
        )
    if intent == "ver_factura":
        return await _handle_ver_factura(session)
    response, chips, offers, total = await _handle_ver_ofertas(session)
    session["phase"] = "idle"
    return response, chips, offers, total


def _extract_quantity_with_unit(message: str) -> tuple[float | None, str | None]:
    return extract_quantity_with_unit(message)


def _extract_quantity(message: str) -> float | None:
    quantity, _ = _extract_quantity_with_unit(message)
    return quantity


def _is_confirm(message: str) -> bool:
    text = _normalize_message(message).rstrip(".,!?")
    if text in _CONFIRM_PHRASES:
        return True
    if text.startswith("confirmar"):
        return True
    return "confirmo" in text and ("compra" in text or "pedido" in text)


def _is_cancel(message: str, phase: str = "idle") -> bool:
    text = _normalize_message(message).rstrip(".,!?")
    if phase in {"awaiting_save_address", "awaiting_use_saved_address"}:
        if text in {"no", "no gracias", "nop"}:
            return False
    if text in _CANCEL_EXACT_PHRASES or _normalize_intent(message) == "cancelar":
        return True
    if any(phrase in text for phrase in ("cancelar", "cancelo", "no quiero", "anular")):
        return True
    if phase in FLOW_PHASES and text in {"no", "no gracias", "nop", "salir"}:
        return True
    return False


_ADD_TO_CART_EXACT = frozenset(
    {
        "agregar otro producto",
        "agrega otro producto",
        "añadir otro producto",
        "anadir otro producto",
    }
)

_ADD_TO_CART_PREFIXES = (
    "agrega ",
    "agregar ",
    "añade ",
    "añadir ",
    "anade ",
    "anadir ",
)


def _strip_leading_y(text: str) -> str:
    if text.startswith("y "):
        return text[2:].strip()
    return text


def _normalize_continue_shopping_text(message: str) -> str:
    return _strip_leading_y(
        _strip_accents(_normalize_message(message).rstrip(".,!?"))
    )


def _is_add_to_cart_intent(message: str, phase: str = "idle") -> bool:
    text = _normalize_continue_shopping_text(message)
    if text in _ADD_TO_CART_EXACT:
        return True
    if "tambien quiero" in text:
        return True
    if any(text.startswith(prefix) for prefix in _ADD_TO_CART_PREFIXES):
        return True
    if phase == "awaiting_confirmation":
        if text.startswith("y dame ") or text.startswith("dame "):
            return True
    return False


def _extract_add_to_cart_query(message: str) -> str | None:
    text = _normalize_continue_shopping_text(message)
    if text in _ADD_TO_CART_EXACT:
        return None
    for prefix in _ADD_TO_CART_PREFIXES:
        if text.startswith(prefix):
            query = text[len(prefix) :].strip()
            if query and query not in {"otro producto", "otro"}:
                return _clean_product_query(query)
            return None
    if text.startswith("tambien quiero "):
        query = text[len("tambien quiero ") :].strip()
        if query:
            return _clean_product_query(query)
    return None


def _extract_add_to_cart_details(
    message: str,
) -> tuple[float | None, str | None, str | None]:
    """Parse «agrega 3 unidades de ketamina» into quantity, unit, and product query."""
    text = _normalize_continue_shopping_text(message)
    if text in _ADD_TO_CART_EXACT:
        return None, None, None
    for prefix in _ADD_TO_CART_PREFIXES:
        if not text.startswith(prefix):
            continue
        remainder = text[len(prefix) :].strip()
        match = _MULTI_ITEM_SEGMENT_RE.match(remainder)
        if match:
            quantity = parse_quantity_text(match.group("qty"))
            if quantity is None or quantity <= 0:
                continue
            unit_raw = match.groupdict().get("unit")
            unit = normalize_unit(unit_raw) if unit_raw else None
            product = _clean_product_query(match.group("product"))
            if product and quantity > 0 and not _is_vague_product_term(product):
                return quantity, unit, product
        product = _clean_product_query(remainder)
        if product and product not in {"otro producto", "otro"}:
            return None, None, product
        return None, None, None
    if text.startswith("tambien quiero "):
        remainder = text[len("tambien quiero ") :].strip()
        match = _MULTI_ITEM_SEGMENT_RE.match(remainder)
        if match:
            quantity = parse_quantity_text(match.group("qty"))
            if quantity is None or quantity <= 0:
                pass
            else:
                unit_raw = match.groupdict().get("unit")
                unit = normalize_unit(unit_raw) if unit_raw else None
                product = _clean_product_query(match.group("product"))
                if product and quantity > 0 and not _is_vague_product_term(product):
                    return quantity, unit, product
        if remainder:
            return None, None, _clean_product_query(remainder)
    return None, None, None


_ABANDON_ADD_MARKERS = (
    "solo dame",
    "solo quiero",
    "mejor solo",
    "dejalo en",
    "dejalo solo",
    "deja solo",
    "olvida",
    "olvidate",
    "no quiero el",
    "no quiero la",
    "cancela el",
    "cancela la",
    "entonces solo",
)

_CONTINUE_SHOPPING_PREFIXES = (
    "y dame ",
    "dame ",
    "agrega ",
    "agregar ",
    "añade ",
    "añadir ",
    "anade ",
    "anadir ",
    "tambien quiero ",
    "también quiero ",
)


def _is_abandon_add_intent(message: str) -> bool:
    text = _strip_accents(_normalize_message(message).rstrip(".,!?"))
    return any(marker in text for marker in _ABANDON_ADD_MARKERS)


def _split_product_queries(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s+y\s+", text) if part.strip()]
    queries = [_clean_product_query(part) for part in parts]
    return [query for query in queries if query and not _is_vague_product_term(query)]


def _extract_continue_shopping_queries(message: str) -> list[str]:
    add_query = _extract_add_to_cart_query(message)
    if add_query:
        return _split_product_queries(add_query)

    text = _normalize_continue_shopping_text(message)
    for prefix in _CONTINUE_SHOPPING_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    else:
        _, _, product = _extract_purchase_details(message)
        if product:
            return _split_product_queries(product)
        return []
    return _split_product_queries(text)


def _is_continue_shopping_intent(message: str, phase: str = "idle") -> bool:
    if phase not in ("awaiting_confirmation", "awaiting_quantity", "awaiting_product_search"):
        return False
    if _is_abandon_add_intent(message):
        return False
    if _is_add_to_cart_intent(message, phase):
        return True
    text = _normalize_continue_shopping_text(message)
    return any(text.startswith(prefix) for prefix in _CONTINUE_SHOPPING_PREFIXES)


def _cart_item_matches_query(item: dict[str, Any], product_query: str) -> bool:
    resolved = _strip_accents(_resolve_product_alias(product_query).lower())
    name = _strip_accents(str(item.get("productName", "")).lower())
    code = str(item.get("productCode", "")).upper()
    if resolved in name or name in resolved:
        return True
    for word in resolved.split():
        if len(word) >= 3 and word in name:
            return True
    if code and resolved.upper() in code:
        return True
    if resolved in _COKE_SLANG_TERMS and code.startswith("PLZ-COC"):
        return True
    if resolved in {"marihuana", "mota", "hierba", "weed"} and code.startswith("PLZ-MJ"):
        return True
    if resolved == "lsd" and code.startswith("PLZ-LSD"):
        return True
    if resolved in {"ketamina", "ketamina liquida", "ketamina líquida"} and code.startswith("PLZ-KET"):
        return True
    return False


def _try_update_cart_quantity(
    session: dict[str, Any],
    message: str,
) -> tuple[str, list[str], dict[str, Any]] | None:
    text = message.strip()
    if text.lower().startswith("y "):
        text = text[2:].strip()

    quantity, measure_unit, product = _extract_purchase_details(text)
    if quantity is None:
        quantity, measure_unit = _extract_quantity_with_unit(text)
        if quantity is None:
            return None
        match = re.search(r"\bde\s+(.+?)(?:\.|$)", _normalize_message(text))
        product = _clean_product_query(match.group(1)) if match else None

    if quantity is None or not product:
        return None

    cart = session.get("cart") or []
    if not cart:
        return None

    target = next((item for item in cart if _cart_item_matches_query(item, product)), None)
    if target is None:
        return None

    sale_unit = target.get("measureUnit", "unit")
    try:
        normalized_qty, resolved_unit = resolve_sale_quantity(
            quantity,
            measure_unit,
            sale_unit,
        )
    except ValueError as exc:
        return str(exc), ["Modificar cantidad", "Cancelar"], None

    target["quantity"] = normalized_qty
    target["measureUnit"] = resolved_unit
    target["subtotal"] = round(float(target["unitPrice"]) * normalized_qty, 2)
    session["cart"] = cart
    return _enter_awaiting_confirmation(session)


def _build_cart_line(session: dict[str, Any]) -> dict[str, Any]:
    subtotal = round(session["unit_price"] * session["quantity"], 2)
    return {
        "productCode": session["product_code"],
        "productName": session["product_name"],
        "quantity": session["quantity"],
        "measureUnit": session.get("measure_unit", "unit"),
        "unitPrice": session["unit_price"],
        "subtotal": subtotal,
    }


def _sync_cart_line(session: dict[str, Any]) -> None:
    if not session.get("product_code"):
        return
    line = _build_cart_line(session)
    cart = [
        item
        for item in (session.get("cart") or [])
        if item.get("productCode") != line["productCode"]
    ]
    cart.append(line)
    session["cart"] = cart


def _confirmation_chips() -> list[str]:
    return ["Agregar otro producto", "Confirmar compra", "Cancelar"]


_DELIVERY_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:mi\s+)?direcci[oó]n(?:\s+de\s+entrega)?\s*(?:es\s+)?|"
    r"vivo\s+en\s+|"
    r"entregar(?:lo|la|los|las)?\s+en\s+|"
    r"env[ií]o\s+a\s+"
    r")",
    re.IGNORECASE,
)

_DELIVERY_CITY_SEPARATORS = re.compile(r"\s*[,;]\s*")

_COLOMBIAN_CITIES: dict[str, str] = {
    "bogota": "Bogotá",
    "bogota dc": "Bogotá",
    "bogota d c": "Bogotá",
    "medellin": "Medellín",
    "cali": "Cali",
    "barranquilla": "Barranquilla",
    "cartagena": "Cartagena",
    "cartagena de indias": "Cartagena",
    "cucuta": "Cúcuta",
    "bucaramanga": "Bucaramanga",
    "pereira": "Pereira",
    "santa marta": "Santa Marta",
    "ibague": "Ibagué",
    "manizales": "Manizales",
    "pasto": "Pasto",
    "neiva": "Neiva",
    "villavicencio": "Villavicencio",
    "armenia": "Armenia",
    "monteria": "Montería",
    "valledupar": "Valledupar",
    "popayan": "Popayán",
    "sincelejo": "Sincelejo",
    "tunja": "Tunja",
    "riohacha": "Riohacha",
    "florencia": "Florencia",
    "quibdo": "Quibdó",
    "arauca": "Arauca",
    "yopal": "Yopal",
    "mocoa": "Mocoa",
    "san andres": "San Andrés",
    "san jose del guaviare": "San José del Guaviare",
    "leticia": "Leticia",
}

_COLOMBIAN_CITY_KEYS = sorted(_COLOMBIAN_CITIES.keys(), key=len, reverse=True)


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _normalize_city_key(text: str) -> str:
    return _strip_accents(text.lower().strip())


def _match_city_at_end(text: str) -> tuple[str, str] | None:
    normalized = _normalize_city_key(text)
    for city_key in _COLOMBIAN_CITY_KEYS:
        if normalized == city_key:
            return "", _COLOMBIAN_CITIES[city_key]
        if normalized.endswith(f" {city_key}"):
            address = text[: len(text) - len(city_key)].strip(" .,-")
            if len(address) >= 5:
                return address, _COLOMBIAN_CITIES[city_key]
    return None


def _parse_delivery_address(message: str) -> tuple[str | None, str | None]:
    """Parse Spanish delivery replies like «vivo en calle X, Bogotá»."""
    text = message.strip()
    if not text:
        return None, None

    text = _DELIVERY_PREFIX_RE.sub("", text).strip(" .")
    if not text:
        return None, None

    if _DELIVERY_CITY_SEPARATORS.search(text):
        address, city = _DELIVERY_CITY_SEPARATORS.split(text, maxsplit=1)
        address = address.strip(" .")
        city = city.strip(" .")
        if len(address) >= 5 and len(city) >= 2:
            city_match = _match_city_at_end(city)
            return address, city_match[1] if city_match else city

    en_match = re.search(r"\ben\b", text, re.IGNORECASE)
    if en_match:
        address = text[: en_match.start()].strip(" .")
        city = text[en_match.end() :].strip(" .")
        if len(address) >= 5 and len(city) >= 2:
            city_match = _match_city_at_end(city)
            return address, city_match[1] if city_match else city

    city_match = _match_city_at_end(text)
    if city_match:
        address, city = city_match
        if address and len(address) >= 5:
            return address, city

    return None, None


_YES_PHRASES = frozenset(
    {
        "si",
        "sí",
        "sip",
        "claro",
        "dale",
        "ok",
        "okay",
        "por supuesto",
        "de acuerdo",
        "afirmativo",
        "usa esa direccion",
        "usa esa dirección",
        "usar direccion guardada",
        "usar dirección guardada",
    }
)

_NO_PHRASES = frozenset(
    {
        "no",
        "nop",
        "no gracias",
        "mejor no",
        "no quiero",
        "otra direccion",
        "otra dirección",
    }
)


def _is_yes(message: str) -> bool:
    text = _normalize_message(message).rstrip(".,!?").lstrip("¿").strip()
    if text in _YES_PHRASES:
        return True
    return text.startswith(("si ", "sí ", "si,", "sí,"))


def _is_no(message: str) -> bool:
    text = _normalize_message(message).rstrip(".,!?").lstrip("¿").strip()
    if text in _NO_PHRASES:
        return True
    return text.startswith(("no ", "no,", "no quiero"))


def _save_address_prompt() -> tuple[str, list[str]]:
    return (
        "¿Quieres **guardar esta dirección** para próximos pedidos?",
        ["Sí", "No"],
    )


def _enter_awaiting_save_address(session: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    session["phase"] = "awaiting_save_address"
    summary = session.get("operation_summary") or _build_summary(session)
    summary["status"] = "Pendiente de confirmar guardado de dirección"
    session["operation_summary"] = summary
    response, chips = _save_address_prompt()
    return response, chips, summary


def _delivery_address_prompt() -> tuple[str, list[str]]:
    return (
        "Para finalizar tu pedido, indícame la **dirección de entrega** y la **ciudad**. "
        "Por ejemplo: `Calle 45 #12-30, Bogotá` o `vivo en Carrera 7 con 80, Medellín`.",
        ["Cancelar"],
    )


def _enter_awaiting_delivery_address(session: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    session["phase"] = "awaiting_delivery_address"
    summary = session.get("operation_summary") or _build_summary(session)
    summary["status"] = "Pendiente de dirección de entrega"
    session["operation_summary"] = summary
    response, chips = _delivery_address_prompt()
    return response, chips, summary


def _enter_awaiting_use_saved_address(
    session: dict[str, Any],
    address: str,
    city: str,
) -> tuple[str, list[str], dict[str, Any]]:
    session["phase"] = "awaiting_use_saved_address"
    session["saved_delivery_address"] = address
    session["saved_delivery_city"] = city
    summary = session.get("operation_summary") or _build_summary(session)
    summary["status"] = "Pendiente de confirmar dirección guardada"
    session["operation_summary"] = summary
    response = (
        f"¿Usar dirección guardada: **{address}, {city}**?"
    )
    return response, ["Sí", "No"], summary


async def _start_delivery_address_flow(
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    saved = await dotnet_tools.get_customer_saved_delivery_address(
        session.get("customer_email", "")
    )
    if saved and saved.get("deliveryAddress") and saved.get("deliveryCity"):
        return _enter_awaiting_use_saved_address(
            session,
            str(saved["deliveryAddress"]),
            str(saved["deliveryCity"]),
        )
    return _enter_awaiting_delivery_address(session)


async def _complete_sale_from_session(
    session: dict[str, Any],
    state: GraphState,
    *,
    save_delivery_address: bool = False,
) -> tuple[str, list[str], dict[str, Any] | None, str]:
    """Create the sale and return response, chips, summary, invoice_number."""
    try:
        result = await dotnet_tools.create_sale(
            session["customer_name"],
            session["customer_email"],
            _cart_sale_line_items(session),
            state["session_id"],
            session.get("delivery_address"),
            session.get("delivery_city"),
            save_delivery_address=save_delivery_address,
        )
    except Exception:
        logger.exception(
            "create_sale failed for session %s cart %s",
            state["session_id"],
            session.get("cart"),
        )
        phase = session.get("phase", "")
        if phase == "awaiting_save_address":
            retry_chips = _save_address_prompt()[1]
        elif phase in {"awaiting_delivery_address", "awaiting_use_saved_address"}:
            retry_chips = _delivery_address_prompt()[1]
        else:
            retry_chips = _confirmation_chips()
        return (
            "No pude completar la compra en este momento. "
            "Inténtalo de nuevo o escribe **Cancelar** para anular.",
            retry_chips,
            session.get("operation_summary") or None,
            session.get("invoice_number", ""),
        )

    invoice_number = result.get("invoiceNumber") or result.get("invoice_number", "")
    session["invoice_number"] = invoice_number
    session["phase"] = "sale_completed"
    summary = _build_summary(session)
    summary["status"] = "Completada"
    session["operation_summary"] = summary
    item_count = len(session.get("cart") or [])
    if item_count > 1:
        response = (
            f"¡Compra confirmada! Pedido **{result.get('orderNumber', '')}** con "
            f"**{item_count} productos** — factura **{invoice_number}**. "
            "El inventario ya fue actualizado en El Plonsazo."
        )
    else:
        response = (
            f"¡Compra confirmada! Pedido **{result.get('orderNumber', '')}** — "
            f"factura **{invoice_number}**. El inventario ya fue actualizado en El Plonsazo."
        )
    return response, ["Nueva consulta"], summary, invoice_number


def _format_cart_summary_text(summary: dict[str, Any]) -> str:
    line_items = summary.get("lineItems") or []
    if len(line_items) <= 1:
        item = line_items[0] if line_items else summary
        qty_label = _format_quantity_label(
            float(item.get("quantity", summary.get("quantity", 0))),
            item.get("measureUnit", summary.get("measureUnit", "unit")),
        )
        product_name = item.get("productName", summary.get("productName", ""))
        return (
            f"Resumen: **{qty_label} de {product_name}** — "
            f"subtotal ${summary['subtotal']:,.0f} COP + IVA ${summary['tax']:,.0f} COP = "
            f"**${summary['total']:,.0f} COP**. ¿Confirmas la compra?"
        )

    lines: list[str] = []
    for item in line_items:
        qty_label = _format_quantity_label(float(item["quantity"]), item["measureUnit"])
        lines.append(
            f"- **{qty_label} de {item['productName']}** — ${item['subtotal']:,.0f} COP"
        )
    items_text = "\n".join(lines)
    return (
        f"Resumen del carrito:\n{items_text}\n\n"
        f"Subtotal ${summary['subtotal']:,.0f} COP + IVA ${summary['tax']:,.0f} COP = "
        f"**${summary['total']:,.0f} COP**. ¿Confirmas la compra?"
    )


def _cart_sale_line_items(session: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "product_code": item["productCode"],
            "quantity": item["quantity"],
            "measure_unit": item.get("measureUnit"),
        }
        for item in (session.get("cart") or [])
    ]


def _build_summary(session: dict[str, Any]) -> dict[str, Any]:
    cart = list(session.get("cart") or [])
    if not cart and session.get("product_code"):
        cart = [_build_cart_line(session)]
    subtotal = round(sum(float(item["subtotal"]) for item in cart), 2)
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    first = cart[0] if cart else {}
    codes = "-".join(item["productCode"] for item in cart[:3])
    return {
        "transactionId": f"TXN-{codes}-{len(cart)}",
        "status": "Pendiente de confirmación",
        "lineItems": cart,
        "productCode": first.get("productCode", session.get("product_code", "")),
        "productName": first.get("productName", session.get("product_name", "")),
        "quantity": first.get("quantity", session.get("quantity", 0)),
        "measureUnit": first.get("measureUnit", session.get("measure_unit", "unit")),
        "unitPrice": first.get("unitPrice", session.get("unit_price", 0)),
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


def _enter_awaiting_confirmation(
    session: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    _sync_cart_line(session)
    session["phase"] = "awaiting_confirmation"
    session["awaiting_quantity"] = False
    session["adding_to_cart"] = False
    _clear_cart_snapshot(session)
    _clear_pending_add_queue(session)
    _clear_pending_order_queue(session)
    summary = _build_summary(session)
    session["operation_summary"] = summary
    return _format_cart_summary_text(summary), _confirmation_chips(), summary


async def process_node(state: GraphState) -> GraphState:
    session = _session(state["session_id"])
    message = state["message"].strip()
    phase = session["phase"]

    response = ""
    chips: list[str] = []
    offers: list[dict[str, Any]] = []
    offers_total_count = 0
    invoice_number = session.get("invoice_number", "")
    operation_summary = session.get("operation_summary") or None
    intent = _normalize_intent(message)
    session["last_intent"] = intent or ""

    if _is_cancel(message, phase):
        # Guard: once we handle cancel, we must return immediately and
        # not continue any phase logic (prevents "looping" states).
        was_in_flow = phase in FLOW_PHASES
        session["last_intent"] = "cancelar"
        if was_in_flow and session.get("adding_to_cart") and (
            session.get("saved_cart") is not None or session.get("cart")
        ):
            abandoned = _abandon_add_flow(session)
            if abandoned is not None:
                response, chips, summary = abandoned
                return {
                    **state,
                    "phase": session["phase"],
                    "response": response,
                    "chips": chips,
                    "invoice_number": "",
                    "operation_summary": summary,
                    "offers": [],
                    "offers_total_count": 0,
                }
        _reset_flow(session)
        response, chips = _cancel_ack() if was_in_flow else _idle_welcome()
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": {},
            "offers": [],
            "offers_total_count": 0,
        }

    if _is_greeting(message):
        if phase in FLOW_PHASES:
            _reset_flow(session)
        response, chips = _idle_welcome()
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": {},
            "offers": [],
            "offers_total_count": 0,
        }

    # Normalize chip/menu intents BEFORE any phase logic or product search
    if intent == "cargar_mas_catalogo" and _is_menu_intent(message):
        load_all = _normalize_message(message) in (
            "ver todo el catálogo",
            "ver todo el catalogo",
        )
        response, chips, offers, offers_total_count = await _handle_ver_ofertas(
            session,
            load_more=not load_all,
            load_all=load_all,
        )
        session["phase"] = "idle"
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": invoice_number,
            "operation_summary": operation_summary or {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if intent in ("ayuda", "consultar_stock", "buscar_producto", "ver_ofertas", "ver_factura") and _is_menu_intent(message):
        response, chips, offers, offers_total_count = await _handle_menu_intent(intent, session)
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": invoice_number,
            "operation_summary": operation_summary or {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if _is_stock_query(message) and not _is_menu_intent(message):
        if phase in FLOW_PHASES:
            _clear_purchase_flow(session)
        response, chips, found = await _resolve_stock_lookup(message)
        session["phase"] = "idle" if found else "awaiting_stock_sku"
        session["awaiting_stock_sku"] = not found
        session["awaiting_product_search"] = False
        session["awaiting_quantity"] = False
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if _is_price_inquiry(message) and not _is_menu_intent(message):
        if phase in FLOW_PHASES:
            _clear_purchase_flow(session)
        response, chips = await _resolve_price_inquiry(message)
        session["phase"] = "idle"
        session["awaiting_stock_sku"] = False
        session["awaiting_product_search"] = False
        session["awaiting_quantity"] = False
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if _is_multi_item_order(message):
        if phase in FLOW_PHASES:
            _reset_flow(session)
        response, chips, purchase_summary = await _resolve_multi_item_order(message, session)
        if purchase_summary is not None:
            operation_summary = purchase_summary
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": operation_summary or session.get("operation_summary") or {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if (
        phase in ("awaiting_quantity", "awaiting_confirmation")
        and _is_purchase_intent(message)
        and not is_quantity_reply(message)
        and not _is_add_to_cart_intent(message, phase)
    ):
        if _is_abandon_add_intent(message):
            abandoned = _abandon_add_flow(session)
            if abandoned is not None:
                response, chips, summary = abandoned
                return {
                    **state,
                    "phase": session["phase"],
                    "response": response,
                    "chips": chips,
                    "invoice_number": "",
                    "operation_summary": summary,
                    "offers": offers,
                    "offers_total_count": offers_total_count,
                }

        if phase == "awaiting_confirmation" and session.get("cart"):
            cart_update = _try_update_cart_quantity(session, message)
            if cart_update is not None:
                response, chips, summary = cart_update
                return {
                    **state,
                    "phase": session["phase"],
                    "response": response,
                    "chips": chips,
                    "invoice_number": "",
                    "operation_summary": summary,
                    "offers": offers,
                    "offers_total_count": offers_total_count,
                }
            if _is_continue_shopping_intent(message, phase):
                response, chips, purchase_summary = await _resolve_add_to_cart_product(
                    message, session
                )
                if purchase_summary is not None:
                    operation_summary = purchase_summary
                return {
                    **state,
                    "phase": session["phase"],
                    "response": response,
                    "chips": chips,
                    "invoice_number": "",
                    "operation_summary": operation_summary or session.get("operation_summary") or {},
                    "offers": offers,
                    "offers_total_count": offers_total_count,
                }

        if (
            phase == "awaiting_quantity"
            and session.get("adding_to_cart")
            and (session.get("saved_cart") is not None or session.get("cart"))
        ):
            if _is_abandon_add_intent(message):
                abandoned = _abandon_add_flow(session)
                if abandoned is not None:
                    response, chips, summary = abandoned
                    return {
                        **state,
                        "phase": session["phase"],
                        "response": response,
                        "chips": chips,
                        "invoice_number": "",
                        "operation_summary": summary,
                        "offers": offers,
                        "offers_total_count": offers_total_count,
                    }
            if _is_continue_shopping_intent(message, phase):
                response, chips, _ = await _resolve_add_to_cart_product(message, session)
                return {
                    **state,
                    "phase": session["phase"],
                    "response": response,
                    "chips": chips,
                    "invoice_number": "",
                    "operation_summary": session.get("operation_summary") or {},
                    "offers": offers,
                    "offers_total_count": offers_total_count,
                }

        _clear_purchase_flow(session)
        session["phase"] = "awaiting_product_search"
        session["awaiting_product_search"] = True
        response, chips, purchase_summary = await _resolve_product_search(message, session)
        if purchase_summary is not None:
            operation_summary = purchase_summary
        if session["phase"] not in ("awaiting_quantity", "awaiting_product_search"):
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
        return {
            **state,
            "phase": session["phase"],
            "response": response,
            "chips": chips,
            "invoice_number": "",
            "operation_summary": session.get("operation_summary") or {},
            "offers": offers,
            "offers_total_count": offers_total_count,
        }

    if phase == "idle":
        if intent == "ayuda":
            response = COMMUNICATION_GUIDE
            chips = MENU_CHIPS
        elif intent == "consultar_stock":
            session["phase"] = "awaiting_stock_sku"
            session["awaiting_stock_sku"] = True
            session["awaiting_quantity"] = False
            response = (
                "Para consultar stock, indícame el **nombre** o **SKU** del producto "
                "(por ejemplo: marihuana, cocaina, `PLZ-MJ-001`)."
            )
            chips = ["PLZ-MJ-001", "Buscar producto", "Cancelar"]
        elif intent == "buscar_producto":
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            session["awaiting_stock_sku"] = False
            session["awaiting_quantity"] = False
            response = (
                "¿Qué producto buscas? Escribe el nombre o el SKU "
                "(por ejemplo: marihuana, lsd, PLZ-MJ-001)."
            )
            chips = ["Ver catálogo", "Consultar stock", "Cancelar"]
        elif intent == "ver_ofertas":
            response, chips, offers, offers_total_count = await _handle_ver_ofertas(session)
            session["phase"] = "idle"
        elif _is_vague_purchase_intent(message):
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            session["awaiting_stock_sku"] = False
            session["awaiting_quantity"] = False
            response, chips = _purchase_prompt()
        else:
            if _is_purchase_intent(message) or _extract_code(message) or _looks_like_product_search(message):
                response, chips, purchase_summary = await _resolve_idle_product_purchase(
                    message, session
                )
                if purchase_summary is not None:
                    operation_summary = purchase_summary
            else:
                response, chips = _idle_welcome()
                session["phase"] = "idle"

    elif phase == "awaiting_stock_sku":
        if intent == "buscar_producto":
            session["phase"] = "awaiting_product_search"
            session["awaiting_stock_sku"] = False
            session["awaiting_product_search"] = True
            response = "¿Qué producto buscas? Escribe el nombre o el SKU."
            chips = ["Ver catálogo", "Cancelar"]
        elif intent == "ver_ofertas":
            session["phase"] = "idle"
            response, chips, offers, offers_total_count = await _handle_ver_ofertas(session)
        else:
            response, chips, found = await _resolve_stock_lookup(message)
            if found:
                session["phase"] = "idle"
                session["awaiting_stock_sku"] = False

    elif phase == "awaiting_product_search":
        if (
            _is_abandon_add_intent(message)
            and (session.get("saved_cart") is not None or session.get("cart"))
        ):
            abandoned = _abandon_add_flow(session)
            if abandoned is not None:
                response, chips, summary = abandoned
                operation_summary = summary
        elif intent == "consultar_stock":
            session["phase"] = "awaiting_stock_sku"
            session["awaiting_stock_sku"] = True
            session["awaiting_quantity"] = False
            session["awaiting_product_search"] = False
            response = (
                "Para consultar stock, indícame el **nombre** o **SKU** del producto "
                "(por ejemplo: marihuana, cocaina, `PLZ-MJ-001`)."
            )
            chips = ["PLZ-MJ-001", "Ver catálogo", "Cancelar"]
        elif intent == "ver_ofertas":
            session["phase"] = "idle"
            response, chips, offers, offers_total_count = await _handle_ver_ofertas(session)
        elif _is_intent_phrase(message):
            response = "Escribe el nombre o SKU del producto que buscas."
            chips = ["Ver catálogo", "Consultar stock", "Cancelar"]
        else:
            response, chips, purchase_summary = await _resolve_product_search(message, session)
            if purchase_summary is not None:
                operation_summary = purchase_summary

    elif phase == "awaiting_quantity":
        if (
            _is_abandon_add_intent(message)
            and (session.get("saved_cart") is not None or session.get("cart"))
        ):
            abandoned = _abandon_add_flow(session)
            if abandoned is not None:
                response, chips, summary = abandoned
                operation_summary = summary
        elif intent == "buscar_producto" or "buscar otro producto" in message.lower():
            session["phase"] = "awaiting_product_search"
            session["awaiting_product_search"] = True
            session["awaiting_quantity"] = False
            session["awaiting_stock_sku"] = False
            response = "¿Qué otro producto buscas? Escribe el nombre o el SKU."
            chips = ["Ver catálogo", "Consultar stock", "Cancelar"]
        elif _is_unit_content_inquiry(message):
            response, chips = _unit_content_inquiry_response(session, phase)
        else:
            quantity, measure_unit = _extract_quantity_with_unit(message)
            if quantity is None:
                sale_unit = session.get("measure_unit", "unit")
                stock = float(session.get("stock", 0))
                example_qty = _quantity_chip_values(stock)[0]
                example_label = _format_quantity_label(example_qty, sale_unit)
                response = (
                    f"Indica la cantidad en números "
                    f"(por ejemplo: {example_label})."
                )
                chips = _quantity_chips(stock, sale_unit) + ["Cancelar"]
            else:
                try:
                    normalized_qty, resolved_unit = resolve_sale_quantity(
                        quantity,
                        measure_unit,
                        session.get("measure_unit", "unit"),
                    )
                except ValueError as exc:
                    response = str(exc)
                    chips = ["Modificar cantidad", "Cancelar"]
                else:
                    if normalized_qty > session["stock"]:
                        stock_label = _format_quantity_label(
                            float(session["stock"]),
                            session.get("measure_unit", "unit"),
                        )
                        response = (
                            f"Solo hay **{stock_label}** de {session['product_name']}. "
                            "Ajusta la cantidad o elige otro producto."
                        )
                        chips = [
                            f"{session['stock']:g} {unit_label(session.get('measure_unit', 'unit'), plural=True)}",
                            "Buscar otro producto",
                            "Cancelar",
                        ]
                    else:
                        session["quantity"] = normalized_qty
                        session["measure_unit"] = resolved_unit
                        response, chips, summary = _enter_awaiting_confirmation(session)
                        operation_summary = summary

    elif phase == "awaiting_confirmation":
        if _is_add_to_cart_intent(message, phase):
            response, chips, purchase_summary = await _resolve_add_to_cart_product(
                message, session
            )
            if purchase_summary is not None:
                operation_summary = purchase_summary
        elif _is_abandon_add_intent(message) and (
            session.get("saved_cart") is not None or session.get("cart")
        ):
            abandoned = _abandon_add_flow(session)
            if abandoned is not None:
                response, chips, summary = abandoned
                operation_summary = summary
        elif _extract_code(message) and session.get("saved_cart") is not None:
            response, chips, purchase_summary = await _resolve_add_to_cart_product(
                message, session
            )
            if purchase_summary is not None:
                operation_summary = purchase_summary
        elif _is_unit_content_inquiry(message):
            response, chips = _unit_content_inquiry_response(session, phase)
            operation_summary = session.get("operation_summary") or None
        elif "modificar" in message.lower() or "cantidad" in message.lower():
            session["phase"] = "awaiting_quantity"
            session["awaiting_quantity"] = True
            session["awaiting_product_search"] = False
            session["operation_summary"] = {}
            operation_summary = None
            response = (
                f"De acuerdo. ¿Cuántos {unit_label(session.get('measure_unit', 'unit'), plural=True)} "
                f"de {session['product_name']} deseas?"
            )
            chips = _quantity_chips(
                float(session.get("stock", 0)),
                session.get("measure_unit", "unit"),
            ) + ["Cancelar"]
        elif _is_confirm(message):
            response, chips, summary = await _start_delivery_address_flow(session)
            operation_summary = summary
        else:
            cart_update = _try_update_cart_quantity(session, message)
            if cart_update is not None:
                response, chips, summary = cart_update
                operation_summary = summary
            elif _is_continue_shopping_intent(message, phase):
                response, chips, purchase_summary = await _resolve_add_to_cart_product(
                    message, session
                )
                if purchase_summary is not None:
                    operation_summary = purchase_summary
            else:
                response = (
                    "Responde **Confirmar compra** para finalizar, "
                    "**Agregar otro producto** para sumar al carrito, o **Cancelar** para anular."
                )
                chips = _confirmation_chips()
                operation_summary = session.get("operation_summary") or None

    elif phase == "awaiting_use_saved_address":
        if _is_yes(message):
            session["delivery_address"] = session.get("saved_delivery_address", "")
            session["delivery_city"] = session.get("saved_delivery_city", "")
            response, chips, operation_summary, invoice_number = await _complete_sale_from_session(
                session, state
            )
        elif _is_no(message):
            response, chips, summary = _enter_awaiting_delivery_address(session)
            operation_summary = summary
        else:
            address, city = _parse_delivery_address(message)
            if address and city:
                session["delivery_address"] = address
                session["delivery_city"] = city
                response, chips, summary = _enter_awaiting_save_address(session)
                operation_summary = summary
            else:
                saved_address = session.get("saved_delivery_address", "")
                saved_city = session.get("saved_delivery_city", "")
                response = (
                    f"Responde **Sí** para usar **{saved_address}, {saved_city}**, "
                    "**No** para ingresar otra dirección, o escribe la nueva dirección."
                )
                chips = ["Sí", "No", "Cancelar"]
                operation_summary = session.get("operation_summary") or None

    elif phase == "awaiting_delivery_address":
        address, city = _parse_delivery_address(message)
        if address and city:
            session["delivery_address"] = address
            session["delivery_city"] = city
            response, chips, summary = _enter_awaiting_save_address(session)
            operation_summary = summary
        else:
            response = (
                "Necesito la **dirección** y la **ciudad** de entrega. "
                "Ejemplo: `Calle 10 #20-30, Bogotá` o `carrera 2da este 87 a 63 sur bogota`."
            )
            chips = ["Cancelar"]
            operation_summary = session.get("operation_summary") or None

    elif phase == "awaiting_save_address":
        if _is_yes(message):
            response, chips, operation_summary, invoice_number = await _complete_sale_from_session(
                session, state, save_delivery_address=True
            )
        elif _is_no(message):
            response, chips, operation_summary, invoice_number = await _complete_sale_from_session(
                session, state, save_delivery_address=False
            )
        else:
            response = (
                "¿Quieres guardar esta dirección para próximos pedidos? "
                "Responde **Sí** o **No**."
            )
            chips = ["Sí", "No", "Cancelar"]
            operation_summary = session.get("operation_summary") or None

    elif phase == "sale_completed":
        session["phase"] = "idle"
        session["operation_summary"] = {}
        operation_summary = None
        invoice_number = ""
        if intent == "buscar_producto" or intent == "consultar_stock" or intent == "ver_ofertas":
            if intent == "consultar_stock":
                session["phase"] = "awaiting_stock_sku"
                session["awaiting_stock_sku"] = True
                session["awaiting_quantity"] = False
                response = (
                    "Para consultar stock, indícame el **nombre** o **SKU** del producto "
                    "(por ejemplo: marihuana, cocaina, `PLZ-MJ-001`)."
                )
                chips = ["PLZ-MJ-001", "Buscar producto", "Cancelar"]
            elif intent == "buscar_producto":
                session["phase"] = "awaiting_product_search"
                session["awaiting_product_search"] = True
                session["awaiting_stock_sku"] = False
                session["awaiting_quantity"] = False
                response = "¿Qué producto buscas? Escribe el nombre o el SKU."
                chips = ["Ver catálogo", "Consultar stock", "Cancelar"]
            else:
                response, chips, offers, offers_total_count = await _handle_ver_ofertas(session)
        elif _is_intent_phrase(message):
            _reset_flow(session)
            response, chips = _idle_welcome()
        else:
            product = await _lookup_product(message)
            if product:
                fields = _product_fields(product)
                _start_awaiting_quantity(session, product)
                stock_label = _format_quantity_label(fields["stock"], fields["saleUnit"])
                response = (
                    f"Encontré **{fields['name']}** ({fields['code']}). "
                    f"Hay **{stock_label}** disponibles. "
                    f"¿Cuántos {unit_label(fields['saleUnit'], plural=True)} necesitas?"
                )
                chips = _quantity_chips(fields["stock"], fields["saleUnit"]) + ["Cancelar"]
            else:
                _reset_flow(session)
                response, chips = _idle_welcome()

    else:
        session["phase"] = "idle"
        response = "Reinicié la conversación. ¿Qué producto necesitas?"

    return {
        **state,
        "phase": session["phase"],
        "response": response,
        "chips": chips,
        "invoice_number": invoice_number,
        "operation_summary": operation_summary or {},
        "offers": offers,
        "offers_total_count": offers_total_count,
    }


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("process", process_node)
    graph.add_edge(START, "process")
    graph.add_edge("process", END)
    return graph.compile()


GRAPH = build_graph()

def _append_chat_history(session: dict[str, Any], user_message: str, assistant_message: str) -> None:
    history = list(session.get("chat_history") or [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_message})
    session["chat_history"] = history[-20:]


def _normalize_user_message(message: str) -> str:
    """Convert legacy slash-prefixed input to natural-language intents."""
    text = message.strip()
    if not text.startswith("/"):
        return text
    body = text[1:].strip()
    if not body:
        return "ayuda"
    parts = body.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    if command in ("ayuda", "help"):
        return "ayuda"
    if command == "cancelar":
        return "cancelar"
    if command in ("stock", "consultar"):
        return f"consultar stock de {args}".strip() if args else "consultar stock"
    if command == "buscar":
        return args if args else "buscar producto"
    if command == "ofertas":
        return "ver ofertas"
    if command == "comprar":
        return f"quiero comprar {args}".strip() if args else "quiero comprar algo"
    return body


def _rules_response_from_state(
    session: dict[str, Any],
    message: str,
    result: dict[str, Any],
) -> ChatMessageResponse:
    summary_data = result.get("operation_summary") or {}
    summary = OperationSummary(**summary_data) if summary_data else None
    offers_data = result.get("offers") or []
    offers = [ProductOffer(**item) for item in offers_data] if offers_data else None
    offers_total_count = result.get("offers_total_count") or None
    session["phase"] = result.get("phase", session.get("phase", "idle"))
    _append_chat_history(session, message, result["response"])
    return ChatMessageResponse(
        response=result["response"],
        state=session["phase"],
        state_json=_export_session_state(session),
        invoice_number=result.get("invoice_number") or None,
        chips=result.get("chips") or None,
        operation_summary=summary,
        offers=offers,
        offers_total_count=offers_total_count if offers else None,
    )


async def _invoke_rules_graph(
    session_id: str,
    message: str,
    session: dict[str, Any],
) -> ChatMessageResponse:
    try:
        result = await GRAPH.ainvoke(
            {
                "session_id": session_id,
                "message": message,
                "phase": session["phase"],
                "product_code": session.get("product_code", ""),
                "product_name": session.get("product_name", ""),
                "unit_price": session.get("unit_price", 0.0),
                "stock": session.get("stock", 0),
                "quantity": session.get("quantity", 0),
                "customer_name": session.get("customer_name", ""),
                "customer_email": session.get("customer_email", ""),
                "response": "",
                "chips": [],
                "invoice_number": session.get("invoice_number", ""),
                "operation_summary": session.get("operation_summary", {}),
                "offers": [],
                "offers_total_count": 0,
            }
        )
    except dotnet_tools.CatalogError as exc:
        return ChatMessageResponse(
            response=str(exc),
            state=session.get("phase", "idle"),
            state_json=_export_session_state(session),
            invoice_number=None,
            chips=["Consultar stock", "Buscar producto", "Cancelar"],
            operation_summary=None,
            offers=None,
            offers_total_count=None,
        )
    return _rules_response_from_state(session, message, result)


async def run_chat(
    session_id: str,
    message: str,
    state: dict[str, Any] | None = None,
    customer_name: str | None = None,
    customer_email: str | None = None,
) -> ChatMessageResponse:
    session = _hydrate_session(session_id, state, customer_name, customer_email)

    if session.get("phase") == "sale_completed" and _is_greeting(message):
        _reset_flow(session)
        response, chips = _idle_welcome()
        _append_chat_history(session, message, response)
        return ChatMessageResponse(
            response=response,
            state=session.get("phase", "idle"),
            state_json=_export_session_state(session),
            invoice_number=None,
            chips=chips,
            operation_summary=None,
            offers=None,
            offers_total_count=None,
        )

    normalized = _normalize_user_message(message)
    return await _invoke_rules_graph(session_id, normalized, session)
