#!/usr/bin/env python3
import os
import json
import hashlib
import argparse
import requests
import tarfile
from pathlib import Path
from tqdm import tqdm
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

class DockerImageDownloader:
    def __init__(
        self,
        registry: str = "https://registry-1.docker.io",
        proxy: Optional[str] = None,
        verify_ssl: bool = True,
        max_workers: int = 5,
    ):
        self.registry = registry.rstrip("/")
        self.proxy = proxy
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.token = None
        self.max_workers = max_workers

        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

    def _get_token(self, repo: str) -> None:
        auth_url = "https://auth.docker.io/token"
        params = {
            "service": "registry.docker.io",
            "scope": f"repository:{repo}:pull"
        }
        try:
            response = self.session.get(
                auth_url,
                params=params,
                verify=self.verify_ssl,
                timeout=10
            )
            response.raise_for_status()
            self.token = response.json().get("token")
            if self.token:
                self.session.headers.update({"Authorization": f"Bearer {self.token}"})
        except Exception as e:
            print(f"[WARNING] Failed to get token: {e}")

    def _get_manifest_list(self, repo: str, tag: str) -> Dict:
        url = f"{self.registry}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": "application/vnd.docker.distribution.manifest.list.v2+json"}
        response = self.session.get(url, headers=headers, verify=self.verify_ssl, timeout=10)
        response.raise_for_status()
        return response

    def _get_manifest(self, repo: str, digest: str) -> Dict:
        url = f"{self.registry}/v2/{repo}/manifests/{digest}"
        headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
        response = self.session.get(url, headers=headers, verify=self.verify_ssl, timeout=10)
        response.raise_for_status()
        return response

    def _download_blob(self, repo: str, digest: str, expected_size: int, cache_dir: Path) -> Tuple[str, bytes]:
        digest_hash = digest.split(":", 1)[-1]
        cache_path = cache_dir / "blobs" / "sha256" / digest_hash

        # Check if already downloaded and complete
        if cache_path.exists() and cache_path.stat().st_size == expected_size:
            return (digest, cache_path.read_bytes())

        # Download from remote
        url = f"{self.registry}/v2/{repo}/blobs/{digest}"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        response = self.session.get(
            url,
            headers=headers,
            stream=True,
            verify=self.verify_ssl,
            timeout=30
        )
        response.raise_for_status()

        # Write chunks to file as they arrive
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded_bytes = 0
        with cache_path.open("wb") as f, tqdm(
            total=expected_size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"Downloading {digest[:12]}",
            leave=False,
        ) as pbar:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    pbar.update(len(chunk))

        # Verify size
        if downloaded_bytes != expected_size:
            raise ValueError(f"Size mismatch for {digest[:12]}: expected {expected_size}, got {downloaded_bytes}")

        return (digest, cache_path.read_bytes())

    def _batch_download_blobs(self, repo: str, blobs: List[Tuple[str, int]], cache_dir: Path) -> Dict[str, bytes]:
        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._download_blob, repo, digest, size, cache_dir): (digest, size)
                for digest, size in blobs
            }
            for future in as_completed(futures):
                digest, data = future.result()
                results[digest] = data
        return results

    @staticmethod
    def _normalize_repo(repo: str) -> str:
        if "/" not in repo:
            return f"library/{repo}"
        return repo

    def download_image(self, image: str, output_dir: str = ".") -> None:
        if ":" in image:
            repo, tag = image.split(":", 1)
        else:
            repo = image
            tag = "latest"

        repo = self._normalize_repo(repo)
        self._get_token(repo)

        cache_dir = Path(output_dir) / f"{repo.replace('/', '_')}_{tag}"
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            print(f"[1/4] Fetching manifest list for {image}...")
            manifest_list = self._get_manifest_list(repo, tag)
            manifests = manifest_list.json().get("manifests", [])

            target_manifest = None
            for m in manifests:
                if (
                    m.get("platform", {}).get("architecture") == "amd64"
                    and m.get("platform", {}).get("os") == "linux"
                ):
                    target_manifest = m
                    break

            if not target_manifest:
                try:
                    manifest = self._get_manifest(repo, tag)
                    config_digest = manifest["config"]["digest"]
                    config_size = manifest["config"]["size"]
                    layers = manifest.get("layers", [])
                    manifest_digest = None
                except Exception:
                    raise ValueError(f"No manifest found for platform: linux/amd64")
            else:
                manifest_digest = target_manifest["digest"]
                print(f"[2/4] Fetching platform-specific manifest: {manifest_digest[:12]}...")
                manifest = self._get_manifest(repo, manifest_digest)
                manifest_path = cache_dir / "blobs" / "sha256" / manifest_digest.split(":",1)[-1]
                if not manifest_path.exists():
                    manifest_path.parent.mkdir(parents=True, exist_ok=True)
                    manifest_path.write_bytes(manifest.content)
                config_digest = manifest.json()["config"]["digest"]
                config_size = manifest.json()["config"]["size"]
                layers = manifest.json().get("layers", [])

            print(f"[3/4] Downloading {len(layers) + 1} blobs in batch...")
            blobs_to_download = [(config_digest, config_size)]
            for layer in layers:
                blobs_to_download.append((layer["digest"], layer["size"]))

            downloaded_blobs = self._batch_download_blobs(repo, blobs_to_download, cache_dir)

            print("[4/4] Generating index.json, manifest.json, and oci-layout...")
            if manifest_digest:
                manifest_list_bytes = manifest_list.content
                manifest_list_digest = hashlib.sha256(manifest_list_bytes).hexdigest()
                manifest_list_sha = f"sha256:{manifest_list_digest}"

                manifest_list_path = cache_dir / "blobs" / "sha256" / manifest_list_digest
                if not manifest_list_path.exists():
                    manifest_list_path.parent.mkdir(parents=True, exist_ok=True)
                    manifest_list_path.write_bytes(manifest_list_bytes)

                index = {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {
                            "mediaType": "application/vnd.oci.image.index.v1+json",
                            "digest": manifest_list_sha,
                            "size": len(manifest_list_bytes),
                            "annotations": {
                                "io.containerd.image.name": "docker.io/" + repo + ":" + tag
                            }
                        }
                    ]
                }
                (cache_dir / "index.json").write_text(json.dumps(index, separators=(',', ':')), encoding="utf-8")
            else:
                manifest_bytes = json.dumps(manifest).encode("utf-8")
                manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
                manifest_sha = f"sha256:{manifest_digest}"

                manifest_path = cache_dir / "blobs" / "sha256" / manifest_digest
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                manifest_path.write_bytes(manifest_bytes)

                index = {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [
                        {
                            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                            "digest": manifest_sha,
                            "size": len(manifest_bytes),
                            "annotations": {
                                "org.opencontainers.image.ref.name": image
                            }
                        }
                    ]
                }
                (cache_dir / "index.json").write_text(json.dumps(index, separators=(',', ':')), encoding="utf-8")

            config_digest_hash = config_digest.split(":", 1)[-1]
            layer_digests = [layer["digest"].split(":", 1)[-1] for layer in layers]
            manifest_json = [
                {
                    "Config": f"blobs/sha256/{config_digest_hash}",  # 不带前缀
                    "RepoTags": [image],
                    "Layers": [f"blobs/sha256/{d}" for d in layer_digests],  # 不带前缀
                }
            ]
            (cache_dir / "manifest.json").write_text(json.dumps(manifest_json, separators=(',', ':')), encoding="utf-8")

            oci_layout = {"imageLayoutVersion": "1.0.0"}
            (cache_dir / "oci-layout").write_text(json.dumps(oci_layout, separators=(',', ':')), encoding="utf-8")

            print(f"\n[SUCCESS] Successfully saved to: {cache_dir.absolute()}")
            print("Directory structure:")
            print(f"  {cache_dir.name}/")
            print(f"    +-- index.json")
            print(f"    +-- manifest.json")
            print(f"    +-- oci-layout")
            print(f"    +-- blobs/")
            print(f"        +-- sha256/")
            if manifest_digest:
                print(f"            +-- {manifest_list_digest[:12]}... (manifest list)")
                print(f"            +-- {manifest_digest[:12]}... (manifest)")
            else:
                print(f"            +-- {manifest_digest[:12]}... (manifest)")
            print(f"            +-- {config_digest[:12]}... (config)")
            for layer in layers:
                print(f"            +-- {layer['digest'][:12]}... (layer)")
            docker_tar = f"{repo.replace('/', '_')}_{tag}.tar"
            docker_tar_path = Path(output_dir) / docker_tar
            with tarfile.open(docker_tar_path, "w") as tar:
                for name in os.listdir(cache_dir):
                    tar.add(os.path.join(cache_dir,name), arcname=name)
            print(f"\n[PACKAGED] Successfully packaged to: {docker_tar_path.absolute()}")
        except Exception as e:
            print(f"\n[ERROR] Failed to download image: {e}")
            raise

def main():
    parser = argparse.ArgumentParser(description="Download Docker image with OCI layout (batch download)")
    parser.add_argument("image", help="Image name with tag (e.g., nginx:13.6.2, hello-world)")
    parser.add_argument("-o", "--output", default=".", help="Output directory (default: .)")
    parser.add_argument("--proxy", help="HTTP/HTTPS proxy (e.g., http://127.0.0.1:10808)")
    parser.add_argument("--no-verify", action="store_false", dest="verify_ssl", help="Disable SSL verification")
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent downloads (default: 5)")

    args = parser.parse_args()

    downloader = DockerImageDownloader(
        proxy=args.proxy,
        verify_ssl=args.verify_ssl,
        max_workers=args.workers,
    )

    try:
        downloader.download_image(
            image=args.image,
            output_dir=args.output,
        )
    except Exception as e:
        print(f"[ERROR] Failed to download image: {e}")
        exit(1)

if __name__ == "__main__":
    main()
