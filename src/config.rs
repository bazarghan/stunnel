use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::{
    collections::HashSet,
    net::{IpAddr, SocketAddr},
    path::Path,
};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CopyMode {
    Auto,
    Buffered,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Settings {
    pub max_connections: usize,
    pub buffer_bytes: usize,
    pub connect_timeout_secs: u64,
    pub drain_timeout_secs: u64,
    pub keepalive_idle_secs: u64,
    pub keepalive_interval_secs: u64,
    pub keepalive_retries: u32,
    pub tcp_user_timeout_secs: u64,
    pub stats_interval_secs: u64,
    pub copy_mode: CopyMode,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            max_connections: 512,
            buffer_bytes: 65536,
            connect_timeout_secs: 5,
            drain_timeout_secs: 30,
            keepalive_idle_secs: 30,
            keepalive_interval_secs: 10,
            keepalive_retries: 3,
            tcp_user_timeout_secs: 45,
            stats_interval_secs: 30,
            copy_mode: CopyMode::Auto,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub name: String,
    pub listen: SocketAddr,
    pub target: SocketAddr,
    #[serde(default)]
    pub allowed_ips: Vec<IpAddr>,
}
impl Route {
    pub fn allows(&self, ip: IpAddr) -> bool {
        let ip = match ip {
            IpAddr::V6(v6) => v6.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(ip),
            _ => ip,
        };
        self.allowed_ips.is_empty() || self.allowed_ips.contains(&ip)
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub settings: Settings,
    pub routes: Vec<Route>,
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
        let config: Self = serde_json::from_slice(&bytes).context("parse configuration JSON")?;
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<()> {
        let s = &self.settings;
        if self.routes.is_empty() {
            bail!("at least one route is required");
        }
        if !(1..=100_000).contains(&s.max_connections) {
            bail!("max_connections must be 1..100000");
        }
        if !(4096..=1_048_576).contains(&s.buffer_bytes) {
            bail!("buffer_bytes must be 4096..1048576");
        }
        for (name, value) in [
            ("connect_timeout_secs", s.connect_timeout_secs),
            ("drain_timeout_secs", s.drain_timeout_secs),
            ("keepalive_idle_secs", s.keepalive_idle_secs),
            ("keepalive_interval_secs", s.keepalive_interval_secs),
            ("tcp_user_timeout_secs", s.tcp_user_timeout_secs),
            ("stats_interval_secs", s.stats_interval_secs),
        ] {
            if !(1..=3600).contains(&value) {
                bail!("{name} must be 1..3600");
            }
        }
        if !(1..=10).contains(&s.keepalive_retries) {
            bail!("keepalive_retries must be 1..10");
        }
        let mut names = HashSet::new();
        let mut listens = HashSet::new();
        for route in &self.routes {
            if route.name.is_empty() || !names.insert(&route.name) {
                bail!("route names must be nonempty and unique");
            }
            if route.listen.port() == 0 || route.target.port() == 0 {
                bail!("route ports must be nonzero");
            }
            if route.target.ip().is_unspecified() {
                bail!("target cannot be an unspecified address");
            }
            if route.listen == route.target {
                bail!("route cannot forward to itself");
            }
            if !listens.insert(route.listen) {
                bail!("duplicate listening address");
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        serde_json::from_str(r#"{"routes":[{"name":"vpn","listen":"127.0.0.1:5500","target":"127.0.0.1:5501","allowed_ips":["127.0.0.1"]}]}"#).unwrap()
    }
    #[test]
    fn rejects_invalid_limits_and_duplicate_listeners() {
        let mut c = config();
        c.settings.max_connections = 0;
        assert!(c.validate().is_err());
        let mut c = config();
        let mut duplicate = c.routes[0].clone();
        duplicate.name = "other".into();
        c.routes.push(duplicate);
        assert!(c.validate().is_err());
    }
    #[test]
    fn rejects_unknown_settings() {
        assert!(serde_json::from_str::<Config>(
            r#"{"routes":[],"settings":{"connect_timout_secs":5}}"#
        )
        .is_err());
    }
    #[test]
    fn allowlist_accepts_only_configured_ip_and_normalizes_mapped_v4() {
        let c = config();
        let r = &c.routes[0];
        assert!(r.allows("127.0.0.1".parse().unwrap()));
        assert!(r.allows("::ffff:127.0.0.1".parse().unwrap()));
        assert!(!r.allows("127.0.0.2".parse().unwrap()));
        assert!(c.validate().is_ok());
    }
}
