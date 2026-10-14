# TODO

## Lunedì 12 ottobre 2026 — costi e GUI v2

- [x] Aggiungere il preflight dei costi prima dei job manuali tema: audit gratuito, canary e sync stimati dallo storico e dall'ultimo audit.
- [x] Registrare una stima per evento usando i token effettivamente restituiti da OpenAI e tariffe versionate, senza salvare contenuti.
- [ ] Introdurre guardrail: limite per evento, limite giornaliero/mensile, arresto automatico e richiesta di approvazione per i job oltre soglia.
- [x] Mostrare nella GUI ultimi eventi, spesa stimata giornaliera/mensile, costo medio prodotto, modelli/token, budget residuo e avvisi 80/100%.
- [ ] Migrare OpenAI key, Shopify Admin token, Neon URL e webhook secret da AWS Secrets Manager a SSM Parameter Store `SecureString` Standard, mantenendo permessi IAM minimi e cache runtime.
- [ ] Verificare tutte le Lambda con i nuovi parametri prima di programmare la cancellazione recuperabile dei vecchi secret.
- [x] Avviare la GUI v2: stato operativo, prodotti IT/DE/FR affiancati, editor strutturato, anteprima, tema, anomalie e storico modifiche.
- [ ] Gestire i template email/notifiche Shopify: inventario dei template e delle lingue, traduzione assistita IT → DE/FR, protezione di Liquid/HTML e variabili, anteprima e revisione manuale prima della pubblicazione.

Vincoli: nessuna modifica ai file del tema live, nessun job massivo senza preflight, nessuna eliminazione dei secret prima della verifica completa e nessuna pubblicazione automatica dei template email senza approvazione manuale.
