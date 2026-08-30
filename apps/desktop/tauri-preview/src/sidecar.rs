use anyhow::{bail, ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::Manager;

const SIDECAR_NAME: &str = "storydex-agentd";
const COOMI_BRIDGE_NAME: &str = "storydex-coomi-bridge";
const DESKTOP_RUNTIME_NAME: &str = "storydex-tauri";
const READY_TIMEOUT: Duration = Duration::from_secs(15);
const HTTP_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, Deserialize)]
struct ReadyMessage {
    event: String,
    runtime: String,
    port: u16,
    token: String,
    version: String,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeInfo {
    runtime: &'static str,
    backend_base_url: String,
    sidecar: &'static str,
    sidecar_version: String,
    status: String,
    pid: u32,
    log_path: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RuntimeStatus {
    Running,
    Stopping,
    Stopped,
    Crashed,
    ForcedStop,
}

impl RuntimeStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Stopped => "stopped",
            Self::Crashed => "crashed",
            Self::ForcedStop => "forced_stop",
        }
    }
}

pub struct SidecarRuntime {
    ready: ReadyMessage,
    backend_base_url: String,
    pid: u32,
    log_path: PathBuf,
    log: Arc<Mutex<File>>,
    webview_data_dir: Option<PathBuf>,
    child: Mutex<Option<Child>>,
    status: Mutex<RuntimeStatus>,
    stopping: AtomicBool,
    process_job: ProcessJob,
}

impl SidecarRuntime {
    pub fn start(app: &tauri::App) -> Result<Arc<Self>> {
        let sidecar_path = resolve_sidecar_path()?;
        let bridge_path = resolve_bridge_path(&sidecar_path)?;
        let runtime_paths = resolve_runtime_paths(app)?;
        fs::create_dir_all(&runtime_paths.runtime_home).with_context(|| {
            format!("failed to create {}", runtime_paths.runtime_home.display())
        })?;
        fs::create_dir_all(&runtime_paths.log_root)
            .with_context(|| format!("failed to create {}", runtime_paths.log_root.display()))?;
        if let Some(webview_data_dir) = &runtime_paths.webview_data_dir {
            fs::create_dir_all(webview_data_dir)
                .with_context(|| format!("failed to create {}", webview_data_dir.display()))?;
        }

        let log_path = runtime_paths
            .log_root
            .join(format!("agentd-{}.log", unix_timestamp()));
        let log = Arc::new(Mutex::new(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&log_path)
                .with_context(|| format!("failed to create {}", log_path.display()))?,
        ));
        write_log(
            &log,
            &format!("[lifecycle] starting {}", sidecar_path.display()),
        );

        let mut command = Command::new(&sidecar_path);
        command
            .arg("--port")
            .arg("0")
            .arg("--shutdown-timeout-ms")
            .arg("5000")
            .arg("--coomi-home")
            .arg(&runtime_paths.runtime_home)
            .current_dir(
                sidecar_path
                    .parent()
                    .context("sidecar path has no parent directory")?,
            )
            .env("STORYDEX_COOMI_BRIDGE", &bridge_path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        configure_runtime_environment(&mut command, app)?;
        configure_hidden_process(&mut command);

        let mut child = command
            .spawn()
            .with_context(|| format!("failed to start {}", sidecar_path.display()))?;
        let pid = child.id();
        let process_job = match ProcessJob::assign(&child) {
            Ok(job) => job,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error.context("failed to isolate sidecar process tree"));
            }
        };
        let stdout = child
            .stdout
            .take()
            .context("sidecar stdout was not captured")?;
        let stderr = child
            .stderr
            .take()
            .context("sidecar stderr was not captured")?;
        pipe_stderr(stderr, log.clone());

        let ready = match read_ready(stdout, log.clone(), READY_TIMEOUT) {
            Ok(ready) => ready,
            Err(error) => {
                process_job.terminate();
                let _ = child.wait();
                return Err(error.context("storydex-agentd did not become ready"));
            }
        };
        validate_ready(&ready)?;
        verify_health(ready.port)?;

        let runtime = Arc::new(Self {
            backend_base_url: format!("http://127.0.0.1:{}/api/v1", ready.port),
            ready,
            pid,
            log_path,
            log: log.clone(),
            webview_data_dir: runtime_paths.webview_data_dir,
            child: Mutex::new(Some(child)),
            status: Mutex::new(RuntimeStatus::Running),
            stopping: AtomicBool::new(false),
            process_job,
        });
        write_log(
            &log,
            &format!("[lifecycle] ready pid={pid} port={}", runtime.ready.port),
        );
        Ok(runtime)
    }

    pub fn runtime_info(&self) -> RuntimeInfo {
        RuntimeInfo {
            runtime: DESKTOP_RUNTIME_NAME,
            backend_base_url: self.backend_base_url.clone(),
            sidecar: SIDECAR_NAME,
            sidecar_version: self.ready.version.clone(),
            status: self.current_status().as_str().to_owned(),
            pid: self.pid,
            log_path: self.log_path.display().to_string(),
        }
    }

    pub fn initialization_script(&self, app_version: String) -> Result<String> {
        adapter_script(&self.backend_base_url, &self.ready.token, &app_version)
    }

    pub fn webview_data_directory(&self) -> Option<PathBuf> {
        self.webview_data_dir.clone()
    }

    pub fn monitor(self: &Arc<Self>, app_handle: tauri::AppHandle) {
        let runtime = self.clone();
        thread::spawn(move || loop {
            thread::sleep(Duration::from_millis(400));
            let exit = {
                let mut child = lock_unpoisoned(&runtime.child);
                match child.as_mut() {
                    Some(process) => match process.try_wait() {
                        Ok(Some(status)) => {
                            child.take();
                            Some(Ok(status.success()))
                        }
                        Ok(None) => None,
                        Err(error) => Some(Err(error)),
                    },
                    None => return,
                }
            };
            let Some(exit) = exit else {
                continue;
            };
            if runtime.stopping.load(Ordering::SeqCst) {
                runtime.set_status(RuntimeStatus::Stopped);
                return;
            }
            runtime.set_status(RuntimeStatus::Crashed);
            let detail = match exit {
                Ok(true) => "sidecar exited unexpectedly with success status".to_owned(),
                Ok(false) => "sidecar exited unexpectedly with failure status".to_owned(),
                Err(error) => format!("failed to inspect sidecar status: {error}"),
            };
            write_log(&runtime.log, &format!("[lifecycle] {detail}"));
            app_handle.exit(1);
            return;
        });
    }

    pub fn shutdown(&self, timeout: Duration) {
        if self.stopping.swap(true, Ordering::SeqCst) {
            return;
        }
        self.set_status(RuntimeStatus::Stopping);
        write_log(&self.log, "[lifecycle] graceful shutdown requested");
        let _ = send_shutdown(self.ready.port, &self.ready.token);

        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            let stopped = {
                let mut child = lock_unpoisoned(&self.child);
                match child.as_mut().and_then(|process| process.try_wait().ok()) {
                    Some(Some(_)) => {
                        child.take();
                        true
                    }
                    _ => child.is_none(),
                }
            };
            if stopped {
                self.set_status(RuntimeStatus::Stopped);
                write_log(&self.log, "[lifecycle] sidecar stopped cleanly");
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }

        write_log(
            &self.log,
            "[lifecycle] graceful shutdown timed out; terminating process job",
        );
        self.process_job.terminate();
        let mut child = lock_unpoisoned(&self.child);
        if let Some(mut process) = child.take() {
            let _ = process.kill();
            let _ = process.wait();
        }
        self.set_status(RuntimeStatus::ForcedStop);
    }

    fn current_status(&self) -> RuntimeStatus {
        *lock_unpoisoned(&self.status)
    }

    fn set_status(&self, status: RuntimeStatus) {
        *lock_unpoisoned(&self.status) = status;
    }
}

struct RuntimePaths {
    runtime_home: PathBuf,
    log_root: PathBuf,
    webview_data_dir: Option<PathBuf>,
}

fn resolve_runtime_paths(app: &tauri::App) -> Result<RuntimePaths> {
    if let Some(test_root) = configured_test_root()? {
        return Ok(RuntimePaths {
            runtime_home: test_root.join("agent-runtime"),
            log_root: test_root.join("logs"),
            webview_data_dir: Some(test_root.join("webview")),
        });
    }

    let coomi_home = dirs::home_dir()
        .context("failed to resolve the Storydex user home directory")?
        .join(".storydex")
        .join(".coomi");
    Ok(RuntimePaths {
        runtime_home: coomi_home,
        log_root: app
            .path()
            .app_log_dir()
            .context("failed to resolve Tauri log directory")?,
        webview_data_dir: None,
    })
}

fn configure_runtime_environment(command: &mut Command, app: &tauri::App) -> Result<()> {
    let mingit_root = resolve_mingit_root(app)?;
    let git_executable = if cfg!(windows) {
        mingit_root.join("cmd").join("git.exe")
    } else {
        mingit_root.join("bin").join("git")
    };
    ensure!(
        git_executable.is_file(),
        "bundled MinGit executable was not found: {}",
        git_executable.display()
    );
    command
        .env("STORYDEX_MINGIT_ROOT", &mingit_root)
        .env("STORYDEX_GIT_EXECUTABLE", &git_executable);
    Ok(())
}

fn resolve_mingit_root(app: &tauri::App) -> Result<PathBuf> {
    let resource_root = app
        .path()
        .resource_dir()
        .context("failed to resolve Tauri resource directory")?;
    let candidates = [
        resource_root.join("mingit"),
        resource_root.join("resources").join("mingit"),
    ];
    if let Some(path) = candidates.into_iter().find(|path| path.is_dir()) {
        return Ok(path);
    }
    if cfg!(debug_assertions) {
        let repository = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("vendor")
            .join("mingit");
        if repository.is_dir() {
            return repository
                .canonicalize()
                .with_context(|| format!("failed to resolve {}", repository.display()));
        }
    }
    bail!(
        "bundled MinGit directory was not found under {}",
        resource_root.display()
    )
}

fn configured_test_root() -> Result<Option<PathBuf>> {
    let Some(configured) = env::var_os("STORYDEX_TAURI_TEST_ROOT") else {
        return Ok(None);
    };
    ensure!(
        env::var("STORYDEX_TESTING").as_deref() == Ok("1"),
        "STORYDEX_TAURI_TEST_ROOT requires STORYDEX_TESTING=1"
    );
    validate_test_root(Path::new(&configured), &env::temp_dir()).map(Some)
}

fn validate_test_root(configured: &Path, temporary_root: &Path) -> Result<PathBuf> {
    ensure!(
        configured.is_dir(),
        "Tauri test root does not exist: {}",
        configured.display()
    );
    let configured = configured
        .canonicalize()
        .with_context(|| format!("failed to resolve {}", configured.display()))?;
    let temporary_root = temporary_root
        .canonicalize()
        .with_context(|| format!("failed to resolve {}", temporary_root.display()))?;
    ensure!(
        configured != temporary_root && configured.starts_with(&temporary_root),
        "Tauri test root must be a child of the operating-system temporary directory"
    );
    Ok(configured)
}

fn resolve_sidecar_path() -> Result<PathBuf> {
    if let Some(configured) = env::var_os("STORYDEX_TAURI_SIDECAR_PATH") {
        let path = PathBuf::from(configured);
        ensure_sidecar_path(&path)?;
        return path
            .canonicalize()
            .with_context(|| format!("failed to resolve {}", path.display()));
    }

    let executable = env::current_exe().context("failed to resolve Tauri preview executable")?;
    if let Some(parent) = executable.parent() {
        let packaged = parent.join(sidecar_file_name());
        if packaged.is_file() {
            return Ok(packaged);
        }
    }

    if cfg!(debug_assertions) {
        let debug = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("agent-runtime")
            .join("target")
            .join("debug")
            .join(sidecar_file_name());
        if debug.is_file() {
            return debug
                .canonicalize()
                .with_context(|| format!("failed to resolve {}", debug.display()));
        }
    }

    bail!(
        "bundled {SIDECAR_NAME} was not found; set STORYDEX_TAURI_SIDECAR_PATH only for an explicit candidate build"
    )
}

fn ensure_sidecar_path(path: &Path) -> Result<()> {
    ensure!(path.is_file(), "sidecar does not exist: {}", path.display());
    let actual = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    ensure!(
        actual.eq_ignore_ascii_case(sidecar_file_name()),
        "Tauri preview sidecar must be named {}",
        sidecar_file_name()
    );
    Ok(())
}

fn resolve_bridge_path(sidecar_path: &Path) -> Result<PathBuf> {
    let parent = sidecar_path
        .parent()
        .context("sidecar path has no parent directory")?;
    let bridge = parent.join(bridge_file_name());
    ensure_bridge_path(&bridge)?;
    bridge
        .canonicalize()
        .with_context(|| format!("failed to resolve {}", bridge.display()))
}

fn ensure_bridge_path(path: &Path) -> Result<()> {
    ensure!(
        path.is_file(),
        "bundled {COOMI_BRIDGE_NAME} was not found: {}",
        path.display()
    );
    Ok(())
}

#[cfg(windows)]
fn sidecar_file_name() -> &'static str {
    "storydex-agentd.exe"
}

#[cfg(not(windows))]
fn sidecar_file_name() -> &'static str {
    SIDECAR_NAME
}

#[cfg(windows)]
fn bridge_file_name() -> &'static str {
    "storydex-coomi-bridge.exe"
}

#[cfg(not(windows))]
fn bridge_file_name() -> &'static str {
    COOMI_BRIDGE_NAME
}

fn read_ready(
    stdout: ChildStdout,
    log: Arc<Mutex<File>>,
    timeout: Duration,
) -> Result<ReadyMessage> {
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut first_line = String::new();
        let result = reader
            .read_line(&mut first_line)
            .context("failed to read sidecar ready line")
            .and_then(|count| {
                ensure!(count > 0, "sidecar stdout closed before ready");
                serde_json::from_str::<ReadyMessage>(first_line.trim())
                    .context("sidecar ready line is not valid JSON")
            });
        if let Ok(ready) = &result {
            write_log(
                &log,
                &format!(
                    "[stdout] ready runtime={} port={} version={}",
                    ready.runtime, ready.port, ready.version
                ),
            );
        }
        let keep_reading = result.is_ok();
        let _ = sender.send(result);
        if keep_reading {
            for line in reader.lines() {
                match line {
                    Ok(line) => write_log(&log, &format!("[stdout] {line}")),
                    Err(error) => {
                        write_log(&log, &format!("[stdout] read error: {error}"));
                        break;
                    }
                }
            }
        }
    });
    receiver
        .recv_timeout(timeout)
        .context("timed out waiting for sidecar ready line")?
}

fn pipe_stderr(stderr: std::process::ChildStderr, log: Arc<Mutex<File>>) {
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            match line {
                Ok(line) => write_log(&log, &format!("[stderr] {line}")),
                Err(error) => {
                    write_log(&log, &format!("[stderr] read error: {error}"));
                    break;
                }
            }
        }
    });
}

fn validate_ready(ready: &ReadyMessage) -> Result<()> {
    ensure!(ready.event == "ready", "unexpected sidecar event");
    ensure!(ready.runtime == SIDECAR_NAME, "unexpected sidecar runtime");
    ensure!(ready.port > 0, "sidecar returned an invalid port");
    ensure!(
        ready.token.len() == 32 && ready.token.bytes().all(|value| value.is_ascii_hexdigit()),
        "sidecar returned an invalid runtime token"
    );
    ensure!(
        !ready.version.trim().is_empty(),
        "sidecar version is missing"
    );
    Ok(())
}

fn verify_health(port: u16) -> Result<()> {
    let body = request_json(port, "GET", "/api/v1/sys/health", None)?;
    ensure!(
        body.get("ok").and_then(Value::as_bool) == Some(true),
        "health check failed"
    );
    ensure!(
        body.pointer("/data/runtime").and_then(Value::as_str) == Some(SIDECAR_NAME),
        "health runtime does not match sidecar"
    );
    Ok(())
}

fn send_shutdown(port: u16, token: &str) -> Result<()> {
    let body = request_json(port, "POST", "/api/v1/sys/shutdown", Some(token))?;
    ensure!(
        body.get("ok").and_then(Value::as_bool) == Some(true),
        "shutdown was rejected"
    );
    Ok(())
}

fn request_json(port: u16, method: &str, path: &str, token: Option<&str>) -> Result<Value> {
    let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let mut stream = TcpStream::connect_timeout(&address, HTTP_TIMEOUT)
        .with_context(|| format!("failed to connect to sidecar on port {port}"))?;
    stream.set_read_timeout(Some(HTTP_TIMEOUT))?;
    stream.set_write_timeout(Some(HTTP_TIMEOUT))?;
    let authorization = token
        .map(|value| format!("Authorization: Bearer {value}\r\n"))
        .unwrap_or_default();
    write!(
        stream,
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{authorization}Accept: application/json\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    )?;
    stream.flush()?;
    let mut response = Vec::new();
    stream.read_to_end(&mut response)?;
    parse_http_json(&response)
}

fn parse_http_json(response: &[u8]) -> Result<Value> {
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .context("sidecar HTTP response has no header terminator")?;
    let headers =
        std::str::from_utf8(&response[..split]).context("sidecar HTTP headers are invalid")?;
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .context("sidecar HTTP status is invalid")?;
    ensure!(
        status == 200,
        "sidecar HTTP request returned status {status}"
    );
    serde_json::from_slice(&response[split + 4..]).context("sidecar HTTP body is invalid JSON")
}

fn adapter_script(backend_base_url: &str, token: &str, app_version: &str) -> Result<String> {
    let platform = if cfg!(windows) {
        "win32"
    } else {
        env::consts::OS
    };
    let bridge = json!({
        "platform": platform,
        "backendBaseUrl": backend_base_url,
        "backendAuthToken": token,
        "versions": {"tauri": app_version},
        "isTitleBarOverlaySupported": false,
    });
    let bridge = serde_json::to_string(&bridge)?;
    Ok(format!(
        "if (window.__TAURI_INTERNALS__ && (window.location.protocol === 'tauri:' || window.location.hostname === 'tauri.localhost' || (window.location.protocol === 'http:' && (window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost')))) {{ const bridge = {bridge}; const invoke = (command, args = {{}}) => window.__TAURI_INTERNALS__.invoke(command, args); Object.assign(bridge, {{ pickDirectory: (options = {{}}) => invoke('pick_directory', {{ options }}), revealPath: (absolutePath) => invoke('reveal_path', {{ absolutePath }}), openWithDialog: (absolutePath) => invoke('open_with_dialog', {{ absolutePath }}), openPreviewWindow: (relativePath) => invoke('open_preview_window', {{ relativePath }}), setTitleBarTheme: (theme) => invoke('set_titlebar_theme', {{ theme }}), startMainWindowDragging: () => invoke('start_main_window_dragging'), minimizeMainWindow: () => invoke('minimize_main_window'), toggleMainWindowMaximized: () => invoke('toggle_main_window_maximized'), isMainWindowMaximized: () => invoke('is_main_window_maximized'), closeMainWindow: () => invoke('close_main_window'), confirmMainWindowClose: () => invoke('confirm_main_window_close'), getPendingOpenTarget: () => invoke('get_pending_open_target'), ackOpenTarget: (targetId) => invoke('ack_open_target', {{ targetId }}) }}); Object.defineProperty(window, 'storydexDesktop', {{ value: bridge, configurable: false, writable: false }}); }}"
    ))
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn write_log(log: &Arc<Mutex<File>>, message: &str) {
    let mut log = lock_unpoisoned(log);
    let _ = writeln!(log, "{message}");
    let _ = log.flush();
}

fn lock_unpoisoned<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|error| error.into_inner())
}

#[cfg(windows)]
fn configure_hidden_process(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn configure_hidden_process(_command: &mut Command) {}

#[cfg(windows)]
struct ProcessJob(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl ProcessJob {
    fn assign(child: &Child) -> Result<Self> {
        use std::mem::size_of;
        use std::os::windows::io::AsRawHandle;
        use std::ptr;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let handle = unsafe { CreateJobObjectW(ptr::null(), ptr::null()) };
        ensure!(!handle.is_null(), "CreateJobObjectW failed");
        let mut information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&information as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            bail!("SetInformationJobObject failed")
        }
        let assigned = unsafe { AssignProcessToJobObject(handle, child.as_raw_handle().cast()) };
        if assigned == 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            bail!("AssignProcessToJobObject failed")
        }
        Ok(Self(handle))
    }

    fn terminate(&self) {
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.0, 1);
        }
    }
}

#[cfg(windows)]
unsafe impl Send for ProcessJob {}

#[cfg(windows)]
unsafe impl Sync for ProcessJob {}

#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) {
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}

#[cfg(not(windows))]
struct ProcessJob;

#[cfg(not(windows))]
impl ProcessJob {
    fn assign(_child: &Child) -> Result<Self> {
        Ok(Self)
    }

    fn terminate(&self) {}
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_message_requires_dynamic_loopback_credentials() {
        let ready: ReadyMessage = serde_json::from_value(json!({
            "event": "ready",
            "runtime": "storydex-agentd",
            "port": 49152,
            "token": "0123456789abcdef0123456789abcdef",
            "version": "2.1.0-storydex-desktop.1"
        }))
        .expect("ready message");
        validate_ready(&ready).expect("valid ready message");

        let mut invalid = ready.clone();
        invalid.port = 0;
        assert!(validate_ready(&invalid).is_err());
        invalid.port = ready.port;
        invalid.token = "predictable".to_owned();
        assert!(validate_ready(&invalid).is_err());
    }

    #[test]
    fn initialization_script_exposes_narrow_runtime_bridge_without_shell() {
        let script = adapter_script(
            "http://127.0.0.1:49152/api/v1",
            "0123456789abcdef0123456789abcdef",
            "2.0.5",
        )
        .expect("adapter script");
        assert!(script.contains("backendBaseUrl"));
        assert!(script.contains("backendAuthToken"));
        assert!(script.contains("pickDirectory"));
        assert!(script.contains("revealPath"));
        assert!(script.contains("openWithDialog"));
        assert!(script.contains("startMainWindowDragging"));
        assert!(script.contains("confirmMainWindowClose"));
        assert!(script.contains("minimizeMainWindow"));
        assert!(script.contains("toggleMainWindowMaximized"));
        assert!(script.contains("closeMainWindow"));
        assert!(script.contains("Object.defineProperty"));
        assert!(!script.contains("shell"));
        assert!(!script.contains("18081"));
    }

    #[test]
    fn http_parser_rejects_non_success_and_accepts_storydex_envelope() {
        let ok = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"ok\":true}";
        assert_eq!(parse_http_json(ok).expect("JSON")["ok"], true);
        let denied = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n";
        assert!(parse_http_json(denied).is_err());
    }

    #[test]
    fn test_root_must_be_an_existing_child_of_the_system_temporary_directory() {
        let temporary_root = env::temp_dir().canonicalize().expect("temporary root");
        let candidate = temporary_root.join(format!(
            "storydex-tauri-test-root-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("timestamp")
                .as_nanos()
        ));
        fs::create_dir_all(&candidate).expect("candidate test root");
        let resolved = validate_test_root(&candidate, &temporary_root).expect("valid test root");
        assert!(resolved.starts_with(&temporary_root));
        assert!(validate_test_root(&temporary_root, &temporary_root).is_err());
        assert!(
            validate_test_root(Path::new(env!("CARGO_MANIFEST_DIR")), &temporary_root).is_err()
        );
        fs::remove_dir_all(candidate).expect("remove candidate test root");
    }

    #[test]
    fn sidecar_requires_the_coomi_bridge_beside_the_agentd_binary() {
        let candidate = env::temp_dir().join(format!(
            "storydex-tauri-bridge-test-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("timestamp")
                .as_nanos()
        ));
        fs::create_dir_all(&candidate).expect("candidate bridge test root");
        let sidecar = candidate.join(sidecar_file_name());
        File::create(&sidecar).expect("candidate sidecar");

        assert!(resolve_bridge_path(&sidecar).is_err());

        let bridge = candidate.join(bridge_file_name());
        File::create(&bridge).expect("candidate bridge");
        assert_eq!(
            resolve_bridge_path(&sidecar).expect("resolved bridge"),
            bridge.canonicalize().expect("canonical bridge")
        );

        fs::remove_dir_all(candidate).expect("remove candidate bridge test root");
    }
}
