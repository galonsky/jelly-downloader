# Jelly Downloader

## Build and deploy

CircleCI builds every commit to `main` and publishes
`galonsky/video-downloader:sha-<full-commit-sha>` plus the convenience `latest`
tag. Kargo watches this repository and Docker Hub, creates Freight only when a
Git commit has a matching image tag, and requires a manual promotion to the
`prod` Stage. Promotion pins Argo CD to that commit and image tag, then waits
for the application to become healthy and synced.

The Argo CD and Kargo resource definitions are in `deploy/`. For a new cluster,
create the namespace-specific `jellyfin-api` Secret and `video-downloader-config`
ConfigMap first, then apply the Kargo Project, ProjectConfig, Warehouse, Stage,
and Argo CD Application. The ConfigMap must contain `JELLYFIN_URL`; keep its
cluster-specific endpoint and the API key out of this public repository.

The checked-in Argo CD Application is a bootstrap snapshot. Kargo updates its
live Git revision and image parameters directly during promotions. Reapplying
the bootstrap file resets the application to its recorded starting revision.

## CircleCI publishing credentials

Set `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as CircleCI project environment
variables. The token needs read/write access to `galonsky/video-downloader`.
The CircleCI job does not need Kubernetes or tailnet access.

See [`chart/video-downloader/README.md`](chart/video-downloader/README.md) for
standalone Helm installation and chart configuration details.
