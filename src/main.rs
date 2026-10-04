mod config;
mod copy;
mod obfuscation;
mod relay;
use anyhow::{bail, Context, Result};
use std::path::PathBuf;

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .with_ansi(false)
        .init();
    let mut args = std::env::args().skip(1);
    let mut path = None;
    let mut check = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" => path = Some(PathBuf::from(args.next().context("--config needs a path")?)),
            "--check" => check = true,
            "--version" => {
                println!("stunnel-relay {}", env!("CARGO_PKG_VERSION"));
                return Ok(());
            }
            "--help" | "-h" => {
                println!("Usage: stunnel-relay --config FILE [--check]\nTCP forwarding; optional iperf3 obfuscation configured in FILE.");
                return Ok(());
            }
            _ => bail!("unknown argument: {arg}"),
        }
    }
    let path = path.context("supply --config FILE; see --help")?;
    let config = config::Config::load(&path)?;
    if check {
        println!(
            "configuration valid: {} routes, {}, obfuscation={:?}",
            config.routes.len(),
            if config.settings.obfuscation == config::Obfuscation::Iperf3 {
                "iperf3-masked-buffered"
            } else {
                copy::engine(config.settings.copy_mode)
            },
            config.settings.obfuscation,
        );
        return Ok(());
    }
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(relay::run(config))
}

async fn shutdown_signal() -> Result<()> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate())?;
        tokio::select! {result=tokio::signal::ctrl_c()=>result?,_=term.recv()=>{}}
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await?;
    Ok(())
}

fn notify(message: &str) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::{
            ffi::OsStrExt,
            net::{SocketAddr, UnixDatagram},
        };
        let Some(path) = std::env::var_os("NOTIFY_SOCKET") else {
            return Ok(());
        };
        let socket = UnixDatagram::unbound()?;
        let path = path.as_encoded_bytes();
        #[cfg(target_os = "linux")]
        if path.starts_with(b"@") {
            use std::os::linux::net::SocketAddrExt;
            socket.connect_addr(&SocketAddr::from_abstract_name(&path[1..])?)?;
            socket.send(message.as_bytes())?;
            return Ok(());
        }
        socket.connect_addr(&SocketAddr::from_pathname(std::path::Path::new(
            std::ffi::OsStr::from_bytes(path),
        ))?)?;
        socket.send(message.as_bytes())?;
    }
    Ok(())
}
