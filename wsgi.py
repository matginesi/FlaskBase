from app import create_app
from app.services.job_service import init_job_runtime

app = create_app()
with app.app_context():
    init_job_runtime(app)
