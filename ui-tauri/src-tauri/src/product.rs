use std::path::PathBuf;

#[cfg(all(target_os = "windows", target_arch = "x86_64", target_env = "msvc"))]
pub const BACKEND_SIDECAR_NAME: &str = "vernest-backend-x86_64-pc-windows-msvc.exe";

pub const LEGACY_BACKEND_SIDECAR_NAME: &str = "yanqi-backend-x86_64-pc-windows-msvc.exe";
pub const BUNDLED_BACKEND_NAME: &str = "vernest-backend.exe";
pub const LEGACY_BUNDLED_BACKEND_NAME: &str = "yanqi-backend.exe";
pub const APP_DATA_DIR_NAME: &str = "Vernest";
pub const APP_VERSION: &str = "0.6.7";
pub const COPYRIGHT: &str = "Copyright © 2026 孙欣阳. All rights reserved.";

pub fn app_data_dir() -> PathBuf {
    std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("USERPROFILE")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("."))
        })
        .join(APP_DATA_DIR_NAME)
}
