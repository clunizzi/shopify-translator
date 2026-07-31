# Shopify Translator

A production-oriented translation engine for Shopify catalogs and themes. It
combines Shopify Admin GraphQL, OpenAI, Neon/PostgreSQL, AWS Lambda/SQS and an
optional Cloudflare Workers control panel.

The project is designed around one rule: **translate only content that has
actually changed**. Inventory-only `products/update` events are filtered before
OpenAI or Shopify writes, remote translations are audited before registration,
and theme writes are blocked when the approved theme is no longer the live
MAIN theme.

## What it covers

- Products, variants, options and selected metafields.
- Custom SEO title and description fields.
- Missing localized handles without rewriting existing URLs.
- JSON templates, section groups and configured locale-file namespaces.
- Store policies and other explicitly enabled global resources.
- Translation memory, dictionary entries, source digests and sync state in
  Neon/PostgreSQL.
- Near-real-time product and theme updates through Shopify webhooks.
- Scheduled reconciliation as a safety net for missed or delayed events.
- An Access-protected Cloudflare dashboard for audits, previews and controlled
  manual corrections.

## Safety model

- Shopify writes require explicit flags and production configuration.
- The example Terraform configuration starts with `disable_sync = "true"`.
- Theme writes require an approved theme ID matching Shopify's current MAIN
  theme.
- Webhook HMAC validation, SQS retries, dead-letter queues, deduplication and
  debounce controls are built in.
- Existing current translations are preserved; only missing or outdated fields
  are registered.
- Liquid, HTML structure, JSON shape and protected terminology are validated
  before a translation is accepted.
- Secrets belong in local environment files, AWS Secrets Manager and
  Cloudflare Worker secrets. They are never required in tracked configuration.

## Architecture

```text
Shopify webhooks
      |
      v
AWS Lambda receiver --HMAC--> SQS --> Lambda worker
                                      |     |     |
                                      |     |     +--> Shopify Admin GraphQL
                                      |     +--------> OpenAI
                                      +--------------> Neon/PostgreSQL

EventBridge --> catalog/theme pollers --> the same sync engine

Cloudflare Access --> Workers dashboard --> Neon + least-privilege AWS invoke
```

## Repository layout

- `src/translate/` — translation engine, validation and cache.
- `src/bootstrap/` — catalog, SEO, handles, theme and reconciliation flows.
- `src/shopify/` — Shopify Admin GraphQL client.
- `src/state/` — Neon/PostgreSQL persistence.
- `src/aws_lambda/` — webhook receiver, worker and scheduled pollers.
- `infra/terraform/` — AWS infrastructure and safe example variables.
- `cloudflare-admin/` — optional operations dashboard.
- `src/config/` — configurable field, metafield, theme and terminology policy.
- `tests/` — unit and contract coverage for the critical sync paths.

## Requirements

- Python 3.11+
- Shopify Admin API credentials with the scopes required by the resources you
  enable, including `read_themes` for theme tracking.
- OpenAI API credentials.
- Neon/PostgreSQL.
- AWS credentials for cloud deployment.
- Node.js and npm for the optional Cloudflare dashboard.
- Docker is recommended for Lambda-compatible Python builds.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill only the local `.env`. The example intentionally contains no credentials.
Important variables include:

- `SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`
- `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_FALLBACK_MODEL`
- `NEON_DATABASE_URL`
- `SOURCE_LOCALE`, `TARGET_LOCALES`
- `TRANSLATOR_SPECIALIZATION`, `TRANSLATOR_BRAND`, `TRANSLATOR_AUDIENCE`
- `DO_NOT_TRANSLATE_YAML`, `METAFIELD_TRANSLATION_POLICY_PATH`,
  `THEME_TRANSLATION_POLICY_PATH`

Store-specific policy overlays can be placed in ignored `*.local.yaml` files.
This keeps public defaults generic while still packaging local policies into a
Lambda build.

## Common commands

```bash
# Catalog bootstrap and incremental sync
shopify-translator bootstrap --apply-translations
shopify-translator sync --apply-translations --dry-run

# SEO audit and controlled rollout
shopify-translator seo-audit --target-locales fr,de
shopify-translator seo-sync --target-locales fr,de --dry-run
shopify-translator seo-sync --target-locales fr,de \
  --apply-translations --max-products 100 --continue-on-error

# Localized handles
shopify-translator handles-audit --target-locales fr,de
shopify-translator handles-complete --target-locales fr,de --dry-run

# Read-only theme tracking; replace the example with the approved MAIN ID
shopify-translator theme-track --approved-theme-id 123456789012

# Validate model behavior without Shopify or Neon writes
shopify-translator model-canary --models gpt-5.6-terra,gpt-5.6-sol
```

Always review a dry-run before enabling writes against a live store.

## AWS deployment

1. Build the Lambda packages:

   ```bash
   make build-receiver
   make build-worker-docker PY=3.12
   ```

2. Copy the safe example and fill the ignored local file:

   ```bash
   cp infra/terraform/terraform.tfvars.example \
      infra/terraform/terraform.tfvars
   ```

3. Keep `disable_sync = "true"`, provision the infrastructure and review the
   outputs:

   ```bash
   terraform -chdir=infra/terraform init
   terraform -chdir=infra/terraform plan
   terraform -chdir=infra/terraform apply
   ```

4. Register `products/create`, `products/update`, `themes/update` and
   `themes/publish` webhooks against the receiver URL.
5. Run audits and canaries, set the approved MAIN theme ID, then deliberately
   enable the required sync paths.

Large worker packages can be uploaded to S3 with
`scripts/deploy-worker.sh`. The script creates a Terraform plan and applies it
only when `DEPLOY_APPLY=1` is provided.

## Cloudflare dashboard

```bash
cd cloudflare-admin
cp .dev.vars.example .dev.vars
npm install
npm run dev
```

The tracked Wrangler config is intentionally locked and contains only example
values. For deployment, use a local ignored config and Cloudflare secrets for:

- `NEON_DATABASE_URL`
- `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

The IAM identity used by the dashboard should only be allowed to invoke the
configured operations Lambda. Put the dashboard behind Cloudflare Access
before unlocking it.

## Configuration policies

The default configuration is deliberately conservative:

- `do_not_translate.yaml` contains generic units, tokens and a minimal glossary.
- `metafield_translation.yaml` allowlists textual leaves and blocks IDs, URLs,
  filenames and other structural values.
- `theme_translation.yaml` allowlists textual theme fields. Locale resources
  are translated only for explicitly listed, store-owned key prefixes because
  Shopify exposes platform checkout/account strings through the same resource.

Review and customize these policies for each store before enabling writes.

## Tests

```bash
pytest
ruff check src tests
black --check src tests

cd cloudflare-admin
npm run check
```

## Operational runbooks

- `docs/cloudflare-neon-cutover.md`
- `docs/cloudflare-aws-key-rotation.md`

## License

No open-source license has been selected yet. Until one is added, standard
copyright rules apply.
