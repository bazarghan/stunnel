use crate::{
    config::{Config, Route, Settings},
    copy,
};
use anyhow::{Context, Result};
use socket2::{Domain, Protocol, SockRef, Socket, TcpKeepalive, Type};
use std::{
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{mpsc, OwnedSemaphorePermit, Semaphore},
    task::JoinSet,
    time,
};
use tracing::{debug, info, warn};

#[derive(Default)]
struct Stats {
    accepted: AtomicU64,
    denied: AtomicU64,
    overloaded: AtomicU64,
    completed: AtomicU64,
    failed: AtomicU64,
    uploaded: AtomicU64,
    downloaded: AtomicU64,
}
struct Incoming {
    stream: TcpStream,
    route: Arc<Route>,
    permit: OwnedSemaphorePermit,
}
fn tune(stream: &TcpStream, s: &Settings) -> std::io::Result<()> {
    stream.set_nodelay(true)?;
    let socket = SockRef::from(stream);
    socket.set_keepalive(true)?;
    let keepalive = TcpKeepalive::new()
        .with_time(Duration::from_secs(s.keepalive_idle_secs))
        .with_interval(Duration::from_secs(s.keepalive_interval_secs))
        .with_retries(s.keepalive_retries);
    socket.set_tcp_keepalive(&keepalive)?;
    #[cfg(target_os = "linux")]
    socket.set_tcp_user_timeout(Some(Duration::from_secs(s.tcp_user_timeout_secs)))?;
    Ok(())
}
fn bind(route: &Route) -> Result<TcpListener> {
    let socket = Socket::new(
        Domain::for_address(route.listen),
        Type::STREAM,
        Some(Protocol::TCP),
    )?;
    socket.set_reuse_address(true)?;
    if route.listen.is_ipv6() {
        socket.set_only_v6(true)?;
    }
    socket.set_nonblocking(true)?;
    socket
        .bind(&route.listen.into())
        .with_context(|| format!("bind {} ({})", route.name, route.listen))?;
    socket.listen(512)?;
    Ok(TcpListener::from_std(socket.into())?)
}
async fn connect_and_copy(
    mut stream: TcpStream,
    route: &Route,
    s: &Settings,
) -> Result<(u64, u64)> {
    tune(&stream, s)?;
    let mut target = time::timeout(
        Duration::from_secs(s.connect_timeout_secs),
        TcpStream::connect(route.target),
    )
    .await
    .context("destination connection timed out")?
    .context("destination connection failed")?;
    tune(&target, s)?;
    Ok(copy::transfer(&mut stream, &mut target, s.copy_mode, s.buffer_bytes).await?)
}
fn log_stats(stats: &Stats, capacity: &Semaphore, max: usize) {
    info!(
        active = max - capacity.available_permits(),
        accepted = stats.accepted.load(Ordering::Relaxed),
        denied = stats.denied.load(Ordering::Relaxed),
        overloaded = stats.overloaded.load(Ordering::Relaxed),
        completed = stats.completed.load(Ordering::Relaxed),
        failed = stats.failed.load(Ordering::Relaxed),
        completed_upload_bytes = stats.uploaded.load(Ordering::Relaxed),
        completed_download_bytes = stats.downloaded.load(Ordering::Relaxed),
        "relay statistics"
    );
}

pub async fn run(config: Config) -> Result<()> {
    let settings = Arc::new(config.settings);
    let semaphore = Arc::new(Semaphore::new(settings.max_connections));
    let stats = Arc::new(Stats::default());
    let (tx, mut rx) = mpsc::channel::<Incoming>(settings.max_connections);
    let mut listeners = JoinSet::new();
    let mut connections = JoinSet::new();
    // Bind every route before accepting any traffic or reporting service readiness.
    let mut bound = Vec::new();
    for route in config.routes {
        let listener = bind(&route)?;
        bound.push((Arc::new(route), listener));
    }
    for (route, listener) in bound {
        info!(route=%route.name,listen=%route.listen,target=%route.target,engine=copy::engine(settings.copy_mode),"listening");
        let tx = tx.clone();
        let semaphore = semaphore.clone();
        let stats = stats.clone();
        listeners.spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, peer)) => {
                        if !route.allows(peer.ip()) {
                            stats.denied.fetch_add(1, Ordering::Relaxed);
                            drop(stream);
                            continue;
                        }
                        let permit = match semaphore.clone().try_acquire_owned() {
                            Ok(p) => p,
                            Err(_) => {
                                stats.overloaded.fetch_add(1, Ordering::Relaxed);
                                drop(stream);
                                continue;
                            }
                        };
                        stats.accepted.fetch_add(1, Ordering::Relaxed);
                        if tx
                            .send(Incoming {
                                stream,
                                route: route.clone(),
                                permit,
                            })
                            .await
                            .is_err()
                        {
                            break;
                        }
                    }
                    Err(e) => {
                        warn!(route=%route.name,error=%e,"accept failed; backing off");
                        time::sleep(Duration::from_millis(250)).await;
                    }
                }
            }
        });
    }
    drop(tx);
    crate::notify("READY=1\nSTATUS=TCP listeners ready")?;
    let mut ticker = time::interval(Duration::from_secs(settings.stats_interval_secs));
    let stop = crate::shutdown_signal();
    tokio::pin!(stop);
    loop {
        tokio::select! {
            signal=&mut stop=>{signal?;break;}
            incoming=rx.recv()=>{
                let Some(incoming)=incoming else{anyhow::bail!("all listener tasks exited")};
                let settings=settings.clone();let stats=stats.clone();
                connections.spawn(async move{
                    let _permit=incoming.permit;
                    match connect_and_copy(incoming.stream,&incoming.route,&settings).await {
                        Ok((up,down))=>{
                            stats.completed.fetch_add(1,Ordering::Relaxed);stats.uploaded.fetch_add(up,Ordering::Relaxed);stats.downloaded.fetch_add(down,Ordering::Relaxed);
                            debug!(route=%incoming.route.name,uploaded=up,downloaded=down,"connection completed");
                        }
                        Err(e)=>{stats.failed.fetch_add(1,Ordering::Relaxed);warn!(route=%incoming.route.name,error=%e,"connection failed");}
                    }
                });
            }
            joined=connections.join_next(),if !connections.is_empty()=>{
                if let Some(Err(e))=joined{warn!(error=%e,"connection task failed");}
            }
            joined=listeners.join_next(),if !listeners.is_empty()=>{
                anyhow::bail!("listener task exited unexpectedly: {joined:?}");
            }
            _=ticker.tick()=>log_stats(&stats,&semaphore,settings.max_connections),
        }
    }
    crate::notify("STOPPING=1\nSTATUS=Draining active connections")?;
    listeners.abort_all();
    while listeners.join_next().await.is_some() {}
    rx.close();
    while rx.try_recv().is_ok() {}
    info!(
        active = connections.len(),
        grace_secs = settings.drain_timeout_secs,
        "listeners stopped; draining connections"
    );
    if time::timeout(Duration::from_secs(settings.drain_timeout_secs), async {
        while connections.join_next().await.is_some() {}
    })
    .await
    .is_err()
    {
        warn!(
            remaining = connections.len(),
            "grace period expired; closing remaining connections"
        );
        connections.abort_all();
        while connections.join_next().await.is_some() {}
    }
    log_stats(&stats, &semaphore, settings.max_connections);
    Ok(())
}
