# Theme Editor TODO

- Add `--resource-id` filtering to `theme-bootstrap` for surgical retries.
- Add a dedicated `theme-sync` path instead of reusing bootstrap semantics.
- Add `theme-clone-translations --from-theme-id --to-theme-id`.
- Wire `themes/update` webhook into the cloud path.
- Decide debounce/dedup strategy for theme updates in Lambda/SQS.
- Add optional CloudWatch payload debug toggles for theme sync in Terraform/env docs.
- Add documentation for the theme translation workflow and recovery queries.
