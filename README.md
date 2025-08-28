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
