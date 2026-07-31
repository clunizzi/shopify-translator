# Cloudflare and Neon cutover

This runbook keeps Shopify writes on AWS until the new Neon project and the
read-only Cloudflare console have both been verified.

## Preconditions

- Product webhooks remain active while the AWS product worker can be paused
  independently during a rollout.
- Record the approved Shopify MAIN theme ID before the cutover.
- Keep scheduled theme and global-resource sync disabled during migration.
- The SQS queue uses a 30-minute visibility timeout, 14-day retention and a
  dead-letter queue after five failed receives.

## Create the target Neon project

1. Create a Free project in the same European region as the current project.
2. Select PostgreSQL 17, matching the current source database.
3. Copy the direct, unpooled connection string for the migration.
4. Copy a direct, unpooled connection string from the source project too.
5. Do not create translator tables manually. The migration restores them.

## Pause and migrate

1. Set `disable_sync = "true"` in `infra/terraform/terraform.tfvars`.
2. Apply Terraform. The receiver remains active and keeps enqueueing valid
   Shopify webhooks while the worker is stopped.
3. Run:

   ```bash
   SOURCE_DATABASE_URL='postgresql://...' \
   TARGET_DATABASE_URL='postgresql://...' \
   scripts/migrate_neon_project.sh
   ```

   The script refuses a non-empty target, uses a matching `pg_dump`, restores
   without owners or ACLs, and compares all translator table inventories.

4. Replace the AWS Secrets Manager value with the new Neon URL.
5. Run a Lambda smoke test with zero records.
6. Set `disable_sync = "false"` and apply Terraform.
7. Confirm the SQS queue drains and the DLQ remains empty.

## Connect the Cloudflare console

1. Create a Cloudflare Access self-hosted application for the Worker hostname.
2. Store these Worker secrets:

   - `NEON_DATABASE_URL`
   - `CF_ACCESS_TEAM_DOMAIN`
   - `CF_ACCESS_AUD`

3. Keep `SCHEDULED_SYNC_ENABLED` set to `false`.
4. Run `npm run check` inside `cloudflare-admin`.
5. Deploy only after Access JWT validation and Neon connectivity pass.

Keep the console deployment locked until Access validation, Neon connectivity
and the least-privilege AWS integration have all been verified.

## Observation and rollback window

- Keep the old Neon project read-only for 14 days.
- Monitor the AWS product worker, SQS queue and DLQ after cutover.
- Compare product source and translation counts after one hour and after one
  day.
- Rotate the old Neon credential only after the new project is stable.
- Do not enable scheduled theme sync until its dry-run has been reviewed
  separately.
