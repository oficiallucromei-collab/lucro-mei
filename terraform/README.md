# LucroMEI on Google Cloud

This configuration manages project `lucro-mei` in `southamerica-east1`. It covers:

- the APIs the app and its build use;
- the `lucromei` Docker repository and its cleanup policies;
- the `lucromei-runtime` and `lucromei-build` service accounts;
- the Secret Manager entries and their runtime grants;
- the public `lucromei-test` Cloud Run service;
- the `lucromei-migrate` Cloud Run job;
- the private bucket that stages release source.

The test service runs with the app's production safeguards. It connects to the
isolated Neon test database through `lucromei-test-database-url`. The
`lucromei-database-url` entry and its grant exist but nothing uses them yet.

Terraform tracks secret metadata and version references, never payloads. To add
or rotate a payload, add a version directly in Secret Manager, then update the
version number in `local.pinned_secret_versions` in `main.tf`. To change it for
one plan only, use `-var='secret_versions={...}'`.

Neon, Meta and the GitHub connection are operated outside Terraform.

## State and access

State lives in the private, versioned bucket `gs://lucro-mei-tfstate-312975377897`
(prefix `lucromei`). The bucket is not managed here. Never commit state or plans;
`.gitignore` already excludes them.

Use Terraform 1.7 or newer. You need an account that can read and update these
resources. Either use Application Default Credentials, or pass a gcloud token:

```sh
export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)"
terraform -chdir=terraform init
terraform -chdir=terraform plan
```

A plan with no changes means the live project matches this configuration. The
services, secrets and repository have `prevent_destroy` or deletion protection.

### Grants the project owner applies

These need project IAM admin rights, so they are kept out of Terraform:

```sh
# Lets Cloud Build write logs as lucromei-build (needed by cloudbuild.yaml).
gcloud projects add-iam-policy-binding lucro-mei \
  --member=serviceAccount:lucromei-build@lucro-mei.iam.gserviceaccount.com \
  --role=roles/logging.logWriter
```

## Build a release

Cloud Build runs as `lucromei-build`. It runs lint and tests, builds the linux/amd64
image, and runs `scripts/smoke-image.sh` against the image with production settings
and dummy credentials. It pushes the commit-tagged image only after the smoke test
passes. The build never deploys.

From the repository root, on the reviewed commit, run in Bash. `git archive` uploads
only committed files, so local changes and untracked credentials stay out:

```bash
set -euo pipefail
commit="$(git rev-parse --verify HEAD^{commit})"
source_dir="$(mktemp -d)"
trap 'rm -rf "$source_dir"' EXIT
git archive "$commit" | tar -x -C "$source_dir"
source_bucket="$(terraform -chdir=terraform output -raw build_source_bucket)"

build_id="$(gcloud builds submit "$source_dir" \
  --project=lucro-mei --region=global \
  --config="$source_dir/cloudbuild.yaml" \
  --gcs-source-staging-dir="$source_bucket/source" \
  --ignore-file=/dev/null --substitutions="COMMIT_SHA=$commit" \
  --async --format='value(id)')"
printf 'Build ID: %s\n' "$build_id"
gcloud builds log "$build_id" --project=lucro-mei --region=global --stream

build=( "$build_id" --project=lucro-mei --region=global )
test "$(gcloud builds describe "${build[@]}" --format='value(status)')" = SUCCESS
digest="$(gcloud builds describe "${build[@]}" --format='value(results.images[0].digest)')"
image="southamerica-east1-docker.pkg.dev/lucro-mei/lucromei/lucromei@$digest"
printf '%s\n' "$image"
```

## Promote a release

Migrations run before the service moves to the new image. They must stay additive
(new tables and columns), so the revision still serving keeps working while they run.

1. Point the migration job at the new image and run it:

   ```bash
   terraform -chdir=terraform plan -var="migration_image=$image" -out=migrate.tfplan
   terraform -chdir=terraform apply migrate.tfplan     # changes only lucromei-migrate
   gcloud run jobs execute lucromei-migrate --project=lucro-mei \
     --region=southamerica-east1 --wait
   ```

2. Move the service to the image. Review that the plan only changes the image:

   ```bash
   terraform -chdir=terraform plan -var="image=$image" -out=release.tfplan
   terraform -chdir=terraform apply release.tfplan
   curl -fsS https://lucromei-test-312975377897.southamerica-east1.run.app/health
   ```

3. Record the promoted digest as the `image` default in `variables.tf` and commit it,
   so later plans keep the release. Note the previous digest in the commit message.

To roll back, plan and apply the previous digest with `-var="image=..."`. First check
that the old code works with the current schema. Migrations are not rolled back
automatically.

The service runs with `cpu_idle = false`, so the embedded job worker keeps its CPU
between requests. Releases of the current code also need `LUCROMEI_TERMS_URL`
(an https link to the terms of use) in the service environment, or the app refuses
to start in production.
