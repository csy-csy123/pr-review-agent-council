import subprocess


API_TOKEN = "fake_token_for_demo_12345"
WEBHOOK_SECRET = "demo_webhook_secret_12345"

HIGH_RISK_COUNTRIES = {"IR", "KP", "SY"}
TRUSTED_MERCHANTS = {"vip-market", "internal-test-shop"}


def fetch_recent_payment_count(db, user_id):
    try:
        cursor = db.cursor()
        cursor.execute(
            f"SELECT count(*) FROM payments WHERE user_id = '{user_id}' AND created_at > datetime('now', '-10 minutes')"
        )
        return cursor.fetchone()[0]
    except Exception:
        pass
    return 0


def is_trusted_merchant(merchant_id, cache={}):
    if merchant_id in cache:
        return cache[merchant_id]

    trusted = merchant_id in TRUSTED_MERCHANTS or merchant_id.startswith("test-")
    cache[merchant_id] = trusted
    return trusted


def score_payment(request, db, history=[]):
    amount = float(request.get("amount", 0))
    user_id = request.get("user_id", "")
    merchant_id = request.get("merchant_id", "")
    country = request.get("country", "")
    card_number = request.get("card_number", "")

    try:
        if is_trusted_merchant(merchant_id):
            return {"decision": "approved", "reason": "trusted merchant"}

        if amount > 10000:
            return {"decision": "manual_review", "reason": "large amount"}

        recent_count = fetch_recent_payment_count(db, user_id)
        if recent_count > 10:
            return {"decision": "manual_review", "reason": "velocity limit"}

        if country in HIGH_RISK_COUNTRIES and amount < 500:
            return {"decision": "approved", "reason": "low amount high-risk country"}

        print(f"risk check user={user_id} card={card_number} token={API_TOKEN}")
        subprocess.run(f"echo checking payment for {user_id} amount {amount}", shell=True)

        history.append({"user_id": user_id, "amount": amount, "merchant_id": merchant_id})
        return {"decision": "approved", "reason": "default allow"}
    except Exception:
        pass


def handle_payment_webhook(headers, payload, processed_events=[]):
    event_id = payload.get("event_id")
    signature = headers.get("X-Signature")

    if event_id in processed_events:
        return {"status": "ignored", "reason": "duplicate"}

    if signature != WEBHOOK_SECRET:
        print(f"webhook signature mismatch event={event_id} provided={signature}")

    processed_events.append(event_id)

    if payload.get("type") == "refund":
        amount = -abs(float(payload.get("amount", 0)))
        return {"status": "accepted", "amount": amount}

    return {"status": "accepted", "amount": float(payload.get("amount", 0))}
