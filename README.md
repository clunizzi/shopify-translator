# Shopify Translator

Strumento Python per tradurre i contenuti Shopify (prodotti, HTML Liquid, metafield JSON, SEO) in modo ripetibile. Funziona sia offline su CSV sia in sincronizzazione continua via webhook AWS.

## Perché usarlo
- Pipeline CSV → CSV che conserva la struttura Shopify e traduce solo ciò che serve.
- Sincronizzazione live con Shopify tramite Receiver + Worker AWS (SQS, Lambda, DynamoDB) con debounce, dedup e snapshot.
- Traduzioni OpenAI ottimizzate per e‑commerce tecnico: protezione Liquid/HTML, gestione metafield JSON, prompt specializzati.
- Cache SQLite e checkpoint per ridurre i costi e riprendere run interrotti.
- Telemetria semplice (token, cache hit/miss, tempo OpenAI) e logging JSONL.

## Struttura principale
- `src/cli.py` – Typer CLI (`shopify-translator`) con comandi `process`, `sync-shopify` e utilità cache.
- `src/pipeline/` – pipeline CSV (lettura, filtri, traduzione, writer) e checkpoint.
- `src/translate/` – motore di traduzione, cache SQLite, regole DNT.
- `src/shopify/` – GraphQL client, sincronizzazione prodotti/metafield, snapshot DynamoDB.
- `src/aws_lambda/` – funzioni `receiver` (webhook → SQS) e `worker` (SQS → Shopify/OpenAI).
- `infra/terraform/` – infrastruttura AWS (SQS, Lambda, DynamoDB) + esempi tfvars.
- `data/` – CSV di esempio (`to_translate` / `translated`).
- `state/`, `logs/` – checkpoint locali e log JSONL.

## Prerequisiti
- Python 3.11+
- `pip`, `make` (per packaging) e opzionalmente Docker (build Lambda compatibili).
- Account AWS con permessi per SQS, Lambda, DynamoDB, Secrets Manager (per la sync cloud).
- Token Shopify Admin e API Key OpenAI (staging: variabili locali; produzione: Secrets Manager).

## Setup rapido locale
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # compila chiavi OpenAI/Shopify se servono
```
Variabili principali (`.env`):
- `OPENAI_API_KEY`, `OPENAI_MODEL`
- `TARGET_LOCALE` (default fr-FR)
- `TRANSLATOR_SPECIALIZATION`, `SOURCE_LANGUAGE_NAME`
- opzionali: `DO_NOT_TRANSLATE_YAML`, `LOG_PAYLOADS`, `TRANSLATION_CACHE_PATH`

## Traduzione CSV → CSV
1. Esporta da Shopify (Markets → Translations) il CSV sorgente.
2. Esegui:
   ```bash
   shopify-translator process \
     --input data/locales/fr/to_translate/prodotti/agrieden_prodotti.csv \
     --output data/locales/fr/translated/prodotti/agrieden_prodotti_tradotti_fr.csv \
     --target-locales fr-FR \
     --resume --stats
   ```
3. Il comando:
   - filtra per Type/ID (`--types`, `--ids`, `--ids-file`, `--id-range`, `--first-n`)
   - riconosce HTML/JSON/tecnicismi (`--auto-classify/--no-auto-classify`)
   - applica regole DNT (`--dnt path.yaml`)
   - supporta run paralleli con cache (`state/cache.sqlite`), checkpoint e logging JSONL (`--log-file`).
4. Output: CSV con colonna `Translated content` compilata per ogni locale richiesto.

## Sincronizzazione manuale con Shopify
Per testare la pipeline live senza webhook:
```bash
shopify-translator sync-shopify \
  --product-id 1234567890 \
  --target-locales fr-FR,de-DE \
  --mf-include namespace.key1,namespace.key2 \
  --dry-run
```
Opzioni utili: `--create` per simulare `products/create`, `--apply-on-dry-run` per aggiornare solo lo snapshot, `--mf-json-paths` per filtrare i campi JSON.

## Sync automatica (AWS)
1. Prepara `infra/terraform/terraform.tfvars` partendo dall’esempio e imposta:
   - `project`, `aws_region`, `shop_domain`, `source_locale`, `target_locales`
   - ARNs Secrets Manager: `openai_api_key_secret_arn`, `shopify_admin_token_secret_arn`, `shopify_webhook_secret_arn`
   - Opzioni worker: `mf_include`, `mf_json_paths`, `debounce_seconds`, `fill_missing_translations`, `dry_run`
2. Builda i pacchetti Lambda:
   ```bash
   make build-receiver
   make build-worker-docker PY=3.12  # oppure PY=3.11
   ```
3. Deploya con Terraform:
   ```bash
   terraform -chdir=infra/terraform init
   terraform -chdir=infra/terraform apply
   ```
4. Registra in Shopify i webhook `products/create` e `products/update` puntando alla Function URL del receiver.
5. Worker env principali: `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `MF_JSON_PATHS`, `DEBOUNCE_SECONDS`, `DRY_RUN`, `FILL_MISSING_TRANSLATIONS`, `OPENAI_API_KEY_SECRET_ARN`, `SHOPIFY_ADMIN_TOKEN_SECRET_ARN`. Receiver richiede `SQS_URL` e il segreto HMAC (`SHOPIFY_WEBHOOK_SECRET[_ARN]`).
6. Snapshot dei digest su DynamoDB (`product_snapshots`) e dedup eventi (`webhook_dedup`). La cache locale vive in `/tmp/cache.sqlite`.

## Telemetria & logging
- Ogni run produce un JSON di riepilogo (`--stats` o log Lambda) con `openai_calls`, token, `cache_hit/miss` e righe tradotte.
- Log strutturati JSONL in `logs/` o CloudWatch; utili con `jq`.
- `shopify-translator cache purge --vacuum` pulisce la cache locale.

## Test & qualità
```bash
pytest
ruff check src tests
black --check src tests
```
La cartella `tests/` include smoke test pipeline, validatori HTML/JSON e sync dry-run.

## Troubleshooting
- Cache corrotta? cancella `state/cache.sqlite` e `state/checkpoint*`.
- Errore OpenAI in Lambda: guarda i log `openai_import_ok` nel worker.
- Se il pacchetto Lambda supera 70 MB, carica `build/worker.zip` su S3 e passa `worker_s3_bucket/worker_s3_key` a Terraform.
- Pausa veloce della sync: imposta `DISABLE_SYNC=true` (env) su receiver/worker oppure usa `shopify-translator sync-toggle`.

## Licenza
Rimuovi dati sensibili prima di condividere il repository. Imposta qui il testo di licenza desiderato.
