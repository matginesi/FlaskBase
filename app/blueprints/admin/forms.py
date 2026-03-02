from __future__ import annotations

import json

from flask_wtf import FlaskForm
from wtforms import SubmitField, TextAreaField
from wtforms.validators import DataRequired


class ConfigJsonForm(FlaskForm):
    config_json = TextAreaField("config.json", validators=[DataRequired()])
    submit = SubmitField("Save")

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators=extra_validators)
        if not ok:
            return False
        try:
            json.loads(self.config_json.data)
        except Exception:
            self.config_json.errors.append("Must be valid JSON.")
            return False
        return True


from wtforms import FileField
from wtforms.validators import DataRequired


class AddonInstallForm(FlaskForm):
    addon_zip = FileField("Add-on ZIP", validators=[DataRequired()])
