# Shopify Translator (ITA)

Strumento Python per tradurre i contenuti Shopify (prodotti, HTML Liquid, metafield JSON, SEO) in modo ripetibile, con bootstrap da product ID e sincronizzazione continua via webhook AWS.

## Perché usarlo
- Sincronizzazione live con Shopify tramite Receiver + Worker AWS (SQS, Lambda, DynamoDB, Neon/PostgreSQL) con debounce, dedup e stato PDP centralizzato.
- Traduzioni OpenAI ottimizzate per e‑commerce tecnico: protezione Liquid/HTML, gestione metafield JSON, prompt specializzati.
- Stato cloud centralizzato su Neon/PostgreSQL con translation memory e dictionary.
- Telemetria semplice (token, cache hit/miss, tempo OpenAI) e logging JSONL.

## Struttura principale
- `src/cli.py` – Typer CLI (`shopify-translator`) con comandi `bootstrap`, `sync`, `sync-webhook`, `neon-reset` e utilità cache.
- `src/translate/` – motore di traduzione, cache SQLite, regole DNT.
- `src/shopify/` – GraphQL client e utility Shopify.
- `src/aws_lambda/` – funzioni `receiver` (webhook → SQS) e `worker` (SQS → Shopify/OpenAI).
- `infra/terraform/` – infrastruttura AWS (SQS, Lambda, DynamoDB) + esempi tfvars.
- `state/`, `logs/` – file locali di supporto e log JSONL.

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

## Bootstrap catalogo da product IDs
Per il bootstrap field-by-field del catalogo:
```bash
shopify-translator bootstrap-products --apply-translations
```
Per default legge gli ID da `state/bootstrap_product_ids.txt` oppure accetta `--ids-file`.
Il bootstrap usa Shopify come sorgente live, salva stato e translation memory su Neon/PostgreSQL e non tocca gli handle dei prodotti esistenti.
Alias breve:
```bash
shopify-translator bootstrap --apply-translations
```

## Sync incrementale da Neon
Per riallineare prodotti già presenti usando il confronto campo-per-campo sullo stato PDP salvato in Neon:
```bash
shopify-translator sync --store-only
```

## Reset schema Neon
Per ripartire pulito con il nuovo backend document-based:
```bash
shopify-translator neon-reset --yes
```

## Sync automatica (AWS)
1. Prepara `infra/terraform/terraform.tfvars` partendo dall’esempio e imposta:
   - `project`, `aws_region`, `shop_domain`, `source_locale`, `target_locales`
   - ARNs Secrets Manager: `openai_api_key_secret_arn`, `shopify_admin_token_secret_arn`, `shopify_webhook_secret_arn`
   - Opzioni worker: `mf_include`, `debounce_seconds`, `dry_run`, `disable_sync`, `log_verbose_sync`
2. Builda i pacchetti Lambda:
   ```bash
   make build-receiver
   make build-worker-docker PY=3.12  # oppure PY=3.11
   ```
   Sequenza completa (build + upload S3 + apply): vedi `comandini`.
3. Deploya con Terraform:
   ```bash
   terraform -chdir=infra/terraform init
   terraform -chdir=infra/terraform apply
   ```
4. Registra in Shopify i webhook `products/create` e `products/update` puntando alla Function URL del receiver.
5. Worker env principali: `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `DEBOUNCE_SECONDS`, `DRY_RUN`, `DISABLE_SYNC`, `LOG_VERBOSE_SYNC`, `OPENAI_API_KEY_SECRET_ARN`, `SHOPIFY_ADMIN_TOKEN_SECRET_ARN`, `NEON_DATABASE_URL_SECRET_ARN`. Receiver richiede `SQS_URL`, `DISABLE_SYNC` e il segreto HMAC (`SHOPIFY_WEBHOOK_SECRET[_ARN]`).
6. Il worker cloud usa Neon/PostgreSQL come fonte di verità per stato PDP, translation memory e dictionary. DynamoDB resta per dedup/debounce eventi (`product_snapshots`, `webhook_dedup`).

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
La cartella `tests/` copre worker/receiver AWS, policy di traduzione, validatori HTML/JSON e dictionary resolution.

## Troubleshooting
- Cache corrotta? cancella `state/cache.sqlite`.
- Errore OpenAI in Lambda: guarda i log `openai_import_ok` nel worker.
- Se il pacchetto Lambda supera 70 MB, carica `build/worker.zip` su S3 e passa `worker_s3_bucket/worker_s3_key` a Terraform.
- Pausa veloce della sync: imposta `DISABLE_SYNC=true` (env) su receiver/worker oppure usa `shopify-translator sync-webhook --disable`.

## Licenza
Rimuovi dati sensibili prima di condividere il repository. Imposta qui il testo di licenza desiderato.


# Shopify Translator (ENG)

Python tool to translate Shopify content (products, Liquid HTML, metafield JSON, SEO) in a consistent and repeatable way, with product-ID bootstrap and continuous sync via AWS webhooks.

## Why use it

* **Live sync** with Shopify using Receiver + Worker on AWS (SQS, Lambda, DynamoDB, Neon/PostgreSQL) with debounce, deduplication, and centralized PDP state.
* **OpenAI translations** optimized for technical e-commerce: protects Liquid/HTML, handles JSON metafields, and uses specialized prompts.
* **Centralized state on Neon/PostgreSQL** with translation memory and dictionary.
* **Lightweight telemetry** (token usage, cache hit/miss, OpenAI time) and JSONL logging.

## Main structure

* `src/cli.py` – Typer CLI (`shopify-translator`) with commands `bootstrap`, `sync`, `sync-webhook`, `neon-reset` and cache utilities.
* `src/translate/` – translation engine, SQLite cache, Do-Not-Translate rules.
* `src/shopify/` – GraphQL client and Shopify utilities.
* `src/aws_lambda/` – `receiver` (webhook → SQS) and `worker` (SQS → Shopify/OpenAI).
* `infra/terraform/` – AWS infrastructure (SQS, Lambda, DynamoDB) + tfvars examples.
* `state/`, `logs/` – local support files and JSONL logs.

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

## Catalog bootstrap from product IDs

```bash
shopify-translator bootstrap --apply-translations
```

By default it reads IDs from `state/bootstrap_product_ids.txt` or accepts `--ids-file`.

## Automatic sync (AWS)

1. Prepare `infra/terraform/terraform.tfvars` from the example and set:

   * `project`, `aws_region`, `shop_domain`, `source_locale`, `target_locales`
   * Secrets Manager ARNs: `openai_api_key_secret_arn`, `shopify_admin_token_secret_arn`, `shopify_webhook_secret_arn`
   * Worker options: `mf_include`, `debounce_seconds`, `dry_run`, `disable_sync`, `log_verbose_sync`
2. Build Lambda packages:

   ```bash
   make build-receiver
   make build-worker-docker PY=3.12
   ```
   Full build + S3 upload + apply sequence: see `comandini`.
3. Deploy with Terraform:

   ```bash
   terraform -chdir=infra/terraform init
   terraform -chdir=infra/terraform apply
   ```
4. Register Shopify webhooks (`products/create`, `products/update`) pointing to the Receiver Function URL.
5. Worker env vars: `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `DEBOUNCE_SECONDS`, `DRY_RUN`, `DISABLE_SYNC`, `LOG_VERBOSE_SYNC`, `OPENAI_API_KEY_SECRET_ARN`, `SHOPIFY_ADMIN_TOKEN_SECRET_ARN`, `NEON_DATABASE_URL_SECRET_ARN`.
   Receiver requires `SQS_URL` and HMAC secret (`SHOPIFY_WEBHOOK_SECRET[_ARN]`).
6. The cloud worker uses Neon/PostgreSQL as the source of truth for PDP state, translation memory, and dictionary. DynamoDB remains only for debounce/dedup (`product_snapshots`, `webhook_dedup`).

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

`tests/` covers AWS worker/receiver, translation policies, HTML/JSON validators, and dictionary resolution.

## Troubleshooting

* Corrupted cache? Delete `state/cache.sqlite`.
* OpenAI import error in Lambda? Check `openai_import_ok` in worker logs.
* Lambda package >70MB? Upload `build/worker.zip` to S3 and set `worker_s3_bucket/worker_s3_key` in Terraform.
* Quick pause for sync: set `DISABLE_SYNC=true` (env) on receiver/worker or use `shopify-translator sync-webhook --disable`.

## License
Remove sensitive data before sharing the repository. Add your license text here.
