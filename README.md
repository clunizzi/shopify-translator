# Shopify Translator

Uno strumento per automatizzare la traduzione dei contenuti Shopify (prodotti, HTML, metafield JSON, SEO) tra più lingue in modo sicuro, affidabile e ripetibile. Si integra con Shopify via GraphQL e usa OpenAI per generare traduzioni di qualità, rispettando Liquid/HTML, numeri, unità, brand e regole SEO. Puoi eseguirlo in due modi: come pipeline locale (CSV → CSV) oppure in cloud con un flusso a webhook su AWS (Receiver + Worker) che traduce solo ciò che cambia.

Perché usarlo
- Qualità: usa OpenAI con prompt pensati per e‑commerce tecnico (giardinaggio/agri), con convalide su meta/handle e similarità di testo.
- Affidabilità: cache SQLite e snapshot su DynamoDB riducono drasticamente chiamate e ritraduzioni; debounce e dedup evitano loop da webhook.
- Sicurezza: segreti letti da AWS Secrets Manager; nessun token in chiaro nei sorgenti o nello state.
- Flessibilità: supporta HTML con Liquid, JSON dei metafield (traduce solo i valori), title/handle/meta/product_type, e backfill opzionale dei locali mancanti.
- Operatività: packaging Lambda a prova di pydantic-core (build Docker), deploy Terraform con S3 per zip grandi.

Pipeline di traduzione **multi-Type** per CSV Shopify, con:
- verifica stato prodotto via **Shopify GraphQL** (batch),
- traduzione con **OpenAI**, cache **SQLite** (con alias tra campi affini),
- gestione **HTML** segmentale + **protezione Liquid** (`{{ }}`, `{% %}`),
- gestione **JSON** (METAFIELD): traduce solo i **valori** e ricostruisce l’oggetto,
- regole su **Option/Value** (skip “Title”, “Default Title”, misure/size, valori tecnici),
- validatori **handle**/**meta**, similarità e controllo lingua soft,
- **checkpoint** per `Identification` (resume sicuro), subset per ID/Range/N e file,
- logging **JSONL** strutturato (stdout o file), comodo con `jq`.

---

## Installazione (Debian 12, Python 3.11+)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # compila le variabili

## Webhook Sync (AWS)

Cartella `infra/terraform/` contiene Terraform per:
- SQS coda webhook prodotti
- DynamoDB tabelle `product_snapshots` (digest, debounce) e `webhook_dedup`
- 2 Lambda: `webhook_receiver` (Function URL per Shopify) e `webhook_worker` (trigger SQS)

Lambda code:
- `src/aws_lambda/receiver.py`: verifica HMAC, accoda su SQS (delay 8s su create)
- `src/aws_lambda/worker.py`: dedup + debounce, chiama `src/shopify/sync.process_product(...)`, aggiorna snapshot su DynamoDB

Pacchetti zip attesi da Terraform:
- `var.receiver_zip` → ZIP con `src/aws_lambda/receiver.py`
- `var.worker_zip` → ZIP con codice Python e dipendenze usate da `process_product` (includere moduli `src/` e librerie 3rd-party)

Env Lambda principali:
- Receiver: `SQS_URL`, `SHOPIFY_WEBHOOK_SECRET_ARN` (oppure `SHOPIFY_WEBHOOK_SECRET` solo per test)
- Worker: `SHOP_DOMAIN`, `OPENAI_API_KEY_SECRET_ARN`, `SHOPIFY_ADMIN_TOKEN_SECRET_ARN`, `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `MF_JSON_PATHS`, `DDB_TABLE`, `DEDUP_TABLE`, `DEBOUNCE_SECONDS`, `REQUEST_TIMEOUT`, `RETRIES`, `DRY_RUN`

Note:
- `process_product` usa la cache SQLite locale (in Lambda: `/tmp/cache.sqlite`). Lo snapshot dei digest per i webhook viene sincronizzato su DynamoDB.
- Le credenziali si leggono da AWS Secrets Manager tramite gli ARN passati in env; evita di usare plaintext in Terraform/state.

Metafield:
- Se `MF_INCLUDE` è vuoto, auto-scoperta: si traducono i metafield del prodotto di tipo testuale/JSON (`single_line_text_field`, `multi_line_text_field`, `json`, `rich_text`) traducendo solo i valori (chiavi invariate). Se `MF_INCLUDE` è impostato, si limitano ai namespace.key indicati.

---

## Architettura (alto livello)

- Receiver (Lambda HTTP): verifica l'HMAC Shopify e accoda l'evento su SQS.
- Worker (Lambda SQS): deduplica e applica una finestra di debounce, sintetizza i contenuti traducibili via GraphQL, traduce con OpenAI, registra su Shopify e aggiorna gli snapshot.
- Cache locale: SQLite in `/tmp/cache.sqlite` su Lambda (riuso per tutto il lifecycle del container).
- Snapshot persistenti: DynamoDB per evitare ritraduzioni inutili fra invocazioni e per coalescere gli update.

## DynamoDB: cosa conserva e come consultarlo

Tabelle create da Terraform (vedi `infra/terraform/outputs.tf`):
- `product_snapshots` (nome: `${var.project}-shopify-product-snapshots`)
  - Chiavi: `pk` (partition), `sk` (sort)
  - Righe principali:
    - Snapshot digest per risorsa: `pk = "<shop>#<rid>"`, `sk = "digest#<source_locale>"`, `digest_map` = mappa `{ key -> digest }`
      - Per prodotto: `rid = gid://shopify/Product/<id>`
      - Per metafield: `rid = gid://shopify/Metafield/<id>`
    - Debounce marker: `pk = "<shop>#<gid_product>"`, `sk = "source#<source_locale>"`, `debounce_until` (epoch seconds)
- `webhook_dedup` (nome: `${var.project}-shopify-webhook-dedup`)
  - Chiave: `event_id` (string), `ttl` (per scadenza automatica)

Perché serve:
- Lo snapshot `digest_map` consente di capire se una chiave è cambiata rispetto all’ultima sincronizzazione e saltare chiamate OpenAI/Shopify quando non necessario.
- Il `debounce_until` evita processi ravvicinati sullo stesso prodotto (coalescing degli update durante una finestra).
- La tabella `webhook_dedup` impedisce il ri‑processamento di eventi duplicati.

Dove lo trovo e come lo interrogo:
- Console AWS → DynamoDB → Tables → cerca i nomi dalle `terraform outputs`.
- CLI esempi (sostituisci variabili):

```bash
# Nomi tabelle
terraform -chdir=infra/terraform output dynamodb_tables

# Snapshot digest prodotto
SHOP="mio-shop.myshopify.com"
PID=14878887018878
PK="${SHOP}#gid://shopify/Product/${PID}"
aws dynamodb get-item \
  --table-name <product_snapshots_table_name> \
  --key '{"pk": {"S": "'"$PK"'"}, "sk": {"S": "digest#en"}}' \
  --query 'Item.digest_map.S'

# Debounce marker
aws dynamodb get-item \
  --table-name <product_snapshots_table_name> \
  --key '{"pk": {"S": "'"$PK"'"}, "sk": {"S": "source#en"}}'

# Dedup evento
aws dynamodb get-item \
  --table-name <webhook_dedup_table_name> \
  --key '{"event_id": {"S": "<EventId>"}}'
```

Nota: per i metafield usa `PK = "<shop>#gid://shopify/Metafield/<RID>"` e `sk = "digest#<locale>"`.

## Build e deploy Lambda (compatibile Lambda)

- Build in Docker Amazon Linux (ruote manylinux):
  - `make build-worker-docker PY=3.12`
  - Output: `build/worker.zip`
- Upload su S3 e deploy con Terraform per zip > 70MB:
  - `aws s3 cp build/worker.zip s3://<bucket>/worker/worker-<ts>.zip`
  - `terraform -chdir=infra/terraform apply \
      -var 'worker_s3_bucket=<bucket>' \
      -var 'worker_s3_key=worker/worker-<ts>.zip'`

Variabili Terraform utili:
- `project`, `receiver_zip`, `worker_zip` (o `worker_s3_bucket` + `worker_s3_key`), `shop_domain`, ARNs Secrets, `target_locales`, `mf_include`, `mf_json_paths`, `debounce_seconds`, `dry_run`, `fill_missing_translations`.

## Variabili ambiente principali

- Generali: `SHOP_DOMAIN`, `SOURCE_LOCALE`, `TARGET_LOCALES`, `MF_INCLUDE`, `MF_JSON_PATHS`
- OpenAI: `OPENAI_API_KEY` (passato via Secrets Manager), `OPENAI_MODEL` (default `gpt-4o`)
- Shopify: `SHOPIFY_ADMIN_TOKEN` (via Secrets Manager)
- Worker: `DEBOUNCE_SECONDS`, `REQUEST_TIMEOUT`, `RETRIES`, `DRY_RUN`, `FILL_MISSING_TRANSLATIONS`
- Cache: `TRANSLATION_CACHE_PATH` (Lambda usa `/tmp/cache.sqlite`)

Specializzazione dominio (personalizza il prompt):
- `TRANSLATOR_SPECIALIZATION`: descrizione del dominio (es. "attrezzatura per il giardinaggio e l'agricoltura").
- `SOURCE_LANGUAGE_NAME`: etichetta lingua sorgente (es. "italiano", "inglese").

Esempi:
- vedi `.env.example` per sviluppo locale
- vedi `infra/terraform/terraform.tfvars.example` per Terraform

## Backfill traduzioni mancanti (opzionale)

- Abilitando `FILL_MISSING_TRANSLATIONS=true` sul worker:
  - Su update prodotto vengono processate anche chiavi “unchanged” per riempire i locali target mancanti.
  - `seo.title`/`seo.description` vuoti vengono backfillati usando rispettivamente `title` e `body_html` come sorgente.
  - L’`handle` può essere generato dal `title` tradotto nella stessa invocazione.

## Sicurezza e dati sensibili

- Usa AWS Secrets Manager per chiavi/token; non committare segreti.
- Non passare segreti in `terraform.tfvars` non cifrati; usa gli ARN e lascia che le Lambda li leggano a runtime.
- I log CloudWatch possono contenere snippet brevi; disabilita `LOG_PAYLOADS` in produzione se non indispensabile.

## CLI locale (CSV → CSV)

Esegui pipeline di traduzione da CSV:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

python -m src.cli process \
  --input data/input.csv \
  --output data/output.csv \
  --target-locale fr-FR \
  --stats --resume
```

## Debug rapido

- Verifica import SDK OpenAI nel worker: cerca log `{"openai_import_ok": true, ...}` in CloudWatch.
- Errori di packaging (pydantic_core): assicurati di buildare in Docker Amazon Linux.
- Dimensioni zip > 70MB: usa S3 (`worker_s3_bucket`/`worker_s3_key`).

## Licenza

Questo repository è pensato per l’uso open source; assicurati di rimuovere o generalizzare configurazioni e dati sensibili prima di pubblicare.
