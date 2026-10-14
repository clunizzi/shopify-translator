# Shopify Translation Control

Console Cloudflare Workers per osservare lo stato del traduttore, confrontare
e correggere traduzioni prodotto/tema e avviare operazioni controllate sul tema
live.

La sezione **Costi OpenAI** mostra le stime per oggi, mese, ultimi 7/30 giorni,
il costo medio per prodotto, token e chiamate per modello e gli ultimi eventi.
Le stime sono calcolate dai token realmente restituiti da OpenAI e dalle tariffe
versionate nel backend; la fattura e la Usage Dashboard OpenAI restano la fonte
contabile definitiva.

## Sicurezza

- Le API richiedono un JWT valido di Cloudflare Access.
- `ALLOW_LOCAL_DEV=true` è accettato solo su `localhost` o `127.0.0.1`.
- Se Access o Neon non sono configurati, l'API fallisce chiusa.
- Il browser dei contenuti legge i testi da Shopify soltanto su richiesta. Neon
  conserva per 90 giorni solo identificativi, digest e hash delle modifiche
  manuali, mai il testo tradotto.
- Le azioni AWS usano un utente IAM dedicato che può soltanto invocare la
  Lambda del tema.
- Ogni salvataggio corregge una sola coppia campo/lingua. La Lambda rilegge
  risorsa e digest prima di scrivere; sul tema ricontrolla anche ID e ruolo MAIN.
- L'editor rifiuta valori vuoti e modifiche strutturali a Liquid, tag HTML o
  JSON. Non modifica mai i file del tema.
- Audit, canary e sync richiedono un job Neon; può esistere un solo job attivo.
- Il canary è limitato a una traduzione. Il sync richiede la conferma testuale
  dell'ID tema autorizzato.
- I job più vecchi di 30 giorni vengono eliminati quando parte una nuova
  operazione.
- La telemetria costi registra una riga idempotente e compatta per evento. Non
  salva prompt, sorgenti o traduzioni e non può trasformare un sync riuscito in
  un retry se Neon non è disponibile.

## Avvio locale

```bash
cp .dev.vars.example .dev.vars
npm install
npm run dev
```

## Configurazione cloud

1. Copia `wrangler.jsonc` in un file locale ignorato, configura hostname,
   dominio Shopify, tema approvato e nomi Lambda. Mantieni
   `DEPLOYMENT_LOCKED=true` fino al termine del setup.
2. Crea un'applicazione Cloudflare Access davanti al Worker.
3. Imposta `CF_ACCESS_TEAM_DOMAIN` e `CF_ACCESS_AUD` come secret.
4. Imposta `NEON_DATABASE_URL` come secret.
5. Imposta `AWS_ACCESS_KEY_ID` e `AWS_SECRET_ACCESS_KEY` come secret del Worker.
6. Mantieni `AWS_REGION`, `AWS_THEME_FUNCTION_NAME`, `APPROVED_THEME_ID` e
   `TARGET_LOCALES` nelle vars non sensibili.
7. Configura `OPENAI_MODEL` e `OPENAI_FALLBACK_MODEL` con i modelli attivi. Puoi
   impostare `OPENAI_MONTHLY_BUDGET_USD` per visualizzare soglia, residuo e
   avvisi; lasciandolo vuoto non viene applicato alcun budget arbitrario. Imposta
   `REPORT_TIME_ZONE` (per AgriEden: `Europe/Rome`) per i confini giorno/mese.
8. Applica in ordine le migration SQL in `migrations/`, inclusa
   `003_translation_usage_events.sql`.
9. Esegui `npm run check`.
10. Sblocca il deployment e distribuisci con Wrangler usando il file locale.

Lo storico costi parte dal momento in cui backend e migration sono distribuiti:
non viene inventato un backfill a partire dai log. Una chiave OpenAI dedicata al
traduttore consente poi di confrontare facilmente la stima interna con il costo
reale nella dashboard OpenAI.

La rotazione della credenziale è descritta in
`../docs/cloudflare-aws-key-rotation.md`.
