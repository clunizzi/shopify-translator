# Shopify Translator (ITA)

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


# Shopify Translator (ENG)

Python tool to translate Shopify content (products, Liquid HTML, metafield JSON, SEO) in a consistent and repeatable way.
Works both **offline on CSVs** and in **continuous sync mode via AWS webhooks**.

## Why use it

* **CSV → CSV pipeline**: keeps Shopify’s structure and only translates what’s needed.
* **Live sync** with Shopify using Receiver + Worker on AWS (SQS, Lambda, DynamoDB) with debounce, deduplication, and snapshots.
* **OpenAI translations** optimized for technical e-commerce: protects Liquid/HTML, handles JSON metafields, and uses specialized prompts.
* **SQLite cache and checkpoints** to reduce costs and safely resume interrupted runs.
* **Lightweight telemetry** (token usage, cache hit/miss, OpenAI time) and JSONL logging.

## Main structure

* `src/cli.py` – Typer CLI (`shopify-translator`) with commands `process`, `sync-shopify` and cache utilities.
* `src/pipeline/` – CSV pipeline (reader, filters, translator, writer) and checkpoints.
* `src/translate/` – translation engine, SQLite cache, Do-Not-Translate rules.
* `src/shopify/` – GraphQL client, product/metafield sync, DynamoDB snapshots.
* `src/aws_lambda/` – `receiver` (webhook → SQS) and `worker` (SQS → Shopify/OpenAI).
* `infra/terraform/` – AWS infrastructure (SQS, Lambda, DynamoDB) + tfvars examples.
* `data/` – sample CSVs (`to_translate` / `translated`).
* `state/`, `logs/` – local checkpoints and JSONL logs.

## Requirements

* Python 3.11+
* `pip`, `make` (for packaging), optionally Docker (for Lambda builds).
* AWS account with SQS, Lambda, DynamoDB, Secrets Manager (for cloud sync).
* Shopify Admin token + OpenAI API key (local dev via `.env`, production via Secrets Manager).

## Quick local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in OpenAI/Shopify keys if needed
```

Main `.env` variables:

* `OPENAI_API_KEY`, `OPENAI_MODEL`
* `TARGET_LOCALE` (default fr-FR)
* `TRANSLATOR_SPECIALIZATION`, `SOURCE_LANGUAGE_NAME`
* optional: `DO_NOT_TRANSLATE_YAML`, `LOG_PAYLOADS`, `TRANSLATION_CACHE_PATH`

## CSV → CSV Translation

1. Export a source CSV from Shopify (Markets → Translations).
2. Run:

   ```bash
   shopify-translator process \
     --input data/locales/fr/to_translate/products/shop_products.csv \
     --output data/locales/fr/translated/products/shop_products_translated_fr.csv \
     --target-locales fr-FR \
     --resume --stats
   ```
3. The command:

   * filters by Type/ID (`--types`, `--ids`, `--ids-file`, `--id-range`, `--first-n`)
   * auto-detects HTML/JSON/technical terms (`--auto-classify/--no-auto-classify`)
   * applies Do-Not-Translate rules (`--dnt path.yaml`)
   * supports parallel runs with SQLite cache, checkpoints, and JSONL logging.
4. Output: CSV with `Translated content` column populated for each target locale.

## Manual Shopify sync

Test the live pipeline without webhooks:

```bash
shopify-translator sync-shopify \
  --product-id 1234567890 \
  --target-locales fr-FR,de-DE \
  --mf-include namespace.key1,namespace.key2 \
  --dry-run
```

Useful flags:

* `--create` to simulate `products/create`
* `--apply-on-dry-run` to update snapshot only
* `--mf-json-paths` to filter JSON fields

## Automatic sync (AWS)

1. Prepare `infra/terraform/terraform.tfvars` from the example and set:

   * `project`, `aws_region`, `shop_domain`, `source_locale`, `target_locales`
   * Secrets Manager ARNs: `openai_api_key_secret_arn`, `shopify_admin_token_secret_arn`, `shopify_webhook_secret_arn`
   * Worker options: `mf_include`, `mf_json_paths`, `debounce_seconds`, `fill_missing_translations`, `dry_run`
2. Build Lambda packages:

   ```bash
   make build-receiver
   make build-worker-docker PY=3.12
   ```
3. Deploy with Terraform:

   ```bash
   terraform -chdir=infra/terraform init
   terraform -chdir=infra/terraform apply
   ```
4. Register Shopify webhooks (`products/create`, `products/update`) pointing to the Receiver Function URL.
5. Worker env vars: `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `MF_JSON_PATHS`, `DEBOUNCE_SECONDS`, `DRY_RUN`, `FILL_MISSING_TRANSLATIONS`, `OPENAI_API_KEY_SECRET_ARN`, `SHOPIFY_ADMIN_TOKEN_SECRET_ARN`.
   Receiver requires `SQS_URL` and HMAC secret (`SHOPIFY_WEBHOOK_SECRET[_ARN]`).
6. Digest snapshots are stored in DynamoDB (`product_snapshots`), dedup markers in `webhook_dedup`. Local cache lives at `/tmp/cache.sqlite`.

## Telemetry & logging

* Each run outputs a JSON summary (`--stats` or Lambda log) with `openai_calls`, tokens, `cache_hit/miss`, translated rows.
* Structured JSONL logs in `logs/` or CloudWatch; easy to parse with `jq`.
* `shopify-translator cache purge --vacuum` cleans the local cache.

## Tests & quality

```bash
pytest
ruff check src tests
black --check src tests
```

`tests/` includes smoke tests for pipeline, HTML/JSON validators, and sync dry-run.

## Troubleshooting

* Corrupted cache? Delete `state/cache.sqlite` and `state/checkpoint*`.
* OpenAI import error in Lambda? Check `openai_import_ok` in worker logs.
* Lambda package >70MB? Upload `build/worker.zip` to S3 and set `worker_s3_bucket/worker_s3_key` in Terraform.
* Quick pause for sync: set `DISABLE_SYNC=true` (env) on receiver/worker or use `shopify-translator sync-toggle`.

## License
Remove sensitive data before sharing the repository. Add your license text here.
