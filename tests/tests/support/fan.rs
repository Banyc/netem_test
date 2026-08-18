use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use netem_test::{NetemConfig, NetemPair};

/// First owner inside the fan scope: signals every flow child to stop if the
/// scope body unwinds (a panicked `NetemPair::spawn` or socket setup), so the
/// scope's implicit join of the flow threads sees them exit instead of
/// hanging before the panic is re-raised.
struct StopOnDrop(Arc<AtomicBool>);

impl Drop for StopOnDrop {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}

pub struct PerFlowNetem {
    addr: std::net::SocketAddr,
    stop: Arc<AtomicBool>,
    /// Handle of the main fan thread; `None` once joined.
    thread: std::sync::Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl PerFlowNetem {
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
        let thread = std::thread::Builder::new()
            .name("netem-fan".into())
            .spawn(move || {
                std::thread::scope(|flow_scope| {
                    // First owner: an unwind anywhere below signals the flow
                    // children before the scope joins them, so a panic cannot
                    // detach a live flow thread.
                    let stop_on_drop = StopOnDrop(Arc::clone(&stop_front));
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
                                    let flow_thread = std::thread::Builder::new()
                                        .name("netem-fan-flow".into())
                                        .spawn_scoped(flow_scope, move || {
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
                                                                == std::io::ErrorKind::TimedOut => {
                                                    }
                                                    Err(_) => break,
                                                }
                                            }
                                        })
                                        .unwrap();
                                    let _ = flow_thread;
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
                    drop(stop_on_drop);
                })
            })?;
        Ok(Self {
            addr,
            stop,
            thread: std::sync::Mutex::new(Some(thread)),
        })
    }
    pub fn client_addr(&self) -> std::net::SocketAddr {
        self.addr
    }
    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for PerFlowNetem {
    fn drop(&mut self) {
        self.stop();
    }
}
