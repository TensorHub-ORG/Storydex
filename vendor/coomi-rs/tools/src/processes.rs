use coomi_engine::ToolResult;
use serde_json::Value;
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWriteExt;
use tokio::process::Child;
use tokio::process::ChildStdin;
use tokio::process::Command;
use tokio::sync::Mutex as AsyncMutex;
use uuid::Uuid;

#[derive(Default)]
pub struct ProcessManager {
    processes: Mutex<HashMap<String, Arc<AsyncMutex<ManagedProcess>>>>,
}

struct ManagedProcess {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: Arc<AsyncMutex<Vec<u8>>>,
    stderr: Arc<AsyncMutex<Vec<u8>>>,
    stdout_offset: usize,
    stderr_offset: usize,
}

impl ProcessManager {
    pub async fn execute(&self, cwd: &std::path::Path, arguments: &Value) -> ToolResult {
        let action = arguments
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or("exec");
        match action {
            "exec" => self.start(cwd, arguments).await,
            "write" => self.write(arguments).await,
            "wait" => self.wait(arguments).await,
            "terminate" => self.terminate(arguments).await,
            _ => ToolResult::error("action must be exec, write, wait, or terminate"),
        }
    }

    async fn start(&self, cwd: &std::path::Path, arguments: &Value) -> ToolResult {
        let Some(command) = arguments.get("command").and_then(Value::as_str) else {
            return ToolResult::error("missing string argument: command");
        };
        let yield_time_ms = arguments
            .get("yield_time_ms")
            .and_then(Value::as_u64)
            .unwrap_or(10_000)
            .min(60_000);
        let mut process = platform_shell(command);
        process
            .current_dir(cwd)
            .kill_on_drop(true)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = match process.spawn() {
            Ok(child) => child,
            Err(error) => return ToolResult::error(format!("failed to start shell: {error}")),
        };
        let stdin = child.stdin.take();
        let stdout_buffer = Arc::new(AsyncMutex::new(Vec::new()));
        let stderr_buffer = Arc::new(AsyncMutex::new(Vec::new()));
        if let Some(mut stdout) = child.stdout.take() {
            let buffer = Arc::clone(&stdout_buffer);
            tokio::spawn(async move {
                let mut chunk = [0_u8; 8192];
                loop {
                    match stdout.read(&mut chunk).await {
                        Ok(0) | Err(_) => break,
                        Ok(size) => buffer.lock().await.extend_from_slice(&chunk[..size]),
                    }
                }
            });
        }
        if let Some(mut stderr) = child.stderr.take() {
            let buffer = Arc::clone(&stderr_buffer);
            tokio::spawn(async move {
                let mut chunk = [0_u8; 8192];
                loop {
                    match stderr.read(&mut chunk).await {
                        Ok(0) | Err(_) => break,
                        Ok(size) => buffer.lock().await.extend_from_slice(&chunk[..size]),
                    }
                }
            });
        }
        let session_id = Uuid::new_v4().to_string();
        let managed = Arc::new(AsyncMutex::new(ManagedProcess {
            child,
            stdin,
            stdout: stdout_buffer,
            stderr: stderr_buffer,
            stdout_offset: 0,
            stderr_offset: 0,
        }));
        if yield_time_ms > 0 {
            let deadline = tokio::time::Instant::now() + Duration::from_millis(yield_time_ms);
            loop {
                {
                    let mut process = managed.lock().await;
                    match process.child.try_wait() {
                        Ok(Some(status)) => {
                            tokio::time::sleep(Duration::from_millis(20)).await;
                            let output = read_delta(&mut process).await;
                            return if status.success() {
                                ToolResult::success(format!("{output}\nexit: {status}"))
                            } else {
                                ToolResult::error(format!("{output}\nexit: {status}"))
                            };
                        }
                        Ok(None) => {}
                        Err(error) => {
                            return ToolResult::error(format!("failed to query process: {error}"));
                        }
                    }
                }
                if tokio::time::Instant::now() >= deadline {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(25)).await;
            }
        }
        self.processes
            .lock()
            .expect("process registry lock")
            .insert(session_id.clone(), managed);
        ToolResult::success(format!("process running\nsession_id: {session_id}"))
    }

    async fn write(&self, arguments: &Value) -> ToolResult {
        let Some(id) = arguments.get("session_id").and_then(Value::as_str) else {
            return ToolResult::error("missing string argument: session_id");
        };
        let input = arguments
            .get("input")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let process = match self.lookup(id) {
            Some(process) => process,
            None => return ToolResult::error(format!("unknown process session: {id}")),
        };
        let mut process = process.lock().await;
        let Some(stdin) = &mut process.stdin else {
            return ToolResult::error("process stdin is closed");
        };
        if let Err(error) = stdin.write_all(input.as_bytes()).await {
            return ToolResult::error(format!("failed to write stdin: {error}"));
        }
        if arguments
            .get("close_stdin")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            process.stdin.take();
        }
        ToolResult::success(format!("wrote {} bytes", input.len()))
    }

    async fn wait(&self, arguments: &Value) -> ToolResult {
        let Some(id) = arguments.get("session_id").and_then(Value::as_str) else {
            return ToolResult::error("missing string argument: session_id");
        };
        let yield_time_ms = arguments
            .get("yield_time_ms")
            .and_then(Value::as_u64)
            .unwrap_or(10_000)
            .min(60_000);
        let process = match self.lookup(id) {
            Some(process) => process,
            None => return ToolResult::error(format!("unknown process session: {id}")),
        };
        let deadline = tokio::time::Instant::now() + Duration::from_millis(yield_time_ms);
        loop {
            let mut process = process.lock().await;
            match process.child.try_wait() {
                Ok(Some(status)) => {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                    let output = read_delta(&mut process).await;
                    drop(process);
                    self.processes
                        .lock()
                        .expect("process registry lock")
                        .remove(id);
                    return if status.success() {
                        ToolResult::success(format!("{output}\nexit: {status}"))
                    } else {
                        ToolResult::error(format!("{output}\nexit: {status}"))
                    };
                }
                Ok(None) => {
                    let output = read_delta(&mut process).await;
                    if !output.trim().is_empty() || tokio::time::Instant::now() >= deadline {
                        return ToolResult::success(format!("{output}\nprocess still running"));
                    }
                }
                Err(error) => {
                    return ToolResult::error(format!("failed to query process: {error}"));
                }
            }
            drop(process);
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    }

    async fn terminate(&self, arguments: &Value) -> ToolResult {
        let Some(id) = arguments.get("session_id").and_then(Value::as_str) else {
            return ToolResult::error("missing string argument: session_id");
        };
        let process = self
            .processes
            .lock()
            .expect("process registry lock")
            .remove(id);
        let Some(process) = process else {
            return ToolResult::error(format!("unknown process session: {id}"));
        };
        match process.lock().await.child.kill().await {
            Ok(()) => ToolResult::success(format!("terminated {id}")),
            Err(error) => ToolResult::error(format!("failed to terminate {id}: {error}")),
        }
    }

    fn lookup(&self, id: &str) -> Option<Arc<AsyncMutex<ManagedProcess>>> {
        self.processes
            .lock()
            .expect("process registry lock")
            .get(id)
            .cloned()
    }
}

async fn read_delta(process: &mut ManagedProcess) -> String {
    let stdout = process.stdout.lock().await;
    let stdout_delta = &stdout[process.stdout_offset.min(stdout.len())..];
    let stdout_text = String::from_utf8_lossy(stdout_delta).into_owned();
    process.stdout_offset = stdout.len();
    drop(stdout);

    let stderr = process.stderr.lock().await;
    let stderr_delta = &stderr[process.stderr_offset.min(stderr.len())..];
    let stderr_text = String::from_utf8_lossy(stderr_delta).into_owned();
    process.stderr_offset = stderr.len();
    match (stdout_text.trim().is_empty(), stderr_text.trim().is_empty()) {
        (true, true) => String::new(),
        (false, true) => stdout_text,
        (true, false) => format!("[stderr]\n{stderr_text}"),
        (false, false) => format!("{stdout_text}\n[stderr]\n{stderr_text}"),
    }
}

#[cfg(windows)]
fn platform_shell(command: &str) -> Command {
    let mut process = Command::new("powershell.exe");
    process.args(["-NoLogo", "-NoProfile", "-Command", command]);
    process
}

#[cfg(not(windows))]
fn platform_shell(command: &str) -> Command {
    let mut process = Command::new("/bin/bash");
    process.args(["-lc", command]);
    process
}
