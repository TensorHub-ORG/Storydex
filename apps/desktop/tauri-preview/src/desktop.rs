use anyhow::{ensure, Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PickDirectoryOptions {
    title: Option<String>,
    default_path: Option<String>,
}

pub fn pick_directory(options: PickDirectoryOptions) -> Result<String> {
    let mut dialog = rfd::FileDialog::new();
    if let Some(title) = normalized(options.title) {
        dialog = dialog.set_title(title);
    }
    if let Some(default_path) = normalized(options.default_path) {
        let path = PathBuf::from(default_path);
        if path.is_dir() {
            dialog = dialog.set_directory(path);
        }
    }
    Ok(dialog
        .pick_folder()
        .map(|path| path.display().to_string())
        .unwrap_or_default())
}

pub fn reveal_path(absolute_path: &str) -> Result<bool> {
    let path = resolve_existing_absolute_path(absolute_path)?;
    reveal_path_impl(&path)?;
    Ok(true)
}

pub fn open_with_dialog(absolute_path: &str) -> Result<bool> {
    let path = resolve_existing_absolute_path(absolute_path)?;
    open_with_dialog_impl(&path)?;
    Ok(true)
}

fn resolve_existing_absolute_path(value: &str) -> Result<PathBuf> {
    let value = value.trim();
    ensure!(!value.is_empty(), "path is required");
    let path = PathBuf::from(value);
    ensure!(
        path.is_absolute(),
        "desktop integration path must be absolute"
    );
    path.canonicalize().with_context(|| {
        format!(
            "desktop integration path does not exist: {}",
            path.display()
        )
    })
}

fn normalized(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

#[cfg(windows)]
fn reveal_path_impl(path: &Path) -> Result<()> {
    let mut command = Command::new("explorer.exe");
    if path.is_dir() {
        command.arg(path);
    } else {
        command.arg("/select,").arg(path);
    }
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to launch Windows Explorer")?;
    Ok(())
}

#[cfg(not(windows))]
fn reveal_path_impl(path: &Path) -> Result<()> {
    Command::new("xdg-open")
        .arg(if path.is_dir() {
            path
        } else {
            path.parent().unwrap_or(path)
        })
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to launch the platform file manager")?;
    Ok(())
}

#[cfg(windows)]
fn open_with_dialog_impl(path: &Path) -> Result<()> {
    Command::new("rundll32.exe")
        .arg("shell32.dll,OpenAs_RunDLL")
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to launch the Windows Open With dialog")?;
    Ok(())
}

#[cfg(not(windows))]
fn open_with_dialog_impl(path: &Path) -> Result<()> {
    Command::new("xdg-open")
        .arg(path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("failed to launch the platform open dialog")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_paths_fail_closed_for_empty_relative_and_missing_values() {
        assert!(resolve_existing_absolute_path("").is_err());
        assert!(resolve_existing_absolute_path("relative/file.md").is_err());
        let missing = std::env::temp_dir().join("storydex-tauri-preview-missing-path");
        assert!(resolve_existing_absolute_path(&missing.display().to_string()).is_err());
    }

    #[test]
    fn picker_options_trim_empty_values() {
        assert_eq!(
            normalized(Some("  title  ".to_owned())),
            Some("title".to_owned())
        );
        assert_eq!(normalized(Some("   ".to_owned())), None);
        assert_eq!(normalized(None), None);
    }
}
