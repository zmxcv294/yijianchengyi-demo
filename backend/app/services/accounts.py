"""Local account workspace, hashed credentials and explicit development verification."""

import hashlib
import hmac
import re
import secrets
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

from app.errors import ApiError
from app.services.studio import now, require


def initialize(database):
    with database.transaction() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, address TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, nickname TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin','reviewer')),
                banned INTEGER NOT NULL DEFAULT 0 CHECK(banned IN (0,1)),
                workspace_id TEXT NOT NULL REFERENCES sessions(id), created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_sessions (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                account_id TEXT NOT NULL REFERENCES accounts(id)
            );
            CREATE TABLE IF NOT EXISTS anonymous_returns (
                login_session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                anonymous_session_id TEXT NOT NULL REFERENCES sessions(id),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_codes (
                id TEXT PRIMARY KEY, address TEXT NOT NULL, purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                address TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
                window_started TEXT NOT NULL, blocked_until TEXT
            );
        """)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(anonymous_returns)")
        }
        if "expires_at" not in columns:
            # Old pointers have no trustworthy age. Fail closed instead of
            # granting an unbounded capability to an anonymous workspace.
            connection.execute(
                "ALTER TABLE anonymous_returns ADD COLUMN expires_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'"
            )


def normalized_address(address):
    value = address.strip().lower()
    require(
        bool(re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}\.[^\s@]{2,30}|1[3-9]\d{9}", value)),
        422,
        "VALIDATION_ERROR",
        "请输入有效邮箱或中国大陆手机号",
    )
    return value


def password_hash(password):
    require(8 <= len(password) <= 128, 422, "VALIDATION_ERROR", "密码长度须为 8 至 128 个字符")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000)
    return f"pbkdf2_sha256$260000${salt}${digest.hex()}"


def password_matches(password, stored):
    try:
        algorithm, rounds, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256" or not 100000 <= int(rounds) <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(rounds)
        ).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


# A timing-equivalent comparison for missing accounts; not a usable credential.
_DUMMY_PASSWORD_HASH = password_hash(secrets.token_urlsafe(32))
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def account_view(row):
    return {
        "id": row["id"],
        "address": row["address"],
        "nickname": row["nickname"],
        "role": row["role"],
        "banned": bool(row["banned"]),
        "createdAt": row["created_at"],
    }


def current_account(connection, session_id):
    return connection.execute(
        "SELECT a.* FROM accounts a JOIN account_sessions s ON s.account_id=a.id WHERE s.session_id=?",
        (session_id,),
    ).fetchone()


def require_account(connection, session_id, roles=None):
    account = current_account(connection, session_id)
    require(account is not None, 401, "LOGIN_REQUIRED", "请先登录")
    require(not account["banned"], 403, "ACCOUNT_BANNED", "账号已停用，请联系管理员")
    if roles is not None:
        require(account["role"] in roles, 403, "FORBIDDEN", "当前账号没有执行此操作的权限")
    return account


def revoke_sessions(connection, account_id):
    # Expire tokens before removing mappings: an account workspace must never
    # become an accessible anonymous session after revocation.
    connection.execute(
        "UPDATE sessions SET expires_at=? WHERE id IN "
        "(SELECT session_id FROM account_sessions WHERE account_id=?)",
        (now(), account_id),
    )
    connection.execute("DELETE FROM account_sessions WHERE account_id=?", (account_id,))


def workspace(connection, session_id):
    # Direct Studio unit tests may use the v1 database without account extensions.
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='account_sessions'"
    ).fetchone():
        return session_id
    account = current_account(connection, session_id)
    if account:
        require(not account["banned"], 403, "ACCOUNT_BANNED", "账号已停用，请联系管理员")
        return account["workspace_id"]
    return session_id


def raw_session(service, token):
    require(bool(token), 401, "SESSION_REQUIRED", "请先建立会话")
    with service.db.transaction() as connection:
        row = connection.execute(
            "SELECT id FROM sessions WHERE token_hash=? AND expires_at>?",
            (hashlib.sha256(token.encode()).hexdigest(), now()),
        ).fetchone()
    require(row is not None, 401, "SESSION_REQUIRED", "会话已过期，请重新建立会话")
    return row["id"]


def issue_code(service, address, purpose):
    address = normalized_address(address)
    settings = service.settings
    development = settings.verification_mode == "development"
    require(
        not (development and settings.app_env == "production"),
        503,
        "VERIFICATION_NOT_CONFIGURED",
        "生产环境尚未配置验证码发送服务",
    )
    if not development:
        require("@" in address, 503, "SMS_NOT_CONFIGURED", "短信服务尚未配置，可使用邮箱验证")
        require(
            all(
                [settings.smtp_host, settings.smtp_user, settings.smtp_password, settings.smtp_from]
            ),
            503,
            "VERIFICATION_NOT_CONFIGURED",
            "邮件验证码服务配置不完整",
        )
    identifier, code = str(uuid4()), f"{secrets.randbelow(1000000):06d}"
    stamp = datetime.now(UTC)
    with service.db.transaction() as connection:
        recent = connection.execute(
            "SELECT created_at FROM verification_codes WHERE address=? ORDER BY created_at DESC LIMIT 1",
            (address,),
        ).fetchone()
        require(
            not recent
            or (stamp - datetime.fromisoformat(recent["created_at"])).total_seconds() >= 60,
            429,
            "VERIFICATION_RATE_LIMIT",
            "验证码发送频繁，请等待 60 秒后重试",
        )
        connection.execute(
            "UPDATE verification_codes SET consumed=1 WHERE address=? AND purpose=?",
            (address, purpose),
        )
        connection.execute(
            "INSERT INTO verification_codes VALUES (?,?,?,?,?,?,0,0)",
            (
                identifier,
                address,
                purpose,
                hashlib.sha256(f"{identifier}:{code}".encode()).hexdigest(),
                (stamp + timedelta(minutes=10)).isoformat(),
                stamp.isoformat(),
            ),
        )
    if not development:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = (
            "一键成艺验证码",
            settings.smtp_from,
            address,
        )
        message.set_content(f"您的验证码为 {code}，10 分钟内有效。请勿向他人透露。")
        try:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=10,
                context=ssl.create_default_context(),
            ) as smtp:
                smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            with service.db.transaction() as connection:
                connection.execute("DELETE FROM verification_codes WHERE id=?", (identifier,))
            raise ApiError(503, "VERIFICATION_SEND_FAILED", "验证码发送失败，请稍后重试") from None
    response = {
        "challengeId": identifier,
        "expiresIn": 600,
        "mode": "development" if development else "smtp",
    }
    if development:
        response.update(developmentCode=code, message="开发验证码：仅供本机演示，未发送邮件或短信")
    return response


def consume_code(service, address, purpose, identifier, code):
    # Failed attempts commit before an error is raised, so retries cannot reset the counter.
    valid = False
    with service.db.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM verification_codes WHERE id=? AND address=? AND purpose=?",
            (identifier, address, purpose),
        ).fetchone()
        if row and not row["consumed"] and row["expires_at"] > now() and row["attempts"] < 5:
            valid = hmac.compare_digest(
                row["code_hash"], hashlib.sha256(f"{identifier}:{code}".encode()).hexdigest()
            )
            connection.execute(
                "UPDATE verification_codes SET attempts=attempts+1,consumed=? WHERE id=?",
                (int(valid), identifier),
            )
    require(valid, 422, "VERIFICATION_INVALID", "验证码无效、已使用、尝试过多或已过期，请重新获取")


def register(service, session_id, payload):
    address = normalized_address(payload.address)
    digest = password_hash(payload.password)
    consume_code(service, address, "register", str(payload.challenge_id), payload.code)
    with service.db.transaction() as connection:
        require_live_session(connection, session_id)
        require(
            not current_account(connection, session_id),
            409,
            "ALREADY_SIGNED_IN",
            "请先退出当前账号",
        )
        require(
            not connection.execute("SELECT 1 FROM accounts WHERE address=?", (address,)).fetchone(),
            409,
            "ACCOUNT_EXISTS",
            "此账号已注册，请登录或找回密码",
        )
        identifier = str(uuid4())
        connection.execute(
            "INSERT INTO accounts VALUES (?,?,?,?, 'user',0,?,?)",
            (identifier, address, digest, payload.nickname.strip(), session_id, now()),
        )
        connection.execute("INSERT INTO account_sessions VALUES (?,?)", (session_id, identifier))
        return account_view(current_account(connection, session_id))


def login(service, session_id, payload):
    address = normalized_address(payload.address)
    with service.db.transaction() as connection:
        attempts = connection.execute(
            "SELECT * FROM login_attempts WHERE address=?", (address,)
        ).fetchone()
        require(
            not attempts or not attempts["blocked_until"] or attempts["blocked_until"] <= now(),
            429,
            "LOGIN_RATE_LIMIT",
            "登录失败次数过多，请等待15分钟后重试，或使用验证码重置密码",
        )
        row = connection.execute("SELECT * FROM accounts WHERE address=?", (address,)).fetchone()
    # Password derivation is intentionally outside the database write lock.
    valid = password_matches(
        payload.password, row["password_hash"] if row else _DUMMY_PASSWORD_HASH
    )
    failed = False
    with service.db.transaction() as connection:
        latest = connection.execute("SELECT * FROM accounts WHERE address=?", (address,)).fetchone()
        if (
            row is None
            or not valid
            or latest is None
            or row["password_hash"] != latest["password_hash"]
        ):
            stamp = datetime.now(UTC)
            attempts = connection.execute(
                "SELECT * FROM login_attempts WHERE address=?", (address,)
            ).fetchone()
            expired = (
                not attempts
                or (stamp - datetime.fromisoformat(attempts["window_started"])).total_seconds()
                >= LOGIN_WINDOW_SECONDS
            )
            count = 1 if expired else attempts["attempts"] + 1
            start = stamp.isoformat() if expired else attempts["window_started"]
            blocked = (
                (stamp + timedelta(seconds=LOGIN_WINDOW_SECONDS)).isoformat()
                if count >= LOGIN_FAILURE_LIMIT
                else None
            )
            connection.execute(
                "INSERT OR REPLACE INTO login_attempts VALUES (?,?,?,?)",
                (address, count, start, blocked),
            )
            failed = True
        else:
            attempts = connection.execute(
                "SELECT * FROM login_attempts WHERE address=?", (address,)
            ).fetchone()
            require(
                not attempts or not attempts["blocked_until"] or attempts["blocked_until"] <= now(),
                429,
                "LOGIN_RATE_LIMIT",
                "登录失败次数过多，请等待15分钟后重试",
            )
            row = latest
        if failed:
            # Commit failed-attempt counters before raising the public error.
            pass
        else:
            return _complete_login(service, connection, session_id, row, payload)
    raise ApiError(401, "LOGIN_FAILED", "账号或密码错误")


def require_live_session(connection, session_id):
    require(
        connection.execute(
            "SELECT 1 FROM sessions WHERE id=? AND expires_at>?", (session_id, now())
        ).fetchone(),
        401,
        "SESSION_REQUIRED",
        "会话已更新或过期，请重新建立会话后重试",
    )


def _complete_login(service, connection, session_id, row, payload):
    require_live_session(connection, session_id)
    require(not row["banned"], 403, "ACCOUNT_BANNED", "账号已停用，请联系管理员")
    existing = current_account(connection, session_id)
    require(
        not existing or existing["id"] == row["id"], 409, "ALREADY_SIGNED_IN", "请先退出当前账号"
    )
    if payload.import_anonymous and session_id != row["workspace_id"] and not existing:
        from app.services.materials import generation_busy

        require(
            not generation_busy(service, session_id),
            409,
            "IMPORT_BUSY",
            "匿名工作区仍有图片生成请求，请等待结束后再导入",
        )
        require(
            not connection.execute(
                "SELECT 1 FROM jobs WHERE owner_session_id=? AND status IN ('queued','running')",
                (session_id,),
            ).fetchone(),
            409,
            "IMPORT_BUSY",
            "匿名工作区仍有生成任务，请等待任务结束后再导入",
        )
        require(
            not connection.execute(
                "SELECT 1 FROM jobs source JOIN jobs target ON source.idempotency_key=target.idempotency_key "
                "WHERE source.owner_session_id=? AND target.owner_session_id=?",
                (session_id, row["workspace_id"]),
            ).fetchone(),
            409,
            "IMPORT_CONFLICT",
            "匿名任务的请求标识与账号工作区冲突，请保留匿名数据并取消导入后登录",
        )
        has_template_drafts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_drafts'"
        ).fetchone()
        if has_template_drafts:
            require(
                not connection.execute(
                    "SELECT 1 FROM template_drafts WHERE owner_id=? AND status='pending'",
                    (session_id,),
                ).fetchone(),
                409,
                "IMPORT_BUSY",
                "匿名工作区仍有待审模板，请完成审核后再导入",
            )
        connection.execute("PRAGMA defer_foreign_keys=ON")
        for table in ("assets", "tasks", "jobs"):
            connection.execute(
                f"UPDATE {table} SET owner_session_id=? WHERE owner_session_id=?",
                (row["workspace_id"], session_id),
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone():
            # Multi-page projects follow their page tasks into the account workspace.
            connection.execute(
                "UPDATE projects SET owner_session_id=? WHERE owner_session_id=?",
                (row["workspace_id"], session_id),
            )
        if has_template_drafts:
            connection.execute(
                "UPDATE template_drafts SET owner_id=? WHERE owner_id=?",
                (row["workspace_id"], session_id),
            )
        for table, column in (("material_favorites", "asset_id"), ("notification_reads", "job_id")):
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                connection.execute(
                    f"INSERT OR IGNORE INTO {table}(owner_id,{column}) SELECT ?,{column} FROM {table} WHERE owner_id=?",
                    (row["workspace_id"], session_id),
                )
                connection.execute(f"DELETE FROM {table} WHERE owner_id=?", (session_id,))
    login_session = session_id
    if not existing and not payload.import_anonymous:
        # Preserve the anonymous workspace for an explicit return on logout.
        # It must not become the authenticated session's owner implicitly.
        login_session = str(uuid4())
        stamp = now()
        expires = (
            datetime.now(UTC) + timedelta(days=service.settings.session_ttl_days)
        ).isoformat()
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (login_session, hashlib.sha256(secrets.token_bytes(32)).hexdigest(), stamp, expires),
        )
        connection.execute(
            "INSERT INTO anonymous_returns VALUES (?,?,?)", (login_session, session_id, expires)
        )
        connection.execute("UPDATE sessions SET expires_at=? WHERE id=?", (stamp, session_id))
    connection.execute(
        "INSERT OR REPLACE INTO account_sessions VALUES (?,?)", (session_id, row["id"])
    )
    if login_session != session_id:
        connection.execute("DELETE FROM account_sessions WHERE session_id=?", (session_id,))
        connection.execute("INSERT INTO account_sessions VALUES (?,?)", (login_session, row["id"]))
    connection.execute("DELETE FROM login_attempts WHERE address=?", (row["address"],))
    return {**account_view(row), "_sessionId": login_session}


def reset_password(service, payload):
    address = normalized_address(payload.address)
    digest = password_hash(payload.password)
    consume_code(service, address, "reset", str(payload.challenge_id), payload.code)
    with service.db.transaction() as connection:
        row = connection.execute("SELECT id FROM accounts WHERE address=?", (address,)).fetchone()
        require(row is not None, 422, "RESET_FAILED", "无法重置该账号，请检查注册信息")
        connection.execute("UPDATE accounts SET password_hash=? WHERE id=?", (digest, row["id"]))
        revoke_sessions(connection, row["id"])
        connection.execute("DELETE FROM login_attempts WHERE address=?", (address,))
        return row["id"]


def take_anonymous_return(connection, login_session):
    row = connection.execute(
        "SELECT * FROM anonymous_returns WHERE login_session_id=?", (login_session,)
    ).fetchone()
    connection.execute("DELETE FROM anonymous_returns WHERE login_session_id=?", (login_session,))
    if not row or row["expires_at"] <= now():
        return None
    target = row["anonymous_session_id"]
    # A stale or corrupted pointer must never reactivate an account workspace.
    if connection.execute("SELECT 1 FROM accounts WHERE workspace_id=?", (target,)).fetchone():
        return None
    if current_account(connection, target):
        return None
    return target


def restore_anonymous(service, login_session):
    with service.db.transaction() as connection:
        return take_anonymous_token(service, connection, login_session)


def take_anonymous_token(service, connection, login_session):
    target = take_anonymous_return(connection, login_session)
    return _rotate_token(service, connection, target) if target else None


def recover_anonymous_after_revocation(service, token):
    """An expired login cookie may recover only its bounded anonymous return."""
    if not token:
        return None
    with service.db.transaction() as connection:
        row = connection.execute(
            "SELECT id FROM sessions WHERE token_hash=? AND expires_at<=?",
            (hashlib.sha256(token.encode()).hexdigest(), now()),
        ).fetchone()
        return take_anonymous_token(service, connection, row["id"]) if row else None


def rotate_token(service, session_id):
    with service.db.transaction() as connection:
        return _rotate_token(service, connection, session_id)


def _rotate_token(service, connection, session_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(days=service.settings.session_ttl_days)).isoformat()
    connection.execute(
        "UPDATE sessions SET token_hash=?,expires_at=? WHERE id=?",
        (hashlib.sha256(token.encode()).hexdigest(), expires, session_id),
    )
    return token


def change_password(service, session_id, old_password, new_password):
    digest = password_hash(new_password)
    with service.db.transaction() as connection:
        account = dict(require_account(connection, session_id))
    require(
        password_matches(old_password, account["password_hash"]),
        401,
        "PASSWORD_INVALID",
        "原密码不正确",
    )
    with service.db.transaction() as connection:
        latest = require_account(connection, session_id)
        require(
            latest["password_hash"] == account["password_hash"],
            409,
            "ACCOUNT_CHANGED",
            "账号凭据已更新，请重新登录",
        )
        connection.execute(
            "UPDATE accounts SET password_hash=? WHERE id=?", (digest, account["id"])
        )
        restore = connection.execute(
            "SELECT * FROM anonymous_returns WHERE login_session_id=?", (session_id,)
        ).fetchone()
        revoke_sessions(connection, account["id"])
    token, _ = service.create_session(None)
    raw = raw_session(service, token)
    with service.db.transaction() as connection:
        connection.execute("INSERT INTO account_sessions VALUES (?,?)", (raw, account["id"]))
        if restore:
            connection.execute(
                "DELETE FROM anonymous_returns WHERE login_session_id=?", (session_id,)
            )
            connection.execute(
                "INSERT INTO anonymous_returns VALUES (?,?,?)",
                (raw, restore["anonymous_session_id"], restore["expires_at"]),
            )
    return token, account_view(account)


def list_accounts(service, session_id, limit=50, offset=0):
    with service.db.transaction() as connection:
        require_account(connection, session_id, {"admin"})
        rows = connection.execute(
            "SELECT * FROM accounts ORDER BY created_at,id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        total = connection.execute("SELECT count(*) FROM accounts").fetchone()[0]
    return {"items": [account_view(row) for row in rows], "total": total}


def update_account(service, session_id, identifier, changes):
    require(
        changes
        and set(changes) <= {"role", "banned"}
        and all(value is not None for value in changes.values()),
        422,
        "VALIDATION_ERROR",
        "请提供角色或停用状态",
    )
    with service.db.transaction() as connection:
        require_account(connection, session_id, {"admin"})
        row = connection.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
        require(row is not None, 404, "NOT_FOUND", "未找到账号")
        role, banned = changes.get("role", row["role"]), changes.get("banned", bool(row["banned"]))
        if row["role"] == "admin" and not row["banned"] and (role != "admin" or banned):
            remaining = connection.execute(
                "SELECT count(*) FROM accounts WHERE role='admin' AND banned=0 AND id<>?",
                (identifier,),
            ).fetchone()[0]
            require(
                remaining > 0, 409, "LAST_ADMIN", "至少保留一位可用管理员，请先创建或指定其他管理员"
            )
        connection.execute(
            "UPDATE accounts SET role=?,banned=? WHERE id=?", (role, int(banned), identifier)
        )
        if banned:
            revoke_sessions(connection, identifier)
        row = connection.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
    return account_view(row)


def create_admin(service, address, password, nickname):
    """Local operator-only helper; no HTTP registration path can call this."""
    address, digest = normalized_address(address), password_hash(password)
    require(bool(nickname.strip()), 422, "VALIDATION_ERROR", "管理员昵称不能为空")
    with service.db.transaction() as connection:
        require(
            not connection.execute("SELECT 1 FROM accounts WHERE address=?", (address,)).fetchone(),
            409,
            "ACCOUNT_EXISTS",
            "账号已存在；此命令不会自动提升现有账号权限",
        )
        workspace_id, identifier = str(uuid4()), str(uuid4())
        discarded_token = secrets.token_urlsafe(32)
        expires = (
            datetime.now(UTC) + timedelta(days=service.settings.session_ttl_days)
        ).isoformat()
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (workspace_id, hashlib.sha256(discarded_token.encode()).hexdigest(), now(), expires),
        )
        connection.execute(
            "INSERT INTO accounts VALUES (?,?,?,?, 'admin',0,?,?)",
            (identifier, address, digest, nickname.strip(), workspace_id, now()),
        )
        return account_view(
            connection.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
        )
