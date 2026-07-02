# El Plonsazo — Chatbot (FastAPI + LangGraph)

Microservicio de chat conversacional para consultas de stock y compras. Orquesta el flujo con **LangGraph** y consulta la API .NET mediante herramientas HTTP.

## Stack

- **FastAPI** — servidor HTTP
- **LangGraph** — máquina de estados del diálogo
- **httpx** — cliente async hacia la API .NET

## Requisitos

- Python **3.11+**
- API .NET en ejecución (`http://localhost:5151`)

## Instalación

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
```

## Variables de entorno

| Variable | Descripción | Por defecto |
|----------|-------------|-------------|
| `DOTNET_API_URL` | URL base de la API .NET | `http://localhost:5151` |
| `OPENAI_API_KEY` | Reservado para extensiones con LLM | vacío |

El grafo actual funciona con reglas y herramientas HTTP; no requiere clave de OpenAI para el flujo del taller.

## Normalización

- **Request/response:** Pydantic serializa con alias camelCase (`sessionId`, `operationSummary`).
- **Tools HTTP:** `app/utils/json_normalize.py` lee claves en camelCase o PascalCase (`items` / `Items`).
- **SKU:** `normalize_product_code()` aplica `strip().upper()` antes de consultar la API.

## Ejecución

Desde la **raíz de `LLMChatBot/`** (no desde `app/`):

```bash
# Opción recomendada
python run.py

# Equivalente
uvicorn app.main:app --reload --port 8000
```

> **Importante:** Tras modificar `app/graph/chat_graph.py` u otro código del servicio, reinicia uvicorn para cargar los cambios. Con `--reload` suele bastar guardar el archivo; si el chatbot sigue con comportamiento antiguo, detén el proceso (Ctrl+C) y vuelve a ejecutar `python run.py`.

También puedes ejecutar `app/main.py` directamente desde el IDE; el archivo ajusta `sys.path` automáticamente.

> Si ves `ModuleNotFoundError: No module named 'app'`, estás en el directorio equivocado. Haz `cd ..` hasta `LLMChatBot/` o usa `python run.py`.

- Health: `GET http://localhost:8000/health`
- Chat: `POST http://localhost:8000/chat/message`

Cuerpo de ejemplo:

```json
{
  "sessionId": "session-abc-123",
  "message": "¿Hay stock de PLZ-MJ-001?"
}
```

El frontend no llama a este servicio directamente: usa el proxy de la API .NET (`POST /api/chat/message`).

## Flujo conversacional

Estados del grafo (`app/graph/chat_graph.py`):

| Fase | Descripción |
|------|-------------|
| `idle` | Espera intención (stock, búsqueda, compra) |
| `awaiting_quantity` | Producto identificado; pide cantidad |
| `awaiting_confirmation` | Muestra resumen y chips de confirmación |
| `sale_completed` | Venta registrada vía API .NET |

Respuesta típica:

```json
{
  "response": "Texto para el usuario",
  "state": "awaiting_confirmation",
  "chips": ["Sí, confirmo", "Cancelar"],
  "invoiceNumber": null,
  "operationSummary": {
    "transactionId": "TXN-PLZ-MJ-001-2",
    "status": "Pendiente de confirmación",
    "productCode": "PLZ-MJ-001",
    "productName": "...",
    "quantity": 2,
    "unitPrice": 45000,
    "subtotal": 90000,
    "tax": 7200,
    "total": 97200
  }
}
```

## Herramientas HTTP (`app/tools/dotnet_tools.py`)

| Función | Endpoint .NET |
|---------|---------------|
| `search_products` | `GET /api/products?q=...` |
| `check_stock` | Búsqueda + lectura de `stock` |
| `create_sale` | `POST /api/sales/from-chatbot` |
| `get_invoice` | `GET /api/invoices` |

## Estructura

```
LLMChatBot/
├── app/
│   ├── main.py              # FastAPI, CORS, rutas
│   ├── schemas.py           # Pydantic request/response
│   ├── graph/
│   │   └── chat_graph.py    # LangGraph + sesiones en memoria
│   └── tools/
│       └── dotnet_tools.py  # Cliente HTTP a la API
├── requirements.txt
├── .env.example
└── .gitignore
```

## Prueba manual

```bash
curl -X POST http://localhost:8000/chat/message \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"test-1","message":"stock PLZ-MJ-001"}'
```

Las sesiones se guardan en memoria (`SESSIONS`); se reinician al reiniciar el proceso.
