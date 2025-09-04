# Shopify Translator

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
