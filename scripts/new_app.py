#!/usr/bin/env python3
"""Interactive wizard for scaffolding new Flux apps."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable, Optional


ROOT_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT_DIR / "k8s" / "coma" / "apps"
FLUX_EXTRAS_DIR = APPS_DIR / "flux-system-extras"
FLUX_EXTRAS_KUSTOMIZATION = FLUX_EXTRAS_DIR / "kustomization.yaml"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_with_default(prompt: str, default: str) -> str:
    response = ask(f"{prompt} [{default}]: ")
    return response or default


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        response = ask(f"{prompt}{suffix}: ").lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Please answer yes or no.", file=sys.stderr)


def require_unique_directory(path: Path) -> None:
    if path.exists():
        print(f"Error: {path} already exists.", file=sys.stderr)
        sys.exit(1)


def prompt_until_valid(
    prompt: str,
    *,
    transform: Optional[Callable[[str], str]] = None,
    error_message: str = "Value is required.",
) -> str:
    while True:
        value = ask(prompt)
        if transform is not None:
            value = transform(value)
        if value:
            return value
        print(error_message, file=sys.stderr)


def normalise_repo_kind(value: str) -> str:
    value = value.strip()
    if not value:
        return "HelmRepository"
    lowered = value.lower()
    if lowered == "helmrepository":
        return "HelmRepository"
    if lowered == "ocirepository":
        return "OCIRepository"
    raise ValueError(f"Unsupported repository kind: {value}")


def ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def update_flux_extras_kustomization(repo_slug: str) -> bool:
    if not FLUX_EXTRAS_KUSTOMIZATION.exists():
        print(
            f"Warning: kustomization file not found at {FLUX_EXTRAS_KUSTOMIZATION}",
            file=sys.stderr,
        )
        return False

    entry = f"  - ./helm-repos/{repo_slug}.yaml"
    lines = FLUX_EXTRAS_KUSTOMIZATION.read_text().splitlines()
    if entry in lines:
        return False

    insert_index = len(lines)
    for idx, line in enumerate(lines):
        if line.strip().startswith("- ./image-repos/"):
            insert_index = idx
            break

    while insert_index > 0 and lines[insert_index - 1].strip() == "":
        insert_index -= 1

    lines.insert(insert_index, entry)
    FLUX_EXTRAS_KUSTOMIZATION.write_text(ensure_trailing_newline("\n".join(lines)))
    return True


def write_files(
    *,
    app_name: str,
    namespace: str,
    create_helm_release: bool,
    chart_name: str,
    helm_repo_name: str,
    helm_repo_namespace: str,
    chart_version: str,
    helm_interval: str,
    add_repo: bool,
    repo_metadata_name: str,
    repo_file_slug: str,
    repo_kind: str,
    repo_interval: str,
    repo_url: str,
    repo_tag: str,
) -> list[Path]:
    created: list[Path] = []

    app_dir = APPS_DIR / app_name
    app_dir.mkdir(parents=True)
    app_subdir = app_dir / "app"
    app_subdir.mkdir(exist_ok=True)

    ns_file = app_dir / "ns.yaml"
    ns_content = f"""---
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""
    ns_file.write_text(ensure_trailing_newline(ns_content))
    created.append(ns_file)

    ks_file = app_dir / "ks.yaml"
    ks_content = f"""---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app {app_name}
  namespace: flux-system
spec:
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  interval: 1h
  path: ./k8s/coma/apps/{app_name}
  postBuild:
    substitute:
      APP: *app
      NAMESPACE: &namespace {namespace}
    substituteFrom:
      - kind: Secret
        name: cluster-secrets
        optional: false
  prune: true
  retryInterval: 2m
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system
  targetNamespace: *namespace
  timeout: 5m
  wait: false
  dependsOn:
    - name: secrets
      namespace: flux-system
"""
    ks_file.write_text(ensure_trailing_newline(ks_content))
    created.append(ks_file)

    kustomization_file = app_dir / "kustomization.yaml"
    resources = ["  - ./ns.yaml"]
    if create_helm_release:
        resources.append("  - ./app/helmrelease.yaml")
    resources_block = "\n".join(resources)
    kustomization_content = (
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        f"{resources_block}\n\n"
        "# Add additional manifests under ./app and include them above as needed.\n"
    )
    kustomization_file.write_text(ensure_trailing_newline(kustomization_content))
    created.append(kustomization_file)

    if create_helm_release:
        hr_file = app_subdir / "helmrelease.yaml"
        hr_lines = [
            "apiVersion: helm.toolkit.fluxcd.io/v2",
            "kind: HelmRelease",
            "metadata:",
            "  name: ${APP}",
            "  namespace: ${NAMESPACE}",
            "spec:",
            "  chart:",
            "    spec:",
            f"      chart: {chart_name}",
            "      reconcileStrategy: ChartVersion",
            "      sourceRef:",
            "        kind: HelmRepository",
            f"        name: {helm_repo_name}",
            f"        namespace: {helm_repo_namespace}",
        ]
        if chart_version:
            hr_lines.append(f"      version: {chart_version}")
        hr_lines.extend(
            [
                f"  interval: {helm_interval}",
            ]
        )
        if chart_name == "app-template":
            hr_lines.extend(
                [
                    "  values:",
                    "    controllers:",
                    "      main:",
                    "        replicas: 1",
                    "        strategy: RollingUpdate",
                    "        containers:",
                    "          main:",
                    "            image:",
                    "              repository: ghcr.io/REPLACE_ME/IMAGE",
                    "              tag: latest",
                    "            env:",
                    "              TZ: Australia/Adelaide",
                    "    service:",
                    "      main:",
                    "        controller: main",
                    "        ports:",
                    "          http:",
                    "            port: 8080",
                    "    ingress:",
                    "      main:",
                    "        enabled: false",
                ]
            )
        else:
            hr_lines.append("  values: {}")
        hr_file.write_text(ensure_trailing_newline("\n".join(hr_lines)))
        created.append(hr_file)
    else:
        gitkeep = app_subdir / ".gitkeep"
        gitkeep.write_text("")
        created.append(gitkeep)

    if add_repo:
        helm_repo_dir = FLUX_EXTRAS_DIR / "helm-repos"
        helm_repo_dir.mkdir(parents=True, exist_ok=True)
        repo_file = helm_repo_dir / f"{repo_file_slug}.yaml"
        repo_created = False
        if repo_file.exists():
            print(f"Repository file {repo_file} already exists, skipping creation.", file=sys.stderr)
        else:
            if repo_kind == "OCIRepository":
                repo_content = f"""apiVersion: source.toolkit.fluxcd.io/v1beta2
kind: OCIRepository
metadata:
  name: {repo_metadata_name}
  namespace: flux-system
spec:
  interval: {repo_interval}
  url: {repo_url}
  ref:
    tag: {repo_tag}
"""
            else:
                repo_content = f"""apiVersion: source.toolkit.fluxcd.io/v1beta2
kind: HelmRepository
metadata:
  name: {repo_metadata_name}
  namespace: flux-system
spec:
  interval: {repo_interval}
  url: {repo_url}
"""
            repo_file.write_text(ensure_trailing_newline(repo_content))
            repo_created = True
        if repo_created:
            created.append(repo_file)
        if update_flux_extras_kustomization(repo_file_slug):
            created.append(FLUX_EXTRAS_KUSTOMIZATION)

    return created


def main() -> int:
    if not APPS_DIR.exists():
        print(f"Error: expected apps directory at {APPS_DIR}", file=sys.stderr)
        return 1

    print("--- Flux App Scaffolding Wizard ---")

    app_name = prompt_until_valid(
        "App name (kebab-case): ",
        transform=lambda value: slugify(value or ""),
        error_message="App name is required.",
    )

    app_dir = APPS_DIR / app_name
    require_unique_directory(app_dir)

    namespace = prompt_until_valid(
        f"Namespace [{app_name}]: ",
        transform=lambda value: slugify(value or app_name),
        error_message="Namespace is required.",
    )

    create_helm_release = confirm("Create HelmRelease?", default=True)

    chart_name = ""
    helm_repo_name = ""
    helm_repo_namespace = "flux-system"
    chart_version = ""
    helm_interval = "1m0s"

    if create_helm_release:
        chart_name = ask_with_default("Chart name", "app-template").strip()
        helm_repo_name = ask_with_default("HelmRepository name", "bjw-s").strip()
        helm_repo_namespace = ask_with_default("HelmRepository namespace", "flux-system").strip()
        chart_version = ask("Chart version (leave blank to omit): ")
        helm_interval = ask_with_default("HelmRelease interval", "1m0s").strip()

    add_repo = confirm(
        "Add HelmRepository/OCIRepository to flux-system-extras?", default=False
    )

    repo_metadata_name = ""
    repo_file_slug = ""
    repo_kind = "HelmRepository"
    repo_interval = ""
    repo_url = ""
    repo_tag = ""

    if add_repo:
        default_repo_meta = helm_repo_name or "example"
        repo_metadata_name = ask_with_default("Repository metadata.name", default_repo_meta)
        repo_file_slug = prompt_until_valid(
            f"Repository file name (kebab-case) [{slugify(repo_metadata_name)}]: ",
            transform=lambda value: slugify(value or repo_metadata_name),
            error_message="Repository file name is required.",
        )
        while True:
            try:
                repo_kind = normalise_repo_kind(
                    ask_with_default("Repository kind [HelmRepository/OCIRepository]", "HelmRepository")
                )
                break
            except ValueError as exc:
                print(exc, file=sys.stderr)
        default_interval = "5m0s" if repo_kind == "OCIRepository" else "2h"
        repo_interval = ask_with_default("Repository interval", default_interval)
        repo_url = prompt_until_valid(
            "Repository URL: ",
            transform=str.strip,
            error_message="Repository URL is required.",
        )
        if repo_kind == "OCIRepository":
            repo_tag = ask_with_default("OCI tag", "latest")

    if not create_helm_release and not add_repo:
        print(
            "Warning: no HelmRelease and no Helm repository selected. Generating basic namespace scaffolding.",
            file=sys.stderr,
        )

    created_paths = write_files(
        app_name=app_name,
        namespace=namespace,
        create_helm_release=create_helm_release,
        chart_name=chart_name,
        helm_repo_name=helm_repo_name,
        helm_repo_namespace=helm_repo_namespace,
        chart_version=chart_version,
        helm_interval=helm_interval,
        add_repo=add_repo,
        repo_metadata_name=repo_metadata_name,
        repo_file_slug=repo_file_slug,
        repo_kind=repo_kind,
        repo_interval=repo_interval,
        repo_url=repo_url,
        repo_tag=repo_tag,
    )

    print("\nDone! Files created:")
    for path in created_paths:
        print(f"  - {path.relative_to(ROOT_DIR)}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        raise SystemExit(1)