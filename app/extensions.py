from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

# Core extensions

db = SQLAlchemy()
migrate = Migrate()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = ""  # nessun flash automatico al redirect
login_manager.login_message_category = "warning"

csrf = CSRFProtect()

limiter = Limiter(key_func=get_remote_address, default_limits=[])
