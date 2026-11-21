# LucroMEI on Google Cloud

This configuration manages project `lucro-mei` in `southamerica-east1`. It covers:

- the APIs the app and its build use;
- the `lucromei` Docker repository and its cleanup policies;
- the `lucromei-runtime` and `lucromei-build` service accounts;
- the Secret Manager entries and their runtime grants;
- the public `lucromei-test` Cloud Run service;
- the `lucromei-migrate` Cloud Run job;
- the `lucromei-ops` Cloud Run job (operator tools, `python -m lucromei.ops`, runtime
  database role; billed only while it runs);
- the private bucket that stages release source;
- the deploy grants for `lucromei-build` and the push-to-main release trigger.

The test service runs with the app's production safeguards. It connects to the
isolated Neon test database through `lucromei-test-database-url`. The
`lucromei-database-url` entry and its grant exist but nothing uses them yet.

Terraform tracks secret metadata and version references, never payloads. To add
or rotate a payload, add a version directly in Secret Manager, then update the
version number in `local.pinned_secret_versions` in `main.tf`. To change it for
one plan only, use `-var='secret_versions={...}'`.

Neon and Meta are operated outside Terraform.

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

### IAM grants

Every grant is tracked here. `iam.tf` holds the ones that need IAM admin rights:

- `roles/logging.logWriter` on the project for `lucromei-build`;
- `roles/iam.serviceAccountUser` on `lucromei-runtime` for `lucromei-build`;
- `roles/secretmanager.admin` on the project for the Cloud Build service agent, which
  stores the GitHub connection's token;
- the project roles of the operator account, `oficiallucromei@gmail.com`
  (`local.operator_roles`). The list keeps no role that another listed role already
  covers.

Not tracked: `roles/owner`, which is only granted temporarily by hand; other human
owners; and the service agents and default service accounts Google manages.

Changing these grants needs an IAM admin (for example Owner); the operator account
cannot grant itself roles. Planning them needs `getIamPolicy`, which the operator
gets from `roles/iam.securityReviewer`.

## Release pipeline

`cloudbuild.yaml` runs as `lucromei-build`. Each step runs only if the previous one passed:

1. lint and tests;
2. build the linux/amd64 image;
3. run `scripts/smoke-image.sh` against it, with production settings and dummy credentials;
4. push the commit-tagged image and resolve its digest;
5. point `lucromei-migrate` at the digest and run it (`alembic upgrade head`);
6. move `lucromei-test` to the digest and check `/health`.

Migrations run while the previous revision still serves, so they must stay additive
(new tables and columns). If the new revision fails to start, Cloud Run keeps traffic
on the previous one and the build fails.

The pipeline owns the image of the service and the job. Terraform ignores their
`image`, and `var.image` only seeds them if they are ever recreated. Other settings
(env, secrets, scaling) still go through Terraform.

### Push-to-main trigger

The `lucromei-release` trigger runs the pipeline on every push to `main`. `trigger.tf`
tracks the trigger and the `lucro-mei` repository link. It also tracks the
`lucromei-github` connection, which uses the Google Cloud Build GitHub App
(installation `183528323`) on `oficiallucromei-collab`.

The connection was created by hand and then imported, because the GitHub
authorization is interactive. Its OAuth token lives in the Secret Manager secret
`lucromei-github-github-oauthtoken-af8e8a`. Cloud Build's service agent created that
secret and keeps it current, so Terraform does not manage it.

If the connection ever has to be recreated, `prevent_destroy` must be lifted first.
Then:

1. Create it and authorize GitHub through the link it prints:

   ```bash
   gcloud builds connections create github lucromei-github \
     --project=lucro-mei --region=southamerica-east1
   ```

2. Check that `installationState.stage` is `COMPLETE`, and note the new
   `appInstallationId` and `oauthTokenSecretVersion`:

   ```bash
   gcloud builds connections describe lucromei-github \
     --project=lucro-mei --region=southamerica-east1
   ```

3. Update both values in `trigger.tf`, then plan and apply.

### Run the pipeline by hand

From the repository root, on the reviewed commit, run in Bash. `git archive` uploads
only committed files, so local changes and untracked credentials stay out. This
deploys too; add `,_DEPLOY=false` to the substitutions to only publish the image:

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

## Roll back

Move the service back to the previous digest (the build log and the service's
revisions list it). First check that the old code works with the current schema.
Migrations are not rolled back automatically.

```bash
gcloud run services update lucromei-test --project=lucro-mei \
  --region=southamerica-east1 --image="southamerica-east1-docker.pkg.dev/lucro-mei/lucromei/lucromei@sha256:..."
```

The next push to `main` deploys again, so revert or fix the bad commit too.

The service runs with `cpu_idle = false`, so the embedded job worker keeps its CPU
between requests. Releases of the current code also need `LUCROMEI_TERMS_URL`
(an https link to the terms of use) in the service environment, or the app refuses
to start in production.
