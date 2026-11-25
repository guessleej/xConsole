// xConsole 桌面版：把內含的 Python 帶起節點代理與主控台，開一個視窗顯示主控台。
//
// - 代理只聽 127.0.0.1；主控台的視窗走只聽 127.0.0.1 的本機通道，用每次啟動隨機產生的鑰匙進入（鑰匙走標準輸入
//   交給主控台、放在網址 # 後面給視窗，不寫檔、不進指令列與環境變數）。別台電腦要連進來，到設定打開「區網存取」，走 HTTPS＋密碼。
// - 子行程掛了（含主控台更新完、切換區網存取時自己結束）就重新帶起；應用程式結束時子行程看到父行程不在，自己收掉（含模型服務）。
// - 關視窗＝縮到系統匣，模型照常服務；系統匣選「結束 xConsole」才真的停。同一時間只開一份，再點捷徑就把視窗叫出來。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};

const MAIN: &str = "main";

struct Ports {
    https: u16,
    agent: u16,
    app: u16,
}

struct Runtime {
    key: String,
    ports: Ports,
    data: PathBuf,
    payload: PathBuf,
    ready: AtomicBool,
    quitting: AtomicBool,
    /// 背景服務一直起不來時的診斷（啟動畫面顯示）：有值就不再重試，免得Windows每兩秒跳一次「已封鎖」通知
    fault: Mutex<Option<String>>,
}

/// 連續幾次「起不來或一起來就掉」就停手。啟動後撐過這麼久才算起來了。
const FAIL_LIMIT: u32 = 3;
const STARTUP_GRACE: Duration = Duration::from_secs(20);

struct Watch {
    child: Option<Child>,
    since: Instant,
    fails: u32,
    last_err: Option<std::io::Error>,
}

impl Watch {
    /// 不帶起任何東西（受管的機器不帶起自己的代理）
    fn idle() -> Watch {
        Watch { child: None, since: Instant::now(), fails: 0, last_err: None }
    }
    fn start(r: std::io::Result<Child>) -> Watch {
        let mut w = Watch { child: None, since: Instant::now(), fails: 0, last_err: None };
        w.take(r);
        w
    }
    fn take(&mut self, r: std::io::Result<Child>) {
        self.since = Instant::now();
        match r {
            Ok(c) => self.child = Some(c),
            Err(e) => {
                self.child = None;
                self.fails += 1;
                self.last_err = Some(e);
            }
        }
    }
    /// 子行程結束了嗎；一起來就結束（撐不過STARTUP_GRACE）算一次失敗，撐過了就把失敗次數歸零
    fn dead(&mut self) -> bool {
        let gone = self.child.as_mut().is_none_or(|p| matches!(p.try_wait(), Ok(Some(_))));
        if !gone && self.since.elapsed() >= STARTUP_GRACE {
            self.fails = 0;
        }
        if gone && self.child.is_some() {
            self.child = None;
            if self.since.elapsed() < STARTUP_GRACE {
                self.fails += 1;
            } else {
                self.fails = 0;
            }
        }
        gone
    }
}

/// Windows 11的「智慧型應用程式控制」狀態：0關閉、1開啟（會擋沒有簽章的程式）、2評估模式（只記錄不擋）。
fn smart_app_control() -> Option<u32> {
    if !cfg!(windows) {
        return None;
    }
    let mut c = Command::new("reg");
    c.args(["query", r"HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy", "/v", "VerifiedAndReputablePolicyState"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000);
    }
    let out = c.output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let hex = text.split_whitespace().find(|t| t.starts_with("0x"))?;
    u32::from_str_radix(hex.trim_start_matches("0x"), 16).ok()
}

/// 記錄檔最後幾行（給畫面顯示；記錄裡沒有機密：權杖與鑰匙都不寫記錄）
fn log_tail(data: &Path, name: &str) -> String {
    let text = fs::read(data.join(".xconsole").join(name)).map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
    let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    lines[lines.len().saturating_sub(12)..].join("\n")
}

/// Windows用來表示「被應用程式控制原則擋下」的錯誤碼：4551是智慧型應用程式控制／WDAC，1260是群組原則
fn blocked_by_policy(e: &Option<std::io::Error>, tail: &str) -> bool {
    matches!(e.as_ref().and_then(|e| e.raw_os_error()), Some(4551) | Some(1260))
        || ["4551", "Application Control", "應用程式控制", "has blocked this file"].iter().any(|k| tail.contains(k))
}

/// 整理成啟動畫面看得懂的診斷：種類|記錄檔位置|最後幾行
fn diagnose(rt: &Runtime, w: &Watch, log: &str) -> String {
    let tail = log_tail(&rt.data, log);
    // 只有錯誤碼或記錄裡真的看得到「被擋」才叫人處理智慧型應用程式控制：它關掉之後要重灌Windows才能再開，
    // 不能因為這台剛好開著它，就把腳本錯誤、檔案損壞也怪到它頭上。登錄檔狀態只當參考寫在記錄後面
    let kind = if blocked_by_policy(&w.last_err, &tail) { "sac" } else { "start" };
    let note = if kind == "start" && smart_app_control() == Some(1) {
        "\n（這台開著智慧型應用程式控制，但記錄裡沒有看到被它擋下的跡象）"
    } else {
        ""
    };
    let err = w.last_err.as_ref().map(|e| format!("{}\n", e)).unwrap_or_default();
    let path = rt.data.join(".xconsole").join(log);
    format!("{}|{}|{}{}{}", kind, path.display(), err, tail, note)
}

/// 視窗可能已經切到主控台（主控台短暫起來過）：主控台頁面不認 #fault，所以關掉重開一個啟動畫面的視窗
fn show_fault(app: &AppHandle, rt: &Runtime) {
    if rt.fault.lock().unwrap().is_none() {
        return;
    }
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.destroy();
    }
    show(app);
}

fn url_encode(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}


fn random_key() -> String {
    let mut b = [0u8; 32];
    getrandom::getrandom(&mut b).expect("系統亂數不可用");
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

/// 想要的埠空著就用，被占了（例如這台另外裝了伺服器版）就讓系統給一個空的。
fn pick_port(want: u16) -> u16 {
    if want != 0 && TcpListener::bind(("127.0.0.1", want)).is_ok() && TcpListener::bind(("0.0.0.0", want)).is_ok() {
        return want;
    }
    TcpListener::bind(("127.0.0.1", 0)).and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(want)
}

fn python(payload: &Path) -> PathBuf {
    if cfg!(windows) {
        payload.join("python").join("python.exe")
    } else {
        payload.join("python").join("bin").join("python3")
    }
}

fn log_file(data: &Path, name: &str) -> Stdio {
    let dir = data.join(".xconsole");
    let _ = fs::create_dir_all(&dir);
    match OpenOptions::new().create(true).append(true).open(dir.join(name)) {
        Ok(f) => Stdio::from(f),
        Err(_) => Stdio::null(),
    }
}

fn command(rt: &Runtime, script: &[&str], log: &str) -> Command {
    let mut c = Command::new(python(&rt.payload));
    // -E -s：不吃這台的 PYTHON* 環境變數與使用者的套件，只用內含的那一份；
    // -E 會連 PYTHONUTF8 一起忽略，所以用 -X utf8 指定（不然記錄檔裡的中文在繁中 Windows 會變成 cp950 亂碼）
    c.arg("-E").arg("-s").arg("-X").arg("utf8");
    c.arg(rt.payload.join("app").join(script[0]));
    c.args(&script[1..]);
    c.current_dir(rt.payload.join("app"));
    c.env("PYTHONUTF8", "1");
    c.stdout(log_file(&rt.data, log)).stderr(log_file(&rt.data, log));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW：不要跳出黑色命令列視窗
    }
    c
}

fn spawn_agent(rt: &Runtime) -> std::io::Result<Child> {
    let pid = std::process::id().to_string();
    let home = rt.data.to_string_lossy().to_string();
    let mut c = command(rt, &["agent/xconsole_agent.py", "--home", &home, "--bind", "127.0.0.1", "--parent-pid", &pid], "agent.log");
    c.env("XCONSOLE_AGENT_PORT", rt.ports.agent.to_string());
    // Linux 也由代理自己帶起、看著模型服務（不走 systemd --user）：資料放在應用程式的資料夾，
    // systemd 找不到那裡的服務檔；而且應用程式結束時要停得掉模型
    c.env("XCONSOLE_SUPERVISOR", "1");
    c.env("XCONSOLE_DESKTOP", "1");
    c.stdin(Stdio::null());
    c.spawn()
}

fn spawn_console(rt: &Runtime) -> std::io::Result<Child> {
    let pid = std::process::id().to_string();
    let home = rt.data.to_string_lossy().to_string();
    let app = rt.ports.app.to_string();
    let mut c = command(rt, &["console/xconsole_server.py", "--home", &home, "--desktop", "--app-port", &app, "--parent-pid", &pid], "console.log");
    c.env("XCONSOLE_PORT", rt.ports.https.to_string());
    c.stdin(Stdio::piped());
    let mut child = c.spawn()?;
    if let Some(mut si) = child.stdin.take() {
        let _ = writeln!(si, "{}", rt.key); // 鑰匙走標準輸入，寫完就關
    }
    Ok(child)
}

/// 代理第一次啟動會產生權杖與憑證；等它們出現，再把這台的代理登記進主控台（每次都做：埠可能換過）。
fn register_agent(rt: &Runtime) -> bool {
    let st = rt.data.join(".xconsole");
    let end = Instant::now() + Duration::from_secs(60);
    while Instant::now() < end && !(st.join("token").exists() && st.join("agent-cert.pem").exists()) {
        thread::sleep(Duration::from_millis(300));
    }
    let home = rt.data.to_string_lossy().to_string();
    let port = rt.ports.agent.to_string();
    let name = hostname();
    let mut c = command(rt, &["install/setup_local.py", "--home", &home, "--agent-port", &port, "--name", &name], "console.log");
    c.stdin(Stdio::null());
    c.status().map(|s| s.success()).unwrap_or(false)
}

/// 這台有學校或公司統一裝的系統代理（本機使用者通道在聽）：登記成經通道連，不再帶起自己的代理。
fn register_managed(rt: &Runtime) -> bool {
    let home = rt.data.to_string_lossy().to_string();
    let name = hostname();
    let mut c = command(rt, &["install/setup_local.py", "--home", &home, "--managed", "--name", &name], "console.log");
    c.stdin(Stdio::null());
    c.status().map(|s| s.success()).unwrap_or(false)
}

fn hostname() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| Command::new("hostname").output().ok().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "這台電腦".into())
}

fn wait_port(port: u16, secs: u64) -> bool {
    let end = Instant::now() + Duration::from_secs(secs);
    while Instant::now() < end {
        if TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500)).is_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(300));
    }
    false
}

fn console_url(rt: &Runtime) -> url::Url {
    url::Url::parse(&format!("http://127.0.0.1:{}/#desktop={}", rt.ports.app, rt.key)).unwrap()
}

/// 背景執行緒：帶起代理與主控台、看著它們，掛了就重新帶起（主控台更新完或切換區網存取時會自己結束，也走這條）。
fn supervise(app: AppHandle, rt: Arc<Runtime>) {
    let managed = register_managed(&rt);
    let mut agent = if managed { Watch::idle() } else { Watch::start(spawn_agent(&rt)) };
    // 代理起來了才登記（起不來時登記也會失敗，還白等60秒才講原因）；第一次沒起來、之後重試起來了再補登記
    // 登記成功才算數：登記失敗（例如代理一起來就掉、還沒產生憑證）之後每30秒再試，直到登記上
    let mut registered = managed || (agent.child.is_some() && register_agent(&rt));
    let mut reg_at = Instant::now();
    let mut console = Watch::start(spawn_console(&rt));
    let mut first = true;
    loop {
        if rt.quitting.load(Ordering::SeqCst) {
            break;
        }
        if first && rt.fault.lock().unwrap().is_none() && wait_port(rt.ports.app, 5) {
            first = false;
            rt.ready.store(true, Ordering::SeqCst);
            if let Some(w) = app.get_webview_window(MAIN) {
                let _ = w.navigate(console_url(&rt));
            }
        }
        thread::sleep(Duration::from_secs(1));
        if rt.quitting.load(Ordering::SeqCst) {
            break;
        }
        if rt.fault.lock().unwrap().is_some() {
            continue; // 已經停手：等使用者處理完重開xConsole，不再重試
        }
        // 連續起不來（被Windows擋、少了檔案、一起來就掉）：停手並在啟動畫面講原因
        for (w, log) in [(&agent, "agent.log"), (&console, "console.log")] {
            if w.fails >= FAIL_LIMIT {
                let d = diagnose(&rt, w, log);
                // 也寫進launcher.log：使用者把記錄檔交給云碩時看得到，CI也靠這行驗證「停手了」
                if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(rt.data.join(".xconsole").join("launcher.log")) {
                    let _ = writeln!(f, "背景服務（{}）連續 {} 次起不來，停止重試：{}", log, w.fails, d.replace('\n', " / "));
                }
                *rt.fault.lock().unwrap() = Some(d);
                show_fault(&app, &rt);
            }
        }
        if rt.fault.lock().unwrap().is_some() {
            continue;
        }
        if !managed && agent.dead() && agent.fails < FAIL_LIMIT {
            thread::sleep(Duration::from_secs(2));
            agent.take(spawn_agent(&rt));
        }
        if !registered && agent.child.is_some() && reg_at.elapsed() >= Duration::from_secs(30) {
            registered = register_agent(&rt);
            reg_at = Instant::now();
        }
        if console.dead() && console.fails < FAIL_LIMIT {
            thread::sleep(Duration::from_secs(1));
            console.take(spawn_console(&rt));
        }
    }
    // 結束時直接收掉主控台；代理留給它自己看到父行程不在、先停模型服務再結束
    if let Some(mut c) = console.child {
        let _ = c.kill();
    }
}

fn show(app: &AppHandle) {
    let rt = app.state::<Arc<Runtime>>();
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
        return;
    }
    let fault = rt.fault.lock().unwrap().clone();
    let url = if let Some(f) = fault {
        WebviewUrl::App(format!("index.html#fault={}", url_encode(&f)).into())
    } else if rt.ready.load(Ordering::SeqCst) {
        WebviewUrl::External(console_url(&rt))
    } else {
        WebviewUrl::App("index.html".into())
    };
    let app_port = rt.ports.app;
    let opener = app.clone();
    let _ = WebviewWindowBuilder::new(app, MAIN, url)
        .title("xConsole")
        .inner_size(1360.0, 880.0)
        .min_inner_size(960.0, 640.0)
        .center()
        .on_navigation(move |u| {
            // 只在視窗裡開主控台自己的頁面；其他網址（模型的 Hugging Face 頁、說明連結）交給系統的瀏覽器
            let mine = (u.scheme() == "http" && u.host_str() == Some("127.0.0.1") && u.port() == Some(app_port))
                || u.scheme() == "tauri"
                || u.host_str() == Some("tauri.localhost");
            if !mine && (u.scheme() == "https" || u.scheme() == "http") {
                let _ = tauri_plugin_opener::OpenerExt::opener(&opener).open_url(u.as_str(), None::<&str>);
            }
            mine
        })
        .build();
}

/// 第一次關視窗時跳一則系統通知：程式還在背景、模型照常服務、怎麼完全結束（只說一次，記在資料夾裡）。
fn tray_hint_once(app: &AppHandle) {
    use tauri_plugin_notification::NotificationExt;
    let rt = app.state::<Arc<Runtime>>();
    let flag = rt.data.join(".xconsole").join("tray-hint-shown");
    if flag.exists() {
        return;
    }
    let _ = app
        .notification()
        .builder()
        .title("xConsole 還在背景執行")
        .body("模型照常服務。要完全結束，請在工作列右下角的 xConsole 圖示按右鍵，選「結束 xConsole」。")
        .show();
    let _ = fs::write(flag, b"1");
}

fn main() {
    let hidden = std::env::args().any(|a| a == "--hidden"); // 開機自動啟動：只放系統匣，不開視窗
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show(app)))
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec!["--hidden"])))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            let data = app.path().app_local_data_dir()?;
            fs::create_dir_all(&data)?;
            let payload = app.path().resource_dir()?.join("payload");
            let rt = Arc::new(Runtime {
                key: random_key(),
                ports: Ports { https: pick_port(9400), agent: pick_port(9401), app: pick_port(0) },
                data,
                payload,
                ready: AtomicBool::new(false),
                quitting: AtomicBool::new(false),
                fault: Mutex::new(None),
            });
            app.manage(rt.clone());

            let open = MenuItem::with_id(app, "open", "打開 xConsole", true, None::<&str>)?;
            let auto = CheckMenuItem::with_id(app, "autostart", "開機自動啟動", true, app.autolaunch().is_enabled().unwrap_or(false), None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "結束 xConsole（停止所有模型）", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&open, &auto, &PredefinedMenuItem::separator(app)?, &quit])?;
            let auto_item = auto.clone();
            TrayIconBuilder::with_id("tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("xConsole 模型主控台")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, ev| match ev.id().as_ref() {
                    "open" => show(app),
                    "autostart" => {
                        let al = app.autolaunch();
                        let on = al.is_enabled().unwrap_or(false);
                        let _ = if on { al.disable() } else { al.enable() };
                        let _ = auto_item.set_checked(al.is_enabled().unwrap_or(!on));
                    }
                    "quit" => {
                        app.state::<Arc<Runtime>>().quitting.store(true, Ordering::SeqCst);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, ev| {
                    if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                        show(tray.app_handle());
                    }
                })
                .build(app)?;

            if !hidden {
                show(app.handle());
            }
            let handle = app.handle().clone();
            thread::spawn(move || supervise(handle, rt));
            Ok(())
        })
        .on_window_event(|w, ev| {
            if let WindowEvent::CloseRequested { api, .. } = ev {
                // 關視窗只是縮到系統匣：模型繼續服務，別的程式照樣呼叫得到
                api.prevent_close();
                let _ = w.hide();
                tray_hint_once(w.app_handle());
            }
        })
        .build(tauri::generate_context!())
        .expect("xConsole 啟動失敗")
        .run(|app, ev| {
            if let tauri::RunEvent::ExitRequested { api, code, .. } = ev {
                // 沒有視窗時 Tauri 會想結束：除非是從系統匣按「結束」，否則留在系統匣
                if code.is_none() && !app.state::<Arc<Runtime>>().quitting.load(Ordering::SeqCst) {
                    api.prevent_exit();
                }
            }
        });
}
