# Rotazione credenziale Cloudflare -> AWS

La console usa un utente IAM dedicato che può esclusivamente invocare la
Lambda operativa configurata per il progetto.

## Frequenza

Controllare la credenziale ogni 90 giorni e ruotarla immediatamente in caso di
sospetta esposizione. La GUI mostra l'età della chiave.

## Procedura senza downtime

1. Creare una seconda access key sullo stesso utente IAM.
2. Aggiornare `AWS_ACCESS_KEY_ID` e `AWS_SECRET_ACCESS_KEY` nei secret del
   Worker Cloudflare.
3. Eseguire un audit tema read-only dalla GUI.
4. Verificare che il job termini con stato `succeeded`.
5. Disattivare la vecchia access key.
6. Dopo la verifica finale, eliminare la vecchia access key.
7. Aggiornare `AWS_CREDENTIAL_CREATED_AT` in `cloudflare-admin/wrangler.jsonc`.

Non mantenere due chiavi attive oltre la finestra necessaria al collaudo.
