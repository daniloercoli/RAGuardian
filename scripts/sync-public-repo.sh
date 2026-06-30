#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/sync-public-repo.sh [--dry-run] [--force] [--init] /path/to/public-repo

Copies the current working tree snapshot into a separate public repository
without copying this repository's .git history.

What is copied:
  - tracked files
  - new, untracked files that are not ignored by .gitignore

What is not copied:
  - this repository's .git directory
  - files ignored by .gitignore
  - publication blocklist entries such as .DS_Store

Options:
  --dry-run  Show what would change without writing to the public repo.
  --force    Allow syncing over a dirty destination working tree.
  --init     Create and initialize the destination repo if needed.
  --help     Show this help.
EOF
}

dry_run=0
force=0
init_repo=0
dest=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --force)
      force=1
      shift
      ;;
    --init)
      init_repo=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "$dest" ]]; then
        echo "Only one destination path is allowed." >&2
        usage >&2
        exit 2
      fi
      dest="$1"
      shift
      ;;
  esac
done

if [[ -z "$dest" ]]; then
  usage >&2
  exit 2
fi

if ! command -v git >/dev/null 2>&1; then
  echo "git is required." >&2
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required." >&2
  exit 1
fi

source_root="$(git rev-parse --show-toplevel)"
source_root="$(cd "$source_root" && pwd -P)"

if [[ ! -d "$dest" ]]; then
  if [[ "$init_repo" -ne 1 ]]; then
    echo "Destination does not exist: $dest" >&2
    echo "Create it first, or rerun with --init." >&2
    exit 1
  fi
  mkdir -p "$dest"
fi

dest_root="$(cd "$dest" && pwd -P)"

if [[ "$dest_root" == "$source_root" ]]; then
  echo "Destination must be a different directory from the source repo." >&2
  exit 1
fi

case "$dest_root" in
  "$source_root"/*)
    echo "Destination must not live inside the source repo." >&2
    exit 1
    ;;
esac

if [[ ! -d "$dest_root/.git" ]]; then
  if [[ "$init_repo" -ne 1 ]]; then
    echo "Destination is not a git repository: $dest_root" >&2
    echo "Initialize it first, or rerun with --init." >&2
    exit 1
  fi
  if ! git -C "$dest_root" init --initial-branch=main >/dev/null 2>&1; then
    git -C "$dest_root" init >/dev/null
    git -C "$dest_root" branch -M main
  fi
fi

if [[ "$force" -ne 1 && "$dry_run" -ne 1 ]]; then
  if [[ -n "$(git -C "$dest_root" status --porcelain)" ]]; then
    echo "Destination working tree is dirty. Commit/stash it, or rerun with --force." >&2
    exit 1
  fi
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/raguardian-public-sync.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT

staging="$tmp_dir/staging"
mkdir -p "$staging"

manifest="$tmp_dir/manifest.z"
(
  cd "$source_root"
  git ls-files -z -c -o --exclude-standard > "$manifest"
)

while IFS= read -r -d '' rel_path; do
  case "$rel_path" in
    .DS_Store|*/.DS_Store|app/uploads/.DS_Store)
      continue
      ;;
  esac

  source_path="$source_root/$rel_path"
  target_path="$staging/$rel_path"
  mkdir -p "$(dirname "$target_path")"

  if [[ -L "$source_path" ]]; then
    cp -P "$source_path" "$target_path"
  else
    cp -p "$source_path" "$target_path"
  fi
done < "$manifest"

rsync_args=(-a --delete --exclude ".git/")
if [[ "$dry_run" -eq 1 ]]; then
  rsync_args+=(--dry-run --itemize-changes)
fi

rsync "${rsync_args[@]}" "$staging/" "$dest_root/"

if [[ "$dry_run" -eq 1 ]]; then
  echo
  echo "Dry run complete. No files were changed in $dest_root"
  exit 0
fi

echo
echo "Public repo synced: $dest_root"
echo
git -C "$dest_root" status --short
echo
echo "Review there, then commit when you are ready:"
echo "  cd \"$dest_root\""
echo "  git diff --stat"
echo "  git add ."
echo "  git commit -m \"initial commit\"   # first publish, or your daily snapshot message"
