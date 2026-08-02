use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};

pub struct NetemFan {
    addr: std::net::SocketAddr,
    stop: Arc<AtomicBool>,
}

impl NetemFan {
    pub fn spawn(
        server_addr: std::net::SocketAddr,
        config: impl Fn() -> (NetemConfig, NetemConfig) + Send + 'static,
    ) -> std::io::Result<Self> {
        use std::net::UdpSocket;
        let front = UdpSocket::bind("127.0.0.1:0")?;
        front.set_read_timeout(Some(Duration::from_millis(5)))?;
        let addr = front.local_addr()?;
        let stop = Arc::new(AtomicBool::new(false));
        let stop_front = Arc::clone(&stop);
        std::thread::Builder::new()
            .name("netem-fan".into())
            .spawn(move || {
                let front = Arc::new(front);
                let mut flows: HashMap<std::net::SocketAddr, UdpSocket> = HashMap::new();
                let mut pairs: Vec<NetemPair> = Vec::new();
                let mut buf = [0u8; 65535];
                while !stop_front.load(Ordering::Relaxed) {
                    match front.recv_from(&mut buf) {
                        Ok((n, from)) => {
                            let sock = flows.entry(from).or_insert_with(|| {
                                let (c2s, s2c) = config();
                                let pair = NetemPair::spawn(server_addr, c2s, s2c)
                                    .expect("spawn per-flow NetemPair");
                                let sock = UdpSocket::bind("127.0.0.1:0").unwrap();
                                sock.connect(pair.client_addr()).unwrap();
                                sock.set_read_timeout(Some(Duration::from_millis(5)))
                                    .unwrap();
                                let back = sock.try_clone().unwrap();
                                let front = Arc::clone(&front);
                                let stop = Arc::clone(&stop_front);
                                std::thread::Builder::new()
                                    .name("netem-fan-flow".into())
                                    .spawn(move || {
                                        let mut buf = [0u8; 65535];
                                        while !stop.load(Ordering::Relaxed) {
                                            match back.recv(&mut buf) {
                                                Ok(n) => {
                                                    let _ = front.send_to(&buf[..n], from);
                                                }
                                                Err(e)
                                                    if e.kind()
                                                        == std::io::ErrorKind::WouldBlock
                                                        || e.kind()
                                                            == std::io::ErrorKind::TimedOut => {}
                                                Err(_) => break,
                                            }
                                        }
                                    })
                                    .unwrap();
                                pairs.push(pair);
                                sock
                            });
                            let _ = sock.send(&buf[..n]);
                        }
                        Err(e)
                            if e.kind() == std::io::ErrorKind::WouldBlock
                                || e.kind() == std::io::ErrorKind::TimedOut => {}
                        Err(_) => break,
                    }
                }
                for pair in &pairs {
                    pair.stop();
                }
            })?;
        Ok(Self { addr, stop })
    }
    pub fn client_addr(&self) -> std::net::SocketAddr {
        self.addr
    }
    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
