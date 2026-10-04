//! iperf3-shaped transport: a control socket and two directional data sockets.
//! The public cookie seeds a reversible mask, not encryption or authentication.
use crate::{config::Settings, relay};
use anyhow::{bail, ensure, Context, Result};
use chacha20::{
    cipher::{KeyIvInit, StreamCipher},
    ChaCha20,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    net::IpAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
    sync::{mpsc, OwnedSemaphorePermit},
    time,
};

const PARAM_EXCHANGE: u8 = 9;
const CREATE_STREAMS: u8 = 10;
const TEST_START: u8 = 1;
const TEST_RUNNING: u8 = 2;
const TEST_END: u8 = 4;
const EXCHANGE_RESULTS: u8 = 13;
const DISPLAY_RESULTS: u8 = 14;
const IPERF_DONE: u8 = 16;
const MAX_JSON: usize = 4096;
type Cookie = [u8; 37];

struct DataSocket {
    stream: TcpStream,
    order: u64,
    _permit: OwnedSemaphorePermit,
}
struct Session {
    peer: IpAddr,
    data: mpsc::Sender<DataSocket>,
    remaining: usize,
}
#[derive(Clone, Default)]
pub struct ServerSessions(Arc<Mutex<HashMap<Cookie, Session>>>);
struct Registration {
    sessions: ServerSessions,
    cookie: Cookie,
}
impl Drop for Registration {
    fn drop(&mut self) {
        self.sessions.0.lock().unwrap().remove(&self.cookie);
    }
}

fn new_cookie() -> Result<Cookie> {
    let mut cookie = [0; 37];
    getrandom::fill(&mut cookie[..36])
        .map_err(|e| anyhow::anyhow!("generate iperf3 cookie: {e}"))?;
    // Same alphabet and NUL termination as iperf3 make_cookie().
    let alphabet = b"abcdefghijklmnopqrstuvwxyz234567";
    for byte in &mut cookie[..36] {
        *byte = alphabet[usize::from(*byte) % alphabet.len()];
    }
    Ok(cookie)
}
async fn read_cookie(stream: &mut TcpStream) -> Result<Cookie> {
    let mut cookie = [0; 37];
    stream.read_exact(&mut cookie).await?;
    ensure!(
        cookie[36] == 0 && cookie[..36].iter().all(u8::is_ascii_alphanumeric),
        "invalid iperf3 cookie"
    );
    Ok(cookie)
}
async fn expect_state(stream: &mut TcpStream, expected: u8) -> Result<()> {
    let state = stream
        .read_u8()
        .await
        .context("iperf3 control connection closed")?;
    ensure!(state == expected, "unexpected iperf3 state {state}, expected {expected}; check both endpoints' obfuscation settings");
    Ok(())
}
async fn write_json(stream: &mut TcpStream, value: &Value) -> Result<()> {
    let bytes = serde_json::to_vec(value)?;
    ensure!(bytes.len() <= MAX_JSON, "iperf3 JSON too large");
    stream.write_u32(bytes.len() as u32).await?;
    stream.write_all(&bytes).await?;
    Ok(())
}
async fn read_json(stream: &mut TcpStream) -> Result<Value> {
    let size = stream.read_u32().await? as usize;
    ensure!((1..=MAX_JSON).contains(&size), "invalid iperf3 JSON length");
    let mut bytes = vec![0; size];
    stream.read_exact(&mut bytes).await?;
    let value: Value = serde_json::from_slice(&bytes).context("invalid iperf3 JSON")?;
    ensure!(value.is_object(), "iperf3 JSON must be an object");
    Ok(value)
}
fn parameters() -> Value {
    // A bidirectional TCP test with no time limit and one stream per direction.
    json!({"tcp": true, "omit": 0, "time": 0, "parallel": 1,
        "len": 131072, "bandwidth": 0, "bidir": true, "client_version": "3.17.1"})
}
fn check_parameters(value: &Value) -> Result<()> {
    ensure!(
        value["tcp"] == true
            && value["bidir"] == true
            && value["parallel"] == 1
            && value["time"] == 0
            && value["len"] == 131072,
        "unsupported iperf3 transport parameters"
    );
    Ok(())
}
fn results(up: u64, down: u64, seconds: f64) -> Value {
    let streams: Vec<Value> = [up, down]
        .into_iter()
        .enumerate()
        .map(|(i, bytes)| {
            json!({"id": i + 1, "bytes": bytes, "retransmits": -1, "jitter": 0,
            "errors": 0, "packets": 0, "start_time": 0, "end_time": seconds})
        })
        .collect();
    json!({"cpu_util_total": 0, "cpu_util_user": 0, "cpu_util_system": 0,
        "sender_has_retransmits": 0, "streams": streams})
}
fn mask(cookie: &Cookie, direction: u8) -> ChaCha20 {
    let mut hash = Sha256::new();
    hash.update(cookie);
    hash.update([direction]);
    ChaCha20::new(&hash.finalize(), &[0u8; 12].into())
}
async fn masked_copy<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    mut source: R,
    mut destination: W,
    cookie: &Cookie,
    direction: u8,
    size: usize,
) -> Result<u64> {
    let mut cipher = mask(cookie, direction);
    let mut buffer = vec![0; size];
    let mut total = 0;
    loop {
        let n = source.read(&mut buffer).await?;
        if n == 0 {
            destination.shutdown().await?;
            return Ok(total);
        }
        cipher
            .try_apply_keystream(&mut buffer[..n])
            .map_err(|_| anyhow::anyhow!("iperf3 mask exhausted"))?;
        destination.write_all(&buffer[..n]).await?;
        total += n as u64;
    }
}

pub async fn client(
    mut local: TcpStream,
    target: std::net::SocketAddr,
    s: &Settings,
) -> Result<(u64, u64)> {
    let cookie = new_cookie()?;
    let (mut control, mut upload, mut download) =
        time::timeout(Duration::from_secs(s.connect_timeout_secs), async {
            let mut control = relay::connect(target, s).await?;
            control.write_all(&cookie).await?;
            expect_state(&mut control, PARAM_EXCHANGE).await?;
            write_json(&mut control, &parameters()).await?;
            expect_state(&mut control, CREATE_STREAMS).await?;
            // Connect sequentially: the server assigns stream direction by accept order.
            let mut upload = relay::connect(target, s).await?;
            upload.write_all(&cookie).await?;
            let mut download = relay::connect(target, s).await?;
            download.write_all(&cookie).await?;
            expect_state(&mut control, TEST_START).await?;
            expect_state(&mut control, TEST_RUNNING).await?;
            Ok::<_, anyhow::Error>((control, upload, download))
        })
        .await
        .context("iperf3 client handshake timed out")??;
    let start = Instant::now();
    let (read, write) = local.split();
    let transfer = async {
        tokio::try_join!(
            masked_copy(read, &mut upload, &cookie, 0, s.buffer_bytes),
            masked_copy(&mut download, write, &cookie, 1, s.buffer_bytes)
        )
    };
    let (up, down) = tokio::select! {
        result = transfer => result?,
        state = control.read_u8() => bail!("iperf3 control interrupted during transfer: {state:?}"),
    };
    time::timeout(Duration::from_secs(s.connect_timeout_secs), async {
        control.write_u8(TEST_END).await?;
        expect_state(&mut control, EXCHANGE_RESULTS).await?;
        write_json(
            &mut control,
            &results(up, down, start.elapsed().as_secs_f64()),
        )
        .await?;
        read_json(&mut control).await?;
        expect_state(&mut control, DISPLAY_RESULTS).await?;
        control.write_u8(IPERF_DONE).await?;
        Ok::<_, anyhow::Error>(())
    })
    .await
    .context("iperf3 result exchange timed out")??;
    Ok((up, down))
}

impl ServerSessions {
    /// Data connections hand their permit to the control session; only that
    /// session opens the real target and accounts for application bytes.
    pub async fn accept(
        &self,
        mut control: TcpStream,
        target: std::net::SocketAddr,
        s: &Settings,
        permit: OwnedSemaphorePermit,
        order: u64,
    ) -> Result<Option<(u64, u64)>> {
        let peer = control.peer_addr()?.ip();
        let cookie = time::timeout(
            Duration::from_secs(s.connect_timeout_secs),
            read_cookie(&mut control),
        )
        .await
        .context("iperf3 cookie timed out")??;
        let (tx, mut rx) = mpsc::channel(2);
        {
            let mut sessions = self.0.lock().unwrap();
            if let Some(session) = sessions.get_mut(&cookie) {
                ensure!(
                    session.peer == peer && session.remaining > 0,
                    "unexpected iperf3 data connection"
                );
                session
                    .data
                    .try_send(DataSocket {
                        stream: control,
                        order,
                        _permit: permit,
                    })
                    .map_err(|_| anyhow::anyhow!("iperf3 data session unavailable"))?;
                session.remaining -= 1;
                return Ok(None);
            }
            sessions.insert(
                cookie,
                Session {
                    peer,
                    data: tx,
                    remaining: 2,
                },
            );
        }
        // Removal also happens on timeout, error, or task cancellation.
        let _registration = Registration {
            sessions: self.clone(),
            cookie,
        };
        let _permit = permit;
        let (mut upload, mut download, mut local) =
            time::timeout(Duration::from_secs(s.connect_timeout_secs), async {
                control.write_u8(PARAM_EXCHANGE).await?;
                check_parameters(&read_json(&mut control).await?)?;
                control.write_u8(CREATE_STREAMS).await?;
                let first = rx.recv().await.context("iperf3 data channel closed")?;
                let second = rx.recv().await.context("iperf3 data channel closed")?;
                let (upload, download) = if first.order < second.order {
                    (first, second)
                } else {
                    (second, first)
                };
                let local = relay::connect(target, s).await?;
                control.write_all(&[TEST_START, TEST_RUNNING]).await?;
                Ok::<_, anyhow::Error>((upload, download, local))
            })
            .await
            .context("iperf3 server handshake timed out")??;
        let start = Instant::now();
        let (read, write) = local.split();
        let transfer = async {
            tokio::try_join!(
                masked_copy(&mut upload.stream, write, &cookie, 0, s.buffer_bytes),
                masked_copy(read, &mut download.stream, &cookie, 1, s.buffer_bytes)
            )
        };
        tokio::pin!(transfer);
        let (up, down) = tokio::select! {
            result = &mut transfer => {
                let counts = result?;
                time::timeout(Duration::from_secs(s.connect_timeout_secs), expect_state(&mut control, TEST_END))
                    .await.context("iperf3 test end timed out")??;
                counts
            },
            state = control.read_u8() => {
                ensure!(state? == TEST_END, "iperf3 control interrupted during transfer");
                time::timeout(Duration::from_secs(s.drain_timeout_secs), &mut transfer)
                    .await.context("iperf3 data drain timed out")??
            },
        };
        time::timeout(Duration::from_secs(s.connect_timeout_secs), async {
            control.write_u8(EXCHANGE_RESULTS).await?;
            read_json(&mut control).await?;
            write_json(
                &mut control,
                &results(up, down, start.elapsed().as_secs_f64()),
            )
            .await?;
            control.write_u8(DISPLAY_RESULTS).await?;
            expect_state(&mut control, IPERF_DONE).await?;
            Ok::<_, anyhow::Error>(())
        })
        .await
        .context("iperf3 result exchange timed out")??;
        Ok(Some((up, down)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mask_is_independent_of_tcp_chunk_boundaries_and_direction() {
        let cookie = new_cookie().unwrap();
        let plain = vec![42u8; 200_000];
        let mut wire = plain.clone();
        mask(&cookie, 0).apply_keystream(&mut wire);
        assert_ne!(wire, plain);
        let mut reverse = plain.clone();
        mask(&cookie, 1).apply_keystream(&mut reverse);
        assert_ne!(wire, reverse);
        let mut decoder = mask(&cookie, 0);
        for chunk in wire.chunks_mut(317) {
            decoder.apply_keystream(chunk);
        }
        assert_eq!(wire, plain);
    }
    #[tokio::test]
    async fn oversized_json_is_rejected_before_reading_the_body() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let mut sender = TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (mut receiver, _) = listener.accept().await.unwrap();
        sender.write_u32(u32::MAX).await.unwrap();
        assert!(read_json(&mut receiver).await.is_err());
    }
    #[tokio::test]
    async fn aborted_or_timed_out_handshakes_release_registry_and_capacity() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let sessions = ServerSessions::default();
        let capacity = Arc::new(tokio::sync::Semaphore::new(3));
        let settings = Settings {
            connect_timeout_secs: 1,
            ..Settings::default()
        };
        for abort in [true, false] {
            let mut client = TcpStream::connect(listener.local_addr().unwrap())
                .await
                .unwrap();
            let (server, _) = listener.accept().await.unwrap();
            let sessions_copy = sessions.clone();
            let settings_copy = settings.clone();
            let permit = capacity.clone().acquire_owned().await.unwrap();
            let target = "127.0.0.1:1".parse().unwrap();
            let task = tokio::spawn(async move {
                sessions_copy
                    .accept(server, target, &settings_copy, permit, 0)
                    .await
            });
            client.write_all(&new_cookie().unwrap()).await.unwrap();
            expect_state(&mut client, PARAM_EXCHANGE).await.unwrap();
            assert_eq!(sessions.0.lock().unwrap().len(), 1);
            if abort {
                task.abort();
                assert!(task.await.unwrap_err().is_cancelled());
            } else {
                assert!(task.await.unwrap().is_err());
            }
            assert!(sessions.0.lock().unwrap().is_empty());
            assert_eq!(capacity.available_permits(), 3);
        }
    }
}
