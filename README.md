# Shopify Translator

Pipeline per tradurre contenuti **PRODUCT** da CSV Shopify verso una lingua target, con:
- verfica stato prodotto via **Shopify GraphQL** (batch),
- traduzione con **OpenAI**, caching **SQLite**,
- gestione HTML segmentale, similarità, validatori handle/meta,
- checkpoint per `Identification`, resume sicuro.

## Setup (Debian 12, Python 3.11+)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # compila le variabili
