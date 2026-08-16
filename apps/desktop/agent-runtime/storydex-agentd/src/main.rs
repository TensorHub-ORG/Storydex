use anyhow::Context;
use anyhow::Result;
use clap::Parser;
use serde_json::json;
use std::io::Write;
use std::path::PathBuf;
use std::time::Duration;
use storydex_agentd::AppState;
use storydex_agentd::SERVICE_NAME;
use storydex_agentd::default_storydex_home;
use storydex_agentd::serve;
use tokio::net::TcpListener;
use tracing::Level;
use tracing::info;
use uuid::Uuid;

#[derive(Parser)]
#[command(name = SERVICE_NAME, version, about = "Storydex Rust Agent sidecar")]
struct Args {
    #[arg(long, default_value_t = 0)]
    port: u16,
    #[arg(long, default_value_t = 5_000)]
    shutdown_timeout_ms: u64,
    #[arg(long)]
    coomi_home: Option<PathBuf>,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .json()
        .with_max_level(Level::INFO)
        .with_writer(std::io::stderr)
        .init();
    if let Err(error) = run().await {
        tracing::error!(error = %error, "storydex-agentd stopped with an error");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let args = Args::parse();
    anyhow::ensure!(
        (100..=30_000).contains(&args.shutdown_timeout_ms),
        "shutdown timeout must be between 100ms and 30000ms"
    );
    let listener = TcpListener::bind(("127.0.0.1", args.port))
        .await
        .context("failed to bind storydex-agentd to loopback")?;
    let address = listener
        .local_addr()
        .context("failed to read bound address")?;
    let token = Uuid::new_v4().simple().to_string();
    let coomi_home = args.coomi_home.unwrap_or_else(default_storydex_home);
    let state = AppState::with_home(token.clone(), coomi_home)?;
    let shutdown = state.shutdown_token();
    let signal_shutdown = shutdown.clone();
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            signal_shutdown.cancel();
        }
    });

    let ready = json!({
        "event": "ready",
        "runtime": SERVICE_NAME,
        "port": address.port(),
        "token": token,
        "version": env!("CARGO_PKG_VERSION"),
    });
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{}", serde_json::to_string(&ready)?)?;
    stdout.flush()?;
    info!(
        port = address.port(),
        "storydex-agentd listening on loopback"
    );

    serve(
        listener,
        state,
        Duration::from_millis(args.shutdown_timeout_ms),
    )
    .await?;
    info!("storydex-agentd stopped cleanly");
    Ok(())
}
