# Video Downloader

This chart runs a simple local web UI backed by `yt-dlp`. It has no authentication; expose it only on a trusted host/network.

## Install

Build and make the image available to the local cluster (for example, load it into kind or minikube):

```sh
docker build -t video-downloader:latest .
helm upgrade --install video-downloader ./chart/video-downloader
```

The default deployment mounts the Kubernetes node's `/srv/media/videos` at the same path in the container and listens on host port `8080`. Open `http://localhost:8080` on that node.

Before installing, make the media and state directories writable by UID/GID `10001`:

```sh
sudo install -d -o 10001 -g 10001 /srv/media/videos /var/lib/video-downloader
```

Set `downloadDirectory.path` to change the in-container `DOWNLOAD_DIR`; set `downloadDirectory.hostPath` to the matching node directory. To use shared cluster storage, set `existingDownloadClaim` instead.

## Jellyfin refresh

After a completed download, the optional Jellyfin integration requests `POST /Library/Refresh`. It is debounced for 60 seconds by default; a refresh failure does not change a completed download to failed.

Create the API-key Secret without putting the secret in Helm values:

```sh
kubectl create secret generic jellyfin-api \
  --from-literal=JELLYFIN_API_KEY='YOUR_KEY'
```

Enable it with a values file or command-line settings. The URL can be supplied
directly, or read from an existing ConfigMap (useful for cluster-specific
endpoints that should not be stored in this repository):

```sh
helm upgrade --install video-downloader ./chart/video-downloader \
  --set image.tag=2026.8.19 \
  --set jellyfin.enabled=true \
  --set jellyfin.url=http://jellyfin.default.svc.cluster.local:8096 \
  --set jellyfin.existingSecret=jellyfin-api
```

For a ConfigMap named `video-downloader-config` with a `JELLYFIN_URL` key, set
`jellyfin.existingUrlConfigMap=video-downloader-config`. When configured, the
ConfigMap value takes precedence over `jellyfin.url`.

## Updating yt-dlp and Deno

The image pins both `yt-dlp` and Deno. Check for newer releases without changing files:

```sh
./scripts/update_dependencies.py --dry-run
```

Apply the update after reviewing the generated diff:

```sh
./scripts/update_dependencies.py
pytest
docker build -t video-downloader:latest .
```

The script updates the Dockerfile, the Helm image tag, and this example together. It also refreshes Deno's published SHA-256 checksum. Run it from cron or another scheduler when you want unattended checks; use `--check` for a non-writing status check.

## CircleCI image publishing

The CircleCI workflow builds the Dockerfile for commits to `main` and pushes
these tags to `galonsky/video-downloader`:

- `sha-<full-commit-sha>` is immutable and is the tag Kargo should promote.
- `latest` is a convenience tag; deployments should use the commit tag.

Create the `galonsky/video-downloader` repository on Docker Hub, then add these
project environment variables in CircleCI:

- `DOCKERHUB_USERNAME`: the Docker Hub account used to publish the image.
- `DOCKERHUB_TOKEN`: a Docker Hub access token with Repo Read & Write
  permission. Do not put the token in this repository.

The publisher only runs on `main`; it does not need access to the cluster or
tailnet.
