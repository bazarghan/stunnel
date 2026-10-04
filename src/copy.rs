use crate::config::CopyMode;
use std::io;
use tokio::net::TcpStream;

pub fn engine(mode: CopyMode) -> &'static str {
    if cfg!(target_os = "linux") && mode == CopyMode::Auto {
        "linux-splice"
    } else {
        "buffered"
    }
}

pub async fn transfer(
    a: &mut TcpStream,
    b: &mut TcpStream,
    mode: CopyMode,
    size: usize,
) -> io::Result<(u64, u64)> {
    #[cfg(not(target_os = "linux"))]
    let _ = mode;
    #[cfg(target_os = "linux")]
    if mode == CopyMode::Auto {
        return tokio::try_join!(splice_direction(a, b, size), splice_direction(b, a, size));
    }
    tokio::io::copy_bidirectional_with_sizes(a, b, size, size).await
}

#[cfg(target_os = "linux")]
async fn splice_direction(
    source: &TcpStream,
    destination: &TcpStream,
    size: usize,
) -> io::Result<u64> {
    use rustix::pipe::{pipe_with, splice, PipeFlags, SpliceFlags};
    use tokio::io::Interest;
    let (read_pipe, write_pipe) = pipe_with(PipeFlags::CLOEXEC | PipeFlags::NONBLOCK)?;
    let mut total = 0;
    loop {
        source.readable().await?;
        let n = match source.try_io(Interest::READABLE, || {
            splice(
                source,
                None,
                &write_pipe,
                None,
                size,
                SpliceFlags::NONBLOCK | SpliceFlags::MOVE,
            )
            .map_err(io::Error::from)
        }) {
            Ok(n) => n,
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                continue
            }
            Err(e) => return Err(e),
        };
        if n == 0 {
            // EOF affects only this direction; the reverse direction can still drain.
            socket2::SockRef::from(destination).shutdown(std::net::Shutdown::Write)?;
            return Ok(total);
        }
        let mut remaining = n;
        while remaining > 0 {
            destination.writable().await?;
            match destination.try_io(Interest::WRITABLE, || {
                splice(
                    &read_pipe,
                    None,
                    destination,
                    None,
                    remaining,
                    SpliceFlags::NONBLOCK | SpliceFlags::MOVE,
                )
                .map_err(io::Error::from)
            }) {
                Ok(0) => {
                    return Err(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "splice returned zero with pending bytes",
                    ))
                }
                Ok(written) => {
                    remaining -= written;
                    total += written as u64;
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                    ) =>
                {
                    continue
                }
                Err(e) => return Err(e),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        time::{timeout, Duration},
    };
    async fn pair() -> (TcpStream, TcpStream) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (client, accepted) = tokio::join!(TcpStream::connect(address), listener.accept());
        (client.unwrap(), accepted.unwrap().0)
    }
    async fn half_close(mode: CopyMode) {
        timeout(Duration::from_secs(10), async {
            let (mut client, mut a) = pair().await;
            let (mut b, mut backend) = pair().await;
            let data: Vec<u8> = (0..1_048_576).map(|n| (n % 251) as u8).collect();
            let expected = data.clone();
            let relay =
                tokio::spawn(async move { transfer(&mut a, &mut b, mode, 65536).await.unwrap() });
            let server = tokio::spawn(async move {
                let mut request = Vec::new();
                backend.read_to_end(&mut request).await.unwrap();
                assert_eq!(request, expected);
                backend.write_all(&request).await.unwrap();
                backend.shutdown().await.unwrap();
            });
            client.write_all(&data).await.unwrap();
            client.shutdown().await.unwrap();
            let mut response = Vec::new();
            client.read_to_end(&mut response).await.unwrap();
            assert_eq!(response, data);
            server.await.unwrap();
            assert_eq!(relay.await.unwrap(), (1_048_576, 1_048_576));
        })
        .await
        .unwrap();
    }
    async fn full_duplex(mode: CopyMode) {
        timeout(Duration::from_secs(10), async {
            let (client, mut a) = pair().await;
            let (mut b, backend) = pair().await;
            let relay =
                tokio::spawn(async move { transfer(&mut a, &mut b, mode, 4096).await.unwrap() });
            async fn endpoint(stream: TcpStream, send: u8, receive: u8) {
                let (mut reader, mut writer) = stream.into_split();
                let sending = async {
                    writer.write_all(&vec![send; 2_097_152]).await.unwrap();
                    writer.shutdown().await.unwrap();
                };
                let receiving = async {
                    let mut data = Vec::new();
                    reader.read_to_end(&mut data).await.unwrap();
                    assert_eq!(data, vec![receive; 2_097_152]);
                };
                tokio::join!(sending, receiving);
            }
            tokio::join!(endpoint(client, 17, 29), endpoint(backend, 29, 17));
            assert_eq!(relay.await.unwrap(), (2_097_152, 2_097_152));
        })
        .await
        .unwrap();
    }
    #[tokio::test]
    async fn buffered_preserves_half_close() {
        half_close(CopyMode::Buffered).await;
    }
    #[tokio::test]
    async fn auto_preserves_half_close() {
        half_close(CopyMode::Auto).await;
    }
    #[tokio::test]
    async fn buffered_handles_full_duplex_and_partial_writes() {
        full_duplex(CopyMode::Buffered).await;
    }
    #[tokio::test]
    async fn auto_handles_full_duplex_and_partial_writes() {
        full_duplex(CopyMode::Auto).await;
    }
    #[tokio::test]
    async fn reset_does_not_leave_relay_hanging() {
        let (mut client, mut a) = pair().await;
        let (mut b, backend) = pair().await;
        let relay =
            tokio::spawn(async move { transfer(&mut a, &mut b, CopyMode::Auto, 65536).await });
        socket2::SockRef::from(&backend)
            .set_linger(Some(Duration::ZERO))
            .unwrap();
        drop(backend);
        let _ = client.write_all(&[1; 8192]).await;
        timeout(Duration::from_secs(3), relay)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
    }
}
