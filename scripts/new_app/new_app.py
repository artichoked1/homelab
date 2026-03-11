#!/usr/bin/env python3
"""Interactive wizard for scaffolding new Flux apps with Jinja2 templates."""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

try:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError, TemplateNotFound
except ImportError:
    print(
        "Error: missing dependency 'jinja2'. Install it with: pip install jinja2",
        file=sys.stderr,
    )
    raise SystemExit(2)


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
K8S_DIR = ROOT_DIR / "k8s"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

DURATION_RE = re.compile(r"^(?:\d+(?:ns|us|ms|s|m|h))+$")


class ScaffoldError(RuntimeError):
    """Raised when scaffolding cannot continue safely."""


class UserAbort(RuntimeError):
    """Raised when user cancels the wizard."""


class Ansi:
    def __init__(self) -> None:
        self.enabled = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    def wrap(self, text: str, *codes: str) -> str:
        if not self.enabled:
            return text
        joined = ";".join(codes)
        return f"\033[{joined}m{text}\033[0m"

    def heading(self, text: str) -> str:
        return self.wrap(text, "1", "36")

    def prompt(self, text: str) -> str:
        return self.wrap(text, "1", "35")

    def info(self, text: str) -> str:
        return self.wrap(text, "34")

    def warn(self, text: str) -> str:
        return self.wrap(text, "33")

    def error(self, text: str) -> str:
        return self.wrap(text, "31")

    def success(self, text: str) -> str:
        return self.wrap(text, "32")

    def dim(self, text: str) -> str:
        return self.wrap(text, "2")

    def accent(self, text: str) -> str:
        return self.wrap(text, "36")

    def label(self, text: str) -> str:
        return self.wrap(text, "1", "37")


class Console:
    def __init__(self) -> None:
        self.ansi = Ansi()

    def heading(self, message: str) -> None:
        print(self.ansi.heading(message))

    def info(self, message: str) -> None:
        print(f"{self.ansi.info('[INFO]')} {message}")

    def warn(self, message: str) -> None:
        print(f"{self.ansi.warn('[WARN]')} {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"{self.ansi.error('[ERROR]')} {message}", file=sys.stderr)

    def success(self, message: str) -> None:
        print(f"{self.ansi.success('[OK]')} {message}")

    def kv(self, label: str, value: str, *, indent: int = 2) -> None:
        print(f"{' ' * indent}{self.ansi.label(f'{label}:')} {self.ansi.accent(value)}")

    def kv_bool(self, label: str, value: bool, *, indent: int = 2) -> None:
        state = self.ansi.success("yes") if value else self.ansi.dim("no")
        print(f"{' ' * indent}{self.ansi.label(f'{label}:')} {state}")

    def list_item(self, value: str, *, indent: int = 2) -> None:
        bullet = self.ansi.dim("-")
        print(f"{' ' * indent}{bullet} {self.ansi.accent(value)}")

    def prompt(self, message: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            return input(f"{self.ansi.prompt('?')} {message}{suffix}: ").strip()
        except EOFError:
            return ""


class TemplateRenderer:
    def __init__(self, template_dir: Path) -> None:
        if not template_dir.exists():
            raise ScaffoldError(f"Template directory not found: {template_dir}")

        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )

    def render(self, template_name: str, **context: object) -> str:
        try:
            rendered = self._env.get_template(template_name).render(**context)
        except (TemplateError, TemplateNotFound) as exc:
            raise ScaffoldError(f"Failed to render template '{template_name}': {exc}") from exc
        return ensure_trailing_newline(rendered)


@dataclass
class HelmReleaseConfig:
    enabled: bool = False
    chart_name: str = ""
    source_ref_name: str = ""
    source_ref_namespace: str = "flux-system"
    source_ref_kind: str = "HelmRepository"
    chart_version: str = ""
    interval: str = "1m0s"
    profile: str = "generic"
    service_port: int = 8080
    image_repository: str = "ghcr.io/REPLACE_ME/IMAGE"
    image_tag: str = "latest"
    timezone: str = "Australia/Adelaide"
    include_service_monitor: bool = False


@dataclass
class RepoConfig:
    enabled: bool = False
    metadata_name: str = ""
    file_slug: str = ""
    kind: str = "HelmRepository"
    interval: str = "2h"
    url: str = ""
    tag: str = ""
    overwrite_existing_file: bool = False


@dataclass(frozen=True)
class ClusterPaths:
    cluster_name: str
    cluster_dir: Path
    apps_dir: Path
    flux_extras_dir: Path
    flux_extras_kustomization: Path


@dataclass
class ScaffoldConfig:
    cluster_name: str
    app_path: str
    app_name: str
    namespace: str
    create_namespace_manifest: bool
    helm: HelmReleaseConfig
    repo: RepoConfig


@dataclass
class ScaffoldResult:
    created: list[Path]
    updated: list[Path]
    skipped: list[Path]


def ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def slugify_segment(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def normalize_app_path(value: str) -> str:
    raw_parts = [part for part in value.strip().split("/") if part.strip()]
    normalized_parts = [slugify_segment(part) for part in raw_parts]
    normalized_parts = [part for part in normalized_parts if part]
    if not normalized_parts:
        raise ValueError("App path is required. Example: media/slskd")
    return "/".join(normalized_parts)


def normalize_namespace(value: str) -> str:
    normalized = slugify_segment(value)
    if not normalized:
        raise ValueError("Namespace is required and must contain letters or numbers.")
    return normalized


def validate_duration(value: str) -> None:
    if not DURATION_RE.match(value):
        raise ValueError("Invalid duration. Use Go-style durations like 1m0s, 2h, 90s.")


def validate_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError("Port must be a number.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def validate_repo_url(kind: str, value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Repository URL is required.")

    if kind == "OCIRepository":
        if not candidate.startswith("oci://"):
            raise ValueError("OCIRepository URL must start with oci://")
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HelmRepository URL must be a valid http:// or https:// URL.")
    return candidate


def normalize_repo_kind(value: str) -> str:
    lowered = value.strip().lower()
    if lowered == "helmrepository":
        return "HelmRepository"
    if lowered == "ocirepository":
        return "OCIRepository"
    raise ValueError("Repository kind must be HelmRepository or OCIRepository.")


def default_repo_name_for_chart(chart_name: str) -> str:
    known = {
        "app-template": "bjw-s",
        "external-secrets": "external-secrets",
    }
    return known.get(chart_name.strip().lower(), "example")


def detect_chart_profile(chart_name: str) -> str:
    lowered = chart_name.strip().lower()
    if lowered == "app-template":
        return "app-template"
    if lowered == "external-secrets":
        return "external-secrets"
    return "generic"


def find_namespace_manifest(directory: Path) -> Path | None:
    for filename in ("ns.yaml", "namespace.yaml"):
        candidate = directory / filename
        if candidate.exists():
            return candidate
    return None


def parse_namespace_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = text.splitlines()
    in_metadata = False
    for line in lines:
        stripped = line.strip()
        if stripped == "metadata:":
            in_metadata = True
            continue
        if in_metadata:
            if stripped and not line.startswith(" "):
                in_metadata = False
                continue
            match = re.match(r"\s*name:\s*([^\s#]+)", line)
            if match:
                name = match.group(1).strip().strip('"\'')
                if "${" in name:
                    return None
                return slugify_segment(name)
    return None


def discover_clusters(k8s_dir: Path) -> list[str]:
    if not k8s_dir.exists():
        return []

    names = [entry.name for entry in k8s_dir.iterdir() if entry.is_dir()]
    return sorted(names, key=lambda item: (item != "coma", item))


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scaffold a Flux app directory structure.")
    parser.add_argument(
        "--cluster",
        help="Target cluster under k8s/. If omitted, an interactive selector is shown.",
    )
    return parser.parse_args(argv)


def build_cluster_paths(cluster_name: str) -> ClusterPaths:
    cluster_dir = K8S_DIR / cluster_name
    apps_dir = cluster_dir / "apps"
    flux_extras_dir = apps_dir / "flux-system-extras"
    flux_extras_kustomization = flux_extras_dir / "kustomization.yaml"
    return ClusterPaths(
        cluster_name=cluster_name,
        cluster_dir=cluster_dir,
        apps_dir=apps_dir,
        flux_extras_dir=flux_extras_dir,
        flux_extras_kustomization=flux_extras_kustomization,
    )


def suggest_namespace(app_path: str, apps_dir: Path) -> str:
    parts = app_path.split("/")
    if len(parts) == 1:
        return parts[0]

    group = parts[0]
    group_dir = apps_dir / group
    manifest = find_namespace_manifest(group_dir)
    if manifest is None:
        return group

    discovered = parse_namespace_name(manifest)
    if discovered:
        return discovered
    return group


def suggest_namespace_manifest(app_path: str, namespace: str, apps_dir: Path) -> bool:
    parts = app_path.split("/")
    if len(parts) == 1:
        return True

    group_dir = apps_dir / parts[0]
    manifest = find_namespace_manifest(group_dir)
    if manifest is None:
        return True

    existing_namespace = parse_namespace_name(manifest)
    return existing_namespace != namespace


def build_repo_index(flux_extras_dir: Path) -> dict[str, tuple[str, Path]]:
    repo_dir = flux_extras_dir / "helm-repos"
    index: dict[str, tuple[str, Path]] = {}

    if not repo_dir.exists():
        return index

    for repo_file in sorted(repo_dir.glob("*.yaml")):
        try:
            lines = repo_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        kind = ""
        name = ""
        in_metadata = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("kind:") and not kind:
                kind = stripped.split(":", 1)[1].strip()
                continue
            if stripped == "metadata:":
                in_metadata = True
                continue
            if in_metadata:
                if stripped and not line.startswith(" "):
                    in_metadata = False
                    continue
                match = re.match(r"\s*name:\s*([^\s#]+)", line)
                if match:
                    name = match.group(1).strip().strip('"\'')
                    break

        if name:
            index[name] = (kind or "Unknown", repo_file)

    return index


def prompt_until_valid(
    console: Console,
    message: str,
    *,
    default: str | None = None,
    transform: Callable[[str], str] | None = None,
    validator: Callable[[str], None] | None = None,
    required: bool = True,
    empty_error: str = "Value is required.",
) -> str:
    while True:
        raw = console.prompt(message, default)
        if not raw and default is not None:
            raw = default

        value = raw.strip()
        if transform is not None:
            try:
                value = transform(value)
            except ValueError as exc:
                console.error(str(exc))
                continue

        if required and not value:
            console.error(empty_error)
            continue

        if validator is not None and value:
            try:
                validator(value)
            except ValueError as exc:
                console.error(str(exc))
                continue

        return value


def prompt_yes_no(console: Console, message: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"

    while True:
        response = console.prompt(f"{message} [{suffix}]").lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        console.error("Please answer yes or no.")


def prompt_choice(
    console: Console,
    message: str,
    choices: Iterable[str],
    *,
    default: str,
) -> str:
    choices_list = list(choices)
    normalized = {choice.lower(): choice for choice in choices_list}
    default_idx = choices_list.index(default) + 1

    console.info(message)
    for idx, choice in enumerate(choices_list, start=1):
        print(f"  {console.ansi.dim(str(idx) + '.')} {console.ansi.accent(choice)}")

    while True:
        raw = console.prompt("Select option", str(default_idx)).strip()
        if not raw:
            return default
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(choices_list):
                return choices_list[idx - 1]
        candidate = normalized.get(raw.lower())
        if candidate:
            return candidate
        console.error("Choose a valid option number or name.")


def update_flux_extras_kustomization(
    repo_slug: str,
    backups: dict[Path, str],
    flux_extras_kustomization: Path,
) -> bool:
    if not flux_extras_kustomization.exists():
        return False

    entry = f"  - ./helm-repos/{repo_slug}.yaml"
    lines = flux_extras_kustomization.read_text(encoding="utf-8").splitlines()
    if entry in lines:
        return False

    insert_index = len(lines)
    for idx, line in enumerate(lines):
        if line.strip().startswith("- ./image-repos/"):
            insert_index = idx
            break

    while insert_index > 0 and lines[insert_index - 1].strip() == "":
        insert_index -= 1

    if flux_extras_kustomization not in backups:
        backups[flux_extras_kustomization] = flux_extras_kustomization.read_text(encoding="utf-8")

    lines.insert(insert_index, entry)
    flux_extras_kustomization.write_text(ensure_trailing_newline("\n".join(lines)), encoding="utf-8")
    return True


def write_text_file(
    path: Path,
    content: str,
    created: list[Path],
    updated: list[Path],
    backups: dict[Path, str],
    *,
    allow_overwrite: bool = False,
) -> None:
    existed = path.exists()
    if existed and not allow_overwrite:
        raise ScaffoldError(f"Refusing to overwrite existing file: {path}")

    if existed and path not in backups:
        backups[path] = path.read_text(encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ensure_trailing_newline(content), encoding="utf-8")

    if existed:
        if path not in updated:
            updated.append(path)
    else:
        created.append(path)


def rollback_changes(
    created: list[Path],
    created_dirs: list[Path],
    backups: dict[Path, str],
) -> None:
    for path in reversed(created):
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    for path, content in backups.items():
        try:
            path.write_text(content, encoding="utf-8")
        except OSError:
            pass

    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue


def collect_config(
    console: Console,
    cluster_paths: ClusterPaths,
    repo_index: dict[str, tuple[str, Path]],
) -> ScaffoldConfig:
    console.heading("=== Flux App Scaffolding Wizard ===")

    console.info(f"Selected cluster: {cluster_paths.cluster_name}")
    app_path = prompt_until_valid(
        console,
        f"App path under k8s/{cluster_paths.cluster_name}/apps (example: media/slskd)",
        transform=normalize_app_path,
        empty_error="App path is required.",
    )

    app_dir = cluster_paths.apps_dir / app_path
    if app_dir.exists():
        raise ScaffoldError(f"App directory already exists: {app_dir}")

    app_name = app_path.split("/")[-1]
    namespace_default = suggest_namespace(app_path, cluster_paths.apps_dir)
    namespace = prompt_until_valid(
        console,
        "Namespace",
        default=namespace_default,
        transform=normalize_namespace,
        empty_error="Namespace is required.",
    )

    create_namespace_manifest = prompt_yes_no(
        console,
        "Create namespace manifest in this app directory?",
        default=suggest_namespace_manifest(app_path, namespace, cluster_paths.apps_dir),
    )

    create_helm_release = prompt_yes_no(console, "Create HelmRelease?", default=True)
    helm = HelmReleaseConfig(enabled=create_helm_release)

    if create_helm_release:
        chart_name = prompt_until_valid(console, "Chart name", default="app-template")
        profile = detect_chart_profile(chart_name)

        source_ref_kind = prompt_choice(
            console,
            "Chart source kind:",
            ("HelmRepository", "OCIRepository"),
            default="HelmRepository",
        )

        source_ref_name = prompt_until_valid(
            console,
            "Source repository name",
            default=default_repo_name_for_chart(chart_name),
        )

        source_ref_namespace = prompt_until_valid(
            console,
            "Source repository namespace",
            default="flux-system",
        )

        chart_version = console.prompt("Chart version (blank to omit)").strip()

        interval = prompt_until_valid(
            console,
            "HelmRelease interval",
            default="1m0s",
            validator=validate_duration,
        )

        helm = HelmReleaseConfig(
            enabled=True,
            chart_name=chart_name,
            source_ref_name=source_ref_name,
            source_ref_namespace=source_ref_namespace,
            source_ref_kind=source_ref_kind,
            chart_version=chart_version,
            interval=interval,
            profile=profile,
        )

        if profile == "app-template":
            image_repository_default = f"ghcr.io/REPLACE_ME/{app_name}"
            helm.image_repository = prompt_until_valid(
                console,
                "Container image repository",
                default=image_repository_default,
            )
            helm.image_tag = prompt_until_valid(console, "Container image tag", default="latest")
            service_port = prompt_until_valid(
                console,
                "Service port",
                default="8080",
                validator=lambda value: validate_port(value),
            )
            helm.service_port = int(service_port)
            helm.timezone = prompt_until_valid(console, "Timezone env value", default="Australia/Adelaide")
            helm.include_service_monitor = prompt_yes_no(
                console,
                "Add serviceMonitor scaffold in values?",
                default=False,
            )

    add_repo_default = False
    if helm.enabled:
        if helm.source_ref_name in repo_index:
            kind, repo_file = repo_index[helm.source_ref_name]
            console.info(
                f"Found existing repo '{helm.source_ref_name}' ({kind}) at "
                f"{repo_file.relative_to(ROOT_DIR)}"
            )
            add_repo_default = False
        else:
            console.warn(
                f"Repository '{helm.source_ref_name}' was not found under "
                f"k8s/{cluster_paths.cluster_name}/apps/flux-system-extras/helm-repos."
            )
            add_repo_default = True

    add_repo = prompt_yes_no(
        console,
        "Add repository manifest to flux-system-extras/helm-repos?",
        default=add_repo_default,
    )

    repo = RepoConfig(enabled=add_repo)
    if add_repo:
        metadata_name_default = helm.source_ref_name or app_name
        metadata_name = prompt_until_valid(console, "Repository metadata.name", default=metadata_name_default)

        default_slug = slugify_segment(metadata_name)
        file_slug = prompt_until_valid(
            console,
            "Repository file slug",
            default=default_slug,
            transform=slugify_segment,
            empty_error="Repository file slug is required.",
        )

        kind_default = helm.source_ref_kind if helm.enabled else "HelmRepository"
        kind = prompt_choice(
            console,
            "Repository kind:",
            ("HelmRepository", "OCIRepository"),
            default=kind_default,
        )

        if helm.enabled and kind != helm.source_ref_kind:
            console.warn(
                "Repository kind differs from HelmRelease sourceRef kind. "
                f"Using {kind} for sourceRef kind."
            )
            helm.source_ref_kind = kind

        interval = prompt_until_valid(
            console,
            "Repository interval",
            default="5m0s" if kind == "OCIRepository" else "2h",
            validator=validate_duration,
        )

        repo_url = prompt_until_valid(
            console,
            "Repository URL",
            validator=lambda value: validate_repo_url(kind, value),
        )

        tag = ""
        if kind == "OCIRepository":
            tag_default = helm.chart_version or "latest"
            tag = prompt_until_valid(console, "OCI tag", default=tag_default)

        repo_file = cluster_paths.flux_extras_dir / "helm-repos" / f"{file_slug}.yaml"
        overwrite = False
        if repo_file.exists():
            overwrite = prompt_yes_no(
                console,
                f"Repository file {repo_file.relative_to(ROOT_DIR)} already exists. Overwrite it?",
                default=False,
            )
            if not overwrite:
                console.warn("Existing repository file will be kept as-is.")

        repo = RepoConfig(
            enabled=True,
            metadata_name=metadata_name,
            file_slug=file_slug,
            kind=normalize_repo_kind(kind),
            interval=interval,
            url=repo_url,
            tag=tag,
            overwrite_existing_file=overwrite,
        )

    if not create_namespace_manifest and not helm.enabled:
        console.warn("No namespace and no HelmRelease selected. Enabling namespace manifest to avoid empty scaffold.")
        create_namespace_manifest = True

    config = ScaffoldConfig(
        cluster_name=cluster_paths.cluster_name,
        app_path=app_path,
        app_name=app_name,
        namespace=namespace,
        create_namespace_manifest=create_namespace_manifest,
        helm=helm,
        repo=repo,
    )

    print()
    console.heading("Planned changes")
    console.kv("cluster", config.cluster_name)
    console.kv("app path", f"k8s/{config.cluster_name}/apps/{config.app_path}")
    console.kv("app name", config.app_name)
    console.kv("namespace", config.namespace)
    console.kv_bool("namespace manifest", config.create_namespace_manifest)
    console.kv_bool("helmrelease", config.helm.enabled)
    if config.helm.enabled:
        console.kv("chart", config.helm.chart_name, indent=4)
        console.kv(
            "sourceRef",
            f"{config.helm.source_ref_kind}/{config.helm.source_ref_name} "
            f"(ns={config.helm.source_ref_namespace})",
            indent=4,
        )
        console.kv("profile", config.helm.profile, indent=4)
    console.kv_bool("repo manifest", config.repo.enabled)
    if config.repo.enabled:
        console.kv(
            "file",
            f"k8s/{config.cluster_name}/apps/flux-system-extras/helm-repos/{config.repo.file_slug}.yaml",
            indent=4,
        )
        console.kv("kind", config.repo.kind, indent=4)

    if not prompt_yes_no(console, "Proceed with file generation?", default=True):
        raise UserAbort("Aborted by user.")

    return config


def scaffold(
    config: ScaffoldConfig,
    cluster_paths: ClusterPaths,
    renderer: TemplateRenderer,
    console: Console,
) -> ScaffoldResult:
    created: list[Path] = []
    updated: list[Path] = []
    skipped: list[Path] = []
    created_dirs: list[Path] = []
    backups: dict[Path, str] = {}

    app_dir = cluster_paths.apps_dir / config.app_path
    app_subdir = app_dir / "app"

    try:
        app_dir.mkdir(parents=True, exist_ok=False)
        created_dirs.append(app_dir)

        app_subdir.mkdir(parents=True, exist_ok=False)
        created_dirs.append(app_subdir)

        if config.create_namespace_manifest:
            ns_content = renderer.render("namespace.yaml.j2", namespace=config.namespace)
            write_text_file(app_dir / "ns.yaml", ns_content, created, updated, backups)

        ks_content = renderer.render(
            "ks.yaml.j2",
            cluster_name=config.cluster_name,
            app_name=config.app_name,
            app_path=config.app_path,
            namespace=config.namespace,
            include_postbuild=config.helm.enabled,
        )
        write_text_file(app_dir / "ks.yaml", ks_content, created, updated, backups)

        resources: list[str] = []
        if config.create_namespace_manifest:
            resources.append("./ns.yaml")
        if config.helm.enabled:
            resources.append("./app/helmrelease.yaml")

        app_kustomization_content = renderer.render("kustomization.yaml.j2", resources=resources)
        write_text_file(app_dir / "kustomization.yaml", app_kustomization_content, created, updated, backups)

        if config.helm.enabled:
            hr_content = renderer.render(
                "helmrelease.yaml.j2",
                app_name=config.app_name,
                chart_name=config.helm.chart_name,
                source_ref_kind=config.helm.source_ref_kind,
                source_ref_name=config.helm.source_ref_name,
                source_ref_namespace=config.helm.source_ref_namespace,
                chart_version=config.helm.chart_version,
                interval=config.helm.interval,
                profile=config.helm.profile,
                image_repository=config.helm.image_repository,
                image_tag=config.helm.image_tag,
                timezone=config.helm.timezone,
                service_port=config.helm.service_port,
                include_service_monitor=config.helm.include_service_monitor,
            )
            write_text_file(app_subdir / "helmrelease.yaml", hr_content, created, updated, backups)
        else:
            gitkeep = app_subdir / ".gitkeep"
            write_text_file(gitkeep, "", created, updated, backups)

        if config.repo.enabled:
            repo_dir = cluster_paths.flux_extras_dir / "helm-repos"
            repo_dir.mkdir(parents=True, exist_ok=True)

            repo_file = repo_dir / f"{config.repo.file_slug}.yaml"
            should_write_repo = config.repo.overwrite_existing_file or not repo_file.exists()

            if should_write_repo:
                template_name = (
                    "ocirepository.yaml.j2" if config.repo.kind == "OCIRepository" else "helmrepository.yaml.j2"
                )
                repo_content = renderer.render(
                    template_name,
                    metadata_name=config.repo.metadata_name,
                    interval=config.repo.interval,
                    url=config.repo.url,
                    tag=config.repo.tag,
                )
                write_text_file(
                    repo_file,
                    repo_content,
                    created,
                    updated,
                    backups,
                    allow_overwrite=config.repo.overwrite_existing_file,
                )
            else:
                skipped.append(repo_file)

            if update_flux_extras_kustomization(
                config.repo.file_slug,
                backups,
                cluster_paths.flux_extras_kustomization,
            ):
                updated.append(cluster_paths.flux_extras_kustomization)

    except Exception:
        rollback_changes(created, created_dirs, backups)
        raise

    console.success("Scaffold complete.")
    return ScaffoldResult(created=created, updated=updated, skipped=skipped)


def main(argv: list[str] | None = None) -> int:
    console = Console()

    if not K8S_DIR.exists():
        console.error(f"Expected clusters directory at {K8S_DIR}")
        return 1

    try:
        args = parse_cli_args(argv)
        renderer = TemplateRenderer(TEMPLATE_DIR)
        clusters = discover_clusters(K8S_DIR)
        if not clusters:
            raise ScaffoldError(f"No cluster directories found under {K8S_DIR}")

        if args.cluster:
            cluster_name = args.cluster.strip()
            if cluster_name not in clusters:
                available = ", ".join(clusters)
                raise ScaffoldError(
                    f"Unknown cluster '{cluster_name}'. Available clusters: {available}"
                )
            console.info(f"Using cluster from --cluster: {cluster_name}")
        else:
            cluster_name = prompt_choice(
                console,
                "Select target cluster:",
                clusters,
                default=clusters[0],
            )

        cluster_paths = build_cluster_paths(cluster_name)

        if not cluster_paths.apps_dir.exists():
            raise ScaffoldError(
                f"Cluster '{cluster_name}' is missing expected apps directory: {cluster_paths.apps_dir}"
            )

        repo_index = build_repo_index(cluster_paths.flux_extras_dir)
        config = collect_config(console, cluster_paths, repo_index)
        result = scaffold(config, cluster_paths, renderer, console)
    except UserAbort as exc:
        console.warn(str(exc))
        return 1
    except ScaffoldError as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.warn("Aborted by user.")
        return 1
    except Exception as exc:
        console.error(f"Unexpected error: {exc}")
        return 1

    print()
    console.heading("Created files")
    for path in result.created:
        console.list_item(str(path.relative_to(ROOT_DIR)))

    if result.updated:
        console.heading("Updated files")
        for path in result.updated:
            console.list_item(str(path.relative_to(ROOT_DIR)))

    if result.skipped:
        console.heading("Skipped files")
        for path in result.skipped:
            console.list_item(str(path.relative_to(ROOT_DIR)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
