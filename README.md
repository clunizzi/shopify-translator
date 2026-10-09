# Shopify Translator

### Keep every Shopify translation in sync. Automatically.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Shopify Admin API](https://img.shields.io/badge/Shopify-Admin_API-7AB55C?logo=shopify&logoColor=white)](https://shopify.dev/docs/api/admin-graphql)
[![AWS](https://img.shields.io/badge/AWS-Lambda_%2B_SQS-FF9900?logo=amazonwebservices&logoColor=white)](https://aws.amazon.com/)
[![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers-F38020?logo=cloudflare&logoColor=white)](https://workers.cloudflare.com/)

Shopify Translator is a cloud translation engine for stores that need more
than a one-off export/import. It watches Shopify, detects meaningful content
changes, translates only what is actually outdated, and keeps every locale
aligned over time.

Products, SEO, URLs and theme content stay current — without wasting API calls
every time an order changes inventory.

## Why it is different

- **Near real-time sync** — Shopify webhooks trigger focused translation jobs.
- **Theme-safe polling** — a lightweight Shopify fingerprint catches theme-file
  and page changes that Shopify does not emit as useful webhooks; unchanged
  checks never wake Neon or OpenAI.
- **Change-aware** — inventory-only updates stop before OpenAI or Shopify
  writes.
- **Complete storefront coverage** — products, variants, options, selected
  metafields, SEO, handles and theme content.
- **Safe on live stores** — retries, deduplication, debounce, dry-runs and a
  strict MAIN-theme guard.
- **Built to operate** — Neon-backed state, scheduled reconciliation and an
  optional Cloudflare dashboard for audits and manual corrections.

## How it works

```text
Shopify webhooks
      │
      ▼
Lambda receiver ──HMAC──▶ SQS ──▶ Translation worker
                                      ├── OpenAI
                                      ├── Shopify Admin GraphQL
                                      └── Neon / PostgreSQL

EventBridge ──▶ digest poller ──changed only──▶ reconciliation
Cloudflare Access ──▶ operations dashboard
```

The worker compares Shopify's live source digests, remote translations and its
stored state. If nothing relevant changed, it does nothing. If content is
missing or outdated, it translates and registers only those fields.

## What it translates

| Area | Coverage |
| --- | --- |
| Catalog | Products, descriptions, product types, variants and options |
| Custom data | Allowlisted textual metafield leaves, including structured JSON |
| SEO | Meta titles and descriptions, with conservative rollout controls |
| URLs | Localized handles and internal links, preserving existing valid URLs |
| Theme | JSON templates, section groups and configured locale namespaces |
| Collections | Titles, SEO, handles and deterministic `Ricambi <model>` localization |
| Global resources | Pages, blogs, articles, menus, links, policies and shop copy |

Liquid, HTML, JSON structure, protected terms, product codes and units are
validated before a translation is accepted.

## Quick start

```bash
git clone https://github.com/clunizzi/shopify-translator.git
cd shopify-translator

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
shopify-translator --help
```

Add your Shopify, OpenAI and Neon credentials only to `.env`. The tracked
example contains no secrets.

Start with read-only checks:

```bash
shopify-translator seo-audit --target-locales fr,de
shopify-translator handles-audit --target-locales fr,de
shopify-translator theme-track --approved-theme-id 123456789012
shopify-translator sync --apply-translations --dry-run
```

Review the output before enabling writes against a live store.

## Cloud deployment

The included Terraform stack provisions the AWS receiver, worker, queues,
dead-letter queue, DynamoDB coordination tables and scheduled pollers.

The theme/global poll defaults to every 10 minutes. It stores only a compact
fingerprint in DynamoDB and runs the Neon/OpenAI reconciliation path only when
Shopify source content or a remote translation actually changes.

Set `COLLECTION_AI_ENABLED=false` to keep collection synchronization limited
to deterministic rules. Free-form collection copy is sent to the configured
AI translator only after this gate is explicitly enabled.

```bash
make build-receiver
make build-worker-docker PY=3.12

cp infra/terraform/terraform.tfvars.example \
   infra/terraform/terraform.tfvars

terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan
```

Public defaults are deliberately locked: `disable_sync = "true"`, scheduled
sync is off and no theme ID is approved. Configure Secrets Manager, run the
audits and unlock each write path intentionally.

## Operations dashboard

`cloudflare-admin/` contains an optional Cloudflare Workers control panel for:

- translation coverage and system health;
- product and theme inspection;
- localized storefront previews;
- controlled manual corrections;
- theme audits, canaries and sync jobs.

```bash
cd cloudflare-admin
cp .dev.vars.example .dev.vars
npm install
npm run dev
```

The dashboard is designed to run behind Cloudflare Access and uses a
least-privilege AWS identity.

## Store-specific configuration

The public policies are conservative and generic. Customize them without
polluting the repository:

- `do_not_translate.local.yaml` for brands, units and terminology;
- `metafield_translation.local.yaml` for translatable custom-data leaves;
- `theme_translation.local.yaml` for store-owned locale namespaces.

Files matching `src/config/*.local.yaml` are ignored by Git but included in
local Lambda builds.

## Quality and safety

```bash
pytest
ruff check src tests
black --check src tests

cd cloudflare-admin
npm run check
```

The sync path is covered by tests for translation failures, Shopify GraphQL
errors, stale digests, retries, reconciliation, theme mismatches and
inventory-driven webhook noise.

## Documentation

- [Cloudflare and Neon cutover](docs/cloudflare-neon-cutover.md)
- [AWS credential rotation](docs/cloudflare-aws-key-rotation.md)
- [Cloudflare dashboard setup](cloudflare-admin/README.md)

## Important

This is an advanced Shopify integration, not a one-click App Store install.
Use dry-runs, canaries and least-privilege credentials before enabling live
writes.

## License

No open-source license has been selected yet. Until one is added, standard
copyright rules apply.
