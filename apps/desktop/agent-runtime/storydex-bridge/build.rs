use std::env;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

fn git_output(repo_root: &Path, arguments: &[&str]) -> Option<Vec<u8>> {
    let output = Command::new("git")
        .current_dir(repo_root)
        .args(arguments)
        .output()
        .ok()?;
    output.status.success().then_some(output.stdout)
}

fn git_output_with_input(repo_root: &Path, arguments: &[&str], input: &[u8]) -> Option<Vec<u8>> {
    let mut child = Command::new("git")
        .current_dir(repo_root)
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .ok()?;
    child.stdin.as_mut()?.write_all(input).ok()?;
    let output = child.wait_with_output().ok()?;
    output.status.success().then_some(output.stdout)
}

fn trimmed_utf8(value: Vec<u8>) -> Option<String> {
    String::from_utf8(value)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn git_path(repo_root: &Path, name: &str) -> Option<PathBuf> {
    let value = trimmed_utf8(git_output(repo_root, &["rev-parse", "--git-path", name])?)?;
    let path = PathBuf::from(value);
    Some(if path.is_absolute() {
        path
    } else {
        repo_root.join(path)
    })
}

fn git_identity_dependencies(repo_root: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(head) = git_path(repo_root, "HEAD") {
        paths.push(head);
    }
    if let Some(reference) = git_output(repo_root, &["symbolic-ref", "-q", "HEAD"])
        .and_then(trimmed_utf8)
        .and_then(|reference| git_path(repo_root, &reference))
    {
        paths.push(reference);
    }
    if let Some(packed_refs) = git_path(repo_root, "packed-refs")
        && packed_refs.exists()
    {
        paths.push(packed_refs);
    }
    paths.sort();
    paths.dedup();
    paths
}

fn runtime_source_identity(manifest_dir: &Path) -> Option<(PathBuf, Vec<String>, String, String)> {
    let repo_root = trimmed_utf8(git_output(manifest_dir, &["rev-parse", "--show-toplevel"])?)?;
    let repo_root = PathBuf::from(repo_root);
    let runtime_root = manifest_dir.parent()?;
    let runtime_relative = runtime_root
        .strip_prefix(&repo_root)
        .ok()?
        .to_string_lossy()
        .replace('\\', "/");
    let listed = git_output(
        &repo_root,
        &[
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--full-name",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            &runtime_relative,
        ],
    )?;
    let mut paths = listed
        .split(|byte| *byte == 0)
        .filter(|path| !path.is_empty())
        .map(|path| String::from_utf8(path.to_vec()).ok())
        .collect::<Option<Vec<_>>>()?;
    paths.sort();
    let hash_input = paths.join("\n") + "\n";
    let blob_hashes = git_output_with_input(
        &repo_root,
        &["hash-object", "--stdin-paths"],
        hash_input.as_bytes(),
    )?;
    let hashes = String::from_utf8(blob_hashes).ok()?;
    let hashes = hashes.lines().collect::<Vec<_>>();
    if hashes.len() != paths.len() {
        return None;
    }
    let mut manifest = Vec::new();
    for (path, hash) in paths.iter().zip(hashes) {
        manifest.extend_from_slice(path.as_bytes());
        manifest.push(0);
        manifest.extend_from_slice(hash.as_bytes());
        manifest.push(b'\n');
    }
    let fingerprint = trimmed_utf8(git_output_with_input(
        &repo_root,
        &["hash-object", "--stdin"],
        &manifest,
    )?)?;
    let git_sha = trimmed_utf8(git_output(&repo_root, &["rev-parse", "HEAD"])?)?;
    Some((repo_root, paths, git_sha, fingerprint))
}

fn main() {
    println!("cargo:rerun-if-env-changed=STORYDEX_COOMI_GIT_SHA");
    println!("cargo:rerun-if-env-changed=STORYDEX_COOMI_SOURCE_FINGERPRINT");
    let manifest_dir = PathBuf::from(
        env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR must be available"),
    );
    let detected = runtime_source_identity(&manifest_dir);
    if let Some((repo_root, paths, _, _)) = &detected {
        for path in paths {
            println!("cargo:rerun-if-changed={}", repo_root.join(path).display());
        }
        for path in git_identity_dependencies(repo_root) {
            println!("cargo:rerun-if-changed={}", path.display());
        }
    }
    let git_sha = env::var("STORYDEX_COOMI_GIT_SHA")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| detected.as_ref().map(|value| value.2.clone()))
        .expect("Git SHA unavailable; set STORYDEX_COOMI_GIT_SHA for archive builds");
    let fingerprint = env::var("STORYDEX_COOMI_SOURCE_FINGERPRINT")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| detected.as_ref().map(|value| value.3.clone()))
        .expect(
            "runtime source fingerprint unavailable; set STORYDEX_COOMI_SOURCE_FINGERPRINT for archive builds",
        );
    println!("cargo:rustc-env=STORYDEX_COOMI_GIT_SHA={git_sha}");
    println!("cargo:rustc-env=STORYDEX_COOMI_SOURCE_FINGERPRINT={fingerprint}");
}
