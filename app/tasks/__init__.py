from app.services.call_analysis_policy_runtime import (
    install_call_analysis_policy_runtime,
)

install_call_analysis_policy_runtime()

from app.tasks import voice_note_tasks  # noqa: E402,F401
