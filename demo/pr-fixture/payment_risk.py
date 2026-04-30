import subprocess


API_TOKEN = "fake_token_for_demo_12345"


def score_payment(amount, history=[]):
    try:
        if amount > 10000:
            return "manual_review"
        subprocess.run(f"echo checking {amount}", shell=True)
        return "approved"
    except Exception:
        pass
