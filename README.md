# El Plonsazo — Chatbot (FastAPI + LangGraph)

Microservicio de chat conversacional para un catálogo de inventario ficticio (proyecto universitario). Resuelve consultas de stock, búsqueda de productos y flujo de compra, orquestando el diálogo con **LangGraph** y consultando una API .NET externa mediante herramientas HTTP.

## 🔗 Repositorios

| Módulo | Enlace |
|--------|--------|
| Frontend | https://github.com/melokaso1/InteligentInventoryFrontend |
| Backend (.NET API) | https://github.com/melokaso1/InteligentInventoryBackend |
| Chatbot (este servicio) | https://github.com/melokaso1/InteligentInventoryLLMChatBot |

---

## 📌 Descripción general

El servicio expone un endpoint de chat que recibe mensajes en lenguaje natural y responde guiando al usuario a través de un flujo determinista de compra (búsqueda → cantidad → confirmación → venta registrada). El lenguaje natural se resuelve principalmente con un **motor de reglas** (`process_node` en `chat_graph.py`); opcionalmente puede apoyarse en OpenAI si se configura una API key, aunque no es requerido para el funcionamiento base.

## 🧱 Stack tecnológico

- **FastAPI** — servidor HTTP y definición de rutas
- **LangGraph** — máquina de estados del diálogo (grafo determinista de conversación)
- **LangChain Core** — utilidades de mensajes/herramientas (integración opcional con OpenAI)
- **httpx** — cliente HTTP asíncrono hacia la API .NET
- **Pydantic** — validación y serialización de esquemas (request/response)
- **pytest** — suite de pruebas automatizadas

## 📂 Estructura del proyecto

```
LLMChatBot/
├── app/
│   ├── main.py               # App FastAPI, CORS, endpoints
│   ├── schemas.py            # Modelos Pydantic (request/response)
│   ├── graph/
│   │   └── chat_graph.py     # Motor de reglas + máquina de estados (LangGraph)
│   ├── llm/
│   │   ├── config.py         # Variables OPENAI_* desde .env
│   │   ├── agent.py          # Agente OpenAI opcional (no usado en el routing principal)
│   │   ├── context.py        # Contexto de sesión para las tools del LLM
│   │   └── tools.py          # Tools: buscar_productos, consultar_stock, listar_ofertas, registrar_venta
│   ├── tools/
│   │   └── dotnet_tools.py   # Cliente HTTP hacia la API .NET (catálogo, ventas, facturas)
│   └── utils/
│       ├── json_normalize.py # Normalización de claves camelCase/PascalCase
│       └── measure_units.py  # Parseo y normalización de unidades de medida y cantidades
├── tests/                    # Suite de pruebas (pytest) — 21 archivos de test
├── scripts/                  # Scripts de utilidad (fix_and_test, patch_slash_routing)
├── requirements.txt
├── run.py                    # Punto de entrada recomendado
├── .env.example
└── README.md
```

## ⚙️ Requisitos

- Python **3.11+**
- API .NET en ejecución (`http://localhost:5151`)
- PostgreSQL en Docker (`cd Backend && docker compose up -d`)

> Entorno previsto: **solo local**. Docker se usa únicamente para PostgreSQL; no hay despliegue en Netlify ni en la nube.

## 🔧 Instalación

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

El repositorio incluye un `.env` con valores de desarrollo listos para usar.

## 🔑 Variables de entorno

| Variable | Descripción | Valor por defecto |
|----------|-------------|--------------------|
| `DOTNET_API_URL` | URL base de la API .NET | `http://localhost:5151` |
| `CHATBOT_API_KEY` | Clave de autenticación hacia endpoints del chatbot en la API .NET | `elplonsazo-chatbot-dev-key` |
| `OPENAI_API_KEY` | Opcional — habilita respuestas conversacionales vía OpenAI | vacío |
| `OPENAI_MODEL` | Modelo OpenAI si la key está configurada | `gpt-4o-mini` |
| `OPENAI_TIMEOUT` | Timeout de inferencia (segundos) | `60` |
| `OPENAI_HISTORY_LIMIT` | Mensajes de historial enviados al agente | `10` |

Sin `OPENAI_API_KEY`, el chatbot funciona íntegramente con el motor de reglas: saludos, búsqueda, stock, ofertas y flujo de compra completo (vía texto libre o chips del menú).

## 🔄 Normalización de datos

- **Request/response:** Pydantic serializa con alias camelCase (`sessionId`, `operationSummary`).
- **Tools HTTP:** `json_normalize.py` lee claves indistintamente en camelCase o PascalCase (`items` / `Items`).
- **SKU:** `normalize_product_code()` aplica `strip().upper()` antes de consultar la API.

## ▶️ Ejecución

Desde la raíz de `LLMChatBot/` (no desde `app/`):

```bash
# Opción recomendada
python run.py

# Equivalente
uvicorn app.main:app --reload --port 8000
```

> **Importante:** tras modificar `chat_graph.py` u otro código del servicio, reinicia uvicorn para cargar los cambios. Con `--reload` suele bastar con guardar el archivo; si el chatbot sigue con comportamiento antiguo, detén el proceso (`Ctrl+C`) y vuelve a ejecutar `python run.py`.

También puedes ejecutar `app/main.py` directamente desde el IDE; el archivo ajusta `sys.path` automáticamente.

> Si ves `ModuleNotFoundError: No module named 'app'`, estás en el directorio equivocado. Haz `cd ..` hasta `LLMChatBot/` o usa `python run.py`.

**Endpoints:**
- Health check: `GET http://localhost:8000/health`
- Mensaje de chat: `POST http://localhost:8000/chat/message`

Cuerpo de ejemplo:

```json
{
  "sessionId": "session-abc-123",
  "message": "¿Hay stock de PLZ-MJ-001?"
}
```

> El frontend no llama directamente a este servicio: usa el proxy de la API .NET (`POST /api/chat/message`).

## 🗣️ Flujo conversacional

Estados del grafo (`chat_graph.py`):

| Fase | Descripción |
|------|-------------|
| `idle` | Espera intención del usuario (stock, búsqueda, compra) |
| `awaiting_quantity` | Producto identificado; se pide la cantidad |
| `awaiting_confirmation` | Muestra resumen de compra y chips de confirmación |
| `sale_completed` | Venta registrada exitosamente vía API .NET |

Respuesta típica del endpoint de chat:

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

## 🛠️ Herramientas HTTP hacia la API .NET

| Función | Endpoint .NET |
|---------|---------------|
| `search_products` | `GET /api/chatbot/products?q=...` |
| `check_stock` | Búsqueda + lectura del campo `stock` |
| `create_sale` | `POST /api/sales/from-chatbot` |
| `get_invoice` | `GET /api/invoices` |
| `get_customer_saved_delivery_address` | `GET /api/chatbot/customers/delivery-address` |

Todas las llamadas al backend se autentican con el header `X-Chatbot-Api-Key`.

## 🧪 Pruebas

El proyecto cuenta con una suite de **21 archivos de test** (`pytest` + `pytest-asyncio`) que cubren, entre otros casos:

- Flujo completo de chat y saludos
- Enrutamiento de intenciones y frases de confirmación
- Búsqueda de productos y catálogo bajo demanda
- Manejo de cantidades y unidades de medida (incluye conversión kg → g)
- Carrito con múltiples ítems y abandono/restauración de carrito
- Contexto de cliente y dirección de entrega
- Codificación de caracteres y slang/alias de productos

Ejecución:

```bash
pytest
```

## 🧾 Prueba manual (cURL)

```bash
curl -X POST http://localhost:8000/chat/message \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"test-1","message":"stock PLZ-MJ-001"}'
```

Las sesiones se guardan en memoria (`SESSIONS`) y se reinician al reiniciar el proceso.

## 📎 Notas de arquitectura

- Este servicio **no tiene persistencia propia**: todo el stock, catálogo y facturación vive en la API .NET (PostgreSQL).
- El componente LLM (OpenAI) es **opcional y desacoplado del routing principal**: sirve únicamente para dar un tono conversacional adicional, no para decidir el flujo de negocio.
- La lógica de negocio crítica (impuestos, cálculo de totales, validación de stock) está centralizada en `chat_graph.py` y replicada de forma consistente en `app/llm/tools.py` para el modo con LLM.