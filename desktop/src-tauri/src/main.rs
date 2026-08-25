#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, State};

struct Engine {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
}

impl Default for Engine {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
            stdin: Mutex::new(None),
        }
    }
}

fn repo_root() -> PathBuf {
    let mut root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    root.pop();
    root.pop();
    root
}

fn python_bin(root: &PathBuf) -> PathBuf {
    let venv = root.join(".venv").join("Scripts").join("python.exe");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python")
    }
}

#[tauri::command]
fn engine_root() -> String {
    repo_root().display().to_string()
}

#[tauri::command]
fn start_engine(app: AppHandle, engine: State<Engine>) -> Result<String, String> {
    let mut child_guard = engine.child.lock().map_err(|e| e.to_string())?;
    if let Some(child) = child_guard.as_mut() {
        if child.try_wait().map_err(|e| e.to_string())?.is_none() {
            return Ok("already-running".into());
        }
    }
    let root = repo_root();
    let python = python_bin(&root);
    let mut child = Command::new(python)
        .current_dir(&root)
        .args(["-u", "run.py", "worker"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Не удалось запустить Python-движок: {e}"))?;
    let stdout = child.stdout.take().ok_or("нет stdout")?;
    let stderr = child.stderr.take().ok_or("нет stderr")?;
    let stdin = child.stdin.take().ok_or("нет stdin")?;
    let app_out = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().flatten() {
            let _ = app_out.emit("engine-line", line);
        }
        let _ = app_out.emit(
            "engine-line",
            r#"{"type":"log","event":"worker.stdout_closed","level":"WARNING"}"#,
        );
    });
    let app_err = app.clone();
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().flatten() {
            let payload = serde_json::json!({
                "type": "log",
                "event": "python.stderr",
                "level": "DEBUG",
                "message": line,
            });
            let _ = app_err.emit("engine-line", payload.to_string());
        }
    });
    *engine.stdin.lock().map_err(|e| e.to_string())? = Some(stdin);
    *child_guard = Some(child);
    Ok("started".into())
}

#[tauri::command]
fn send_engine_cmd(engine: State<Engine>, command: Value) -> Result<(), String> {
    let mut stdin = engine.stdin.lock().map_err(|e| e.to_string())?;
    let handle = stdin.as_mut().ok_or("движок не запущен")?;
    writeln!(handle, "{command}").map_err(|e| e.to_string())?;
    handle.flush().map_err(|e| e.to_string())
}

#[tauri::command]
fn stop_engine(engine: State<Engine>) -> Result<(), String> {
    if let Ok(mut stdin) = engine.stdin.lock() {
        if let Some(handle) = stdin.as_mut() {
            let _ = writeln!(handle, r#"{{"cmd":"stop"}}"#);
            let _ = handle.flush();
        }
        *stdin = None;
    }
    if let Ok(mut child) = engine.child.lock() {
        if let Some(mut proc) = child.take() {
            let _ = proc.kill();
        }
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(Engine::default())
        .invoke_handler(tauri::generate_handler![
            engine_root,
            start_engine,
            send_engine_cmd,
            stop_engine
        ])
        .run(tauri::generate_context!())
        .expect("error while running EmpireBot desktop");
}
