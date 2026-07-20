# Cloud Run + Supabase deployment

The same image is used for two workloads:

- `soutui-api`: public FastAPI/HTML service.
- `soutui-train`: private Cloud Run Job that trains CTR/CVR and persists the artifact in PostgreSQL.

## 1. Supabase

Create a Supabase project in a region close to the Cloud Run region. Copy the **pooled** PostgreSQL connection string and ensure it requires SSL. Do not commit it.

For an existing SQLite deployment, import it before switching traffic:

```bash
export DATABASE_URL='postgresql://...'
python scripts/migrate_sqlite_to_postgres.py --source data/soutui.db --replace
```

The import is transactional. `--replace` is intentionally explicit because it deletes target application data before importing.

If the Supabase CLI is linked but the database password is not available, export a transactional SQL bundle and run it through the authenticated Management API:

```bash
python scripts/migrate_sqlite_to_postgres.py --source data/soutui.db --output-sql /tmp/soutui-import.sql --replace
supabase db query --linked --file /tmp/soutui-import.sql
```

## 2. Deploy

Run from Google Cloud Shell or a machine with `gcloud` authenticated:

```bash
export PROJECT_ID='your-gcp-project'
export REGION='us-central1'
export DATABASE_URL='postgresql://...'
# Optional during infrastructure migration; required before enabling real checkout:
export STRIPE_SECRET_KEY='sk_...'
export STRIPE_WEBHOOK_SECRET='whsec_...'
# Optional for a fresh database:
export SOUTUI_BOOTSTRAP_MERCHANT_EMAIL='merchant@example.com'
export SOUTUI_BOOTSTRAP_MERCHANT_PASSWORD='use-a-strong-password'
bash deploy/cloudrun/deploy.sh
```

The script enables required APIs, builds the container, stores credentials in Secret Manager, deploys the API and creates the training Job.
Stripe and merchant bootstrap secrets are optional pairs. Without Stripe, the site deploys normally but checkout fails closed until the two Stripe secrets are configured and the script is rerun.

After deployment, update the Stripe webhook endpoint to the printed Cloud Run URL and subscribe to:

- `checkout.session.completed`
- `checkout.session.expired`

Execute training with:

```bash
gcloud run jobs execute soutui-train --region us-central1 --wait
```

Do not remove the Tencent deployment until database counts, login, checkout and Stripe webhook delivery have been verified on Cloud Run.
