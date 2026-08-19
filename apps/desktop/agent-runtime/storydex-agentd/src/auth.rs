use crate::workspace::atomic_write;
use crate::{ApiEnvelope, AppState, error_response_with_details};
use axum::Json;
use axum::extract::{Path as AxumPath, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use base64::Engine;
use chacha20poly1305::aead::{Aead, AeadCore, KeyInit, OsRng, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use reqwest::{Client, Method, Url};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
#[cfg(windows)]
use sha2::{Digest, Sha256};
use std::fs;
#[cfg(any(test, not(windows)))]
use std::fs::OpenOptions;
#[cfg(any(test, not(windows)))]
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const DEFAULT_ACCOUNT_BASE_URL: &str = "https://storykeeper.septemc.cn";
const SESSION_AAD: &[u8] = b"storydex-rust-candidate-auth-session-v1";
const MAX_REMOTE_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const KEY_BYTES: usize = 32;

#[derive(Clone, Copy)]
pub(crate) enum AuthKeyMode {
    Platform,
    #[cfg(test)]
    Local,
}

#[derive(Clone)]
pub(crate) struct AuthService {
    client: Client,
    base_url: Url,
    sessions: SessionStore,
}

#[derive(Clone)]
struct SessionStore {
    root: Arc<PathBuf>,
    key_mode: AuthKeyMode,
    lock: Arc<Mutex<()>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SessionRecord {
    version: u32,
    access_token: String,
    user_id: String,
    username: String,
    server_base_url: String,
    user: Value,
    saved_at: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct EncryptedSession {
    version: u32,
    scheme: String,
    nonce: String,
    ciphertext: String,
}

#[derive(Debug)]
struct AuthFailure {
    status: StatusCode,
    code: String,
    message: String,
    details: Option<Value>,
}

impl AuthFailure {
    fn new(status: StatusCode, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            status,
            code: code.into(),
            message: message.into(),
            details: None,
        }
    }

    fn with_details(mut self, details: Value) -> Self {
        self.details = Some(details);
        self
    }

    fn into_response(self) -> Response {
        error_response_with_details(self.status, &self.code, &self.message, self.details)
    }
}

#[derive(Deserialize)]
pub(crate) struct RegisterRequest {
    username: String,
    password: String,
    email: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct LoginRequest {
    username: String,
    password: String,
}

impl AuthService {
    pub(crate) fn new(
        coomi_home: &Path,
        key_mode: AuthKeyMode,
        account_base_url: Option<&str>,
    ) -> anyhow::Result<Self> {
        let root = coomi_home
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .unwrap_or(coomi_home)
            .to_path_buf();
        let configured = account_base_url
            .map(str::to_owned)
            .or_else(|| non_empty_env("STORYDEX_ACCOUNT_BASE_URL"))
            .or_else(|| non_empty_env("STORYKEEPER_BASE_URL"))
            .unwrap_or_else(|| DEFAULT_ACCOUNT_BASE_URL.to_owned());
        let base_url = validate_base_url(&configured)?;
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(20))
            .user_agent(format!(
                "StorydexRustCandidate/{}",
                env!("CARGO_PKG_VERSION")
            ))
            .build()?;
        Ok(Self {
            client,
            base_url,
            sessions: SessionStore {
                root: Arc::new(root),
                key_mode,
                lock: Arc::new(Mutex::new(())),
            },
        })
    }

    fn base_url_text(&self) -> String {
        self.base_url.as_str().trim_end_matches('/').to_owned()
    }

    fn endpoint(&self, path: &str) -> Result<Url, AuthFailure> {
        let mut url = self.base_url.clone();
        let base_path = url.path().trim_end_matches('/');
        let suffix = path.trim_start_matches('/');
        let next_path = if base_path.is_empty() || base_path == "/" {
            format!("/{suffix}")
        } else {
            format!("{base_path}/{suffix}")
        };
        url.set_path(&next_path);
        url.set_query(None);
        url.set_fragment(None);
        Ok(url)
    }

    fn username_endpoint(&self, username: &str) -> Result<Url, AuthFailure> {
        let mut url = self.endpoint("/api/auth/check-username")?;
        url.path_segments_mut()
            .map_err(|_| {
                AuthFailure::new(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "account_base_url_invalid",
                    "The account service base URL cannot accept path segments.",
                )
            })?
            .push(username);
        Ok(url)
    }

    async fn request(
        &self,
        method: Method,
        url: Url,
        payload: Option<Value>,
        token: Option<&str>,
    ) -> Result<Value, AuthFailure> {
        let mut request = self
            .client
            .request(method, url)
            .header(header::ACCEPT, "application/json");
        if let Some(payload) = payload {
            request = request.json(&payload);
        }
        if let Some(token) = token.filter(|value| !value.trim().is_empty()) {
            request = request.bearer_auth(token.trim());
        }

        let response = request.send().await.map_err(|error| {
            AuthFailure::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "account_service_unreachable",
                "Failed to connect to the Storydex account service.",
            )
            .with_details(json!({
                "exceptionType": "reqwest",
                "reason": error.to_string(),
            }))
        })?;
        let status = response.status();
        let bytes = response.bytes().await.map_err(|error| {
            AuthFailure::new(
                StatusCode::BAD_GATEWAY,
                "account_service_response_read_failed",
                "The Storydex account service response could not be read.",
            )
            .with_details(json!({"reason": error.to_string()}))
        })?;
        if bytes.len() > MAX_REMOTE_RESPONSE_BYTES {
            return Err(AuthFailure::new(
                StatusCode::BAD_GATEWAY,
                "account_service_response_too_large",
                "The Storydex account service response exceeded the candidate limit.",
            ));
        }

        let decoded = if bytes.is_empty() {
            json!({})
        } else {
            serde_json::from_slice::<Value>(&bytes).map_err(|_| {
                if status.is_success() {
                    AuthFailure::new(
                        StatusCode::BAD_GATEWAY,
                        "account_service_invalid_json",
                        "The Storydex account service returned invalid JSON.",
                    )
                } else {
                    let message = String::from_utf8_lossy(&bytes)
                        .trim()
                        .chars()
                        .take(240)
                        .collect::<String>();
                    AuthFailure::new(
                        status,
                        "account_service_request_failed",
                        if message.is_empty() {
                            "The Storydex account service request failed.".to_owned()
                        } else {
                            message
                        },
                    )
                    .with_details(json!({"remoteStatus": status.as_u16()}))
                }
            })?
        };

        if !status.is_success() {
            return Err(remote_failure(status, &decoded));
        }
        if !decoded.is_object() {
            return Err(AuthFailure::new(
                StatusCode::BAD_GATEWAY,
                "account_service_invalid_payload",
                "The Storydex account service returned an invalid payload.",
            ));
        }
        Ok(decoded)
    }

    async fn request_path(
        &self,
        method: Method,
        path: &str,
        payload: Option<Value>,
        token: Option<&str>,
    ) -> Result<Value, AuthFailure> {
        self.request(method, self.endpoint(path)?, payload, token)
            .await
    }

    fn persist_session(&self, access_token: &str, user: &Value) -> Result<(), AuthFailure> {
        let user_id = required_string(user, &["userId"], "account_user_invalid")?;
        let username = required_string(user, &["username"], "account_user_invalid")?;
        self.sessions.write(&SessionRecord {
            version: 1,
            access_token: access_token.to_owned(),
            user_id,
            username,
            server_base_url: self.base_url_text(),
            user: user.clone(),
            saved_at: chrono::Utc::now().to_rfc3339(),
        })
    }

    fn refresh_matching_session(
        &self,
        access_token: &str,
        user: &Value,
    ) -> Result<(), AuthFailure> {
        if self
            .sessions
            .read()?
            .is_some_and(|session| session.access_token == access_token)
        {
            self.persist_session(access_token, user)?;
        }
        Ok(())
    }
}

impl SessionStore {
    fn session_path(&self) -> PathBuf {
        self.root.join("auth").join("rust-candidate-session.json")
    }

    #[cfg(any(test, not(windows)))]
    fn local_key_path(&self) -> PathBuf {
        self.root
            .join("auth")
            .join("rust-candidate-local-secret.bin")
    }

    fn read(&self) -> Result<Option<SessionRecord>, AuthFailure> {
        let _guard = self.lock.lock().map_err(|_| session_lock_error())?;
        let path = self.session_path();
        if !path.exists() {
            return Ok(None);
        }
        let raw = fs::read(&path).map_err(|_| session_storage_error("read_failed"))?;
        let encrypted: EncryptedSession =
            serde_json::from_slice(&raw).map_err(|_| session_storage_error("record_invalid"))?;
        if encrypted.version != 1 || encrypted.scheme != "chacha20poly1305-v1" {
            return Err(session_storage_error("scheme_unsupported"));
        }
        let nonce = decode_base64(&encrypted.nonce, "nonce_invalid")?;
        let ciphertext = decode_base64(&encrypted.ciphertext, "ciphertext_invalid")?;
        if nonce.len() != 12 {
            return Err(session_storage_error("nonce_invalid"));
        }
        let key = self.load_key()?;
        let cipher = ChaCha20Poly1305::new(Key::from_slice(&key));
        let plaintext = cipher
            .decrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: &ciphertext,
                    aad: SESSION_AAD,
                },
            )
            .map_err(|_| session_storage_error("authentication_failed"))?;
        let session: SessionRecord = serde_json::from_slice(&plaintext)
            .map_err(|_| session_storage_error("payload_invalid"))?;
        if session.version != 1
            || session.access_token.trim().is_empty()
            || session.user_id.trim().is_empty()
            || session.username.trim().is_empty()
            || session.server_base_url.trim().is_empty()
            || !session.user.is_object()
        {
            return Err(session_storage_error("payload_invalid"));
        }
        Ok(Some(session))
    }

    fn write(&self, session: &SessionRecord) -> Result<(), AuthFailure> {
        let _guard = self.lock.lock().map_err(|_| session_lock_error())?;
        let plaintext = serde_json::to_vec(session)
            .map_err(|_| session_storage_error("payload_encode_failed"))?;
        let key = self.load_key()?;
        let cipher = ChaCha20Poly1305::new(Key::from_slice(&key));
        let nonce = ChaCha20Poly1305::generate_nonce(&mut OsRng);
        let ciphertext = cipher
            .encrypt(
                &nonce,
                Payload {
                    msg: &plaintext,
                    aad: SESSION_AAD,
                },
            )
            .map_err(|_| session_storage_error("encryption_failed"))?;
        let payload = EncryptedSession {
            version: 1,
            scheme: "chacha20poly1305-v1".to_owned(),
            nonce: base64::engine::general_purpose::STANDARD.encode(nonce),
            ciphertext: base64::engine::general_purpose::STANDARD.encode(ciphertext),
        };
        let bytes = serde_json::to_vec_pretty(&payload)
            .map_err(|_| session_storage_error("record_encode_failed"))?;
        let path = self.session_path();
        atomic_write(&path, &bytes).map_err(|_| session_storage_error("write_failed"))?;
        restrict_file_permissions(&path).map_err(|_| session_storage_error("permission_failed"))?;
        Ok(())
    }

    fn clear(&self) -> Result<(), AuthFailure> {
        let _guard = self.lock.lock().map_err(|_| session_lock_error())?;
        let path = self.session_path();
        match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(session_storage_error("clear_failed")),
        }
    }

    fn load_key(&self) -> Result<[u8; KEY_BYTES], AuthFailure> {
        match self.key_mode {
            AuthKeyMode::Platform => self.load_platform_key(),
            #[cfg(test)]
            AuthKeyMode::Local => self.load_local_key(),
        }
    }

    #[cfg(any(test, not(windows)))]
    fn load_local_key(&self) -> Result<[u8; KEY_BYTES], AuthFailure> {
        let path = self.local_key_path();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|_| session_storage_error("key_root_failed"))?;
        }
        match OpenOptions::new().read(true).open(&path) {
            Ok(mut file) => {
                let mut bytes = Vec::new();
                file.read_to_end(&mut bytes)
                    .map_err(|_| session_storage_error("key_read_failed"))?;
                return exact_key(&bytes);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(session_storage_error("key_read_failed")),
        }

        let generated = ChaCha20Poly1305::generate_key(&mut OsRng);
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        match options.open(&path) {
            Ok(mut created) => {
                created
                    .write_all(generated.as_slice())
                    .map_err(|_| session_storage_error("key_write_failed"))?;
                created
                    .sync_all()
                    .map_err(|_| session_storage_error("key_sync_failed"))?;
                drop(created);
                restrict_file_permissions(&path)
                    .map_err(|_| session_storage_error("key_permission_failed"))?;
                exact_key(generated.as_slice())
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let bytes =
                    fs::read(&path).map_err(|_| session_storage_error("key_read_failed"))?;
                exact_key(&bytes)
            }
            Err(_) => Err(session_storage_error("key_create_failed")),
        }
    }

    #[cfg(windows)]
    fn load_platform_key(&self) -> Result<[u8; KEY_BYTES], AuthFailure> {
        let root_hash = hex_digest(self.root.to_string_lossy().as_bytes());
        let account = format!("auth-key-{}", &root_hash[..32]);
        let entry = keyring::Entry::new("Storydex Rust Candidate", &account)
            .map_err(|_| session_storage_error("credential_entry_failed"))?;
        match entry.get_password() {
            Ok(encoded) => {
                let decoded = decode_base64(&encoded, "credential_value_invalid")?;
                exact_key(&decoded)
            }
            Err(keyring::Error::NoEntry) => {
                let generated = ChaCha20Poly1305::generate_key(&mut OsRng);
                let encoded = base64::engine::general_purpose::STANDARD.encode(generated);
                entry
                    .set_password(&encoded)
                    .map_err(|_| session_storage_error("credential_write_failed"))?;
                exact_key(generated.as_slice())
            }
            Err(_) => Err(session_storage_error("credential_read_failed")),
        }
    }

    #[cfg(not(windows))]
    fn load_platform_key(&self) -> Result<[u8; KEY_BYTES], AuthFailure> {
        self.load_local_key()
    }
}

pub(crate) async fn register(
    State(state): State<AppState>,
    Json(request): Json<RegisterRequest>,
) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = service
        .request_path(
            Method::POST,
            "/api/auth/register",
            Some(json!({
                "username": request.username.trim(),
                "password": request.password,
                "email": request.email.and_then(non_empty_text),
            })),
            None,
        )
        .await
        .and_then(|remote| {
            let user = normalize_user(field(&remote, &["user"]).unwrap_or(&Value::Null))?;
            Ok(json!({
                "success": bool_field(&remote, &["success"]).unwrap_or(true),
                "message": string_field(&remote, &["message"])
                    .unwrap_or_else(|| "Registered successfully.".to_owned()),
                "user": user,
            }))
        });
    auth_result(result, started, json!({"action": "register_account"}))
}

pub(crate) async fn login(
    State(state): State<AppState>,
    Json(request): Json<LoginRequest>,
) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let remote = service
            .request_path(
                Method::POST,
                "/api/auth/login",
                Some(json!({
                    "username": request.username.trim(),
                    "password": request.password,
                })),
                None,
            )
            .await?;
        let access_token = required_string(
            &remote,
            &["accessToken", "access_token"],
            "account_login_invalid",
        )?;
        let user = match field(&remote, &["user"]) {
            Some(value) if value.is_object() => normalize_user(value)?,
            _ => normalize_user(
                &service
                    .request_path(Method::GET, "/api/auth/me", None, Some(&access_token))
                    .await?,
            )?,
        };
        service.persist_session(&access_token, &user)?;
        Ok(json!({
            "accessToken": access_token,
            "userId": required_string(&user, &["userId"], "account_login_invalid")?,
            "username": required_string(&user, &["username"], "account_login_invalid")?,
            "role": string_field(&user, &["role"]).unwrap_or_else(|| "USER".to_owned()),
            "user": user,
        }))
    }
    .await;
    auth_result(result, started, json!({"action": "login_account"}))
}

pub(crate) async fn session(State(state): State<AppState>) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let Some(stored) = service.sessions.read()? else {
            return Ok(json!({"authenticated": false, "accessToken": "", "user": null}));
        };
        if normalize_base_text(&stored.server_base_url)
            != normalize_base_text(&service.base_url_text())
        {
            service.sessions.clear()?;
            return Ok(json!({"authenticated": false, "accessToken": "", "user": null}));
        }
        match service
            .request_path(
                Method::GET,
                "/api/auth/me",
                None,
                Some(&stored.access_token),
            )
            .await
        {
            Ok(remote) => {
                let user = normalize_user(&remote)?;
                service.persist_session(&stored.access_token, &user)?;
                Ok(json!({
                    "authenticated": true,
                    "accessToken": stored.access_token,
                    "user": user,
                }))
            }
            Err(error) if error.status == StatusCode::UNAUTHORIZED => {
                service.sessions.clear()?;
                Ok(json!({"authenticated": false, "accessToken": "", "user": null}))
            }
            Err(error) => Err(error),
        }
    }
    .await;
    auth_result(result, started, json!({"action": "read_persisted_session"}))
}

pub(crate) async fn current_account(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let token = bearer_token(&headers)?;
        let user = normalize_user(
            &service
                .request_path(Method::GET, "/api/auth/me", None, Some(&token))
                .await?,
        )?;
        service.refresh_matching_session(&token, &user)?;
        Ok(user)
    }
    .await;
    auth_result(result, started, json!({"action": "read_current_account"}))
}

pub(crate) async fn update_profile(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let token = bearer_token(&headers)?;
        let object = payload.as_object().ok_or_else(|| {
            AuthFailure::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "profile_payload_invalid",
                "Profile update payload must be an object.",
            )
        })?;
        let mut outgoing = Map::new();
        for key in ["nickname", "email", "avatar"] {
            if let Some(value) = object.get(key) {
                outgoing.insert(key.to_owned(), value.clone());
            }
        }
        let user = normalize_user(
            &service
                .request_path(
                    Method::PUT,
                    "/api/auth/profile",
                    Some(Value::Object(outgoing)),
                    Some(&token),
                )
                .await?,
        )?;
        service.refresh_matching_session(&token, &user)?;
        Ok(user)
    }
    .await;
    auth_result(result, started, json!({"action": "update_current_profile"}))
}

pub(crate) async fn update_password(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let token = bearer_token(&headers)?;
        let current_password = required_string(
            &payload,
            &["currentPassword", "oldPassword"],
            "current_password_required",
        )?;
        let new_password = required_string(&payload, &["newPassword"], "new_password_required")?;
        let remote = service
            .request_path(
                Method::PUT,
                "/api/auth/password",
                Some(json!({
                    "current_password": current_password,
                    "new_password": new_password,
                })),
                Some(&token),
            )
            .await?;
        Ok(json!({
            "success": bool_field(&remote, &["success"]).unwrap_or(true),
            "message": string_field(&remote, &["message"])
                .unwrap_or_else(|| "Password updated.".to_owned()),
        }))
    }
    .await;
    auth_result(result, started, json!({"action": "update_password"}))
}

pub(crate) async fn logout(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let token = bearer_token(&headers)?;
        let remote = service
            .request_path(Method::POST, "/api/auth/logout", None, Some(&token))
            .await?;
        if service
            .sessions
            .read()?
            .is_some_and(|session| session.access_token == token)
        {
            service.sessions.clear()?;
        }
        Ok(json!({
            "success": bool_field(&remote, &["success"]).unwrap_or(true),
            "message": string_field(&remote, &["message"])
                .unwrap_or_else(|| "Logged out.".to_owned()),
        }))
    }
    .await;
    auth_result(result, started, json!({"action": "logout_account"}))
}

pub(crate) async fn check_username(
    State(state): State<AppState>,
    AxumPath(username): AxumPath<String>,
) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let remote = service
            .request(
                Method::GET,
                service.username_endpoint(&username)?,
                None,
                None,
            )
            .await?;
        Ok(json!({
            "available": bool_field(&remote, &["available"]).unwrap_or(false),
        }))
    }
    .await;
    auth_result(result, started, json!({"action": "check_username"}))
}

pub(crate) async fn account_summary(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let started = Instant::now();
    let service = state.auth_service();
    let result = async {
        let token = bearer_token(&headers)?;
        let remote = service
            .request_path(Method::GET, "/api/auth/account-summary", None, Some(&token))
            .await?;
        let user = normalize_user(field(&remote, &["user"]).unwrap_or(&Value::Null))?;
        service.refresh_matching_session(&token, &user)?;
        Ok(json!({
            "user": user,
            "quota": normalize_quota(field(&remote, &["quota"])),
            "profile": normalize_profile(field(&remote, &["profile"])),
            "assets": normalize_assets(field(&remote, &["assets"])),
        }))
    }
    .await;
    auth_result(result, started, json!({"action": "read_account_summary"}))
}

fn auth_result(result: Result<Value, AuthFailure>, started: Instant, audit: Value) -> Response {
    match result {
        Ok(data) => {
            Json(ApiEnvelope::success(data, started).with_audit(vec![audit])).into_response()
        }
        Err(error) => error.into_response(),
    }
}

fn bearer_token(headers: &HeaderMap) -> Result<String, AuthFailure> {
    let raw = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .trim();
    let mut parts = raw.splitn(2, char::is_whitespace);
    let scheme = parts.next().unwrap_or_default();
    let token = parts.next().unwrap_or_default().trim();
    if !scheme.eq_ignore_ascii_case("bearer") || token.is_empty() {
        return Err(AuthFailure::new(
            StatusCode::UNAUTHORIZED,
            "auth_token_missing",
            "Authentication token is required.",
        ));
    }
    Ok(token.to_owned())
}

fn normalize_user(value: &Value) -> Result<Value, AuthFailure> {
    let user_id = required_string(value, &["userId", "user_id"], "account_user_invalid")?;
    let username = required_string(value, &["username"], "account_user_invalid")?;
    let nickname = string_field(value, &["nickname"])
        .map(Value::String)
        .unwrap_or_else(|| Value::String(username.clone()));
    Ok(json!({
        "userId": user_id,
        "username": username,
        "email": nullable_string_field(value, &["email"]),
        "nickname": nickname,
        "avatar": nullable_string_field(value, &["avatar"]),
        "role": string_field(value, &["role"]).unwrap_or_else(|| "USER".to_owned()),
        "isActive": bool_field(value, &["isActive", "is_active"]).unwrap_or(true),
        "createdAt": string_field(value, &["createdAt", "created_at"]).unwrap_or_default(),
        "updatedAt": nullable_string_field(value, &["updatedAt", "updated_at"]),
        "lastLoginAt": nullable_string_field(value, &["lastLoginAt", "last_login_at"]),
    }))
}

fn normalize_quota(value: Option<&Value>) -> Value {
    let value = value.unwrap_or(&Value::Null);
    json!({
        "balance": integer_field(value, &["balance"]).unwrap_or(0),
        "totalGranted": integer_field(value, &["totalGranted", "total_granted"]).unwrap_or(0),
        "totalConsumed": integer_field(value, &["totalConsumed", "total_consumed"]).unwrap_or(0),
        "isUnlimited": bool_field(value, &["isUnlimited", "is_unlimited"]).unwrap_or(false),
        "lastGrantedAt": nullable_string_field(value, &["lastGrantedAt", "last_granted_at"]),
        "lastConsumedAt": nullable_string_field(value, &["lastConsumedAt", "last_consumed_at"]),
    })
}

fn normalize_profile(value: Option<&Value>) -> Value {
    let value = value.unwrap_or(&Value::Null);
    json!({
        "defaultSessionId": nullable_string_field(value, &["defaultSessionId", "default_session_id"]),
        "defaultWorldbookId": nullable_string_field(value, &["defaultWorldbookId", "default_worldbook_id"]),
        "defaultScriptId": nullable_string_field(value, &["defaultScriptId", "default_script_id"]),
        "allowPersonalApiKey": bool_field(value, &["allowPersonalApiKey", "allow_personal_api_key"]).unwrap_or(true),
        "allowSystemQuota": bool_field(value, &["allowSystemQuota", "allow_system_quota"]).unwrap_or(true),
        "quotaCostPerGeneration": integer_field(value, &["quotaCostPerGeneration", "quota_cost_per_generation"]).unwrap_or(1),
    })
}

fn normalize_assets(value: Option<&Value>) -> Value {
    let value = value.unwrap_or(&Value::Null);
    json!({
        "stories": integer_field(value, &["stories"]).unwrap_or(0),
        "characters": integer_field(value, &["characters"]).unwrap_or(0),
        "worldbook": integer_field(value, &["worldbook"]).unwrap_or(0),
        "words": integer_field(value, &["words"]).unwrap_or(0),
    })
}

fn remote_failure(status: StatusCode, payload: &Value) -> AuthFailure {
    let message = string_field(payload, &["message", "detail"])
        .or_else(|| {
            field(payload, &["error"]).and_then(|error| string_field(error, &["message", "detail"]))
        })
        .unwrap_or_else(|| "The Storydex account service request failed.".to_owned());
    let code = string_field(payload, &["code"])
        .or_else(|| field(payload, &["error"]).and_then(|error| string_field(error, &["code"])))
        .unwrap_or_else(|| "account_service_request_failed".to_owned());
    let mut details = field(payload, &["details"])
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    if let Some(object) = details.as_object_mut() {
        object.insert("remoteStatus".to_owned(), json!(status.as_u16()));
    }
    AuthFailure::new(status, code, message).with_details(details)
}

fn field<'a>(value: &'a Value, names: &[&str]) -> Option<&'a Value> {
    let object = value.as_object()?;
    names.iter().find_map(|name| object.get(*name))
}

fn string_field(value: &Value, names: &[&str]) -> Option<String> {
    field(value, names)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
}

fn nullable_string_field(value: &Value, names: &[&str]) -> Option<Value> {
    let value = field(value, names)?;
    if value.is_null() {
        return Some(Value::Null);
    }
    Some(
        value
            .as_str()
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .map(|text| Value::String(text.to_owned()))
            .unwrap_or(Value::Null),
    )
}

fn bool_field(value: &Value, names: &[&str]) -> Option<bool> {
    field(value, names).and_then(Value::as_bool)
}

fn integer_field(value: &Value, names: &[&str]) -> Option<i64> {
    field(value, names).and_then(Value::as_i64)
}

fn required_string(
    value: &Value,
    names: &[&str],
    code: &'static str,
) -> Result<String, AuthFailure> {
    string_field(value, names).ok_or_else(|| {
        AuthFailure::new(
            StatusCode::BAD_GATEWAY,
            code,
            "The Storydex account service response is missing a required field.",
        )
    })
}

fn non_empty_env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn non_empty_text(value: String) -> Option<String> {
    let value = value.trim().to_owned();
    (!value.is_empty()).then_some(value)
}

fn validate_base_url(value: &str) -> anyhow::Result<Url> {
    let mut url = Url::parse(value.trim())?;
    anyhow::ensure!(
        matches!(url.scheme(), "http" | "https"),
        "account base URL must use http or https"
    );
    anyhow::ensure!(
        url.host_str().is_some(),
        "account base URL must have a host"
    );
    anyhow::ensure!(
        url.username().is_empty() && url.password().is_none(),
        "account base URL must not contain credentials"
    );
    anyhow::ensure!(
        url.query().is_none() && url.fragment().is_none(),
        "account base URL must not contain query or fragment"
    );
    let normalized_path = url.path().trim_end_matches('/').to_owned();
    url.set_path(if normalized_path.is_empty() {
        "/"
    } else {
        &normalized_path
    });
    Ok(url)
}

fn normalize_base_text(value: &str) -> String {
    value.trim().trim_end_matches('/').to_ascii_lowercase()
}

fn session_storage_error(reason: &'static str) -> AuthFailure {
    AuthFailure::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "auth_session_storage_failed",
        "The encrypted Storydex account session could not be accessed.",
    )
    .with_details(json!({"reason": reason}))
}

fn session_lock_error() -> AuthFailure {
    session_storage_error("lock_poisoned")
}

fn decode_base64(value: &str, reason: &'static str) -> Result<Vec<u8>, AuthFailure> {
    base64::engine::general_purpose::STANDARD
        .decode(value.trim())
        .map_err(|_| session_storage_error(reason))
}

fn exact_key(bytes: &[u8]) -> Result<[u8; KEY_BYTES], AuthFailure> {
    bytes
        .try_into()
        .map_err(|_| session_storage_error("key_length_invalid"))
}

#[cfg(unix)]
fn restrict_file_permissions(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn restrict_file_permissions(_path: &Path) -> std::io::Result<()> {
    Ok(())
}

#[cfg(windows)]
fn hex_digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::router;
    use axum::Router;
    use axum::body::{Body, to_bytes};
    use axum::http::Request;
    use axum::routing::{get, post, put};
    use tempfile::tempdir;
    use tower::ServiceExt;

    fn test_store(root: &Path) -> SessionStore {
        SessionStore {
            root: Arc::new(root.to_path_buf()),
            key_mode: AuthKeyMode::Local,
            lock: Arc::new(Mutex::new(())),
        }
    }

    #[test]
    fn encrypted_session_never_persists_the_raw_token() {
        let root = tempdir().expect("root");
        let store = test_store(root.path());
        let token = "account-token-that-must-not-appear-on-disk";
        store
            .write(&SessionRecord {
                version: 1,
                access_token: token.to_owned(),
                user_id: "user-1".to_owned(),
                username: "writer".to_owned(),
                server_base_url: "http://127.0.0.1:1234".to_owned(),
                user: json!({"userId": "user-1", "username": "writer"}),
                saved_at: chrono::Utc::now().to_rfc3339(),
            })
            .expect("write encrypted session");

        let raw = fs::read(store.session_path()).expect("session file");
        assert!(!String::from_utf8_lossy(&raw).contains(token));
        assert_eq!(
            store
                .read()
                .expect("read encrypted session")
                .expect("session")
                .access_token,
            token
        );
    }

    #[test]
    fn corrupted_session_fails_closed_instead_of_becoming_anonymous() {
        let root = tempdir().expect("root");
        let store = test_store(root.path());
        let path = store.session_path();
        fs::create_dir_all(path.parent().expect("parent")).expect("auth root");
        fs::write(
            path,
            br#"{"version":1,"scheme":"chacha20poly1305-v1","nonce":"bad","ciphertext":"bad"}"#,
        )
        .expect("corrupt session");
        let error = store.read().expect_err("corrupt session must fail");
        assert_eq!(error.code, "auth_session_storage_failed");
    }

    #[test]
    fn remote_user_normalization_accepts_snake_case_without_losing_contract_fields() {
        let user = normalize_user(&json!({
            "user_id": "user-1",
            "username": "writer",
            "is_active": true,
            "created_at": "2026-08-19T00:00:00Z",
        }))
        .expect("normalize user");
        assert_eq!(user["userId"], "user-1");
        assert_eq!(user["nickname"], "writer");
        assert_eq!(user["isActive"], true);
        assert_eq!(user["role"], "USER");
    }

    fn mock_account_user(username: &str) -> Value {
        json!({
            "user_id": "user-1",
            "username": username,
            "email": null,
            "nickname": username,
            "avatar": null,
            "role": "USER",
            "is_active": true,
            "created_at": "2026-08-19T00:00:00Z",
            "updated_at": null,
            "last_login_at": null,
        })
    }

    fn candidate_request(
        method: &str,
        uri: &str,
        payload: Option<Value>,
        account_token: Option<&str>,
    ) -> Request<Body> {
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header("x-storydex-runtime-token", "test-token");
        if payload.is_some() {
            builder = builder.header(header::CONTENT_TYPE, "application/json");
        }
        if let Some(token) = account_token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        builder
            .body(
                payload
                    .map(|value| Body::from(serde_json::to_vec(&value).expect("serialize request")))
                    .unwrap_or_else(Body::empty),
            )
            .expect("candidate request")
    }

    async fn response_json(response: Response) -> Value {
        let bytes = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        serde_json::from_slice(&bytes).expect("response JSON")
    }

    async fn register_mock(Json(payload): Json<Value>) -> Response {
        if payload["username"] == "taken" {
            return (
                StatusCode::CONFLICT,
                Json(json!({
                    "message": "Username already exists.",
                    "code": "username_already_exists",
                })),
            )
                .into_response();
        }
        Json(json!({
            "success": true,
            "message": "Registered successfully.",
            "user": mock_account_user("writer"),
        }))
        .into_response()
    }

    async fn login_mock(Json(payload): Json<Value>) -> Json<Value> {
        Json(json!({
            "access_token": "account-secret-token",
            "user": mock_account_user(payload["username"].as_str().unwrap_or("writer")),
        }))
    }

    fn authorized_mock(headers: &HeaderMap) -> bool {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            == Some("Bearer account-secret-token")
    }

    async fn me_mock(headers: HeaderMap) -> Response {
        if !authorized_mock(&headers) {
            return (
                StatusCode::UNAUTHORIZED,
                Json(json!({
                    "message": "Invalid account token.",
                    "code": "auth_token_invalid",
                })),
            )
                .into_response();
        }
        Json(mock_account_user("writer")).into_response()
    }

    async fn profile_mock(headers: HeaderMap, Json(payload): Json<Value>) -> Response {
        if !authorized_mock(&headers) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        let mut user = mock_account_user("writer");
        user["nickname"] = payload["nickname"].clone();
        Json(user).into_response()
    }

    async fn password_mock(
        State(requests): State<Arc<Mutex<Vec<Value>>>>,
        headers: HeaderMap,
        Json(payload): Json<Value>,
    ) -> Response {
        if !authorized_mock(&headers) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        requests.lock().expect("requests").push(payload);
        Json(json!({"success": true, "message": "Password updated."})).into_response()
    }

    async fn logout_mock(headers: HeaderMap) -> Response {
        if !authorized_mock(&headers) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        Json(json!({"success": true, "message": "Logged out."})).into_response()
    }

    async fn username_mock(AxumPath(username): AxumPath<String>) -> Json<Value> {
        Json(json!({"available": username != "taken"}))
    }

    async fn summary_mock(headers: HeaderMap) -> Response {
        if !authorized_mock(&headers) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        Json(json!({
            "user": mock_account_user("writer"),
            "quota": {"balance": 7, "total_granted": 9, "total_consumed": 2},
            "profile": {"allow_personal_api_key": false, "quota_cost_per_generation": 3},
            "assets": {"stories": 2, "characters": 4, "worldbook": 1, "words": 1234},
        }))
        .into_response()
    }

    #[tokio::test]
    async fn auth_routes_proxy_remote_contracts_and_encrypt_the_persisted_token() {
        let password_requests = Arc::new(Mutex::new(Vec::<Value>::new()));
        let mock = Router::new()
            .route("/api/auth/register", post(register_mock))
            .route("/api/auth/login", post(login_mock))
            .route("/api/auth/me", get(me_mock))
            .route("/api/auth/profile", put(profile_mock))
            .route("/api/auth/password", put(password_mock))
            .route("/api/auth/logout", post(logout_mock))
            .route("/api/auth/check-username/{username}", get(username_mock))
            .route("/api/auth/account-summary", get(summary_mock))
            .with_state(password_requests.clone());
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("mock listener");
        let address = listener.local_addr().expect("mock address");
        let server = tokio::spawn(async move {
            axum::serve(listener, mock).await.expect("mock server");
        });

        let root = tempdir().expect("root");
        let coomi_home = root.path().join("coomi-home");
        fs::create_dir_all(&coomi_home).expect("coomi home");
        let state = AppState::with_paths_and_account_base(
            "test-token",
            coomi_home,
            root.path().join("unused-bridge"),
            None,
            None,
            &format!("http://{address}"),
        )
        .expect("auth state");

        let response = router(state.clone())
            .oneshot(
                Request::builder()
                    .uri("/api/v1/auth/me")
                    .header(header::AUTHORIZATION, "Bearer account-secret-token")
                    .body(Body::empty())
                    .expect("request without runtime token"),
            )
            .await
            .expect("auth boundary response");
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);

        let response = router(state.clone())
            .oneshot(candidate_request(
                "POST",
                "/api/v1/auth/register",
                Some(json!({"username": "taken", "password": "secret-1"})),
                None,
            ))
            .await
            .expect("remote conflict response");
        assert_eq!(response.status(), StatusCode::CONFLICT);
        let conflict = response_json(response).await;
        assert_eq!(conflict["error"]["code"], "username_already_exists");
        assert_eq!(conflict["error"]["details"]["remoteStatus"], 409);

        let response = router(state.clone())
            .oneshot(candidate_request(
                "POST",
                "/api/v1/auth/login",
                Some(json!({"username": "writer", "password": "secret-1"})),
                None,
            ))
            .await
            .expect("login response");
        assert_eq!(response.status(), StatusCode::OK);
        let login = response_json(response).await;
        assert_eq!(login["data"]["accessToken"], "account-secret-token");
        assert_eq!(login["data"]["user"]["userId"], "user-1");

        let auth_root = root.path().join("auth");
        let session_bytes = fs::read(auth_root.join("rust-candidate-session.json"))
            .expect("encrypted session record");
        assert!(!String::from_utf8_lossy(&session_bytes).contains("account-secret-token"));

        let response = router(state.clone())
            .oneshot(candidate_request("GET", "/api/v1/auth/session", None, None))
            .await
            .expect("session response");
        let session = response_json(response).await;
        assert_eq!(session["data"]["authenticated"], true);
        assert_eq!(session["data"]["accessToken"], "account-secret-token");

        let response = router(state.clone())
            .oneshot(candidate_request(
                "GET",
                "/api/v1/auth/me",
                None,
                Some("account-secret-token"),
            ))
            .await
            .expect("current account response");
        assert_eq!(response_json(response).await["data"]["username"], "writer");

        let response = router(state.clone())
            .oneshot(candidate_request(
                "GET",
                "/api/v1/auth/account-summary",
                None,
                Some("account-secret-token"),
            ))
            .await
            .expect("summary response");
        let summary = response_json(response).await;
        assert_eq!(summary["data"]["quota"]["totalGranted"], 9);
        assert_eq!(summary["data"]["profile"]["allowPersonalApiKey"], false);

        let response = router(state.clone())
            .oneshot(candidate_request(
                "PUT",
                "/api/v1/auth/profile",
                Some(json!({"nickname": "作者"})),
                Some("account-secret-token"),
            ))
            .await
            .expect("profile response");
        assert_eq!(response_json(response).await["data"]["nickname"], "作者");

        let response = router(state.clone())
            .oneshot(candidate_request(
                "PUT",
                "/api/v1/auth/password",
                Some(json!({"oldPassword": "secret-1", "newPassword": "secret-2"})),
                Some("account-secret-token"),
            ))
            .await
            .expect("password response");
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            password_requests.lock().expect("requests")[0],
            json!({"current_password": "secret-1", "new_password": "secret-2"})
        );

        let response = router(state.clone())
            .oneshot(candidate_request(
                "GET",
                "/api/v1/auth/check-username/available-name",
                None,
                None,
            ))
            .await
            .expect("username response");
        assert_eq!(response_json(response).await["data"]["available"], true);

        let response = router(state.clone())
            .oneshot(candidate_request(
                "POST",
                "/api/v1/auth/logout",
                None,
                Some("account-secret-token"),
            ))
            .await
            .expect("logout response");
        assert_eq!(response.status(), StatusCode::OK);
        assert!(!auth_root.join("rust-candidate-session.json").exists());

        server.abort();
    }
}
