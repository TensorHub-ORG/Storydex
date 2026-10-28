use anyhow::{Context, Result};
use fs2::FileExt;
use std::fs::{self, File, OpenOptions};
use std::path::Path;

/// Shared by the editor HTTP endpoint and the Agent tool process. The lock
/// covers revision validation and the write, including multi-file patches.
pub struct ProjectWriteLock(File);

impl ProjectWriteLock {
    pub fn acquire(root: &Path) -> Result<Self> {
        let directory = root.join(".storydex/.agent/locks");
        fs::create_dir_all(&directory)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(directory.join("project-write.lock"))?;
        file.try_lock_exclusive()
            .context("项目正在写入，请保留草稿并稍后重试")?;
        Ok(Self(file))
    }
}

impl Drop for ProjectWriteLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn serializes_writers_and_releases_on_drop() {
        let root = tempfile::tempdir().expect("test fixture");
        let first = ProjectWriteLock::acquire(root.path()).expect("test fixture");
        assert!(ProjectWriteLock::acquire(root.path()).is_err());
        drop(first);
        assert!(ProjectWriteLock::acquire(root.path()).is_ok());
    }
}
