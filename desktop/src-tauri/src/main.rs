#![windows_subsystem = "windows"]

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const PANEL_PORT: u16 = 8766;
const PANEL_URL: &str = "http://127.0.0.1:8766/";

fn repo_root() -> PathBuf {
    let mut root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    root.pop();
    root.pop();
    root
}

fn python_bin(root: &PathBuf) -> PathBuf {
    let venv = root.join(".venv").join("Scripts").join("pythonw.exe");
    if venv.exists() {
        return venv;
    }
    let venv = root.join(".venv").join("Scripts").join("python.exe");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("pythonw")
    }
}

fn panel_listening() -> bool {
    TcpStream::connect_timeout(
        &SocketAddr::from(([127, 0, 0, 1], PANEL_PORT)),
        Duration::from_millis(250),
    )
    .is_ok()
}

fn pid_running(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    let output = Command::new("tasklist")
        .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
        .output();
    match output {
        Ok(out) => String::from_utf8_lossy(&out.stdout).contains(&pid.to_string()),
        Err(_) => false,
    }
}

fn live_bot_pid(root: &PathBuf) -> Option<u32> {
    let lock = root.join("data").join("bot.lock");
    let text = std::fs::read_to_string(lock).ok()?;
    let pid: u32 = text.trim().parse().ok()?;
    if pid_running(pid) {
        Some(pid)
    } else {
        None
    }
}

fn spawn_backend(root: &PathBuf) -> Result<&'static str, String> {
    if panel_listening() {
        return Ok("attached");
    }
    if let Some(_pid) = live_bot_pid(root) {
        return Ok("waiting-existing-bot");
    }
    let python = python_bin(root);
    let mut cmd = Command::new(&python);
    cmd.current_dir(root)
        .args(["-u", "run.py", "--no-panel"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x00000008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
    let _child = cmd
        .spawn()
        .map_err(|e| format!("Не удалось запустить FastAPI-панель: {e}"))?;
    Ok("started-paused")
}

fn wait_for_panel(timeout: Duration) -> bool {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if panel_listening() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

#[tauri::command]
fn panel_url() -> String {
    PANEL_URL.to_string()
}

#[tauri::command]
fn ensure_panel() -> Result<String, String> {
    let root = repo_root();
    if panel_listening() {
        return Ok("attached".into());
    }
    let status = spawn_backend(&root)?;
    if wait_for_panel(Duration::from_secs(45)) {
        return Ok(status.into());
    }
    Err("Панель FastAPI не поднялась на 127.0.0.1:8766".into())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![ensure_panel, panel_url])
        .run(tauri::generate_context!())
        .expect("error while running EmpireBot desktop");
}
