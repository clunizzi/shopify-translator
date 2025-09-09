# translate_metafield_json.py
import os, json, requests, sys
import os, sys
from dotenv import load_dotenv, find_dotenv

# Carica .env.local se presente, altrimenti .env
dotenv_path = find_dotenv(".env.local", usecwd=True) or find_dotenv(".env", usecwd=True)
if dotenv_path:
    load_dotenv(dotenv_path)
else:
    print("⚠️  Nessun .env trovato (cerco .env/.env.local)", file=sys.stderr)

def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required env var: {name}")
    return val

def env_int(name: str, default: int | None = None) -> int | None:
    v = os.getenv(name)
    return int(v) if v is not None else default

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.lower() in {"1", "true", "t", "yes", "y"}

SHOP_DOMAIN = os.environ["SHOPIFY_STORE_DOMAIN"]            # es: myshop.myshopify.com
ADMIN_TOKEN = os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"]    # token Admin API
API_URL = f"https://{SHOP_DOMAIN}/admin/api/2025-07/graphql.json"

PRODUCT_NUMERIC_ID = os.environ.get("PRODUCT_NUMERIC_ID")  # es: "1234567890"
NAMESPACE = os.environ.get("MF_NAMESPACE", "custom")
KEY = os.environ.get("MF_KEY", "features")
TARGET_LOCALE = os.environ.get("TARGET_LOCALE", "it")

PRODUCT_GID = f"gid://shopify/Product/{PRODUCT_NUMERIC_ID}"

def gql(query: str, variables: dict):
    r = requests.post(
        API_URL,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": ADMIN_TOKEN,
        },
        data=json.dumps({"query": query, "variables": variables}),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]

def get_metafield_id(product_gid: str, namespace: str, key: str) -> dict:
    q = """
    query($pid: ID!, $ns: String!, $key: String!) {
      product(id: $pid) {
        id
        metafield(namespace: $ns, key: $key) {
          id
          type
          value
        }
      }
    }"""
    d = gql(q, {"pid": product_gid, "ns": namespace, "key": key})
    mf = d["product"]["metafield"]
    if not mf:
        raise RuntimeError(f"Metafield {namespace}.{key} non trovato sul prodotto.")
    return mf  # {id, type, value}

def get_digests_for_ids(ids: list[str]) -> dict:
    q = """
    query($ids: [ID!]!) {
      translatableResourcesByIds(first: 250, resourceIds: $ids) {
        nodes {
          resourceId
          translatableContent { key value digest locale }
        }
      }
    }"""
    d = gql(q, {"ids": ids})
    out = {}
    for node in d["translatableResourcesByIds"]["nodes"]:
        rid = node["resourceId"]
        out[rid] = node["translatableContent"]
    return out

def fake_translate_json_strings(data):
    # placeholder: "traduce" aggiungendo [IT] alle stringhe
    if isinstance(data, str):
        return data + " [de gustibus cazzone]"
    if isinstance(data, list):
        return [fake_translate_json_strings(x) for x in data]
    if isinstance(data, dict):
        return {k: fake_translate_json_strings(v) for k, v in data.items()}
    return data

def main():
    if not PRODUCT_NUMERIC_ID:
        print("Setta PRODUCT_NUMERIC_ID nell'ambiente.", file=sys.stderr)
        sys.exit(1)

    print(f"Prodotto: {PRODUCT_GID}")
    mf = get_metafield_id(PRODUCT_GID, NAMESPACE, KEY)
    metafield_gid = mf["id"]
    print(f"Metafield {NAMESPACE}.{KEY} → {metafield_gid} (type={mf['type']})")

    # Parse valore JSON sorgente (se non è JSON valido, alza)
    try:
        source_json = json.loads(mf["value"])
    except Exception as e:
        raise RuntimeError(f"Il valore del metafield non è JSON valido: {e}")

    # Recupera digest per Product e Metafield
    digests = get_digests_for_ids([PRODUCT_GID, metafield_gid])
    mf_tc = digests[metafield_gid]
    # Per i metafield, la chiave traducibile è "value"
    mf_digest = next((c["digest"] for c in mf_tc if c["key"] == "value"), None)
    if not mf_digest:
        raise RuntimeError("Digest del metafield non trovato: verifica che la definition sia translatable.")

    # Simula traduzione del JSON (sostituisci con la tua MT)
    translated_json_obj = fake_translate_json_strings(source_json)

    # IMPORTANTISSIMO: serializzare in STRINGA JSON
    translated_json_str = json.dumps(translated_json_obj, ensure_ascii=False, separators=(",", ":"))

    # Mutation: registriamo SOLO il metafield (puoi aggiungere alias per title/description nello stesso colpo)
    m = """
    mutation RegisterMetafield($id: ID!, $translations: [TranslationInput!]!) {
      mf: translationsRegister(resourceId: $id, translations: $translations) {
        userErrors { field message }
        translations { key locale }
      }
    }"""

    variables = {
        "id": metafield_gid,  # resourceId = GID del Metafield
        "translations": [{
            "key": "value",                              # per metafield la chiave traducibile è SEMPRE "value"
            "locale": TARGET_LOCALE,                     # es. "it"
            "value": translated_json_str,                # STRINGA JSON, non oggetto!
            "translatableContentDigest": mf_digest       # necessario
        }]
    }

    resp = gql(m, variables)
    print("Risposta:", json.dumps(resp, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
