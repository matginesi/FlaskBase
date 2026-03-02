from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, EqualTo, InputRequired, Length, Optional
from ...services.i18n import translate


class LocalizedForm(FlaskForm):
    TRANSLATION_MAP: dict[str, str] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, key in self.TRANSLATION_MAP.items():
            field = getattr(self, field_name, None)
            if field is not None and hasattr(field, "label"):
                field.label.text = translate(key, field.label.text)


class LoginForm(LocalizedForm):
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=4, max=128)])
    remember = BooleanField("Remember me")
    submit = SubmitField("Login")
    TRANSLATION_MAP = {
        "email": "auth.field_email",
        "password": "auth.field_password",
        "remember": "auth.remember_me",
        "submit": "auth.login_submit",
    }


class MfaVerifyForm(LocalizedForm):
    otp_code = StringField("Authenticator code", validators=[Optional(), Length(min=6, max=16)])
    recovery_code = StringField("Recovery code", validators=[Optional(), Length(min=8, max=64)])
    submit = SubmitField("Verify")
    TRANSLATION_MAP = {
        "otp_code": "auth.authenticator_code",
        "recovery_code": "auth.recovery_code",
        "submit": "auth.verify",
    }


class UserSettingsForm(LocalizedForm):
    name = StringField("Name", validators=[DataRequired(), Length(min=2, max=120)])
    username = StringField("Username", validators=[Optional(), Length(min=3, max=80)])
    locale = StringField("Language / Locale", validators=[Optional(), Length(max=16)])
    timezone = StringField("Timezone", validators=[Optional(), Length(max=64)])
    notes = TextAreaField("Technical notes", validators=[Optional(), Length(max=4000)])
    notification_email_enabled = BooleanField("Email notifications enabled")
    notification_security_enabled = BooleanField("Security alerts enabled")
    submit = SubmitField("Save")
    TRANSLATION_MAP = {
        "name": "auth.field_name",
        "username": "auth.field_username",
        "locale": "auth.field_locale",
        "timezone": "auth.field_timezone",
        "notes": "auth.field_notes",
        "notification_email_enabled": "auth.notifications_email_enabled",
        "notification_security_enabled": "auth.notifications_security_enabled",
        "submit": "common.save_changes",
    }


class RegistrationForm(LocalizedForm):
    name = StringField("Name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, max=128)])
    password_confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(), Length(min=8, max=128), EqualTo("password", message="Passwords do not match.")],
    )
    accept_terms = BooleanField(
        "I have read and accept the Privacy Policy and terms",
        validators=[InputRequired(message="You must accept the privacy policy and terms to sign up.")],
    )
    submit = SubmitField("Sign up")
    TRANSLATION_MAP = {
        "name": "auth.field_name",
        "email": "auth.field_email",
        "password": "auth.field_password",
        "password_confirm": "auth.field_password_confirm",
        "accept_terms": "auth.accept_terms_full",
        "submit": "auth.register_submit",
    }
